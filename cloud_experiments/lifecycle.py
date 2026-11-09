"""Durable balanced-block publication and bounded research process ownership."""

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from cloud_experiments import campaign, process_isolation
from kaggle_experiments import protocol, runner

MAX_SUPERVISOR_ATTEMPTS = 3


class IntegrityError(ValueError):
    pass


class StorageGuardError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


_RESEARCH_CHILD_ENV = frozenset({
    "APPDATA",
    "ATHENA_CORE_PROBE_BIN",
    "ATHENA_LEXBOR_ADAPTER_TARGET_DIR",
    "ATHENA_LEXBOR_ARCHIVE_CACHE",
    "ATHENA_LEXBOR_BUILD_DIR",
    "ATHENA_LEXBOR_LIB_DIR",
    "ATHENA_LEXBOR_SOURCE_DIR",
    "ATHENA_LEXBOR_WORK_ROOT",
    "ATHENA_MCP_RUST_BIN",
    "ATHENA_MCP_TS_SERVER",
    "ATHENA_OFFLINE",
    "ATHENA_RESEARCH_HASH",
    "ATHENA_RESEARCH_PREPARED",
    "ATHENA_RESEARCH_RANK_MODEL",
    "ATHENA_RESEARCH_RESERVE",
    "ATHENA_RESEARCH_TABLE_EXTRACTOR",
    "ATHENA_RESEARCH_WRITER",
    "ATHENA_WPT_REPORT_ONLY",
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_HOME",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
    "CARGO_PROFILE_RELEASE_LTO",
    "CARGO_TARGET_DIR",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NODE_PATH",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUTF8",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USERPROFILE",
    "WINDIR",
})


def _research_child_environment():
    return {key: value for key, value in os.environ.items()
            if key.upper() in _RESEARCH_CHILD_ENV}


def read_bytes(path, limit=8 * 1024 ** 2):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or linked lifecycle input")
    chunks, size = [], 0
    with path.open("rb") as stream:
        while size <= limit:
            chunk = stream.read(min(65536, limit - size + 1))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    if size > limit:
        raise ValueError("Lifecycle input exceeds byte bound")
    return b"".join(chunks)


def read_json(path, limit=8 * 1024 ** 2):
    return protocol.strict_json_loads(read_bytes(path, limit))


class CollectionJournal:
    def __init__(self, root, context, total_limit, session_id, *, metadata_limit=64 * 1024 ** 2, block_limit=256 * 1024 ** 2):
        if type(metadata_limit) is not int or metadata_limit < 0:
            raise ValueError("Invalid collection metadata capacity")
        context = deepcopy(context)
        self.root = Path(root).resolve(strict=True)
        self.folder = self.root / "collection"
        if self.folder.is_symlink():
            raise ValueError("Collection cannot escape its owned root")
        self.folder.mkdir(exist_ok=True)
        self.context, self.session_id = context, session_id
        self.total_limit, self.total_bytes = total_limit, 0
        self.metadata_limit, self.metadata_bytes = metadata_limit, 0
        self.block_limit = min(block_limit, total_limit)
        self.commits = {}
        if type(total_limit) is not int or total_limit < 1:
            raise ValueError("Invalid collection capacity")
        self.binding_path = self.root / "collection-bindings.json"
        self.bindings = {"context": context, "channels": {}}
        if self.binding_path.exists():
            self.bindings = self._read_metadata(self.binding_path)
            if self.bindings.get("context") != context or not isinstance(self.bindings.get("channels"), dict):
                raise ValueError("Collection source or execution environment changed")
        for path in self.folder.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Unexpected collection entry")
            if path.name.startswith(".pending-"):
                if not protocol.is_digest(path.name[9:]):
                    raise ValueError("Unknown pending collection owner")
                self._discard_pending(path)
                continue
            if not protocol.is_digest(path.name):
                raise ValueError("Unknown committed collection owner")
            receipt = self._read_metadata(path / "receipt.json")
            data = path / "samples.jsonl"
            if (receipt.get("version") != "athena-collection-block/1"
                    or receipt.get("context") != context
                    or digest(receipt.get("descriptor")) != path.name
                    or receipt.get("session_id") is None
                    or set(p.name for p in path.iterdir()) != {"samples.jsonl", "receipt.json"}):
                raise ValueError("Committed collection identity or bytes changed")
            if data.is_symlink() or not data.is_file():
                raise ValueError("Committed collection identity or bytes changed")
            data_bytes = data.stat().st_size
            if data_bytes > self.block_limit or data_bytes > total_limit - self.total_bytes:
                raise ValueError("Committed collection exceeds capacity")
            if (data_bytes != receipt.get("bytes")
                    or protocol.sha256_file(data) != receipt.get("sha256")):
                raise ValueError("Committed collection identity or bytes changed")
            self.total_bytes += data_bytes
            self.commits[path.name] = receipt
        if self.metadata_bytes > metadata_limit:
            raise ValueError("Committed collection exceeds capacity")

    def _read_metadata(self, path):
        path = Path(path)
        remaining = self.metadata_limit - self.metadata_bytes
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or linked lifecycle input")
        if path.stat().st_size > remaining:
            raise ValueError("Committed collection exceeds metadata capacity")
        limit = min(8 * 1024 ** 2, remaining)
        try:
            data = read_bytes(path, limit)
        except ValueError as error:
            if str(error) == "Lifecycle input exceeds byte bound" and remaining <= 8 * 1024 ** 2:
                raise ValueError("Committed collection exceeds metadata capacity") from None
            raise
        self.metadata_bytes += len(data)
        return protocol.strict_json_loads(data)

    def _discard_pending(self, path):
        if path.parent != self.folder or path.is_symlink():
            raise ValueError("Pending collection escape")
        for child in path.iterdir():
            temporary = (child.name.startswith(".receipt.json.") and child.name.endswith(".tmp")
                         and len(child.name[len(".receipt.json."):-4]) == 8
                         and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in child.name[len(".receipt.json."):-4]))
            if (child.name not in ("samples.jsonl", "receipt.json") and not temporary
                    or child.is_symlink() or not child.is_file()):
                raise ValueError("Unexpected pending collection content")
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
        campaign.sync_directory(self.folder)

    @property
    def shards(self):
        return [(self.folder / key / "samples.jsonl").relative_to(self.root).as_posix()
                for key in sorted(self.commits)]

    def collect(self, descriptor, produce):
        descriptor = deepcopy(descriptor)
        channel, binding = descriptor["channel"], descriptor["binding"]
        previous = self.bindings["channels"].get(channel)
        if previous is not None and previous != binding:
            raise ValueError("Collection binary, fixture or measurement settings changed")
        if previous is None:
            replacement = {**self.bindings, "channels": {**self.bindings["channels"], channel: binding}}
            old_bytes = self.binding_path.stat().st_size if self.binding_path.exists() else 0
            campaign.write_json(self.binding_path, replacement,
                                max_bytes=self.metadata_limit - self.metadata_bytes + old_bytes)
            self.bindings = replacement
            self.metadata_bytes += self.binding_path.stat().st_size - old_bytes
        key = digest(descriptor)
        target = self.folder / key
        if key not in self.commits:
            pending = self.folder / (".pending-" + key)
            if pending.exists():
                self._discard_pending(pending)
            pending.mkdir(mode=0o700)
            size, count, raw_digest = 0, 0, hashlib.sha256()
            started = time.monotonic()
            try:
                with (pending / "samples.jsonl").open("xb") as stream:
                    for row in produce():
                        data = canonical(dict(row, session_id=self.session_id)) + b"\n"
                        if (len(data) > 65536 or len(data) > self.total_limit - self.total_bytes - size
                                or len(data) > self.block_limit - size):
                            raise ValueError("Collection capacity exceeded before write")
                        stream.write(data)
                        raw_digest.update(data)
                        size += len(data)
                        count += 1
                    stream.flush()
                    os.fsync(stream.fileno())
                if count == 0:
                    raise ValueError("Empty collection block")
                receipt = {"version": "athena-collection-block/1", "context": self.context,
                           "descriptor": descriptor, "session_id": self.session_id,
                           "collection_wall_seconds": time.monotonic() - started,
                           "bytes": size, "rows": count, "sha256": raw_digest.hexdigest()}
                campaign.write_json(pending / "receipt.json", receipt, max_bytes=self.metadata_limit - self.metadata_bytes)
                campaign.sync_directory(pending)
                os.rename(pending, target)
                campaign.sync_directory(self.folder)
                self.commits[key] = receipt
                self.total_bytes += size
                self.metadata_bytes += (target / "receipt.json").stat().st_size
            except BaseException:
                if pending.exists():
                    self._discard_pending(pending)
                raise
        receipt = self.commits[key]
        if receipt["descriptor"] != descriptor:
            raise ValueError("Collection descriptor collision")
        count = 0
        with (target / "samples.jsonl").open("rb") as stream:
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536 or not line.endswith(b"\n"):
                    raise ValueError("Incomplete committed collection row")
                row = protocol.strict_json_loads(line)
                if row.get("session_id") != receipt["session_id"]:
                    raise ValueError("A balanced block mixes execution sessions")
                count += 1
                yield row
        if count != receipt["rows"]:
            raise ValueError("Committed collection row count mismatch")


def process_tree_rss(pid):
    if os.name != "posix" or not Path("/proc").is_dir():
        return None
    pending, visited, total = [pid], set(), 0
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        try:
            status = Path(f"/proc/{current}/status").read_text()
            total += next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:"))
        except (OSError, StopIteration, ValueError):
            continue
        task_root = Path(f"/proc/{current}/task")
        try:
            tids = [entry.name for entry in task_root.iterdir() if entry.name.isdigit()]
        except OSError:
            tids = [str(current)]
        for tid in tids:
            try:
                children = (task_root / tid / "children").read_text().split()
            except OSError:
                continue
            pending.extend(int(child) for child in children if child.isdecimal())
    return total


def memory_peak_bytes():
    if os.name != "posix":
        return None
    import resource
    raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = raw_rss if sys.platform == "darwin" else raw_rss * 1024
    try:
        line = next(line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
        path = Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "memory.peak"
        peak = max(peak, int(path.read_text()))
    except (OSError, StopIteration, ValueError):
        pass
    return peak


def memory_limit_bytes(cgroup_proc="/proc/self/cgroup", cgroup_root="/sys/fs/cgroup"):
    line = next(line for line in Path(cgroup_proc).read_text().splitlines() if line.startswith("0::"))
    root = Path(cgroup_root).resolve(strict=True)
    folder = (root / line[3:].lstrip("/")).resolve(strict=True)
    if not folder.is_relative_to(root):
        raise ValueError("Cgroup hierarchy escaped its mount")
    limits = []
    while folder.is_relative_to(root):
        max_file = folder / "memory.max"
        if folder == root and not max_file.exists():
            break
        value = max_file.read_text().strip()
        if value != "max":
            limits.append(int(value))
        if folder == root:
            break
        folder = folder.parent
    return min(limits) if limits else None


def tree_bytes(folder, *, max_entries=2000000):
    folder = Path(folder)
    if not folder.exists() and not folder.is_symlink():
        return 0
    total, entries, pending = 0, 0, [folder]
    while pending:
        path = pending.pop()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("Resource tree contains a linked entry")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                with os.scandir(path) as stream:
                    for entry in stream:
                        pending.append(Path(entry.path))
                        entries += 1
                        if entries > max_entries:
                            raise ValueError("Resource tree entry bound exceeded")
            except FileNotFoundError:
                continue
        elif stat.S_ISREG(metadata.st_mode):
            total += metadata.st_size
    return total


def reusable_storage_bytes(paths, *, device, deadline, max_entries=2000000):
    if type(device) is not int or type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("Invalid reusable storage boundary")
    total, entries, seen, pending = 0, 0, set(), [Path(path) for path in paths]
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError("Reusable storage scan exceeded its deadline")
        path = pending.pop()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("Reusable storage contains a linked entry")
        if metadata.st_dev != device:
            raise ValueError("Reusable storage crossed a filesystem boundary")
        if stat.S_ISDIR(metadata.st_mode):
            try:
                with os.scandir(path) as stream:
                    for entry in stream:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Reusable storage scan exceeded its deadline")
                        entries += 1
                        if entries > max_entries:
                            raise ValueError("Reusable storage entry bound exceeded")
                        pending.append(Path(entry.path))
            except FileNotFoundError:
                continue
        elif stat.S_ISREG(metadata.st_mode):
            key = (metadata.st_dev, metadata.st_ino)
            if key in seen:
                continue
            seen.add(key)
            blocks = getattr(metadata, "st_blocks", 0)
            if metadata.st_nlink == 1 and type(blocks) is int and blocks >= 0:
                total += min(metadata.st_size, blocks * 512)
        else:
            raise ValueError("Reusable storage contains a non-regular entry")
    return total


def _rotate_gzip_file(path: Path, manifest: dict, list_key: str, max_rotations: int = 64):
    import gzip
    rotations = manifest.setdefault(list_key, [])
    if len(rotations) >= max_rotations:
        raise ValueError("Rotated log archive capacity exceeded")
    idx = len(rotations)
    gz_name = f"{path.name}.{idx:04d}.gz"
    gz_path = path.with_name(gz_name)
    raw = path.read_bytes()
    with gzip.open(gz_path, "wb", compresslevel=6) as gz_out:
        gz_out.write(raw)
    rotations.append({
        "archive": gz_name,
        "uncompressed_bytes": len(raw),
        "compressed_bytes": gz_path.stat().st_size,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    path.write_bytes(b"")


class ManagedRun(runner.EvidenceRun):
    def __init__(self, directory, cluster, bundle_id, spec, *, resume=False, isolate_children=False):
        if type(isolate_children) is not bool:
            raise TypeError("Managed child-isolation selector must be a boolean")
        self.isolate_children = isolate_children
        self.process_isolation_policy = process_isolation.POLICY if isolate_children else "DISABLED"
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("Run directory cannot be linked")
        if resume and directory.exists():
            self.directory = directory.resolve(strict=True)
            self.manifest = read_json(self.directory / "manifest.json", 16 * 1024 ** 2)
            if (self.manifest.get("bundle_id") != bundle_id or self.manifest.get("spec") != spec
                    or self.manifest.get("lifecycle") != "athena-managed-run/1"):
                raise ValueError("Resume source or study identity changed")
            if self.manifest.get("process_isolation_policy") != self.process_isolation_policy:
                raise ValueError("Resume process-isolation policy changed or is absent")
            self.log_path, self.samples_path = self.directory / "run.log", self.directory / "samples.jsonl"
            if self.log_path.is_symlink() or self.samples_path.is_symlink():
                raise ValueError("Linked run output")
            self.sample_stream = self.samples_path.open("a", encoding="utf-8")
            self.deadline = time.monotonic() + spec.get("limits", {}).get("wall_seconds", 5400)
        else:
            super().__init__(directory, cluster, bundle_id, spec)
            self.manifest["lifecycle"] = "athena-managed-run/1"
            self.manifest["process_isolation_policy"] = self.process_isolation_policy
        self.session_id = uuid.uuid4().hex
        self.campaign_session_id = self.manifest.setdefault("campaign_session_id", self.session_id)
        self.collection = None
        self.manifest.setdefault("sessions", []).append({
            "id": self.session_id,
            "campaign_session_id": self.campaign_session_id,
            "started_at": runner.utc_now(),
        })
        self.manifest["status"] = "RUNNING"
        self.save()

    def save(self):
        campaign.write_json(self.directory / "manifest.json", self.manifest, max_bytes=16 * 1024 ** 2)

    def finish(self, error=None):
        if self.manifest.get("status") in ("COMPLETE", "FAILED") and self.manifest.get("finished_at"):
            self.save()
            return
        self.sample_stream.close()
        self.manifest["status"] = "FAILED" if error else "COMPLETE"
        self.manifest["finished_at"] = runner.utc_now()
        if error:
            self.manifest["error"] = repr(error)
            self.log("FAILED " + repr(error))
        self.manifest["artifacts"] = {
            path.name: protocol.sha256_file(path) for path in self.directory.iterdir()
            if path.is_file() and path.name not in ("manifest.json", "export-receipt.json", "export-ack.json")}
        self.save()

    def bind_deadline(self, seconds, *, deadline_unix=None):
        if type(seconds) is not int or seconds < 1:
            raise ValueError("A finite campaign deadline is required")
        previous = self.manifest.get("deadline_duration_seconds")
        if previous is not None and previous != seconds:
            raise ValueError("Resume cannot reset its spending/deadline window")
        previous_deadline = self.manifest.get("deadline_unix")
        if deadline_unix is None:
            deadline_unix = previous_deadline if previous_deadline is not None else time.time() + seconds
        if (type(deadline_unix) not in (int, float) or not math.isfinite(deadline_unix)
                or (previous_deadline is not None and previous_deadline != deadline_unix)):
            raise ValueError("Resume cannot reset its absolute campaign deadline")
        self.manifest.setdefault("deadline_duration_seconds", seconds)
        self.manifest.setdefault("deadline_unix", deadline_unix)
        self.deadline = time.monotonic() + self.manifest["deadline_unix"] - time.time()
        if self.deadline <= time.monotonic():
            error = TimeoutError("Persisted campaign deadline exhausted")
            self.finish(error)
            raise error
        self.save()

    def check(self, name, action, *, persist_across_sessions=False):
        previous = self.manifest["checks"].get(name, {})
        if previous.get("status") == "PASS":
            if previous.get("session_id") == self.session_id:
                return previous.get("evidence")
            if persist_across_sessions and previous.get("campaign_session_id") == self.campaign_session_id:
                return previous.get("evidence")
        self.manifest["checks"][name] = {
            "status": "RUNNING",
            "session_id": self.session_id,
            "campaign_session_id": self.campaign_session_id,
        }
        self.save()
        result = super().check(name, action)
        self.manifest["checks"][name]["session_id"] = self.session_id
        self.manifest["checks"][name]["campaign_session_id"] = self.campaign_session_id
        self.save()
        return result

    def log(self, message):
        data = (f"[{runner.utc_now()}] {message}\n").encode("utf-8")
        limits = self.manifest.get("spec", {}).get("limits", {})
        bound = limits.get("log_bytes", 64 * 1024 ** 2)
        if self.log_path.is_symlink() or len(data) > bound:
            raise ValueError("Run log capacity exceeded")
        if self.log_path.exists() and self.log_path.stat().st_size + len(data) > bound:
            if limits.get("rotate_logs_gzip", False):
                _rotate_gzip_file(self.log_path, self.manifest, "rotated_run_logs")
            else:
                raise ValueError("Run log capacity exceeded")
        with self.log_path.open("ab") as stream:
            stream.write(data)

    def guard_storage(self):
        limits = self.manifest.get("spec", {}).get("limits", {})
        reserve = limits.get("disk_reserve_bytes", 0)
        free_bytes = shutil.disk_usage(self.directory).free
        if free_bytes < reserve:
            self.manifest["storage_probe"] = {
                "free_bytes": free_bytes,
                "scope": "Disk-reserve sample; aggregate tree not scanned after reserve breach",
            }
            raise StorageGuardError("DISK_RESERVE", "Research disk reserve exhausted")
        if "build_bytes" in limits:
            cache = self.directory.parent / "cargo-target"
            binaries = tree_bytes(self.directory / "bins")
            build = tree_bytes(cache) + tree_bytes(self.directory / "work") + binaries
            analysis = sum(path.stat().st_size for path in self.directory.glob("confirmation-*.json"))
            self.manifest["storage_probe"] = {"build_bytes": build, "binary_bytes": binaries,
                                               "analysis_bytes": analysis, "free_bytes": free_bytes,
                                               "scope": "Command-boundary and one-second Cargo samples; not a filesystem quota"}
            if build > limits["build_bytes"] or binaries > limits["binary_bytes"]:
                raise StorageGuardError("BUILD_CAPACITY", "Research build/binary capacity exhausted")
            if analysis > limits["analysis_bytes"]:
                raise StorageGuardError("ANALYSIS_CAPACITY", "Research analysis capacity exhausted")

    def run(self, command, cwd=None, timeout=300, input_bytes=None, log_output=True, *,
            monitor_build_tree=None, network_mode="deny", redact_output=False):
        command = [str(value) for value in command]
        if type(redact_output) is not bool:
            raise TypeError("Child-output redaction selector must be a boolean")
        execution_command = process_isolation.command_argv(
            command, required=self.isolate_children, network_mode=network_mode)
        command_isolation_policy = (
            process_isolation.CARGO_FETCH_POLICY
            if network_mode == process_isolation.CARGO_FETCH_NETWORK_MODE
            else process_isolation.POLICY if self.isolate_children else "DISABLED")
        limits = self.manifest.get("spec", {}).get("limits", {})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Experiment wall-clock budget exhausted")
        timeout = min(timeout, remaining)
        output_limit = limits.get("command_output_bytes", 8 * 1024 ** 2)
        now = time.monotonic()
        cargo = Path(command[0]).name in ("cargo", "cargo.exe")
        if monitor_build_tree is not None and type(monitor_build_tree) is not bool:
            raise TypeError("Build-tree monitoring selector must be a boolean")
        monitor_build_tree = cargo or monitor_build_tree is True
        if monitor_build_tree or now - getattr(self, "last_storage_check", 0) >= 30:
            self.guard_storage()
            self.last_storage_check = now
        buffers, held, failure = [bytearray(), bytearray()], [0], []
        guard = threading.Lock()
        process = subprocess.Popen(execution_command, cwd=cwd, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=(os.name == "posix"),
                                   env=_research_child_environment())

        def terminate():
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

        def drain(index, pipe):
            try:
                while True:
                    chunk = pipe.read(65536)
                    if not chunk:
                        break
                    with guard:
                        if len(chunk) > output_limit - held[0]:
                            failure.append("OUTPUT_LIMIT")
                            terminate()
                            break
                        buffers[index].extend(chunk)
                        held[0] += len(chunk)
            finally:
                pipe.close()

        def feed():
            try:
                process.stdin.write(input_bytes)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        threads = [threading.Thread(target=drain, args=(index, pipe), daemon=True)
                   for index, pipe in enumerate((process.stdout, process.stderr))]
        if input_bytes is not None:
            threads.append(threading.Thread(target=feed, daemon=True))
        started_threads = []
        started, peak, next_disk_check = time.monotonic(), None, time.monotonic() + 1
        next_storage_check = next_disk_check
        storage_error = None
        try:
            for thread in threads:
                try:
                    thread.start()
                finally:
                    if thread.ident is not None:
                        started_threads.append(thread)
            while process.poll() is None:
                rss = process_tree_rss(process.pid)
                if rss is not None:
                    peak = max(peak or 0, rss)
                if time.monotonic() - started >= timeout:
                    failure.append("TIMEOUT")
                    terminate()
                    break
                if time.monotonic() >= next_disk_check:
                    next_disk_check += 1
                    if shutil.disk_usage(self.directory).free < limits.get("disk_reserve_bytes", 0):
                        failure.append("DISK_RESERVE")
                        terminate()
                        break
                if monitor_build_tree and time.monotonic() >= next_storage_check:
                    next_storage_check = time.monotonic() + 1
                    try:
                        self.guard_storage()
                    except StorageGuardError as error:
                        storage_error = str(error)
                        failure.append(error.code)
                        terminate()
                        break
                    except Exception as error:
                        storage_error = f"{type(error).__name__}: {error}"
                        failure.append("STORAGE_GUARD")
                        terminate()
                        break
                time.sleep(.05)
            process.wait(timeout=10)
        except BaseException:
            terminate()
            process.wait(timeout=10)
            raise
        finally:
            if os.name == "posix":
                terminate()
            for thread in started_threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in started_threads):
                terminate()
                failure.append("PIPE_LIFETIME")
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        if monitor_build_tree:
            try:
                self.guard_storage()
            except StorageGuardError as error:
                storage_error = str(error)
                if not failure:
                    failure.append(error.code)
            except Exception as error:
                storage_error = f"{type(error).__name__}: {error}"
                if not failure:
                    failure.append("STORAGE_GUARD")
        stdout, stderr = map(bytes, buffers)
        record = {"argv": command, "execution_argv": execution_command,
                  "process_isolation_policy": command_isolation_policy,
                  "cwd": str(cwd), "exit_code": process.returncode,
                  "wall_ns": round((time.monotonic() - started) * 1e9),
                  "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                  "sampled_process_tree_rss_bytes": peak, "status": failure[0] if failure else "EXITED"}
        if monitor_build_tree and "storage_probe" in self.manifest:
            record["storage_probe"] = dict(self.manifest["storage_probe"])
        if storage_error is not None:
            record["storage_guard_error"] = storage_error
        self.manifest["commands"].append(record)
        self.manifest["commands"] = self.manifest["commands"][-64:]
        self.manifest["command_count"] = self.manifest.get("command_count", 0) + 1
        if peak is not None:
            self.manifest["sampled_process_tree_rss_bytes"] = max(self.manifest.get("sampled_process_tree_rss_bytes", 0), peak)
        events = self.directory / "commands.jsonl"
        event = canonical(record) + b"\n"
        meta_bound = limits.get("metadata_bytes", 64 * 1024 ** 2)
        if events.is_symlink() or len(event) > meta_bound:
            raise ValueError("Command metadata capacity exceeded")
        if events.exists() and events.stat().st_size + len(event) > meta_bound:
            if limits.get("rotate_logs_gzip", False):
                _rotate_gzip_file(events, self.manifest, "rotated_command_logs")
            else:
                raise ValueError("Command metadata capacity exceeded")
        with events.open("ab") as stream:
            stream.write(event)
        self.log("RESULT " + json.dumps(record))
        if redact_output and (log_output or process.returncode):
            self.log("CHILD_OUTPUT_REDACTED " + json.dumps({
                "stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
                "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest()}))
        elif log_output or process.returncode:
            self.log((stdout + stderr).decode("utf-8", errors="replace"))
        self.save()
        if failure:
            if failure[0] == "TIMEOUT":
                raise TimeoutError("Supervised process timed out")
            raise RuntimeError("Supervised process exceeded " + failure[0])
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return stdout


class BoundedArchive:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.bytes = stream, limit, 0

    def write(self, data):
        if len(data) > self.limit - self.bytes:
            raise ValueError("Export capacity exceeded before write")
        self.stream.write(data)
        self.bytes += len(data)
        return len(data)

    def tell(self):
        return self.bytes

    def flush(self):
        self.stream.flush()


def _verify_export(path, inventory):
    observed = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if (not member.isfile() or member.name not in inventory or member.name in observed
                    or member.size != inventory[member.name]["bytes"]):
                raise ValueError("Export inventory changed")
            raw_digest = hashlib.sha256()
            with archive.extractfile(member) as source:
                for block in iter(lambda: source.read(1048576), b""):
                    raw_digest.update(block)
            observed[member.name] = {"bytes": member.size, "sha256": raw_digest.hexdigest()}
    if observed != inventory:
        raise ValueError("Export round-trip mismatch")


def _export_receipt(directory, destination, manifest, inventory):
    receipt = {"schema": "athena-export/1", "archive_name": destination.name,
               "bytes": destination.stat().st_size, "sha256": protocol.sha256_file(destination),
               "study_id": manifest["bundle_id"], "files": inventory,
               "copy_verified": False, "result_authenticity": "NOT VERIFIED: same OS authority"}
    campaign.write_json(directory / "export-receipt.json", receipt)
    return receipt


def export_run(directory, destination):
    directory = Path(directory).resolve(strict=True)
    destination = Path(destination)
    if destination.is_symlink() or destination.resolve().is_relative_to(directory):
        raise ValueError("Export must use an owned file outside the run")
    manifest = read_json(directory / "manifest.json", 16 * 1024 ** 2)
    if manifest.get("status") not in ("COMPLETE", "FAILED"):
        raise ValueError("Active collection cannot be exported")
    limit = manifest.get("spec", {}).get("limits", {}).get("export_bytes", 8 * 1024 ** 3)
    inventory = {}
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        if name.split("/")[0] == "work" or name in ("export-receipt.json", "export-ack.json"):
            continue
        if any(part.startswith(".pending-") for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError("Export input cannot be linked or escape the run")
        if path.is_file():
            inventory[name] = {"bytes": path.stat().st_size, "sha256": protocol.sha256_file(path)}
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size > limit:
            raise ValueError("Invalid existing export")
        _verify_export(destination, inventory)
        return _export_receipt(directory, destination, manifest, inventory)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + destination.name + ".", suffix=".tmp", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            bounded = BoundedArchive(stream, limit)
            with tarfile.open(fileobj=bounded, mode="w|gz") as archive:
                for name, expected in inventory.items():
                    path = directory / name
                    if path.is_symlink() or path.stat().st_size != expected["bytes"] or protocol.sha256_file(path) != expected["sha256"]:
                        raise ValueError("Export input changed during capture")
                    info = archive.gettarinfo(str(path), arcname=name)
                    with path.open("rb") as source:
                        archive.addfile(info, source)
            stream.flush()
            os.fsync(stream.fileno())
        _verify_export(temporary, inventory)
        try:
            # Same-directory hard-link publication is atomic and never replaces an existing file.
            os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or not destination.is_file() or destination.stat().st_size > limit:
                raise ValueError("Conflicting existing export") from None
            _verify_export(destination, inventory)
        campaign.sync_directory(destination.parent)
        return _export_receipt(directory, destination, manifest, inventory)
    finally:
        temporary.unlink(missing_ok=True)


def acknowledge_export(directory, copy_path):
    directory, copy_path = Path(directory).resolve(strict=True), Path(copy_path)
    receipt = read_json(directory / "export-receipt.json", 16 * 1024 ** 2)
    original = directory.parent / receipt["archive_name"]
    if copy_path.is_symlink() or not copy_path.is_file() or (original.exists() and os.path.samefile(original, copy_path)):
        raise ValueError("Acknowledgement requires a verified distinct copy")
    if copy_path.stat().st_size != receipt["bytes"] or protocol.sha256_file(copy_path) != receipt["sha256"]:
        raise ValueError("Export copy size/checksum mismatch")
    acknowledgement = {"schema": "athena-export-ack/1", "copy_verified": True,
                       "sha256": receipt["sha256"], "bytes": receipt["bytes"], "study_id": receipt["study_id"],
                       "independent_collector": "NOT VERIFIED: caller-supplied copy; no execution authenticity claim"}
    campaign.write_json(directory / "export-ack.json", acknowledgement)
    return acknowledgement


def render_unit(memory_bytes, source_path="/opt/athena/current"):
    if type(memory_bytes) is not int or memory_bytes < 512 * 1024 ** 2:
        raise ValueError("Explicit supervisor memory cap required")
    if not isinstance(source_path, str):
        raise ValueError("Explicit owned source path required")
    source = PurePosixPath(source_path)
    if (str(source) != source_path or ".." in source.parts
            or source != PurePosixPath("/opt/athena/current") and not (
                source.name == "source" and source.parent.parent == PurePosixPath("/opt/athena/runs")
                and 1 <= len(source.parent.name) <= 80
                and all(c.isascii() and (c.isalnum() or c in "-_") for c in source.parent.name))):
        raise ValueError("Supervisor source must be a fixed owned source path")
    return f"""[Unit]
Description=Athena bounded resumable research
After=network-online.target
Wants=network-online.target
OnSuccess=athena-research-poweroff.service
OnFailure=athena-research-poweroff.service
ConditionPathExists=/opt/athena/active-job.json
StartLimitIntervalSec=3600
StartLimitBurst=3

[Service]
Type=exec
User=athena-research
Group=athena-research
WorkingDirectory={source_path}
Environment=PATH=/home/athena-research/.cargo/bin:/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -B -m cloud_experiments.lifecycle supervise /opt/athena/active-job.json
ExecStopPost=/bin/bash {source_path}/cloud_experiments/finish.sh
RuntimeMaxSec=infinity
TimeoutStartSec=120
TimeoutStopSec=90
KillMode=control-group
Restart=on-failure
RestartMode=direct
RestartPreventExitStatus=78
RestartSec=30
MemoryAccounting=yes
MemoryMax={memory_bytes}
TasksMax=256
LimitNOFILE=8192
LimitFSIZE=34359738368
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/opt/athena/runs
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


def render_poweroff_unit():
    """Install root-owned; no command or path is obtained from research output."""
    return """[Unit]
Description=Stop the dedicated Athena VM after terminal research outcome
After=athena-research.service
ConditionPathExists=/opt/athena/active-job.json

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/systemctl --no-block poweroff
TimeoutStartSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
"""


def _is_managed_export_name(output_name, name):
    prefix, legacy = output_name + "-evidence-", output_name + "-evidence.tgz"
    return (name == legacy or name.startswith(prefix) and name.endswith(".tgz")
            or (name.startswith("." + prefix) or name.startswith("." + legacy + ".")) and name.endswith(".tmp"))


def _run_bounded_export(output, campaign_deadline_unix):
    output = Path(output).resolve(strict=True)
    manifest_bytes = read_bytes(output / "manifest.json", 16 * 1024 ** 2)
    manifest = protocol.strict_json_loads(manifest_bytes)
    export_deadline = manifest.get("export_deadline_unix")
    recorded_campaign_deadline = manifest.get("campaign_deadline_unix")
    shutdown_seconds = manifest.get("shutdown_seconds")
    if (type(campaign_deadline_unix) not in (int, float) or not math.isfinite(campaign_deadline_unix)
            or type(recorded_campaign_deadline) not in (int, float)
            or not math.isfinite(recorded_campaign_deadline)
            or recorded_campaign_deadline != campaign_deadline_unix
            or type(export_deadline) not in (int, float) or not math.isfinite(export_deadline)
            or type(shutdown_seconds) is not int or shutdown_seconds < 0):
        raise ValueError("Missing or changed frozen export deadline")
    deadline_unix = min(export_deadline, campaign_deadline_unix - shutdown_seconds)
    timeout = deadline_unix - time.time()
    if timeout <= 0:
        raise TimeoutError("Reserved evidence export deadline exhausted")
    export_limit = manifest.get("spec", {}).get("limits", {}).get("export_bytes", 8 * 1024 ** 3)
    if type(export_limit) is not int or export_limit < 1:
        raise ValueError("Invalid managed export byte bound")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    prefix = output.name + "-evidence-"
    destination = output.with_name(prefix + manifest_sha256 + ".tgz")
    archive_count = 0
    for path in output.parent.iterdir():
        if time.time() >= deadline_unix:
            raise TimeoutError("Reserved evidence export deadline exhausted")
        if _is_managed_export_name(output.name, path.name):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > export_limit:
                raise ValueError("Invalid retained managed export")
            archive_count += 1
            if archive_count > MAX_SUPERVISOR_ATTEMPTS:
                raise StorageGuardError("EXPORT_CAPACITY", "Managed export snapshot capacity exhausted")
    if not destination.exists() and archive_count >= MAX_SUPERVISOR_ATTEMPTS:
        raise StorageGuardError("EXPORT_CAPACITY", "Managed export snapshot capacity exhausted")
    timeout = deadline_unix - time.time()
    if timeout <= 0:
        raise TimeoutError("Reserved evidence export deadline exhausted")
    result = runner.run_bounded_process(
        [sys.executable, "-B", "-m", "cloud_experiments.lifecycle", "export",
         str(output), str(destination), "--summary"],
        timeout=timeout, output_limit=4096,
        cwd=Path(__file__).resolve().parent.parent,
        env=_research_child_environment())
    if result.status == "TIMEOUT":
        raise TimeoutError("Reserved evidence export deadline exhausted")
    if result.status != "EXITED" or result.returncode != 0:
        raise RuntimeError("Bounded evidence export failed: " + result.status)
    summary = protocol.strict_json_loads(result.stdout)
    if (not isinstance(summary, dict)
            or set(summary) != {"schema", "archive_name", "bytes", "sha256", "study_id", "copy_verified"}
            or summary.get("schema") != "athena-export/1"
            or summary.get("archive_name") != destination.name
            or summary.get("study_id") != manifest.get("bundle_id")
            or type(summary.get("bytes")) is not int or summary["bytes"] < 1
            or not protocol.is_digest(summary.get("sha256"))
            or summary.get("copy_verified") is not False
            or destination.is_symlink() or not destination.is_file()
            or destination.stat().st_size != summary["bytes"]):
        raise ValueError("Bounded export worker returned an invalid receipt summary")
    if read_bytes(output / "manifest.json", 16 * 1024 ** 2) != manifest_bytes:
        raise IntegrityError("Finalized manifest changed during export")
    return {"export_status": "VERIFIED_LOCAL_ARCHIVE", "export_sha256": summary["sha256"],
            "export_bytes": summary["bytes"], "export_archive_name": destination.name,
            "export_manifest_sha256": manifest_sha256}


def export_finalized_run(output, *, campaign_deadline_unix=None):
    from cloud_experiments import research
    output = Path(output).resolve(strict=True)
    manifest_path = output / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {"export_status": "NO_FINALIZED_MANIFEST"}
    manifest = read_json(manifest_path, 16 * 1024 ** 2)
    if manifest.get("status") not in ("COMPLETE", "FAILED"):
        return {"export_status": "RUN_NOT_FINALIZED"}
    if campaign_deadline_unix is None:
        return {"export_status": "NO_FROZEN_CAMPAIGN_DEADLINE"}
    with research._analysis_lock(output.with_name(output.name + ".export.lock")):
        return _run_bounded_export(output, campaign_deadline_unix)


def supervise(job_path):
    from cloud_experiments import research
    job = read_json(job_path)
    required = {"source", "spec", "output", "budget"}
    if not required <= set(job) or set(job) - required - {"pilot_only", "variants"}:
        raise ValueError("Unknown or incomplete supervisor job")
    root = Path("/opt/athena/runs").resolve(strict=True)
    for key in required:
        path = Path(job[key])
        if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("Supervisor job path escaped its research root")
    output = Path(job["output"])
    state_path = output.with_name(output.name + ".supervisor.json")
    budget_bytes = read_bytes(job["budget"])
    budget = protocol.strict_json_loads(budget_bytes)
    budget_sha256 = hashlib.sha256(budget_bytes).hexdigest()
    budget_seconds = budget.get("deadline_seconds")
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("Supervisor requires a finite reviewed campaign deadline")
    state_exists = state_path.exists()
    state = read_json(state_path) if state_exists else {
        "job_digest": digest(job), "attempts": 0,
        "campaign_deadline_unix": time.time() + budget_seconds,
        "budget_sha256": budget_sha256,
    }
    if state.get("job_digest") != digest(job):
        raise ValueError("Supervisor job identity changed")
    if state.get("budget_sha256") != budget_sha256:
        raise ValueError("Supervisor budget identity changed")
    campaign_deadline_unix = state.get("campaign_deadline_unix")
    if (type(campaign_deadline_unix) not in (int, float) or not math.isfinite(campaign_deadline_unix)):
        raise ValueError("Supervisor campaign deadline is missing or invalid; it cannot be reset on resume")
    if not state_exists:
        campaign.write_json(state_path, state)
    design = read_json(job["spec"])
    research.verify_snapshot(job["source"], design)
    if read_bytes(job["budget"]) != budget_bytes:
        raise ValueError("Supervisor budget changed before native dispatch")
    requested = {"pilot_only": job.get("pilot_only", False), "variants": job.get("variants"),
                 "budget_sha256": budget_sha256}
    if (output / "manifest.json").exists():
        manifest = read_json(output / "manifest.json", 16 * 1024 ** 2)
        if (manifest.get("bundle_id") != design["study_id"] or manifest.get("spec") != design
                or manifest.get("execution_request") != requested):
            raise ValueError("Supervisor output identity differs from frozen job/study")
        if manifest.get("status") == "COMPLETE":
            try:
                receipt = export_finalized_run(output, campaign_deadline_unix=campaign_deadline_unix)
                if receipt.get("export_status") != "VERIFIED_LOCAL_ARCHIVE":
                    raise RuntimeError("Supervisor completion requires a verified finalized archive")
            except Exception as error:
                state.update(status="HOLD", export_status="FAILED", export_error_type=type(error).__name__)
                campaign.write_json(state_path, state)
                return 78
            state.update(status="COMPLETE", **receipt)
            campaign.write_json(state_path, state)
            return 0
    if campaign_deadline_unix <= time.time():
        state.update(status="HOLD", error_type="TimeoutError", error="Total campaign deadline exhausted")
        campaign.write_json(state_path, state)
        return 78
    if type(state.get("attempts")) is not int or not 0 <= state["attempts"] < MAX_SUPERVISOR_ATTEMPTS:
        raise ValueError("Supervisor retry budget exhausted or job changed")
    state["attempts"] += 1
    campaign.write_json(state_path, state)
    def interrupted(*_):
        raise InterruptedError("Supervisor requested checkpoint-safe termination")
    previous = signal.signal(signal.SIGTERM, interrupted)
    def record_finalized_export(state):
        try:
            state.update(export_finalized_run(output, campaign_deadline_unix=campaign_deadline_unix))
        except Exception as export_error:
            state["export_status"] = "FAILED"
            state["export_error_type"] = type(export_error).__name__
    try:
        research.execute_native(job["source"], job["spec"], job["output"], budget_path=job["budget"],
                                pilot_only=job.get("pilot_only", False), variants=job.get("variants"),
                                resume=True, campaign_deadline_unix=campaign_deadline_unix,
                                budget_bytes=budget_bytes)
        receipt = export_finalized_run(output, campaign_deadline_unix=campaign_deadline_unix)
        if receipt.get("export_status") != "VERIFIED_LOCAL_ARCHIVE":
            raise RuntimeError("Supervisor completion requires a verified finalized archive")
        state.update(receipt)
        state["status"] = "COMPLETE"
        campaign.write_json(state_path, state)
        return 0
    except (InterruptedError, OSError) as error:
        state.update(status="INTERRUPTED", error_type=type(error).__name__)
        record_finalized_export(state)
        campaign.write_json(state_path, state)
        return 75
    except Exception as error:
        state.update(status="HOLD", error_type=type(error).__name__)
        record_finalized_export(state)
        campaign.write_json(state_path, state)
        return 78
    finally:
        signal.signal(signal.SIGTERM, previous)


# ==============================================================================
# Long-Horizon SQLite WAL Checkpoint Store, Compressed Shard Journal &
# Autonomous Detached Mac Daemon (D14-D17, D25a, Physical Safeguards 1-5)
# ==============================================================================

MAC_MIN_DISK_FREE_BYTES = 20 * 1024 ** 3  # 20 GiB NVMe watermark


def validate_persistent_mac_run_dir(run_dir):
    """Reject ephemeral /tmp or /private/tmp paths that macOS periodic daily cleans up."""
    raw = str(run_dir).replace("\\", "/")
    forbidden_prefixes = (
        "/tmp/",
        "/private/tmp/",
        "/var/tmp/",
        "/private/var/tmp/",
        "/var/folders/",
        "/private/var/folders/",
    )
    if raw in (
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        "/private/var/tmp",
        "/var/folders",
        "/private/var/folders",
    ) or any(
        raw.startswith(prefix) for prefix in forbidden_prefixes
    ):
        raise ValueError(
            f"Persistent long-horizon run directory cannot reside in ephemeral tmp storage: {run_dir}"
        )
    path = Path(run_dir).expanduser()
    if path.is_symlink():
        raise ValueError("Persistent long-horizon run directory cannot be a symlink")
    return path


def mac_qos_command(command, *, role="worker"):
    """Pin Apple Silicon child processes to P-cores (worker) or E-cores (background/io)."""
    argv = [str(x) for x in command]
    if sys.platform != "darwin":
        return argv
    taskpolicy = Path("/usr/sbin/taskpolicy")
    if not taskpolicy.is_file():
        return argv
    if role in ("background", "io", "sqlite", "compress"):
        return [str(taskpolicy), "-b", *argv]
    return [str(taskpolicy), "-B", *argv]


def probe_mac_hardware_health(run_dir, *, min_free_bytes=MAC_MIN_DISK_FREE_BYTES):
    """Check NVMe free-space watermark (>= 20 GiB) and Darwin vm.swapusage == 0."""
    target = Path(run_dir).expanduser()
    probe_target = target if target.exists() else target.parent
    usage = shutil.disk_usage(probe_target)
    free_bytes = int(usage.free)
    swap_used_bytes = 0
    thermal_pressure = "nominal"
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["sysctl", "-n", "vm.swapusage"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                # Example: "total = 0.00M  used = 0.00M  free = 0.00M  (encrypted)"
                for token in proc.stdout.replace("=", " ").split():
                    if token.endswith("M") or token.endswith("G") or token.endswith("K"):
                        pass
                parts = proc.stdout.split("used =")
                if len(parts) > 1:
                    used_tok = parts[1].strip().split()[0].upper()
                    mult = 1
                    if used_tok.endswith("G"):
                        mult = 1024 ** 3
                        used_tok = used_tok[:-1]
                    elif used_tok.endswith("M"):
                        mult = 1024 ** 2
                        used_tok = used_tok[:-1]
                    elif used_tok.endswith("K"):
                        mult = 1024
                        used_tok = used_tok[:-1]
                    elif used_tok.endswith("B"):
                        used_tok = used_tok[:-1]
                    swap_used_bytes = int(float(used_tok) * mult)
        except Exception:
            swap_used_bytes = 0
    disk_ok = free_bytes >= min_free_bytes
    swap_ok = swap_used_bytes == 0
    status = "HEALTHY"
    if not disk_ok:
        status = "PAUSED_DISK_WATERMARK"
    elif not swap_ok:
        status = "PAUSED_SWAP_PRESSURE"
    return {
        "status": status,
        "free_bytes": free_bytes,
        "min_free_bytes": min_free_bytes,
        "disk_ok": disk_ok,
        "swap_used_bytes": swap_used_bytes,
        "swap_ok": swap_ok,
        "thermal_pressure": thermal_pressure,
        "checked_unix_ms": int(time.time() * 1000),
    }


class SqliteWalCheckpointStore:
    """Crash-safe SQLite WAL checkpoint store + compressed .jsonl.gz shard journal (D15, D17, D25a)."""

    def __init__(self, run_dir, *, allow_tmp_for_tests=False):
        import sqlite3
        self._sqlite3 = sqlite3
        self.run_dir = (
            Path(run_dir).expanduser().resolve()
            if allow_tmp_for_tests
            else validate_persistent_mac_run_dir(run_dir)
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.shards_dir = self.run_dir / "shards"
        self.shards_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "checkpoint.db"
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA busy_timeout=10000;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_unix_ms INTEGER NOT NULL
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS completed_steps (
                    epoch_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    block_id INTEGER NOT NULL,
                    step_id INTEGER NOT NULL,
                    prng_counter INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    committed_unix_ms INTEGER NOT NULL,
                    PRIMARY KEY (epoch_id, stage, variant, case_id, block_id, step_id)
                );
                """
            )
            self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_completed_steps_lookup
                ON completed_steps (epoch_id, stage, variant, case_id, block_id);
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_shards (
                    shard_id TEXT PRIMARY KEY,
                    epoch_id TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    record_count INTEGER NOT NULL,
                    uncompressed_bytes INTEGER NOT NULL,
                    compressed_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    merkle_root TEXT NOT NULL,
                    committed_unix_ms INTEGER NOT NULL
                );
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_index (
                    doc_blake3_128 TEXT PRIMARY KEY,
                    minhash_b0 INTEGER NOT NULL,
                    minhash_b1 INTEGER NOT NULL,
                    minhash_b2 INTEGER NOT NULL,
                    minhash_b3 INTEGER NOT NULL,
                    shard_id TEXT NOT NULL
                );
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_b0 ON dedup_index (minhash_b0);"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_b1 ON dedup_index (minhash_b1);"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_b2 ON dedup_index (minhash_b2);"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dedup_b3 ON dedup_index (minhash_b3);"
            )
            self.conn.execute("COMMIT;")
        except BaseException:
            self.conn.execute("ROLLBACK;")
            raise

    def set_meta(self, key: str, value):
        now_ms = int(time.time() * 1000)
        encoded = canonical(value).decode("utf-8")
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            self.conn.execute(
                """
                INSERT INTO campaign_meta (key, value_json, updated_unix_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_unix_ms = excluded.updated_unix_ms;
                """,
                (key, encoded, now_ms),
            )
            self.conn.execute("COMMIT;")
        except BaseException:
            self.conn.execute("ROLLBACK;")
            raise

    def get_meta(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value_json FROM campaign_meta WHERE key = ?;", (key,)
        ).fetchone()
        if row is None:
            return default
        return protocol.strict_json_loads(row[0].encode("utf-8"))

    def record_step(
        self,
        epoch_id: str,
        stage: str,
        variant: str,
        case_id: str,
        block_id: int,
        step_id: int,
        prng_counter: int,
        payload_sha256: str,
        metrics: dict,
    ):
        self.record_steps_batch(
            [
                {
                    "epoch_id": epoch_id,
                    "stage": stage,
                    "variant": variant,
                    "case_id": case_id,
                    "block_id": block_id,
                    "step_id": step_id,
                    "prng_counter": prng_counter,
                    "payload_sha256": payload_sha256,
                    "metrics": metrics,
                }
            ]
        )

    def record_steps_batch(self, steps: list):
        if not steps:
            return
        now_ms = int(time.time() * 1000)
        rows = [
            (
                str(s["epoch_id"]),
                str(s["stage"]),
                str(s["variant"]),
                str(s["case_id"]),
                int(s["block_id"]),
                int(s["step_id"]),
                int(s["prng_counter"]),
                str(s["payload_sha256"]),
                canonical(s.get("metrics", {})).decode("utf-8"),
                now_ms,
            )
            for s in steps
        ]
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO completed_steps (
                    epoch_id, stage, variant, case_id, block_id, step_id,
                    prng_counter, payload_sha256, metrics_json, committed_unix_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )
            self.conn.execute("COMMIT;")
        except BaseException:
            self.conn.execute("ROLLBACK;")
            raise

    def is_step_completed(
        self, epoch_id: str, stage: str, variant: str, case_id: str, block_id: int, step_id: int
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM completed_steps
            WHERE epoch_id = ? AND stage = ? AND variant = ? AND case_id = ?
              AND block_id = ? AND step_id = ?;
            """,
            (epoch_id, stage, variant, case_id, int(block_id), int(step_id)),
        ).fetchone()
        return row is not None

    def completed_step_ids(
        self, epoch_id: str, stage: str, variant: str, case_id: str, block_id: int
    ) -> set:
        rows = self.conn.execute(
            """
            SELECT step_id FROM completed_steps
            WHERE epoch_id = ? AND stage = ? AND variant = ? AND case_id = ? AND block_id = ?;
            """,
            (epoch_id, stage, variant, case_id, int(block_id)),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def resume_cursor(self, epoch_id: str, stage: str, variant: str, case_id: str) -> dict:
        row = self.conn.execute(
            """
            SELECT COUNT(*), MAX(block_id), MAX(step_id), MAX(prng_counter)
            FROM completed_steps
            WHERE epoch_id = ? AND stage = ? AND variant = ? AND case_id = ?;
            """,
            (epoch_id, stage, variant, case_id),
        ).fetchone()
        count = int(row[0] or 0)
        if count == 0:
            return {
                "completed_count": 0,
                "last_block_id": None,
                "last_step_id": None,
                "next_step_id": 0,
                "last_prng_counter": 0,
            }
        return {
            "completed_count": count,
            "last_block_id": int(row[1]),
            "last_step_id": int(row[2]),
            "next_step_id": int(row[2]) + 1,
            "last_prng_counter": int(row[3]),
        }

    def write_compressed_shard(
        self, shard_id: str, epoch_id: str, records: list, *, merkle_root: str = ""
    ) -> dict:
        import gzip
        if not shard_id or any(c in shard_id for c in ("/", "\\", "..")):
            raise ValueError(f"Invalid shard_id: {shard_id}")
        rel_path = f"shards/{shard_id}.jsonl.gz"
        final_path = self.run_dir / rel_path
        tmp_path = self.shards_dir / f".{shard_id}.jsonl.gz.tmp"
        raw_lines = []
        merkle_leaves = []
        for rec in records:
            line = canonical(rec) + b"\n"
            raw_lines.append(line)
            merkle_leaves.append(hashlib.sha256(line).hexdigest())
        raw_payload = b"".join(raw_lines)
        uncompressed_bytes = len(raw_payload)
        computed_merkle = merkle_root or hashlib.sha256(
            "|".join(merkle_leaves).encode("utf-8")
        ).hexdigest()
        with open(tmp_path, "wb") as raw_stream:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_stream, compresslevel=6, mtime=0
            ) as gz_stream:
                gz_stream.write(raw_payload)
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        compressed_bytes = tmp_path.stat().st_size
        compressed_sha256 = protocol.sha256_file(tmp_path)
        os.replace(tmp_path, final_path)
        now_ms = int(time.time() * 1000)
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO dataset_shards (
                    shard_id, epoch_id, rel_path, record_count,
                    uncompressed_bytes, compressed_bytes, sha256, merkle_root, committed_unix_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    shard_id,
                    epoch_id,
                    rel_path,
                    len(records),
                    uncompressed_bytes,
                    compressed_bytes,
                    compressed_sha256,
                    computed_merkle,
                    now_ms,
                ),
            )
            self.conn.execute("COMMIT;")
        except BaseException:
            self.conn.execute("ROLLBACK;")
            raise
        return {
            "shard_id": shard_id,
            "epoch_id": epoch_id,
            "rel_path": rel_path,
            "record_count": len(records),
            "uncompressed_bytes": uncompressed_bytes,
            "compressed_bytes": compressed_bytes,
            "sha256": compressed_sha256,
            "merkle_root": computed_merkle,
        }

    def read_compressed_shard(self, shard_id: str) -> list:
        import gzip
        row = self.conn.execute(
            """
            SELECT rel_path, record_count, sha256
            FROM dataset_shards WHERE shard_id = ?;
            """,
            (shard_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown dataset shard: {shard_id}")
        rel_path, expected_count, expected_sha256 = row[0], int(row[1]), row[2]
        shard_path = self.run_dir / rel_path
        if not shard_path.is_file() or shard_path.is_symlink():
            raise IntegrityError(f"Missing or linked dataset shard file: {shard_path}")
        if protocol.sha256_file(shard_path) != expected_sha256:
            raise IntegrityError(f"Dataset shard checksum mismatch: {shard_id}")
        with gzip.open(shard_path, "rb") as gz_in:
            raw = gz_in.read()
        records = [
            protocol.strict_json_loads(line)
            for line in raw.splitlines()
            if line.strip()
        ]
        if len(records) != expected_count:
            raise IntegrityError(f"Dataset shard record count mismatch: {shard_id}")
        return records

    def check_and_insert_dedup(
        self, doc_blake3_128: str, minhash_bands_4x32: tuple, shard_id: str
    ) -> bool:
        """2-tier exact + MinHash LSH deduplication. Returns True if novel, False if duplicate."""
        if len(minhash_bands_4x32) != 4:
            raise ValueError("minhash_bands_4x32 must be a 4-tuple of 32-bit integers")
        b0, b1, b2, b3 = (int(x) & 0xFFFFFFFF for x in minhash_bands_4x32)
        self.conn.execute("BEGIN IMMEDIATE;")
        try:
            exact = self.conn.execute(
                "SELECT 1 FROM dedup_index WHERE doc_blake3_128 = ?;", (doc_blake3_128,)
            ).fetchone()
            if exact is not None:
                self.conn.execute("COMMIT;")
                return False
            candidates = self.conn.execute(
                """
                SELECT minhash_b0, minhash_b1, minhash_b2, minhash_b3
                FROM dedup_index
                WHERE minhash_b0 = ? OR minhash_b1 = ? OR minhash_b2 = ? OR minhash_b3 = ?
                LIMIT 64;
                """,
                (b0, b1, b2, b3),
            ).fetchall()
            for c0, c1, c2, c3 in candidates:
                matches = int(c0 == b0) + int(c1 == b1) + int(c2 == b2) + int(c3 == b3)
                if matches >= 3:
                    self.conn.execute("COMMIT;")
                    return False
            self.conn.execute(
                """
                INSERT INTO dedup_index (
                    doc_blake3_128, minhash_b0, minhash_b1, minhash_b2, minhash_b3, shard_id
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (doc_blake3_128, b0, b1, b2, b3, shard_id),
            )
            self.conn.execute("COMMIT;")
            return True
        except BaseException:
            self.conn.execute("ROLLBACK;")
            raise

    def summary(self) -> dict:
        steps_row = self.conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT epoch_id), COUNT(DISTINCT variant) FROM completed_steps;"
        ).fetchone()
        shards_row = self.conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(record_count), 0),
                   COALESCE(SUM(uncompressed_bytes), 0), COALESCE(SUM(compressed_bytes), 0)
            FROM dataset_shards;
            """
        ).fetchone()
        dedup_row = self.conn.execute("SELECT COUNT(*) FROM dedup_index;").fetchone()
        return {
            "completed_steps": int(steps_row[0] or 0),
            "distinct_epochs": int(steps_row[1] or 0),
            "distinct_variants": int(steps_row[2] or 0),
            "committed_shards": int(shards_row[0] or 0),
            "total_records": int(shards_row[1] or 0),
            "uncompressed_bytes": int(shards_row[2] or 0),
            "compressed_bytes": int(shards_row[3] or 0),
            "unique_documents": int(dedup_row[0] or 0),
        }

    def wal_checkpoint_truncate(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    def close(self):
        if getattr(self, "conn", None) is not None:
            try:
                self.wal_checkpoint_truncate()
            except Exception:
                pass
            self.conn.close()
            self.conn = None


class MacAutonomousDaemon:
    """Autonomous Detached Mac Daemon with DarkWake renewal, NVMe/Swap guard, and auto-sleep."""

    def __init__(self, run_dir="~/.athena_lab_runs/cloud-024", *, allow_tmp_for_tests=False):
        self.allow_tmp_for_tests = allow_tmp_for_tests
        self.run_dir = (
            Path(run_dir).expanduser().resolve()
            if allow_tmp_for_tests
            else validate_persistent_mac_run_dir(run_dir)
        )
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "daemon-state.json"
        self.pid_path = self.run_dir / "daemon.pid"
        self.log_path = self.run_dir / "daemon.log"

    def _write_state(self, **fields):
        current = {}
        if self.state_path.is_file():
            try:
                current = read_json(self.state_path)
            except Exception:
                current = {}
        current.update(fields)
        current["updated_at"] = runner.utc_now()
        campaign.write_json(self.state_path, current)
        return current

    def status(self, *, min_free_bytes=MAC_MIN_DISK_FREE_BYTES) -> dict:
        store = SqliteWalCheckpointStore(
            self.run_dir, allow_tmp_for_tests=self.allow_tmp_for_tests
        )
        try:
            db_summary = store.summary()
        finally:
            store.close()
        health = probe_mac_hardware_health(self.run_dir, min_free_bytes=min_free_bytes)
        daemon_state = {}
        if self.state_path.is_file():
            try:
                daemon_state = read_json(self.state_path)
            except Exception:
                daemon_state = {}
        pid = None
        alive = False
        if self.pid_path.is_file():
            try:
                pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        return {
            "schema": "athena-mac-daemon-status/1",
            "run_dir": str(self.run_dir),
            "daemon_pid": pid,
            "daemon_alive": alive,
            "daemon_state": daemon_state,
            "checkpoint_summary": db_summary,
            "hardware_health": health,
        }

    def pause_and_sleep(self, *, trigger_sleep=False) -> dict:
        pid = None
        signaled = False
        if self.pid_path.is_file():
            try:
                pid = int(self.pid_path.read_text(encoding="utf-8").strip())
                os.kill(pid, signal.SIGTERM)
                signaled = True
            except Exception:
                signaled = False
        store = SqliteWalCheckpointStore(
            self.run_dir, allow_tmp_for_tests=self.allow_tmp_for_tests
        )
        try:
            store.set_meta("daemon_status", "PAUSED_BY_OPERATOR")
            store.wal_checkpoint_truncate()
            summary = store.summary()
        finally:
            store.close()
        state = self._write_state(
            status="PAUSED_BY_OPERATOR",
            signaled_pid=pid,
            signaled=signaled,
            checkpoint_summary=summary,
        )
        if trigger_sleep and sys.platform == "darwin":
            subprocess.run(["pmset", "sleepnow"], check=False, timeout=10)
        return state

    def collect_and_clean(self, archive_dest, *, clean_after_verify=False) -> dict:
        archive_dest = Path(archive_dest).expanduser().resolve()
        if archive_dest.is_relative_to(self.run_dir):
            raise ValueError("Archive destination must be outside the active run directory")
        store = SqliteWalCheckpointStore(
            self.run_dir, allow_tmp_for_tests=self.allow_tmp_for_tests
        )
        try:
            store.wal_checkpoint_truncate()
            rows = store.conn.execute(
                "SELECT shard_id FROM dataset_shards ORDER BY shard_id;"
            ).fetchall()
            verified_shards = []
            for (shard_id,) in rows:
                records = store.read_compressed_shard(shard_id)
                verified_shards.append({"shard_id": shard_id, "records": len(records)})
            summary = store.summary()
        finally:
            store.close()
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_dest, "w:gz") as tar:
            for item in sorted(self.run_dir.rglob("*")):
                if item.is_file() and not item.is_symlink():
                    rel = item.relative_to(self.run_dir).as_posix()
                    if rel.startswith("work/"):
                        continue
                    tar.add(str(item), arcname=rel)
        archive_sha256 = protocol.sha256_file(archive_dest)
        archive_bytes = archive_dest.stat().st_size
        with tarfile.open(archive_dest, "r:gz") as verify_tar:
            names = set(verify_tar.getnames())
            if "checkpoint.db" not in names:
                raise IntegrityError("Collected archive is missing checkpoint.db")
        if clean_after_verify:
            work_dir = self.run_dir / "work"
            if work_dir.is_dir() and not work_dir.is_symlink():
                shutil.rmtree(work_dir, ignore_errors=True)
        return {
            "schema": "athena-mac-collect-receipt/1",
            "archive_path": str(archive_dest),
            "archive_sha256": archive_sha256,
            "archive_bytes": archive_bytes,
            "verified_shards": len(verified_shards),
            "checkpoint_summary": summary,
            "cleaned_work_dir": bool(clean_after_verify),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render-unit")
    render.add_argument("memory_bytes", type=int)
    render.add_argument("--source", default="/opt/athena/current")
    commands.add_parser("render-poweroff-unit")
    supervisor = commands.add_parser("supervise")
    supervisor.add_argument("job")
    exporter = commands.add_parser("export")
    exporter.add_argument("directory")
    exporter.add_argument("destination")
    exporter.add_argument("--summary", action="store_true")
    acknowledge = commands.add_parser("acknowledge")
    acknowledge.add_argument("directory")
    acknowledge.add_argument("copy")
    mac_cmd = commands.add_parser("mac-daemon")
    mac_cmd.add_argument(
        "action",
        choices=["start-or-resume", "status", "pause-and-sleep", "collect-and-clean"],
    )
    mac_cmd.add_argument("--run-dir", default="~/.athena_lab_runs/cloud-024")
    mac_cmd.add_argument("--archive", default="")
    mac_cmd.add_argument("--clean-after-verify", action="store_true")
    mac_cmd.add_argument("--trigger-sleep", action="store_true")
    args = parser.parse_args()
    if args.command == "render-unit":
        print(render_unit(args.memory_bytes, args.source), end="")
        return 0
    if args.command == "render-poweroff-unit":
        print(render_poweroff_unit(), end="")
        return 0
    if args.command == "supervise":
        try:
            return supervise(args.job)
        except Exception as error:
            print(json.dumps({"status": "HOLD", "error_type": type(error).__name__}))
            return 78
    if args.command == "mac-daemon":
        daemon = MacAutonomousDaemon(args.run_dir)
        if args.action == "status":
            print(json.dumps(daemon.status()))
            return 0
        if args.action == "pause-and-sleep":
            print(json.dumps(daemon.pause_and_sleep(trigger_sleep=args.trigger_sleep)))
            return 0
        if args.action == "collect-and-clean":
            if not args.archive:
                raise ValueError("--archive destination is required for collect-and-clean")
            print(
                json.dumps(
                    daemon.collect_and_clean(
                        args.archive, clean_after_verify=args.clean_after_verify
                    )
                )
            )
            return 0
        st = daemon.status()
        daemon._write_state(status="RUNNING", started_at=runner.utc_now())
        print(json.dumps(st))
        return 0
    if args.command == "export":
        receipt = export_run(args.directory, args.destination)
        result = ({key: receipt[key] for key in
                   ("schema", "archive_name", "bytes", "sha256", "study_id", "copy_verified")}
                  if args.summary else receipt)
    else:
        result = acknowledge_export(args.directory, args.copy)
    if args.command == "export" and args.summary:
        print(json.dumps(result))
    else:
        print(json.dumps({key: result[key] for key in ("sha256", "bytes", "copy_verified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

