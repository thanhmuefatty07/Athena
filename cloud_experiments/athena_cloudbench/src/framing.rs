#![forbid(unsafe_code)]

use std::io::{self, Read, Write};

/// Default hard ceiling (`64 MiB`) on a single binary IPC frame payload.
pub const DEFAULT_MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;

/// Borrowed view over a decoded `[u32 total_len][u32 uri_len][uri][u32 hdr_len][headers][u32 body_len][raw_body_bytes]` frame.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RawWarcFrameRef<'a> {
    pub uri: &'a [u8],
    pub headers: &'a [u8],
    pub raw_body_bytes: &'a [u8],
}

/// Owned decoded binary IPC frame preserving raw untranscoded HTTP headers and body bytes.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RawWarcFrame {
    pub uri: Vec<u8>,
    pub headers: Vec<u8>,
    pub raw_body_bytes: Vec<u8>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FrameError {
    PayloadTooLarge {
        total_len: usize,
        max_bytes: usize,
    },
    LengthOverflow,
    TruncatedFrame {
        expected: usize,
        actual: usize,
    },
    TotalLengthMismatch {
        declared_total: usize,
        inner_sum: usize,
    },
    Io(String),
}

impl std::fmt::Display for FrameError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::PayloadTooLarge {
                total_len,
                max_bytes,
            } => write!(
                f,
                "frame total_len {total_len} exceeds max_bytes {max_bytes}"
            ),
            Self::LengthOverflow => write!(f, "frame component length overflows u32"),
            Self::TruncatedFrame { expected, actual } => {
                write!(
                    f,
                    "truncated frame: expected {expected} bytes, got {actual}"
                )
            }
            Self::TotalLengthMismatch {
                declared_total,
                inner_sum,
            } => write!(
                f,
                "frame total_len mismatch: declared {declared_total}, inner sum {inner_sum}"
            ),
            Self::Io(msg) => write!(f, "frame I/O error: {msg}"),
        }
    }
}

impl std::error::Error for FrameError {}

/// Encodes `[u32 total_len][u32 uri_len][uri][u32 hdr_len][headers][u32 body_len][raw_body_bytes]` in network (big-endian) byte order.
pub fn encode_raw_warc_frame(
    uri: &[u8],
    headers: &[u8],
    raw_body_bytes: &[u8],
) -> Result<Vec<u8>, FrameError> {
    let uri_len = u32::try_from(uri.len()).map_err(|_| FrameError::LengthOverflow)?;
    let hdr_len = u32::try_from(headers.len()).map_err(|_| FrameError::LengthOverflow)?;
    let body_len = u32::try_from(raw_body_bytes.len()).map_err(|_| FrameError::LengthOverflow)?;

    let inner_total = 4usize
        .checked_add(uri.len())
        .and_then(|n| n.checked_add(4))
        .and_then(|n| n.checked_add(headers.len()))
        .and_then(|n| n.checked_add(4))
        .and_then(|n| n.checked_add(raw_body_bytes.len()))
        .ok_or(FrameError::LengthOverflow)?;
    let total_len = u32::try_from(inner_total).map_err(|_| FrameError::LengthOverflow)?;

    let mut buf = Vec::with_capacity(4 + inner_total);
    buf.extend_from_slice(&total_len.to_be_bytes());
    buf.extend_from_slice(&uri_len.to_be_bytes());
    buf.extend_from_slice(uri);
    buf.extend_from_slice(&hdr_len.to_be_bytes());
    buf.extend_from_slice(headers);
    buf.extend_from_slice(&body_len.to_be_bytes());
    buf.extend_from_slice(raw_body_bytes);
    Ok(buf)
}

/// Zero-copy decode of a complete `[u32 total_len]...` buffer.
pub fn decode_raw_warc_frame(
    frame_bytes: &[u8],
    max_bytes: usize,
) -> Result<RawWarcFrameRef<'_>, FrameError> {
    if frame_bytes.len() < 4 {
        return Err(FrameError::TruncatedFrame {
            expected: 4,
            actual: frame_bytes.len(),
        });
    }
    let total_len = u32::from_be_bytes([
        frame_bytes[0],
        frame_bytes[1],
        frame_bytes[2],
        frame_bytes[3],
    ]) as usize;
    if total_len > max_bytes {
        return Err(FrameError::PayloadTooLarge {
            total_len,
            max_bytes,
        });
    }
    let expected_wire = 4usize
        .checked_add(total_len)
        .ok_or(FrameError::LengthOverflow)?;
    if frame_bytes.len() != expected_wire {
        return Err(FrameError::TruncatedFrame {
            expected: expected_wire,
            actual: frame_bytes.len(),
        });
    }
    decode_inner_payload(&frame_bytes[4..], total_len)
}

fn decode_inner_payload(
    payload: &[u8],
    declared_total: usize,
) -> Result<RawWarcFrameRef<'_>, FrameError> {
    if payload.len() < 12 {
        return Err(FrameError::TruncatedFrame {
            expected: 12,
            actual: payload.len(),
        });
    }
    let mut pos = 0usize;
    let uri_len = u32::from_be_bytes([
        payload[pos],
        payload[pos + 1],
        payload[pos + 2],
        payload[pos + 3],
    ]) as usize;
    pos += 4;
    let uri_end = pos.checked_add(uri_len).ok_or(FrameError::LengthOverflow)?;
    if uri_end + 4 > payload.len() {
        return Err(FrameError::TruncatedFrame {
            expected: uri_end + 4,
            actual: payload.len(),
        });
    }
    let uri = &payload[pos..uri_end];
    pos = uri_end;

    let hdr_len = u32::from_be_bytes([
        payload[pos],
        payload[pos + 1],
        payload[pos + 2],
        payload[pos + 3],
    ]) as usize;
    pos += 4;
    let hdr_end = pos.checked_add(hdr_len).ok_or(FrameError::LengthOverflow)?;
    if hdr_end + 4 > payload.len() {
        return Err(FrameError::TruncatedFrame {
            expected: hdr_end + 4,
            actual: payload.len(),
        });
    }
    let headers = &payload[pos..hdr_end];
    pos = hdr_end;

    let body_len = u32::from_be_bytes([
        payload[pos],
        payload[pos + 1],
        payload[pos + 2],
        payload[pos + 3],
    ]) as usize;
    pos += 4;
    let body_end = pos
        .checked_add(body_len)
        .ok_or(FrameError::LengthOverflow)?;
    if body_end != declared_total {
        return Err(FrameError::TotalLengthMismatch {
            declared_total,
            inner_sum: body_end,
        });
    }
    if body_end > payload.len() {
        return Err(FrameError::TruncatedFrame {
            expected: body_end,
            actual: payload.len(),
        });
    }
    let raw_body_bytes = &payload[pos..body_end];
    Ok(RawWarcFrameRef {
        uri,
        headers,
        raw_body_bytes,
    })
}

pub fn write_raw_warc_frame<W: Write>(
    writer: &mut W,
    uri: &[u8],
    headers: &[u8],
    raw_body_bytes: &[u8],
) -> Result<(), FrameError> {
    let encoded = encode_raw_warc_frame(uri, headers, raw_body_bytes)?;
    writer
        .write_all(&encoded)
        .map_err(|e| FrameError::Io(e.to_string()))?;
    writer.flush().map_err(|e| FrameError::Io(e.to_string()))
}

/// Reads the next binary IPC frame from `reader`, returning `Ok(None)` on clean EOF at a frame boundary.
pub fn read_raw_warc_frame<R: Read>(
    reader: &mut R,
    max_bytes: usize,
) -> Result<Option<RawWarcFrame>, FrameError> {
    let mut len_prefix = [0u8; 4];
    match reader.read_exact(&mut len_prefix) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(err) => return Err(FrameError::Io(err.to_string())),
    }
    let total_len = u32::from_be_bytes(len_prefix) as usize;
    if total_len > max_bytes {
        return Err(FrameError::PayloadTooLarge {
            total_len,
            max_bytes,
        });
    }
    let mut payload = vec![0u8; total_len];
    reader.read_exact(&mut payload).map_err(|err| {
        if err.kind() == io::ErrorKind::UnexpectedEof {
            FrameError::TruncatedFrame {
                expected: total_len,
                actual: 0,
            }
        } else {
            FrameError::Io(err.to_string())
        }
    })?;
    let view = decode_inner_payload(&payload, total_len)?;
    Ok(Some(RawWarcFrame {
        uri: view.uri.to_vec(),
        headers: view.headers.to_vec(),
        raw_body_bytes: view.raw_body_bytes.to_vec(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn roundtrips_non_utf8_legacy_charset_raw_bytes_without_transcoding() {
        let uri = b"http://legacy.example.jp/shift_jis/index.html";
        let headers = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=Shift_JIS\r\n\r\n";
        // Invalid UTF-8 (Shift_JIS "日本語") preserved byte-for-byte
        let raw_body: &[u8] = &[
            0x3c, 0x70, 0x3e, 0x93, 0xfa, 0x96, 0x7b, 0x8c, 0xea, 0x3c, 0x2f, 0x70, 0x3e,
        ];
        assert!(std::str::from_utf8(std::hint::black_box(raw_body)).is_err());

        let wire = encode_raw_warc_frame(uri, headers, raw_body).unwrap();
        let decoded = decode_raw_warc_frame(&wire, DEFAULT_MAX_FRAME_BYTES).unwrap();
        assert_eq!(decoded.uri, uri);
        assert_eq!(decoded.headers, headers);
        assert_eq!(decoded.raw_body_bytes, raw_body);

        let mut stream = Vec::new();
        write_raw_warc_frame(&mut stream, uri, headers, raw_body).unwrap();
        write_raw_warc_frame(&mut stream, b"", b"", b"").unwrap();
        let mut cursor = Cursor::new(stream);
        let f1 = read_raw_warc_frame(&mut cursor, DEFAULT_MAX_FRAME_BYTES)
            .unwrap()
            .unwrap();
        assert_eq!(f1.raw_body_bytes, raw_body);
        let f2 = read_raw_warc_frame(&mut cursor, DEFAULT_MAX_FRAME_BYTES)
            .unwrap()
            .unwrap();
        assert!(f2.uri.is_empty() && f2.headers.is_empty() && f2.raw_body_bytes.is_empty());
        assert!(
            read_raw_warc_frame(&mut cursor, DEFAULT_MAX_FRAME_BYTES)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn rejects_truncated_oversized_and_mismatched_frames() {
        let wire = encode_raw_warc_frame(b"https://www.w3.org/", b"K: V\r\n", b"hello").unwrap();
        assert!(matches!(
            decode_raw_warc_frame(&wire[..wire.len() - 1], DEFAULT_MAX_FRAME_BYTES),
            Err(FrameError::TruncatedFrame { .. })
        ));
        assert!(matches!(
            decode_raw_warc_frame(&wire, 8),
            Err(FrameError::PayloadTooLarge { .. })
        ));
        let mut corrupted = wire.clone();
        // Corrupt body_len (last 4 bytes before "hello") from 5 -> 3
        let body_len_pos = corrupted.len() - 5 - 4;
        corrupted[body_len_pos..body_len_pos + 4].copy_from_slice(&3u32.to_be_bytes());
        assert!(matches!(
            decode_raw_warc_frame(&corrupted, DEFAULT_MAX_FRAME_BYTES),
            Err(FrameError::TotalLengthMismatch { .. })
        ));
    }

    #[test]
    fn roundtrips_frame_exceeding_4_mib_payload_under_64_mib_cap() {
        let mut payload = vec![b'x'; 5 * 1024 * 1024 + 123];
        payload[0..6].copy_from_slice(b"<html>");
        let uri = b"https://en.wikipedia.org/wiki/Archive";
        let headers = b"Content-Type: text/html; charset=utf-8\r\n";
        let encoded =
            encode_raw_warc_frame(uri, headers, &payload).expect("5 MiB frame fits 64 MiB cap");
        assert!(encoded.len() > 4 * 1024 * 1024);
        let decoded = decode_raw_warc_frame(&encoded, DEFAULT_MAX_FRAME_BYTES).expect("decode ok");
        assert_eq!(decoded.uri, uri);
        assert_eq!(decoded.headers, headers);
        assert_eq!(decoded.raw_body_bytes.len(), payload.len());
    }
}
