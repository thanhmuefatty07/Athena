use athena_core_v2::{
    aae::export_jsonl_with_limit,
    arena::Tag,
    projection::{
        ParseLimits, ResearchNode, ResearchNodeKind, parse_html_document_with_node_limit,
        parse_html_with_limits,
    },
};
use athena_lexbor_adapter_research::{
    parse_and_project, parse_and_project_with_diagnostics, parse_tree,
    tables::{HtmlTableLimits, extract_tables},
};
use scraper::{Html, Node as ScraperNode};
use url::Url;

fn scraper_research_nodes(html: &str) -> Vec<ResearchNode> {
    let document = Html::parse_document(html);
    let mut nodes = Vec::new();
    let mut stack = vec![(document.tree.root(), None)];
    while let Some((node, parent)) = stack.pop() {
        let (kind, name, text, attributes) = match node.value() {
            ScraperNode::Element(element) => {
                let attributes = [
                    "id",
                    "href",
                    "rowspan",
                    "colspan",
                    "headers",
                    "scope",
                    "data-unit",
                    "abbr",
                ]
                .into_iter()
                .filter_map(|key| {
                    element
                        .attr(key)
                        .map(|value| (key.to_owned(), value.to_owned()))
                })
                .collect();
                (
                    ResearchNodeKind::Element,
                    element.name().to_owned(),
                    String::new(),
                    attributes,
                )
            }
            ScraperNode::Text(text) => (
                ResearchNodeKind::Text,
                String::new(),
                text.text.to_string(),
                Vec::new(),
            ),
            _ => (
                ResearchNodeKind::Other,
                String::new(),
                String::new(),
                Vec::new(),
            ),
        };
        let index = nodes.len();
        nodes.push(ResearchNode {
            parent,
            kind,
            name,
            text,
            attributes,
        });
        for child in node.children().rev() {
            stack.push((child, Some(index)));
        }
    }
    nodes
}

#[test]
fn lexbor_projection_preserves_literal_chunks_links_and_aae_bytes() {
    let html = concat!(
        "<!doctype html><table><tr><td>table &amp; 内容</td></tr></table>",
        "<p>A &amp; B <a href='/x'>link</a> <a href='/x'>again</a> ",
        "<a href='javascript:alert(1)'>blocked</a></p>",
        "<script>ignored()</script><template><p>hidden template text</p></template>"
    );
    let base = Url::parse("https://example.org/source").unwrap();
    let limits = ParseLimits {
        max_nodes: 512,
        max_parser_nodes: 512,
        max_words_per_chunk: 64,
        ..ParseLimits::default()
    };

    let expected = parse_html_with_limits(html, &base, limits).expect("reference parse succeeds");
    let candidate = parse_and_project(html, &base, limits).expect("Lexbor parse succeeds");

    let chunks = candidate
        .projection
        .chunks
        .iter()
        .map(|chunk| (chunk.role, chunk.text.as_str(), chunk.links.as_slice()))
        .collect::<Vec<_>>();
    assert_eq!(
        chunks,
        [
            ("table_cell", "table & 内容", &[][..]),
            ("paragraph", "A & B link again blocked", &[0][..]),
        ]
    );
    assert_eq!(candidate.projection.links, ["https://example.org/x"]);
    assert_eq!(candidate.projection.parse_warnings, 0);
    assert!(
        candidate
            .projection
            .dom
            .nodes()
            .iter()
            .all(|node| node.tag != Tag::Script)
    );
    assert!(
        candidate
            .projection
            .chunks
            .iter()
            .all(|chunk| !chunk.text.contains("hidden template text"))
    );

    let baseline = export_jsonl_with_limit(&expected, &base, 17, None, 0, 16_384).unwrap();
    let actual =
        export_jsonl_with_limit(&candidate.projection, &base, 17, None, 0, 16_384).unwrap();
    assert_eq!(
        actual, baseline,
        "complete AAE bytes differ from the Rust reference"
    );
}

#[test]
fn lexbor_projection_preserves_rust_warning_metadata_and_characterizes_native_diagnostics() {
    let base = Url::parse("https://example.org/source").unwrap();
    let limits = ParseLimits {
        max_nodes: 4096,
        max_parser_nodes: 8192,
        max_words_per_chunk: 64,
        ..ParseLimits::default()
    };
    let cases = [
        (
            "implied paragraph and list end tags",
            "<p>one<p>two<ul><li>alpha<li>beta</ul>",
            1,
            1,
        ),
        (
            "table foster parenting and implicit cells",
            "<table>lead<div>outside</div><tr><td>A<td>B</table><p>after",
            5,
            2,
        ),
        (
            "duplicate attributes and link policy",
            "<a HREF='/first' href='/second' id='one' id='two'>target</a>",
            3,
            1,
        ),
        (
            "comments raw text and template descendants",
            "<!--head--><script>hidden <p>raw</script><textarea>&amp;<b>x</b></textarea><template><div>hidden</div></template>",
            1,
            1,
        ),
        (
            "foreign content and case-sensitive source names",
            "<svg viewBox='0 0 1 1'><title>vector</title><foreignObject><p>visible</p></foreignObject></svg>",
            1,
            1,
        ),
        (
            "unicode entities null replacement and stray end tags",
            "</span><p>é &amp; 雪 \0</p>",
            4,
            4,
        ),
    ];

    for (case, html, rust_warning_count, lexbor_warning_count) in cases {
        let expected = parse_html_with_limits(html, &base, limits)
            .unwrap_or_else(|error| panic!("Rust reference rejected {case}: {error}"));
        let rust_errors = Html::parse_document(html).errors;
        let (actual, trace) = parse_and_project_with_diagnostics(html, &base, limits)
            .unwrap_or_else(|error| panic!("Lexbor rejected {case}: {error}"));
        assert_eq!(
            expected.parse_warnings, rust_warning_count,
            "Rust warning oracle changed for {case}"
        );
        assert_eq!(
            rust_errors.len(),
            rust_warning_count,
            "Rust diagnostic trace changed for {case}"
        );
        assert_eq!(
            actual
                .diagnostics
                .tree_errors
                .saturating_add(actual.diagnostics.tokenizer_errors),
            lexbor_warning_count,
            "native Lexbor diagnostic total changed for {case}"
        );
        assert_eq!(
            actual.projection.parse_warnings, rust_warning_count,
            "serialized warning count must preserve Rust compatibility for {case}"
        );
        assert!(
            !trace.truncated,
            "diagnostic trace was truncated for {case}"
        );
        assert_eq!(
            trace
                .events
                .iter()
                .filter(|event| event.source
                    == athena_lexbor_adapter_research::LexborDiagnosticSource::Tree)
                .count(),
            actual.diagnostics.tree_errors,
            "tree diagnostic trace does not match Lexbor's tree error array for {case}"
        );
        assert_eq!(
            trace
                .events
                .iter()
                .filter(|event| event.source
                    == athena_lexbor_adapter_research::LexborDiagnosticSource::Tokenizer)
                .count(),
            actual.diagnostics.tokenizer_errors,
            "tokenizer diagnostic trace does not match Lexbor's tokenizer error array for {case}"
        );
        assert!(
            trace
                .events
                .iter()
                .all(|event| !event.description.is_empty())
        );
        for event in &trace.events {
            let start = event
                .start_offset
                .expect("Lexbor diagnostic start must point into input bytes");
            let end = event
                .end_offset
                .expect("Lexbor diagnostic end must point into input bytes");
            assert!(start <= end && end <= html.len());
        }
        match case {
            "table foster parenting and implicit cells" => {
                assert_eq!(
                    trace
                        .events
                        .iter()
                        .map(|event| event.error_id)
                        .collect::<Vec<_>>(),
                    [4, 29]
                );
                assert_eq!(
                    rust_errors
                        .iter()
                        .map(|error| error.as_ref())
                        .collect::<Vec<_>>(),
                    [
                        "Unexpected token",
                        "Non-space table text",
                        "Unexpected token",
                        "Unexpected characters in table",
                        "Unexpected token",
                    ]
                );
                assert_eq!(
                    trace
                        .events
                        .iter()
                        .map(|event| event.description.as_str())
                        .collect::<Vec<_>>(),
                    [
                        "unexpected token in initial mode",
                        "characters in table text"
                    ]
                );
                assert_eq!(
                    trace
                        .events
                        .iter()
                        .map(|event| (event.start_offset, event.end_offset))
                        .collect::<Vec<_>>(),
                    [(Some(1), Some(6)), (Some(12), Some(15))]
                );
            }
            "duplicate attributes and link policy" => {
                assert_eq!(
                    trace
                        .events
                        .iter()
                        .map(|event| event.error_id)
                        .collect::<Vec<_>>(),
                    [4]
                );
                assert_eq!(
                    rust_errors
                        .iter()
                        .map(|error| error.as_ref())
                        .collect::<Vec<_>>(),
                    [
                        "Duplicate attribute",
                        "Duplicate attribute",
                        "Unexpected token",
                    ]
                );
                assert_eq!(
                    trace
                        .events
                        .iter()
                        .map(|event| event.description.as_str())
                        .collect::<Vec<_>>(),
                    ["unexpected token in initial mode"]
                );
                assert_eq!(
                    trace.events[0].source,
                    athena_lexbor_adapter_research::LexborDiagnosticSource::Tree
                );
                assert_eq!(
                    (trace.events[0].start_offset, trace.events[0].end_offset),
                    (Some(1), Some(2))
                );
            }
            _ => {}
        }
        let expected_aae = export_jsonl_with_limit(&expected, &base, 17, None, 0, 16_384).unwrap();
        let actual_aae =
            export_jsonl_with_limit(&actual.projection, &base, 17, None, 0, 16_384).unwrap();

        let expected_warning_field = format!("\"parse_warnings\":{}", expected.parse_warnings);
        let actual_warning_field =
            format!("\"parse_warnings\":{}", actual.projection.parse_warnings);
        assert!(expected_aae.contains(&expected_warning_field));
        assert!(actual_aae.contains(&actual_warning_field));
        let expected_without_warning_count =
            expected_aae.replacen(&expected_warning_field, "\"parse_warnings\":<count>", 1);
        let actual_without_warning_count =
            actual_aae.replacen(&actual_warning_field, "\"parse_warnings\":<count>", 1);
        assert_eq!(
            actual_without_warning_count, expected_without_warning_count,
            "AAE content differs apart from its parser-warning count for {case}"
        );
        assert_eq!(
            actual_aae, expected_aae,
            "complete AAE bytes must preserve the exact Rust warning count for {case}"
        );
        assert_eq!(
            actual.projection.links, expected.links,
            "resolved link set differs for {case}"
        );
    }
}

#[test]
fn lexbor_diagnostic_trace_is_bounded_without_changing_count() {
    let html = "</div>".repeat(300);
    let limits = ParseLimits::default();
    let (traced_tree, trace) =
        athena_lexbor_adapter_research::parse_tree_with_diagnostics(&html, limits)
            .expect("bounded diagnostic capture should parse the document");
    let plain_tree = parse_tree(&html, limits).expect("plain adapter parse should succeed");

    assert!(trace.truncated);
    assert_eq!(trace.events.len(), 256);
    assert!(
        traced_tree.diagnostics.tree_errors + traced_tree.diagnostics.tokenizer_errors
            > trace.events.len()
    );
    assert_eq!(traced_tree.diagnostics, plain_tree.diagnostics);
}

#[test]
fn malformed_html_reports_lexbor_tree_errors() {
    let html = "<table><tr><td>x</table>";
    let base = Url::parse("https://example.org/source").unwrap();
    let result = parse_and_project(html, &base, ParseLimits::default())
        .expect("HTML5 repair should produce a document");
    let (_, reference_warnings) =
        parse_html_document_with_node_limit(html, ParseLimits::default().max_parser_nodes)
            .expect("Rust reference parse succeeds");

    assert!(result.diagnostics.tree_errors > 0);
    assert_eq!(
        result.projection.parse_warnings,
        result
            .diagnostics
            .tree_errors
            .saturating_add(result.diagnostics.tokenizer_errors)
    );
    assert_eq!(result.projection.parse_warnings, reference_warnings);
}

#[test]
fn lexbor_candidate_observes_athena_projection_budgets() {
    let html = "<p><a href='/long'>text</a></p>";
    let base = Url::parse("https://example.org/source").unwrap();
    let defaults = ParseLimits::default();
    let tree = parse_tree(html, defaults).expect("unbounded research tree succeeds");
    let exact_parser_nodes = tree.nodes.len() + 1;
    let exact_parser_limits = ParseLimits {
        max_parser_nodes: exact_parser_nodes,
        ..defaults
    };
    let exact_parser = parse_and_project(html, &base, exact_parser_limits)
        .expect("parser-node boundary must be accepted");
    let exact_projected_nodes = exact_parser.projection.dom.nodes().len();
    let exact_projection_limits = ParseLimits {
        max_nodes: exact_projected_nodes,
        ..defaults
    };
    assert!(parse_and_project(html, &base, exact_projection_limits).is_ok());

    for (limits, expected_error) in [
        (
            ParseLimits {
                max_parser_nodes: exact_parser_nodes - 1,
                ..defaults
            },
            "max DOM nodes exceeded",
        ),
        (
            ParseLimits {
                max_nodes: exact_projected_nodes - 1,
                ..defaults
            },
            "max DOM nodes exceeded",
        ),
        (
            ParseLimits {
                max_link_url_bytes: 4,
                ..defaults
            },
            "resolved link byte budget exceeded",
        ),
    ] {
        let error = parse_and_project(html, &base, limits)
            .err()
            .expect("research budget must reject this input");
        assert_eq!(error, expected_error);
    }
}

#[test]
fn lexbor_table_nodes_preserve_groups_spans_headers_and_nested_ownership() {
    let html = concat!(
        "<!doctype html><table id='outer'><caption>Metrics &amp; Units</caption>",
        "<thead><tr><th id='quantity' scope='col' abbr='Q'>Quantity</th>",
        "<th id='value'>Value</th></tr></thead>",
        "<tbody><tr><td id='metric' headers='quantity value' rowspan='0' colspan='2' ",
        "data-unit='kg'> A <b>10</b><table id='inner'><tr><td id='nested'>nested</td>",
        "</tr></table></td></tr></tbody>",
        "<tbody><tr><td>next</td></tr></tbody></table>"
    );
    let candidate_tree = parse_tree(html, ParseLimits::default()).expect("Lexbor tree succeeds");
    let reference_nodes = scraper_research_nodes(html);
    let candidate = extract_tables(
        html.len(),
        &candidate_tree.nodes,
        HtmlTableLimits::default(),
    )
    .expect("Lexbor table extraction succeeds");
    let reference = extract_tables(html.len(), &reference_nodes, HtmlTableLimits::default())
        .expect("Rust reference table extraction succeeds");

    assert_eq!(
        candidate, reference,
        "complete table consumer output differs"
    );
    assert_eq!(candidate.len(), 2);
    let outer = &candidate[0];
    assert_eq!(outer.id.as_deref(), Some("outer"));
    assert_eq!(outer.caption.as_deref(), Some("Metrics & Units"));
    assert_eq!(
        outer
            .rows
            .iter()
            .map(|row| row.group_type.as_str())
            .collect::<Vec<_>>(),
        ["thead", "tbody", "tbody"]
    );
    let header = &outer.rows[0].cells[0];
    assert_eq!(
        (header.tag.as_str(), header.text.as_str()),
        ("th", "Quantity")
    );
    assert_eq!(header.scope.as_deref(), Some("col"));
    assert_eq!(header.abbr.as_deref(), Some("Q"));
    let metric = &outer.rows[1].cells[0];
    assert_eq!(metric.text, "A 10 nested");
    assert_eq!((metric.rowspan, metric.colspan), (0, 2));
    assert_eq!(metric.headers, ["quantity", "value"]);
    assert_eq!(metric.unit.as_deref(), Some("kg"));
    assert_eq!(metric.id.as_deref(), Some("metric"));
    assert_eq!(metric.locator, "table[0]/row[1]/cell[0]");
    assert_eq!(
        outer.grid.slots,
        vec![
            vec![Some(0), Some(1)],
            vec![Some(2), Some(2)],
            vec![Some(3), None],
        ]
    );
    assert_eq!(candidate[1].id.as_deref(), Some("inner"));
    assert_eq!(candidate[1].rows[0].group_type, "tbody");
    assert_eq!(candidate[1].rows[0].cells[0].text, "nested");
}
