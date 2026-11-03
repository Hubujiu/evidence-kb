from copy import deepcopy
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from kb_platform.remote import LunaClient
from kb_platform.provider_admission import ProviderAdmission
from .contracts import digest
from .provider_capture import MeteredTransport, capture_unresolved, request_context
from .cache import StageCache, StageKey


class SDKGateway:
    def __init__(self, config, ledger, output, *, recovery=None, dependencies=None):
        self.provider = LunaClient(config)
        self.provider.client.close()
        self.ledger, self.output = ledger, output
        self._context, self.seen, self.blocked = {}, set(), False
        self.lock = threading.RLock()
        self.close_lock = threading.Lock()
        self.closing, self.closed = False, False
        self.recovery_cache = StageCache(output / "recovery-cache") if dependencies else None
        self.dependencies = dependencies
        self.retry_of = {}
        self.cache_hits = []
        self.recovered_records = []
        if recovery:
            if self.recovery_cache is None:
                raise ValueError("RECOVERY_DEPENDENCIES_REQUIRED")
            paths = sorted(
                p for directory in recovery for p in (directory / "physical-requests").glob("*.json")
            )
            records = {str(p.resolve()): digest(json.loads(p.read_text("utf-8"))) for p in paths}
            (output / "recovery-record-hashes.json").write_text(
                json.dumps(records, indent=2), encoding="utf-8"
            )
            groups, identities = {}, {}
            for p in paths:
                old = json.loads(p.read_text("utf-8"))
                self.recovered_records.append(old)
                key = self.stage_key(old["context"], old["payload"])
                if old["payload_sha256"] != digest(old["payload"]):
                    raise ValueError("CORRUPT_RECOVERY_PAYLOAD")
                if capture_unresolved(old):
                    raise ValueError("UNRESOLVED_RECOVERY_REQUEST")
                identity = old["physical_request_id"]
                if identity in identities and identities[identity] != old:
                    raise ValueError("CONFLICTING_RECOVERY_PHYSICAL_IDENTITY")
                identities[identity] = old
                groups.setdefault(digest(key), (key, {}))[1][identity] = old
            for key_hash, (key, group) in groups.items():
                completed = [old for old in group.values() if old["status"] == "COMPLETE"]
                if completed:
                    if len(completed) != 1 or completed[0].get("http_status") != 200:
                        raise ValueError("AMBIGUOUS_OR_UNREPLAYABLE_RECOVERY_RESULT")
                    old = completed[0]
                    self.recovery_cache.put(
                        key, dict(response=old["response"], physical_id=old["physical_request_id"])
                    )
                else:
                    referenced = {old["context"].get("retry_of") for old in group.values()}
                    leaves = set(group) - referenced
                    if len(leaves) != 1:
                        raise ValueError("AMBIGUOUS_RECOVERY_RETRY_LINEAGE")
                    latest = leaves.pop()
                    linked, current = set(), latest
                    while current in group and current not in linked:
                        linked.add(current)
                        current = group[current]["context"].get("retry_of")
                    if linked != set(group) or current in linked:
                        raise ValueError("AMBIGUOUS_RECOVERY_RETRY_LINEAGE")
                    self.retry_of[key_hash] = latest
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
            timeout = 180

            def log_message(self, *args):
                pass

            def do_POST(self):
                with gateway.lock:
                    context = deepcopy(gateway._context)
                    status, valid_request = 400, False
                    try:
                        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                        if not self.path.endswith("/chat/completions") or payload.get("stream"):
                            raise ValueError("ONLY_NONSTREAM_CHAT_SUPPORTED")
                        payload["max_tokens"] = min(
                            payload.pop("max_completion_tokens", payload.get("max_tokens") or 4096), 4096
                        )
                        limit = context.get("max_wire_codepoints")
                        if limit and len(json.dumps(payload.get("messages", []), ensure_ascii=False)) > limit:
                            raise ValueError("FROZEN_WIRE_INPUT_LIMIT_EXCEEDED")
                        valid_request = True
                        key = digest([context, payload])
                        stage_key = gateway.stage_key(context, payload) if gateway.recovery_cache else None
                        if gateway.blocked or gateway.closing:
                            raise ValueError("GATE_CLOSED")
                        cached = (
                            gateway.recovery_cache.get(
                                stage_key, authorize=gateway.authorize_cache, mode="replay"
                            )
                            if stage_key else None
                        )
                        if key in gateway.seen and not cached:
                            raise ValueError("DUPLICATE_UNSETTLED_PAYLOAD")
                        gateway.seen.add(key)
                        if cached:
                            gateway.cache_hits.append(
                                dict(key=digest(stage_key), physical_id=cached["physical_id"])
                            )
                            (output / "reused-requests.json").write_text(
                                json.dumps(gateway.cache_hits, indent=2), encoding="utf-8"
                            )
                            status, body = 200, cached["response"]
                        else:
                            physical_context = deepcopy(context)
                            if stage_key and digest(stage_key) in gateway.retry_of:
                                physical_context.update(
                                    retry_of=gateway.retry_of[digest(stage_key)],
                                    retry_reason="explicit recovery after terminal capture error; new physical identity",
                                )
                            with request_context(physical_context):
                                response = gateway.client.post(
                                    gateway.provider.base_url + "/chat/completions",
                                    json=payload,
                                    headers=gateway.provider._headers,
                                )
                            status, body = response.status_code, response.json()
                            if status == 200 and stage_key:
                                physical_id = response.extensions["_kb_physical_request_id"]
                                if not isinstance(physical_id, str) or re.fullmatch(r"[0-9a-f]{32}", physical_id) is None:
                                    raise ValueError("INVALID_NEW_REQUEST_IDENTITY")
                                record = json.loads((output / "physical-requests" / (physical_id + ".json"))
                                                    .read_text("utf-8"))
                                if (record["physical_request_id"] != physical_id
                                        or record["payload_sha256"] != digest(payload)
                                        or record["payload"] != payload or record["context"] != physical_context
                                        or record["status"] != "COMPLETE" or capture_unresolved(record)
                                        or record["http_status"] != 200 or record["response"] != body):
                                    raise ValueError("NEW_REQUEST_CACHE_BINDING_MISMATCH")
                                gateway.recovery_cache.put(stage_key, dict(response=body, physical_id=physical_id))
                        if status >= 400:
                            gateway.blocked = True
                        print(
                            json.dumps({"phase": context, "status": status,
                                        "calls": gateway.ledger.summary()["physical_requests"]}),
                            flush=True,
                        )
                    except Exception as exc:
                        gateway.blocked = True
                        status = 500 if valid_request else 400
                        body = {"error": {
                            "message": type(exc).__name__ + ": gateway closed; inspect saved records",
                            "type": "gateway_error" if valid_request else "invalid_request_error",
                        }}
                        try:
                            with (output / "local-rejections.jsonl").open("a", encoding="utf-8") as handle:
                                handle.write(json.dumps(dict(context=context,
                                    error=type(exc).__name__ + ": " + str(exc), payload=locals().get("payload")),
                                    ensure_ascii=False) + "\n")
                        except OSError:
                            pass
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.server.block_on_close = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.backend = dict(
            api_key="local-metered-gateway", base_url=f"http://127.0.0.1:{self.server.server_port}/v1"
        )

    @property
    def context(self):
        with self.lock:
            return deepcopy(self._context)

    @context.setter
    def context(self, value):
        with self.lock:
            if self.closing:
                raise ValueError("GATEWAY_CLOSING")
            self._context = deepcopy(value)

    def stage_key(self, context, payload):
        return StageKey(
            stage="official-sdk-" + context["stage"] + "-request",
            implementation=self.dependencies["installed_code_hash"],
            upstream=(digest(payload), digest({k: v for k, v in context.items()
                                               if k not in {"retry_of", "retry_reason"}})),
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
        with self.close_lock:
            if self.closed:
                return
            with self.lock:
                self.closing = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.client.close()
            if self.recovery_cache is not None:
                self.recovery_cache.close()
            self.closed = True
