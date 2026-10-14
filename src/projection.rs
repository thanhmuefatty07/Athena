//! Project standards-parsed HTML into the compact 32-byte-per-node arena.
//! We explicitly COPY decoded text: HTML entities and reparenting make
//! alleged 'zero-copy from HTTP bytes' incorrect for general HTML.
use crate::arena::{DomArena, NONE, NodeId, Tag};
use bytes::Bytes;
use scraper::{Html, Node as HtmlNode};
use std::collections::HashMap;
use url::Url;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chunk {
    pub node: NodeId,
    pub role: &'static str,
    pub text: String,
    pub links: Vec<u32>,
}
pub struct Projection {
    pub dom: DomArena,
    pub chunks: Vec<Chunk>,
    pub links: Vec<String>,
    pub parse_warnings: usize,
}

#[derive(Debug, Clone, Copy)]
pub struct ParseLimits {
    pub max_html_bytes: usize,
    pub max_text_bytes: usize,
    pub max_nodes: usize,
    pub max_words_per_chunk: usize,
    pub max_link_bytes: usize,
    pub max_link_url_bytes: usize,
}
impl Default for ParseLimits {
    fn default() -> Self {
        Self {
            max_html_bytes: 4 * 1024 * 1024,
            max_text_bytes: 4 * 1024 * 1024,
            max_nodes: 250_000,
            max_words_per_chunk: 1200,
            max_link_bytes: 4 * 1024 * 1024,
            max_link_url_bytes: 8192,
        }
    }
}
struct Draft {
    parent: NodeId,
    tag: Tag,
    range: Option<(usize, usize)>,
    link_idx: u32,
}
fn tag(name: &str) -> Tag {
    match name {
        "main" => Tag::Main,
        "article" => Tag::Article,
        "section" => Tag::Section,
        "div" => Tag::Div,
        "p" => Tag::P,
        "h1" => Tag::H1,
        "h2" => Tag::H2,
        "h3" => Tag::H3,
        "h4" => Tag::H4,
        "table" => Tag::Table,
        "thead" => Tag::Thead,
        "tbody" => Tag::Tbody,
        "tfoot" => Tag::Tfoot,
        "tr" => Tag::Tr,
        "th" => Tag::Th,
        "td" => Tag::Td,
        "ul" => Tag::Ul,
        "ol" => Tag::Ol,
        "li" => Tag::Li,
        "a" => Tag::A,
        "pre" => Tag::Pre,
        "code" => Tag::Code,
        "nav" => Tag::Nav,
        "footer" => Tag::Footer,
        "header" => Tag::Header,
        "aside" => Tag::Aside,
        "form" => Tag::Form,
        "script" => Tag::Script,
        "style" => Tag::Style,
        _ => Tag::Unknown,
    }
}
fn role(tag: Tag) -> Option<&'static str> {
    match tag {
        Tag::H1 | Tag::H2 | Tag::H3 | Tag::H4 => Some("heading"),
        Tag::P => Some("paragraph"),
        Tag::Li => Some("list_item"),
        Tag::Pre => Some("preformatted"),
        Tag::Th => Some("table_header"),
        Tag::Td => Some("table_cell"),
        _ => None,
    }
}
fn skip_subtree(name: &str) -> bool {
    matches!(
        name,
        "script" | "style" | "template" | "noscript" | "svg" | "canvas" | "iframe"
    )
}
// Query-neutral chunk assignment in one preorder pass after HTML5 parsing.
// Every decoded text node belongs to its NEAREST content-bearing ancestor;
// nested list/table components are not repeatedly flattened (avoids O(N^2)
// aggregation on deeply nested lists).
fn build_chunks(dom: &DomArena, max_words_per_chunk: usize, link_count: usize) -> Vec<Chunk> {
    let mut chunk_owner: Vec<Option<usize>> = vec![None; dom.nodes().len()];
    let mut chunks = Vec::<Chunk>::new();
    for (id, node) in dom.nodes().iter().enumerate() {
        let inherited = if node.parent == NONE {
            None
        } else {
            chunk_owner[node.parent as usize]
        };
        let owner = if let Some(kind) = role(node.tag) {
            let index = chunks.len();
            chunks.push(Chunk {
                node: id as u32,
                role: kind,
                text: String::new(),
                links: Vec::new(),
            });
            Some(index)
        } else {
            inherited
        };
        chunk_owner[id] = owner;
        if let Some(index) = owner {
            if node.tag == Tag::Text
                && let Ok(segment) = dom.raw_text(id as u32)
            {
                chunks[index].text.push_str(segment);
            }
            if node.tag == Tag::A && node.attributes_id != NONE {
                chunks[index].links.push(node.attributes_id);
            }
        }
    }
    // Finish each owner's list after traversal; nested owners can revisit a link.
    // Node zero is the arena root and never a content owner.
    let mut last_owner = vec![0u32; link_count];
    for chunk in &mut chunks {
        chunk.links.retain(|&id| {
            let previous = &mut last_owner[id as usize];
            if *previous == chunk.node {
                return false;
            }
            *previous = chunk.node;
            true
        });
        // Token count is NOT inferred from word count. This is solely an
        // input-memory/CPU safeguard for text; true LLM budget needs tokenizer.
        if chunk.role == "preformatted" {
            if let Some(excluded) = chunk.text.split_whitespace().nth(max_words_per_chunk) {
                let start = excluded.as_ptr() as usize - chunk.text.as_ptr() as usize;
                let end = chunk.text[..start].trim_end().len();
                chunk.text.truncate(end);
            }
        } else {
            let mut normalized = String::new();
            for word in chunk.text.split_whitespace().take(max_words_per_chunk) {
                if !normalized.is_empty() {
                    normalized.push(' ');
                }
                normalized.push_str(word);
            }
            chunk.text = normalized;
        }
    }
    chunks.retain(|c| !c.text.is_empty());
    chunks
}

pub fn parse_html(
    html: &str,
    base: &Url,
    max_nodes: usize,
    max_words_per_chunk: usize,
) -> Result<Projection, &'static str> {
    parse_html_with_limits(
        html,
        base,
        ParseLimits {
            max_nodes,
            max_words_per_chunk,
            ..ParseLimits::default()
        },
    )
}

/// Text bytes count decoded UTF-8; link bytes count unique resolved URLs.
/// max_nodes includes the arena root.
pub fn parse_html_with_limits(
    html: &str,
    base: &Url,
    limits: ParseLimits,
) -> Result<Projection, &'static str> {
    let ParseLimits {
        max_nodes,
        max_words_per_chunk,
        ..
    } = limits;
    if max_nodes == 0
        || max_words_per_chunk == 0
        || html.len() > limits.max_html_bytes
        || html.len() > u32::MAX as usize
    {
        return Err("invalid resource budget");
    }
    let parsed = Html::parse_document(html);
    let parse_warnings = parsed.errors.len();
    let mut text = Vec::<u8>::new();
    let mut drafts = Vec::<Draft>::new();
    let mut link_ids = HashMap::<String, u32>::new();
    let mut link_bytes = 0usize;
    let mut stack = vec![(parsed.tree.root(), 0u32)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                if skip_subtree(el.name()) {
                    visit_children = false;
                } else {
                    if drafts.len() >= max_nodes - 1 {
                        return Err("max DOM nodes exceeded");
                    }
                    let mut link_idx = NONE;
                    if el.name() == "a"
                        && let Some(href) = el.attr("href")
                        && let Ok(target) = base.join(href)
                        && target.scheme() == "https"
                        && target.username().is_empty()
                        && target.password().is_none()
                    {
                        if let Some(&id) = link_ids.get(target.as_str()) {
                            link_idx = id;
                        } else {
                            if target.as_str().len() > limits.max_link_url_bytes
                                || target.as_str().len()
                                    > limits.max_link_bytes.saturating_sub(link_bytes)
                            {
                                return Err("resolved link byte budget exceeded");
                            }
                            link_bytes += target.as_str().len();
                            link_idx =
                                u32::try_from(link_ids.len()).map_err(|_| "link ID overflow")?;
                            link_ids.insert(target.into(), link_idx);
                        }
                    }
                    next_parent =
                        u32::try_from(drafts.len() + 1).map_err(|_| "node ID overflow")?;
                    drafts.push(Draft {
                        parent,
                        tag: tag(el.name()),
                        range: None,
                        link_idx,
                    });
                }
            }
            HtmlNode::Text(content) => {
                if !content.is_empty() {
                    if drafts.len() >= max_nodes - 1 {
                        return Err("max DOM nodes exceeded");
                    }
                    if content.text.len() > limits.max_text_bytes.saturating_sub(text.len()) {
                        return Err("decoded text byte budget exceeded");
                    }
                    let start = text.len();
                    text.extend_from_slice(content.text.as_bytes());
                    drafts.push(Draft {
                        parent,
                        tag: Tag::Text,
                        range: Some((start, content.text.len())),
                        link_idx: NONE,
                    });
                }
                visit_children = false;
            }
            _ => {}
        }
        if visit_children {
            // Stack-based traversal prevents source-controlled call-stack recursion.
            for child in node.children().rev() {
                stack.push((child, next_parent));
            }
        }
    }
    let mut links = vec![String::new(); link_ids.len()];
    for (value, id) in link_ids {
        links[id as usize] = value;
    }
    let mut dom = DomArena::new(Bytes::from(text), drafts.len() + 1);
    for (i, draft) in drafts.into_iter().enumerate() {
        let idx = dom
            .append_child(draft.parent, draft.tag, draft.range)
            .map_err(|_| "DOM arena capacity/depth/range error")?;
        debug_assert_eq!(idx as usize, i + 1);
        if draft.link_idx != NONE {
            dom.set_attribute_id(idx, draft.link_idx)
                .map_err(|_| "invalid link index")?;
        }
    }
    let chunks = build_chunks(&dom, max_words_per_chunk, links.len());
    Ok(Projection {
        dom,
        chunks,
        links,
        parse_warnings,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn many_unique_links_remain_ordered_and_repeated_ids_are_removed() {
        let base = Url::parse("https://example.org/").unwrap();
        let mut html = String::from("<p>");
        for i in 0..1000 {
            html.push_str(&format!("<a href='/{i}'>x</a>"));
        }
        html.push_str("<a href='/0'>first</a><a href='/999'>last</a><a href='/1'>second</a></p>");
        let page = parse_html(&html, &base, 5000, 1200).unwrap();
        assert_eq!(page.chunks[0].links, (0..1000).collect::<Vec<_>>());
        assert_eq!(page.links.len(), 1000);
    }
    #[test]
    fn decoded_text_bytes_have_an_independent_budget() {
        let base = Url::parse("https://example.org/").unwrap();
        let limits = ParseLimits {
            max_text_bytes: 2,
            ..ParseLimits::default()
        };
        assert!(parse_html_with_limits("<p>é</p>", &base, limits).is_ok());
        assert!(
            parse_html_with_limits(
                "<p>é</p>",
                &base,
                ParseLimits {
                    max_text_bytes: 1,
                    ..limits
                }
            )
            .is_err()
        );
    }
    #[test]
    fn preformatted_text_preserves_lines_and_indentation() {
        let base = Url::parse("https://example.org/").unwrap();
        let p = parse_html(
            "<pre>if ready:\n    run()\n    stop()</pre>",
            &base,
            100,
            100,
        )
        .unwrap();
        assert_eq!(p.chunks[0].text, "if ready:\n    run()\n    stop()");
    }
    #[test]
    fn total_node_budget_includes_arena_root() {
        let base = Url::parse("https://example.org/").unwrap();
        let p = parse_html("<p>x</p>", &base, 100, 100).unwrap();
        let count = p.dom.nodes().len();
        assert!(parse_html("<p>x</p>", &base, count - 1, 100).is_err());
        assert_eq!(
            parse_html("<p>x</p>", &base, count, 100)
                .unwrap()
                .dom
                .nodes()
                .len(),
            count
        );
    }
    #[test]
    fn resolved_link_amplification_is_rejected() {
        let base = Url::parse(&format!("https://example.org/{}", "x".repeat(8000))).unwrap();
        let html: String = (0..600).map(|i| format!("<a href='#{i}'>x</a>")).collect();
        assert!(parse_html(&html, &base, 2000, 100).is_err());
    }
    #[test]
    fn explicit_byte_budgets_accept_the_boundary_and_reject_expansion() {
        let base = Url::parse("https://example.org/").unwrap();
        let html = "<p><a href='/a'>one</a><a href='/b'>two</a></p>";
        let limits = ParseLimits {
            max_html_bytes: html.len(),
            max_link_bytes: 42,
            max_link_url_bytes: 21,
            ..ParseLimits::default()
        };
        assert_eq!(
            parse_html_with_limits(html, &base, limits)
                .unwrap()
                .links
                .len(),
            2
        );
        for limits in [
            ParseLimits {
                max_html_bytes: html.len() - 1,
                ..limits
            },
            ParseLimits {
                max_link_bytes: 41,
                ..limits
            },
            ParseLimits {
                max_link_url_bytes: 20,
                ..limits
            },
        ] {
            assert!(parse_html_with_limits(html, &base, limits).is_err());
        }
    }
    #[test]
    fn link_order_and_deduplication_survive_nested_block_ownership() {
        let base = Url::parse("https://example.org/").unwrap();
        let page = parse_html("<li><a href='/a'>first</a><ul><li><a href='/a'>nested</a></li></ul><a href='/b'>second</a><a href='/a'>repeat</a></li>", &base, 100, 100).unwrap();
        assert_eq!(page.chunks[0].links, vec![0, 1]);
        assert_eq!(page.chunks[1].links, vec![0]);
    }
    #[test]
    fn whitespace_between_inline_nodes_is_preserved() {
        let base = Url::parse("https://example.org/").unwrap();
        let p = parse_html("<p>Hello<b> </b>world</p>", &base, 1000, 100).unwrap();
        assert_eq!(p.chunks[0].text, "Hello world");
    }
    #[test]
    fn entities_and_link_allowlist() {
        let base = Url::parse("https://example.org/a").unwrap();
        let p = parse_html("<main><h1>News &amp; Updates</h1><p>Hello <a href='/x'>world</a> <a href='javascript:alert(1)'>bad</a></p><script>do_not_show()</script></main>", &base, 1000, 100).unwrap();
        assert_eq!(p.chunks[0].text, "News & Updates");
        assert!(p.chunks.iter().any(|c| c.text.contains("Hello world")));
        assert_eq!(p.links, vec!["https://example.org/x"]);
        assert!(p.chunks.iter().all(|c| !c.text.contains("do_not_show")));
    }
}
