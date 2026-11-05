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

pub fn role_weight(role: &str) -> f64 {
    match role {
        "heading" => 2.2,
        "table_header" => 1.6,
        "preformatted" => 1.15,
        _ => 1.0,
    }
}

type PreparedPostingList = (Box<str>, Box<[(u32, u32)]>);

#[derive(Debug, Clone)]
pub struct PreparedRank {
    terms: Vec<PreparedPostingList>,
    lengths: Vec<u32>,
    fingerprints: Vec<Box<str>>,
    avg_len: f64,
    retained_bytes: usize,
}

impl PreparedRank {
    pub fn new(chunks: &[Chunk]) -> Self {
        let mut vocab = std::collections::BTreeMap::<String, Vec<(u32, u32)>>::new();
        let mut lengths = Vec::with_capacity(chunks.len());
        let mut fingerprints = Vec::with_capacity(chunks.len());
        let mut total_len = 0usize;
        for (doc_idx, chunk) in chunks.iter().enumerate() {
            let doc_id = doc_idx as u32;
            let mut len = 0u32;
            for word in words(&chunk.text) {
                len = len.saturating_add(1);
                let key = lowercase(word);
                if let Some(postings) = vocab.get_mut(key.as_ref()) {
                    if let Some((last_doc, freq)) = postings.last_mut()
                        && *last_doc == doc_id
                    {
                        *freq = freq.saturating_add(1);
                    } else {
                        postings.push((doc_id, 1));
                    }
                } else {
                    vocab.insert(key.into_owned(), vec![(doc_id, 1)]);
                }
            }
            total_len = total_len.saturating_add(len as usize);
            lengths.push(len);
            fingerprints.push(chunk.text.to_lowercase().into_boxed_str());
        }
        let mut retained_bytes = lengths.len() * std::mem::size_of::<u32>()
            + fingerprints
                .iter()
                .map(|fp| std::mem::size_of::<Box<str>>() + fp.len())
                .sum::<usize>();
        let mut terms = Vec::with_capacity(vocab.len());
        for (term, list) in vocab {
            let boxed_term = term.into_boxed_str();
            let boxed_list = list.into_boxed_slice();
            retained_bytes = retained_bytes
                .saturating_add(std::mem::size_of::<(Box<str>, Box<[(u32, u32)]>)>())
                .saturating_add(boxed_term.len())
                .saturating_add(boxed_list.len() * std::mem::size_of::<(u32, u32)>());
            terms.push((boxed_term, boxed_list));
        }
        let avg_len = if chunks.is_empty() {
            1.0
        } else {
            (total_len as f64 / chunks.len() as f64).max(1.0)
        };
        Self {
            terms,
            lengths,
            fingerprints,
            avg_len,
            retained_bytes,
        }
    }

    pub fn retained_payload_bytes(&self) -> usize {
        self.retained_bytes
    }

    pub fn scores(&self, query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {
        if self.lengths.is_empty() {
            return Vec::new();
        }
        let mut qterms: Vec<_> = words(query).map(|w| lowercase(w).into_owned()).collect();
        qterms.sort_unstable();
        qterms.dedup();
        if qterms.is_empty() {
            return (0..self.lengths.len()).map(|i| (i, 0.0)).collect();
        }
        let n = self.lengths.len() as f64;
        let mut ranked: Vec<_> = (0..self.lengths.len()).map(|i| (i, 0.0)).collect();
        for q in &qterms {
            if let Ok(idx) = self
                .terms
                .binary_search_by(|(term, _)| term.as_ref().cmp(q.as_str()))
            {
                let postings = &self.terms[idx].1;
                let df = postings.len() as f64;
                let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
                for &(doc_id, frequency) in postings.iter() {
                    let i = doc_id as usize;
                    let freq = frequency as f64;
                    let norm = k1 * (1.0 - b + b * self.lengths[i] as f64 / self.avg_len);
                    ranked[i].1 += idf * freq * (k1 + 1.0) / (freq + norm);
                }
            }
        }
        ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
        ranked
    }

    pub fn top_indices(&self, query: &str, limit: usize) -> Vec<usize> {
        if limit == 0 || self.lengths.is_empty() {
            return Vec::new();
        }
        let ranked = self.scores(query, 1.2, 0.75);
        let mut selected = Vec::new();
        let mut seen = HashSet::<&str>::new();
        for (index, score) in ranked {
            if selected.len() >= limit || score <= 0.0 {
                break;
            }
            if seen.insert(self.fingerprints[index].as_ref()) {
                selected.push(index);
            }
        }
        selected
    }
}

struct QueryFilter {
    min_len: usize,
    max_len: usize,
    first_mask: [u64; 4],
    last_mask: [u64; 4],
}

impl QueryFilter {
    fn new(qterms: &[String]) -> Self {
        let mut min_len = usize::MAX;
        let mut max_len = 0usize;
        let mut first_mask = [0u64; 4];
        let mut last_mask = [0u64; 4];
        for term in qterms {
            let bytes = term.as_bytes();
            if bytes.is_empty() {
                continue;
            }
            min_len = min_len.min(bytes.len());
            max_len = max_len.max(bytes.len());
            for b0 in [bytes[0], bytes[0].to_ascii_uppercase()] {
                first_mask[(b0 >> 6) as usize] |= 1u64 << (b0 & 63);
            }
            let last = bytes[bytes.len() - 1];
            for b1 in [last, last.to_ascii_uppercase()] {
                last_mask[(b1 >> 6) as usize] |= 1u64 << (b1 & 63);
            }
        }
        Self {
            min_len,
            max_len,
            first_mask,
            last_mask,
        }
    }

    #[inline(always)]
    fn match_term(&self, word: &str, qterms: &[String]) -> Option<usize> {
        let raw = word.as_bytes();
        if raw.is_empty() {
            return None;
        }
        if (raw[0] < 128
            && (self.first_mask[(raw[0] >> 6) as usize] & (1u64 << (raw[0] & 63))) == 0)
            || (raw[raw.len() - 1] < 128
                && (self.last_mask[(raw[raw.len() - 1] >> 6) as usize]
                    & (1u64 << (raw[raw.len() - 1] & 63)))
                    == 0)
        {
            return None;
        }
        let is_ascii = word.is_ascii();
        if is_ascii && !(self.min_len..=self.max_len).contains(&raw.len()) {
            return None;
        }
        let key = lowercase(word);
        let bytes = key.as_bytes();
        if !is_ascii
            && (!(self.min_len..=self.max_len).contains(&bytes.len())
                || (self.first_mask[(bytes[0] >> 6) as usize] & (1u64 << (bytes[0] & 63))) == 0
                || (self.last_mask[(bytes[bytes.len() - 1] >> 6) as usize]
                    & (1u64 << (bytes[bytes.len() - 1] & 63)))
                    == 0)
        {
            return None;
        }
        qterms
            .binary_search_by(|t| t.as_str().cmp(key.as_ref()))
            .ok()
    }
}

#[inline(always)]
fn score_chunks_bm25f(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {
    score_chunks_bm25f_with_delta(chunks, query, k1, b, 0.0)
}

#[inline(always)]
fn score_chunks_bm25f_with_delta(
    chunks: &[Chunk],
    query: &str,
    k1: f64,
    b: f64,
    delta: f64,
) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let mut qterms: Vec<_> = words(query).map(|w| lowercase(w).into_owned()).collect();
    qterms.sort_unstable();
    qterms.dedup();
    if qterms.is_empty() {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    }
    let filter = QueryFilter::new(&qterms);
    let mut postings = vec![Vec::<(usize, usize)>::new(); qterms.len()];
    let mut counts = vec![0usize; qterms.len()];
    let mut window_counts = vec![0usize; qterms.len()];
    let mut touched = Vec::new();
    let mut doc_matches = Vec::<(usize, usize)>::new();
    let mut lengths = Vec::with_capacity(chunks.len());
    let mut min_span = vec![usize::MAX; chunks.len()];
    let mut matched_distinct = vec![0usize; chunks.len()];
    let mut active_heading_hits = vec![0usize; qterms.len()];
    let mut heading_context_boost = vec![0.0f64; chunks.len()];
    for (doc_idx, chunk) in chunks.iter().enumerate() {
        let mut length = 0usize;
        doc_matches.clear();
        for word in words(&chunk.text) {
            length += 1;
            if let Some(term) = filter.match_term(word, &qterms) {
                if counts[term] == 0 {
                    touched.push(term);
                }
                counts[term] += 1;
                doc_matches.push((length, term));
            }
        }
        lengths.push(length);
        let distinct = touched.len();
        matched_distinct[doc_idx] = distinct;
        if distinct >= 2 {
            let mut covered = 0usize;
            let mut left = 0usize;
            let mut best_span = usize::MAX;
            for &(r_pos, r_term) in &doc_matches {
                if window_counts[r_term] == 0 {
                    covered += 1;
                }
                window_counts[r_term] += 1;
                while covered == distinct {
                    let (l_pos, l_term) = doc_matches[left];
                    let span = r_pos - l_pos + 1;
                    if span < best_span {
                        best_span = span;
                    }
                    window_counts[l_term] -= 1;
                    if window_counts[l_term] == 0 {
                        covered -= 1;
                    }
                    left += 1;
                }
            }
            for &(_, term) in &doc_matches[left..] {
                window_counts[term] = 0;
            }
            min_span[doc_idx] = best_span;
        }
        if chunk.role == "heading" {
            active_heading_hits.fill(0);
            for &term in &touched {
                active_heading_hits[term] = counts[term];
            }
        } else if !touched.is_empty() {
            let inherited: usize = active_heading_hits.iter().copied().sum();
            if inherited > 0 {
                heading_context_boost[doc_idx] = 0.08 * (inherited.min(3) as f64);
            }
        }
        for term in touched.drain(..) {
            postings[term].push((doc_idx, counts[term]));
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
    let mut ranked: Vec<_> = (0..chunks.len()).map(|i| (i, 0.0)).collect();
    for matches in postings {
        let df = matches.len() as f64;
        let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
        for (doc_idx, frequency) in matches {
            let weight = role_weight(chunks[doc_idx].role);
            let freq = frequency as f64 * weight;
            let tf_gain = freq * (k1 + 1.0) / (freq + norms[doc_idx]);
            ranked[doc_idx].1 += idf
                * if delta > 0.0 {
                    tf_gain + delta
                } else {
                    tf_gain
                };
        }
    }
    for (doc_idx, entry) in ranked.iter_mut().enumerate() {
        if entry.1 > 0.0 {
            let distinct = matched_distinct[doc_idx];
            if distinct >= 2 && min_span[doc_idx] < usize::MAX {
                let span = min_span[doc_idx].max(distinct);
                let slack = (span - distinct) as f64;
                entry.1 *= 1.0 + 0.20 / (1.0 + slack);
            }
            if chunks[doc_idx].role == "heading" {
                entry.1 *= 1.35;
            } else {
                entry.1 *= 1.0 + heading_context_boost[doc_idx];
            }
        }
    }
    ranked
}

pub fn bm25f(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {
    let mut ranked = score_chunks_bm25f(chunks, query, k1, b);
    ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    ranked
}

pub fn bm25f_highlight_indices(chunks: &[Chunk], query: &str, limit: usize) -> Vec<usize> {
    if limit == 0 {
        return Vec::new();
    }
    let mut ranked = score_chunks_bm25f(chunks, query, 1.2, 0.75);
    ranked.retain(|(_, score)| *score > 0.0);
    if ranked.is_empty() {
        return Vec::new();
    }
    let cmp =
        |(a, sa): &(usize, f64), (b, sb): &(usize, f64)| sb.total_cmp(sa).then_with(|| a.cmp(b));
    let cap = (limit * 4).max(16).min(ranked.len());
    if ranked.len() > cap {
        ranked.select_nth_unstable_by(cap - 1, cmp);
    }
    ranked[..cap].sort_unstable_by(cmp);
    let mut selected = Vec::with_capacity(limit.min(ranked.len()));
    let mut seen = HashSet::<String>::new();
    for &(index, _) in &ranked[..cap] {
        if selected.len() >= limit {
            return selected;
        }
        let fingerprint = chunks[index].text.to_lowercase();
        if seen.insert(fingerprint) {
            selected.push(index);
        }
    }
    if ranked.len() > cap {
        ranked[cap..].sort_unstable_by(cmp);
        for &(index, _) in &ranked[cap..] {
            if selected.len() >= limit {
                break;
            }
            let fingerprint = chunks[index].text.to_lowercase();
            if seen.insert(fingerprint) {
                selected.push(index);
            }
        }
    }
    selected
}

type TermPostings = (Vec<Vec<(usize, usize)>>, Vec<usize>, Vec<usize>);

fn collect_term_postings(chunks: &[Chunk], query: &str) -> Option<TermPostings> {
    let mut qterms: Vec<_> = words(query).map(|w| lowercase(w).into_owned()).collect();
    qterms.sort_unstable();
    qterms.dedup();
    if qterms.is_empty() {
        return None;
    }
    let filter = QueryFilter::new(&qterms);
    let mut postings = vec![Vec::<(usize, usize)>::new(); qterms.len()];
    let mut cf = vec![0usize; qterms.len()];
    let mut counts = vec![0usize; qterms.len()];
    let mut touched = Vec::new();
    let mut doc_lengths = Vec::with_capacity(chunks.len());
    for (doc_idx, chunk) in chunks.iter().enumerate() {
        let mut doc_len = 0usize;
        for word in words(&chunk.text) {
            doc_len += 1;
            if let Some(term) = filter.match_term(word, &qterms) {
                if counts[term] == 0 {
                    touched.push(term);
                }
                counts[term] += 1;
            }
        }
        doc_lengths.push(doc_len);
        for term in touched.drain(..) {
            postings[term].push((doc_idx, counts[term]));
            cf[term] += counts[term];
            counts[term] = 0;
        }
    }
    Some((postings, cf, doc_lengths))
}

pub fn bm25_plus(chunks: &[Chunk], query: &str, k1: f64, b: f64, delta: f64) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let Some((postings, _cf, doc_lengths)) = collect_term_postings(chunks, query) else {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    };
    let total_len: usize = doc_lengths.iter().sum();
    let avg_len = (total_len as f64 / chunks.len() as f64).max(1.0);
    let doc_norms: Vec<_> = doc_lengths
        .iter()
        .map(|&len| k1 * (1.0 - b + b * len as f64 / avg_len))
        .collect();
    let n = chunks.len() as f64;
    let mut ranked: Vec<_> = (0..chunks.len()).map(|i| (i, 0.0)).collect();
    for matches in postings {
        let df = matches.len() as f64;
        let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
        for (doc_idx, frequency) in matches {
            let freq = frequency as f64;
            ranked[doc_idx].1 += idf * (freq * (k1 + 1.0) / (freq + doc_norms[doc_idx]) + delta);
        }
    }
    ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    ranked
}

pub fn bm25l(chunks: &[Chunk], query: &str, k1: f64, b: f64, delta: f64) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let Some((postings, _cf, doc_lengths)) = collect_term_postings(chunks, query) else {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    };
    let total_len: usize = doc_lengths.iter().sum();
    let avg_len = (total_len as f64 / chunks.len() as f64).max(1.0);
    let len_norms: Vec<_> = doc_lengths
        .iter()
        .map(|&len| (1.0 - b + b * len as f64 / avg_len).max(1e-9))
        .collect();
    let n = chunks.len() as f64;
    let mut ranked: Vec<_> = (0..chunks.len()).map(|i| (i, 0.0)).collect();
    for matches in postings {
        let df = matches.len() as f64;
        let idf = (1.0 + (n - df + 0.5) / (df + 0.5)).ln();
        for (doc_idx, frequency) in matches {
            let c_prime = (frequency as f64) / len_norms[doc_idx];
            if c_prime > 0.0 {
                let shifted = c_prime + delta;
                ranked[doc_idx].1 += idf * (k1 + 1.0) * shifted / (k1 + shifted);
            }
        }
    }
    ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    ranked
}

pub fn ql_dirichlet(chunks: &[Chunk], query: &str, mu: f64) -> Vec<(usize, f64)> {
    if chunks.is_empty() {
        return Vec::new();
    }
    let Some((postings, cf, doc_lengths)) = collect_term_postings(chunks, query) else {
        return chunks.iter().enumerate().map(|(i, _)| (i, 0.0)).collect();
    };
    let total_tokens = doc_lengths.iter().sum::<usize>().max(1) as f64;
    let mu_prior = mu.max(1.0);
    let mut ranked: Vec<_> = (0..chunks.len()).map(|i| (i, 0.0)).collect();
    for (term_idx, matches) in postings.into_iter().enumerate() {
        if cf[term_idx] == 0 {
            continue;
        }
        let p_collection = (cf[term_idx] as f64 / total_tokens).max(1e-12);
        for (doc_idx, frequency) in matches {
            let gain = (1.0 + (frequency as f64) / (mu_prior * p_collection)).ln();
            let len_penalty = 1.0 + (1.0 + doc_lengths[doc_idx] as f64 / mu_prior).ln();
            ranked[doc_idx].1 += gain / len_penalty;
        }
    }
    ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    ranked
}

pub fn simhash_64(text: &str) -> u64 {
    let mut weights = [0i32; 64];
    for word in words(text) {
        let mut h = 0xcbf29ce484222325u64;
        for b in lowercase(word).bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        // SplitMix64 avalanche so token bits distribute uniformly across all 64 dimensions.
        h = (h ^ (h >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        h = (h ^ (h >> 27)).wrapping_mul(0x94d049bb133111eb);
        h ^= h >> 31;
        for (bit, slot) in weights.iter_mut().enumerate() {
            if (h >> bit) & 1 == 1 {
                *slot += 1;
            } else {
                *slot -= 1;
            }
        }
    }
    let mut fingerprint = 0u64;
    for (bit, &w) in weights.iter().enumerate() {
        if w > 0 {
            fingerprint |= 1u64 << bit;
        }
    }
    fingerprint
}

pub fn bm25f_simhash_highlight_indices(
    chunks: &[Chunk],
    query: &str,
    limit: usize,
    max_hamming: u32,
) -> Vec<usize> {
    if limit == 0 {
        return Vec::new();
    }
    let mut ranked = score_chunks_bm25f(chunks, query, 1.2, 0.75);
    ranked.retain(|(_, score)| *score > 0.0);
    if ranked.is_empty() {
        return Vec::new();
    }
    let cmp =
        |(a, sa): &(usize, f64), (b, sb): &(usize, f64)| sb.total_cmp(sa).then_with(|| a.cmp(b));
    let cap = (limit * 8).max(32).min(ranked.len());
    if ranked.len() > cap {
        ranked.select_nth_unstable_by(cap - 1, cmp);
    }
    ranked[..cap].sort_unstable_by(cmp);
    let mut selected = Vec::with_capacity(limit.min(ranked.len()));
    let mut seen_exact = HashSet::<String>::new();
    let mut seen_sketches = Vec::<u64>::new();
    let mut try_select = |index: usize, selected: &mut Vec<usize>| -> bool {
        let exact = chunks[index].text.to_lowercase();
        if !seen_exact.insert(exact) {
            return false;
        }
        let sketch = simhash_64(&chunks[index].text);
        if seen_sketches
            .iter()
            .any(|&prev| (prev ^ sketch).count_ones() <= max_hamming)
        {
            return false;
        }
        seen_sketches.push(sketch);
        selected.push(index);
        selected.len() >= limit
    };
    for &(index, _) in &ranked[..cap] {
        if try_select(index, &mut selected) {
            return selected;
        }
    }
    if ranked.len() > cap {
        ranked[cap..].sort_unstable_by(cmp);
        for &(index, _) in &ranked[cap..] {
            if try_select(index, &mut selected) {
                break;
            }
        }
    }
    selected
}

pub fn bm25f_plus_mslp(
    chunks: &[Chunk],
    query: &str,
    k1: f64,
    b: f64,
    delta: f64,
) -> Vec<(usize, f64)> {
    let mut ranked = score_chunks_bm25f_with_delta(chunks, query, k1, b, delta);
    ranked.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
    ranked
}

pub fn bm25f_plus_mslp_simhash_highlight_indices(
    chunks: &[Chunk],
    query: &str,
    limit: usize,
    delta: f64,
    max_hamming: u32,
) -> Vec<usize> {
    if limit == 0 {
        return Vec::new();
    }
    let mut ranked = score_chunks_bm25f_with_delta(chunks, query, 1.2, 0.75, delta);
    ranked.retain(|(_, score)| *score > 0.0);
    if ranked.is_empty() {
        return Vec::new();
    }
    let cmp =
        |(a, sa): &(usize, f64), (b, sb): &(usize, f64)| sb.total_cmp(sa).then_with(|| a.cmp(b));
    let cap = (limit * 8).max(32).min(ranked.len());
    if ranked.len() > cap {
        ranked.select_nth_unstable_by(cap - 1, cmp);
    }
    ranked[..cap].sort_unstable_by(cmp);
    let mut selected = Vec::with_capacity(limit.min(ranked.len()));
    let mut seen_exact = HashSet::<String>::new();
    let mut seen_sketches = Vec::<u64>::new();
    let mut try_select = |index: usize, selected: &mut Vec<usize>| -> bool {
        let exact = chunks[index].text.to_lowercase();
        if !seen_exact.insert(exact) {
            return false;
        }
        let sketch = simhash_64(&chunks[index].text);
        if seen_sketches
            .iter()
            .any(|&prev| (prev ^ sketch).count_ones() <= max_hamming)
        {
            return false;
        }
        seen_sketches.push(sketch);
        selected.push(index);
        selected.len() >= limit
    };
    for &(index, _) in &ranked[..cap] {
        if try_select(index, &mut selected) {
            return selected;
        }
    }
    if ranked.len() > cap {
        ranked[cap..].sort_unstable_by(cmp);
        for &(index, _) in &ranked[cap..] {
            if try_select(index, &mut selected) {
                break;
            }
        }
    }
    selected
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
    #[test]
    fn prepared_rank_matches_one_shot_bm25_and_highlights_across_queries() {
        let docs = vec![
            chunk("Rust rust systems programming"),
            chunk("rust tokio async runtime"),
            chunk("RUST rust Systems Programming"),
            chunk("unrelated paragraph"),
        ];
        let prepared = PreparedRank::new(&docs);
        assert!(prepared.retained_payload_bytes() > 0);
        for q in ["rust", "tokio async", "!?", "systems runtime"] {
            let expected = bm25(&docs, q, 1.2, 0.75);
            let actual = prepared.scores(q, 1.2, 0.75);
            assert_eq!(expected.len(), actual.len());
            for ((ea, es), (aa, ascore)) in expected.iter().zip(actual.iter()) {
                assert_eq!(ea, aa);
                assert!((es - ascore).abs() < 1e-12);
            }
            assert_eq!(highlight_indices(&docs, q, 3), prepared.top_indices(q, 3));
        }
    }
    #[test]
    fn bm25f_boosts_headings_and_table_headers_above_equal_length_paragraphs() {
        let docs = vec![
            Chunk {
                node: 1,
                role: "paragraph",
                text: "distributed consensus protocol overview".into(),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "table_header",
                text: "distributed consensus protocol benchmark".into(),
                links: vec![],
            },
            Chunk {
                node: 3,
                role: "heading",
                text: "distributed consensus protocol specification".into(),
                links: vec![],
            },
        ];
        let ranked = bm25f_highlight_indices(&docs, "distributed consensus", 3);
        assert_eq!(ranked, vec![2, 1, 0]);
    }
    #[test]
    fn bm25_plus_bm25l_ql_dirichlet_and_simhash_novelty_deduplication_work() {
        let filler = "background context token ".repeat(60);
        let docs = vec![
            Chunk {
                node: 1,
                role: "paragraph",
                text: format!("Byzantine fault tolerance quorum safety {filler}"),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "paragraph",
                text: "Byzantine fault tolerance quorum safety".into(),
                links: vec![],
            },
            Chunk {
                node: 3,
                role: "paragraph",
                text: "Byzantine fault tolerance quorum safety!".into(),
                links: vec![],
            },
            Chunk {
                node: 4,
                role: "paragraph",
                text: "Completely unrelated gardening text".into(),
                links: vec![],
            },
        ];
        let classic = bm25(&docs, "byzantine quorum", 1.2, 0.75);
        let plus = bm25_plus(&docs, "byzantine quorum", 1.2, 0.75, 1.0);
        let l_scores = bm25l(&docs, "byzantine quorum", 1.2, 0.75, 0.5);
        let ql = ql_dirichlet(&docs, "byzantine quorum", 200.0);
        // Long relevant doc (index 0) retains a much higher relative score under BM25+ than classic BM25.
        let classic_long_ratio = classic[2].1 / classic[0].1;
        let plus_long_ratio = plus[2].1 / plus[0].1;
        assert!(plus_long_ratio > classic_long_ratio);
        assert_eq!(l_scores[3].1, 0.0);
        assert!(ql[0].1 > 0.0 && ql[3].1 == 0.0);

        // Near-duplicate doc 2 ("... safety") and doc 3 ("... safety!") differ only by punctuation,
        // so SimHash novelty deduplication suppresses the near-duplicate while exact lowercase dedup would keep both.
        let exact_only = bm25f_highlight_indices(&docs, "byzantine quorum", 3);
        assert!(exact_only.contains(&1) && exact_only.contains(&2));
        let simhash_dedup = bm25f_simhash_highlight_indices(&docs, "byzantine quorum", 3, 3);
        assert_eq!(simhash_dedup, vec![1, 0]);
    }

    #[test]
    fn rank_01_direct_heading_beats_inherited_paragraph_and_sliding_window_mslp() {
        // 1. Direct heading match must always outscore an identical-length paragraph with inherited heading_context_boost
        let heading_vs_para = vec![
            Chunk {
                node: 1,
                role: "heading",
                text: "byzantine quorum safety liveness".into(),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "paragraph",
                text: "byzantine quorum safety liveness".into(),
                links: vec![],
            },
        ];
        let scores = bm25f(
            &heading_vs_para,
            "byzantine quorum safety liveness",
            1.2,
            0.75,
        );
        assert_eq!(scores[0].0, 0);
        assert!(scores[0].1 > scores[1].1);

        // 2. Sliding-window MSLP: repeating a query term later in the chunk must not inflate minimal span
        let mslp_docs = vec![
            Chunk {
                node: 1,
                role: "paragraph",
                // "byzantine" at pos 1, "quorum" at pos 10 (minimal span = 10), "alpha" repeated
                text: "byzantine alpha alpha alpha alpha alpha alpha alpha alpha quorum alpha"
                    .into(),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "paragraph",
                // "byzantine quorum" adjacent at pos 1..2 (minimal span = 2), with "byzantine" repeated at pos 11
                text: "byzantine quorum alpha alpha alpha alpha alpha alpha alpha alpha byzantine"
                    .into(),
                links: vec![],
            },
        ];
        let mslp_ranked = bm25f(&mslp_docs, "byzantine quorum", 1.2, 0.75);
        assert_eq!(
            mslp_ranked[0].0, 1,
            "adjacent match with later repeated term must outscore wide-span match"
        );
    }

    #[test]
    fn bm25f_plus_mslp_simhash64_rescues_long_complete_chunk_and_suppresses_near_duplicates() {
        let filler = "metadata archive record entry ".repeat(45);
        let docs = vec![
            Chunk {
                node: 1,
                role: "heading",
                text: "Byzantine Quorum Consensus Handbook".into(),
                links: vec![],
            },
            Chunk {
                node: 2,
                role: "paragraph",
                text: format!("Byzantine quorum consensus safety liveness proof {filler}"),
                links: vec![],
            },
            Chunk {
                node: 3,
                role: "paragraph",
                text: format!("Byzantine quorum consensus safety liveness proof! {filler}"),
                links: vec![],
            },
            Chunk {
                node: 4,
                role: "paragraph",
                text: "Byzantine quorum short stub".into(),
                links: vec![],
            },
        ];
        let f_only = bm25f(
            &docs,
            "byzantine quorum consensus safety liveness",
            1.2,
            0.75,
        );
        let f_plus = bm25f_plus_mslp(
            &docs,
            "byzantine quorum consensus safety liveness",
            1.2,
            0.75,
            1.0,
        );
        // Under BM25F+MSLP+delta=1.0, the long paragraph matching all 5 query terms (doc 1)
        // receives a higher relative score against the short 3-term heading (doc 0).
        let f_only_ratio = f_only.iter().find(|(i, _)| *i == 1).unwrap().1
            / f_only.iter().find(|(i, _)| *i == 0).unwrap().1;
        let f_plus_ratio = f_plus.iter().find(|(i, _)| *i == 1).unwrap().1
            / f_plus.iter().find(|(i, _)| *i == 0).unwrap().1;
        assert!(f_plus_ratio > f_only_ratio);

        let selected = bm25f_plus_mslp_simhash_highlight_indices(
            &docs,
            "byzantine quorum consensus safety liveness",
            3,
            1.0,
            3,
        );
        assert!(selected.contains(&0));
        assert!(selected.contains(&1));
        assert!(
            !selected.contains(&2),
            "near-duplicate doc 2 must be suppressed by SimHash-64"
        );
        assert_eq!(selected.len(), 3);
    }
}
