"""Durable balanced-block publication and bounded research process ownership."""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from cloud_experiments import campaign
from kaggle_experiments import protocol, runner


class IntegrityError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


_RESEARCH_CHILD_ENV = frozenset({
    "CARGO_ENCODED_RUSTFLAGS",
    "CARGO_HOME",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
    "CARGO_PROFILE_RELEASE_LTO",
    "CARGO_TARGET_DIR",
    "ATHENA_RESEARCH_HASH",
    "ATHENA_RESEARCH_PREPARED",
    "ATHENA_RESEARCH_RESERVE",
    "ATHENA_RESEARCH_TABLE_EXTRACTOR",
    "ATHENA_RESEARCH_TABLE_HEADERS",
    "ATHENA_RESEARCH_WRITER",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONUTF8",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "WINDIR",
})


def _research_child_environment():
    return {key: value for key, value in os.environ.items()
            if key.upper() in _RESEARCH_CHILD_ENV}


def read_json(path, limit=8 * 1024 ** 2):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or linked lifecycle input")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Lifecycle input exceeds byte bound")
    return json.loads(data)


class CollectionJournal:
    def __init__(self, root, context, total_limit, session_id, *, metadata_limit=64 * 1024 ** 2, block_limit=256 * 1024 ** 2):
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
            self.bindings = read_json(self.binding_path)
            self.metadata_bytes += self.binding_path.stat().st_size
            if self.bindings.get("context") != context or not isinstance(self.bindings.get("channels"), dict):
                raise ValueError("Collection source or execution environment changed")
        for path in sorted(self.folder.iterdir()):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Unexpected collection entry")
            if path.name.startswith(".pending-"):
                if not protocol.is_digest(path.name[9:]):
                    raise ValueError("Unknown pending collection owner")
                self._discard_pending(path)
                continue
            if not protocol.is_digest(path.name):
                raise ValueError("Unknown committed collection owner")
            receipt = read_json(path / "receipt.json")
            self.metadata_bytes += (path / "receipt.json").stat().st_size
            data = path / "samples.jsonl"
            if (receipt.get("version") != "athena-collection-block/1"
                    or receipt.get("context") != context
                    or digest(receipt.get("descriptor")) != path.name
                    or receipt.get("session_id") is None
                    or data.is_symlink() or not data.is_file()
                    or set(p.name for p in path.iterdir()) != {"samples.jsonl", "receipt.json"}
                    or data.stat().st_size != receipt.get("bytes")
                    or protocol.sha256_file(data) != receipt.get("sha256")):
                raise ValueError("Committed collection identity or bytes changed")
            self.total_bytes += data.stat().st_size
            self.commits[path.name] = receipt
        if self.total_bytes > total_limit or self.metadata_bytes > metadata_limit:
            raise ValueError("Committed collection exceeds capacity")

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
                row = json.loads(line)
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
            pending.extend(int(value) for value in Path(f"/proc/{current}/task/{current}/children").read_text().split())
        except (OSError, StopIteration, ValueError):
            continue
    return total


def memory_peak_bytes():
    if os.name != "posix":
        return None
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    try:
        line = next(line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
        path = Path("/sys/fs/cgroup") / line[3:].lstrip("/") / "memory.peak"
        peak = max(peak, int(path.read_text()))
    except (OSError, StopIteration, ValueError):
        pass
    return peak


def memory_limit_bytes():
    line = next(line for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    root = Path("/sys/fs/cgroup").resolve(strict=True)
    folder = (root / line[3:].lstrip("/")).resolve(strict=True)
    if not folder.is_relative_to(root):
        raise ValueError("Cgroup hierarchy escaped its mount")
    limits = []
    while folder.is_relative_to(root):
        value = (folder / "memory.max").read_text().strip()
        if value != "max":
            limits.append(int(value))
        if folder == root:
            break
        folder = folder.parent
    return min(limits) if limits else None


def tree_bytes(folder):
    folder = Path(folder)
    if not folder.exists():
        return 0
    total, entries, pending = 0, 0, [folder]
    while pending:
        path = pending.pop()
        if path.is_symlink():
            raise ValueError("Resource tree contains a linked entry")
        if path.is_dir():
            with os.scandir(path) as stream:
                for entry in stream:
                    pending.append(Path(entry.path))
                    entries += 1
                    if entries > 200000:
                        raise ValueError("Resource tree entry bound exceeded")
        elif path.is_file():
            total += path.stat().st_size
    return total


class ManagedRun(runner.EvidenceRun):
    def __init__(self, directory, cluster, bundle_id, spec, *, resume=False):
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("Run directory cannot be linked")
        if resume and directory.exists():
            self.directory = directory.resolve(strict=True)
            self.manifest = read_json(self.directory / "manifest.json", 16 * 1024 ** 2)
            if (self.manifest.get("bundle_id") != bundle_id or self.manifest.get("spec") != spec
                    or self.manifest.get("lifecycle") != "athena-managed-run/1"):
                raise ValueError("Resume source or study identity changed")
            self.log_path, self.samples_path = self.directory / "run.log", self.directory / "samples.jsonl"
            if self.log_path.is_symlink() or self.samples_path.is_symlink():
                raise ValueError("Linked run output")
            self.sample_stream = self.samples_path.open("a", encoding="utf-8")
            self.deadline = time.monotonic() + spec.get("limits", {}).get("wall_seconds", 5400)
        else:
            super().__init__(directory, cluster, bundle_id, spec)
            self.manifest["lifecycle"] = "athena-managed-run/1"
        self.session_id = uuid.uuid4().hex
        self.collection = None
        self.manifest.setdefault("sessions", []).append({"id": self.session_id, "started_at": runner.utc_now()})
        self.manifest["status"] = "RUNNING"
        self.save()

    def save(self):
        campaign.write_json(self.directory / "manifest.json", self.manifest, max_bytes=16 * 1024 ** 2)

    def bind_deadline(self, seconds):
        if type(seconds) is not int or seconds < 1:
            raise ValueError("A finite campaign deadline is required")
        previous = self.manifest.get("deadline_duration_seconds")
        if previous is not None and previous != seconds:
            raise ValueError("Resume cannot reset its spending/deadline window")
        self.manifest.setdefault("deadline_duration_seconds", seconds)
        self.manifest.setdefault("deadline_unix", time.time() + seconds)
        self.deadline = time.monotonic() + self.manifest["deadline_unix"] - time.time()
        if self.deadline <= time.monotonic():
            raise TimeoutError("Persisted campaign deadline exhausted")
        self.save()

    def check(self, name, action):
        previous = self.manifest["checks"].get(name, {})
        if previous.get("status") == "PASS":
            return previous.get("evidence")
        self.manifest["checks"][name] = {"status": "RUNNING"}
        self.save()
        return super().check(name, action)

    def log(self, message):
        data = (f"[{runner.utc_now()}] {message}\n").encode("utf-8")
        bound = self.manifest.get("spec", {}).get("limits", {}).get("log_bytes", 64 * 1024 ** 2)
        if self.log_path.is_symlink() or (self.log_path.exists() and self.log_path.stat().st_size + len(data) > bound):
            raise ValueError("Run log capacity exceeded")
        with self.log_path.open("ab") as stream:
            stream.write(data)

    def guard_storage(self):
        limits = self.manifest.get("spec", {}).get("limits", {})
        reserve = limits.get("disk_reserve_bytes", 0)
        if shutil.disk_usage(self.directory).free < reserve:
            raise RuntimeError("Research disk reserve exhausted")
        if "build_bytes" in limits:
            cache = self.directory.parent / "cargo-target"
            binaries = tree_bytes(self.directory / "bins")
            build = tree_bytes(cache) + tree_bytes(self.directory / "work") + binaries
            if build > limits["build_bytes"] or binaries > limits["binary_bytes"]:
                raise RuntimeError("Research build/binary capacity exhausted")
            analysis = sum(path.stat().st_size for path in self.directory.glob("confirmation-*.json"))
            if analysis > limits["analysis_bytes"]:
                raise RuntimeError("Research analysis capacity exhausted")
            self.manifest["storage_probe"] = {"build_bytes": build, "binary_bytes": binaries,
                                               "analysis_bytes": analysis, "free_bytes": shutil.disk_usage(self.directory).free,
                                               "scope": "Boundary/periodic admission checks, not a filesystem quota"}

    def run(self, command, cwd=None, timeout=300, input_bytes=None, log_output=True):
        command = [str(value) for value in command]
        limits = self.manifest.get("spec", {}).get("limits", {})
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Experiment wall-clock budget exhausted")
        timeout = min(timeout, remaining)
        output_limit = limits.get("command_output_bytes", 8 * 1024 ** 2)
        now = time.monotonic()
        cargo = Path(command[0]).name in ("cargo", "cargo.exe")
        if cargo or now - getattr(self, "last_storage_check", 0) >= 30:
            self.guard_storage()
            self.last_storage_check = now
        buffers, held, failure = [bytearray(), bytearray()], [0], []
        guard = threading.Lock()
        windows_job = None
        process = None
        if os.name == "nt":
            # A supervised command is an untrusted boundary.  Reuse the
            # producer's verified Job Object so timeout/output/disk failure
            # cannot leave descendants running after the leader is gone.
            from cloud_experiments.agent_lab import _WindowsJobObject
            windows_job = _WindowsJobObject()
        try:
            process = subprocess.Popen(
                command, cwd=cwd,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=(os.name == "posix"),
                env=_research_child_environment())
            if windows_job is not None:
                try:
                    windows_job.assign(process._handle)
                except OSError:
                    process.kill()
                    process.wait(timeout=10)
                    raise
        except BaseException:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if windows_job is not None:
                windows_job.close()
            raise

        def terminate():
            try:
                if windows_job is not None:
                    windows_job.terminate()
                elif os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, PermissionError):
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
        for thread in threads:
            thread.start()
        started, peak, next_disk_check = time.monotonic(), None, time.monotonic() + 1
        try:
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
                time.sleep(.05)
            process.wait(timeout=10)
        except BaseException:
            terminate()
            process.wait(timeout=10)
            raise
        finally:
            if windows_job is not None or os.name == "posix":
                terminate()
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads):
                terminate()
                failure.append("PIPE_LIFETIME")
            if windows_job is not None:
                windows_job.close()
        stdout, stderr = map(bytes, buffers)
        record = {"argv": command, "cwd": str(cwd), "exit_code": process.returncode,
                  "wall_ns": round((time.monotonic() - started) * 1e9),
                  "stdout_sha256": hashlib.sha256(stdout).hexdigest(), "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                  "sampled_process_tree_rss_bytes": peak, "status": failure[0] if failure else "EXITED"}
        self.manifest["commands"].append(record)
        self.manifest["commands"] = self.manifest["commands"][-64:]
        self.manifest["command_count"] = self.manifest.get("command_count", 0) + 1
        if peak is not None:
            self.manifest["sampled_process_tree_rss_bytes"] = max(self.manifest.get("sampled_process_tree_rss_bytes", 0), peak)
        events = self.directory / "commands.jsonl"
        event = canonical(record) + b"\n"
        if events.is_symlink() or (events.exists() and events.stat().st_size + len(event) > limits.get("metadata_bytes", 64 * 1024 ** 2)):
            raise ValueError("Command metadata capacity exceeded")
        with events.open("ab") as stream:
            stream.write(event)
        self.log("RESULT " + json.dumps(record))
        if log_output or process.returncode:
            self.log((stdout + stderr).decode("utf-8", errors="replace"))
        self.save()
        if failure:
            if failure[0] == "TIMEOUT":
                raise TimeoutError("Supervised process timed out")
            raise RuntimeError("Supervised process exceeded " + failure[0])
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        if cargo:
            self.guard_storage()
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


def export_finalized_run(output):
    output = Path(output).resolve(strict=True)
    manifest_path = output / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return {"export_status": "NO_FINALIZED_MANIFEST"}
    manifest = read_json(manifest_path, 16 * 1024 ** 2)
    if manifest.get("status") not in ("COMPLETE", "FAILED"):
        return {"export_status": "RUN_NOT_FINALIZED"}
    receipt = export_run(output, output.with_name(output.name + "-evidence.tgz"))
    return {"export_status": "VERIFIED_LOCAL_ARCHIVE", "export_sha256": receipt["sha256"],
            "export_bytes": receipt["bytes"]}


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
    state = read_json(state_path) if state_path.exists() else {"job_digest": digest(job), "attempts": 0}
    if state.get("job_digest") != digest(job):
        raise ValueError("Supervisor job identity changed")
    design = read_json(job["spec"])
    research.verify_snapshot(job["source"], design)
    requested = {"pilot_only": job.get("pilot_only", False), "variants": job.get("variants"),
                 "budget_sha256": protocol.sha256_file(job["budget"])}
    if (output / "manifest.json").exists():
        manifest = read_json(output / "manifest.json", 16 * 1024 ** 2)
        if (manifest.get("bundle_id") != design["study_id"] or manifest.get("spec") != design
                or manifest.get("execution_request") != requested):
            raise ValueError("Supervisor output identity differs from frozen job/study")
        if manifest.get("status") == "COMPLETE":
            export_run(output, output.with_name(output.name + "-evidence.tgz"))
            return 0
    if type(state.get("attempts")) is not int or not 0 <= state["attempts"] < 3:
        raise ValueError("Supervisor retry budget exhausted or job changed")
    state["attempts"] += 1
    campaign.write_json(state_path, state)
    def interrupted(*_):
        raise InterruptedError("Supervisor requested checkpoint-safe termination")
    previous = signal.signal(signal.SIGTERM, interrupted)
    def record_finalized_export(state):
        try:
            state.update(export_finalized_run(output))
        except Exception as export_error:
            state["export_status"] = "FAILED"
            state["export_error_type"] = type(export_error).__name__
    try:
        research.execute_native(job["source"], job["spec"], job["output"], budget_path=job["budget"],
                                pilot_only=job.get("pilot_only", False), variants=job.get("variants"), resume=True)
        export_run(output, output.with_name(output.name + "-evidence.tgz"))
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
    acknowledge = commands.add_parser("acknowledge")
    acknowledge.add_argument("directory")
    acknowledge.add_argument("copy")
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
    result = export_run(args.directory, args.destination) if args.command == "export" else acknowledge_export(args.directory, args.copy)
    print(json.dumps({key: result[key] for key in ("sha256", "bytes", "copy_verified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
