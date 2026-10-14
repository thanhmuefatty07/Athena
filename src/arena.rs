//! Compact DOM storage. The actual HTML tokenizer/tree builder is intentionally
//! external: HTML5 error recovery, character encoding and entity decoding are
//! not accomplished by merely storing byte offsets.
use bytes::Bytes;

pub type NodeId = u32;
pub const NONE: NodeId = u32::MAX;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum Tag {
    Root,
    Text,
    Main,
    Article,
    Section,
    Div,
    P,
    H1,
    H2,
    H3,
    H4,
    Table,
    Thead,
    Tbody,
    Tfoot,
    Tr,
    Th,
    Td,
    Ul,
    Ol,
    Li,
    A,
    Pre,
    Code,
    Nav,
    Footer,
    Header,
    Aside,
    Form,
    Script,
    Style,
    Unknown,
}

/// 32-byte payload, alignment 4, stored contiguously.
/// Alignment is not forced to 32: unnecessary alignment can increase
/// allocator constraints without proving a performance advantage.
#[derive(Debug, Clone, Copy)]
#[repr(C)]
pub struct Node {
    pub parent: NodeId,
    pub first_child: NodeId,
    pub last_child: NodeId,
    pub next_sibling: NodeId,
    pub source_start: u32,
    pub source_len: u32,
    pub attributes_id: u32,
    pub tag: Tag,
    pub flags: u8,
    pub depth: u16,
}

const _: () = assert!(std::mem::size_of::<Node>() == 32);

impl Node {
    fn root() -> Self {
        Self {
            parent: NONE,
            first_child: NONE,
            last_child: NONE,
            next_sibling: NONE,
            source_start: 0,
            source_len: 0,
            attributes_id: NONE,
            tag: Tag::Root,
            flags: 0,
            depth: 0,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArenaError {
    BadParent,
    BadNode,
    BadRange,
    TooManyNodes,
    TooDeep,
}

/// Owns a ref-counted raw buffer. Reuse across unrelated requests is safe
/// from a Rust lifetime perspective; sharing still requires synchronization.
pub struct DomArena {
    raw: Bytes,
    nodes: Vec<Node>,
}

impl DomArena {
    pub fn new(raw: Bytes, capacity: usize) -> Self {
        let mut nodes = Vec::with_capacity(capacity.max(1));
        nodes.push(Node::root());
        Self { raw, nodes }
    }

    #[inline]
    pub fn reset(&mut self, raw: Bytes) {
        self.raw = raw;
        self.nodes.truncate(1);
        self.nodes[0] = Node::root();
    }

    #[inline]
    pub fn nodes(&self) -> &[Node] {
        &self.nodes
    }

    pub fn set_attribute_id(&mut self, id: NodeId, index: u32) -> Result<(), ArenaError> {
        let n = self.nodes.get_mut(id as usize).ok_or(ArenaError::BadNode)?;
        n.attributes_id = index;
        Ok(())
    }

    pub fn append_child(
        &mut self,
        parent: NodeId,
        tag: Tag,
        source: Option<(usize, usize)>,
    ) -> Result<NodeId, ArenaError> {
        let p = usize::try_from(parent).map_err(|_| ArenaError::BadParent)?;
        if p >= self.nodes.len() {
            return Err(ArenaError::BadParent);
        }
        // Reserve u32::MAX as a sentinel (therefore the last valid ID is MAX-1).
        if self.nodes.len() >= NONE as usize {
            return Err(ArenaError::TooManyNodes);
        }
        let depth = self.nodes[p]
            .depth
            .checked_add(1)
            .ok_or(ArenaError::TooDeep)?;
        let (start, len) = match source {
            Some((start, len)) => {
                let end = start.checked_add(len).ok_or(ArenaError::BadRange)?;
                if end > self.raw.len() || start > u32::MAX as usize || len > u32::MAX as usize {
                    return Err(ArenaError::BadRange);
                }
                (start as u32, len as u32)
            }
            None => (0, 0),
        };
        let id = self.nodes.len() as NodeId;
        let prev = self.nodes[p].last_child;
        self.nodes.push(Node {
            parent,
            first_child: NONE,
            last_child: NONE,
            next_sibling: NONE,
            source_start: start,
            source_len: len,
            attributes_id: NONE,
            tag,
            flags: 0,
            depth,
        });
        if prev == NONE {
            self.nodes[p].first_child = id;
        } else {
            self.nodes[prev as usize].next_sibling = id;
        }
        self.nodes[p].last_child = id;
        Ok(id)
    }

    /// Returns a raw UTF-8 substring, not rendered/decoded HTML text.
    /// For HTML input with entities or non-UTF-8 encoding, first decode using
    /// an HTML5-compliant parser and store resulting text separately.
    pub fn raw_text(&self, id: NodeId) -> Result<&str, ArenaError> {
        let node = self.nodes.get(id as usize).ok_or(ArenaError::BadNode)?;
        let start = node.source_start as usize;
        let end = start
            .checked_add(node.source_len as usize)
            .ok_or(ArenaError::BadRange)?;
        let bytes = self.raw.get(start..end).ok_or(ArenaError::BadRange)?;
        std::str::from_utf8(bytes).map_err(|_| ArenaError::BadRange)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn size_layout_and_siblings() {
        assert_eq!(std::mem::size_of::<Node>(), 32);
        let mut a = DomArena::new(Bytes::from_static(b"alpha beta"), 4);
        let x = a.append_child(0, Tag::Text, Some((0, 5))).unwrap();
        let y = a.append_child(0, Tag::Text, Some((6, 4))).unwrap();
        assert_eq!(a.nodes()[0].first_child, x);
        assert_eq!(a.nodes()[0].last_child, y);
        assert_eq!(a.nodes()[x as usize].next_sibling, y);
        assert_eq!(a.raw_text(x).unwrap(), "alpha");
    }

    #[test]
    fn resets_every_root_field_and_buffer() {
        let mut a = DomArena::new(Bytes::from_static(b"before"), 2);
        a.append_child(0, Tag::P, None).unwrap();
        a.reset(Bytes::from_static(b"after"));
        assert_eq!(a.nodes().len(), 1);
        assert_eq!(a.nodes()[0].first_child, NONE);
        assert_eq!(a.nodes()[0].last_child, NONE);
        assert_eq!(a.nodes()[0].flags, 0);
        let id = a.append_child(0, Tag::Text, Some((0, 5))).unwrap();
        assert_eq!(a.raw_text(id).unwrap(), "after");
    }

    #[test]
    fn bounds_and_utf8_are_checked() {
        let mut a = DomArena::new(Bytes::from_static(b"\xc3\xa9"), 1);
        assert_eq!(
            a.append_child(0, Tag::Text, Some((2, 1))),
            Err(ArenaError::BadRange)
        );
        let id = a.append_child(0, Tag::Text, Some((1, 1))).unwrap();
        assert_eq!(a.raw_text(id), Err(ArenaError::BadRange));
    }
}
