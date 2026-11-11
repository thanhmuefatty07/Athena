//! Mode B: Persistent Homogeneous P-Core Streaming IPC Pool (`--qos worker`, `50ms` Watchdog Quarantine).
//! Consumes raw `[u32 total_len][u32 uri_len][uri][u32 hdr_len][headers][u32 body_len][raw_body_bytes]`
//! binary frames (`0` Python UTF-8 pre-transcoding, `0` URL fallback, `0` JSON-escape inflation).

use athena_cloudbench::framing::{
    DEFAULT_MAX_FRAME_BYTES, RawWarcFrame, encode_raw_warc_frame, read_raw_warc_frame,
};
use athena_cloudbench::host_qos::{QosProfile, apply_qos_profile};
use athena_core_v2::projection::{BoilerplatePolicy, ParseLimits, parse_html_with_limits};
use athena_lexbor_adapter_research::{Limits as LexborLimits, parse_and_project_pure};
use serde_json::{Value, json};
use std::env;
use std::fs::File;
use std::io::{self, BufReader, BufWriter, Cursor, Read, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use url::Url;

const WORKER_READY_MAGIC: &[u8; 4] = b"RDY\n";

fn parse_stall_injection_ms(headers: &[u8]) -> Option<u64> {
    for line in headers.split(|&b| b == b'\n') {
        let trimmed = line.strip_suffix(b"\r").unwrap_or(line);
        if let Some(colon) = trimmed.iter().position(|&b| b == b':') {
            let name = &trimmed[..colon];
            if name.eq_ignore_ascii_case(b"x-athena-test-stall-ms") {
                let val_str = std::str::from_utf8(&trimmed[colon + 1..]).ok()?.trim();
                return val_str.parse::<u64>().ok();
            }
        }
    }
    None
}

fn run_worker_loop(slot: usize, qos: QosProfile) -> Result<(), String> {
    let qos_status = apply_qos_profile(qos, Some(slot));
    let stdin = io::stdin();
    let mut reader = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut writer = BufWriter::new(stdout.lock());

    // Signal readiness after dyld/startup + QoS pinning so watchdog measures strictly per-record latency.
    writer
        .write_all(WORKER_READY_MAGIC)
        .and_then(|()| writer.flush())
        .map_err(|e| e.to_string())?;

    let core_limits = ParseLimits {
        boilerplate: BoilerplatePolicy::MainContentV1,
        ..ParseLimits::default()
    };
    let lxb_limits = LexborLimits::default();

    while let Some(frame) =
        read_raw_warc_frame(&mut reader, DEFAULT_MAX_FRAME_BYTES).map_err(|e| e.to_string())?
    {
        let t0 = Instant::now();
        if let Some(stall_ms) = parse_stall_injection_ms(&frame.headers)
            && stall_ms > 0
        {
            thread::sleep(Duration::from_millis(stall_ms));
        }

        // Strict raw-URI & raw-byte handling: 0 URL fallback (`bench.invalid` forbidden), 0 pre-transcoding.
        let uri_utf8 = std::str::from_utf8(&frame.uri).ok();
        let parsed_url = uri_utf8.and_then(|u| Url::parse(u).ok());
        let body_utf8 = std::str::from_utf8(&frame.raw_body_bytes).ok();

        let (track_a_status, track_a_chunks, track_a_blake3) = match (&parsed_url, body_utf8) {
            (None, _) => ("INVALID_RAW_URI", 0usize, String::new()),
            (Some(_), None) => ("NON_UTF8_RAW_BODY_REJECTED", 0usize, String::new()),
            (Some(url), Some(html_str)) => match parse_html_with_limits(html_str, url, core_limits)
            {
                Ok(proj) => {
                    let mut hasher = blake3::Hasher::new();
                    for c in &proj.chunks {
                        hasher.update(c.role.as_bytes());
                        hasher.update(&[0]);
                        hasher.update(c.text.as_bytes());
                        hasher.update(&[0xff]);
                    }
                    (
                        "OK",
                        proj.chunks.len(),
                        hasher.finalize().to_hex().to_string(),
                    )
                }
                Err(_) => ("TRACK_A_PARSE_ERR", 0usize, String::new()),
            },
        };

        let (lexbor_status, lexbor_chunks) = match uri_utf8 {
            Some(u) if parsed_url.is_some() => {
                match parse_and_project_pure(&frame.raw_body_bytes, u, lxb_limits) {
                    Ok(proj) => ("OK", proj.chunks.len()),
                    Err(_) => ("LEXBOR_PARSE_ERR", 0usize),
                }
            }
            _ => ("INVALID_RAW_URI", 0usize),
        };

        let resp = json!({
            "status": "OK",
            "worker_slot": slot,
            "qos": qos_status.profile,
            "uri_lossy": String::from_utf8_lossy(&frame.uri),
            "raw_header_bytes": frame.headers.len(),
            "raw_body_bytes": frame.raw_body_bytes.len(),
            "track_a_utf8_valid": body_utf8.is_some(),
            "track_a_status": track_a_status,
            "track_a_chunks": track_a_chunks,
            "track_a_blake3": track_a_blake3,
            "lexbor_pure_status": lexbor_status,
            "lexbor_pure_chunks": lexbor_chunks,
            "worker_elapsed_us": t0.elapsed().as_micros() as u64,
        });
        let resp_bytes = serde_json::to_vec(&resp).map_err(|e| e.to_string())?;
        let resp_len = u32::try_from(resp_bytes.len()).map_err(|_| "response too large")?;
        writer
            .write_all(&resp_len.to_be_bytes())
            .and_then(|()| writer.write_all(&resp_bytes))
            .and_then(|()| writer.flush())
            .map_err(|e| e.to_string())?;
    }
    Ok(())
}

struct ManagedWorker {
    slot: usize,
    qos: QosProfile,
    exe_path: std::path::PathBuf,
    child: Child,
    stdin: ChildStdin,
    rx: Receiver<Result<Vec<u8>, String>>,
    reader_thread: Option<JoinHandle<()>>,
}

impl ManagedWorker {
    fn spawn(slot: usize, qos: QosProfile, exe_path: std::path::PathBuf) -> Result<Self, String> {
        let mut child = Command::new(&exe_path)
            .arg("--internal-worker-slot")
            .arg(slot.to_string())
            .arg("--qos")
            .arg(qos.as_str())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("failed to spawn worker {slot}: {e}"))?;

        let stdin = child.stdin.take().ok_or("missing worker stdin")?;
        let mut stdout = child.stdout.take().ok_or("missing worker stdout")?;

        let mut magic = [0u8; 4];
        stdout
            .read_exact(&mut magic)
            .map_err(|e| format!("worker {slot} failed startup handshake: {e}"))?;
        if &magic != WORKER_READY_MAGIC {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("worker {slot} bad handshake magic: {magic:?}"));
        }

        let (tx, rx) = mpsc::channel::<Result<Vec<u8>, String>>();
        let reader_thread = thread::spawn(move || {
            let mut rdr = BufReader::new(stdout);
            loop {
                let mut len_buf = [0u8; 4];
                match rdr.read_exact(&mut len_buf) {
                    Ok(()) => {}
                    Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => break,
                    Err(e) => {
                        let _ = tx.send(Err(e.to_string()));
                        break;
                    }
                }
                let n = u32::from_be_bytes(len_buf) as usize;
                let mut payload = vec![0u8; n];
                if let Err(e) = rdr.read_exact(&mut payload) {
                    let _ = tx.send(Err(e.to_string()));
                    break;
                }
                if tx.send(Ok(payload)).is_err() {
                    break;
                }
            }
        });

        Ok(Self {
            slot,
            qos,
            exe_path,
            child,
            stdin,
            rx,
            reader_thread: Some(reader_thread),
        })
    }

    fn kill_and_respawn(&mut self) -> Result<(), String> {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(handle) = self.reader_thread.take() {
            let _ = handle.join();
        }
        let replacement = Self::spawn(self.slot, self.qos, self.exe_path.clone())?;
        *self = replacement;
        Ok(())
    }

    fn evaluate_frame(
        &mut self,
        record_index: usize,
        frame: &RawWarcFrame,
        watchdog: Duration,
    ) -> Result<Value, String> {
        let t0 = Instant::now();
        let wire = encode_raw_warc_frame(&frame.uri, &frame.headers, &frame.raw_body_bytes)
            .map_err(|e| e.to_string())?;
        if self
            .stdin
            .write_all(&wire)
            .and_then(|()| self.stdin.flush())
            .is_err()
        {
            self.kill_and_respawn()?;
            return Ok(json!({
                "record_index": record_index,
                "status": "PAGE_QUARANTINED",
                "quarantine_reason": "WORKER_PIPE_BROKEN",
                "worker_slot": self.slot,
                "worker_respawned": true,
                "uri_lossy": String::from_utf8_lossy(&frame.uri),
                "raw_body_bytes": frame.raw_body_bytes.len(),
                "wall_us": t0.elapsed().as_micros() as u64,
            }));
        }

        match self.rx.recv_timeout(watchdog) {
            Ok(Ok(bytes)) => {
                let mut val: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
                if let Some(obj) = val.as_object_mut() {
                    obj.insert("record_index".into(), json!(record_index));
                    obj.insert("wall_us".into(), json!(t0.elapsed().as_micros() as u64));
                }
                Ok(val)
            }
            Ok(Err(pipe_err)) => {
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": format!("WORKER_CRASHED:{pipe_err}"),
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "wall_us": t0.elapsed().as_micros() as u64,
                }))
            }
            Err(RecvTimeoutError::Timeout) => {
                let elapsed_us = t0.elapsed().as_micros() as u64;
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": format!("WATCHDOG_TIMEOUT_{}MS", watchdog.as_millis()),
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "watchdog_elapsed_us": elapsed_us,
                    "wall_us": t0.elapsed().as_micros() as u64,
                }))
            }
            Err(RecvTimeoutError::Disconnected) => {
                self.kill_and_respawn()?;
                Ok(json!({
                    "record_index": record_index,
                    "status": "PAGE_QUARANTINED",
                    "quarantine_reason": "WORKER_DISCONNECTED",
                    "worker_slot": self.slot,
                    "worker_respawned": true,
                    "uri_lossy": String::from_utf8_lossy(&frame.uri),
                    "raw_body_bytes": frame.raw_body_bytes.len(),
                    "wall_us": t0.elapsed().as_micros() as u64,
                }))
            }
        }
    }

    fn shutdown(mut self) {
        drop(self.stdin);
        let _ = self.child.wait();
        if let Some(handle) = self.reader_thread.take() {
            let _ = handle.join();
        }
    }
}

fn execute_pool_stream<R: Read + Send>(
    reader: R,
    workers: usize,
    qos: QosProfile,
    watchdog_ms: u64,
) -> Result<Vec<Value>, String> {
    let exe_path = env::current_exe().map_err(|e| e.to_string())?;
    let worker_count = workers.max(1);
    let watchdog = Duration::from_millis(watchdog_ms.max(1));

    let mut pool = Vec::with_capacity(worker_count);
    for slot in 0..worker_count {
        pool.push(ManagedWorker::spawn(slot, qos, exe_path.clone())?);
    }

    let source = Mutex::new((0usize, BufReader::new(reader)));
    let outcomes: Mutex<Vec<Value>> = Mutex::new(Vec::new());

    let run_res: Result<(), String> = thread::scope(|scope| {
        let mut handles = Vec::with_capacity(pool.len());
        for mut worker in pool {
            let src_ref = &source;
            let out_ref = &outcomes;
            handles.push(scope.spawn(move || -> Result<(), String> {
                loop {
                    let next_item = {
                        let mut guard = src_ref.lock().map_err(|_| "poisoned source mutex")?;
                        let idx = guard.0;
                        match read_raw_warc_frame(&mut guard.1, DEFAULT_MAX_FRAME_BYTES)
                            .map_err(|e| e.to_string())?
                        {
                            Some(frame) => {
                                guard.0 += 1;
                                Some((idx, frame))
                            }
                            None => None,
                        }
                    };
                    let Some((rec_idx, frame)) = next_item else {
                        break;
                    };
                    let val = worker.evaluate_frame(rec_idx, &frame, watchdog)?;
                    out_ref
                        .lock()
                        .map_err(|_| "poisoned outcomes mutex")?
                        .push(val);
                }
                worker.shutdown();
                Ok(())
            }));
        }
        for h in handles {
            h.join()
                .map_err(|_| "worker thread panicked".to_owned())??;
        }
        Ok(())
    });
    run_res?;

    let mut collected = outcomes.into_inner().map_err(|_| "poisoned outcomes")?;
    collected.sort_by_key(|v| v["record_index"].as_u64().unwrap_or(u64::MAX));
    Ok(collected)
}

fn run_self_test() -> Result<(), String> {
    let mut stream_bytes = Vec::new();
    // 0: Normal UTF-8 HTML article
    stream_bytes.extend_from_slice(
        &encode_raw_warc_frame(
            b"https://example.org/article-1",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><h1>Article One</h1><p>Valid UTF-8 content preserved end to end.</p></main></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    // 1: Raw Shift_JIS non-UTF-8 body (verifies 0 pre-transcoding distortion)
    stream_bytes.extend_from_slice(
        &encode_raw_warc_frame(
            b"https://legacy.example.jp/sjis",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=Shift_JIS\r\n\r\n",
            b"<html><body><p>\x93\xfa\x96\x7b\x8c\xea</p></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    // 2: Poison-pill page exceeding 50ms per-record watchdog (250ms stall)
    stream_bytes.extend_from_slice(
        &encode_raw_warc_frame(
            b"https://poison.example.com/warc-00432-offset-544317574",
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-Stall-Ms: 250\r\n\r\n",
            b"<html><body><p>Pathological stall payload</p></body></html>",
        )
        .map_err(|e| e.to_string())?,
    );
    // 3 & 4: Post-quarantine normal records proving zero segment stall and clean respawn
    for idx in 3..=4 {
        stream_bytes.extend_from_slice(
            &encode_raw_warc_frame(
                format!("https://example.org/post-quarantine-{idx}").as_bytes(),
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n",
                format!("<html><body><main><h1>Post Quarantine {idx}</h1><p>Recovered worker processes record {idx} cleanly.</p></main></body></html>").as_bytes(),
            )
            .map_err(|e| e.to_string())?,
        );
    }

    let results = execute_pool_stream(Cursor::new(stream_bytes), 2, QosProfile::Worker, 50)?;
    if results.len() != 5 {
        return Err(format!("expected 5 records, got {}", results.len()));
    }
    if results[0]["status"] != "OK" || results[0]["track_a_status"] != "OK" {
        return Err(format!("record 0 failed: {}", results[0]));
    }
    if results[1]["track_a_utf8_valid"] != false
        || results[1]["track_a_status"] != "NON_UTF8_RAW_BODY_REJECTED"
        || results[1]["lexbor_pure_chunks"].as_u64().unwrap_or(0) == 0
    {
        return Err(format!(
            "record 1 raw non-UTF-8 check failed: {}",
            results[1]
        ));
    }
    if results[2]["status"] != "PAGE_QUARANTINED" || results[2]["worker_respawned"] != true {
        return Err(format!(
            "record 2 poison-pill failed to trigger PAGE_QUARANTINED: {}",
            results[2]
        ));
    }
    if results[3]["status"] != "OK" || results[4]["status"] != "OK" {
        return Err(format!(
            "post-quarantine records failed to recover: {}, {}",
            results[3], results[4]
        ));
    }

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "schema": "AthenaCloudBenchModeBPoolSelfTest/1",
            "status": "PASS",
            "records": results,
        }))
        .map_err(|e| e.to_string())?
    );
    Ok(())
}

fn main() -> Result<(), String> {
    let args: Vec<String> = env::args().collect();
    let mut internal_slot: Option<usize> = None;
    let mut qos = QosProfile::Worker;
    let mut workers: usize = 8;
    let mut watchdog_ms: u64 = 50;
    let mut frames_in: Option<String> = None;
    let mut jsonl_out: Option<String> = None;
    let mut self_test = false;

    let mut i = 1usize;
    while i < args.len() {
        match args[i].as_str() {
            "--internal-worker-slot" => {
                i += 1;
                internal_slot = Some(
                    args.get(i)
                        .ok_or("missing --internal-worker-slot")?
                        .parse::<usize>()
                        .map_err(|e| e.to_string())?,
                );
            }
            "--qos" => {
                i += 1;
                qos = QosProfile::parse(args.get(i).ok_or("missing --qos value")?)?;
            }
            "--workers" => {
                i += 1;
                workers = args
                    .get(i)
                    .ok_or("missing --workers")?
                    .parse::<usize>()
                    .map_err(|e| e.to_string())?
                    .max(1);
            }
            "--watchdog-ms" => {
                i += 1;
                watchdog_ms = args
                    .get(i)
                    .ok_or("missing --watchdog-ms")?
                    .parse::<u64>()
                    .map_err(|e| e.to_string())?
                    .max(1);
            }
            "--frames-in" => {
                i += 1;
                frames_in = Some(args.get(i).ok_or("missing --frames-in")?.clone());
            }
            "--jsonl-out" => {
                i += 1;
                jsonl_out = Some(args.get(i).ok_or("missing --jsonl-out")?.clone());
            }
            "--self-test" => self_test = true,
            other => return Err(format!("unrecognized argument '{other}'")),
        }
        i += 1;
    }

    if let Some(slot) = internal_slot {
        return run_worker_loop(slot, qos);
    }
    if self_test {
        return run_self_test();
    }

    let in_path = frames_in.unwrap_or_else(|| "-".into());
    let results = if in_path == "-" {
        execute_pool_stream(io::stdin(), workers, qos, watchdog_ms)?
    } else {
        let f = File::open(&in_path).map_err(|e| format!("open {in_path}: {e}"))?;
        execute_pool_stream(f, workers, qos, watchdog_ms)?
    };

    let out_path = jsonl_out.unwrap_or_else(|| "-".into());
    let mut writer: Box<dyn Write> = if out_path == "-" {
        Box::new(BufWriter::new(io::stdout().lock()))
    } else {
        Box::new(BufWriter::new(
            File::create(&out_path).map_err(|e| format!("create {out_path}: {e}"))?,
        ))
    };
    for row in &results {
        let line = serde_json::to_string(row).map_err(|e| e.to_string())?;
        writer
            .write_all(line.as_bytes())
            .and_then(|()| writer.write_all(b"\n"))
            .map_err(|e| e.to_string())?;
    }
    writer.flush().map_err(|e| e.to_string())
}
