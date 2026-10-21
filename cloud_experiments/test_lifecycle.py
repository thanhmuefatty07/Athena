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
    def test_checksum_mismatch_never_reaches_installer_execution(self):
        import hashlib
        from io import BytesIO

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        payload = b"untrusted installer bytes"
        trusted_digest = hashlib.sha256(b"different trusted bytes").hexdigest()
        with tempfile.TemporaryDirectory(prefix="athena-rustup-mismatch-") as folder:
            with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: trusted_digest}), \
                    patch.object(rustup_bootstrap, "urlopen", return_value=BytesIO(payload)), \
                    patch.object(rustup_bootstrap.subprocess, "run") as execute, \
                    patch.object(rustup_bootstrap, "run_bounded_process", create=True) as bounded:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    rustup_bootstrap.install_rustup(target, folder)
            execute.assert_not_called()
            bounded.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_matching_checksum_runs_only_the_verified_rustup_installer(self):
        import hashlib
        from io import BytesIO
        from types import SimpleNamespace

        from cloud_experiments import rustup_bootstrap

        target = "x86_64-unknown-linux-gnu"
        payload = b"synthetic verified rustup installer"
        trusted_digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(prefix="athena-rustup-match-") as folder:
            def inspect_execution(argv, **kwargs):
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
                    patch.object(rustup_bootstrap, "urlopen", return_value=BytesIO(payload)), \
                    patch.object(rustup_bootstrap, "run_bounded_process", side_effect=inspect_execution,
                                 create=True) as execute, \
                    patch.object(rustup_bootstrap.subprocess, "run",
                                 return_value=subprocess.CompletedProcess([], 0)) as unbounded:
                rustup_bootstrap.install_rustup(target, folder)
            execute.assert_called_once()
            unbounded.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_installer_rejects_timeout_output_limit_and_nonzero_exit(self):
        import hashlib
        from io import BytesIO
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
                with patch.dict(rustup_bootstrap.PINNED_SHA256, {target: trusted_digest}), \
                        patch.object(rustup_bootstrap, "urlopen", return_value=BytesIO(payload)), \
                        patch.object(rustup_bootstrap, "run_bounded_process", return_value=result,
                                     create=True) as bounded, \
                        patch.object(rustup_bootstrap.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], 0)) as unbounded:
                    with self.assertRaises(expected_error):
                        rustup_bootstrap.install_rustup(target, folder)
                bounded.assert_called_once()
                self.assertLessEqual(bounded.call_args.kwargs["timeout"], 1800)
                self.assertLessEqual(bounded.call_args.kwargs["output_limit"], 2 * 1024 ** 2)
                unbounded.assert_not_called()
                self.assertEqual(list(Path(folder).iterdir()), [])

    def test_unsupported_host_architecture_fails_before_download(self):
        from cloud_experiments import rustup_bootstrap

        with tempfile.TemporaryDirectory(prefix="athena-rustup-arch-") as folder:
            with patch.object(rustup_bootstrap, "urlopen") as download:
                with self.assertRaisesRegex(ValueError, "Unsupported Linux architecture"):
                    rustup_bootstrap.install_rustup(None, folder, machine="mips64")
            download.assert_not_called()


class LifecycleControls(unittest.TestCase):
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
            research.campaign.write_json(root / "budget.json", {"reviewed": "synthetic"})
            research.campaign.write_json(root / "job.json", job)
            manifest = {"status": "COMPLETE", "bundle_id": "b" * 64, "spec": spec}
            research.campaign.write_json(output / "manifest.json", manifest)
            def configured_path(value):
                return root if value == "/opt/athena/runs" else Path(value)
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                with self.assertRaisesRegex(ValueError, "identity|job|study"):
                    lifecycle.supervise(root / "job.json")
            self.assertFalse((root / "output-evidence.tgz").exists())
            self.assertEqual(lifecycle.read_json(output / "manifest.json"), manifest)
            manifest.update(bundle_id=spec["study_id"], execution_request={"pilot_only": False, "variants": None,
                "budget_sha256": lifecycle.protocol.sha256_file(root / "budget.json")})
            research.campaign.write_json(output / "manifest.json", manifest)
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                self.assertEqual(lifecycle.supervise(root / "job.json"), 0)
            original = (root / "output-evidence.tgz").read_bytes()
            research.campaign.write_json(root / "budget.json", {"reviewed": "changed budget"})
            with patch.object(lifecycle, "Path", side_effect=configured_path):
                with self.assertRaisesRegex(ValueError, "identity|job|study"):
                    lifecycle.supervise(root / "job.json")
            self.assertEqual((root / "output-evidence.tgz").read_bytes(), original)

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
                "ATHENA_RESEARCH_TABLE_EXTRACTOR",
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
                })
            finally:
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

    def test_finalized_failed_run_keeps_an_exportable_failure_record(self):
        with tempfile.TemporaryDirectory(prefix="athena-failed-export-") as folder:
            root = Path(folder)
            run = root / "run"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"status": "FAILED", "bundle_id": "b" * 64,
                "spec": {"limits": {"export_bytes": 1048576}}, "failed_phase": "measurement"}))
            (run / "run.log").write_text("failure evidence\n")
            outcome = lifecycle.export_finalized_run(run)
            self.assertEqual(outcome["export_status"], "VERIFIED_LOCAL_ARCHIVE")
            self.assertGreater(outcome["export_bytes"], 0)
            with tarfile.open(root / "run-evidence.tgz", "r:gz") as saved:
                self.assertIn("manifest.json", saved.getnames())
                self.assertIn("run.log", saved.getnames())

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


if __name__ == "__main__":
    unittest.main()
