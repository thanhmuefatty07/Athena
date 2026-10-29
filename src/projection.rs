//! Project standards-parsed HTML into the compact 32-byte-per-node arena.
//! We explicitly COPY decoded text: HTML entities and reparenting make
//! alleged 'zero-copy from HTTP bytes' incorrect for general HTML.
use crate::arena::{DomArena, NONE, NodeId, Tag};
use bytes::Bytes;
use html5ever::{
    Attribute, LocalName, Namespace, QualName,
    driver::{self, ParseOpts},
    tendril::{StrTendril, stream::TendrilSink},
    tree_builder::{ElemName, ElementFlags, NodeOrText, QuirksMode, TreeSink},
};
use scraper::{Html, HtmlTreeSink, Node as HtmlNode};
use std::borrow::Cow;
use std::cell::{Cell, Ref, RefCell};
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
    pub max_parser_nodes: usize,
    pub max_words_per_chunk: usize,
    pub max_link_bytes: usize,
    pub max_link_url_bytes: usize,
}
impl Default for ParseLimits {
    fn default() -> Self {
        Self {
            max_html_bytes: DEFAULT_MAX_HTML_BYTES,
            max_text_bytes: 4 * 1024 * 1024,
            max_nodes: 250_000,
            max_parser_nodes: 250_000,
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

struct ProjectionBuilder<'a> {
    base: &'a Url,
    limits: ParseLimits,
    text: Vec<u8>,
    drafts: Vec<Draft>,
    link_ids: HashMap<String, u32>,
    link_bytes: usize,
}

impl<'a> ProjectionBuilder<'a> {
    fn new(base: &'a Url, limits: ParseLimits) -> Self {
        Self {
            base,
            limits,
            text: Vec::new(),
            drafts: Vec::new(),
            link_ids: HashMap::new(),
            link_bytes: 0,
        }
    }

    fn add_element(
        &mut self,
        parent: NodeId,
        name: &str,
        href: Option<&str>,
    ) -> Result<NodeId, &'static str> {
        if self.drafts.len() >= self.limits.max_nodes - 1 {
            return Err("max DOM nodes exceeded");
        }
        let mut link_idx = NONE;
        if name == "a"
            && let Some(href) = href
            && let Ok(target) = self.base.join(href)
            && target.scheme() == "https"
            && target.username().is_empty()
            && target.password().is_none()
        {
            if let Some(&id) = self.link_ids.get(target.as_str()) {
                link_idx = id;
            } else {
                if target.as_str().len() > self.limits.max_link_url_bytes
                    || target.as_str().len()
                        > self.limits.max_link_bytes.saturating_sub(self.link_bytes)
                {
                    return Err("resolved link byte budget exceeded");
                }
                self.link_bytes += target.as_str().len();
                link_idx = u32::try_from(self.link_ids.len()).map_err(|_| "link ID overflow")?;
                self.link_ids.insert(target.into(), link_idx);
            }
        }
        let id = u32::try_from(self.drafts.len() + 1).map_err(|_| "node ID overflow")?;
        self.drafts.push(Draft {
            parent,
            tag: tag(name),
            range: None,
            link_idx,
        });
        Ok(id)
    }

    fn add_text(&mut self, parent: NodeId, value: &str) -> Result<(), &'static str> {
        if value.is_empty() {
            return Ok(());
        }
        if self.drafts.len() >= self.limits.max_nodes - 1 {
            return Err("max DOM nodes exceeded");
        }
        if value.len() > self.limits.max_text_bytes.saturating_sub(self.text.len()) {
            return Err("decoded text byte budget exceeded");
        }
        let start = self.text.len();
        self.text.extend_from_slice(value.as_bytes());
        self.drafts.push(Draft {
            parent,
            tag: Tag::Text,
            range: Some((start, value.len())),
            link_idx: NONE,
        });
        Ok(())
    }

    fn finish(self, parse_warnings: usize) -> Result<Projection, &'static str> {
        let mut links = vec![String::new(); self.link_ids.len()];
        for (value, id) in self.link_ids {
            links[id as usize] = value;
        }
        let mut dom = DomArena::new(Bytes::from(self.text), self.drafts.len() + 1);
        for (i, draft) in self.drafts.into_iter().enumerate() {
            let idx = dom
                .append_child(draft.parent, draft.tag, draft.range)
                .map_err(|_| "DOM arena capacity/depth/range error")?;
            debug_assert_eq!(idx as usize, i + 1);
            if draft.link_idx != NONE {
                dom.set_attribute_id(idx, draft.link_idx)
                    .map_err(|_| "invalid link index")?;
            }
        }
        let chunks = build_chunks(&dom, self.limits.max_words_per_chunk, links.len());
        Ok(Projection {
            dom,
            chunks,
            links,
            parse_warnings,
        })
    }
}

#[cfg(feature = "research-probes")]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResearchNodeKind {
    Element,
    Text,
    Other,
}

#[cfg(feature = "research-probes")]
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResearchNode {
    /// Index of the parent in the parser's preorder event list. The parser
    /// document root is represented by `None` and is not a projected node.
    pub parent: Option<usize>,
    pub kind: ResearchNodeKind,
    pub name: String,
    pub text: String,
    pub attributes: Vec<(String, String)>,
}

#[cfg(feature = "research-probes")]
impl ResearchNode {
    pub fn attr(&self, name: &str) -> Option<&str> {
        self.attributes
            .iter()
            .find(|(attribute, _)| attribute == name)
            .map(|(_, value)| value.as_str())
    }
}

const DEFAULT_MAX_HTML_BYTES: usize = 4 * 1024 * 1024;
const HTML_PARSER_INPUT_CHUNK_BYTES: usize = 16 * 1024;

type HtmlHandle = <HtmlTreeSink as TreeSink>::Handle;

#[derive(Clone)]
enum BoundedHandle {
    Tree(HtmlHandle),
    Discarded(u64),
}

#[derive(Debug)]
struct BoundedElemName<'a>(Ref<'a, QualName>);

impl ElemName for BoundedElemName<'_> {
    fn ns(&self) -> &Namespace {
        &self.0.ns
    }

    fn local_name(&self) -> &LocalName {
        &self.0.local
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ParserNodeLimitExceeded {
    limit: usize,
    allocated: usize,
    requested: usize,
    processed_bytes: usize,
}

struct BoundedParseOutput {
    document: Html,
    parse_warnings: usize,
    node_limit_error: Option<ParserNodeLimitExceeded>,
}

struct BoundedTreeSink {
    inner: HtmlTreeSink,
    max_nodes: usize,
    allocated_nodes: Cell<usize>,
    node_limit_error: Cell<Option<ParserNodeLimitExceeded>>,
    parse_warnings: Cell<usize>,
    next_discarded_id: Cell<u64>,
    discarded_name: QualName,
    discarded_names: RefCell<HashMap<u64, QualName>>,
}

impl BoundedTreeSink {
    fn new(max_nodes: usize) -> Self {
        Self {
            inner: HtmlTreeSink::new(Html::new_document()),
            max_nodes,
            allocated_nodes: Cell::new(1),
            node_limit_error: Cell::new(None),
            parse_warnings: Cell::new(0),
            next_discarded_id: Cell::new(0),
            discarded_name: QualName::new(
                None,
                html5ever::ns!(html),
                html5ever::local_name!("div"),
            ),
            discarded_names: RefCell::new(HashMap::new()),
        }
    }

    fn reserve_nodes(&self, requested: usize) -> bool {
        if self.node_limit_error.get().is_some() {
            return false;
        }
        let allocated = self.allocated_nodes.get();
        if requested > self.max_nodes.saturating_sub(allocated) {
            self.node_limit_error.set(Some(ParserNodeLimitExceeded {
                limit: self.max_nodes,
                allocated,
                requested,
                processed_bytes: 0,
            }));
            return false;
        }
        self.allocated_nodes.set(allocated + requested);
        true
    }

    fn append_text_allocates(&self, parent: &HtmlHandle) -> bool {
        let document = self.inner.0.borrow();
        document
            .tree
            .get(*parent)
            .and_then(|node| node.last_child())
            .is_none_or(|node| !node.value().is_text())
    }

    fn append_text_before_allocates(&self, sibling: &HtmlHandle) -> bool {
        let document = self.inner.0.borrow();
        let sibling = document.tree.get(*sibling).expect("parser handle exists");
        sibling.parent().is_some()
            && sibling
                .prev_sibling()
                .is_none_or(|node| !node.value().is_text())
    }

    fn has_node_limit_error(&self) -> bool {
        self.node_limit_error.get().is_some()
    }

    fn discarded_handle(&self) -> BoundedHandle {
        self.discarded_named_handle(self.discarded_name.clone())
    }

    fn discarded_named_handle(&self, name: QualName) -> BoundedHandle {
        let id = self.next_discarded_id.get();
        self.next_discarded_id.set(id.saturating_add(1));
        self.discarded_names.borrow_mut().insert(id, name);
        BoundedHandle::Discarded(id)
    }
}

impl TreeSink for BoundedTreeSink {
    type Output = BoundedParseOutput;
    type Handle = BoundedHandle;
    type ElemName<'a>
        = BoundedElemName<'a>
    where
        Self: 'a;

    fn finish(self) -> Self::Output {
        BoundedParseOutput {
            document: self.inner.finish(),
            parse_warnings: self.parse_warnings.get(),
            node_limit_error: self.node_limit_error.get(),
        }
    }

    fn parse_error(&self, _message: Cow<'static, str>) {
        if !self.has_node_limit_error() {
            self.parse_warnings
                .set(self.parse_warnings.get().saturating_add(1));
        }
    }

    fn get_document(&self) -> Self::Handle {
        BoundedHandle::Tree(self.inner.get_document())
    }

    fn elem_name<'a>(&'a self, target: &'a Self::Handle) -> Self::ElemName<'a> {
        match target {
            BoundedHandle::Tree(handle) => BoundedElemName(self.inner.elem_name(handle)),
            BoundedHandle::Discarded(id) => {
                BoundedElemName(Ref::map(self.discarded_names.borrow(), |names| {
                    names.get(id).expect("discarded parser handle exists")
                }))
            }
        }
    }

    fn create_element(
        &self,
        name: QualName,
        attrs: Vec<Attribute>,
        flags: ElementFlags,
    ) -> Self::Handle {
        if self.has_node_limit_error() {
            return self.discarded_named_handle(name);
        }
        let template_nodes = usize::from(flags.template) + 1;
        if !self.reserve_nodes(template_nodes) {
            return self.discarded_named_handle(name);
        }
        BoundedHandle::Tree(self.inner.create_element(name, attrs, flags))
    }

    fn create_comment(&self, text: StrTendril) -> Self::Handle {
        if !self.reserve_nodes(1) {
            return self.discarded_handle();
        }
        BoundedHandle::Tree(self.inner.create_comment(text))
    }

    fn create_pi(&self, target: StrTendril, data: StrTendril) -> Self::Handle {
        if !self.reserve_nodes(1) {
            return self.discarded_handle();
        }
        BoundedHandle::Tree(self.inner.create_pi(target, data))
    }

    fn append(&self, parent: &Self::Handle, child: NodeOrText<Self::Handle>) {
        if self.has_node_limit_error() {
            return;
        }
        let BoundedHandle::Tree(parent) = parent else {
            return;
        };
        match child {
            NodeOrText::AppendNode(BoundedHandle::Tree(child)) => {
                self.inner.append(parent, NodeOrText::AppendNode(child));
            }
            NodeOrText::AppendNode(BoundedHandle::Discarded(_)) => {}
            NodeOrText::AppendText(text) => {
                if self.append_text_allocates(parent) && !self.reserve_nodes(1) {
                    return;
                }
                self.inner.append(parent, NodeOrText::AppendText(text));
            }
        }
    }

    fn append_based_on_parent_node(
        &self,
        element: &Self::Handle,
        previous_element: &Self::Handle,
        child: NodeOrText<Self::Handle>,
    ) {
        if self.has_node_limit_error() {
            return;
        }
        let (BoundedHandle::Tree(element_id), BoundedHandle::Tree(_previous_element_id)) =
            (element, previous_element)
        else {
            return;
        };
        let has_parent = {
            self.inner
                .0
                .borrow()
                .tree
                .get(*element_id)
                .expect("parser handle exists")
                .parent()
                .is_some()
        };
        if has_parent {
            self.append_before_sibling(element, child);
        } else {
            self.append(previous_element, child);
        }
    }

    fn append_doctype_to_document(
        &self,
        name: StrTendril,
        public_id: StrTendril,
        system_id: StrTendril,
    ) {
        if self.reserve_nodes(1) {
            self.inner
                .append_doctype_to_document(name, public_id, system_id);
        }
    }

    fn get_template_contents(&self, target: &Self::Handle) -> Self::Handle {
        if self.has_node_limit_error() {
            return self.discarded_handle();
        }
        match target {
            BoundedHandle::Tree(handle) => {
                BoundedHandle::Tree(self.inner.get_template_contents(handle))
            }
            BoundedHandle::Discarded(_) => self.discarded_handle(),
        }
    }

    fn same_node(&self, left: &Self::Handle, right: &Self::Handle) -> bool {
        match (left, right) {
            (BoundedHandle::Tree(left), BoundedHandle::Tree(right)) => {
                self.inner.same_node(left, right)
            }
            (BoundedHandle::Discarded(left), BoundedHandle::Discarded(right)) => left == right,
            _ => false,
        }
    }

    fn set_quirks_mode(&self, mode: QuirksMode) {
        if !self.has_node_limit_error() {
            self.inner.set_quirks_mode(mode);
        }
    }

    fn append_before_sibling(&self, sibling: &Self::Handle, new_node: NodeOrText<Self::Handle>) {
        if self.has_node_limit_error() {
            return;
        }
        let BoundedHandle::Tree(sibling) = sibling else {
            return;
        };
        match new_node {
            NodeOrText::AppendNode(BoundedHandle::Tree(node)) => {
                self.inner
                    .append_before_sibling(sibling, NodeOrText::AppendNode(node));
            }
            NodeOrText::AppendNode(BoundedHandle::Discarded(_)) => {}
            NodeOrText::AppendText(text) => {
                if self.append_text_before_allocates(sibling) && !self.reserve_nodes(1) {
                    return;
                }
                self.inner
                    .append_before_sibling(sibling, NodeOrText::AppendText(text));
            }
        }
    }

    fn add_attrs_if_missing(&self, target: &Self::Handle, attrs: Vec<Attribute>) {
        if self.has_node_limit_error() {
            return;
        }
        if let BoundedHandle::Tree(target) = target {
            self.inner.add_attrs_if_missing(target, attrs);
        }
    }

    fn remove_from_parent(&self, target: &Self::Handle) {
        if self.has_node_limit_error() {
            return;
        }
        if let BoundedHandle::Tree(target) = target {
            self.inner.remove_from_parent(target);
        }
    }

    fn reparent_children(&self, node: &Self::Handle, new_parent: &Self::Handle) {
        if self.has_node_limit_error() {
            return;
        }
        if let (BoundedHandle::Tree(node), BoundedHandle::Tree(new_parent)) = (node, new_parent) {
            self.inner.reparent_children(node, new_parent);
        }
    }
}

fn parse_html_document_bounded(
    html: &str,
    max_nodes: usize,
) -> Result<(Html, usize), ParserNodeLimitExceeded> {
    let mut parser = driver::parse_document(BoundedTreeSink::new(max_nodes), ParseOpts::default());
    let mut start = 0;
    while start < html.len() {
        let mut end = start
            .saturating_add(HTML_PARSER_INPUT_CHUNK_BYTES)
            .min(html.len());
        while !html.is_char_boundary(end) {
            end -= 1;
        }
        TendrilSink::process(&mut parser, StrTendril::from(&html[start..end]));
        if let Some(mut error) = parser.tokenizer.sink.sink.node_limit_error.get() {
            error.processed_bytes = end;
            return Err(error);
        }
        start = end;
    }
    let output = TendrilSink::finish(parser);
    if let Some(mut error) = output.node_limit_error {
        error.processed_bytes = html.len();
        return Err(error);
    }
    Ok((output.document, output.parse_warnings))
}

/// Parses HTML with a tree-sink node cap and the default HTML byte cap, returning parse warnings separately from `Html`.
pub fn parse_html_document_with_node_limit(
    html: &str,
    max_nodes: usize,
) -> Result<(Html, usize), &'static str> {
    if max_nodes == 0 || html.len() > DEFAULT_MAX_HTML_BYTES || html.len() > u32::MAX as usize {
        return Err("invalid resource budget");
    }
    parse_html_document_bounded(html, max_nodes).map_err(|_| "max DOM nodes exceeded")
}

#[cfg(feature = "research-probes")]
pub fn parse_warning_count_with_limits(
    html: &str,
    limits: ParseLimits,
) -> Result<usize, &'static str> {
    if limits.max_nodes == 0
        || limits.max_parser_nodes == 0
        || limits.max_words_per_chunk == 0
        || html.len() > limits.max_html_bytes
        || html.len() > u32::MAX as usize
    {
        return Err("invalid resource budget");
    }
    parse_html_document_bounded(html, limits.max_parser_nodes)
        .map(|(_, warning_count)| warning_count)
        .map_err(|_| "max DOM nodes exceeded")
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
        || limits.max_parser_nodes == 0
        || max_words_per_chunk == 0
        || html.len() > limits.max_html_bytes
        || html.len() > u32::MAX as usize
    {
        return Err("invalid resource budget");
    }
    let (parsed, parse_warnings) = parse_html_document_bounded(html, limits.max_parser_nodes)
        .map_err(|_| "max DOM nodes exceeded")?;
    let mut builder = ProjectionBuilder::new(base, limits);
    let mut stack = vec![(parsed.tree.root(), 0u32)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                if skip_subtree(el.name()) {
                    visit_children = false;
                } else {
                    next_parent = builder.add_element(parent, el.name(), el.attr("href"))?;
                }
            }
            HtmlNode::Text(content) => {
                builder.add_text(parent, content.text.as_ref())?;
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
    builder.finish(parse_warnings)
}

/// Apply Athena's shared projection rules to a foreign parser's preorder tree.
/// This feature-gated entry point exists only for controlled parser comparisons.
#[cfg(feature = "research-probes")]
pub fn project_research_nodes(
    html_len: usize,
    nodes: &[ResearchNode],
    base: &Url,
    limits: ParseLimits,
    parse_warnings: usize,
) -> Result<Projection, &'static str> {
    if limits.max_nodes == 0
        || limits.max_parser_nodes == 0
        || limits.max_words_per_chunk == 0
        || html_len > limits.max_html_bytes
        || html_len > u32::MAX as usize
    {
        return Err("invalid resource budget");
    }
    let parser_node_count = nodes.len().checked_add(1).ok_or("max DOM nodes exceeded")?;
    if parser_node_count > limits.max_parser_nodes {
        return Err("max DOM nodes exceeded");
    }

    let mut builder = ProjectionBuilder::new(base, limits);
    let mut projected_ids = vec![0u32; nodes.len()];
    let mut suppressed = vec![false; nodes.len()];
    for (index, node) in nodes.iter().enumerate() {
        let parent_index = match node.parent {
            Some(parent) if parent < index => Some(parent),
            Some(_) => return Err("invalid research parser tree"),
            None => None,
        };
        let parent_id = parent_index.map_or(0, |parent| projected_ids[parent]);
        if parent_index.is_some_and(|parent| suppressed[parent]) {
            suppressed[index] = true;
            continue;
        }

        match node.kind {
            ResearchNodeKind::Element => {
                if skip_subtree(&node.name) {
                    suppressed[index] = true;
                } else {
                    projected_ids[index] =
                        builder.add_element(parent_id, &node.name, node.attr("href"))?;
                }
            }
            ResearchNodeKind::Text => {
                builder.add_text(parent_id, &node.text)?;
                projected_ids[index] = parent_id;
            }
            ResearchNodeKind::Other => projected_ids[index] = parent_id,
        }
    }
    builder.finish(parse_warnings)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn public_bounded_parser_rejects_html_over_the_default_byte_budget() {
        let html = "x".repeat(ParseLimits::default().max_html_bytes + 1);
        assert!(matches!(
            parse_html_document_with_node_limit(&html, 16),
            Err("invalid resource budget")
        ));
    }

    #[cfg(feature = "research-probes")]
    #[test]
    fn research_warning_counter_preserves_rust_counts_on_known_divergences() {
        let limits = ParseLimits::default();
        let base = Url::parse("https://example.org/source").unwrap();
        for (html, expected_count) in [
            (
                "<table>lead<div>outside</div><tr><td>A<td>B</table><p>after",
                5,
            ),
            (
                "<a HREF='/first' href='/second' id='one' id='two'>target</a>",
                3,
            ),
        ] {
            let expected = parse_html_with_limits(html, &base, limits).unwrap();
            assert_eq!(expected.parse_warnings, expected_count);
            assert_eq!(
                parse_warning_count_with_limits(html, limits),
                Ok(expected_count)
            );
        }
    }

    #[test]
    fn bounded_parser_matches_scraper_across_utf8_chunk_boundary() {
        let opening = "<table><tr><td>";
        let prefix_len = HTML_PARSER_INPUT_CHUNK_BYTES - opening.len() - 1;
        let html = format!(
            "{opening}{}é</td></tr></table><template><p>x</p></template><!--tail>",
            "x".repeat(prefix_len)
        );
        let baseline = Html::parse_document(&html);
        let (candidate, warnings) =
            parse_html_document_with_node_limit(&html, baseline.tree.nodes().len()).unwrap();
        assert_eq!(candidate.html(), baseline.html());
        assert_eq!(warnings, baseline.errors.len());
        assert_eq!(candidate.tree.nodes().len(), baseline.tree.nodes().len());
    }

    #[test]
    fn parser_node_limit_covers_templates_text_and_doctype_at_the_boundary() {
        let html = "<!doctype html><!--comment--><template><p>a<b>b</b>c</p></template><p>x</p>";
        let baseline = Html::parse_document(html);
        let exact = baseline.tree.nodes().len();
        let (candidate, _) = parse_html_document_with_node_limit(html, exact).unwrap();
        assert_eq!(candidate.tree.nodes().len(), exact);
        assert_eq!(candidate.html(), baseline.html());
        assert_eq!(
            parse_html_document_with_node_limit(html, exact - 1),
            Err("max DOM nodes exceeded")
        );
    }

    #[test]
    fn parser_node_limit_stops_before_consuming_the_remaining_input() {
        let html = "<div><p>x</p></div>".repeat(16_384);
        let error = parse_html_document_bounded(&html, 16).unwrap_err();
        assert_eq!(error.limit, 16);
        assert!(error.allocated <= error.limit);
        assert!(error.requested > error.limit - error.allocated);
        assert!(error.processed_bytes < html.len());
    }

    #[test]
    fn parser_node_limit_discards_repair_and_template_callbacks_without_panicking() {
        let html = concat!(
            "<!doctype html><table><tr><td>x</td></tr>",
            "<template><section><p>template</p></section></template>",
            "<table><b>fostered</b><tr><td>repair</td></tr></table>",
            "<svg><foreignObject><p>foreign</p></foreignObject></svg>",
            "<a><b><i>adoption</a>repair</i><!--tail-->",
            "<div>"
        );
        let error = parse_html_document_bounded(html, 8).unwrap_err();
        assert_eq!(error.limit, 8);
        assert!(error.allocated <= error.limit);
        assert!(error.processed_bytes <= html.len());

        let template_html = concat!(
            "<!doctype html><html><head><template><p>x</p>",
            "<table><tr><td>repair</td></tr></table></template></head>"
        );
        let template_error = parse_html_document_bounded(template_html, 6).unwrap_err();
        assert_eq!(template_error.limit, 6);
        assert!(template_error.allocated <= template_error.limit);
        assert!(template_error.processed_bytes <= template_html.len());
    }

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
    fn parser_tree_budget_is_independent_of_projected_arena_budget() {
        let base = Url::parse("https://example.org/").unwrap();
        let html = "<p>x</p>";
        let parser_nodes = Html::parse_document(html).tree.nodes().len();
        let limits = ParseLimits {
            max_nodes: 100,
            max_parser_nodes: parser_nodes,
            ..ParseLimits::default()
        };
        assert!(parse_html_with_limits(html, &base, limits).is_ok());
        assert!(matches!(
            parse_html_with_limits(
                html,
                &base,
                ParseLimits {
                    max_parser_nodes: parser_nodes - 1,
                    ..limits
                }
            ),
            Err("max DOM nodes exceeded")
        ));
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
