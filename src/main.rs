//! Offline deterministic demonstrator: run with `cargo run -- file.html https://example.org/ [query]`
use athena_core_v2::projection::ParseLimits;
use athena_core_v2::{aae::export_jsonl, projection::parse_html};
use std::{
    env, fs,
    io::{self, Read},
    process,
};
use url::Url;

fn read_html_bounded(input: impl Read, max_bytes: usize) -> io::Result<String> {
    let read_limit = u64::try_from(max_bytes)
        .ok()
        .and_then(|limit| limit.checked_add(1))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "input limit is too large"))?;
    let mut input = input.take(read_limit);
    let mut bytes = Vec::new();
    input.read_to_end(&mut bytes)?;
    if bytes.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("Input exceeds {max_bytes}-byte limit"),
        ));
    }
    String::from_utf8(bytes).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("Usage: athena-core-v2 <file.html> <https-base-url> [query]");
        process::exit(2);
    }
    let html = fs::File::open(&args[1])
        .and_then(|input| read_html_bounded(input, ParseLimits::default().max_html_bytes))
        .unwrap_or_else(|error| {
            eprintln!("{error}");
            process::exit(2);
        });
    let base = Url::parse(&args[2]).expect("Invalid base URL");
    let page = parse_html(&html, &base, 250_000, 1200).expect("DOM parser limit exceeded");
    let out = export_jsonl(&page, &base, 0, args.get(3).map(String::as_str), 5)
        .expect("Could not serialize AAE");
    print!("{out}");
}

#[cfg(test)]
mod tests {
    use super::read_html_bounded;
    use std::io::{self, Read};

    struct RepeatingReader {
        remaining: usize,
        bytes_read: usize,
    }

    impl Read for RepeatingReader {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            let count = buffer.len().min(self.remaining);
            buffer[..count].fill(b'x');
            self.remaining -= count;
            self.bytes_read += count;
            Ok(count)
        }
    }

    #[test]
    fn bounded_reader_stops_after_the_single_oversize_probe_byte() {
        let limit = 17;
        let mut input = RepeatingReader {
            remaining: limit + 100,
            bytes_read: 0,
        };
        let error = read_html_bounded(&mut input, limit).expect_err("oversized input must fail");
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert_eq!(input.bytes_read, limit + 1);
        assert_eq!(error.to_string(), "Input exceeds 17-byte limit");
    }

    #[test]
    fn bounded_reader_preserves_valid_input_at_the_limit() {
        assert_eq!(read_html_bounded(b"ok".as_slice(), 2).unwrap(), "ok");
    }
}
