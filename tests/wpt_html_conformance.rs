//! Pinned WPT tree-construction conformance for Athena's bounded document parser.
//! The corpus and redistribution notice are under tests/fixtures/wpt-html5.
//! Fragment tests are excluded because Athena has no fragment parsing API.
//! Four scripted fixtures are excluded because their expected trees require
//! JavaScript execution, which this parser API deliberately does not perform.

use athena_core_v2::projection::{ParseLimits, parse_html_document_with_node_limit};
use scraper::Node;
use std::collections::BTreeMap;
use std::fmt::Write as _;

const WPT: &[(&str, &str)] = &[
    (
        "adoption01.dat",
        include_str!("fixtures/wpt-html5/adoption01.dat"),
    ),
    (
        "adoption02.dat",
        include_str!("fixtures/wpt-html5/adoption02.dat"),
    ),
    ("blocks.dat", include_str!("fixtures/wpt-html5/blocks.dat")),
    (
        "comments01.dat",
        include_str!("fixtures/wpt-html5/comments01.dat"),
    ),
    (
        "doctype01.dat",
        include_str!("fixtures/wpt-html5/doctype01.dat"),
    ),
    (
        "domjs-unsafe.dat",
        include_str!("fixtures/wpt-html5/domjs-unsafe.dat"),
    ),
    (
        "entities01.dat",
        include_str!("fixtures/wpt-html5/entities01.dat"),
    ),
    (
        "entities02.dat",
        include_str!("fixtures/wpt-html5/entities02.dat"),
    ),
    (
        "foreign-fragment.dat",
        include_str!("fixtures/wpt-html5/foreign-fragment.dat"),
    ),
    (
        "html5test-com.dat",
        include_str!("fixtures/wpt-html5/html5test-com.dat"),
    ),
    (
        "inbody01.dat",
        include_str!("fixtures/wpt-html5/inbody01.dat"),
    ),
    (
        "isindex.dat",
        include_str!("fixtures/wpt-html5/isindex.dat"),
    ),
    (
        "main-element.dat",
        include_str!("fixtures/wpt-html5/main-element.dat"),
    ),
    ("math.dat", include_str!("fixtures/wpt-html5/math.dat")),
    (
        "menuitem-element.dat",
        include_str!("fixtures/wpt-html5/menuitem-element.dat"),
    ),
    (
        "namespace-sensitivity.dat",
        include_str!("fixtures/wpt-html5/namespace-sensitivity.dat"),
    ),
    (
        "pending-spec-changes-plain-text-unsafe.dat",
        include_str!("fixtures/wpt-html5/pending-spec-changes-plain-text-unsafe.dat"),
    ),
    (
        "pending-spec-changes.dat",
        include_str!("fixtures/wpt-html5/pending-spec-changes.dat"),
    ),
    (
        "plain-text-unsafe.dat",
        include_str!("fixtures/wpt-html5/plain-text-unsafe.dat"),
    ),
    (
        "processing-instructions.dat",
        include_str!("fixtures/wpt-html5/processing-instructions.dat"),
    ),
    (
        "quirks01.dat",
        include_str!("fixtures/wpt-html5/quirks01.dat"),
    ),
    ("ruby.dat", include_str!("fixtures/wpt-html5/ruby.dat")),
    (
        "scriptdata01.dat",
        include_str!("fixtures/wpt-html5/scriptdata01.dat"),
    ),
    (
        "search-element.dat",
        include_str!("fixtures/wpt-html5/search-element.dat"),
    ),
    ("svg.dat", include_str!("fixtures/wpt-html5/svg.dat")),
    (
        "tables01.dat",
        include_str!("fixtures/wpt-html5/tables01.dat"),
    ),
    (
        "template.dat",
        include_str!("fixtures/wpt-html5/template.dat"),
    ),
    ("tests1.dat", include_str!("fixtures/wpt-html5/tests1.dat")),
    (
        "tests10.dat",
        include_str!("fixtures/wpt-html5/tests10.dat"),
    ),
    (
        "tests11.dat",
        include_str!("fixtures/wpt-html5/tests11.dat"),
    ),
    (
        "tests12.dat",
        include_str!("fixtures/wpt-html5/tests12.dat"),
    ),
    (
        "tests14.dat",
        include_str!("fixtures/wpt-html5/tests14.dat"),
    ),
    (
        "tests15.dat",
        include_str!("fixtures/wpt-html5/tests15.dat"),
    ),
    (
        "tests16.dat",
        include_str!("fixtures/wpt-html5/tests16.dat"),
    ),
    (
        "tests17.dat",
        include_str!("fixtures/wpt-html5/tests17.dat"),
    ),
    (
        "tests18.dat",
        include_str!("fixtures/wpt-html5/tests18.dat"),
    ),
    (
        "tests19.dat",
        include_str!("fixtures/wpt-html5/tests19.dat"),
    ),
    ("tests2.dat", include_str!("fixtures/wpt-html5/tests2.dat")),
    (
        "tests20.dat",
        include_str!("fixtures/wpt-html5/tests20.dat"),
    ),
    (
        "tests21.dat",
        include_str!("fixtures/wpt-html5/tests21.dat"),
    ),
    (
        "tests22.dat",
        include_str!("fixtures/wpt-html5/tests22.dat"),
    ),
    (
        "tests23.dat",
        include_str!("fixtures/wpt-html5/tests23.dat"),
    ),
    (
        "tests24.dat",
        include_str!("fixtures/wpt-html5/tests24.dat"),
    ),
    (
        "tests25.dat",
        include_str!("fixtures/wpt-html5/tests25.dat"),
    ),
    (
        "tests26.dat",
        include_str!("fixtures/wpt-html5/tests26.dat"),
    ),
    ("tests3.dat", include_str!("fixtures/wpt-html5/tests3.dat")),
    ("tests4.dat", include_str!("fixtures/wpt-html5/tests4.dat")),
    ("tests5.dat", include_str!("fixtures/wpt-html5/tests5.dat")),
    ("tests6.dat", include_str!("fixtures/wpt-html5/tests6.dat")),
    ("tests7.dat", include_str!("fixtures/wpt-html5/tests7.dat")),
    ("tests8.dat", include_str!("fixtures/wpt-html5/tests8.dat")),
    ("tests9.dat", include_str!("fixtures/wpt-html5/tests9.dat")),
    (
        "tests_innerHTML_1.dat",
        include_str!("fixtures/wpt-html5/tests_innerHTML_1.dat"),
    ),
    (
        "tricky01.dat",
        include_str!("fixtures/wpt-html5/tricky01.dat"),
    ),
    (
        "void-in-phrasing.dat",
        include_str!("fixtures/wpt-html5/void-in-phrasing.dat"),
    ),
    (
        "webkit01.dat",
        include_str!("fixtures/wpt-html5/webkit01.dat"),
    ),
    (
        "webkit02.dat",
        include_str!("fixtures/wpt-html5/webkit02.dat"),
    ),
];

const HTML_NS: &str = "http://www.w3.org/1999/xhtml";
const SVG_NS: &str = "http://www.w3.org/2000/svg";
const MATHML_NS: &str = "http://www.w3.org/1998/Math/MathML";
const XLINK_NS: &str = "http://www.w3.org/1999/xlink";
const XML_NS: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS_NS: &str = "http://www.w3.org/2000/xmlns/";

struct WptCase {
    description: String,
    input: String,
    annotated_error_count: usize,
    fragment_context: Option<String>,
    scripting_enabled: Option<bool>,
    expected_tree: String,
}

fn is_directive(line: &str) -> bool {
    matches!(
        line,
        "#new-errors" | "#document-fragment" | "#script-off" | "#script-on" | "#document"
    )
}

fn parse_fixture(name: &str, fixture: &str) -> Result<Vec<WptCase>, String> {
    let lines: Vec<&str> = fixture.split('\n').collect();
    let mut index = 0;
    let mut cases = Vec::new();

    while index < lines.len() {
        while index < lines.len() && lines[index].is_empty() {
            index += 1;
        }
        if index == lines.len() {
            break;
        }

        let mut description = String::new();
        if lines[index] == "#description" {
            index += 1;
            let start = index;
            while index < lines.len() && lines[index] != "#data" {
                index += 1;
            }
            if index == lines.len() {
                return Err(format!("{name}: description is not followed by #data"));
            }
            description = lines[start..index].join("\n");
        }
        if lines[index] != "#data" {
            return Err(format!(
                "{name}: expected #data at line {}, found {:?}",
                index + 1,
                lines[index]
            ));
        }

        index += 1;
        let data_start = index;
        while index < lines.len() && lines[index] != "#errors" {
            index += 1;
        }
        if index == lines.len() {
            return Err(format!("{name}: test has no #errors section"));
        }
        let input = lines[data_start..index].join("\n");

        index += 1;
        let mut annotated_error_count = 0;
        while index < lines.len() && !is_directive(lines[index]) {
            if lines[index].is_empty() {
                return Err(format!(
                    "{name}: blank parse-error entry at line {}",
                    index + 1
                ));
            }
            annotated_error_count += 1;
            index += 1;
        }

        if index < lines.len() && lines[index] == "#new-errors" {
            index += 1;
            while index < lines.len() && !is_directive(lines[index]) {
                if lines[index].is_empty() {
                    return Err(format!(
                        "{name}: blank #new-errors entry at line {}",
                        index + 1
                    ));
                }
                annotated_error_count += 1;
                index += 1;
            }
        }

        let fragment_context = if index < lines.len() && lines[index] == "#document-fragment" {
            index += 1;
            let context = lines
                .get(index)
                .ok_or_else(|| format!("{name}: missing fragment context"))?;
            index += 1;
            Some((*context).to_owned())
        } else {
            None
        };

        let scripting_enabled = if index < lines.len() {
            match lines[index] {
                "#script-off" => {
                    index += 1;
                    Some(false)
                }
                "#script-on" => {
                    index += 1;
                    Some(true)
                }
                _ => None,
            }
        } else {
            None
        };

        if index >= lines.len() || lines[index] != "#document" {
            return Err(format!(
                "{name}: expected #document at line {}, found {:?}",
                index + 1,
                lines.get(index)
            ));
        }
        index += 1;

        let tree_start = index;
        while index < lines.len() {
            if lines[index].is_empty() {
                let mut next_case = index;
                while next_case < lines.len() && lines[next_case].is_empty() {
                    next_case += 1;
                }
                if next_case == lines.len() || matches!(lines[next_case], "#data" | "#description")
                {
                    break;
                }
            }
            index += 1;
        }
        if tree_start == index {
            return Err(format!("{name}: empty #document tree"));
        }
        let expected_tree = lines[tree_start..index].join("\n");

        cases.push(WptCase {
            description,
            input,
            annotated_error_count,
            fragment_context,
            scripting_enabled,
            expected_tree,
        });
    }

    if cases.is_empty() {
        return Err(format!("{name}: fixture contains no cases"));
    }
    Ok(cases)
}

fn tag_name(name: &html5ever::QualName) -> Result<String, String> {
    let namespace: &str = name.ns.as_ref();
    let local: &str = name.local.as_ref();
    let prefix = match namespace {
        HTML_NS => "",
        SVG_NS => "svg ",
        MATHML_NS => "math ",
        other => return Err(format!("unsupported element namespace {other:?}")),
    };
    Ok(format!("{prefix}{local}"))
}

fn attribute_name(name: &html5ever::QualName) -> Result<String, String> {
    let namespace: &str = name.ns.as_ref();
    let local: &str = name.local.as_ref();
    let prefix = match namespace {
        "" => "",
        XLINK_NS => "xlink ",
        XML_NS => "xml ",
        XMLNS_NS => "xmlns ",
        other => return Err(format!("unsupported attribute namespace {other:?}")),
    };
    Ok(format!("{prefix}{local}"))
}

fn serialize_tree(document: &scraper::Html) -> Result<String, String> {
    let mut output = String::new();
    let mut skipped_subtree_depth = None;
    for node in document.tree.root().descendants() {
        let depth = node.ancestors().count().saturating_sub(1);
        if let Some(skip_depth) = skipped_subtree_depth {
            if depth > skip_depth {
                continue;
            }
            skipped_subtree_depth = None;
        }
        let indent = "  ".repeat(depth);
        match node.value() {
            Node::Document => {}
            Node::Fragment => {
                writeln!(output, "| {indent}content").unwrap();
            }
            Node::Doctype(doctype) => {
                let mut line = format!("<!DOCTYPE {}", doctype.name);
                if !doctype.public_id.is_empty() || !doctype.system_id.is_empty() {
                    write!(line, " \"{}\" \"{}\"", doctype.public_id, doctype.system_id).unwrap();
                }
                line.push('>');
                writeln!(output, "| {indent}{line}").unwrap();
            }
            Node::Comment(comment) => {
                writeln!(output, "| {indent}<!-- {} -->", comment.comment).unwrap();
            }
            Node::Text(text) => {
                let in_template_content = node
                    .parent()
                    .is_some_and(|parent| matches!(parent.value(), Node::Fragment));
                let merge_siblings = !in_template_content;
                if merge_siblings
                    && node
                        .prev_sibling()
                        .is_some_and(|previous| matches!(previous.value(), Node::Text(_)))
                {
                    continue;
                }
                let mut normalized_text = text.text.to_string();
                if merge_siblings {
                    let mut sibling = node.next_sibling();
                    while let Some(next) = sibling {
                        if let Node::Text(next_text) = next.value() {
                            normalized_text.push_str(&next_text.text);
                            sibling = next.next_sibling();
                        } else {
                            break;
                        }
                    }
                }
                if !normalized_text.is_empty() {
                    writeln!(output, "| {indent}\"{normalized_text}\"").unwrap();
                }
            }
            Node::Element(element) => {
                if element.attrs.iter().any(|(name, _)| {
                    let namespace: &str = name.ns.as_ref();
                    let local_name: &str = name.local.as_ref();
                    namespace.is_empty() && local_name == "data-skip"
                }) {
                    skipped_subtree_depth = Some(depth);
                    continue;
                }
                let rendered_tag = tag_name(&element.name)?;
                writeln!(output, "| {indent}<{rendered_tag}>").unwrap();

                let mut attributes = element
                    .attrs
                    .iter()
                    .map(|(name, value)| Ok((attribute_name(name)?, value.as_ref().to_owned())))
                    .collect::<Result<Vec<_>, String>>()?;
                attributes
                    .sort_by(|(left, _), (right, _)| left.encode_utf16().cmp(right.encode_utf16()));
                for (name, value) in attributes {
                    writeln!(output, "| {indent}  {name}=\"{value}\"").unwrap();
                }
            }
            Node::ProcessingInstruction(instruction) => {
                writeln!(
                    output,
                    "| {indent}<?{} {}?>",
                    instruction.target, instruction.data
                )
                .unwrap();
            }
        }
    }
    output.pop();
    Ok(output)
}

fn first_difference(expected: &str, actual: &str) -> String {
    for (line, (left, right)) in expected.lines().zip(actual.lines()).enumerate() {
        if left != right {
            return format!("line {}: expected {:?}, actual {:?}", line + 1, left, right);
        }
    }
    format!(
        "line count or final newline differs (expected {} bytes, actual {} bytes)",
        expected.len(),
        actual.len()
    )
}

#[test]
#[ignore = "research oracle: pinned WPT exposes known parser/library gaps; run explicitly with --ignored"]
fn pinned_wpt_document_tree_conformance() {
    let report_only = matches!(std::env::var("ATHENA_WPT_REPORT_ONLY").as_deref(), Ok("1"));
    let mut total_cases = 0usize;
    let mut executed = 0usize;
    let mut skipped_fragments = 0usize;
    let mut skipped_script_off = 0usize;
    let mut tree_mismatches = 0usize;
    let mut tree_mismatch_cases_by_fixture: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    let mut tree_failures = BTreeMap::new();
    let mut annotation_count_deltas = 0usize;
    let mut annotation_examples = Vec::new();
    let mut case_reports = Vec::new();
    let max_nodes = ParseLimits::default().max_parser_nodes;

    for (fixture_name, fixture) in WPT {
        let cases = parse_fixture(fixture_name, fixture)
            .unwrap_or_else(|error| panic!("invalid pinned WPT fixture: {error}"));
        for (case_index, case) in cases.into_iter().enumerate() {
            total_cases += 1;
            if case.fragment_context.is_some() {
                skipped_fragments += 1;
                continue;
            }
            if case.scripting_enabled == Some(false) {
                skipped_script_off += 1;
                continue;
            }

            let (document, warnings) =
                parse_html_document_with_node_limit(&case.input, max_nodes).unwrap_or_else(
                    |error| {
                        panic!(
                            "{fixture_name} case {} {:?}: Athena parser rejected fixture input: {error}",
                            case_index + 1,
                            case.description
                        )
                    },
                );
            let actual_tree = serialize_tree(&document).unwrap_or_else(|error| {
                panic!(
                    "{fixture_name} case {} {:?}: cannot serialize parser tree: {error}",
                    case_index + 1,
                    case.description
                )
            });
            let tree_differs = actual_tree != case.expected_tree;
            let annotations_differ = warnings != case.annotated_error_count;
            case_reports.push(serde_json::json!({
                "fixture": fixture_name,
                "case": case_index + 1,
                "actual_tree_blake3": blake3::hash(actual_tree.as_bytes()).to_hex().to_string(),
                "expected_tree_blake3": blake3::hash(case.expected_tree.as_bytes()).to_hex().to_string(),
                "warnings": warnings,
                "expected_warning_count": case.annotated_error_count,
            }));
            if tree_differs {
                tree_mismatches += 1;
                tree_mismatch_cases_by_fixture
                    .entry((*fixture_name).to_owned())
                    .or_default()
                    .push(case_index + 1);
            }
            if annotations_differ {
                annotation_count_deltas += 1;
                if annotation_examples.len() < 4 {
                    annotation_examples.push(format!(
                        "{fixture_name} case {}: fixture annotation {}, Athena warnings {}",
                        case_index + 1,
                        case.annotated_error_count,
                        warnings
                    ));
                }
            }
            if tree_differs {
                tree_failures
                    .entry((*fixture_name).to_owned())
                    .or_insert_with(|| {
                        let input_preview = case.input.chars().take(120).collect::<String>();
                        let context = format!(
                            "{fixture_name} case {} input {:?}",
                            case_index + 1,
                            input_preview
                        );
                        format!(
                            "{context}: {}",
                            first_difference(&case.expected_tree, &actual_tree)
                        )
                    });
            }
            executed += 1;
        }
    }

    assert_eq!(WPT.len(), 57, "the pinned parser-only fixture set changed");
    assert!(executed > 0, "WPT oracle executed no supported cases");
    assert_eq!(
        total_cases, 1949,
        "the pinned fixture case inventory changed"
    );
    assert_eq!(executed, 1733, "document-mode case eligibility changed");
    assert_eq!(
        skipped_fragments, 206,
        "fragment-mode case inventory changed"
    );
    assert_eq!(
        skipped_script_off, 10,
        "scripting-off case inventory changed"
    );
    assert_eq!(
        total_cases,
        executed + skipped_fragments + skipped_script_off,
        "every pinned WPT case must execute or have one explicit unsupported-mode reason"
    );
    if report_only {
        let report = serde_json::json!({
            "schema": "athena-wpt-differential/1",
            "total_cases": total_cases,
            "executed": executed,
            "skipped_fragments": skipped_fragments,
            "skipped_script_off": skipped_script_off,
            "tree_mismatches": tree_mismatches,
            "annotation_count_deltas": annotation_count_deltas,
            "cases": case_reports,
        });
        println!(
            "ATHENA_WPT_REPORT_JSON={}",
            serde_json::to_string(&report).unwrap()
        );
    } else {
        assert_eq!(
            tree_mismatches,
            0,
            "pinned WPT tree mismatches: {tree_mismatches} cases by fixture (case IDs) {tree_mismatch_cases_by_fixture:?}; executed {executed}, skipped fragments {skipped_fragments}, skipped scripting-off {skipped_script_off}; WPT runner does not assert #errors/#new-errors, so annotation deltas are diagnostic only ({annotation_count_deltas} cases; examples: {}); first tree differences:\n{}",
            annotation_examples.join("; "),
            tree_failures
                .values()
                .cloned()
                .collect::<Vec<_>>()
                .join("\n")
        );
    }
    eprintln!(
        "WPT pinned corpus: {total_cases} cases, {executed} production document-mode cases, {skipped_fragments} fragment cases and {skipped_script_off} scripting-off cases excluded"
    );
}
