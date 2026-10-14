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
fn build_chunks(dom: &DomArena, max_words_per_chunk: usize) -> Vec<Chunk> {
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
            if node.tag == Tag::A
                && node.attributes_id != NONE
                && !chunks[index].links.contains(&node.attributes_id)
            {
                chunks[index].links.push(node.attributes_id);
            }
        }
    }
    for chunk in &mut chunks {
        // Token count is NOT inferred from word count. This is solely an
        // input-memory/CPU safeguard for text; true LLM budget needs tokenizer.
        chunk.text = chunk
            .text
            .split_whitespace()
            .take(max_words_per_chunk)
            .collect::<Vec<_>>()
            .join(" ");
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
    if max_nodes == 0 || max_words_per_chunk == 0 || html.len() > u32::MAX as usize {
        return Err("invalid resource budget");
    }
    let parsed = Html::parse_document(html);
    let parse_warnings = parsed.errors.len();
    let mut text = Vec::<u8>::new();
    let mut drafts = Vec::<Draft>::new();
    let mut links = Vec::<String>::new();
    let mut link_ids = HashMap::<String, u32>::new();
    let mut stack = vec![(parsed.tree.root(), 0u32)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                if skip_subtree(el.name()) {
                    visit_children = false;
                } else {
                    if drafts.len() >= max_nodes {
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
                        let value = target.as_str().to_owned();
                        link_idx = *link_ids.entry(value.clone()).or_insert_with(|| {
                            let id = links.len() as u32;
                            links.push(value);
                            id
                        });
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
                    if drafts.len() >= max_nodes {
                        return Err("max DOM nodes exceeded");
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
            let children: Vec<_> = node.children().collect();
            for child in children.into_iter().rev() {
                stack.push((child, next_parent));
            }
        }
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
    let chunks = build_chunks(&dom, max_words_per_chunk);
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
