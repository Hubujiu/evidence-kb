import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from kb_platform.remote import LunaClient
from kb_platform.provider_admission import ProviderAdmission
from .contracts import digest
from .provider_capture import MeteredTransport, request_context
from .cache import StageCache, StageKey


class SDKGateway:
    def __init__(self, config, ledger, output, *, recovery=None, dependencies=None):
        self.provider = LunaClient(config)
        self.provider.client.close()
        self.ledger, self.output = ledger, output
        self.context, self.seen, self.blocked = {}, set(), False
        self.lock = threading.Lock()
        self.recovery_cache = StageCache(output / "recovery-cache") if dependencies else None
        self.dependencies = dependencies
        self.retry_of = {}
        self.cache_hits = []
        self.recovered_records = []
        if recovery:
            paths = sorted(
                p for directory in recovery for p in (directory / "physical-requests").glob("*.json")
            )
            records = {p.name: digest(json.loads(p.read_text("utf-8"))) for p in paths}
            (output / "recovery-record-hashes.json").write_text(
                json.dumps(records, indent=2), encoding="utf-8"
            )
            for p in paths:
                old = json.loads(p.read_text("utf-8"))
                self.recovered_records.append(old)
                key = self.stage_key(old["context"], old["payload"])
                if old["payload_sha256"] != digest(old["payload"]):
                    raise ValueError("CORRUPT_RECOVERY_PAYLOAD")
                if old["status"] == "COMPLETE":
                    self.recovery_cache.put(
                        key, dict(response=old["response"], physical_id=old["physical_request_id"])
                    )
                elif old["status"] == "ERROR":
                    self.retry_of[digest(key)] = old["physical_request_id"]
                else:
                    raise ValueError("UNRESOLVED_RECOVERY_REQUEST")
        admission = ProviderAdmission(self.provider, self.provider.cache_dir / "provider-admission")
        self.client = httpx.Client(
            transport=MeteredTransport(
                httpx.HTTPTransport(retries=0),
                ledger,
                output / "physical-requests",
                category="model",
                admission=admission,
            ),
            timeout=180,
            trust_env=False,
        )
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                status, body = (
                    400,
                    {"error": {"message": "gateway request rejected", "type": "invalid_request_error"}},
                )
                try:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    if not self.path.endswith("/chat/completions") or payload.get("stream"):
                        raise ValueError("ONLY_NONSTREAM_CHAT_SUPPORTED")
                    payload["max_tokens"] = min(
                        payload.pop("max_completion_tokens", payload.get("max_tokens") or 4096), 4096
                    )
                    limit = gateway.context.get("max_wire_codepoints")
                    if limit and len(json.dumps(payload.get("messages", []), ensure_ascii=False)) > limit:
                        raise ValueError("FROZEN_WIRE_INPUT_LIMIT_EXCEEDED")
                    with gateway.lock:
                        key = digest([gateway.context, payload])
                        stage_key = (
                            gateway.stage_key(gateway.context, payload) if gateway.recovery_cache else None
                        )
                        cached = (
                            gateway.recovery_cache.get(
                                stage_key, authorize=gateway.authorize_cache, mode="replay"
                            )
                            if stage_key
                            else None
                        )
                        if gateway.blocked or (key in gateway.seen and not cached):
                            raise ValueError("GATE_CLOSED_OR_DUPLICATE_UNSETTLED_PAYLOAD")
                        gateway.seen.add(key)
                        if cached:
                            gateway.cache_hits.append(
                                dict(key=digest(stage_key), physical_id=cached["physical_id"])
                            )
                            (output / "reused-requests.json").write_text(
                                json.dumps(gateway.cache_hits, indent=2), encoding="utf-8"
                            )
                            response = None
                            status, body = 200, cached["response"]
                        else:
                            context = {**gateway.context}
                            if stage_key and digest(stage_key) in gateway.retry_of:
                                context.update(
                                    retry_of=gateway.retry_of[digest(stage_key)],
                                    retry_reason="explicit recovery after terminal HTTP error; new physical identity",
                                )
                            with request_context(context):
                                response = gateway.client.post(
                                    gateway.provider.base_url + "/chat/completions",
                                    json=payload,
                                    headers=gateway.provider._headers,
                                )
                            status, body = response.status_code, response.json()
                            if status == 200 and stage_key:
                                candidates = [
                                    json.loads(p.read_text("utf-8"))
                                    for p in (output / "physical-requests").glob("*.json")
                                ]
                                matching = [
                                    r
                                    for r in candidates
                                    if r["payload_sha256"] == digest(payload)
                                    and r["context"].get("document") == gateway.context.get("document")
                                ]
                                if len(matching) != 1:
                                    raise ValueError("NEW_REQUEST_CACHE_BINDING_AMBIGUOUS")
                                gateway.recovery_cache.put(
                                    stage_key,
                                    dict(response=body, physical_id=matching[0]["physical_request_id"]),
                                )
                        if status >= 400:
                            gateway.blocked = True
                        print(
                            json.dumps(
                                {
                                    "phase": gateway.context,
                                    "status": status,
                                    "calls": gateway.ledger.summary()["physical_requests"],
                                }
                            ),
                            flush=True,
                        )
                except Exception as exc:
                    gateway.blocked = True
                    with (output / "local-rejections.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(
                            json.dumps(
                                dict(
                                    context=gateway.context,
                                    error=type(exc).__name__ + ": " + str(exc),
                                    payload=locals().get("payload"),
                                ),
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                    body = {
                        "error": {
                            "message": type(exc).__name__ + ": gateway closed; inspect saved records",
                            "type": "invalid_request_error",
                        }
                    }
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backend = dict(
            api_key="local-metered-gateway", base_url=f"http://127.0.0.1:{self.server.server_port}/v1"
        )

    def stage_key(self, context, payload):
        return StageKey(
            stage="official-sdk-" + context["stage"] + "-request",
            implementation=self.dependencies["installed_code_hash"],
            upstream=(digest(payload),),
            config_hash=digest(self.dependencies),
            corpus_hash=self.dependencies["pdfs"][context["document"]]["sha256"],
            index_snapshot="rebuild-from-exact-completed-prefix",
            principal="public-pdf-evaluation",
            acl_revision="public-originals/1",
            model_revision=payload["model"],
            sample_id="/".join(
                [
                    context["stage"],
                    context["document"],
                    context.get("variant", ""),
                    context.get("case_id", ""),
                ]
            ),
        )

    def authorize_cache(self):
        import hashlib
        from pathlib import Path

        return all(
            hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest() == p["sha256"]
            for p in self.dependencies["pdfs"].values()
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.client.close()
