use athena_core_v2::{projection::parse_html, aae::export_jsonl, table::{Row,Cell,expand}};
use url::Url;

#[test]
fn html5_repairs_entities_and_escaped_aae() {
    let u=Url::parse("https://example.com/").unwrap();
    let p=parse_html("<main><h2>A &amp; B</h2><p>Rust <b>works</b> reliably.</p></main>", &u, 1000, 100).unwrap();
    let aae=export_jsonl(&p,&u,17,Some("Rust reliably"),3).unwrap();
    let frames:Vec<serde_json::Value>=aae.lines().map(|x|serde_json::from_str(x).unwrap()).collect();
    assert_eq!(frames.first().unwrap()["type"],"meta");
    assert_eq!(frames.last().unwrap()["type"],"end");
    assert!(frames.iter().any(|f|f["text"]=="A & B"));
}
#[test]
fn span_grid_has_bounded_memory() {
    let rows=vec![Row { group:0, cells:vec![Cell { text:"large".into(),rowspan:10000,colspan:10000,is_header:true }] }];
    assert!(expand(&rows,4,16).is_err());
}
