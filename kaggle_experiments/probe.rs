//! Independent finite oracles and consumer probes; copied into scratch snapshots only.
use athena_core_v2::{
    bandit::{Bandit, SelectionPolicy, Tier},
    ip_policy::is_public_candidate,
    projection::Chunk,
    rank::bm25,
    singleflight::SingleFlight,
    table::{Cell, Row, expand},
};
use rand::{SeedableRng, rngs::StdRng};
use rand_distr::{Beta, Distribution};
use serde_json::{Value, json};
use std::{
    env,
    future::{Future, poll_fn},
    hint::black_box,
    io::{self, Read},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    task::Poll,
    time::Instant,
};

fn stdin_json() -> Value {
    let mut text = String::new();
    io::stdin().read_to_string(&mut text).unwrap();
    serde_json::from_str(&text).unwrap()
}

async fn register<F: Future>(future: &mut std::pin::Pin<Box<F>>) {
    poll_fn(|cx| {
        assert!(
            future.as_mut().poll(cx).is_pending(),
            "flight completed before gate"
        );
        Poll::Ready(())
    })
    .await;
}

#[tokio::main]
async fn main() {
    let args: Vec<_> = env::args().collect();
    match args.get(1).map(String::as_str).unwrap() {
        "hash" => {
            let values = stdin_json();
            let joined = values
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect::<Vec<_>>()
                .join("\n");
            println!(
                "{}",
                json!({"hash": blake3::hash(joined.as_bytes()).to_hex().to_string()})
            );
        }
        "rank" => {
            let input = stdin_json();
            let chunks = input["texts"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
                .map(|(i, text)| Chunk {
                    node: i as u32,
                    role: "paragraph",
                    text: text.as_str().unwrap().to_owned(),
                    links: vec![],
                })
                .collect::<Vec<_>>();
            println!(
                "{}",
                json!({"scores": bm25(&chunks, input["query"].as_str().unwrap(), 1.2, 0.75)})
            );
        }
        "table" => {
            let input = stdin_json();
            let rows = input["rows"]
                .as_array()
                .unwrap()
                .iter()
                .map(|row| Row {
                    group: row["group"].as_u64().unwrap() as usize,
                    cells: row["cells"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|cell| Cell {
                            text: cell["text"].as_str().unwrap().to_owned(),
                            rowspan: cell["rowspan"].as_u64().unwrap() as usize,
                            colspan: cell["colspan"].as_u64().unwrap() as usize,
                            is_header: cell["header"].as_bool().unwrap(),
                        })
                        .collect(),
                })
                .collect::<Vec<_>>();
            match expand(
                &rows,
                input["max_rows"].as_u64().unwrap() as usize,
                input["max_cols"].as_u64().unwrap() as usize,
            ) {
                Ok(grid) => println!(
                    "{}",
                    json!({"slots": grid.slots, "width": grid.width,
                    "cells": grid.cells.iter().map(|c| json!({"text": c.text, "header": c.is_header})).collect::<Vec<_>>()})
                ),
                Err(error) => println!("{}", json!({"error": error.to_string()})),
            }
        }
        "rng" => {
            let alpha: f64 = args[2].parse().unwrap();
            let beta: f64 = args[3].parse().unwrap();
            let n: usize = args[4].parse().unwrap();
            let seed: u64 = args[5].parse().unwrap();
            assert!((1..=1_000_000).contains(&n));
            assert!(alpha.is_finite() && beta.is_finite() && alpha > 0.0 && beta > 0.0);
            let mut rng = StdRng::seed_from_u64(seed);
            let dist = Beta::new(alpha, beta).unwrap();
            let mut values = Vec::with_capacity(n);
            let start = Instant::now();
            for _ in 0..n {
                values.push(black_box(dist.sample(&mut rng)));
            }
            let duration_ns = start.elapsed().as_nanos();
            assert!(
                values
                    .iter()
                    .all(|x: &f64| x.is_finite() && (0.0..=1.0).contains(x))
            );
            let sum: f64 = values.iter().sum();
            let sum_sq: f64 = values.iter().map(|x| x * x).sum();
            assert_eq!(n % 20, 0);
            let expected_mean = alpha / (alpha + beta);
            let central_second_block_means = values
                .chunks_exact(n / 20)
                .map(|block| {
                    block
                        .iter()
                        .map(|x| (x - expected_mean).powi(2))
                        .sum::<f64>()
                        / block.len() as f64
                })
                .collect::<Vec<_>>();
            let cutoffs = [0.1, 0.25, 0.5, 0.9];
            let counts = cutoffs.map(|cutoff| values.iter().filter(|&&x| x <= cutoff).count());
            let now = Instant::now();
            let bandit = Bandit::new(now, [1.0; 5]);
            let policy = SelectionPolicy {
                cost_usd_per_attempt: [1.0; 5],
                latency_value_usd_per_ms: 0.0,
                eligible: [true, false, false, false, false],
            };
            assert_eq!(bandit.choose(&mut rng, now, &policy), Some(Tier::Direct));
            println!(
                "{}",
                json!({"n": n, "seed": seed, "alpha": alpha, "beta": beta,
                "duration_ns": duration_ns, "sum": sum, "sum_sq": sum_sq, "cutoffs": cutoffs,
                "cdf_counts": counts, "central_second_block_means": central_second_block_means,
                "bandit_consumer": true})
            );
        }
        "ip" => {
            let cases = [
                ("127.0.0.1", false),
                ("10.0.0.1", false),
                ("172.16.0.1", false),
                ("172.31.255.255", false),
                ("192.168.1.1", false),
                ("169.254.169.254", false),
                ("0.0.0.0", false),
                ("8.8.8.8", true),
                ("1.1.1.1", true),
                ("::1", false),
                ("fe80::1", false),
                ("fc00::1", false),
                ("2001:db8::1", false),
                ("2606:4700:4700::1111", true),
                ("::ffff:127.0.0.1", false),
                ("2001:2::1", false),
                ("3fff::1", false),
            ];
            let details = cases.map(|(ip, expected)| {
                let actual = is_public_candidate(ip.parse().unwrap());
                assert_eq!(actual, expected, "{ip}");
                json!({"ip": ip, "expected": expected, "actual": actual})
            });
            println!(
                "{}",
                json!({"scope": "address-classifier points, not DNS/TLS/SSRF integration", "details": details})
            );
        }
        "singleflight" => {
            let sf = Arc::new(SingleFlight::default());
            let count = Arc::new(AtomicUsize::new(0));
            let (release, _) = tokio::sync::watch::channel(false);
            let mut calls = Vec::new();
            for i in 0..500 {
                let sf = sf.clone();
                let count = count.clone();
                let mut gate = release.subscribe();
                calls.push(Box::pin(async move {
                    sf.call(i % 50, || async move {
                        count.fetch_add(1, Ordering::SeqCst);
                        while !*gate.borrow_and_update() {
                            gate.changed().await.unwrap();
                        }
                        Ok::<_, ()>("payload")
                    })
                    .await
                }));
            }
            for call in &mut calls {
                register(call).await;
            }
            assert_eq!(count.load(Ordering::SeqCst), 50);
            release.send_replace(true);
            for call in calls {
                assert_eq!(*call.await.unwrap(), "payload");
            }
            let count_again = count.clone();
            sf.call(0, || async move {
                count_again.fetch_add(1, Ordering::SeqCst);
                Ok::<_, ()>("new")
            })
            .await
            .unwrap();
            assert_eq!(count.load(Ordering::SeqCst), 51);
            let never = std::future::pending::<Result<&str, ()>>();
            let mut leader = Box::pin(sf.call(99, || never));
            register(&mut leader).await;
            let mut follower = Box::pin(sf.call(99, || async { Ok("unexpected") }));
            register(&mut follower).await;
            drop(leader);
            assert!(follower.await.is_err());
            println!(
                "{}",
                json!({"registered_calls":500,"keys":50,"leader_executions":50,
                "after_sequential_call":51,"leader_cancellation":true,
                "scope":"deterministic SingleFlight state test; no origin HTTP requests"})
            );
        }
        _ => panic!("unknown probe"),
    }
}
