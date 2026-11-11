#![forbid(unsafe_code)]

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::hash::Hash;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct UntruncatedLcsMetrics {
    pub pred_tokens: usize,
    pub gold_tokens: usize,
    pub token_lcs_len: usize,
    pub uax29_precision: f64,
    pub uax29_recall: f64,
    pub uax29_f1: f64,
    pub rouge_l_precision: f64,
    pub rouge_l_recall: f64,
    pub rouge_l_f1: f64,
    pub line_break_f1: f64,
}

#[inline]
fn is_combining_or_extend(ch: char) -> bool {
    let u = ch as u32;
    matches!(
        u,
        0x0300..=0x036F // Combining Diacritical Marks
        | 0x0483..=0x0489 // Cyrillic combining
        | 0x0591..=0x05BD | 0x05BF | 0x05C1..=0x05C2 | 0x05C4..=0x05C5 | 0x05C7 // Hebrew niqqud
        | 0x0610..=0x061A | 0x064B..=0x065F | 0x0670 | 0x06D6..=0x06ED // Arabic harakat
        | 0x0900..=0x0903 | 0x093A..=0x094F | 0x0951..=0x0957 | 0x0962..=0x0963 // Devanagari matras/virama
        | 0x0981..=0x0983 | 0x09BC | 0x09BE..=0x09CD | 0x09D7 | 0x09E2..=0x09E3 // Bengali
        | 0x0B82 | 0x0BBE..=0x0BCD | 0x0BD7 // Tamil
        | 0x0C00..=0x0C04 | 0x0C3E..=0x0C56 | 0x0C81..=0x0C83 | 0x0CBC..=0x0CD6 // Telugu/Kannada
        | 0x0D00..=0x0D03 | 0x0D3B..=0x0D4D | 0x0D57 // Malayalam
        | 0x0E31 | 0x0E34..=0x0E3A | 0x0E47..=0x0E4E // Thai combining
        | 0x0EB1 | 0x0EB4..=0x0EBC | 0x0EC8..=0x0ECD // Lao combining
        | 0x102B..=0x103E | 0x1056..=0x1059 | 0x105E..=0x1060 | 0x1062..=0x1064 | 0x1067..=0x106D | 0x1071..=0x1074 | 0x1082..=0x108D | 0x108F | 0x109A..=0x109D // Myanmar combining
        | 0x135D..=0x135F // Ethiopic combining
        | 0x17B4..=0x17D3 | 0x17DD // Khmer combining
        | 0x1AB0..=0x1AFF | 0x1DC0..=0x1DFF | 0x200C..=0x200D | 0x20D0..=0x20FF | 0x3099..=0x309A | 0xFE00..=0xFE0F | 0xFE20..=0xFE2F
    )
}

#[inline]
fn is_single_cluster_script_base(ch: char) -> bool {
    let u = ch as u32;
    matches!(
        u,
        0x3040..=0x3096 // Hiragana
        | 0x3400..=0x4DBF // CJK Ext A
        | 0x4E00..=0x9FFF // CJK Unified
        | 0xF900..=0xFAFF // CJK Compatibility
        | 0x20000..=0x2FA1F // CJK Ext B..F
        | 0x0E01..=0x0E30 | 0x0E32..=0x0E33 | 0x0E40..=0x0E46 // Thai base
        | 0x0E81..=0x0EB0 | 0x0EB2..=0x0EB3 | 0x0EC0..=0x0EC6 // Lao base
        | 0x1000..=0x102A | 0x103F | 0x1050..=0x1055 // Myanmar base
        | 0x1780..=0x17B3 // Khmer base
    )
}

#[inline]
fn is_katakana_word_char(ch: char) -> bool {
    let u = ch as u32;
    matches!(u, 0x30A0..=0x30FF | 0x31F0..=0x31FF | 0xFF66..=0xFF9F)
}

#[inline]
fn is_mid_word_connector(ch: char) -> bool {
    matches!(ch, '\'' | '\u{2019}' | '_')
}

/// Multilingual UAX #29-aligned word tokenizer covering all 12 world script families:
/// - Contiguous `ALetter`/`Numeric`/`ExtendNumLet` words with attached Indic/Semitic/Latin combining marks (`Extend`/`ZWJ`)
/// - Contiguous `Katakana` runs (`WB13`)
/// - Orthographic base+combining cluster units for unspaced CJK Han, Hiragana, Thai, Lao, Khmer, and Myanmar (`WB14`/SA)
pub fn uax29_word_tokens(text: &str) -> Vec<String> {
    #[derive(Clone, Copy, PartialEq, Eq)]
    enum RunKind {
        None,
        AlphaNum,
        Katakana,
        SingleCluster,
    }

    let chars: Vec<char> = text.chars().collect();
    let mut tokens = Vec::new();
    let mut cur = String::new();
    let mut kind = RunKind::None;

    let flush = |tokens: &mut Vec<String>, cur: &mut String, kind: &mut RunKind| {
        if !cur.is_empty() {
            tokens.push(std::mem::take(cur));
        }
        *kind = RunKind::None;
    };

    for (idx, &ch) in chars.iter().enumerate() {
        if is_combining_or_extend(ch) {
            if kind != RunKind::None {
                for lc in ch.to_lowercase() {
                    cur.push(lc);
                }
            }
            continue;
        }
        if is_single_cluster_script_base(ch) {
            flush(&mut tokens, &mut cur, &mut kind);
            for lc in ch.to_lowercase() {
                cur.push(lc);
            }
            kind = RunKind::SingleCluster;
            continue;
        }
        if is_katakana_word_char(ch) {
            if kind != RunKind::Katakana {
                flush(&mut tokens, &mut cur, &mut kind);
                kind = RunKind::Katakana;
            }
            for lc in ch.to_lowercase() {
                cur.push(lc);
            }
            continue;
        }
        if ch.is_alphanumeric() {
            if kind != RunKind::AlphaNum {
                flush(&mut tokens, &mut cur, &mut kind);
                kind = RunKind::AlphaNum;
            }
            for lc in ch.to_lowercase() {
                cur.push(lc);
            }
            continue;
        }
        if ch == '_' {
            if kind != RunKind::AlphaNum {
                flush(&mut tokens, &mut cur, &mut kind);
                kind = RunKind::AlphaNum;
            }
            cur.push('_');
            continue;
        }
        if is_mid_word_connector(ch)
            && kind == RunKind::AlphaNum
            && chars
                .get(idx + 1)
                .is_some_and(|next| next.is_alphanumeric())
        {
            cur.push('\'');
            continue;
        }
        flush(&mut tokens, &mut cur, &mut kind);
    }
    flush(&mut tokens, &mut cur, &mut kind);
    tokens
}

/// Exact untruncated multi-limb `u64` Hyyrö / Allison-Dosh bit-parallel token LCS length (`0` `1,200`-word cap).
///
/// Time complexity: $O(|A| + |B| + N_{\cap} \cdot \lceil M_{\cap} / 64 \rceil)$ where $M_{\cap} \le N_{\cap}$
/// are the shared-vocabulary filtered lengths.
pub fn exact_token_lcs_len<T: Eq + Hash>(a: &[T], b: &[T]) -> usize {
    if a.is_empty() || b.is_empty() {
        return 0;
    }
    let (smaller_raw, larger_raw) = if a.len() <= b.len() { (a, b) } else { (b, a) };

    let mut vocab: HashMap<&T, u32> = HashMap::with_capacity(smaller_raw.len());
    let mut next_id = 0u32;
    for tok in smaller_raw {
        vocab.entry(tok).or_insert_with(|| {
            let id = next_id;
            next_id += 1;
            id
        });
    }
    let mut present_in_large = vec![false; next_id as usize];
    let mut flarge: Vec<u32> = Vec::new();
    for tok in larger_raw {
        if let Some(&id) = vocab.get(tok) {
            present_in_large[id as usize] = true;
            flarge.push(id);
        }
    }
    if flarge.is_empty() {
        return 0;
    }
    let mut fsmall: Vec<u32> = Vec::with_capacity(smaller_raw.len());
    for tok in smaller_raw {
        let id = vocab[tok];
        if present_in_large[id as usize] {
            fsmall.push(id);
        }
    }
    if fsmall.is_empty() {
        return 0;
    }

    let (short, long_seq) = if fsmall.len() <= flarge.len() {
        (&fsmall[..], &flarge[..])
    } else {
        (&flarge[..], &fsmall[..])
    };

    let m = short.len();
    let limbs = m.div_ceil(64);
    let last_mask = if m % 64 == 0 {
        u64::MAX
    } else {
        (1u64 << (m % 64)) - 1
    };

    // Compact active symbol IDs in `short` -> contiguous rows of `limbs` u64 masks
    let mut dense_row: Vec<u32> = vec![u32::MAX; next_id as usize];
    let mut pmasks: Vec<u64> = Vec::new();
    let mut row_count = 0u32;
    for (pos, &sym) in short.iter().enumerate() {
        let row = if dense_row[sym as usize] == u32::MAX {
            let r = row_count;
            dense_row[sym as usize] = r;
            row_count += 1;
            pmasks.resize((row_count as usize) * limbs, 0u64);
            r
        } else {
            dense_row[sym as usize]
        };
        let base = (row as usize) * limbs;
        pmasks[base + (pos / 64)] |= 1u64 << (pos % 64);
    }

    let mut v = vec![u64::MAX; limbs];
    v[limbs - 1] = last_mask;

    for &sym in long_seq {
        let row = dense_row[sym as usize];
        if row == u32::MAX {
            continue;
        }
        let base = (row as usize) * limbs;
        let p = &pmasks[base..base + limbs];
        let mut carry = 0u64;
        for w in 0..limbs {
            let vw = v[w];
            let uw = vw & p[w];
            let (s1, c1) = vw.overflowing_add(uw);
            let (s2, c2) = s1.overflowing_add(carry);
            carry = u64::from(c1 | c2);
            // Since uw = vw & p[w], multi-word (V - U) has zero borrow and equals (vw ^ uw) limb-wise.
            v[w] = s2 | (vw ^ uw);
        }
        v[limbs - 1] &= last_mask;
    }

    let ones: usize = v.iter().map(|x| x.count_ones() as usize).sum();
    m - ones
}

/// Computes untruncated UAX #29 Bag-of-Tokens F1, exact untruncated ROUGE-L Token-LCS F1, and Line-Break Boundary F1.
pub fn compute_untruncated_uax29_metrics(
    pred_text: &str,
    gold_text: &str,
) -> UntruncatedLcsMetrics {
    let p_toks = uax29_word_tokens(pred_text);
    let g_toks = uax29_word_tokens(gold_text);

    if p_toks.is_empty() && g_toks.is_empty() {
        return UntruncatedLcsMetrics {
            pred_tokens: 0,
            gold_tokens: 0,
            token_lcs_len: 0,
            uax29_precision: 1.0,
            uax29_recall: 1.0,
            uax29_f1: 1.0,
            rouge_l_precision: 1.0,
            rouge_l_recall: 1.0,
            rouge_l_f1: 1.0,
            line_break_f1: 1.0,
        };
    }
    if p_toks.is_empty() || g_toks.is_empty() {
        return UntruncatedLcsMetrics {
            pred_tokens: p_toks.len(),
            gold_tokens: g_toks.len(),
            token_lcs_len: 0,
            uax29_precision: 0.0,
            uax29_recall: 0.0,
            uax29_f1: 0.0,
            rouge_l_precision: 0.0,
            rouge_l_recall: 0.0,
            rouge_l_f1: 0.0,
            line_break_f1: 0.0,
        };
    }

    let mut p_counts: HashMap<&str, usize> = HashMap::new();
    for t in &p_toks {
        *p_counts.entry(t.as_str()).or_insert(0) += 1;
    }
    let mut overlap = 0usize;
    for t in &g_toks {
        if let Some(cnt) = p_counts.get_mut(t.as_str())
            && *cnt > 0
        {
            *cnt -= 1;
            overlap += 1;
        }
    }
    let uax29_precision = overlap as f64 / p_toks.len() as f64;
    let uax29_recall = overlap as f64 / g_toks.len() as f64;
    let uax29_f1 = if uax29_precision + uax29_recall > 0.0 {
        (2.0 * uax29_precision * uax29_recall) / (uax29_precision + uax29_recall)
    } else {
        0.0
    };

    let lcs = exact_token_lcs_len(&p_toks, &g_toks);
    let rouge_l_precision = lcs as f64 / p_toks.len() as f64;
    let rouge_l_recall = lcs as f64 / g_toks.len() as f64;
    let rouge_l_f1 = if rouge_l_precision + rouge_l_recall > 0.0 {
        (2.0 * rouge_l_precision * rouge_l_recall) / (rouge_l_precision + rouge_l_recall)
    } else {
        0.0
    };

    let line_break_f1 = compute_line_break_f1(pred_text, gold_text);

    UntruncatedLcsMetrics {
        pred_tokens: p_toks.len(),
        gold_tokens: g_toks.len(),
        token_lcs_len: lcs,
        uax29_precision,
        uax29_recall,
        uax29_f1,
        rouge_l_precision,
        rouge_l_recall,
        rouge_l_f1,
        line_break_f1,
    }
}

fn compute_line_break_f1(pred_text: &str, gold_text: &str) -> f64 {
    fn boundary_pairs(txt: &str) -> Vec<(String, String)> {
        let non_empty_lines: Vec<Vec<String>> = txt
            .lines()
            .map(uax29_word_tokens)
            .filter(|v| !v.is_empty())
            .collect();
        let mut pairs = Vec::with_capacity(non_empty_lines.len().saturating_sub(1));
        for win in non_empty_lines.windows(2) {
            if let (Some(last_prev), Some(first_next)) = (win[0].last(), win[1].first()) {
                pairs.push((last_prev.clone(), first_next.clone()));
            }
        }
        pairs
    }

    let p_pairs = boundary_pairs(pred_text);
    let g_pairs = boundary_pairs(gold_text);
    if p_pairs.is_empty() && g_pairs.is_empty() {
        return 1.0;
    }
    if p_pairs.is_empty() || g_pairs.is_empty() {
        return 0.0;
    }
    let mut counts: HashMap<&(String, String), usize> = HashMap::new();
    for pair in &p_pairs {
        *counts.entry(pair).or_insert(0) += 1;
    }
    let mut ov = 0usize;
    for pair in &g_pairs {
        if let Some(c) = counts.get_mut(pair)
            && *c > 0
        {
            *c -= 1;
            ov += 1;
        }
    }
    let prec = ov as f64 / p_pairs.len() as f64;
    let rec = ov as f64 / g_pairs.len() as f64;
    if prec + rec > 0.0 {
        (2.0 * prec * rec) / (prec + rec)
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reference_dp_lcs<T: Eq>(a: &[T], b: &[T]) -> usize {
        let mut dp = vec![0usize; b.len() + 1];
        for x in a {
            let mut prev = 0usize;
            for (j, y) in b.iter().enumerate() {
                let cur = dp[j + 1];
                dp[j + 1] = if x == y { prev + 1 } else { cur.max(dp[j]) };
                prev = cur;
            }
        }
        dp[b.len()]
    }

    #[test]
    fn exact_bit_parallel_lcs_matches_reference_dp_across_64_bit_limb_boundaries() {
        for len in [0usize, 1, 2, 63, 64, 65, 127, 128, 129, 193] {
            let mut a = Vec::with_capacity(len);
            let mut b = Vec::with_capacity(len + 7);
            let mut state = 0x9E37_79B9_7F4A_7C15u64 ^ (len as u64);
            for _ in 0..len {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                a.push((state >> 60) as u8);
            }
            for _ in 0..(len + 7) {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1);
                b.push((state >> 60) as u8);
            }
            assert_eq!(
                exact_token_lcs_len(&a, &b),
                reference_dp_lcs(&a, &b),
                "limb mismatch at len={len}"
            );
        }
    }

    #[test]
    fn zero_1200_word_truncation_cap_measures_tail_beyond_word_2500() {
        let full: String = (0..2600)
            .map(|i| format!("word{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        let prefix_1200: String = (0..1200)
            .map(|i| format!("word{i}"))
            .collect::<Vec<_>>()
            .join(" ");
        let tail_mutated: String = (0..2600)
            .map(|i| {
                if i >= 1500 {
                    format!("dropped{i}")
                } else {
                    format!("word{i}")
                }
            })
            .collect::<Vec<_>>()
            .join(" ");

        let m_full = compute_untruncated_uax29_metrics(&full, &full);
        assert_eq!(m_full.token_lcs_len, 2600);
        assert!((m_full.rouge_l_f1 - 1.0).abs() < 1e-12);

        let m_prefix = compute_untruncated_uax29_metrics(&prefix_1200, &full);
        assert_eq!(m_prefix.token_lcs_len, 1200);
        assert!((m_prefix.rouge_l_recall - (1200.0 / 2600.0)).abs() < 1e-12);

        let m_tail = compute_untruncated_uax29_metrics(&tail_mutated, &full);
        assert_eq!(m_tail.token_lcs_len, 1500);
        assert!((m_tail.rouge_l_f1 - (1500.0 / 2600.0)).abs() < 1e-12);
    }

    #[test]
    fn uax29_tokenizer_segments_12_script_families_including_cjk_indic_and_thai() {
        // Devanagari combining matras stay attached to base akshara; CJK & Thai segment cleanly
        let sample = "Hello мир विज्ञान 東京タワー カタカナ ภาษาไทย 인공지능";
        let toks = uax29_word_tokens(sample);
        assert!(toks.contains(&"hello".to_owned()));
        assert!(toks.contains(&"мир".to_owned()));
        assert!(
            toks.contains(&"विज्ञान".to_owned()),
            "Devanagari matras must stay attached: {toks:?}"
        );
        assert!(toks.contains(&"東".to_owned()) && toks.contains(&"京".to_owned()));
        assert!(toks.contains(&"タワー".to_owned()) && toks.contains(&"カタカナ".to_owned()));
        assert!(toks.contains(&"인공지능".to_owned()));
        assert!(
            toks.len() >= 10,
            "Thai/CJK must not collapse into 1 giant space-delimited blob: {toks:?}"
        );
    }
}
