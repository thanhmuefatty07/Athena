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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum CommentUnwrapPolicy {
    #[default]
    None,
    AnnasArchiveScrollCards,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum BoilerplatePolicy {
    #[default]
    FullDom,
    MainContentV1,
    CetdEntropyV1,
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
    pub include_links: bool,
    pub comment_unwrap: CommentUnwrapPolicy,
    pub boilerplate: BoilerplatePolicy,
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
            include_links: true,
            comment_unwrap: CommentUnwrapPolicy::None,
            boilerplate: BoilerplatePolicy::FullDom,
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
        if self.limits.include_links
            && name == "a"
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
        let mut chunks = build_chunks(&dom, self.limits.max_words_per_chunk, links.len());
        apply_boilerplate_policy(&dom, &mut chunks, &mut links, self.limits.boilerplate);
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
    let html_cow = unwrap_comments_if_enabled(html, limits.comment_unwrap);
    let html = html_cow.as_ref();
    let (parsed, parse_warnings) = parse_html_document_bounded(html, limits.max_parser_nodes)
        .map_err(|_| "max DOM nodes exceeded")?;
    if !limits.include_links {
        collect_unindexed_bib_hrefs(&parsed, base);
    } else {
        crate::arena::set_pending_bib_hrefs(Vec::new());
    }
    let mut builder = ProjectionBuilder::new(base, limits);
    let mut stack = vec![(parsed.tree.root(), 0u32)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                let name = el.name();
                if skip_subtree(name) {
                    visit_children = false;
                } else {
                    let href = if limits.include_links && name == "a" {
                        el.attr("href")
                    } else {
                        None
                    };
                    next_parent = builder.add_element(parent, name, href)?;
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
                    let href = if limits.include_links && node.name == "a" {
                        node.attr("href")
                    } else {
                        None
                    };
                    projected_ids[index] = builder.add_element(parent_id, &node.name, href)?;
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

fn is_html_tag_delimiter(byte: u8) -> bool {
    matches!(byte, b'>' | b'/' | b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

fn ends_in_unclosed_html_comment(prefix: &str) -> bool {
    let bytes = prefix.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] != b'<' {
            i += 1;
            continue;
        }
        if bytes[i..].starts_with(b"<!-->") {
            i += 5;
            continue;
        }
        if bytes[i..].starts_with(b"<!--->") {
            i += 6;
            continue;
        }
        if bytes[i..].starts_with(b"<!--") {
            if let Some(rel_end) = prefix[i + 4..].find("-->") {
                i = i + 4 + rel_end + 3;
                continue;
            }
            return true;
        }
        let mut raw_close: Option<&[u8]> = None;
        for (open_tag, close_tag) in [
            (&b"<script"[..], &b"</script"[..]),
            (&b"<style"[..], &b"</style"[..]),
            (&b"<textarea"[..], &b"</textarea"[..]),
            (&b"<title"[..], &b"</title"[..]),
            (&b"<xmp"[..], &b"</xmp"[..]),
        ] {
            if i + open_tag.len() < bytes.len()
                && bytes[i..i + open_tag.len()].eq_ignore_ascii_case(open_tag)
                && is_html_tag_delimiter(bytes[i + open_tag.len()])
            {
                raw_close = Some(close_tag);
                break;
            }
        }
        if let Some(close_tag) = raw_close {
            i += close_tag.len() - 1;
            let mut quote = 0u8;
            let mut prev_slash = false;
            while i < bytes.len() {
                let b = bytes[i];
                i += 1;
                if quote != 0 {
                    if b == quote {
                        quote = 0;
                    }
                } else if b == b'"' || b == b'\'' {
                    quote = b;
                    prev_slash = false;
                } else if b == b'>' {
                    break;
                } else if b == b'/' {
                    prev_slash = true;
                } else if !b.is_ascii_whitespace() {
                    prev_slash = false;
                }
            }
            if i >= bytes.len() {
                return false;
            }
            if prev_slash {
                continue;
            }
            while i + close_tag.len() <= bytes.len() {
                if bytes[i..i + close_tag.len()].eq_ignore_ascii_case(close_tag)
                    && (i + close_tag.len() == bytes.len()
                        || is_html_tag_delimiter(bytes[i + close_tag.len()]))
                {
                    i += close_tag.len();
                    while i < bytes.len() && bytes[i] != b'>' {
                        i += 1;
                    }
                    if i < bytes.len() {
                        i += 1;
                    }
                    break;
                }
                i += 1;
            }
            continue;
        }
        if i + 1 < bytes.len() && (bytes[i + 1].is_ascii_alphabetic() || bytes[i + 1] == b'/') {
            i += 2;
            let mut quote = 0u8;
            while i < bytes.len() {
                let b = bytes[i];
                i += 1;
                if quote != 0 {
                    if b == quote {
                        quote = 0;
                    }
                } else if b == b'"' || b == b'\'' {
                    quote = b;
                } else if b == b'>' {
                    break;
                }
            }
            continue;
        }
        i += 1;
    }
    false
}

fn unwrap_comments_if_enabled(html: &str, policy: CommentUnwrapPolicy) -> Cow<'_, str> {
    if policy == CommentUnwrapPolicy::None || !html.contains("<!--") {
        return Cow::Borrowed(html);
    }
    let mut out = String::with_capacity(html.len());
    let mut cursor = 0usize;
    let mut unwrapped_any = false;
    while let Some(rel_start) = html[cursor..].find("<!--") {
        let start = cursor + rel_start;
        out.push_str(&html[cursor..start]);
        if html[start..].starts_with("<!-->") {
            out.push_str("<!-->");
            cursor = start + 5;
            continue;
        }
        if html[start..].starts_with("<!--->") {
            out.push_str("<!--->");
            cursor = start + 6;
            continue;
        }
        let inner_start = start + 4;
        if let Some(rel_end) = html[inner_start..].find("-->") {
            let inner_end = inner_start + rel_end;
            let inner = &html[inner_start..inner_end];
            let rfind_pos = inner.rfind("<!--");
            let candidate = match rfind_pos {
                Some(pos) => &inner[pos + 4..],
                None => inner,
            };
            let lower = candidate.to_ascii_lowercase();
            if lower.contains("/md5/")
                && !lower.contains("<script")
                && !lower.contains("<style")
                && !lower.contains("<iframe")
                && !lower.contains("<object")
                && !lower.contains("<embed")
                && !lower.contains("<template")
            {
                if let Some(pos) = rfind_pos {
                    out.push_str(&html[start..inner_start + pos]);
                    if ends_in_unclosed_html_comment(&html[cursor..inner_start + pos]) {
                        out.push_str("-->");
                    }
                }
                out.push_str(candidate);
                unwrapped_any = true;
            } else {
                out.push_str(&html[start..inner_end + 3]);
            }
            cursor = inner_end + 3;
        } else {
            out.push_str(&html[start..]);
            cursor = html.len();
            break;
        }
    }
    if !unwrapped_any {
        return Cow::Borrowed(html);
    }
    out.push_str(&html[cursor..]);
    Cow::Owned(out)
}

fn classify_landmark(dom: &DomArena, node_id: NodeId) -> (bool, bool, bool) {
    let nodes = dom.nodes();
    let mut cur = node_id;
    let mut nearest_decision: Option<bool> = None;
    let mut inside_main_or_article = false;
    let mut inside_nav_or_footer = false;
    while cur != NONE && (cur as usize) < nodes.len() {
        let node = &nodes[cur as usize];
        match node.tag {
            Tag::Nav | Tag::Footer => {
                inside_nav_or_footer = true;
                if nearest_decision.is_none() {
                    nearest_decision = Some(true);
                }
            }
            Tag::Header | Tag::Aside | Tag::Form => {
                if nearest_decision.is_none() {
                    nearest_decision = Some(true);
                }
            }
            Tag::Main | Tag::Article => {
                inside_main_or_article = true;
                if nearest_decision.is_none() {
                    nearest_decision = Some(false);
                }
            }
            _ => {}
        }
        cur = node.parent;
    }
    (
        nearest_decision.unwrap_or(false),
        inside_main_or_article,
        inside_nav_or_footer,
    )
}

fn shannon_byte_entropy(text: &str) -> f64 {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return 0.0;
    }
    let mut counts = [0u32; 256];
    for &b in bytes {
        counts[b as usize] += 1;
    }
    let len = bytes.len() as f64;
    let mut entropy = 0.0f64;
    for &c in &counts {
        if c > 0 {
            let p = (c as f64) / len;
            entropy -= p * p.log2();
        }
    }
    entropy
}

fn extract_md5_after_segment(lower_url: &str, segment: &str) -> Option<String> {
    let mut search_from = 0usize;
    while let Some(rel_pos) = lower_url[search_from..].find(segment) {
        let start = search_from + rel_pos + segment.len();
        let tail = &lower_url[start..];
        let md5: String = tail.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
        if md5.len() == 32 {
            return Some(md5);
        }
        search_from = start;
    }
    None
}

fn is_bibliographic_url(url: &str) -> bool {
    let u = url.to_ascii_lowercase();
    if extract_md5_after_segment(&u, "/md5/").is_some()
        || extract_md5_after_segment(&u, "/slow_download/").is_some()
        || extract_md5_after_segment(&u, "/fast_download/").is_some()
    {
        return true;
    }
    if (u.contains("/doi/") || u.contains("doi.org/")) && extract_doi_from_text(url).is_some() {
        return true;
    }
    if let Some(pos) = u.find("/isbn/")
        && parse_isbn_token(&url[pos + 6..]).is_some()
    {
        return true;
    }
    false
}

fn collect_unindexed_bib_hrefs(parsed: &Html, base: &Url) {
    let mut bib_hrefs = Vec::<(NodeId, String)>::new();
    let mut stack = vec![parsed.tree.root()];
    let mut next_id = 0u32;
    while let Some(node) = stack.pop() {
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                let name = el.name();
                if skip_subtree(name) {
                    visit_children = false;
                } else {
                    next_id = next_id.saturating_add(1);
                    if name == "a"
                        && let Some(href) = el.attr("href")
                        && is_bibliographic_url(href)
                        && bib_hrefs.len() < 256
                    {
                        let resolved = base
                            .join(href)
                            .ok()
                            .filter(|u| {
                                u.scheme() == "https"
                                    && u.username().is_empty()
                                    && u.password().is_none()
                            })
                            .map(|u| u.into())
                            .unwrap_or_else(|| href.to_owned());
                        bib_hrefs.push((next_id, resolved));
                    }
                }
            }
            HtmlNode::Text(content) => {
                if !content.text.is_empty() {
                    next_id = next_id.saturating_add(1);
                }
                visit_children = false;
            }
            _ => {}
        }
        if visit_children {
            for child in node.children().rev() {
                stack.push(child);
            }
        }
    }
    crate::arena::set_pending_bib_hrefs(bib_hrefs);
}

fn compute_dom_link_metadata(dom: &DomArena) -> (Vec<NodeId>, Vec<u32>) {
    let nodes = dom.nodes();
    let mut owner_of_node = vec![NONE; nodes.len()];
    let mut anchor_counts = vec![0u32; nodes.len()];
    for (id, node) in nodes.iter().enumerate() {
        let inherited = if node.parent == NONE {
            NONE
        } else {
            owner_of_node[node.parent as usize]
        };
        let owner = if role(node.tag).is_some() {
            id as u32
        } else {
            inherited
        };
        owner_of_node[id] = owner;
        if node.tag == Tag::A && owner != NONE {
            anchor_counts[owner as usize] = anchor_counts[owner as usize].saturating_add(1);
        }
    }
    (owner_of_node, anchor_counts)
}

fn has_bibliographic_signal(
    dom: &DomArena,
    chunk: &Chunk,
    links: &[String],
    owner_of_node: &[NodeId],
) -> bool {
    if chunk.role == "table_header" || chunk.role == "table_cell" {
        return true;
    }
    if extract_doi_from_text(&chunk.text).is_some()
        || extract_isbn_from_text(&chunk.text, None).is_some()
        || parse_32_hex_md5(&chunk.text).is_some()
    {
        return true;
    }
    let lower = chunk.text.to_ascii_lowercase();
    if let Some(pos) = lower.find("md5:") {
        let token: String = chunk.text[pos + 4..]
            .trim_start()
            .chars()
            .take_while(|c| c.is_ascii_hexdigit())
            .collect();
        if token.len() == 32 {
            return true;
        }
    }
    if chunk.links.iter().any(|&id| {
        links
            .get(id as usize)
            .is_some_and(|url| is_bibliographic_url(url))
    }) {
        return true;
    }
    dom.unindexed_bib_hrefs().iter().any(|(node_id, url)| {
        (owner_of_node.get(*node_id as usize).copied() == Some(chunk.node)
            || is_dom_ancestor_or_self(dom, *node_id, chunk.node))
            && is_bibliographic_url(url)
    })
}

fn is_boilerplate_chunk(
    dom: &DomArena,
    chunk: &Chunk,
    links: &[String],
    owner_of_node: &[NodeId],
    anchor_counts: &[u32],
    policy: BoilerplatePolicy,
) -> bool {
    let (is_chrome, inside_main_or_article, inside_nav_or_footer) =
        classify_landmark(dom, chunk.node);
    if chunk.role == "heading" {
        return inside_nav_or_footer;
    }
    if is_chrome {
        return true;
    }
    let dom_anchors = anchor_counts.get(chunk.node as usize).copied().unwrap_or(0) as usize;
    let effective_link_count = chunk.links.len().max(dom_anchors);
    let bib_signal = has_bibliographic_signal(dom, chunk, links, owner_of_node);
    if !inside_main_or_article && effective_link_count >= 3 && !bib_signal {
        let word_count = chunk.text.split_whitespace().count();
        if word_count <= effective_link_count.saturating_mul(3) {
            return true;
        }
    }
    if policy == BoilerplatePolicy::CetdEntropyV1
        && !inside_main_or_article
        && chunk.role != "heading"
        && !bib_signal
    {
        let word_count = chunk.text.split_whitespace().count();
        let entropy = shannon_byte_entropy(&chunk.text);
        let link_density = (effective_link_count as f64) / (word_count.max(1) as f64);
        let effective_entropy = entropy * (1.0 - link_density.min(0.95));
        if entropy < 3.0 {
            return true;
        }
        if effective_link_count > 0 && (link_density >= 0.25 || effective_entropy < 2.8) {
            return true;
        }
        let lower = chunk.text.to_ascii_lowercase();
        if word_count <= 24
            && effective_entropy < 4.5
            && (lower.contains("all rights reserved")
                || lower.contains("privacy policy")
                || lower.contains("terms of service")
                || lower.contains("cookie policy")
                || lower.contains("sign in | register"))
        {
            return true;
        }
    }
    false
}

fn apply_boilerplate_policy(
    dom: &DomArena,
    chunks: &mut Vec<Chunk>,
    links: &mut Vec<String>,
    policy: BoilerplatePolicy,
) {
    if policy == BoilerplatePolicy::FullDom || chunks.is_empty() {
        return;
    }
    let (owner_of_node, anchor_counts) = compute_dom_link_metadata(dom);
    let keep_count = chunks
        .iter()
        .filter(|c| !is_boilerplate_chunk(dom, c, links, &owner_of_node, &anchor_counts, policy))
        .count();
    if keep_count > 0 && keep_count < chunks.len() {
        chunks.retain(|c| {
            !is_boilerplate_chunk(dom, c, links, &owner_of_node, &anchor_counts, policy)
        });
        if !links.is_empty() {
            let mut remap = vec![NONE; links.len()];
            let mut new_links = Vec::with_capacity(links.len());
            for chunk in chunks.iter_mut() {
                for id in &mut chunk.links {
                    let old_idx = *id as usize;
                    if old_idx < links.len() {
                        if remap[old_idx] == NONE {
                            remap[old_idx] = new_links.len() as u32;
                            new_links.push(links[old_idx].clone());
                        }
                        *id = remap[old_idx];
                    }
                }
            }
            *links = new_links;
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AnnasArchiveRecord {
    pub title: String,
    pub md5: String,
    pub doi: Option<String>,
    pub isbn: Option<String>,
    pub format: Option<String>,
    pub download_links: Vec<String>,
}

fn parse_32_hex_md5(s: &str) -> Option<String> {
    let trimmed = s.trim();
    if trimmed.len() == 32 && trimmed.bytes().all(|b| b.is_ascii_hexdigit()) {
        Some(trimmed.to_ascii_lowercase())
    } else {
        None
    }
}

fn parse_isbn_token(s: &str) -> Option<String> {
    let mut rest = s.trim_start_matches([' ', '\t', ':']);
    if let Some(stripped) = rest
        .strip_prefix("-13")
        .or_else(|| rest.strip_prefix("-10"))
    {
        rest = stripped;
    } else if let Some(stripped) = rest
        .strip_prefix("13:")
        .or_else(|| rest.strip_prefix("10:"))
        .or_else(|| rest.strip_prefix("13 "))
        .or_else(|| rest.strip_prefix("10 "))
    {
        rest = stripped;
    }
    let candidate: String = rest
        .trim_start_matches([' ', '\t', ':', '-'])
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '-' || *c == 'X' || *c == 'x')
        .collect();
    let candidate = candidate.trim_end_matches('-');
    let compact: Vec<u8> = candidate.bytes().filter(|&b| b != b'-').collect();
    let valid = match compact.len() {
        13 => compact.iter().all(|b| b.is_ascii_digit()),
        10 => {
            compact[..9].iter().all(|b| b.is_ascii_digit())
                && (compact[9].is_ascii_digit() || compact[9] == b'X' || compact[9] == b'x')
        }
        _ => false,
    };
    if valid {
        Some(candidate.to_owned())
    } else {
        None
    }
}

fn extract_doi_from_text(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut search_from = 0usize;
    while let Some(rel_idx) = text[search_from..].find("10.") {
        let idx = search_from + rel_idx;
        let valid_boundary = idx == 0 || {
            let prev = bytes[idx - 1];
            !prev.is_ascii_alphanumeric() && prev != b'.'
        };
        if valid_boundary {
            let raw: String = text[idx..]
                .chars()
                .take_while(|c| !c.is_whitespace() && *c != '·')
                .collect();
            let candidate = raw.trim_end_matches(['.', ')', ']', ';', ',', '"', '\'']);
            if let Some(slash_pos) = candidate.find('/') {
                let registrant = &candidate[3..slash_pos];
                let suffix = &candidate[slash_pos + 1..];
                let primary_reg = registrant.split('.').next().unwrap_or("");
                if primary_reg.len() >= 4
                    && registrant
                        .split('.')
                        .all(|seg| !seg.is_empty() && seg.bytes().all(|b| b.is_ascii_digit()))
                    && !suffix.is_empty()
                {
                    return Some(candidate.to_owned());
                }
            }
        }
        search_from = idx + 3;
    }
    None
}

fn extract_isbn_from_text(text: &str, next_text: Option<&str>) -> Option<String> {
    let lower = text.to_ascii_lowercase();
    let mut search_from = 0usize;
    while let Some(rel_idx) = lower[search_from..].find("isbn") {
        let idx = search_from + rel_idx;
        if let Some(isbn) = parse_isbn_token(&text[idx + 4..]) {
            return Some(isbn);
        }
        search_from = idx + 4;
    }
    if lower.contains("isbn")
        && let Some(nt) = next_text
    {
        return parse_isbn_token(nt);
    }
    None
}

fn is_dom_ancestor_or_self(dom: &DomArena, anc: NodeId, mut cur: NodeId) -> bool {
    let nodes = dom.nodes();
    while cur != NONE && (cur as usize) < nodes.len() {
        if cur == anc {
            return true;
        }
        cur = nodes[cur as usize].parent;
    }
    false
}

fn dom_subtree_normalized_text(dom: &DomArena, root_id: NodeId) -> String {
    let mut raw = String::new();
    for (id, node) in dom.nodes().iter().enumerate() {
        if node.tag == Tag::Text
            && is_dom_ancestor_or_self(dom, root_id, id as u32)
            && let Ok(seg) = dom.raw_text(id as u32)
        {
            if !raw.is_empty() {
                raw.push(' ');
            }
            raw.push_str(seg);
        }
    }
    raw.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn extract_annas_archive_records(page: &Projection) -> Vec<AnnasArchiveRecord> {
    let mut discovered = Vec::<(NodeId, Option<usize>, AnnasArchiveRecord)>::new();
    for (chunk_idx, chunk) in page.chunks.iter().enumerate() {
        for &link_id in &chunk.links {
            if let Some(url) = page.links.get(link_id as usize) {
                let lower_url = url.to_ascii_lowercase();
                if let Some(md5) = extract_md5_after_segment(&lower_url, "/md5/")
                    && !discovered.iter().any(|(_, _, r)| r.md5 == md5)
                {
                    discovered.push((
                        chunk.node,
                        Some(chunk_idx),
                        AnnasArchiveRecord {
                            title: chunk.text.clone(),
                            md5,
                            doi: None,
                            isbn: None,
                            format: None,
                            download_links: vec![url.clone()],
                        },
                    ));
                }
            }
        }
    }
    // Support LinkMode::None (unindexed_bib_hrefs), outer-anchor `<a href="/md5/..."><h3>Title</h3></a>`,
    // and pure `<div>`-only Anna's Archive cards.
    let mut dom_anchor_urls = Vec::<(NodeId, String)>::new();
    for (id, node) in page.dom.nodes().iter().enumerate() {
        if node.tag == Tag::A
            && node.attributes_id != NONE
            && let Some(url) = page.links.get(node.attributes_id as usize)
        {
            dom_anchor_urls.push((id as u32, url.clone()));
        }
    }
    for (node_id, url) in page.dom.unindexed_bib_hrefs() {
        dom_anchor_urls.push((*node_id, url.clone()));
    }
    for (a_node_id, url) in &dom_anchor_urls {
        let lower_url = url.to_ascii_lowercase();
        if let Some(md5) = extract_md5_after_segment(&lower_url, "/md5/")
            && !discovered.iter().any(|(_, _, r)| r.md5 == md5)
        {
            if let Some((chunk_idx, chunk)) = page.chunks.iter().enumerate().find(|(_, c)| {
                is_dom_ancestor_or_self(&page.dom, c.node, *a_node_id)
                    || is_dom_ancestor_or_self(&page.dom, *a_node_id, c.node)
            }) {
                discovered.push((
                    chunk.node.min(*a_node_id),
                    Some(chunk_idx),
                    AnnasArchiveRecord {
                        title: chunk.text.clone(),
                        md5,
                        doi: None,
                        isbn: None,
                        format: None,
                        download_links: vec![url.clone()],
                    },
                ));
            } else {
                let title = dom_subtree_normalized_text(&page.dom, *a_node_id);
                let card_root = page
                    .dom
                    .nodes()
                    .get(*a_node_id as usize)
                    .map(|n| n.parent)
                    .filter(|&p| p != NONE)
                    .unwrap_or(*a_node_id);
                let card_text = dom_subtree_normalized_text(&page.dom, card_root);
                let doi = extract_doi_from_text(&card_text);
                let isbn = extract_isbn_from_text(&card_text, None);
                let format = ["pdf", "epub", "djvu", "mobi", "cbr", "azw3"]
                    .into_iter()
                    .find(|&fmt_token| {
                        card_text
                            .split(|c: char| !c.is_ascii_alphanumeric())
                            .any(|w| w.eq_ignore_ascii_case(fmt_token))
                    })
                    .map(str::to_owned);
                discovered.push((
                    *a_node_id,
                    None,
                    AnnasArchiveRecord {
                        title: if title.is_empty() { md5.clone() } else { title },
                        md5,
                        doi,
                        isbn,
                        format,
                        download_links: vec![url.clone()],
                    },
                ));
            }
        }
    }
    if discovered.is_empty() {
        for i in 0..page.chunks.len().saturating_sub(1) {
            if page.chunks[i].text.eq_ignore_ascii_case("md5")
                && let Some(md5) = parse_32_hex_md5(&page.chunks[i + 1].text)
            {
                let title = page
                    .chunks
                    .iter()
                    .find(|c| c.role == "heading")
                    .map(|c| c.text.clone())
                    .unwrap_or_else(|| md5.clone());
                discovered.push((
                    0,
                    Some(0),
                    AnnasArchiveRecord {
                        title,
                        md5,
                        doi: None,
                        isbn: None,
                        format: None,
                        download_links: Vec::new(),
                    },
                ));
                break;
            }
        }
    }
    discovered.sort_by_key(|(dom_order, _, _)| *dom_order);
    let chunk_starts: Vec<Option<usize>> = discovered.iter().map(|(_, cs, _)| *cs).collect();
    let mut records: Vec<AnnasArchiveRecord> = discovered.into_iter().map(|(_, _, r)| r).collect();
    let total_records = records.len();
    for (rec_idx, record) in records.iter_mut().enumerate() {
        let chunk_slice = match chunk_starts[rec_idx] {
            None => &[][..],
            Some(start) => {
                let slice_start = if total_records <= 1 {
                    0
                } else {
                    start.min(page.chunks.len())
                };
                let slice_end = if total_records <= 1 {
                    page.chunks.len()
                } else {
                    chunk_starts[rec_idx + 1..]
                        .iter()
                        .flatten()
                        .copied()
                        .find(|&s| s >= slice_start)
                        .unwrap_or(page.chunks.len())
                        .min(page.chunks.len())
                };
                &page.chunks[slice_start..slice_end]
            }
        };
        for link in &page.links {
            if link.to_ascii_lowercase().contains(&record.md5)
                && !record.download_links.contains(link)
            {
                record.download_links.push(link.clone());
            }
        }
        for (_, link) in page.dom.unindexed_bib_hrefs() {
            if link.to_ascii_lowercase().contains(&record.md5)
                && !record.download_links.contains(link)
            {
                record.download_links.push(link.clone());
            }
        }
        for chunk in chunk_slice {
            for &link_id in &chunk.links {
                if let Some(link) = page.links.get(link_id as usize) {
                    let lower_link = link.to_ascii_lowercase();
                    if (lower_link.contains(&record.md5) || lower_link.contains("doi.org/"))
                        && !record.download_links.contains(link)
                    {
                        record.download_links.push(link.clone());
                    }
                }
            }
            for (node_id, link) in page.dom.unindexed_bib_hrefs() {
                if is_dom_ancestor_or_self(&page.dom, chunk.node, *node_id) {
                    let lower_link = link.to_ascii_lowercase();
                    if (lower_link.contains(&record.md5) || lower_link.contains("doi.org/"))
                        && !record.download_links.contains(link)
                    {
                        record.download_links.push(link.clone());
                    }
                }
            }
        }
        for (rel_idx, chunk) in chunk_slice.iter().enumerate() {
            let text = &chunk.text;
            let next_text = chunk_slice.get(rel_idx + 1).map(|c| c.text.as_str());
            if record.doi.is_none() {
                record.doi = extract_doi_from_text(text);
            }
            if record.isbn.is_none() {
                record.isbn = extract_isbn_from_text(text, next_text);
            }
            if record.format.is_none() {
                let check_text = if (text.eq_ignore_ascii_case("extension")
                    || text.eq_ignore_ascii_case("format"))
                    && next_text.is_some()
                {
                    next_text.unwrap_or(text)
                } else {
                    text
                };
                for fmt_token in ["pdf", "epub", "djvu", "mobi", "cbr", "azw3"] {
                    if check_text
                        .split(|c: char| !c.is_ascii_alphanumeric())
                        .any(|w| w.eq_ignore_ascii_case(fmt_token))
                    {
                        record.format = Some(fmt_token.to_owned());
                        break;
                    }
                }
            }
        }
    }
    records
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
    #[test]
    fn optional_links_disabled_skips_link_resolution_and_preserves_chunk_text() {
        let base = Url::parse("https://example.org/docs/index.html").unwrap();
        let html = "<main><h1>Spec</h1><p>Read <a href='/guide'>the guide</a> and <a href='https://example.org/api#v2'>API reference</a>.</p></main>";
        let with_links = parse_html_with_limits(html, &base, ParseLimits::default()).unwrap();
        let without_links = parse_html_with_limits(
            html,
            &base,
            ParseLimits {
                include_links: false,
                max_link_bytes: 0,
                max_link_url_bytes: 0,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        assert_eq!(with_links.links.len(), 2);
        assert!(without_links.links.is_empty());
        assert_eq!(with_links.chunks.len(), without_links.chunks.len());
        for (a, b) in with_links.chunks.iter().zip(without_links.chunks.iter()) {
            assert_eq!(a.node, b.node);
            assert_eq!(a.role, b.role);
            assert_eq!(a.text, b.text);
            assert!(b.links.is_empty());
        }
    }
    #[test]
    fn annas_archive_search_results_preserve_md5_doi_and_bibliographic_fields() {
        let base = Url::parse("https://annas-archive.org/search?q=information+retrieval").unwrap();
        let html = r#"<nav><ul><li><a href="/login">Login</a></li><li><a href="/donate">Donate</a></li></ul></nav>
        <main>
            <h1>Search results for "information retrieval"</h1>
            <article>
                <h3><a href="/md5/4f8e9a1b2c3d4e5f6a7b8c9d0e1f2a3b">Introduction to Information Retrieval — Manning, Raghavan, Schütze (2008)</a></h3>
                <p>English [en] · pdf · 14.2 MB · Cambridge University Press · ISBN 978-0521865715 · DOI 10.1017/CBO9780511809071</p>
                <ul>
                    <li><a href="/slow_download/4f8e9a1b2c3d4e5f6a7b8c9d0e1f2a3b/0/0">Slow Partner Server #1</a></li>
                    <li><a href="https://doi.org/10.1017/CBO9780511809071">DOI Resolver</a></li>
                </ul>
            </article>
            <!-- <article><h3><a href="/md5/9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d">Modern Information Retrieval — Baeza-Yates (2011)</a></h3><p>English [en] · epub · 8.4 MB</p></article> -->
            <!-- <script>evil()</script><a href="/md5/00000000000000000000000000000000">Blocked</a> -->
        </main>
        <footer><p>Anna's Archive footer chrome</p></footer>"#;
        let default_proj = parse_html(html, &base, 1000, 200).unwrap();
        assert_eq!(extract_annas_archive_records(&default_proj).len(), 1);
        assert!(default_proj.chunks.iter().any(|c| c.text == "Login"));

        let unwrapped = parse_html_with_limits(
            html,
            &base,
            ParseLimits {
                comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
                boilerplate: BoilerplatePolicy::MainContentV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        assert!(unwrapped.chunks.iter().all(|c| c.text != "Login"));
        assert!(
            unwrapped
                .chunks
                .iter()
                .all(|c| !c.text.contains("footer chrome"))
        );
        assert!(unwrapped.chunks.iter().all(|c| !c.text.contains("evil")));
        let records = extract_annas_archive_records(&unwrapped);
        assert_eq!(records.len(), 2);
        assert_eq!(records[0].md5, "4f8e9a1b2c3d4e5f6a7b8c9d0e1f2a3b");
        assert_eq!(records[0].doi.as_deref(), Some("10.1017/CBO9780511809071"));
        assert_eq!(records[0].isbn.as_deref(), Some("978-0521865715"));
        assert_eq!(records[0].format.as_deref(), Some("pdf"));
        assert_eq!(records[1].md5, "9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d");
        assert_eq!(records[1].doi, None);
        assert_eq!(records[1].isbn, None);
        assert_eq!(records[1].format.as_deref(), Some("epub"));
        assert_eq!(
            records[1].download_links,
            vec!["https://annas-archive.org/md5/9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"]
        );
    }
    #[test]
    fn annas_archive_detail_table_and_abrupt_or_nested_comments_and_link_density_boilerplate() {
        let base =
            Url::parse("https://annas-archive.org/md5/0123456789abcdef0123456789abcdef").unwrap();
        let detail_html = concat!(
            "<!doctype html><h1>Quantum Error Correction</h1>",
            "<table id=\"md5-metadata\"><thead><tr><th>Field</th><th>Value</th></tr></thead>",
            "<tbody><tr><th>MD5</th><td>0123456789abcdef0123456789abcdef</td></tr>",
            "<tr><th>DOI</th><td>10.1103/PhysRevLett.132.010601</td></tr>",
            "<tr><th>ISBN-13</th><td>978-0-521-87996-5</td></tr>",
            "<tr><th>Extension</th><td>pdf</td></tr></tbody></table>"
        );
        let detail_proj = parse_html(detail_html, &base, 1000, 200).unwrap();
        let detail_records = extract_annas_archive_records(&detail_proj);
        assert_eq!(detail_records.len(), 1);
        assert_eq!(detail_records[0].md5, "0123456789abcdef0123456789abcdef");
        assert_eq!(
            detail_records[0].doi.as_deref(),
            Some("10.1103/PhysRevLett.132.010601")
        );
        assert_eq!(detail_records[0].isbn.as_deref(), Some("978-0-521-87996-5"));
        assert_eq!(detail_records[0].format.as_deref(), Some("pdf"));

        let edge_html = concat!(
            "<div class=\"menu\"><p><a href=\"/1\">Home</a> <a href=\"/2\">Docs</a> <a href=\"/3\">Pricing</a> <a href=\"/4\">Login</a></p></div>",
            "<!--><p>Abrupt comment survivor</p><!--->",
            "<main><nav><p>Inner nav chrome</p></nav>",
            "<!-- outer note <!-- <article><p><a href=\"/md5/fedcba9876543210fedcba9876543210\">Unwrapped Card</a></p></article> -->",
            "<p>Trailing paragraph</p></main>"
        );
        let proj = parse_html_with_limits(
            edge_html,
            &base,
            ParseLimits {
                max_html_bytes: edge_html.len(),
                comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
                boilerplate: BoilerplatePolicy::MainContentV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        let texts: Vec<&str> = proj.chunks.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(
            texts,
            vec![
                "Abrupt comment survivor",
                "Unwrapped Card",
                "Trailing paragraph"
            ]
        );
        let recs = extract_annas_archive_records(&proj);
        assert_eq!(recs.len(), 1);
        assert_eq!(recs[0].md5, "fedcba9876543210fedcba9876543210");

        let cetd_html = concat!(
            "<div class=\"utility\"><p><a href=\"/login\">Sign in</a> | <a href=\"/reg\">Register</a></p></div>",
            "<div class=\"legal\"><p>Copyright 2026 Example Corp. All rights reserved. Privacy Policy.</p></div>",
            "<main><p>Primary research content on composite text density and entropy filtering.</p></main>"
        );
        let cetd_proj = parse_html_with_limits(
            cetd_html,
            &base,
            ParseLimits {
                max_html_bytes: cetd_html.len(),
                boilerplate: BoilerplatePolicy::CetdEntropyV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        let cetd_texts: Vec<&str> = cetd_proj.chunks.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(
            cetd_texts,
            vec!["Primary research content on composite text density and entropy filtering."]
        );
    }

    #[test]
    fn proj_01_02_03_script_comment_prefix_article_header_and_metadata_edge_cases() {
        let base = Url::parse("https://annas-archive.org/search?q=consensus").unwrap();
        // PROJ-01: exact prompt string "<script>let s = \"<!--\";</script><main><p>Body</p></main><!-- <a href=\"/md5/0123456789abcdef0123456789abcdef\">Card</a> -->"
        let html_proj01 = concat!(
            "<script>let s = \"<!--\";</script><main><p>Body</p></main>",
            "<!-- <a href=\"/md5/0123456789abcdef0123456789abcdef\">Card</a> -->"
        );
        let unwrapped01 =
            unwrap_comments_if_enabled(html_proj01, CommentUnwrapPolicy::AnnasArchiveScrollCards);
        assert!(unwrapped01.contains("<main><p>Body</p></main>"));
        assert!(unwrapped01.contains("<a href=\"/md5/0123456789abcdef0123456789abcdef\">Card</a>"));
        let p01 = parse_html_with_limits(
            html_proj01,
            &base,
            ParseLimits {
                max_html_bytes: html_proj01.len() + 64,
                comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
                boilerplate: BoilerplatePolicy::MainContentV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        let texts01: Vec<&str> = p01.chunks.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts01, vec!["Body"]);

        // PROJ-01 adversarial: outer comment containing '<b>tag</b>' ('>') or following <script> before nested '<!--'
        let html_proj01_adv = concat!(
            "<script>let s = \"<!--\";</script><main><p>BodyA</p>",
            "<!-- outer comment with <b>tag</b> <!-- <article><p><a href=\"/md5/0123456789abcdef0123456789abcdef\">CardA</a></p></article> -->",
            "<p>BodyB</p></main>"
        );
        let p01_adv = parse_html_with_limits(
            html_proj01_adv,
            &base,
            ParseLimits {
                max_html_bytes: html_proj01_adv.len() + 64,
                comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
                boilerplate: BoilerplatePolicy::MainContentV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        let texts01_adv: Vec<&str> = p01_adv.chunks.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts01_adv, vec!["BodyA", "CardA", "BodyB"]);

        // PROJ-02: <main><article><header><h1>Title</h1></header><p>Body</p></article></main> preserves <h1>Title</h1>
        let html_proj02 = concat!(
            "<nav><h2>Site Navigation</h2><p><a href=\"/a\">A</a></p></nav>",
            "<div class=\"sep\"><p>----------------------------------------</p></div>",
            "<main><article><header><h1>Title</h1></header><p>Body</p></article></main>"
        );
        let p02 = parse_html_with_limits(
            html_proj02,
            &base,
            ParseLimits {
                max_html_bytes: html_proj02.len(),
                boilerplate: BoilerplatePolicy::CetdEntropyV1,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        let texts02: Vec<&str> = p02.chunks.iter().map(|c| c.text.as_str()).collect();
        assert_eq!(texts02, vec!["Title", "Body"]);

        // PROJ-03: multi-"10." DOI with version/IP false positives & trailing punctuation, inline ISBN-13/10 vs 11/12-digit & placeholder rejection, and uppercase MD5 links
        let html_proj03 = concat!(
            "<main><article>",
            "<h2><a href=\"/md5/0123456789ABCDEF0123456789ABCDEF\">Distributed Algorithms</a></h2>",
            "<p>PDF · 10.4 MB · v1.10.2/rel · 192.168.10.1/24 · (10.1007/s00220-024-04900-1). · ISBN: XXXXXXXXXX · isbn-13: 978-0-521-87996-5</p>",
            "<p><a href=\"/slow_download/0123456789ABCDEF0123456789ABCDEF/0/0\">Mirror 1</a></p>",
            "</article>",
            "<article>",
            "<h2><a href=\"/md5/fedcba9876543210fedcba9876543210\">Invalid ISBN Card</a></h2>",
            "<p>EPUB · ISBN: 12345678901 · ISBN: 123456789012 · ISBN: XXXXXXXXXX</p>",
            "</article></main>"
        );
        let p03 = parse_html(html_proj03, &base, 1000, 200).unwrap();
        let recs03 = extract_annas_archive_records(&p03);
        assert_eq!(recs03.len(), 2);
        assert_eq!(recs03[0].md5, "0123456789abcdef0123456789abcdef");
        assert_eq!(recs03[0].doi.as_deref(), Some("10.1007/s00220-024-04900-1"));
        assert_eq!(recs03[0].isbn.as_deref(), Some("978-0-521-87996-5"));
        assert_eq!(recs03[0].format.as_deref(), Some("pdf"));
        assert_eq!(recs03[0].download_links.len(), 2);
        assert_eq!(recs03[1].md5, "fedcba9876543210fedcba9876543210");
        assert_eq!(recs03[1].isbn, None);

        // PROJ-04: Div-soup Anna's Archive book cards outside <main>/<article> preserve MD5, ISBN, and download mirrors under CetdEntropyV1 and MainContentV1
        let html_proj04 = concat!(
            "<nav><p><a href=\"/home\">Home</a> <a href=\"/donate\">Donate</a> <a href=\"/faq\">FAQ</a></p></nav>",
            "<div class=\"menu\"><p><a href=\"/login\">Login</a> <a href=\"/register\">Register</a> <a href=\"/terms\">Terms</a></p></div>",
            "<div class=\"card\">",
            "<h3><a href=\"/md5/abcdef0123456789abcdef0123456789\">Information Retrieval Handbook</a></h3>",
            "<p>PDF · ISBN: 978-0-521-86571-5</p>",
            "<p><a href=\"/slow_download/abcdef0123456789abcdef0123456789/0/0\">Slow #1</a> <a href=\"/slow_download/abcdef0123456789abcdef0123456789/0/1\">Slow #2</a> <a href=\"/fast_download/abcdef0123456789abcdef0123456789/0/0\">Fast #1</a></p>",
            "</div>"
        );
        for policy in [
            BoilerplatePolicy::MainContentV1,
            BoilerplatePolicy::CetdEntropyV1,
        ] {
            let p04 = parse_html_with_limits(
                html_proj04,
                &base,
                ParseLimits {
                    max_html_bytes: html_proj04.len() + 64,
                    boilerplate: policy,
                    ..ParseLimits::default()
                },
            )
            .unwrap();
            let recs04 = extract_annas_archive_records(&p04);
            assert_eq!(recs04.len(), 1);
            assert_eq!(recs04[0].md5, "abcdef0123456789abcdef0123456789");
            assert_eq!(recs04[0].isbn.as_deref(), Some("978-0-521-86571-5"));
            assert_eq!(recs04[0].download_links.len(), 4);
            assert!(
                !p04.chunks
                    .iter()
                    .any(|c| c.text.contains("Donate") || c.text.contains("Register")),
                "navigation and menu links must still be removed under {policy:?}"
            );

            // PROJ-05: include_links=false (LinkMode::None) achieves identical boilerplate pruning
            // and Anna's Archive MD5 record extraction while keeping page.links & chunk.links empty!
            let p05_no_links = parse_html_with_limits(
                html_proj04,
                &base,
                ParseLimits {
                    max_html_bytes: html_proj04.len() + 64,
                    boilerplate: policy,
                    include_links: false,
                    max_link_bytes: 0,
                    max_link_url_bytes: 0,
                    ..ParseLimits::default()
                },
            )
            .unwrap();
            assert!(p05_no_links.links.is_empty());
            assert!(p05_no_links.chunks.iter().all(|c| c.links.is_empty()));
            assert_eq!(p05_no_links.chunks.len(), p04.chunks.len());
            let recs05 = extract_annas_archive_records(&p05_no_links);
            assert_eq!(recs05.len(), 1);
            assert_eq!(recs05[0].md5, "abcdef0123456789abcdef0123456789");
            assert_eq!(recs05[0].isbn.as_deref(), Some("978-0-521-86571-5"));
            assert_eq!(recs05[0].download_links.len(), 4);
        }

        // PROJ-05b: outer `<a href="/md5/..."><h3>Title</h3></a>` card and false-positive sidebar rejection
        let html_proj05b = concat!(
            "<div class=\"fake-bib-sidebar\"><p>v10.2 · Search by ISBN · <a href=\"/md5/faq\">MD5 Help</a> <a href=\"/login\">Login</a> <a href=\"/donate\">Donate</a></p></div>",
            "<div class=\"outer-anchor-card\"><a href=\"/md5/11223344556677889900aabbccddeeff\"><h3>Outer Anchor Card Title</h3></a><p>PDF · DOI: 10.1007/s00220-024-04900-1</p></div>"
        );
        let p05b = parse_html_with_limits(
            html_proj05b,
            &base,
            ParseLimits {
                max_html_bytes: html_proj05b.len() + 64,
                boilerplate: BoilerplatePolicy::CetdEntropyV1,
                include_links: false,
                ..ParseLimits::default()
            },
        )
        .unwrap();
        assert!(
            !p05b.chunks.iter().any(|c| c.text.contains("MD5 Help")),
            "false-positive v10.2 / Search by ISBN / /md5/faq sidebar must be stripped"
        );
        let recs05b = extract_annas_archive_records(&p05b);
        assert_eq!(recs05b.len(), 1);
        assert_eq!(recs05b[0].md5, "11223344556677889900aabbccddeeff");
        assert_eq!(recs05b[0].title, "Outer Anchor Card Title");
        assert_eq!(
            recs05b[0].doi.as_deref(),
            Some("10.1007/s00220-024-04900-1")
        );

        // PROJ-05c: mixed outer-anchor, div-only, and standard heading-anchor cards in DOM order
        let html_proj05c = concat!(
            "<div class=\"card-1\"><a href=\"/md5/11112222333344445555666677778888\"><h3>First Outer Anchor Card</h3></a><p>PDF · DOI: 10.1007/s00220-024-04900-1</p></div>",
            "<div class=\"card-2\"><a href=\"/md5/9999aaaabbbbccccddddeeeeffff0000\">Second Div Only Card</a><div>EPUB · ISBN 978-0-387-94880-5</div></div>",
            "<div class=\"card-3\"><h3><a href=\"/md5/abcdefabcdefabcdefabcdefabcdef12\">Third Standard Heading Card</a></h3><p>DJVU · DOI: 10.1145/3581784.3607090</p></div>"
        );
        for include_links in [true, false] {
            let p05c = parse_html_with_limits(
                html_proj05c,
                &base,
                ParseLimits {
                    max_html_bytes: html_proj05c.len() + 64,
                    boilerplate: BoilerplatePolicy::CetdEntropyV1,
                    include_links,
                    ..ParseLimits::default()
                },
            )
            .unwrap();
            let recs05c = extract_annas_archive_records(&p05c);
            assert_eq!(recs05c.len(), 3);
            assert_eq!(recs05c[0].md5, "11112222333344445555666677778888");
            assert_eq!(recs05c[1].md5, "9999aaaabbbbccccddddeeeeffff0000");
            assert_eq!(recs05c[2].md5, "abcdefabcdefabcdefabcdefabcdef12");
            assert_eq!(recs05c[1].isbn.as_deref(), Some("978-0-387-94880-5"));
            assert_eq!(recs05c[2].doi.as_deref(), Some("10.1145/3581784.3607090"));
        }
    }
}
