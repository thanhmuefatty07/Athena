//! Transparent lexical BM25, not an INT8 neural reranker.
//! Query-only term statistics; full ranking is O(text_bytes + N log N + Q log Q).
use crate::projection::Chunk;
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
}
fn lowercase(word: &str) -> Cow<'_, str> {
    if word
        .bytes()
        .all(|b| b.is_ascii() && !b.is_ascii_uppercase())
    {
        Cow::Borrowed(word)
    } else {
        Cow::Owned(word.to_lowercase())
    }
}

pub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let mut terms: Vec<_> = words(query).map(str::to_lowercase).collect();
    terms.sort_unstable();
    terms.dedup();
    if terms.is_empty() {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    }
    let term_ids: HashMap<_, _> = terms
        .iter()
        .enumerate()
        .map(|(i, term)| (term.as_str(), i))
        .collect();
    let mut postings = vec![Vec::<(usize, usize)>::new(); terms.len()];
    let mut counts = vec![0usize; terms.len()];
    let mut touched = Vec::new();
    let mut lengths = Vec::with_capacity(chunks.len());
    for (i, chunk) in chunks.iter().enumerate() {
        let mut length = 0usize;
        for word in words(&chunk.text) {
            length += 1;
            if let Some(&term) = term_ids.get(lowercase(word).as_ref()) {
                if counts[term] == 0 {
                    touched.push(term);
                }
                counts[term] += 1;
            }
        }
        lengths.push(length);
        for term in touched.drain(..) {
            postings[term].push((i, counts[term]));
            counts[term] = 0;
        }
    }
    let total_len: usize = lengths.iter().sum();
    let avg_len = (total_len as f64 / chunks.len() as f64).max(1.0);
    let norms: Vec<_> = lengths
        .iter()
        .map(|&len| k1 * (1.0 - b + b * len as f64 / avg_len))
        .collect();
    let n = chunks.len() as f64;
    let mut scores: Vec<_> = (0..chunks.len()).map(|i| (i, 0.0)).collect();
    for matches in postings {
        let df = matches.len() as f64;
        let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
        for (i, frequency) in matches {
            let freq = frequency as f64;
            scores[i].1 += idf * freq * (k1 + 1.0) / (freq + norms[i]);
        }
    }
    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    scores
}

pub fn highlight_indices(chunks: &[Chunk], query: &str, limit: usize) -> Vec<usize> {
    if limit == 0 {
        return Vec::new();
    }
    let scores = bm25(chunks, query, 1.2, 0.75);
    let mut selected = Vec::new();
    let mut seen = HashSet::<String>::new();
    for (index, score) in scores {
        if selected.len() >= limit || score <= 0.0 {
            break;
        }
        // Simple novelty gate; semantic MMR is a separate, benchmarked enhancement.
        let fingerprint = chunks[index].text.to_lowercase();
        if seen.insert(fingerprint) {
            selected.push(index);
        }
    }
    selected
}

#[cfg(test)]
mod tests {
    use super::*;
    fn chunk(text: &str) -> Chunk {
        Chunk {
            node: 0,
            role: "paragraph",
            text: text.into(),
            links: vec![],
        }
    }
    #[test]
    fn empty_query_and_ties_follow_document_order() {
        let docs = vec![chunk("Rust"), chunk("rust"), chunk("unrelated")];
        assert_eq!(
            bm25(&docs, "!?", 1.2, 0.75),
            vec![(0, 0.0), (1, 0.0), (2, 0.0)]
        );
        assert_eq!(highlight_indices(&docs, "RUST rust", 10), vec![0]);
        assert_eq!(highlight_indices(&docs, "rust", 0), Vec::<usize>::new());
    }
    #[test]
    fn bm25_matches_hand_calculated_term_statistics() {
        let docs = vec![chunk("Rust rust"), chunk("rust tokio"), chunk("unrelated")];
        let scores = bm25(&docs, "RUST rust", 1.2, 0.75);
        assert_eq!(
            scores.iter().map(|x| x.0).collect::<Vec<_>>(),
            vec![0, 1, 2]
        );
        // N=3, df=2, average length=5/3; repeated query terms count once.
        let idf = (1.0_f64 + 1.5 / 2.5).ln();
        assert!((scores[0].1 - idf * 4.4 / 3.38).abs() < 1e-12);
        assert!((scores[1].1 - idf * 2.2 / 2.38).abs() < 1e-12);
        assert_eq!(scores[2].1, 0.0);
    }
    #[test]
    fn unicode_case_mapping_and_length_normalization_are_preserved() {
        let docs = vec![chunk("TÀI liệu 内容"), chunk("TÀI"), chunk("nothing")];
        assert_eq!(highlight_indices(&docs, "tài", 2), vec![1, 0]);
    }
    #[test]
    fn relevant_text_ranks_first() {
        let x = vec![
            Chunk {
                node: 1,
                role: "paragraph",
                text: "Dogs run in parks".into(),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "paragraph",
                text: "Rust async tokio runtime".into(),
                links: vec![],
            },
        ];
        assert_eq!(highlight_indices(&x, "tokio runtime", 1), vec![1]);
    }
}
