//! Athena Agentic Envelope v1: versioned JSON Lines, explicit provenance.
//! Page text is ALWAYS serialized through serde_json rather than interpolated
//! into control directives, preserving framing against content injection.
use crate::{
    projection::Projection,
    rank::{PreparedRank, highlight_indices},
};
use serde::Serialize;
use std::io::{self, BufWriter, Write};
use url::Url;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum LinkMode {
    #[default]
    All,
    HighlightedOnly,
    None,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Frame<'a> {
    Meta {
        version: &'static str,
        source_url: &'a str,
        fetched_at_unix_ms: u64,
        content_hash: String,
        total_blocks: usize,
        parse_warnings: usize,
    },
    Block {
        id: String,
        role: &'a str,
        text: &'a str,
        links: &'a [u32],
        highlight: bool,
    },
    Link {
        id: u32,
        url: &'a str,
    },
    End {
        block_count: usize,
    },
}
struct Output {
    bytes: Vec<u8>,
    limit: usize,
}
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            return Err(io::Error::other("AAE output byte budget exceeded"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn json_line(frame: &Frame<'_>, out: &mut impl Write) -> Result<(), serde_json::Error> {
    serde_json::to_writer(&mut *out, frame)?;
    out.write_all(b"\n").map_err(serde_json::Error::io)?;
    Ok(())
}

pub fn export_jsonl(
    page: &Projection,
    url: &Url,
    observed_at_ms: u64,
    query: Option<&str>,
    highlight_limit: usize,
) -> Result<String, serde_json::Error> {
    export_jsonl_with_limit(
        page,
        url,
        observed_at_ms,
        query,
        highlight_limit,
        16 * 1024 * 1024,
    )
}

/// A complete envelope or an error; the cap counts UTF-8 JSON bytes and newlines.
pub fn export_jsonl_with_limit(
    page: &Projection,
    url: &Url,
    observed_at_ms: u64,
    query: Option<&str>,
    highlight_limit: usize,
    max_output_bytes: usize,
) -> Result<String, serde_json::Error> {
    export_jsonl_with_options(
        page,
        url,
        observed_at_ms,
        query,
        highlight_limit,
        max_output_bytes,
        true,
    )
}

/// Exports AAE v1 JSONL with configurable source link emission (`include_links`).
pub fn export_jsonl_with_options(
    page: &Projection,
    url: &Url,
    observed_at_ms: u64,
    query: Option<&str>,
    highlight_limit: usize,
    max_output_bytes: usize,
    include_links: bool,
) -> Result<String, serde_json::Error> {
    export_jsonl_with_options_and_prepared(
        page,
        url,
        observed_at_ms,
        query,
        highlight_limit,
        max_output_bytes,
        include_links,
        None,
    )
}

/// Exports AAE v1 JSONL using an optional pre-built `PreparedRank` lexical index.
#[allow(clippy::too_many_arguments)]
pub fn export_jsonl_with_options_and_prepared(
    page: &Projection,
    url: &Url,
    observed_at_ms: u64,
    query: Option<&str>,
    highlight_limit: usize,
    max_output_bytes: usize,
    include_links: bool,
    prepared: Option<&PreparedRank>,
) -> Result<String, serde_json::Error> {
    export_jsonl_with_link_mode_and_prepared(
        page,
        url,
        observed_at_ms,
        query,
        highlight_limit,
        max_output_bytes,
        if include_links {
            LinkMode::All
        } else {
            LinkMode::None
        },
        prepared,
    )
}

/// Exports AAE v1 JSONL with `LinkMode` (`All`, `HighlightedOnly`, `None`) and optional `PreparedRank`.
#[allow(clippy::too_many_arguments)]
pub fn export_jsonl_with_link_mode_and_prepared(
    page: &Projection,
    url: &Url,
    observed_at_ms: u64,
    query: Option<&str>,
    highlight_limit: usize,
    max_output_bytes: usize,
    link_mode: LinkMode,
    prepared: Option<&PreparedRank>,
) -> Result<String, serde_json::Error> {
    let mut out = BufWriter::with_capacity(
        8192,
        Output {
            bytes: Vec::new(),
            limit: max_output_bytes,
        },
    );
    let mut content = blake3::Hasher::new();
    {
        // Bounded batching lets BLAKE3 hash multiple chunks together without a page copy.
        let mut digest_input = BufWriter::with_capacity(8192, &mut content);
        for (i, chunk) in page.chunks.iter().enumerate() {
            if i != 0 {
                digest_input
                    .write_all(b"\n")
                    .map_err(serde_json::Error::io)?;
            }
            digest_input
                .write_all(chunk.text.as_bytes())
                .map_err(serde_json::Error::io)?;
        }
        digest_input.flush().map_err(serde_json::Error::io)?;
    }
    let digest = content.finalize().to_hex().to_string();
    json_line(
        &Frame::Meta {
            version: "aae/1",
            source_url: url.as_str(),
            fetched_at_unix_ms: observed_at_ms,
            content_hash: digest,
            total_blocks: page.chunks.len(),
            parse_warnings: page.parse_warnings,
        },
        &mut out,
    )?;

    let mut highlighted_links: Vec<u32> = Vec::new();
    let mut link_remap: Vec<u32> = Vec::new();

    if let Some(q) = query {
        let highlights = if let Some(index) = prepared {
            index.top_indices(q, highlight_limit)
        } else {
            highlight_indices(&page.chunks, q, highlight_limit)
        };
        let mut highlighted = vec![false; page.chunks.len()];
        for &i in &highlights {
            highlighted[i] = true;
        }
        if link_mode == LinkMode::HighlightedOnly && !page.links.is_empty() {
            link_remap.resize(page.links.len(), u32::MAX);
            for &i in &highlights {
                for &orig_id in &page.chunks[i].links {
                    if let Some(slot) = link_remap.get_mut(orig_id as usize)
                        && *slot == u32::MAX
                    {
                        *slot = highlighted_links.len() as u32;
                        highlighted_links.push(orig_id);
                    }
                }
            }
        }
        let ordering = highlights
            .iter()
            .copied()
            .chain((0..page.chunks.len()).filter(|&i| !highlighted[i]));
        let mut remapped_buf: Vec<u32> = Vec::new();
        for i in ordering {
            let chunk = &page.chunks[i];
            let mut hasher = blake3::Hasher::new();
            hasher.update(chunk.role.as_bytes());
            hasher.update(&[0]);
            hasher.update(chunk.text.as_bytes());
            // Disambiguate repeated identical strings within the same snapshot.
            hasher.update(&(i as u64).to_le_bytes());
            let hash = hasher.finalize().to_hex().to_string();
            let block_links: &[u32] = match link_mode {
                LinkMode::All => &chunk.links,
                LinkMode::None => &[],
                LinkMode::HighlightedOnly => {
                    if highlighted[i] && !chunk.links.is_empty() {
                        remapped_buf.clear();
                        for &orig_id in &chunk.links {
                            if let Some(&new_id) = link_remap.get(orig_id as usize)
                                && new_id != u32::MAX
                                && !remapped_buf.contains(&new_id)
                            {
                                remapped_buf.push(new_id);
                            }
                        }
                        &remapped_buf
                    } else {
                        &[]
                    }
                }
            };
            json_line(
                &Frame::Block {
                    id: hash,
                    role: chunk.role,
                    text: &chunk.text,
                    links: block_links,
                    highlight: highlighted[i],
                },
                &mut out,
            )?;
        }
    } else {
        for (i, chunk) in page.chunks.iter().enumerate() {
            let mut hasher = blake3::Hasher::new();
            hasher.update(chunk.role.as_bytes());
            hasher.update(&[0]);
            hasher.update(chunk.text.as_bytes());
            // Disambiguate repeated identical strings within the same snapshot.
            hasher.update(&(i as u64).to_le_bytes());
            let hash = hasher.finalize().to_hex().to_string();
            let block_links: &[u32] = match link_mode {
                LinkMode::All => &chunk.links,
                LinkMode::HighlightedOnly | LinkMode::None => &[],
            };
            json_line(
                &Frame::Block {
                    id: hash,
                    role: chunk.role,
                    text: &chunk.text,
                    links: block_links,
                    highlight: false,
                },
                &mut out,
            )?;
        }
    }

    match link_mode {
        LinkMode::All => {
            for (i, target) in page.links.iter().enumerate() {
                json_line(
                    &Frame::Link {
                        id: i as u32,
                        url: target,
                    },
                    &mut out,
                )?;
            }
        }
        LinkMode::HighlightedOnly => {
            for (new_id, &orig_id) in highlighted_links.iter().enumerate() {
                if let Some(target) = page.links.get(orig_id as usize) {
                    json_line(
                        &Frame::Link {
                            id: new_id as u32,
                            url: target,
                        },
                        &mut out,
                    )?;
                }
            }
        }
        LinkMode::None => {}
    }
    json_line(
        &Frame::End {
            block_count: page.chunks.len(),
        },
        &mut out,
    )?;
    let out = out
        .into_inner()
        .map_err(|error| serde_json::Error::io(error.into_error()))?;
    String::from_utf8(out.bytes).map_err(|error| serde_json::Error::io(io::Error::other(error)))
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MerkleProvenanceTuple {
    pub url_digest: String,
    pub pre_filter_dom_id: u32,
    pub dom_path_slug: String,
    pub chunk_content_hash: String,
    pub merkle_block_id: String,
    pub merkle_hash_hex: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ContrastiveNegativeSample {
    pub block_id: String,
    pub pre_filter_dom_id: u32,
    pub text: String,
    pub score: f64,
    pub score_ratio_to_pos: f64,
    pub simhash_hamming: u32,
    pub sim_ans_weight: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ContrastiveTrainingRecord {
    pub schema: &'static str,
    pub query: String,
    pub source_url: String,
    pub provenance: MerkleProvenanceTuple,
    pub section_path: Vec<String>,
    pub lead_in_context: Option<String>,
    pub table_context: Option<String>,
    pub positive_block_id: String,
    pub positive_text: String,
    pub positive_grounded_passage: String,
    pub positive_score: f64,
    pub hard_negatives: Vec<ContrastiveNegativeSample>,
}

/// Computes an immutable pre-filter DOM Merkle provenance tuple (`D24`) that stays invariant
/// across boilerplate filtering policies (`FullDom`, `MainContentV1`, `CetdEntropyV1`).
pub fn compute_merkle_provenance(
    source_url: &Url,
    pre_filter_dom_id: u32,
    dom_path_slug: &str,
    chunk_text: &str,
) -> MerkleProvenanceTuple {
    let url_digest = blake3::hash(source_url.as_str().as_bytes())
        .to_hex()
        .to_string();
    let chunk_content_hash = blake3::hash(chunk_text.as_bytes()).to_hex().to_string();
    let locator_payload = format!(
        "{}|dom:{}|path:{}",
        source_url.as_str(),
        pre_filter_dom_id,
        dom_path_slug
    );
    let locator_hex = blake3::hash(locator_payload.as_bytes())
        .to_hex()
        .to_string();
    let merkle_payload = format!("{locator_payload}|content:{chunk_content_hash}");
    let merkle_hash_hex = blake3::hash(merkle_payload.as_bytes()).to_hex().to_string();
    let merkle_block_id = format!("b_{}_{}", pre_filter_dom_id, &locator_hex[..16]);
    MerkleProvenanceTuple {
        url_digest,
        pre_filter_dom_id,
        dom_path_slug: dom_path_slug.to_owned(),
        chunk_content_hash,
        merkle_block_id,
        merkle_hash_hex,
    }
}

/// Exports `aae/2` Contrastive Training Records with `NV-Embed-v2 TopK-PercPos`
/// ($0.35\,\hat{s}^+ \le \hat{s}^- \le 0.95\,\hat{s}^+$), `SimANS` density weighting,
/// `SimHash-64` near-duplicate exclusion ($\text{Hamming} \ge 8$), and non-adjacency ($|i - i^+| > 1$)
/// alongside structural section lineage `[H1 > H2 > H3]` and lead-in `":"` discourse binding (`D22`, `D24`).
pub fn export_contrastive_training_records(
    rich_page: &crate::projection::BreakthroughProjection,
    source_url: &Url,
    query: &str,
    max_positives: usize,
    max_negatives_per_positive: usize,
) -> Vec<ContrastiveTrainingRecord> {
    if rich_page.rich_chunks.is_empty() || query.trim().is_empty() || max_positives == 0 {
        return Vec::new();
    }
    let ranked =
        crate::rank::bm25f_fused_single_pass(&rich_page.projection.chunks, query, 1.2, 0.75, 0.5);
    let mut score_by_idx = vec![0.0f64; rich_page.rich_chunks.len()];
    for &(idx, sc) in &ranked {
        if idx < score_by_idx.len() {
            score_by_idx[idx] = sc;
        }
    }

    let mut records = Vec::<ContrastiveTrainingRecord>::new();
    for &(pos_idx, pos_score) in &ranked {
        if pos_score <= 0.0 || pos_idx >= rich_page.rich_chunks.len() {
            break;
        }
        let pos_rc = &rich_page.rich_chunks[pos_idx];
        if pos_rc.chunk.role == "heading" && rich_page.rich_chunks.len() > 1 {
            continue;
        }
        let provenance = compute_merkle_provenance(
            source_url,
            pos_rc.pre_filter_dom_id,
            &pos_rc.dom_path_slug,
            &pos_rc.chunk.text,
        );
        let section_path: Vec<String> = pos_rc
            .heading_stack
            .iter()
            .filter_map(|h| h.as_ref().cloned())
            .collect();
        let table_context = pos_rc
            .table_grounding
            .as_ref()
            .map(|g| g.grounded_context.clone());

        let mut grounded_parts = Vec::<String>::new();
        if !section_path.is_empty() {
            grounded_parts.push(format!("[{}]", section_path.join(" > ")));
        }
        if let Some(lead) = &pos_rc.lead_in_text {
            grounded_parts.push(lead.clone());
        }
        grounded_parts.push(pos_rc.chunk.text.clone());
        let positive_grounded_passage = grounded_parts.join(" ");

        // Mine hard negatives via NV-Embed-v2 TopK-PercPos + SimANS + SimHash >= 8 + |i - i+| > 1
        let lower_bound = 0.35 * pos_score;
        let upper_bound = 0.95 * pos_score;
        let target_center = 0.70 * pos_score;
        let mut neg_candidates = Vec::<ContrastiveNegativeSample>::new();

        for (cand_idx, cand_rc) in rich_page.rich_chunks.iter().enumerate() {
            if cand_idx.abs_diff(pos_idx) <= 1 {
                continue;
            }
            let cand_score = score_by_idx[cand_idx];
            let hamming = (pos_rc.simhash64 ^ cand_rc.simhash64).count_ones();
            if hamming < 8 || pos_rc.exact_hash64 == cand_rc.exact_hash64 {
                continue;
            }
            let ratio = cand_score / pos_score.max(1e-9);
            // Accept candidates inside the PercPos band [0.35*s+, 0.95*s+], or fallback lexical distractors (> 0)
            if (cand_score >= lower_bound && cand_score <= upper_bound)
                || (neg_candidates.len() < max_negatives_per_positive && cand_score <= upper_bound)
            {
                let diff = (cand_score - target_center) / pos_score.max(1e-6);
                let in_band_bonus = if cand_score >= lower_bound && cand_score <= upper_bound {
                    1.0
                } else {
                    0.25
                };
                let sim_ans_weight = in_band_bonus * (-4.5 * diff * diff).exp();
                let cand_prov = compute_merkle_provenance(
                    source_url,
                    cand_rc.pre_filter_dom_id,
                    &cand_rc.dom_path_slug,
                    &cand_rc.chunk.text,
                );
                neg_candidates.push(ContrastiveNegativeSample {
                    block_id: cand_prov.merkle_block_id,
                    pre_filter_dom_id: cand_rc.pre_filter_dom_id,
                    text: cand_rc.chunk.text.clone(),
                    score: cand_score,
                    score_ratio_to_pos: ratio,
                    simhash_hamming: hamming,
                    sim_ans_weight,
                });
            }
        }

        neg_candidates.sort_by(|a, b| {
            b.sim_ans_weight
                .total_cmp(&a.sim_ans_weight)
                .then_with(|| a.pre_filter_dom_id.cmp(&b.pre_filter_dom_id))
        });
        neg_candidates.truncate(max_negatives_per_positive);

        records.push(ContrastiveTrainingRecord {
            schema: "aae/2",
            query: query.to_owned(),
            source_url: source_url.as_str().to_owned(),
            positive_block_id: provenance.merkle_block_id.clone(),
            provenance,
            section_path,
            lead_in_context: pos_rc.lead_in_text.clone(),
            table_context,
            positive_text: pos_rc.chunk.text.clone(),
            positive_grounded_passage,
            positive_score: pos_score,
            hard_negatives: neg_candidates,
        });
        if records.len() >= max_positives {
            break;
        }
    }
    records
}

pub fn export_contrastive_jsonl(
    rich_page: &crate::projection::BreakthroughProjection,
    source_url: &Url,
    query: &str,
    max_positives: usize,
    max_negatives_per_positive: usize,
) -> Result<String, serde_json::Error> {
    let records = export_contrastive_training_records(
        rich_page,
        source_url,
        query,
        max_positives,
        max_negatives_per_positive,
    );
    let mut lines_buf = String::new();
    for rec in &records {
        let serialized = serde_json::to_string(rec)?;
        lines_buf.push_str(&serialized);
        lines_buf.push('\n');
    }
    Ok(lines_buf)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::parse_html;
    #[test]
    fn output_cap_counts_escaped_bytes_and_end_frame() {
        let base = Url::parse("https://example.org/").unwrap();
        let large = format!("<p>{}</p>", "\"\\内容".repeat(2500));
        for html in ["<p>A &amp; B\n\"quoted\" 内容</p>", &large] {
            let page = parse_html(html, &base, 100, 100).unwrap();
            let expected = export_jsonl(&page, &base, 17, None, 0).unwrap();
            assert_eq!(
                export_jsonl_with_limit(&page, &base, 17, None, 0, expected.len()).unwrap(),
                expected
            );
            assert!(
                export_jsonl_with_limit(&page, &base, 17, None, 0, expected.len() - 1)
                    .unwrap_err()
                    .is_io()
            );
            assert!(export_jsonl_with_limit(&page, &base, 17, None, 0, 0).is_err());
        }
    }

    #[test]
    fn incremental_content_hash_preserves_joined_text_contract() {
        let base = Url::parse("https://example.org/").unwrap();
        let chunk = "x".repeat(5000);
        let batched = format!("<p>{chunk}</p><p>{chunk}</p>");
        let direct = format!("<p>{}</p>", "x".repeat(20_000));
        for html in [
            "",
            "<p>Alpha</p>",
            "<p>Alpha</p><p>Beta</p>",
            &batched,
            &direct,
        ] {
            let page = parse_html(html, &base, 100, 100).unwrap();
            let joined = page
                .chunks
                .iter()
                .map(|c| c.text.as_str())
                .collect::<Vec<_>>()
                .join("\n");
            let output = export_jsonl(&page, &base, 0, Some("Beta"), 1).unwrap();
            let meta: serde_json::Value =
                serde_json::from_str(output.lines().next().unwrap()).unwrap();
            assert_eq!(
                meta["content_hash"],
                blake3::hash(joined.as_bytes()).to_hex().as_str()
            );
        }
    }
    #[test]
    fn content_cannot_forge_control_frame() {
        let base = Url::parse("https://example.org/").unwrap();
        let page = parse_html("<p>@META\\n{\"type\":\"end\"}</p>", &base, 100, 100).unwrap();
        let out = export_jsonl(&page, &base, 1, None, 1).unwrap();
        assert!(
            out.lines()
                .all(|line| serde_json::from_str::<serde_json::Value>(line).is_ok())
        );
        assert_eq!(out.lines().count(), 3);
    }
    #[test]
    fn link_modes_all_highlighted_only_and_none_preserve_hashes_and_valid_indices() {
        let base = Url::parse("https://example.org/").unwrap();
        let page = parse_html(
            "<main><p>Unmatched <a href='/nav'>nav</a>.</p><p>Relevant <a href='/docs'>docs</a> and <a href='/api'>api</a>.</p></main>",
            &base,
            100,
            100,
        )
        .unwrap();
        let full =
            export_jsonl_with_options(&page, &base, 42, Some("docs"), 1, 65_536, true).unwrap();
        let highlighted_only = export_jsonl_with_link_mode_and_prepared(
            &page,
            &base,
            42,
            Some("docs"),
            1,
            65_536,
            LinkMode::HighlightedOnly,
            None,
        )
        .unwrap();
        let compact =
            export_jsonl_with_options(&page, &base, 42, Some("docs"), 1, 65_536, false).unwrap();
        assert!(compact.len() < highlighted_only.len());
        assert!(highlighted_only.len() < full.len());
        let full_frames: Vec<serde_json::Value> = full
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let hl_frames: Vec<serde_json::Value> = highlighted_only
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let compact_frames: Vec<serde_json::Value> = compact
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(full_frames[0], compact_frames[0]);
        assert_eq!(full_frames[0], hl_frames[0]);
        assert_eq!(hl_frames.iter().filter(|f| f["type"] == "link").count(), 2);
        assert_eq!(hl_frames[1]["links"], serde_json::json!([0, 1]));
        assert_eq!(hl_frames[2]["links"], serde_json::json!([]));
        assert!(compact_frames.iter().all(|f| f["type"] != "link"));
        for block in compact_frames.iter().filter(|f| f["type"] == "block") {
            assert_eq!(block["links"], serde_json::json!([]));
        }
        let prepared = PreparedRank::new(&page.chunks);
        let from_prepared = export_jsonl_with_options_and_prepared(
            &page,
            &base,
            42,
            Some("docs"),
            1,
            65_536,
            false,
            Some(&prepared),
        )
        .unwrap();
        assert_eq!(compact, from_prepared);

        let mut custom = page;
        custom.links = (0..6)
            .map(|i| format!("https://example.org/l/{i}"))
            .collect();
        custom.chunks[1].links = vec![5, 2, 5];
        let remapped_out = export_jsonl_with_link_mode_and_prepared(
            &custom,
            &base,
            42,
            Some("docs"),
            1,
            65_536,
            LinkMode::HighlightedOnly,
            None,
        )
        .unwrap();
        let remapped_frames: Vec<serde_json::Value> = remapped_out
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(remapped_frames[1]["links"], serde_json::json!([0, 1]));
        let link_frames: Vec<&serde_json::Value> = remapped_frames
            .iter()
            .filter(|f| f["type"] == "link")
            .collect();
        assert_eq!(link_frames.len(), 2);
        assert_eq!(
            link_frames[0],
            &serde_json::json!({"type": "link", "id": 0, "url": "https://example.org/l/5"})
        );
        assert_eq!(
            link_frames[1],
            &serde_json::json!({"type": "link", "id": 1, "url": "https://example.org/l/2"})
        );
    }
}
