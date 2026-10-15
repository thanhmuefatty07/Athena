"""Bounded experiment execution. Imported locally without starting a run."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

if __package__:
    from .protocol import (
        PROTOCOL_VERSION, is_digest, parse_peak_rss, require_parity,
        sha256_file, validate_aae,
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def unpack_snapshot(encoded, expected_digest, hashes, destination):
    destination = Path(destination)
    raw = base64.b64decode(encoded, validate=True)
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise ValueError("Snapshot archive checksum mismatch")
    if destination.exists():
        raise ValueError("Snapshot directory must be new")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or "\\" in member.name or ":" in member.name or member.name not in hashes
                    or member.name in files or member.size > 8 * 1024 * 1024):
                raise ValueError("Unsafe or unexpected archive member")
            total += member.size
            if total > 64 * 1024 * 1024:
                raise ValueError("Source archive exceeds extraction budget")
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != hashes[member.name]:
                raise ValueError("Source file checksum mismatch: " + member.name)
            files[member.name] = content
    if set(files) != set(hashes):
        raise ValueError("Archive file inventory mismatch")
    destination.mkdir(parents=True)
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(content)
    return {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}


class EvidenceRun:
    def __init__(self, directory, cluster, bundle_id, spec):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.deadline = time.monotonic() + 5400
        self.manifest = {"protocol": PROTOCOL_VERSION, "cluster": cluster, "bundle_id": bundle_id,
                         "status": "RUNNING", "started_at": utc_now(), "spec": spec,
                         "checks": {}, "commands": [], "snapshots": {}, "artifacts": {}}
        self.log_path = self.directory / "run.log"
        self.samples_path = self.directory / "samples.jsonl"
        self.sample_stream = self.samples_path.open("x", encoding="utf-8")
        self.save()

    def log(self, message):
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{utc_now()}] {message}\n")

    def save(self):
        (self.directory / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")

    def run(self, command, cwd=None, timeout=300, input_bytes=None, log_output=True):
        command = [str(value) for value in command]
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Experiment wall-clock budget exhausted")
        self.log("CMD " + json.dumps(command) + " cwd=" + str(cwd))
        start = time.perf_counter_ns()
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=(os.name == "posix"))
        try:
            stdout, stderr = process.communicate(input_bytes, timeout=min(timeout, remaining))
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
            self.manifest["commands"].append({"argv": command, "cwd": str(cwd),
                "status": "TIMEOUT", "exit_code": process.returncode})
            self.log("TIMEOUT\n" + stderr.decode("utf-8", errors="replace"))
            raise TimeoutError("Supervised process timed out")
        record = {"argv": command, "cwd": str(cwd), "exit_code": process.returncode,
                  "wall_ns": time.perf_counter_ns() - start,
                  "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                  "stderr_sha256": hashlib.sha256(stderr).hexdigest()}
        self.manifest["commands"].append(record)
        self.log("RESULT " + json.dumps(record))
        if log_output or process.returncode:
            self.log(stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace"))
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return stdout

    def check(self, name, action):
        try:
            result = action()
        except Exception as error:
            self.manifest["checks"][name] = {"status": "FAIL", "error": str(error)}
            self.save()
            raise
        self.manifest["checks"][name] = {"status": "PASS", "evidence": result}
        self.save()
        return result

    def sample(self, case, arm, round_id, iteration, value, **details):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid measurement")
        record = {"protocol": PROTOCOL_VERSION, "bundle_id": self.manifest["bundle_id"],
                  "case_id": case["id"], "stage": case["stage"], "size": case["size"],
                  "unit": case["unit"], "round": round_id, "iteration": iteration,
                  "order_in_round": (0 if arm == "baseline" else 1) ^ (round_id % 2),
                  "candidate": arm, "value": value, "status": "PASS", **details}
        self.sample_stream.write(json.dumps(record, allow_nan=False) + "\n")
        self.sample_stream.flush()

    def finish(self, error=None):
        self.sample_stream.close()
        self.manifest["status"] = "FAILED" if error else "COMPLETE"
        self.manifest["finished_at"] = utc_now()
        if error:
            self.manifest["error"] = repr(error)
            self.log("FAILED " + repr(error))
        self.manifest["artifacts"] = {
            path.name: sha256_file(path) for path in self.directory.iterdir()
            if path.is_file() and path.name != "manifest.json"}
        self.save()


def prepare_toolchain(run):
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    os.environ["PATH"] = cargo_bin + os.pathsep + os.environ.get("PATH", "")
    os.environ["RUSTUP_TOOLCHAIN"] = "1.99.0"
    if not shutil.which("rustup"):
        installer = run.run(["curl", "--proto", "=https", "--tlsv1.2", "-sSf",
                             "https://sh.rustup.rs"])
        path = run.directory / "rustup-init.sh"
        path.write_bytes(installer)
        run.run(["sh", str(path), "-y", "--profile", "minimal", "--default-toolchain", "none"], timeout=600)
    run.run(["rustup", "toolchain", "install", "1.99.0", "--profile", "minimal",
             "--component", "rustfmt", "--component", "clippy"], timeout=900)
    rustc = run.run(["rustc", "-Vv"]).decode()
    if "release: 1.99.0\n" not in rustc:
        raise RuntimeError("Unexpected Rust toolchain; stable fallback forbidden")
    environment = {"rustc": rustc, "cargo": run.run(["cargo", "-Vv"]).decode(),
                   "python": sys.version, "platform": platform.platform(),
                   "cpu_count": os.cpu_count(), "cpu_model_flags": Path("/proc/cpuinfo").read_text(),
                   "affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
                   "rustflags": os.environ.get("RUSTFLAGS", ""),
                   "image": os.environ.get("KAGGLE_DOCKER_IMAGE", "UNKNOWN")}
    for file in ("cpu.max", "memory.max"):
        path = Path("/sys/fs/cgroup") / file
        environment[file] = path.read_text().strip() if path.exists() else "UNKNOWN"
    if os.environ.get("RUSTFLAGS") or any(name.startswith("CARGO_PROFILE_RELEASE_") for name in os.environ):
        raise RuntimeError("External release/CPU flags would change the controlled experiment")
    run.manifest["environment"] = environment
    return {"rust_version": "1.99.0", "target": re.search(r"host: (.+)", rustc).group(1)}


def dependency_metadata(run, source):
    metadata = json.loads(run.run(["cargo", "metadata", "--locked", "--format-version", "1"],
                                 cwd=source, log_output=False))
    root_id = metadata["resolve"]["root"]
    root_node = next(node for node in metadata["resolve"]["nodes"] if node["id"] == root_id)
    packages = {package["id"]: package for package in metadata["packages"]}
    direct = {dependency["name"]: {"name": packages[dependency["pkg"]]["name"],
                                  "version": packages[dependency["pkg"]]["version"],
                                  "id": dependency["pkg"]}
              for dependency in root_node["deps"]}
    return {"root": root_id, "direct": direct,
            "packages": {key: {"name": item["name"], "version": item["version"], "source": item["source"]}
                         for key, item in packages.items()},
            "nodes": metadata["resolve"]["nodes"],
            "lock_sha256": sha256_file(source / "Cargo.lock")}


def materialize(run, payload, temporary):
    snapshots = {}
    for arm in ("baseline", "candidate"):
        source = Path(temporary) / arm
        unpack_snapshot(payload[arm], payload["metadata"][arm + "_tar_sha256"],
                        payload["metadata"][arm + "_files"], source)
        probe = source / "src" / "bin" / "research_probe.rs"
        probe.write_bytes(base64.b64decode(payload["probe"]))
        snapshots[arm] = str(source)
        run.manifest["snapshots"][arm] = {
            "archive_sha256": payload["metadata"][arm + "_tar_sha256"],
            "source_files": payload["metadata"][arm + "_files"],
            "injected_probe_sha256": sha256_file(probe)}
    return snapshots


def build(run, source, arm, run_tests=False):
    target = source / "target"
    run.run(["cargo", "build", "--locked", "--release", "--bin", "athena-core-v2",
             "--bin", "bench_core", "--bin", "research_probe", "--target-dir", target], cwd=source, timeout=1800)
    if run_tests:
        run.run(["cargo", "fmt", "--all", "--", "--check"], cwd=source)
        transcript = run.run(["cargo", "test", "--locked", "--all-targets", "--target-dir", target],
                             cwd=source, timeout=600).decode()
        run.run(["cargo", "clippy", "--locked", "--all-targets", "--target-dir", target, "--", "-D", "warnings"],
                cwd=source, timeout=600)
        run.manifest.setdefault("unit_test_transcripts", {})[arm] = transcript
    binary = target / "release"
    entry = run.manifest["snapshots"].setdefault(arm, {})
    entry["dependency_graph"] = dependency_metadata(run, source)
    entry["binaries"] = {name: sha256_file(binary / name)
                         for name in ("athena-core-v2", "bench_core", "research_probe")}
    run.manifest["checks"]["build_" + arm] = {"status": "PASS", "binaries": entry["binaries"]}
    run.save()
    return binary


def probe_json(run, binary, mode, data=None, args=()):
    content = None if data is None else json.dumps(data).encode()
    return json.loads(run.run([binary / "research_probe", mode, *args],
                              input_bytes=content, log_output=False))


def bm25_reference(texts, query):
    tokenize = lambda text: re.findall(r"[^\W_]+", text.lower(), flags=re.UNICODE)
    documents, terms = [tokenize(text) for text in texts], set(tokenize(query))
    average = max(sum(map(len, documents)) / len(documents), 1.0)
    result = []
    for i, document in enumerate(documents):
        score = 0.0
        for term in sorted(terms):
            frequency = document.count(term)
            df = sum(term in other for other in documents)
            if frequency:
                idf = math.log1p((len(documents) - df + 0.5) / (df + 0.5))
                score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * len(document) / average))
        result.append([i, score])
    return sorted(result, key=lambda item: (-item[1], item[0]))


def probe_oracles(run, binary):
    texts = ["tokio tokio runtime", "runtime", "unrelated", "TOKIO runtime"]
    for query in ("tokio runtime", "tokio tokio", "", "missing"):
        expected = bm25_reference(texts, query)
        actual = probe_json(run, binary, "rank", {"texts": texts, "query": query})["scores"]
        if len(actual) != len(expected):
            raise ValueError("BM25 lost documents")
        for (index, score), (expected_index, expected_score) in zip(actual, expected):
            if index != expected_index or not math.isclose(score, expected_score, rel_tol=5e-13, abs_tol=1e-14):
                raise ValueError("BM25 independent formula/order oracle failed")
    cell = lambda text, rs=1, cs=1, header=False: {
        "text": text, "rowspan": rs, "colspan": cs, "header": header}
    rows = [{"group": 0, "cells": [cell("A", 0, header=True), cell("B")]},
            {"group": 0, "cells": [cell("C")]}, {"group": 1, "cells": [cell("D", cs=2)]}]
    result = probe_json(run, binary, "table", {"rows": rows, "max_rows": 3, "max_cols": 2})
    if (result.get("slots") != [[0, 1], [0, 2], [3, 3]] or result.get("width") != 2
            or result["cells"][0] != {"text": "A", "header": True}):
        raise ValueError("Rowspan/group/header oracle failed")
    overlap = [{"group": 0, "cells": [cell("A"), cell("B", 2)]},
               {"group": 0, "cells": [cell("C", cs=2)]}]
    for data, error in (({"rows": overlap, "max_rows": 2, "max_cols": 3}, "Overlap"),
                        ({"rows": rows, "max_rows": 3, "max_cols": 1}, "TooLarge"),
                        ({"rows": [rows[2], rows[0]], "max_rows": 3, "max_cols": 4}, "InvalidGroupOrder")):
        if probe_json(run, binary, "table", data).get("error") != error:
            raise ValueError("Table boundary/error oracle failed")
    return {"bm25_queries": 4, "table_oracles": 4}


def content_cases():
    return [
        ("entities", '<p>&amp; &quot; &lt; &gt; &copy; &#38; &#x26;</p>',
         [("paragraph", '& " < > © & &', [])], []),
        ("unicode", "<p>Tiếng Việt có dấu và 日本語テキスト</p>",
         [("paragraph", "Tiếng Việt có dấu và 日本語テキスト", [])], []),
        ("scripts", "<script>alert('hidden')</script><style>.hidden{}</style><p>Visible</p>",
         [("paragraph", "Visible", [])], []),
        ("implicit-p", "<p>One<p>Two",
         [("paragraph", "One", []), ("paragraph", "Two", [])], []),
        ("inline-repair", "<p>One <b>two",
         [("paragraph", "One two", [])], []),
        ("table-repair", "<table><tr><td>Cell 1<td>Cell 2</table>",
         [("table_cell", "Cell 1", []), ("table_cell", "Cell 2", [])], []),
        ("links", "<p>A <a href='/a'>B</a> <a href='/a'>C</a></p>",
         [("paragraph", "A B C", [0])], ["https://example.org/a"]),
        ("framing", '<p>X\n{"type":"end"}</p>',
         [("paragraph", 'X {"type":"end"}', [])], []),
    ]


def content_oracles(run, binary, fixture_dir, include_pre=False):
    receipt = {}
    cases = content_cases()
    if include_pre:
        cases.append(("preformatted", "<pre>if ready:\n    run()\n    stop()</pre>",
                      [("preformatted", "if ready:\n    run()\n    stop()", [])], []))
    for name, html, blocks, links in cases:
        path = fixture_dir / (name + ".html")
        path.write_text(html, encoding="utf-8")
        payload = run.run([binary / "athena-core-v2", path, "https://example.org/"], log_output=False)
        frames = validate_aae(payload, {"blocks": blocks, "links": links})
        expected_hash = probe_json(run, binary, "hash", [block[1] for block in blocks])["hash"]
        if frames[0]["content_hash"] != expected_hash:
            raise ValueError("Joined-text hash oracle failed")
        receipt[name] = hashlib.sha256(payload).hexdigest()
    return receipt


def bench_result(run, binary, stage, size, iterations):
    result = json.loads(run.run([binary / "bench_core", stage, size, iterations], log_output=False))
    raw, offsets = result.get("raw_ns"), result.get("sample_offsets_ns")
    if (result.get("protocol") != PROTOCOL_VERSION or result.get("stage") != stage
            or result.get("iterations") != iterations or not isinstance(raw, list)
            or len(raw) != iterations or not isinstance(offsets, list) or len(offsets) != iterations
            or any(type(value) is not int or value <= 0 for value in raw)
            or any(type(value) is not int or value < 0 for value in offsets)
            or offsets != sorted(offsets) or not is_digest(result.get("fingerprint"))
            or (stage != "fixture" and result.get("size") != size)):
        raise ValueError("Incomplete or malformed Rust timing packet")
    return result


def verify_variant_identity(run, binaries, fixture_dir):
    path = fixture_dir / "preformatted.html"
    path.write_text("<pre>if ready:\n    run()\n    stop()</pre>", encoding="utf-8")
    outputs = [run.run([binaries[arm] / "athena-core-v2", path, "https://example.org/"],
                       log_output=False) for arm in ("baseline", "candidate")]
    for output in outputs:
        validate_aae(output)
    validate_aae(outputs[1], {"blocks": [("preformatted", "if ready:\n    run()\n    stop()", [])]})
    if outputs[0] == outputs[1]:
        raise ValueError("Known source-variant control did not distinguish baseline and candidate")
    return {arm: hashlib.sha256(output).hexdigest()
            for arm, output in zip(("baseline", "candidate"), outputs)}


def timing_case(run, binaries, case, size_argument=None):
    fingerprint = None
    for round_id in range(case["rounds"]):
        for arm in (("baseline", "candidate") if round_id % 2 == 0 else ("candidate", "baseline")):
            result = bench_result(run, binaries[arm], case["stage"],
                                  case["size"] if size_argument is None else size_argument, case["iterations"])
            if fingerprint is None:
                fingerprint = result["fingerprint"]
            if result["fingerprint"] != fingerprint:
                raise ValueError("Benchmark result parity failed")
            for iteration, (duration, offset) in enumerate(zip(result["raw_ns"], result["sample_offsets_ns"])):
                run.sample(case, arm, round_id, iteration, duration / 1e6,
                           duration_ns=duration, measurement_offset_ns=offset, output_fingerprint=fingerprint)
    return fingerprint


def execute_c1(run, snapshots):
    binaries = {arm: build(run, source, arm) for arm, source in snapshots.items()}
    run.manifest["checks"]["locked_builds"] = {"status": "PASS"}
    fixture_dir = Path(tempfile.mkdtemp(prefix="fixtures-", dir=snapshots["candidate"].parent))
    for arm in binaries:
        run.check("content_" + arm, lambda arm=arm: content_oracles(run, binaries[arm], fixture_dir))
        run.check("probe_" + arm, lambda arm=arm: probe_oracles(run, binaries[arm]))
    run.check("candidate_preformatted", lambda: content_oracles(run, binaries["candidate"], fixture_dir, True))
    run.check("variant_identity", lambda: verify_variant_identity(run, binaries, fixture_dir))
    receipts = []
    for path in [snapshots["candidate"] / "fixtures" / "example.html", *sorted(fixture_dir.glob("*.html"))]:
        if path.stem == "preformatted":
            continue
        for query in (None, "tokio runtime", "tài 内容", "H100 pricing"):
            outputs = [run.run([binaries[arm] / "athena-core-v2", path, "https://example.org/",
                               *([] if query is None else [query])], log_output=False)
                       for arm in ("baseline", "candidate")]
            require_parity(*outputs)
            receipts.append({"fixture": path.name, "query": query,
                             "sha256": hashlib.sha256(outputs[0]).hexdigest()})
    run.manifest["checks"]["byte_parity"] = {"status": "PASS", "evidence": receipts}
    for case in run.manifest["spec"]["cases"]:
        timing_case(run, binaries, case)


def measured_peak_rss(run, command, rss_path):
    if rss_path.exists():
        raise ValueError("RSS output must be new")
    run.run(["/usr/bin/time", "-f", "%M", "-o", rss_path, "--", *command], log_output=False)
    return parse_peak_rss(rss_path.read_text(encoding="ascii"))


def execute_c2(run, snapshots):
    version = run.run(["/usr/bin/time", "--version"]).decode()
    if "GNU" not in version:
        raise RuntimeError("GNU time with %M is required; no statm polling fallback")
    run.manifest["rss_method"] = {"tool": version, "format": "%M", "unit": "KiB",
        "scope": "kernel peak RSS of one fresh CLI process including startup/input/output; not allocator bytes"}
    binaries = {arm: build(run, source, arm) for arm, source in snapshots.items()}
    fixture_dir = Path(tempfile.mkdtemp(prefix="links-", dir=snapshots["candidate"].parent))
    run.check("candidate_content", lambda: content_oracles(run, binaries["candidate"], fixture_dir, True))
    run.check("variant_identity", lambda: verify_variant_identity(run, binaries, fixture_dir))
    for anchors in (100, 500, 1000, 2500, 5000, 10000, 20000):
        path = fixture_dir / (str(anchors) + ".html")
        path.write_text("<p>" + "".join(f"<a href='/a/{i}'>link {i}</a> " for i in range(anchors)) + "</p>",
                        encoding="utf-8")
        run.manifest.setdefault("fixtures", {})[path.name] = sha256_file(path)
        outputs = [run.run([binaries[arm] / "athena-core-v2", path, "https://example.org/"],
                           log_output=False) for arm in ("baseline", "candidate")]
        require_parity(*outputs)
        frames = validate_aae(outputs[1])
        if sum(frame["type"] == "link" for frame in frames) != anchors:
            raise ValueError("Link sweep dropped catalog entries")
        cases = [case for case in run.manifest["spec"]["cases"] if case["size"] == anchors]
        timing = next(case for case in cases if case["unit"] == "ms")
        timing_case(run, binaries, timing, str(path))
        memory = next(case for case in cases if case["unit"] == "KiB")
        for round_id in range(memory["rounds"]):
            for arm in (("baseline", "candidate") if round_id % 2 == 0 else ("candidate", "baseline")):
                rss_path = fixture_dir / f"rss-{anchors}-{round_id}-{arm}.txt"
                peak = measured_peak_rss(run,
                    [binaries[arm] / "athena-core-v2", path, "https://example.org/"], rss_path)
                run.sample(memory, arm, round_id, 0, peak,
                           output_fingerprint=hashlib.sha256(outputs[0]).hexdigest())
    deep_receipts = {}
    for depth in (50, 100, 200, 400, 500):
        path = fixture_dir / f"deep-{depth}.html"
        text = f"Leaf content at depth {depth}"
        path.write_text("<div>" * depth + f"<p>{text}</p>" + "</div>" * depth, encoding="utf-8")
        output = run.run([binaries["candidate"] / "athena-core-v2", path, "https://example.org/"],
                         log_output=False)
        validate_aae(output, {"blocks": [("paragraph", text, [])]})
        deep_receipts[str(depth)] = hashlib.sha256(output).hexdigest()
    run.manifest["checks"]["deep_dom"] = {"status": "PASS", "evidence": deep_receipts}


def beta_variance_bound(alpha, beta, n, family):
    total = alpha + beta
    variance = (alpha / total) * (beta / total) / (total + 1)
    excess = 6 * ((alpha - beta) ** 2 * (total + 1) - alpha * beta * (total + 2)) / (
        alpha * beta * (total + 2) * (total + 3))
    group_size, group_error = n // 20, 0.05
    if n % 20 or group_size < 1:
        raise ValueError("RNG variance diagnostic requires 20 equal independent groups")
    relative_bound = math.sqrt((excess + 2) / (group_size * group_error))
    failure_bound = sum(math.comb(20, k) * group_error ** k * (1 - group_error) ** (20 - k)
                        for k in range(10, 21))
    if failure_bound > 0.001 / family:
        raise ValueError("RNG variance diagnostic exceeds its predeclared error budget")
    return {"expected": variance, "excess_kurtosis": excess,
            "relative_bound": relative_bound, "failure_bound": failure_bound}


def validate_rng_packet(packet, alpha, beta, n, seed, family):
    if any(packet.get(key) != value for key, value in
           (("alpha", alpha), ("beta", beta), ("n", n), ("seed", seed), ("bandit_consumer", True))):
        raise ValueError("RNG packet identity/consumer mismatch")
    if (packet.get("cutoffs") != [0.1, 0.25, 0.5, 0.9]
            or not isinstance(packet.get("cdf_counts"), list) or len(packet["cdf_counts"]) != 4
            or packet.get("bandit_consumer") is not True
            or type(packet.get("duration_ns")) is not int or packet["duration_ns"] <= 0):
        raise ValueError("Missing RNG CDF/consumer evidence")
    mean = alpha / (alpha + beta)
    second = alpha * (alpha + 1) / ((alpha + beta) * (alpha + beta + 1))
    epsilon = math.sqrt(math.log(2 * family / 0.001) / (2 * n))
    if (not math.isfinite(packet["sum"]) or not math.isfinite(packet["sum_sq"])
            or abs(packet["sum"] / n - mean) > epsilon
            or abs(packet["sum_sq"] / n - second) > epsilon):
        raise ValueError("RNG bounded-moment diagnostic failed")
    for cutoff, count in zip(packet["cutoffs"], packet["cdf_counts"]):
        expected = None
        if alpha == beta and cutoff == 0.5:
            expected = 0.5
        elif alpha == 1:
            expected = -math.expm1(beta * math.log1p(-cutoff))
        elif beta == 1:
            expected = cutoff ** alpha
        elif (alpha, beta) == (2, 5):
            expected = 1 - (1 - cutoff) ** 5 * (1 + 5 * cutoff)
        elif (alpha, beta) == (5, 2):
            expected = cutoff ** 5 * (6 - 5 * cutoff)
        elif alpha == beta == 0.5:
            expected = 2 * math.asin(math.sqrt(cutoff)) / math.pi
        if type(count) is not int or not 0 <= count <= n:
            raise ValueError("Invalid RNG CDF count")
        if expected is not None and abs(count / n - expected) > epsilon:
            raise ValueError("RNG analytic CDF diagnostic failed")
    groups = packet.get("central_second_block_means")
    if (not isinstance(groups, list) or len(groups) != 20
            or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in groups)):
        raise ValueError("Missing RNG variance diagnostic")
    variance = beta_variance_bound(alpha, beta, n, family)
    median = sorted(groups)[10]
    if abs(median / variance["expected"] - 1) > variance["relative_bound"]:
        raise ValueError("RNG median-of-means central variance diagnostic failed")
    return {"epsilon": epsilon, "family_checks": family,
            "central_variance": variance,
            "scope": "predeclared moment/CDF/variance diagnostics under iid model, not a distribution or crypto proof"}


def rng_dependency_closure(graph):
    nodes = {node["id"]: node for node in graph["nodes"]}
    pending = [value["id"] for key, value in graph["direct"].items() if key in ("rand", "rand_distr")]
    reached = set()
    while pending:
        node_id = pending.pop()
        if node_id not in reached:
            reached.add(node_id)
            pending.extend(dependency["pkg"] for dependency in nodes[node_id]["deps"])
    return reached


def preserve_rng_attempt(run, source):
    parent = run.manifest["snapshots"]["candidate"]
    run.manifest["snapshots"]["rng-upgrade"] = {
        "source_transform": "RNG manifest pins only; compilation/resolution status recorded by commands",
        "parent_archive_sha256": parent["archive_sha256"],
        "source_files": {name: sha256_file(source / name) for name in parent["source_files"]},
        "injected_probe_sha256": sha256_file(source / "src/bin/research_probe.rs")}
    for name in ("Cargo.toml", "Cargo.lock"):
        (run.directory / ("rng-upgrade-" + name)).write_bytes((source / name).read_bytes())
    run.save()


def execute_c3(run, snapshots):
    original = snapshots["candidate"]
    old_bin = build(run, original, "candidate", run_tests=True)
    run.check("core_oracles", lambda: probe_oracles(run, old_bin))
    run.check("singleflight", lambda: probe_json(run, old_bin, "singleflight"))
    run.check("ip_classifier", lambda: probe_json(run, old_bin, "ip"))
    transcript = run.manifest["unit_test_transcripts"]["candidate"]
    names = ("cancelled_dns_worker_retains_origin_admission_until_exit",
             "cancelled_blocking_job_keeps_cpu_admission_until_exit",
             "pending_query_fanout_is_bounded_before_shared_source_wait",
             "caller_deadline_covers_waiting_for_a_shared_source")
    if any(not re.search(r"test [^\n]*" + name + r"\s+\.\.\.\s+ok", transcript) for name in names):
        raise ValueError("Required Engine admission/cancellation regressions were not executed")
    run.manifest["checks"]["engine_resource_regressions"] = {"status": "PASS", "tests": names}
    upgraded = original.parent / "rng-upgrade"
    shutil.copytree(original, upgraded, ignore=shutil.ignore_patterns("target"))
    manifest_path = upgraded / "Cargo.toml"
    text = manifest_path.read_text(encoding="utf-8")
    for name, version in (("rand", "0.10.3"), ("rand_distr", "0.6.0")):
        text, count = re.subn(r"(?m)^" + name + r'\s*=\s*"[^"]+"', f'{name} = "={version}"', text)
        if count != 1:
            raise ValueError("Unexpected dependency declaration: " + name)
    manifest_path.write_text(text, encoding="utf-8")
    preserve_rng_attempt(run, upgraded)
    try:
        run.run(["cargo", "check", "--all-targets", "--target-dir", upgraded / "target"], cwd=upgraded, timeout=900)
    finally:
        preserve_rng_attempt(run, upgraded)
    graph = dependency_metadata(run, upgraded)
    old_graph = run.manifest["snapshots"]["candidate"]["dependency_graph"]
    allowed = rng_dependency_closure(old_graph) | rng_dependency_closure(graph)
    changed = set(old_graph["packages"]) ^ set(graph["packages"])
    if changed - allowed:
        raise ValueError("Unexpected dependency changes outside RNG closure")
    for key, version in (("rand", "0.10.3"), ("rand_distr", "0.6.0")):
        if graph["direct"][key]["version"] != version:
            raise ValueError("Unexpected resolved RNG version")
    run.manifest["snapshots"]["rng-upgrade"]["dependency_changes"] = sorted(changed)
    new_bin = build(run, upgraded, "rng-upgrade", run_tests=True)
    binaries = {"baseline": old_bin, "candidate": new_bin}
    cases = run.manifest["spec"]["cases"]
    family = len(cases) * cases[0]["rounds"] * 2 * 7
    for case in cases:
        alpha, beta, n = case["alpha"], case["beta"], case["size"]
        for round_id in range(case["rounds"]):
            seed = 1009 + round_id
            for arm in (("baseline", "candidate") if round_id % 2 == 0 else ("candidate", "baseline")):
                packet = probe_json(run, binaries[arm], "rng", args=(alpha, beta, n, seed))
                diagnostic = validate_rng_packet(packet, alpha, beta, n, seed, family)
                run.sample(case, arm, round_id, 0, packet["duration_ns"] / n,
                           seed=seed, diagnostics=packet, distribution_check=diagnostic)
    run.manifest["checks"]["rng_distribution"] = {"status": "PASS", "family_checks": family}
    run.manifest["coverage"] = {
        "R1": {"status": "MEASURED", "scope": "exact RNG tuple, real Bandit consumer, core tests, 8 Beta shapes, paired throughput"},
        "R2": {"status": "NOT_RUN", "reason": "No alternative TreeSink/lol_html candidate supplied"},
        "R3": {"status": "CLASSIFIER_ONLY", "reason": "No live DNS/TLS/connection-reuse experiment"},
        "R4": {"status": "ORACLE_AND_C1", "reason": "No reused-index/top-K candidate supplied"},
        "R5": {"status": "SEE_C2"}, "R6": {"status": "GRID_ORACLES_ONLY", "reason": "HTML table header integration not implemented"},
        "R7": {"status": "SEE_C1", "reason": "No socket streaming candidate supplied"},
        "R8": {"status": "STATE_TESTED", "scope": "SingleFlight and existing Engine blocking-lifetime/admission regressions; not production load"},
        "R9": {"status": "NOT_RUN", "reason": "No labelled corpus/model candidate"},
        "dependency_advisories": {"status": "NOT_RUN", "reason": "Audit the preserved exact lockfiles separately before a library adoption"}}


def execute_cluster(cluster, payload, spec, bundle_id):
    if not Path("/kaggle/working").is_dir() or platform.system() != "Linux":
        raise RuntimeError("Kaggle/Linux runner only; local use is preparation/unit verification")
    output = Path("/kaggle/working") / ("athena-v2-" + cluster)
    run = EvidenceRun(output, cluster, bundle_id, spec)
    try:
        run.check("environment", lambda: prepare_toolchain(run))
        with tempfile.TemporaryDirectory(prefix="athena-v2-") as temporary:
            snapshots = run.check("source_integrity", lambda: materialize(run, payload, temporary))
            snapshots = {arm: Path(path) for arm, path in snapshots.items()}
            # Paths are runtime ownership, not JSON evidence.
            run.manifest["checks"]["source_integrity"]["evidence"] = {
                arm: payload["metadata"][arm + "_tar_sha256"] for arm in snapshots}
            run.save()
            {"c1": execute_c1, "c2": execute_c2, "c3": execute_c3}[cluster](run, snapshots)
    except BaseException as error:
        run.finish(error)
        raise
    run.finish()
    print("Saved raw evidence:", output, "; no automatic core/library approval")

