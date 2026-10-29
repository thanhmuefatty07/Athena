"""Pinned, bounded source preparation for HTML attribute-index research arms."""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tarfile
import tempfile
import tomllib

from kaggle_experiments import protocol


CRATES = {
    "0.39.0": ("cloud_experiments/vendor/html5ever-0.39.0.crate",
               "46a1761807faccc9a19e86944bbf40610014066306f96edcdedc2fb714bcb7b8"),
    "0.40.1": ("cloud_experiments/vendor/html5ever-0.40.1.crate",
               "456a1a377e608e555d22ddab27ac0114bc7a7b4199078108e34c2aeae6c9b130"),
}
STRATEGIES = {"vector": None, "hash": 0, "hybrid8": 8, "hybrid32": 32, "hybrid128": 128}
MAX_ARCHIVE_BYTES = 100_000
MAX_MEMBERS = 64
MAX_UNPACKED_BYTES = 1_000_000
WPT_REPORT_LIMITS = {"total_cases": 1949, "executed": 1733,
                    "skipped_fragments": 206, "skipped_script_off": 10}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def vendor_directory(version: str, strategy: str) -> str:
    if version not in CRATES or strategy not in STRATEGIES or strategy == "vector":
        raise ValueError("Unsupported parser-study crate or strategy")
    return f"research-html5ever-{version}-{strategy}"


def parse_wpt_report(output: str | bytes) -> dict:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="strict")
    if not isinstance(output, str):
        raise ValueError("WPT report output is not text")
    prefix = "ATHENA_WPT_REPORT_JSON="
    payloads = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if len(payloads) != 1:
        raise ValueError("WPT run did not emit exactly one structured report")
    report = protocol.strict_json_loads(payloads[0])
    return validate_wpt_report(report)


def validate_wpt_report(report: dict) -> dict:
    expected_keys = {"schema", *WPT_REPORT_LIMITS, "tree_mismatches",
                     "annotation_count_deltas", "cases"}
    if not isinstance(report, dict) or set(report) != expected_keys:
        raise ValueError("WPT report schema is incomplete or has unknown fields")
    if report["schema"] != "athena-wpt-differential/1":
        raise ValueError("Unsupported WPT report schema")
    for name, expected in WPT_REPORT_LIMITS.items():
        if type(report[name]) is not int or report[name] != expected:
            raise ValueError("WPT report case inventory differs from the pinned corpus: " + name)
    if (type(report["tree_mismatches"]) is not int
            or type(report["annotation_count_deltas"]) is not int
            or not isinstance(report["cases"], list)
            or len(report["cases"]) != WPT_REPORT_LIMITS["executed"]):
        raise ValueError("WPT report contains invalid counts or an incomplete executed-case list")

    case_keys = set()
    tree_mismatches = annotation_deltas = 0
    for record in report["cases"]:
        if not isinstance(record, dict) or set(record) != {
                "fixture", "case", "actual_tree_blake3", "expected_tree_blake3",
                "warnings", "expected_warning_count"}:
            raise ValueError("WPT report has an invalid case record")
        fixture, number = record["fixture"], record["case"]
        if not isinstance(fixture, str) or not fixture or type(number) is not int or number < 1:
            raise ValueError("WPT report has an invalid case identity")
        if (fixture, number) in case_keys:
            raise ValueError("WPT report contains a duplicate case identity")
        case_keys.add((fixture, number))
        for field in ("actual_tree_blake3", "expected_tree_blake3"):
            digest = record[field]
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)):
                raise ValueError("WPT report contains an invalid tree digest")
        for field in ("warnings", "expected_warning_count"):
            if type(record[field]) is not int or record[field] < 0:
                raise ValueError("WPT report contains an invalid warning count")
        tree_mismatches += record["actual_tree_blake3"] != record["expected_tree_blake3"]
        annotation_deltas += record["warnings"] != record["expected_warning_count"]
    if (tree_mismatches != report["tree_mismatches"]
            or annotation_deltas != report["annotation_count_deltas"]):
        raise ValueError("WPT report summary does not match its case-level evidence")
    return report


def compare_wpt_cases(reference: list[dict], candidate: list[dict]) -> dict:
    def index(records):
        result = {}
        for record in records:
            key = (record["fixture"], record["case"])
            if key in result:
                raise ValueError("WPT differential has duplicate case identities")
            result[key] = record
        return result

    before, after = index(reference), index(candidate)
    if not before or before.keys() != after.keys():
        raise ValueError("WPT differential case inventories differ")
    changed_tree, changed_warnings = [], []
    new_tree_mismatches, resolved_tree_mismatches = [], []
    new_warning_deltas, resolved_warning_deltas = [], []
    for key in sorted(before):
        old, new = before[key], after[key]
        if (old["expected_tree_blake3"] != new["expected_tree_blake3"]
                or old["expected_warning_count"] != new["expected_warning_count"]):
            raise ValueError("WPT differential expected oracle changed between parser arms")
        name = f"{key[0]}:{key[1]}"
        old_tree_bad = old["actual_tree_blake3"] != old["expected_tree_blake3"]
        new_tree_bad = new["actual_tree_blake3"] != new["expected_tree_blake3"]
        old_warning_bad = old["warnings"] != old["expected_warning_count"]
        new_warning_bad = new["warnings"] != new["expected_warning_count"]
        if old["actual_tree_blake3"] != new["actual_tree_blake3"]:
            changed_tree.append(name)
        if old["warnings"] != new["warnings"]:
            changed_warnings.append(name)
        if not old_tree_bad and new_tree_bad:
            new_tree_mismatches.append(name)
        if old_tree_bad and not new_tree_bad:
            resolved_tree_mismatches.append(name)
        if not old_warning_bad and new_warning_bad:
            new_warning_deltas.append(name)
        if old_warning_bad and not new_warning_bad:
            resolved_warning_deltas.append(name)
    return {"schema": "athena-wpt-arm-delta/1", "case_count": len(before),
            "changed_tree_cases": changed_tree, "changed_warning_cases": changed_warnings,
            "new_tree_mismatches": new_tree_mismatches,
            "resolved_tree_mismatches": resolved_tree_mismatches,
            "new_warning_deltas": new_warning_deltas,
            "resolved_warning_deltas": resolved_warning_deltas,
            "status": "SEMANTICALLY_IDENTICAL" if not changed_tree and not changed_warnings
                      else "CHANGED_REQUIRES_REVIEW"}


def _archive_files(files, version: str) -> dict[str, bytes]:
    try:
        archive_name, expected_digest = CRATES[version]
        data = files[archive_name]
    except (KeyError, TypeError) as error:
        raise ValueError("Pinned html5ever source archive is missing") from error
    if not isinstance(data, bytes) or len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Pinned html5ever source archive exceeds its input bound")
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise ValueError("Pinned html5ever source archive digest mismatch")

    root = f"html5ever-{version}"
    output: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if not 0 < len(members) <= MAX_MEMBERS:
                raise ValueError("html5ever archive member count exceeds its bound")
            for member in members:
                name = member.name
                path = PurePosixPath(name)
                if ("\\" in name or path.is_absolute() or not path.parts
                        or path.parts[0] != root or any(part in ("", ".", "..") for part in path.parts)
                        or member.issym() or member.islnk() or not (member.isdir() or member.isfile())):
                    raise ValueError("Unsafe path or member type in pinned html5ever archive")
                if member.isdir():
                    continue
                total += member.size
                if member.size < 0 or total > MAX_UNPACKED_BYTES or name in output:
                    raise ValueError("html5ever archive size or path inventory is invalid")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Unreadable file in pinned html5ever archive")
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    raise ValueError("Truncated or overlong file in pinned html5ever archive")
                output["/".join(path.parts[1:])] = content
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ValueError("Pinned html5ever archive is malformed") from error

    required = {"Cargo.toml", "LICENSE-APACHE", "LICENSE-MIT", "src/tokenizer/mod.rs"}
    if not required <= output.keys():
        raise ValueError("Pinned html5ever source archive lacks required package files")
    try:
        package = tomllib.loads(output["Cargo.toml"].decode("utf-8"))["package"]
    except (UnicodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError("Pinned html5ever manifest is invalid") from error
    if package.get("name") != "html5ever" or package.get("version") != version:
        raise ValueError("Pinned html5ever archive identity mismatch")
    return output


def patched_tokenizer(files, version: str, strategy: str) -> bytes:
    if version not in CRATES or strategy not in STRATEGIES or strategy == "vector":
        raise ValueError("Unsupported parser-study crate or strategy")
    source_files = _archive_files(files, version)
    source = source_files["src/tokenizer/mod.rs"].decode("utf-8")
    threshold = STRATEGIES[strategy]

    def replace_once(old: str, new: str) -> None:
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("Pinned tokenizer source anchor is absent or ambiguous")
        source = source.replace(old, new, 1)

    replace_once("use std::collections::BTreeMap;",
                 "use std::collections::{BTreeMap, HashSet};")
    replace_once("    current_tag_attrs: RefCell<Vec<Attribute>>,",
                 "    current_tag_attrs: RefCell<Vec<Attribute>>,\n"
                 "    current_tag_attr_index: RefCell<HashSet<LocalName>>,")
    replace_once("            current_tag_attrs: RefCell::new(vec![]),",
                 "            current_tag_attrs: RefCell::new(vec![]),\n"
                 "            current_tag_attr_index: RefCell::new(HashSet::new()),")
    replace_once(
        "        self.current_tag_had_duplicate_attributes.set(false);\n"
        "        *self.current_tag_attrs.borrow_mut() = vec![];",
        "        let had_duplicate_attributes = self.current_tag_had_duplicate_attributes.replace(false);\n"
        "        let mut attrs = self.current_tag_attrs.borrow_mut();\n"
        "        if attrs.len() > ATHENA_ATTR_INDEX_THRESHOLD\n"
        "            || (attrs.len() == ATHENA_ATTR_INDEX_THRESHOLD && had_duplicate_attributes)\n"
        "        {\n"
        "            self.current_tag_attr_index.borrow_mut().clear();\n"
        "        }\n"
        "        attrs.clear();",
    )
    replace_once(
        "        let token = TagToken(Tag {",
        "        let attrs = std::mem::take(&mut *self.current_tag_attrs.borrow_mut());\n"
        "        let had_duplicate_attributes = self.current_tag_had_duplicate_attributes.get();\n"
        "        if attrs.len() > ATHENA_ATTR_INDEX_THRESHOLD\n"
        "            || (attrs.len() == ATHENA_ATTR_INDEX_THRESHOLD && had_duplicate_attributes)\n"
        "        {\n"
        "            self.current_tag_attr_index.borrow_mut().clear();\n"
        "        }\n"
        "        let token = TagToken(Tag {",
    )
    replace_once(
        "            attrs: std::mem::take(&mut self.current_tag_attrs.borrow_mut()),\n"
        "            had_duplicate_attributes: self.current_tag_had_duplicate_attributes.get(),",
        "            attrs,\n"
        "            had_duplicate_attributes,",
    )

    method_start = "    fn finish_attribute(&self) {"
    method_end = "\n    fn emit_current_doctype(&self) {"
    if source.count(method_start) != 1 or source.count(method_end) != 1:
        raise ValueError("Pinned tokenizer attribute method boundary is absent or ambiguous")
    start = source.index(method_start)
    end = source.index(method_end, start)
    body = (
        "        let name = {\n"
        "            let mut attr_name = self.current_attr_name.borrow_mut();\n"
        "            if attr_name.is_empty() {\n"
        "                return;\n"
        "            }\n"
        "            let name = LocalName::from(&**attr_name);\n"
        "            attr_name.clear();\n"
        "            name\n"
        "        };\n"
        "        let dup = {\n"
        "            let attrs = self.current_tag_attrs.borrow();\n"
        "            if attrs.len() < ATHENA_ATTR_INDEX_THRESHOLD {\n"
        "                attrs.iter().any(|a| a.name.local == name)\n"
        "            } else {\n"
        "                let mut seen = self.current_tag_attr_index.borrow_mut();\n"
        "                if seen.is_empty() {\n"
        "                    seen.reserve(attrs.len().saturating_add(8));\n"
        "                    for attr in attrs.iter() {\n"
        "                        seen.insert(attr.name.local.clone());\n"
        "                    }\n"
        "                }\n"
        "                !seen.insert(name.clone())\n"
        "            }\n"
        "        };\n"
        "        if dup {\n"
        '            self.emit_error(Borrowed("Duplicate attribute"));\n'
        "            self.current_tag_had_duplicate_attributes.set(true);\n"
        "            self.current_attr_value.borrow_mut().clear();\n"
        "        } else {\n"
        "            self.current_tag_attrs.borrow_mut().push(Attribute {\n"
        "                // The tree builder will adjust the namespace if necessary.\n"
        "                // This only happens in foreign elements.\n"
        "                name: QualName::new(None, ns!(), name),\n"
        "                value: mem::take(&mut self.current_attr_value.borrow_mut()),\n"
        "            });\n"
        "        }\n"
        "    }\n"
    )
    source = source[:start] + method_start + "\n" + body + source[end:]
    replace_once("use crate::{Attribute, LocalName, QualName, SmallCharSet};",
                 "use crate::{Attribute, LocalName, QualName, SmallCharSet};\n\n"
                 f"const ATHENA_ATTR_INDEX_THRESHOLD: usize = {threshold};")
    return source.encode("utf-8")


def materialize_vendor(files, workspace, version: str, strategy: str) -> dict[str, str]:
    """Safely materialize one source-pinned crate copy beneath a run-owned workspace."""
    destination_name = vendor_directory(version, strategy)
    workspace = Path(workspace).resolve(strict=True)
    destination = workspace / destination_name
    if _is_link(destination) or not destination.resolve(strict=False).is_relative_to(workspace):
        raise ValueError("html5ever candidate path escaped the run workspace")
    package_files = _archive_files(files, version)
    package_files["src/tokenizer/mod.rs"] = patched_tokenizer(files, version, strategy)
    expected_files = set(package_files)

    if destination.exists():
        if not destination.is_dir():
            raise ValueError("Existing html5ever candidate path is not a directory")
        observed_files = set()
        for path in destination.rglob("*"):
            if _is_link(path) or not path.resolve().is_relative_to(workspace):
                raise ValueError("html5ever candidate contains a link or escaped path")
            if path.is_file():
                observed_files.add(path.relative_to(destination).as_posix())
            elif not path.is_dir():
                raise ValueError("html5ever candidate contains a non-regular entry")
        if observed_files != expected_files:
            raise ValueError("Existing html5ever candidate inventory differs from the pinned archive")
        for relative, content in package_files.items():
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if target.read_bytes() != content:
                raise ValueError("Existing html5ever candidate contents differ from the frozen strategy")
    else:
        with tempfile.TemporaryDirectory(prefix=".html5ever-study-", dir=workspace) as temporary:
            staging = Path(temporary)
            for relative, content in package_files.items():
                target = staging.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(destination)
            os.replace(staging, destination)
    return {"directory": destination_name,
            "source_sha256": hashlib.sha256(package_files["src/tokenizer/mod.rs"]).hexdigest(),
            "archive_sha256": CRATES[version][1],
            "strategy": strategy, "version": version}
