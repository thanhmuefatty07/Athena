"""Real filesystem/process checks for research collection and operation ownership."""

import json
import os
import shutil
import tarfile
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cloud_experiments import lifecycle, research


class RustupBootstrapControls(unittest.TestCase):
    def test_download_timeout_is_supervised_and_never_runs_installer(self):
        from types import SimpleNamespace

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        calls = []

        def timeout_download(command, **kwargs):
            calls.append((command, kwargs))
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(Path(command[1]).name, "download-rustup-installer.py")
            self.assertEqual(command[2],
                             "https://static.rust-lang.org/rustup/archive/"
                             f"{rustup_bootstrap.RUSTUP_VERSION}/{target}/rustup-init")
            self.assertEqual(command[4], str(rustup_bootstrap.MAX_INSTALLER_BYTES))
            self.assertEqual(kwargs["timeout"], rustup_bootstrap.DOWNLOAD_TIMEOUT_SECONDS)
            self.assertEqual(kwargs["output_limit"], rustup_bootstrap.INSTALL_OUTPUT_LIMIT_BYTES)
            return SimpleNamespace(returncode=-9, stdout=b"", stderr=b"", status="TIMEOUT")

        with tempfile.TemporaryDirectory(prefix="athena-rustup-download-timeout-") as folder:
            with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: "0" * 64}), \
                    patch.object(rustup_bootstrap, "urlopen",
                                 side_effect=AssertionError("download bypassed process supervision"),
                                 create=True), \
                    patch.object(rustup_bootstrap, "run_bounded_process", side_effect=timeout_download):
                with self.assertRaisesRegex(TimeoutError, "download exceeded"):
                    rustup_bootstrap.install_rustup(target, folder)
            self.assertEqual(len(calls), 1, "timed-out download must not start the installer")
            self.assertEqual(list(Path(folder).iterdir()), [],
                             "timed-out download must clean its temporary directory")

    def test_checksum_mismatch_never_reaches_installer_execution(self):
        import hashlib
        from types import SimpleNamespace

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        payload = b"untrusted installer bytes"
        trusted_digest = hashlib.sha256(b"different trusted bytes").hexdigest()
        with tempfile.TemporaryDirectory(prefix="athena-rustup-mismatch-") as folder:
            def controlled_process(command, **kwargs):
                self.assertEqual(kwargs["timeout"], rustup_bootstrap.DOWNLOAD_TIMEOUT_SECONDS)
                Path(command[3]).write_bytes(payload)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", status="EXITED")

            with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: trusted_digest}), \
                    patch.object(rustup_bootstrap, "run_bounded_process", side_effect=controlled_process) as bounded:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    rustup_bootstrap.install_rustup(target, folder)
            bounded.assert_called_once()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_matching_checksum_runs_only_the_verified_rustup_installer(self):
        import hashlib
        import runpy
        from io import BytesIO
        from types import SimpleNamespace

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        payload = b"synthetic verified rustup installer"
        trusted_digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(prefix="athena-rustup-match-") as folder:
            expected_url = ("https://static.rust-lang.org/rustup/archive/"
                            f"{rustup_bootstrap.RUSTUP_VERSION}/{target}/rustup-init")

            def inspect_execution(argv, **kwargs):
                if argv[0] == sys.executable:
                    self.assertEqual(Path(argv[1]).name, "download-rustup-installer.py")
                    self.assertEqual(argv[2], expected_url)
                    self.assertEqual(argv[4], str(rustup_bootstrap.MAX_INSTALLER_BYTES))
                    self.assertEqual(kwargs, {
                        "timeout": rustup_bootstrap.DOWNLOAD_TIMEOUT_SECONDS,
                        "output_limit": rustup_bootstrap.INSTALL_OUTPUT_LIMIT_BYTES,
                    })
                    with patch.object(sys, "argv", argv[1:]), \
                            patch("urllib.request.urlopen", return_value=BytesIO(payload)) as download:
                        runpy.run_path(argv[1], run_name="__main__")
                    download.assert_called_once_with(expected_url, timeout=60)
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", status="EXITED")
                self.assertEqual(argv[:4], ["runuser", "-u", "athena-research", "--"])
                installer = Path(argv[4])
                self.assertEqual(installer.read_bytes(), payload)
                if os.name != "nt":
                    self.assertTrue(installer.stat().st_mode & 0o111)
                self.assertEqual(argv[5:], ["-y", "--profile", "minimal", "--default-toolchain",
                                             "1.99.1", "--no-modify-path"])
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 1800)
                self.assertGreater(kwargs["output_limit"], 0)
                self.assertLessEqual(kwargs["output_limit"], 2 * 1024 ** 2)
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", status="EXITED")

            with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: trusted_digest}), \
                    patch.object(rustup_bootstrap, "run_bounded_process", side_effect=inspect_execution) as execute, \
                    patch.object(rustup_bootstrap.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)) as unbounded:
                rustup_bootstrap.install_rustup(target, folder)
            self.assertEqual(execute.call_count, 2, "download and installer must both use the bounded runner")
            unbounded.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_installer_rejects_timeout_output_limit_and_nonzero_exit(self):
        import hashlib
        from types import SimpleNamespace

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        payload = b"synthetic verified rustup installer"
        trusted_digest = hashlib.sha256(payload).hexdigest()
        cases = (("TIMEOUT", -9, TimeoutError), ("OUTPUT_LIMIT", -9, RuntimeError),
                 ("EXITED", 7, subprocess.CalledProcessError))
        for status, returncode, expected_error in cases:
            with self.subTest(status=status, returncode=returncode), \
                    tempfile.TemporaryDirectory(prefix="athena-rustup-failure-") as folder:
                result = SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"", status=status)
                def controlled_process(command, **kwargs):
                    if command[0] == sys.executable:
                        Path(command[3]).write_bytes(payload)
                        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"", status="EXITED")
                    return result

                with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: trusted_digest}), \
                        patch.object(rustup_bootstrap, "run_bounded_process", side_effect=controlled_process) as bounded, \
                        patch.object(rustup_bootstrap.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0)) as unbounded:
                    with self.assertRaises(expected_error):
                        rustup_bootstrap.install_rustup(target, folder)
                self.assertEqual(bounded.call_count, 2)
                installer_call = bounded.call_args_list[-1]
                self.assertLessEqual(installer_call.kwargs["timeout"], 1800)
                self.assertLessEqual(installer_call.kwargs["output_limit"], 2 * 1024 ** 2)
                unbounded.assert_not_called()
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_unsupported_host_architecture_fails_before_download(self):
        from cloud_experiments import rustup_bootstrap

        with tempfile.TemporaryDirectory(prefix="athena-rustup-arch-") as folder:
            with patch.object(rustup_bootstrap, "run_bounded_process") as download:
                with self.assertRaisesRegex(ValueError, "Unsupported Linux architecture"):
                    rustup_bootstrap.install_rustup(None, folder, machine="mips64")
            download.assert_not_called()


class LifecycleControls(unittest.TestCase):
    def test_required_process_isolation_builder_is_fail_closed_and_preserves_argv(self):
        isolation = lifecycle.process_isolation
        with tempfile.TemporaryDirectory(prefix="athena-unshare-builder-") as folder:
            executable = Path(folder) / "unshare"
            executable.write_bytes(b"fixture")
            with patch.object(isolation.sys, "platform", "linux"), \
                    patch.object(isolation.shutil, "which", return_value=str(executable)), \
                    patch.object(isolation.os, "access", return_value=True):
                command = isolation.command_argv(["cargo", "test", "--locked"])
            separator = command.index("--")
            self.assertEqual(command[0], str(executable.resolve()))
            self.assertEqual(command[1:separator],
                             ["--user", "--map-root-user", "--net", "--pid", "--fork",
                              "--mount-proc", "--kill-child"])
            self.assertEqual(command[separator + 1:separator + 6],
                             [sys.executable, "-B", str(Path(isolation.__file__).resolve()),
                              "--athena-isolated-init", "--"])
            self.assertEqual(command[separator + 6:], ["cargo", "test", "--locked"])
            with patch.object(isolation.sys, "platform", "linux"), \
                    patch.object(isolation.shutil, "which", return_value=str(executable)), \
                    patch.object(isolation.os, "access", return_value=True):
                self.assertEqual(isolation.CARGO_FETCH_ARGS,
                                 ("fetch", "--offline", "--locked", "--target", "x86_64-unknown-linux-gnu"))
                fetch = isolation.command_argv(["cargo", *isolation.CARGO_FETCH_ARGS],
                                               network_mode="offline-cargo-fetch")
            fetch_separator = fetch.index("--")
            self.assertEqual(fetch[1:fetch_separator],
                             ["--user", "--map-root-user", "--net", "--pid", "--fork",
                              "--mount-proc", "--kill-child"])
            self.assertEqual(fetch[fetch_separator + 1:fetch_separator + 6],
                             [sys.executable, "-B", str(Path(isolation.__file__).resolve()),
                              "--athena-isolated-init", "--"])
            self.assertEqual(fetch[fetch_separator + 6:], ["cargo", *isolation.CARGO_FETCH_ARGS])
            with self.assertRaisesRegex(ValueError, "only the exact locked"):
                isolation.command_argv(["cargo", "build"], network_mode="offline-cargo-fetch")
            with self.assertRaisesRegex(ValueError, "only the exact locked"):
                isolation.command_argv(
                    ["cargo", *isolation.CARGO_FETCH_ARGS[:-1], "aarch64-unknown-linux-gnu"],
                    network_mode="offline-cargo-fetch")
            with self.assertRaisesRegex(RuntimeError, "requires process containment"):
                isolation.command_argv(["cargo", *isolation.CARGO_FETCH_ARGS], required=False,
                                       network_mode="offline-cargo-fetch")
            with self.assertRaisesRegex(ValueError, "Unknown child network policy"):
                isolation.command_argv(["cargo", *isolation.CARGO_FETCH_ARGS],
                                       network_mode="locked-cargo-fetch")
            self.assertEqual(isolation.command_argv(["cargo", "test"], required=False),
                             ["cargo", "test"])
        with patch.object(isolation.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "available only on Linux"):
                isolation.command_argv(["cargo", "test"])
        with tempfile.TemporaryDirectory(prefix="athena-isolation-resume-") as folder:
            path = Path(folder) / "run"
            original = research.ResearchRun(path, "isolation", "d" * 64, {},
                                            isolate_children=True)
            original.sample_stream.close()
            resumed = research.ResearchRun(path, "isolation", "d" * 64, {}, resume=True,
                                            isolate_children=True)
            resumed.sample_stream.close()
            with self.assertRaisesRegex(ValueError, "process-isolation policy changed"):
                research.ResearchRun(path, "isolation", "d" * 64, {}, resume=True,
                                     isolate_children=False)

    def test_native_executor_freezes_budget_bytes_before_output_lock(self):
        import hashlib
        from contextlib import contextmanager

        with tempfile.TemporaryDirectory(prefix="athena-native-budget-snapshot-") as folder:
            root = Path(folder).resolve()
            source = root / "source"
            source.mkdir()
            budget_path = root / "budget.json"
            original = b'{"deadline_seconds":3600,"reviewed":"original"}'
            budget_path.write_bytes(original)
            expected_budget = {"deadline_seconds": 3600, "reviewed": "original"}
            captured = {}

            @contextmanager
            def change_budget_during_lock(_path):
                budget_path.write_bytes(b'{"deadline_seconds":7200,"reviewed":"changed"}')
                yield

            def capture_execution_inputs(*_args, **kwargs):
                captured.update(kwargs)
                return "captured frozen input"

            with patch.object(research, "_analysis_lock", side_effect=change_budget_during_lock), \
                    patch.object(research, "_execute_native_locked", side_effect=capture_execution_inputs):
                result = research.execute_native(source, root / "spec.json", root / "output",
                    budget_path=budget_path, campaign_deadline_unix=time.time() + 3600)

            self.assertEqual(result, "captured frozen input")
            self.assertEqual(captured.get("budget"), expected_budget)
            self.assertEqual(captured.get("budget_sha256"), hashlib.sha256(original).hexdigest())

    def test_supervisor_rejects_changed_budget_before_native_manifest_exists(self):
        with tempfile.TemporaryDirectory(prefix="athena-supervisor-budget-") as folder:
            root = Path(folder).resolve()
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "owned.txt").write_bytes(b"frozen input")
            spec = {"protocol": research.PROTOCOL,
                    "files": {"owned.txt": lifecycle.protocol.sha256_file(source / "owned.txt")},
                    "limits": {"export_bytes": 1048576}}
            spec["study_id"] = research.identity(spec)
            research.campaign.write_json(root / "spec.json", spec)
            budget_path = root / "budget.json"
            research.campaign.write_json(budget_path, {"reviewed": "original", "deadline_seconds": 3600})
            job = {"source": str(source), "spec": str(root / "spec.json"),
                   "output": str(output), "budget": str(budget_path)}
            research.campaign.write_json(root / "job.json", job)
            state_path = root / "output.supervisor.json"
            research.campaign.write_json(state_path, {
                "job_digest": lifecycle.digest(job), "attempts": 0,
                "campaign_deadline_unix": time.time() + 3600,
                "budget_sha256": lifecycle.protocol.sha256_file(budget_path)})
            frozen_state = state_path.read_bytes()
            research.campaign.write_json(budget_path, {"reviewed": "changed", "deadline_seconds": 3600})

            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)

            with patch.object(lifecycle, "Path", side_effect=configured_path), \
                    patch.object(research, "execute_native") as execute_native:
                with self.assertRaisesRegex(ValueError, "budget identity changed"):
                    lifecycle.supervise(root / "job.json")

            execute_native.assert_not_called()
            self.assertEqual(state_path.read_bytes(), frozen_state)
            self.assertFalse((output / "manifest.json").exists())

    def test_supervisor_rechecks_frozen_budget_before_native_dispatch(self):
        with tempfile.TemporaryDirectory(prefix="athena-supervisor-budget-race-") as folder:
            root = Path(folder).resolve()
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "owned.txt").write_bytes(b"frozen input")
            spec = {"protocol": research.PROTOCOL,
                    "files": {"owned.txt": lifecycle.protocol.sha256_file(source / "owned.txt")},
                    "limits": {"export_bytes": 1048576}}
            spec["study_id"] = research.identity(spec)
            research.campaign.write_json(root / "spec.json", spec)
            budget_path = root / "budget.json"
            research.campaign.write_json(budget_path, {"reviewed": "original", "deadline_seconds": 3600})
            job = {"source": str(source), "spec": str(root / "spec.json"),
                   "output": str(output), "budget": str(budget_path)}
            research.campaign.write_json(root / "job.json", job)
            original_verify = research.verify_snapshot

            def change_budget_after_snapshot(source_path, design):
                result = original_verify(source_path, design)
                research.campaign.write_json(
                    budget_path, {"reviewed": "changed", "deadline_seconds": 3600})
                return result

            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)

            with patch.object(lifecycle, "Path", side_effect=configured_path), \
                    patch.object(research, "verify_snapshot", side_effect=change_budget_after_snapshot), \
                    patch.object(research, "execute_native") as execute_native:
                with self.assertRaisesRegex(ValueError, "budget changed before native dispatch"):
                    lifecycle.supervise(root / "job.json")

            execute_native.assert_not_called()
            state = lifecycle.read_json(root / "output.supervisor.json")
            self.assertEqual(state["attempts"], 0)
            self.assertFalse((output / "manifest.json").exists())

    def test_active_cargo_build_is_stopped_when_aggregate_tree_exceeds_limit(self):
        temporary_root = None
        try:
            with tempfile.TemporaryDirectory(prefix="athena-active-build-limit-") as folder:
                root = Path(folder).resolve()
                temporary_root = root
                cache = root / "cargo-target"
                started = root / "started"
                completed = root / "completed"
                spec = {"limits": {"build_bytes": 1024, "binary_bytes": 4096,
                                   "analysis_bytes": 4096, "disk_reserve_bytes": 0,
                                   "command_output_bytes": 4096, "log_bytes": 4096,
                                   "metadata_bytes": 4096, "wall_seconds": 20}}
                run = research.ResearchRun(root / "run", "control", "a" * 64, spec)
                code = ("from pathlib import Path;import sys,time;"
                        "target=Path(sys.argv[1]);target.mkdir(parents=True,exist_ok=True);"
                        "(target/'artifact.bin').write_bytes(b'x'*2048);"
                        "Path(sys.argv[2]).write_text('running');time.sleep(5);"
                        "Path(sys.argv[3]).write_text('finished')")

                def local_cargo(command):
                    return [sys.executable, "-B", "-c", code, *map(str, command[1:])]

                try:
                    with patch.object(research, "_network_namespace_command",
                                      side_effect=local_cargo):
                        with self.assertRaises(RuntimeError):
                            research._run_isolated_cargo(
                                run, ["cargo", str(cache), str(started), str(completed)], cwd=root, timeout=10)
                    self.assertTrue(started.is_file())
                    self.assertFalse(completed.exists(), "over-limit build finished before supervision stopped it")
                    self.assertEqual(run.manifest["commands"][-1]["status"], "BUILD_CAPACITY")
                    self.assertEqual(run.manifest["storage_probe"]["build_bytes"], 2048)
                finally:
                    run.sample_stream.close()
        finally:
            if temporary_root is not None:
                self.assertFalse(temporary_root.exists(), "owned temporary build tree was not cleaned")

    def test_cargo_that_exits_after_capacity_breach_records_final_guard_result(self):
        temporary_root = None
        try:
            with tempfile.TemporaryDirectory(prefix="athena-final-build-limit-") as folder:
                root = Path(folder).resolve()
                temporary_root = root
                cache = root / "cargo-target"
                spec = {"limits": {"build_bytes": 1024, "binary_bytes": 4096,
                                   "analysis_bytes": 4096, "disk_reserve_bytes": 0,
                                   "command_output_bytes": 4096, "log_bytes": 4096,
                                   "metadata_bytes": 4096, "wall_seconds": 20}}
                run = research.ResearchRun(root / "run", "control", "b" * 64, spec)
                code = ("from pathlib import Path;import sys;"
                        "target=Path(sys.argv[1]);target.mkdir(parents=True,exist_ok=True);"
                        "(target/'artifact.bin').write_bytes(b'x'*2048)")

                def local_cargo(command):
                    return [sys.executable, "-B", "-c", code, *map(str, command[1:])]

                try:
                    with patch.object(research, "_network_namespace_command",
                                      side_effect=local_cargo):
                        with self.assertRaises(RuntimeError):
                            research._run_isolated_cargo(
                                run, ["cargo", str(cache)], cwd=root, timeout=10)
                    self.assertEqual(run.manifest["commands"][-1]["status"], "BUILD_CAPACITY")
                    self.assertEqual(run.manifest["commands"][-1]["storage_probe"]["build_bytes"], 2048)
                finally:
                    run.sample_stream.close()
        finally:
            if temporary_root is not None:
                self.assertFalse(temporary_root.exists(), "owned final-sample tree was not cleaned")

    def test_read_json_rejects_duplicate_members_and_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory(prefix="athena-strict-json-") as folder:
            root = Path(folder)
            duplicate = root / "duplicate.json"
            duplicate.write_bytes(b'{"status":"INCOMPLETE","status":"COMPLETE"}')
            nonfinite = root / "nonfinite.json"
            nonfinite.write_bytes(b'{"cost":NaN}')
            for path in (duplicate, nonfinite):
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    lifecycle.read_json(path)

    def test_collection_freezes_nested_caller_binding_before_reuse(self):
        with tempfile.TemporaryDirectory(prefix="athena-binding-owner-") as folder:
            root = Path(folder)
            journal = lifecycle.CollectionJournal(root, {"study": "frozen"}, 4096, "session")
            descriptor = {"channel": "screen", "binding": {"randomization": {"seed": 91}}, "block": 0}
            list(journal.collect(descriptor, lambda: iter([{"value": 10}])))
            saved = journal.binding_path.read_bytes()
            descriptor["binding"]["randomization"]["seed"] = 92
            with self.assertRaisesRegex(ValueError, "settings changed"):
                list(journal.collect({**descriptor, "block": 1}, lambda: iter([{"value": 20}])))
            self.assertEqual(journal.binding_path.read_bytes(), saved)
            self.assertEqual(journal.bindings["channels"]["screen"]["randomization"]["seed"], 91)
            self.assertEqual(len(journal.shards), 1)

    def test_completed_supervisor_output_is_bound_to_the_frozen_job(self):
        with tempfile.TemporaryDirectory(prefix="athena-job-identity-") as folder:
            root = Path(folder).resolve()
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "owned.txt").write_bytes(b"frozen input")
            spec = {"protocol": research.PROTOCOL, "files": {"owned.txt": lifecycle.protocol.sha256_file(source / "owned.txt")},
                    "limits": {"export_bytes": 1048576}}
            spec["study_id"] = research.identity(spec)
            job = {"source": str(source), "spec": str(root / "spec.json"),
                   "output": str(output), "budget": str(root / "budget.json")}
            research.campaign.write_json(root / "spec.json", spec)
            research.campaign.write_json(root / "budget.json", {"reviewed": "synthetic", "deadline_seconds": 3600})
            research.campaign.write_json(root / "job.json", job)
            manifest = {"status": "COMPLETE", "bundle_id": "b" * 64, "spec": spec}
            research.campaign.write_json(output / "manifest.json", manifest)
            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                with self.assertRaisesRegex(ValueError, "identity|job|study"):
                    lifecycle.supervise(root / "job.json")
            self.assertEqual(list(root.glob("output-evidence*.tgz")), [])
            self.assertEqual(lifecycle.read_json(output / "manifest.json"), manifest)
            state = lifecycle.read_json(root / "output.supervisor.json")
            manifest.update(bundle_id=spec["study_id"], execution_request={"pilot_only": False, "variants": None,
                "budget_sha256": lifecycle.protocol.sha256_file(root / "budget.json")},
                campaign_deadline_unix=state["campaign_deadline_unix"], shutdown_seconds=600,
                export_deadline_unix=time.time() - 1)
            research.campaign.write_json(output / "manifest.json", manifest)
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                self.assertEqual(lifecycle.supervise(root / "job.json"), 78)
            self.assertEqual(list(root.glob("output-evidence*.tgz")), [])
            held = lifecycle.read_json(root / "output.supervisor.json")
            self.assertEqual(held["status"], "HOLD")
            self.assertEqual(held["export_error_type"], "TimeoutError")
            manifest["export_deadline_unix"] = time.time() + 30
            research.campaign.write_json(output / "manifest.json", manifest)
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                self.assertEqual(lifecycle.supervise(root / "job.json"), 0)
            completed = lifecycle.read_json(root / "output.supervisor.json")
            archive = root / completed["export_archive_name"]
            original = archive.read_bytes()
            research.campaign.write_json(root / "budget.json", {"reviewed": "changed budget", "deadline_seconds": 3600})
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                with self.assertRaisesRegex(ValueError, "identity|job|study"):
                    lifecycle.supervise(root / "job.json")
            self.assertEqual(archive.read_bytes(), original)

    def test_supervisor_requires_finalized_export_before_reporting_completion(self):
        with tempfile.TemporaryDirectory(prefix="athena-supervisor-completion-") as folder:
            root = Path(folder).resolve()
            source, output = root / "source", root / "output"
            source.mkdir()
            (source / "owned.txt").write_bytes(b"frozen input")
            spec = {"protocol": research.PROTOCOL,
                    "files": {"owned.txt": lifecycle.protocol.sha256_file(source / "owned.txt")},
                    "limits": {"export_bytes": 1048576}}
            spec["study_id"] = research.identity(spec)
            research.campaign.write_json(root / "spec.json", spec)
            research.campaign.write_json(root / "budget.json", {"deadline_seconds": 3600})
            research.campaign.write_json(root / "job.json", {
                "source": str(source), "spec": str(root / "spec.json"),
                "output": str(output), "budget": str(root / "budget.json")})

            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)

            def incomplete_native(*_args, **_kwargs):
                output.mkdir()
                research.campaign.write_json(output / "manifest.json", {
                    "status": "RUNNING", "bundle_id": spec["study_id"], "spec": spec})

            with patch.object(lifecycle, "Path", side_effect=configured_path), patch.object(
                    research, "execute_native", side_effect=incomplete_native):
                self.assertEqual(lifecycle.supervise(root / "job.json"), 78)
            state = lifecycle.read_json(root / "output.supervisor.json")
            self.assertEqual(state["status"], "HOLD")
            self.assertEqual(state["export_status"], "RUN_NOT_FINALIZED")
            self.assertEqual(list(root.glob("output-evidence*.tgz")), [])

    def test_supervisor_does_not_rebase_expired_campaign_deadline_on_restart(self):
        with tempfile.TemporaryDirectory(prefix="athena-supervisor-deadline-") as folder:
            root = Path(folder).resolve()
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "owned.txt").write_bytes(b"frozen input")
            spec = {"protocol": research.PROTOCOL,
                    "files": {"owned.txt": lifecycle.protocol.sha256_file(source / "owned.txt")},
                    "limits": {"export_bytes": 1048576}}
            spec["study_id"] = research.identity(spec)
            budget_path = root / "budget.json"
            research.campaign.write_json(budget_path, {"reviewed": "synthetic", "deadline_seconds": 3600})
            job = {"source": str(source), "spec": str(root / "spec.json"),
                   "output": str(output), "budget": str(budget_path)}
            research.campaign.write_json(root / "spec.json", spec)
            research.campaign.write_json(root / "job.json", job)
            expired_deadline = time.time() - 1
            request = {"pilot_only": False, "variants": None,
                       "budget_sha256": lifecycle.protocol.sha256_file(budget_path)}
            research.campaign.write_json(output / "manifest.json", {
                "status": "COMPLETE", "bundle_id": spec["study_id"], "spec": spec,
                "execution_request": request, "campaign_deadline_unix": expired_deadline,
                "export_deadline_unix": time.time() + 30, "shutdown_seconds": 600})
            research.campaign.write_json(root / "output.supervisor.json", {
                "job_digest": lifecycle.digest(job), "attempts": 1,
                "campaign_deadline_unix": expired_deadline,
                "budget_sha256": lifecycle.protocol.sha256_file(budget_path)})

            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)

            with patch.object(lifecycle, "Path", side_effect=configured_path):
                self.assertEqual(lifecycle.supervise(root / "job.json"), 78)
            self.assertEqual(list(root.glob("output-evidence*.tgz")), [])
            state = lifecycle.read_json(root / "output.supervisor.json")
            self.assertEqual(state["status"], "HOLD")
            self.assertEqual(state["export_error_type"], "TimeoutError")

    def test_export_publication_does_not_clobber_a_concurrent_destination(self):
        with tempfile.TemporaryDirectory(prefix="athena-export-race-") as folder:
            root = Path(folder)
            output, archive = root / "run", root / "evidence.tgz"
            output.mkdir()
            research.campaign.write_json(output / "manifest.json", {"status": "COMPLETE", "bundle_id": "a" * 64,
                "spec": {"limits": {"export_bytes": 1048576}}})
            (output / "result.json").write_bytes(b'{"answer":42}')
            original_verify = lifecycle._verify_export
            sentinel = b"already published by a different run"
            def concurrent_publish(path, inventory):
                original_verify(path, inventory)
                if Path(path) != archive:
                    archive.write_bytes(sentinel)
            with patch.object(lifecycle, "_verify_export", side_effect=concurrent_publish):
                with self.assertRaises((ValueError, tarfile.TarError, EOFError)):
                    lifecycle.export_run(output, archive)
            self.assertEqual(archive.read_bytes(), sentinel)
            self.assertFalse((output / "export-receipt.json").exists())
            self.assertEqual(list(root.glob(".evidence.tgz.*.tmp")), [])

    def test_rendered_unit_has_no_fixed_job_duration_and_bounded_system_resources(self):
        unit = lifecycle.render_unit(12 * 1024 ** 3)
        self.assertIn("RuntimeMaxSec=infinity", unit)
        self.assertIn("MemoryMax=12884901888", unit)
        self.assertIn("TasksMax=256", unit)
        self.assertIn("RestartPreventExitStatus=78", unit)
        self.assertIn("RestartMode=direct", unit)
        self.assertIn("OnSuccess=athena-research-poweroff.service", unit)
        self.assertIn("OnFailure=athena-research-poweroff.service", unit)
        self.assertIn("ProtectHome=read-only", unit)
        self.assertIn("ReadWritePaths=/opt/athena/runs\n", unit)
        self.assertNotIn("ReadWritePaths=/opt/athena/runs /home/athena-research/.cargo", unit)
        poweroff = lifecycle.render_poweroff_unit()
        self.assertIn("After=athena-research.service", poweroff)
        self.assertIn("ExecStart=/usr/bin/systemctl --no-block poweroff", poweroff)
        self.assertNotIn("/opt/athena/current", poweroff)
        fixed = lifecycle.render_unit(12 * 1024 ** 3, "/opt/athena/runs/frozen-study/source")
        self.assertIn("WorkingDirectory=/opt/athena/runs/frozen-study/source", fixed)
        self.assertIn("ExecStopPost=/bin/bash /opt/athena/runs/frozen-study/source/cloud_experiments/finish.sh", fixed)
        self.assertNotIn("/opt/athena/current", fixed)
        for bad in ("/opt/athena/runs/../foreign/source", "/opt/athena/runs/x\nUser=root/source"):
            with self.assertRaises(ValueError):
                lifecycle.render_unit(12 * 1024 ** 3, bad)
        self.assertNotIn("RuntimeMaxSec=5400", unit)

    def test_research_subprocesses_do_not_inherit_credentials_from_parent_environment(self):
        with tempfile.TemporaryDirectory(prefix="athena-child-env-") as folder:
            root = Path(folder)
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            child_keys = (
                "NVIDIA_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                "KAGGLE_KEY", "ATHENA_RESEARCH_NVIDIA_API_KEY", "ATHENA_RESEARCH_WRITER",
                "ATHENA_RESEARCH_HASH", "ATHENA_RESEARCH_RESERVE", "ATHENA_RESEARCH_PREPARED",
                "ATHENA_RESEARCH_TABLE_EXTRACTOR", "ATHENA_WPT_REPORT_ONLY",
                "ATHENA_LEXBOR_ADAPTER_TARGET_DIR", "ATHENA_LEXBOR_ARCHIVE_CACHE",
                "ATHENA_LEXBOR_WORK_ROOT", "ATHENA_LEXBOR_SOURCE_DIR",
                "ATHENA_LEXBOR_BUILD_DIR", "ATHENA_LEXBOR_LIB_DIR",
                "ATHENA_MCP_RUST_BIN", "ATHENA_MCP_TS_SERVER", "ATHENA_CORE_PROBE_BIN",
                "ATHENA_OFFLINE", "NODE_PATH",
            )
            code = f"import json,os;print(json.dumps({{'PATH':bool(os.environ.get('PATH')),**{{k:os.environ.get(k) for k in {child_keys!r}}}}}))"
            try:
                with patch.dict(os.environ, {
                    "NVIDIA_API_KEY": "synthetic-nim-key",
                    "OPENROUTER_API_KEY": "synthetic-router-key",
                    "GOOGLE_APPLICATION_CREDENTIALS": "synthetic-gcp-credential-path",
                    "KAGGLE_KEY": "synthetic-kaggle-key",
                    "ATHENA_RESEARCH_NVIDIA_API_KEY": "synthetic-namespaced-key",
                    "ATHENA_RESEARCH_WRITER": "direct",
                    "ATHENA_RESEARCH_HASH": "incremental",
                    "ATHENA_RESEARCH_RESERVE": "small",
                    "ATHENA_RESEARCH_PREPARED": "lexical",
                    "ATHENA_RESEARCH_TABLE_EXTRACTOR": "selectors",
                    "ATHENA_WPT_REPORT_ONLY": "1",
                    "ATHENA_LEXBOR_ADAPTER_TARGET_DIR": "/opt/athena/runs/cargo-target/lexbor-adapter-target",
                    "ATHENA_LEXBOR_ARCHIVE_CACHE": "/opt/athena/runs/cargo-target/lexbor.zip",
                    "ATHENA_LEXBOR_WORK_ROOT": "/opt/athena/runs/cargo-target/lexbor-v3.0.0",
                    "ATHENA_LEXBOR_SOURCE_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/src",
                    "ATHENA_LEXBOR_BUILD_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/build",
                    "ATHENA_LEXBOR_LIB_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/build",
                    "ATHENA_MCP_RUST_BIN": "/opt/athena/current/source/cloud_experiments/mcp_sdk/rust/target/release/athena-mcp-rust-fixture",
                    "ATHENA_MCP_TS_SERVER": "/opt/athena/current/source/cloud_experiments/mcp_sdk/typescript/server.mjs",
                    "ATHENA_CORE_PROBE_BIN": "/opt/athena/current/source/target/release/core_probe",
                    "ATHENA_OFFLINE": "1",
                    "NODE_PATH": "/opt/athena/current/source/cloud_experiments/mcp_sdk/typescript/node_modules",
                }):
                    observed = json.loads(run.run([sys.executable, "-B", "-c", code], log_output=False))
                    self.assertEqual(os.environ["NVIDIA_API_KEY"], "synthetic-nim-key")
                self.assertEqual(observed, {
                    "PATH": True,
                    "NVIDIA_API_KEY": None,
                    "OPENROUTER_API_KEY": None,
                    "GOOGLE_APPLICATION_CREDENTIALS": None,
                    "KAGGLE_KEY": None,
                    "ATHENA_RESEARCH_NVIDIA_API_KEY": None,
                    "ATHENA_RESEARCH_WRITER": "direct",
                    "ATHENA_RESEARCH_HASH": "incremental",
                    "ATHENA_RESEARCH_RESERVE": "small",
                    "ATHENA_RESEARCH_PREPARED": "lexical",
                    "ATHENA_RESEARCH_TABLE_EXTRACTOR": "selectors",
                    "ATHENA_WPT_REPORT_ONLY": "1",
                    "ATHENA_LEXBOR_ADAPTER_TARGET_DIR": "/opt/athena/runs/cargo-target/lexbor-adapter-target",
                    "ATHENA_LEXBOR_ARCHIVE_CACHE": "/opt/athena/runs/cargo-target/lexbor.zip",
                    "ATHENA_LEXBOR_WORK_ROOT": "/opt/athena/runs/cargo-target/lexbor-v3.0.0",
                    "ATHENA_LEXBOR_SOURCE_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/src",
                    "ATHENA_LEXBOR_BUILD_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/build",
                    "ATHENA_LEXBOR_LIB_DIR": "/opt/athena/runs/cargo-target/lexbor-v3.0.0/build",
                    "ATHENA_MCP_RUST_BIN": "/opt/athena/current/source/cloud_experiments/mcp_sdk/rust/target/release/athena-mcp-rust-fixture",
                    "ATHENA_MCP_TS_SERVER": "/opt/athena/current/source/cloud_experiments/mcp_sdk/typescript/server.mjs",
                    "ATHENA_CORE_PROBE_BIN": "/opt/athena/current/source/target/release/core_probe",
                    "ATHENA_OFFLINE": "1",
                    "NODE_PATH": "/opt/athena/current/source/cloud_experiments/mcp_sdk/typescript/node_modules",
                })
            finally:
                run.sample_stream.close()

    def test_thread_start_failure_reaps_launched_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="athena-thread-start-failure-") as folder:
            root = Path(folder)
            marker = root / "started"
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            real_popen = subprocess.Popen
            spawned = []
            real_thread = lifecycle.threading.Thread
            created_threads = []

            def capture_process(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process)
                return process

            def make_thread(*args, **kwargs):
                thread = real_thread(*args, **kwargs)
                created_threads.append(thread)
                if len(created_threads) == 2:
                    def fail_after_child_starts():
                        deadline = time.monotonic() + 5
                        while not marker.exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        if not marker.exists():
                            raise AssertionError("child did not reach its startup marker")
                        raise RuntimeError("injected reader-thread startup failure")

                    thread.start = fail_after_child_starts
                return thread

            code = "from pathlib import Path;import sys,time;Path(sys.argv[1]).write_text('started');time.sleep(30)"
            try:
                with patch.object(lifecycle.subprocess, "Popen", side_effect=capture_process), \
                        patch.object(lifecycle.threading, "Thread", side_effect=make_thread):
                    with self.assertRaisesRegex(RuntimeError, "injected reader-thread startup failure"):
                        run.run([sys.executable, "-B", "-c", code, str(marker)], timeout=10)
                self.assertEqual(len(spawned), 1)
                self.assertIsNotNone(spawned[0].poll(), "launched child was left running")
                self.assertEqual(len(created_threads), 2)
                self.assertFalse(created_threads[0].is_alive(), "started reader thread was left running")
            finally:
                for process in spawned:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                run.sample_stream.close()

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux process-tree boundary")
    def test_linux_timeout_reaps_descendant_group_and_reports_its_memory(self):
        with tempfile.TemporaryDirectory(prefix="athena-process-tree-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = "import os,sys,time;from pathlib import Path;owned=bytearray(b'x'*(32*1024**2));Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)"
            parent = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]]);time.sleep(30)"
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            try:
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run.run([sys.executable, "-B", "-c", parent, descendant, str(marker)], timeout=2)
                self.assertTrue(marker.exists())
                self.assertGreater(run.manifest["sampled_process_tree_rss_bytes"], 32 * 1024 ** 2)
                process = Path("/proc") / marker.read_text() / "status"
                deadline = time.monotonic() + 2
                while process.exists() and time.monotonic() < deadline:
                    state = next(line for line in process.read_text().splitlines() if line.startswith("State:"))
                    if "Z" in state:
                        break
                    time.sleep(.02)
                if process.exists():
                    self.assertIn("Z", next(line for line in process.read_text().splitlines() if line.startswith("State:")))
            finally:
                run.sample_stream.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux PID/network namespace boundary")
    def test_linux_isolated_worker_blocks_egress_keeps_tls_loopback_and_kills_escaped_descendants(self):
        from cloud_experiments import service_lab

        with tempfile.TemporaryDirectory(prefix="athena-isolation-boundary-") as folder:
            root = Path(folder).resolve()
            lab_root = root / "tls"
            marker, double_marker, evidence = (root / "setsid.pid", root / "double-fork.pid",
                                                root / "boundary.json")
            code = r'''
import json, os, socket, ssl, subprocess, sys, time
from pathlib import Path
from cloud_experiments.service_lab import TLSLab
root, marker, double_marker, evidence = (Path(sys.argv[1]), Path(sys.argv[2]),
                                         Path(sys.argv[3]), Path(sys.argv[4]))
host_net, host_pid = sys.argv[5], sys.argv[6]
blocked = {}
for name, address in (("metadata", ("169.254.169.254", 80)), ("public", ("1.1.1.1", 443))):
    try:
        connection = socket.create_connection(address, timeout=0.2)
    except OSError:
        blocked[name] = True
    else:
        connection.close()
        blocked[name] = False
with TLSLab(root, bodies={"article": "isolated"}) as lab:
    port = lab.server.server_address[1]
    raw = socket.create_connection(("127.0.0.1", port), timeout=1)
    with ssl._create_unverified_context().wrap_socket(raw, server_hostname="athena-lab.invalid") as client:
        client.sendall(b"GET /article HTTP/1.1\r\nHost: athena-lab.invalid\r\nX-Lab-Tenant: A\r\nX-Lab-Epoch: 1\r\nX-Lab-Attempt: 0\r\nConnection: close\r\n\r\n")
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
setsid_child = "import os,sys,time;from pathlib import Path;os.setsid();Path(sys.argv[1]).write_text('ready');time.sleep(60)"
subprocess.Popen([sys.executable, "-B", "-c", setsid_child, str(marker)])
double_fork_child = (
    "import os,sys,time\n"
    "from pathlib import Path\n"
    "first = os.fork()\n"
    "if first:\n"
    "    os._exit(0)\n"
    "os.setsid()\n"
    "second = os.fork()\n"
    "if second:\n"
    "    os._exit(0)\n"
    "Path(sys.argv[1]).write_text('ready')\n"
    "time.sleep(60)\n"
)
subprocess.Popen([sys.executable, "-B", "-c", double_fork_child, str(double_marker)])
deadline = time.monotonic() + 3
while not (marker.exists() and double_marker.exists()) and time.monotonic() < deadline:
    time.sleep(0.01)
report = {"host_netns": host_net, "child_netns": os.readlink("/proc/self/ns/net"),
          "host_pidns": host_pid, "child_pidns": os.readlink("/proc/self/ns/pid"),
          "blocked": blocked, "loopback_tls_ok": b"200 OK" in response and b"tenant=A" in response,
          "origin_cleanup": lab.cleanup, "setsid_child_started": marker.exists(),
          "double_fork_child_started": double_marker.exists()}
evidence.write_text(json.dumps(report, sort_keys=True))
if not report["setsid_child_started"] or not report["double_fork_child_started"]:
    raise SystemExit(42)
time.sleep(60)
'''
            run = research.ResearchRun(root / "run", "isolation", "c" * 64, {},
                                       isolate_children=True)
            command = [sys.executable, "-B", "-c", code, str(lab_root), str(marker),
                       str(double_marker), str(evidence), research._current_network_namespace_id(),
                       research._current_pid_namespace_id()]
            try:
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run.run(command, timeout=8, log_output=False)
                self.assertTrue(evidence.is_file(), "isolated child did not write its bounded control result")
                observed = json.loads(evidence.read_text(encoding="utf-8"))
                self.assertNotEqual(observed["child_netns"], observed["host_netns"])
                self.assertNotEqual(observed["child_pidns"], observed["host_pidns"])
                self.assertEqual(observed["blocked"], {"metadata": True, "public": True})
                self.assertTrue(observed["loopback_tls_ok"], "TLS origin and probe could not share isolated loopback")
                self.assertTrue(observed["origin_cleanup"]["private_fixture_removed"])
                self.assertTrue(observed["origin_cleanup"]["server_thread_stopped"])
                self.assertTrue(observed["setsid_child_started"])
                self.assertTrue(observed["double_fork_child_started"])
                marker_bytes = (str(marker).encode(), str(double_marker).encode())
                live = []
                for status in Path("/proc").glob("[0-9]*/cmdline"):
                    try:
                        command_line = status.read_bytes()
                        if any(value in command_line for value in marker_bytes):
                            live.append(status.parent.name)
                    except (OSError, PermissionError):
                        continue
                self.assertEqual(live, [], "a setsid/double-fork descendant survived PID-namespace teardown")
            finally:
                run.sample_stream.close()

    def test_process_tree_rss_includes_worker_thread_children(self):
        with tempfile.TemporaryDirectory(prefix="athena-proc-tree-fixture-") as folder:
            proc = Path(folder) / "proc"
            for pid, kib in ((100, 1024), (200, 2048)):
                process = proc / str(pid)
                (process / "task" / str(pid)).mkdir(parents=True)
                (process / "status").write_text(f"Name:\ttest\nVmRSS:\t{kib} kB\n")
                (process / "task" / str(pid) / "children").write_text("")
            worker_children = proc / "100" / "task" / "101"
            worker_children.mkdir()
            (worker_children / "children").write_text("200\n")

            def proc_path(value):
                value = str(value)
                if value == "/proc":
                    return proc
                if value.startswith("/proc/"):
                    return proc / value[len("/proc/"):]
                return Path(value)

            with patch.object(lifecycle.os, "name", "posix"), patch.object(lifecycle, "Path", proc_path):
                observed = lifecycle.process_tree_rss(100)
            self.assertEqual(observed, (1024 + 2048) * 1024)

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux process-tree boundary")
    def test_linux_rss_includes_worker_thread_descendant(self):
        with tempfile.TemporaryDirectory(prefix="athena-thread-tree-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = ("import os,sys,time;from pathlib import Path;"
                          "owned=bytearray(b'x'*(32*1024**2));"
                          "Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)")
            parent = ("import subprocess,sys,threading,time\n"
                      "def spawn():\n"
                      "    subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]])\n"
                      "threading.Thread(target=spawn).start()\n"
                      "time.sleep(30)")
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            try:
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run.run([sys.executable, "-B", "-c", parent, descendant, str(marker)], timeout=2)
                self.assertTrue(marker.exists())
                self.assertGreater(run.manifest["sampled_process_tree_rss_bytes"], 32 * 1024 ** 2)
                process = Path("/proc") / marker.read_text() / "status"
                deadline = time.monotonic() + 2
                while process.exists() and time.monotonic() < deadline:
                    state = next(line for line in process.read_text().splitlines() if line.startswith("State:"))
                    if "Z" in state:
                        break
                    time.sleep(.02)
                if process.exists():
                    self.assertIn("Z", next(line for line in process.read_text().splitlines() if line.startswith("State:")))
            finally:
                run.sample_stream.close()

    def test_export_requires_complete_bytes_and_verified_distinct_copy_before_acknowledgement(self):
        with tempfile.TemporaryDirectory(prefix="athena-export-") as folder:
            root = Path(folder)
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"status": "COMPLETE", "bundle_id": "a" * 64,
                                                          "spec": {"limits": {"export_bytes": 1048576}}}))
            (run / "result.json").write_bytes(b'{"answer":42}\n')
            (run / "work").mkdir()
            (run / "work" / "cache").write_bytes(b"disposable build state")
            archive = root / "evidence.tgz"
            receipt = lifecycle.export_run(run, archive)
            original_archive = archive.read_bytes()
            (run / "export-receipt.json").unlink()
            self.assertEqual(lifecycle.export_run(run, archive)["sha256"], receipt["sha256"])
            self.assertEqual(archive.read_bytes(), original_archive)
            with tarfile.open(archive, "r:gz") as saved:
                self.assertEqual(set(saved.getnames()), {"manifest.json", "result.json"})
                self.assertEqual(saved.extractfile("result.json").read(), b'{"answer":42}\n')
            self.assertFalse((run / "export-ack.json").exists())
            with self.assertRaisesRegex(ValueError, "distinct copy"):
                lifecycle.acknowledge_export(run, archive)
            copy = root / "downloaded.tgz"
            shutil.copyfile(archive, copy)
            copy.write_bytes(copy.read_bytes()[:-1] + b"X")
            with self.assertRaisesRegex(ValueError, "checksum"):
                lifecycle.acknowledge_export(run, copy)
            self.assertFalse((run / "export-ack.json").exists())
            shutil.copyfile(archive, copy)
            ack = lifecycle.acknowledge_export(run, copy)
            self.assertEqual(ack["sha256"], receipt["sha256"])
            self.assertTrue(ack["copy_verified"])
            self.assertEqual((run / "result.json").read_bytes(), b'{"answer":42}\n')

    def test_reusable_storage_credit_counts_allocated_single_link_bytes_once(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="athena-storage-credit-") as folder:
            root = Path(folder)
            for name, size in (("sparse.bin", 4096), ("full.bin", 1000), ("hard.bin", 4096)):
                (root / name).write_bytes(b"x" * size)
            device = root.lstat().st_dev
            original = Path.lstat

            def metadata(path):
                actual = original(path)
                values = {name: getattr(actual, name) for name in dir(actual) if name.startswith("st_")}
                values["st_blocks"] = 2 if path.name == "sparse.bin" else 8
                values["st_nlink"] = 2 if path.name == "hard.bin" else 1
                return SimpleNamespace(**values)

            with patch.object(Path, "lstat", metadata):
                self.assertEqual(lifecycle.reusable_storage_bytes(
                    [root, root / "full.bin"], device=device, deadline=time.monotonic() + 10), 2024)
            with self.assertRaisesRegex(ValueError, "filesystem boundary"):
                lifecycle.reusable_storage_bytes([root], device=device + 1, deadline=time.monotonic() + 10)
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                lifecycle.reusable_storage_bytes([root], device=device, deadline=time.monotonic() - 1)
            self.assertEqual((root / "full.bin").read_bytes(), b"x" * 1000)

    def test_finalized_failed_run_keeps_an_exportable_failure_record(self):
        with tempfile.TemporaryDirectory(prefix="athena-failed-export-") as folder:
            root = Path(folder)
            run_path = root / "run"
            spec = {"limits": {"wall_seconds": 60, "export_bytes": 1048576}}
            campaign_deadline = time.time() + 120
            run = research.ResearchRun(run_path, "recovery", "b" * 64, spec)
            run.bind_deadline(60)
            run.manifest.update(failed_phase="measurement", campaign_deadline_unix=campaign_deadline,
                                export_deadline_unix=campaign_deadline - 10, shutdown_seconds=10)
            run.finish(InterruptedError("controlled interruption"))
            outcome = lifecycle.export_finalized_run(run_path, campaign_deadline_unix=campaign_deadline)
            self.assertEqual(outcome["export_status"], "VERIFIED_LOCAL_ARCHIVE")
            self.assertGreater(outcome["export_bytes"], 0)
            receipt = lifecycle.read_json(run_path / "export-receipt.json")
            failed_archive = root / receipt["archive_name"]
            failure_bytes = failed_archive.read_bytes()
            failure_copy = root / "failure-copy.tgz"
            shutil.copyfile(failed_archive, failure_copy)
            failed_ack = lifecycle.acknowledge_export(run_path, failure_copy)
            with tarfile.open(failed_archive, "r:gz") as saved:
                self.assertIn("manifest.json", saved.getnames())
                self.assertIn("run.log", saved.getnames())
                self.assertEqual(json.load(saved.extractfile("manifest.json"))["status"], "FAILED")
            resumed = research.ResearchRun(run_path, "recovery", "b" * 64, spec, resume=True)
            resumed.bind_deadline(60)
            (run_path / "result.json").write_bytes(b'{"recovered":true}\n')
            resumed.finish()
            recovered = lifecycle.export_finalized_run(run_path, campaign_deadline_unix=campaign_deadline)
            recovered_receipt = lifecycle.read_json(run_path / "export-receipt.json")
            recovered_archive = root / recovered_receipt["archive_name"]
            self.assertNotEqual(failed_archive, recovered_archive)
            self.assertEqual(failed_archive.read_bytes(), failure_bytes)
            self.assertEqual(recovered["export_archive_name"], recovered_archive.name)
            self.assertFalse(recovered_receipt["copy_verified"])
            self.assertNotEqual(recovered_receipt["sha256"], failed_ack["sha256"])
            with tarfile.open(recovered_archive, "r:gz") as saved:
                recovered_manifest = json.load(saved.extractfile("manifest.json"))
                self.assertEqual(recovered_manifest["status"], "COMPLETE")
                self.assertTrue({"export-receipt.json", "export-ack.json"}.isdisjoint(recovered_manifest["artifacts"]))
                self.assertEqual(saved.extractfile("result.json").read(), b'{"recovered":true}\n')
            recovered_bytes = recovered_archive.read_bytes()
            repeated = lifecycle.export_finalized_run(run_path, campaign_deadline_unix=campaign_deadline)
            self.assertEqual(repeated, recovered)
            self.assertEqual(recovered_archive.read_bytes(), recovered_bytes)
            self.assertEqual(failed_archive.read_bytes(), failure_bytes)

    def test_managed_export_retains_legacy_archive_and_caps_retry_snapshots(self):
        with tempfile.TemporaryDirectory(prefix="athena-export-cap-") as folder:
            root = Path(folder)
            run = root / "run"
            run.mkdir()
            campaign_deadline = time.time() + 120
            manifest = {"status": "FAILED", "bundle_id": "c" * 64,
                        "spec": {"limits": {"export_bytes": 1048576}},
                        "campaign_deadline_unix": campaign_deadline,
                        "export_deadline_unix": campaign_deadline - 10, "shutdown_seconds": 10}
            research.campaign.write_json(run / "manifest.json", manifest)
            legacy = root / "run-evidence.tgz"
            lifecycle.export_run(run, legacy)
            legacy_bytes = legacy.read_bytes()
            partial = root / (".run-evidence-" + "d" * 64 + ".tgz.crashed.tmp")
            partial_bytes = b"preserved incomplete archive"
            partial.write_bytes(partial_bytes)
            manifest["snapshot"] = 2
            research.campaign.write_json(run / "manifest.json", manifest)
            lifecycle.export_finalized_run(run, campaign_deadline_unix=campaign_deadline)
            retained = {path.name: path.read_bytes() for path in root.glob("run-evidence*.tgz")}
            self.assertEqual(len(retained), 2)
            manifest["snapshot"] = 3
            research.campaign.write_json(run / "manifest.json", manifest)
            with self.assertRaisesRegex(lifecycle.StorageGuardError, "snapshot capacity"):
                lifecycle.export_finalized_run(run, campaign_deadline_unix=campaign_deadline)
            self.assertEqual({path.name: path.read_bytes() for path in root.glob("run-evidence*.tgz")}, retained)
            self.assertEqual(legacy.read_bytes(), legacy_bytes)
            self.assertEqual(partial.read_bytes(), partial_bytes)

    def test_killed_collection_retains_complete_block_and_releases_writer_lock(self):
        with tempfile.TemporaryDirectory(prefix="athena-recovery-") as folder:
            root = Path(folder)
            script = """
import sys,time
from pathlib import Path
from cloud_experiments import lifecycle,research
root=Path(sys.argv[1])
context={'study':'frozen'}
with research._analysis_lock(root/'execution.lock'):
    journal=lifecycle.CollectionJournal(root,context,4096,'first-session')
    first={'channel':'pilot','binding':{'binary':'original'},'block':0}
    list(journal.collect(first,lambda:iter([{'value':10},{'value':20}])))
    def partial():
        yield {'value':999}
        (root/'ready').write_text('staged, not published')
        time.sleep(30)
    list(journal.collect(dict(first,block=1),partial))
"""
            child = subprocess.Popen([sys.executable, "-B", "-c", script, str(root)],
                                     cwd=research.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not (root / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / "ready").exists(), "Child must reach the actual partial-file boundary")
                with self.assertRaisesRegex(ValueError, "already active"):
                    with research._analysis_lock(root / "execution.lock"):
                        pass
                child.kill()
                _, stderr = child.communicate(timeout=10)
                self.assertNotEqual(child.returncode, 0, stderr.decode(errors="replace"))
                pending = list((root / "collection").glob(".pending-*"))
                self.assertEqual(len(pending), 1)
                (pending[0] / ".receipt.json.abcdefgh.tmp").write_bytes(b"interrupted atomic checkpoint")
                with research._analysis_lock(root / "execution.lock"):
                    journal = lifecycle.CollectionJournal(root, {"study": "frozen"}, 4096, "second-session")
                    descriptor = {"channel": "pilot", "binding": {"binary": "original"}, "block": 0}
                    def forbidden_recompute():
                        raise AssertionError("Complete block recomputed")
                    retained = list(journal.collect(descriptor, forbidden_recompute))
                    resumed = list(journal.collect(dict(descriptor, block=1), lambda: iter([{"value": 30}, {"value": 40}])))
                self.assertEqual([row["value"] for row in retained + resumed], [10, 20, 30, 40])
                self.assertEqual({r["session_id"] for r in retained}, {"first-session"})
                self.assertEqual({r["session_id"] for r in resumed}, {"second-session"})
                self.assertFalse(list((root / "collection").glob(".pending-*")))
                self.assertEqual(len(journal.shards), 2)
                with self.assertRaisesRegex(ValueError, "settings changed"):
                    list(journal.collect({**descriptor, "binding": {"binary": "different"}}, lambda: iter([{"value": 50}])))
                with self.assertRaisesRegex(ValueError, "environment changed"):
                    lifecycle.CollectionJournal(root, {"study": "changed"}, 4096, "new")
                shard = root / "collection" / lifecycle.digest(descriptor) / "samples.jsonl"
                original = shard.read_bytes()
                self.assertIn(b'"value":10', original)
                shard.write_bytes(original.replace(b'"value":10', b'"value":11'))
                with self.assertRaisesRegex(ValueError, "bytes changed"):
                    lifecycle.CollectionJournal(root, {"study": "frozen"}, 4096, "new")
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)

    def test_collection_capacity_is_enforced_before_publication_and_cleans_partial_state(self):
        with tempfile.TemporaryDirectory(prefix="athena-journal-cap-") as folder:
            journal = lifecycle.CollectionJournal(folder, {"study": "one"}, 128, "session")
            descriptor = {"channel": "screen", "binding": {}, "block": 0}
            with self.assertRaisesRegex(ValueError, "before write"):
                list(journal.collect(descriptor, lambda: iter([{"value": "x" * 256}])))
            self.assertEqual(journal.total_bytes, 0)
            self.assertEqual(journal.shards, [])
            self.assertEqual(list((Path(folder) / "collection").iterdir()), [])

    def test_collection_recovery_rejects_metadata_overflow_before_reading_receipt(self):
        with tempfile.TemporaryDirectory(prefix="athena-journal-metadata-cap-") as folder:
            root = Path(folder)
            journal = lifecycle.CollectionJournal(root, {"study": "bounded"}, 4096, "session")
            descriptor = {"channel": "screen", "binding": {}, "block": 0}
            list(journal.collect(descriptor, lambda: iter([{"value": 1}])))
            receipt_path = next((root / "collection").glob("*/receipt.json"))
            metadata_bytes = (root / "collection-bindings.json").stat().st_size + receipt_path.stat().st_size

            with patch.object(lifecycle, "read_bytes", wraps=lifecycle.read_bytes) as read_bytes:
                with self.assertRaisesRegex(ValueError, "capacity"):
                    lifecycle.CollectionJournal(
                        root, {"study": "bounded"}, 4096, "resume", metadata_limit=metadata_bytes - 1)

            read_paths = {Path(call.args[0]) for call in read_bytes.call_args_list}
            self.assertNotIn(receipt_path, read_paths,
                             "recovery must reject before allocating over-budget receipt metadata")

    def test_collection_recovery_rejects_block_overflow_before_hashing_shard(self):
        with tempfile.TemporaryDirectory(prefix="athena-journal-block-cap-") as folder:
            root = Path(folder)
            journal = lifecycle.CollectionJournal(root, {"study": "bounded"}, 4096, "session")
            descriptor = {"channel": "screen", "binding": {}, "block": 0}
            list(journal.collect(descriptor, lambda: iter([{"value": "bounded"}])))
            shard = root / "collection" / lifecycle.digest(descriptor) / "samples.jsonl"
            smaller_block_limit = shard.stat().st_size - 1

            with patch.object(lifecycle.protocol, "sha256_file",
                              side_effect=AssertionError("over-capacity shard was hashed")):
                with self.assertRaisesRegex(ValueError, "capacity"):
                    lifecycle.CollectionJournal(root, {"study": "bounded"}, 4096, "resume",
                                                block_limit=smaller_block_limit)

    def test_collection_recovery_rejects_aggregate_overflow_before_hashing_later_shard(self):
        with tempfile.TemporaryDirectory(prefix="athena-journal-total-cap-") as folder:
            root = Path(folder)
            journal = lifecycle.CollectionJournal(root, {"study": "bounded"}, 4096, "session")
            descriptors = [
                {"channel": "screen", "binding": {}, "block": 0},
                {"channel": "screen", "binding": {}, "block": 1},
            ]
            for descriptor in descriptors:
                list(journal.collect(descriptor, lambda: iter([{"value": "bounded"}])))
            shard_bytes = (root / "collection" / lifecycle.digest(descriptors[0]) / "samples.jsonl").stat().st_size
            self.assertEqual(
                shard_bytes,
                (root / "collection" / lifecycle.digest(descriptors[1]) / "samples.jsonl").stat().st_size,
            )

            with patch.object(lifecycle.protocol, "sha256_file", wraps=lifecycle.protocol.sha256_file) as hash_file:
                with self.assertRaisesRegex(ValueError, "capacity"):
                    lifecycle.CollectionJournal(root, {"study": "bounded"}, shard_bytes, "resume")

            self.assertEqual(hash_file.call_count, 1,
                             "recovery must stop before hashing the shard that exceeds aggregate capacity")

    def test_child_output_and_deadline_are_bounded_and_resume_does_not_reset_total_deadline(self):
        with tempfile.TemporaryDirectory(prefix="athena-process-cap-") as folder:
            root = Path(folder)
            spec = {"limits": {"command_output_bytes": 4096, "log_bytes": 65536, "metadata_bytes": 65536}}
            run = research.ResearchRun(root / "run", "control", "a" * 64, spec)
            try:
                run.bind_deadline(60)
                frozen_deadline = run.manifest["deadline_unix"]
                with self.assertRaisesRegex(RuntimeError, "OUTPUT_LIMIT"):
                    run.run([sys.executable, "-B", "-c", "import sys,time;sys.stdout.buffer.write(b'x'*1048576);sys.stdout.flush();time.sleep(30)"], timeout=5)
                self.assertEqual(run.manifest["commands"][-1]["status"], "OUTPUT_LIMIT")
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run.run([sys.executable, "-B", "-c", "import time;time.sleep(30)"], timeout=.2)
                self.assertEqual(run.manifest["commands"][-1]["status"], "TIMEOUT")
            finally:
                run.sample_stream.close()
            resumed = research.ResearchRun(root / "run", "control", "a" * 64, spec, resume=True)
            try:
                resumed.bind_deadline(60)
                self.assertEqual(resumed.manifest["deadline_unix"], frozen_deadline)
                self.assertEqual(resumed.manifest["command_count"], 2)
                self.assertLess((resumed.directory / "run.log").stat().st_size, 16384)
                with self.assertRaisesRegex(ValueError, "cannot reset"):
                    resumed.bind_deadline(120)
            finally:
                resumed.sample_stream.close()
            with self.assertRaisesRegex(ValueError, "identity changed"):
                research.ResearchRun(root / "run", "control", "b" * 64, spec, resume=True)

    def test_redacted_child_failure_logs_only_output_hashes_and_sizes(self):
        import hashlib

        marker = b"SENSITIVE_WCEB_PAGE_CONTENT_MARKER"
        with tempfile.TemporaryDirectory(prefix="athena-redacted-child-output-") as folder:
            run = research.ResearchRun(Path(folder) / "run", "control", "a" * 64,
                                       {"limits": {"command_output_bytes": 4096,
                                                   "log_bytes": 65536, "metadata_bytes": 65536}})
            code = ("import sys;value=sys.stdin.buffer.read();"
                    "sys.stdout.buffer.write(value);sys.stderr.buffer.write(value);sys.exit(17)")
            try:
                with self.assertRaises(subprocess.CalledProcessError):
                    run.run([sys.executable, "-B", "-c", code], input_bytes=marker,
                            log_output=False, redact_output=True)
                observed = run.log_path.read_text(encoding="utf-8")
                self.assertNotIn(marker.decode("ascii"), observed)
                self.assertIn("CHILD_OUTPUT_REDACTED", observed)
                self.assertIn(hashlib.sha256(marker).hexdigest(), observed)
                self.assertNotIn(marker.decode("ascii"), (run.directory / "commands.jsonl").read_text())
                self.assertEqual(run.manifest["commands"][-1]["exit_code"], 17)
            finally:
                run.sample_stream.close()

    def test_managed_check_revalidates_prior_pass_after_resume(self):
        import hashlib

        with tempfile.TemporaryDirectory(prefix="athena-check-resume-integrity-") as folder:
            root = Path(folder)
            spec = {"limits": {"command_output_bytes": 4096, "log_bytes": 65536,
                               "metadata_bytes": 65536}}
            run_dir = root / "run"
            artifact = run_dir / "h07-block.json"
            artifact_digest = hashlib.sha256(b"committed block").hexdigest()
            first = research.ResearchRun(run_dir, "control", "a" * 64, spec)
            artifact.write_bytes(b"committed block")
            first.check("h07:provider-lifetime-study", lambda: {"sha256": artifact_digest})
            first.finish()
            artifact.write_bytes(b"tampered block")

            resumed = research.ResearchRun(run_dir, "control", "a" * 64, spec, resume=True)
            try:
                def validate_committed_artifact():
                    observed = hashlib.sha256(artifact.read_bytes()).hexdigest()
                    if observed != artifact_digest:
                        raise lifecycle.IntegrityError("Committed H07 artifact digest changed")
                    return {"sha256": observed}

                with self.assertRaisesRegex(lifecycle.IntegrityError, "digest changed"):
                    resumed.check("h07:provider-lifetime-study", validate_committed_artifact)

                self.assertEqual(resumed.manifest["checks"]["h07:provider-lifetime-study"]["status"], "FAIL")
                self.assertNotEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), artifact_digest)
            finally:
                resumed.sample_stream.close()

    def test_expired_resumed_deadline_finalizes_failure_instead_of_leaving_run_active(self):
        with tempfile.TemporaryDirectory(prefix="athena-expired-resume-") as folder:
            root = Path(folder)
            spec = {"limits": {"command_output_bytes": 4096, "log_bytes": 65536, "metadata_bytes": 65536}}
            first = research.ResearchRun(root / "run", "control", "a" * 64, spec)
            first.bind_deadline(60)
            first.manifest["deadline_unix"] = time.time() - 1
            first.save()
            first.sample_stream.close()

            resumed = research.ResearchRun(root / "run", "control", "a" * 64, spec, resume=True)
            try:
                with self.assertRaisesRegex(TimeoutError, "deadline exhausted"):
                    resumed.bind_deadline(60)

                self.assertEqual(resumed.manifest["status"], "FAILED")
                self.assertIn("Persisted campaign deadline exhausted", resumed.manifest["error"])
                self.assertTrue(resumed.manifest["finished_at"])
                self.assertIn("run.log", resumed.manifest["artifacts"])
                resumed.finish(TimeoutError("Persisted campaign deadline exhausted"))
                failures = [line.split("] ", 1)[1]
                            for line in resumed.log_path.read_text(encoding="utf-8").splitlines()
                            if "] FAILED " in line]
                self.assertEqual(failures, ["FAILED TimeoutError('Persisted campaign deadline exhausted')"])
            finally:
                resumed.sample_stream.close()

    def test_memory_limit_bytes_handles_cgroup_v2_root_without_memory_max(self):
        with tempfile.TemporaryDirectory(prefix="athena-cgroup-v2-") as folder:
            root = Path(folder)
            proc_cgroup = root / "proc_cgroup"
            cgroup_root = root / "cgroup"
            leaf = cgroup_root / "system.slice" / "run-athena.service"
            leaf.mkdir(parents=True)
            (cgroup_root / "system.slice" / "memory.max").write_text("max\n", encoding="utf-8")
            (leaf / "memory.max").write_text("12884901888\n", encoding="utf-8")
            proc_cgroup.write_text("0::/system.slice/run-athena.service\n", encoding="utf-8")
            self.assertEqual(
                lifecycle.memory_limit_bytes(proc_cgroup, cgroup_root),
                12884901888,
            )


if __name__ == "__main__":
    unittest.main()

