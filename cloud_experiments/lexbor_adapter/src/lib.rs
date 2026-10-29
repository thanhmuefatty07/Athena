#![deny(unsafe_op_in_unsafe_fn)]

use athena_core_v2::projection::{
    ParseLimits, Projection, ResearchNode, ResearchNodeKind, parse_warning_count_with_limits,
    project_research_nodes,
};
use std::{
    collections::HashMap,
    ffi::{c_int, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
    ptr, slice,
};
use url::Url;

pub mod tables;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LexborDiagnostics {
    pub tree_errors: usize,
    pub tokenizer_errors: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LexborDiagnosticSource {
    Tree,
    Tokenizer,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LexborDiagnosticEvent {
    pub source: LexborDiagnosticSource,
    pub error_id: usize,
    pub description: String,
    pub start_offset: Option<usize>,
    pub end_offset: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LexborDiagnosticTrace {
    pub events: Vec<LexborDiagnosticEvent>,
    pub truncated: bool,
}

pub struct LexborOutput {
    pub projection: Projection,
    pub diagnostics: LexborDiagnostics,
}

pub struct LexborTree {
    pub nodes: Vec<ResearchNode>,
    pub diagnostics: LexborDiagnostics,
}

type NodeCallback = unsafe extern "C" fn(
    *mut c_void,
    *const c_void,
    *const c_void,
    *const c_void,
    u8,
    u8,
    *const u8,
    usize,
    *const u8,
    usize,
) -> c_int;

type DiagnosticCallback =
    unsafe extern "C" fn(*mut c_void, u8, usize, *const u8, usize, usize, usize) -> c_int;

const MAX_DIAGNOSTIC_EVENTS: usize = 256;

const EVENT_NODE: u8 = 0;
const EVENT_ATTRIBUTE: u8 = 1;
const NODE_ELEMENT: u8 = 1;
const NODE_TEXT: u8 = 2;

unsafe extern "C" {
    fn athena_lexbor_parse(
        html: *const u8,
        html_len: usize,
        callback: NodeCallback,
        context: *mut c_void,
        diagnostic_callback: Option<DiagnosticCallback>,
        tree_errors: *mut usize,
        tokenizer_errors: *mut usize,
    ) -> c_int;
}

struct Collector {
    document_node: usize,
    max_parser_nodes: usize,
    node_indices: HashMap<usize, usize>,
    nodes: Vec<ResearchNode>,
    diagnostic_events: Vec<LexborDiagnosticEvent>,
    capture_diagnostics: bool,
    diagnostics_truncated: bool,
    input_base: usize,
    input_len: usize,
    error: Option<&'static str>,
}

#[derive(Clone, Copy)]
struct RawNodeEvent {
    document_node: *const c_void,
    node: *const c_void,
    parent: *const c_void,
    event_kind: u8,
    kind: u8,
    name: *const u8,
    name_len: usize,
    text: *const u8,
    text_len: usize,
}

impl Collector {
    fn new(max_parser_nodes: usize, capture_diagnostics: bool, input: &str) -> Self {
        Self {
            document_node: 0,
            max_parser_nodes,
            node_indices: HashMap::new(),
            nodes: Vec::new(),
            diagnostic_events: Vec::new(),
            capture_diagnostics,
            diagnostics_truncated: false,
            input_base: input.as_ptr() as usize,
            input_len: input.len(),
            error: None,
        }
    }

    fn input_offset(&self, address: usize) -> Option<usize> {
        address
            .checked_sub(self.input_base)
            .filter(|offset| *offset <= self.input_len)
    }

    unsafe fn copy_utf8(data: *const u8, len: usize) -> Result<String, &'static str> {
        if len == 0 {
            return Ok(String::new());
        }
        if data.is_null() {
            return Err("Lexbor returned a null pointer for nonempty data");
        }
        // The C shim invokes this callback while the owning Lexbor document is alive.
        let bytes = unsafe { slice::from_raw_parts(data, len) };
        let value = std::str::from_utf8(bytes).map_err(|_| "Lexbor returned invalid UTF-8")?;
        let mut copied = String::new();
        copied
            .try_reserve_exact(value.len())
            .map_err(|_| "Lexbor event memory allocation failed")?;
        copied.push_str(value);
        Ok(copied)
    }

    unsafe fn capture(&mut self, event: RawNodeEvent) -> Result<(), &'static str> {
        if event.event_kind == EVENT_ATTRIBUTE {
            if self.document_node == 0 || event.document_node as usize != self.document_node {
                return Err("Lexbor changed the document root during attribute traversal");
            }
            let node = event.node as usize;
            let index = *self
                .node_indices
                .get(&node)
                .ok_or("Lexbor attribute owner was not observed")?;
            let name = unsafe { Self::copy_utf8(event.name, event.name_len)? }.to_ascii_lowercase();
            if name.is_empty() {
                return Err("Lexbor returned an attribute without a name");
            }
            let value = unsafe { Self::copy_utf8(event.text, event.text_len)? };
            let attributes = &mut self.nodes[index].attributes;
            attributes
                .try_reserve(1)
                .map_err(|_| "Lexbor event memory allocation failed")?;
            attributes.push((name, value));
            return Ok(());
        }
        if event.event_kind != EVENT_NODE {
            return Err("Lexbor returned an unknown event kind");
        }
        let document_node = event.document_node as usize;
        if document_node == 0 || (self.document_node != 0 && self.document_node != document_node) {
            return Err("Lexbor changed the document root during traversal");
        }
        self.document_node = document_node;

        // The document node is the parser root and is not included by Lexbor's
        // simple walker. Count it here so the research cap matches Athena's API.
        if self.nodes.len() >= self.max_parser_nodes.saturating_sub(1) {
            return Err("max DOM nodes exceeded");
        }
        let node = event.node as usize;
        if node == 0 || self.node_indices.contains_key(&node) {
            return Err("Lexbor returned an invalid or duplicate node pointer");
        }
        let parent = event.parent as usize;
        let parent_index = if parent == document_node {
            None
        } else {
            Some(
                *self
                    .node_indices
                    .get(&parent)
                    .ok_or("Lexbor preorder parent was not observed")?,
            )
        };

        let kind = match event.kind {
            NODE_ELEMENT => ResearchNodeKind::Element,
            NODE_TEXT => ResearchNodeKind::Text,
            _ => ResearchNodeKind::Other,
        };
        let mut name = if kind == ResearchNodeKind::Element {
            // Lexbor 3.0.0 returns uppercase HTML names through this accessor.
            unsafe { Self::copy_utf8(event.name, event.name_len)? }.to_ascii_lowercase()
        } else {
            String::new()
        };
        if kind == ResearchNodeKind::Element && name.is_empty() {
            return Err("Lexbor returned an element without a name");
        }
        let text = if kind == ResearchNodeKind::Text {
            unsafe { Self::copy_utf8(event.text, event.text_len)? }
        } else {
            String::new()
        };
        self.nodes
            .try_reserve(1)
            .map_err(|_| "Lexbor event memory allocation failed")?;
        self.node_indices
            .try_reserve(1)
            .map_err(|_| "Lexbor event memory allocation failed")?;
        let index = self.nodes.len();
        self.nodes.push(ResearchNode {
            parent: parent_index,
            kind,
            name: std::mem::take(&mut name),
            text,
            attributes: Vec::new(),
        });
        self.node_indices.insert(node, index);
        Ok(())
    }

    unsafe fn capture_diagnostic(
        &mut self,
        source: u8,
        error_id: usize,
        description: *const u8,
        description_len: usize,
        start_address: usize,
        end_address: usize,
    ) -> Result<(), &'static str> {
        if self.diagnostic_events.len() == MAX_DIAGNOSTIC_EVENTS {
            self.diagnostics_truncated = true;
            return Ok(());
        }
        let source = match source {
            0 => LexborDiagnosticSource::Tree,
            1 => LexborDiagnosticSource::Tokenizer,
            _ => return Err("Lexbor returned an unknown diagnostic source"),
        };
        let description = unsafe { Self::copy_utf8(description, description_len)? };
        let start_offset = self.input_offset(start_address);
        let end_offset = self.input_offset(end_address);
        if matches!((start_offset, end_offset), (Some(start), Some(end)) if start > end) {
            return Err("Lexbor returned a reversed diagnostic byte range");
        }
        self.diagnostic_events
            .try_reserve(1)
            .map_err(|_| "Lexbor diagnostic allocation failed")?;
        self.diagnostic_events.push(LexborDiagnosticEvent {
            source,
            error_id,
            description,
            start_offset,
            end_offset,
        });
        Ok(())
    }
}

unsafe extern "C" fn collect_node(
    context: *mut c_void,
    document_node: *const c_void,
    node: *const c_void,
    parent: *const c_void,
    event_kind: u8,
    kind: u8,
    name: *const u8,
    name_len: usize,
    text: *const u8,
    text_len: usize,
) -> c_int {
    if context.is_null() {
        return 0;
    }
    // The state is exclusively borrowed for this synchronous callback. The C
    // shim does not retain pointers or invoke this callback from another thread.
    let collector = unsafe { &mut *context.cast::<Collector>() };
    if collector.error.is_some() {
        return 0;
    }
    match catch_unwind(AssertUnwindSafe(|| unsafe {
        collector.capture(RawNodeEvent {
            document_node,
            node,
            parent,
            event_kind,
            kind,
            name,
            name_len,
            text,
            text_len,
        })
    })) {
        Ok(Ok(())) => 1,
        Ok(Err(error)) => {
            collector.error = Some(error);
            0
        }
        Err(_) => {
            collector.error = Some("Lexbor event callback panicked");
            0
        }
    }
}

unsafe extern "C" fn collect_diagnostic(
    context: *mut c_void,
    source: u8,
    error_id: usize,
    description: *const u8,
    description_len: usize,
    start_address: usize,
    end_address: usize,
) -> c_int {
    if context.is_null() {
        return 0;
    }
    let collector = unsafe { &mut *context.cast::<Collector>() };
    if collector.error.is_some() || !collector.capture_diagnostics {
        return 0;
    }
    if collector.diagnostic_events.len() == MAX_DIAGNOSTIC_EVENTS {
        collector.diagnostics_truncated = true;
        return 0;
    }
    match catch_unwind(AssertUnwindSafe(|| unsafe {
        collector.capture_diagnostic(
            source,
            error_id,
            description,
            description_len,
            start_address,
            end_address,
        )
    })) {
        Ok(Ok(())) => 1,
        Ok(Err(error)) => {
            collector.error = Some(error);
            0
        }
        Err(_) => {
            collector.error = Some("Lexbor diagnostic callback panicked");
            0
        }
    }
}

/// Parse one UTF-8 HTML document with the pinned Lexbor C build and copy its
/// preorder nodes and attributes while the native document is still alive.
/// Lexbor's internal allocations are not capped by `max_parser_nodes`; that
/// logical node limit is checked while collecting the completed parser tree.
fn parse_tree_internal(
    html: &str,
    limits: ParseLimits,
    capture_diagnostics: bool,
) -> Result<(LexborTree, LexborDiagnosticTrace), String> {
    if limits.max_nodes == 0
        || limits.max_parser_nodes == 0
        || limits.max_words_per_chunk == 0
        || html.len() > limits.max_html_bytes
        || html.len() > u32::MAX as usize
    {
        return Err("invalid resource budget".into());
    }

    let mut collector = Collector::new(limits.max_parser_nodes, capture_diagnostics, html);
    let mut tree_errors = 0usize;
    let mut tokenizer_errors = 0usize;
    // The C function copies no Rust-owned data and keeps its document alive
    // until every callback has returned.
    let status = unsafe {
        athena_lexbor_parse(
            html.as_ptr(),
            html.len(),
            collect_node,
            ptr::addr_of_mut!(collector).cast(),
            capture_diagnostics.then_some(collect_diagnostic),
            ptr::addr_of_mut!(tree_errors),
            ptr::addr_of_mut!(tokenizer_errors),
        )
    };
    if let Some(error) = collector.error {
        return Err(error.into());
    }
    if status != 0 {
        return Err(format!("Lexbor parse failed with status {status}"));
    }

    Ok((
        LexborTree {
            nodes: collector.nodes,
            diagnostics: LexborDiagnostics {
                tree_errors,
                tokenizer_errors,
            },
        },
        LexborDiagnosticTrace {
            events: collector.diagnostic_events,
            truncated: collector.diagnostics_truncated,
        },
    ))
}

pub fn parse_tree(html: &str, limits: ParseLimits) -> Result<LexborTree, String> {
    parse_tree_internal(html, limits, false).map(|(tree, _)| tree)
}

pub fn parse_tree_with_diagnostics(
    html: &str,
    limits: ParseLimits,
) -> Result<(LexborTree, LexborDiagnosticTrace), String> {
    parse_tree_internal(html, limits, true)
}

/// Parse with Lexbor and apply Athena's shared arena, link, chunk and
/// projection rules to the copied parser tree.
pub fn parse_and_project(
    html: &str,
    base: &Url,
    limits: ParseLimits,
) -> Result<LexborOutput, String> {
    let warning_count = parse_warning_count_with_limits(html, limits).map_err(str::to_owned)?;
    let tree = parse_tree(html, limits)?;
    project_tree(html, base, limits, tree, warning_count)
}

pub fn parse_and_project_with_diagnostics(
    html: &str,
    base: &Url,
    limits: ParseLimits,
) -> Result<(LexborOutput, LexborDiagnosticTrace), String> {
    let warning_count = parse_warning_count_with_limits(html, limits).map_err(str::to_owned)?;
    let (tree, trace) = parse_tree_with_diagnostics(html, limits)?;
    project_tree(html, base, limits, tree, warning_count).map(|output| (output, trace))
}

fn project_tree(
    html: &str,
    base: &Url,
    limits: ParseLimits,
    tree: LexborTree,
    warning_count: usize,
) -> Result<LexborOutput, String> {
    let projection = project_research_nodes(html.len(), &tree.nodes, base, limits, warning_count)
        .map_err(str::to_owned)?;
    Ok(LexborOutput {
        projection,
        diagnostics: tree.diagnostics,
    })
}
