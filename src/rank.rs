//! Transparent lexical BM25, not an INT8 neural reranker.
//! Top-k selection is O(total_tokens + N log k) on typical workloads.
use crate::projection::Chunk;
use std::collections::{HashMap, HashSet};

fn words(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|w| !w.is_empty())
        .map(|w| w.to_lowercase())
        .collect()
}

pub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let query: HashSet<_> = words(query).into_iter().collect();
    if query.is_empty() {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    }
    let doc_tokens: Vec<Vec<String>> = chunks.iter().map(|c| words(&c.text)).collect();
    let total_len: usize = doc_tokens.iter().map(Vec::len).sum();
    let avg_len = (total_len as f64 / chunks.len() as f64).max(1.0);
    let mut df: HashMap<&str, usize> = HashMap::new();
    for tokens in &doc_tokens {
        let distinct: HashSet<_> = tokens.iter().map(String::as_str).collect();
        for w in distinct {
            *df.entry(w).or_default() += 1;
        }
    }
    let n = chunks.len() as f64;
    let mut scores = Vec::with_capacity(chunks.len());
    for (i, tokens) in doc_tokens.iter().enumerate() {
        let mut tf: HashMap<&str, usize> = HashMap::new();
        for w in tokens {
            if query.contains(w) {
                *tf.entry(w).or_default() += 1;
            }
        }
        let mut score = 0.0;
        for w in &query {
            let freq = *tf.get(w.as_str()).unwrap_or(&0) as f64;
            if freq == 0.0 {
                continue;
            }
            let freq_docs = *df.get(w.as_str()).unwrap_or(&0) as f64;
            let idf = (1.0 + (n - freq_docs + 0.5) / (freq_docs + 0.5)).ln();
            let norm = freq + k1 * (1.0 - b + b * tokens.len() as f64 / avg_len);
            score += idf * freq * (k1 + 1.0) / norm;
        }
        scores.push((i, score));
    }
    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    scores
}

pub fn highlight_indices(chunks: &[Chunk], query: &str, limit: usize) -> Vec<usize> {
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
