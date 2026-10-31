use std::io::{self, BufRead, Write};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::process::ExitCode;

use agent_harness::{MAX_REQUEST_BYTES, call};
use serde_json::{Value, json};

enum Frame {
    Request(Vec<u8>),
    TooLarge,
}

fn read_frame(reader: &mut impl BufRead, limit: usize) -> io::Result<Option<Frame>> {
    let mut request = Vec::new();
    let mut too_large = false;
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            if request.is_empty() && !too_large {
                return Ok(None);
            }
            break;
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let length = newline.unwrap_or(available.len());
        if !too_large {
            if length > limit.saturating_add(1).saturating_sub(request.len()) {
                too_large = true;
                request.clear();
            } else {
                request.reserve_exact(length);
                request.extend_from_slice(&available[..length]);
            }
        }
        reader.consume(length + usize::from(newline.is_some()));
        if newline.is_some() {
            break;
        }
    }
    if request.last() == Some(&b'\r') {
        request.pop();
    }
    Ok(Some(if too_large || request.len() > limit {
        Frame::TooLarge
    } else {
        Frame::Request(request)
    }))
}

fn response(frame: Frame) -> Value {
    match frame {
        Frame::Request(request) => catch_unwind(AssertUnwindSafe(|| call(&request)))
            .unwrap_or_else(|_| {
                json!({"ok": false, "error": {
                    "code": "panic", "message": "native harness panicked; request was not retried"
                }})
            }),
        Frame::TooLarge => json!({"ok": false, "error": {
            "code": "request_too_large", "message": "request exceeds protocol limit"
        }}),
    }
}

fn serve(reader: &mut impl BufRead, writer: &mut impl Write, limit: usize) -> io::Result<()> {
    while let Some(frame) = read_frame(reader, limit)? {
        serde_json::to_writer(&mut *writer, &response(frame)).map_err(io::Error::other)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    Ok(())
}

fn main() -> ExitCode {
    let result = serve(
        &mut io::stdin().lock(),
        &mut io::stdout().lock(),
        MAX_REQUEST_BYTES,
    );
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("agent-harness transport failed: {error}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufReader, Cursor};

    fn exchange(input: &[u8], limit: usize, chunk_size: usize) -> Vec<Value> {
        let mut output = Vec::new();
        let mut reader = BufReader::with_capacity(chunk_size, Cursor::new(input));
        serve(&mut reader, &mut output, limit).unwrap();
        std::str::from_utf8(&output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    #[test]
    fn malformed_json_is_one_structured_response_and_does_not_consume_the_next_request() {
        let replies = exchange(
            b"not-json\n{\"version\":99,\"command\":\"loop_create\"}\n",
            1024,
            3,
        );
        assert_eq!(replies.len(), 2);
        assert_eq!(replies[0]["error"]["code"], "invalid_json");
        assert_eq!(replies[1]["error"]["code"], "protocol_version");
    }

    #[test]
    fn oversized_frames_are_drained_once_without_unbounded_buffering() {
        let mut input = vec![b'x'; 10000];
        input.extend_from_slice(b"\n{\"version\":99}\n");
        let replies = exchange(&input, 32, 7);
        assert_eq!(replies.len(), 2);
        assert_eq!(replies[0]["error"]["code"], "request_too_large");
        assert_eq!(replies[1]["error"]["code"], "protocol_version");
    }

    #[test]
    fn exact_limit_accepts_lf_crlf_and_final_eof() {
        let payload = b"{\"version\":99}";
        for suffix in [b"\n".as_slice(), b"\r\n".as_slice(), b"".as_slice()] {
            let mut input = payload.to_vec();
            input.extend_from_slice(suffix);
            let replies = exchange(&input, payload.len(), 1);
            assert_eq!(replies.len(), 1);
            assert_eq!(replies[0]["error"]["code"], "protocol_version");
        }
        assert!(exchange(b"", 32, 8).is_empty());
    }

    #[test]
    fn native_handle_lifecycle_works_without_python_or_a_model_transport() {
        let request = json!({"version": 1, "command": "loop_create", "mode": "model-tools",
            "message": "hello", "tools": []});
        let first = exchange(request.to_string().as_bytes(), 1024, 8);
        let id = first[0]["result"]["loop_id"].clone();
        assert!(id.is_string());
        let next = json!({"version": 1, "command": "loop_next", "loop_id": id});
        let instruction = exchange(next.to_string().as_bytes(), 1024, 8);
        assert_eq!(instruction[0]["result"]["name"], "session.start");
        let accept = json!({"version": 1, "command": "loop_accept", "loop_id": id,
            "effect_id": instruction[0]["result"]["effect_id"], "result": {"cached": "saved"}});
        let close = json!({"version": 1, "command": "loop_close", "loop_id": id});
        let batch = format!("{accept}\n{next}\n{close}\n");
        let replies = exchange(batch.as_bytes(), 1024, 8);
        assert_eq!(
            replies[1]["result"],
            json!({"kind": "complete", "value": "saved"})
        );
        assert_eq!(replies[2]["result"], json!({"closed": true}));
    }

    #[test]
    fn malformed_utf8_and_empty_lines_return_errors_without_panicking() {
        let replies = exchange(&[0xff, b'\n', b'\n'], 32, 1);
        assert_eq!(replies.len(), 2);
        for reply in replies {
            assert_eq!(reply["ok"], false);
            assert_eq!(reply["error"]["code"], "invalid_json");
        }
    }
}
