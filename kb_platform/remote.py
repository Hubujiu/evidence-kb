from __future__ import annotations

import base64
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

import httpx

from .agent_engine import current_execution
from .embeddings import EmbeddingError
from .model_errors import RemoteModelError, TerminalContractError
from .luna_sdk import TRANSPORT_MANIFEST, chat_response
from .provider_runtime import (ProviderCircuit, RequestReceipts, RuntimePolicyError,
                               classify_failure, retry_after_seconds)


_FILE_LOCK = threading.Lock()
_RAPTOR_CORRECTION_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "raptor_summary",
        "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False, "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    },
}
_GRAPH_CORRECTION_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "graph_relations",
        "strict": True,
        "schema": {
            "type": "object", "additionalProperties": False, "required": ["relations"],
            "properties": {"relations": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["subject", "predicate", "object", "source_doc_ids", "quote"],
                    "properties": {
                        "subject": {"type": "string"}, "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "source_doc_ids": {"type": "array", "items": {"type": "string"}},
                        "quote": {"type": "string"},
                    },
                },
            }},
        },
    },
}


class _FifoSlots:
    def __init__(self, capacity: int):
        self._available = capacity
        self._waiters = deque()
        self._cond = threading.Condition()

    def __enter__(self):
        token = object()
        with self._cond:
            self._waiters.append(token)
            try:
                while self._waiters[0] is not token or self._available == 0:
                    self._cond.wait()
            except BaseException:
                self._waiters.remove(token)
                self._cond.notify_all()
                raise
            self._waiters.popleft()
            self._available -= 1
            self._cond.notify_all()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        with self._cond:
            self._available += 1
            self._cond.notify_all()


def _embedding_error(error):


    converted = EmbeddingError(str(error))
    converted.code, converted.status = error.code, error.status
    converted.retry_after_seconds = error.retry_after_seconds
    return converted


def _provider_guard(operation):

    def guarded(self, *args, **kwargs):
        try:
            probe = self._circuit.acquire()
        except RuntimePolicyError as exc:
            raise RemoteModelError(exc.code) from None
        try:
            result = operation(self, *args, **kwargs)
        except BaseException as exc:
            failure = classify_failure(exc)
            admission = getattr(exc, 'code', '') in {
                'PROVIDER_RATE_LIMIT_WAIT_EXCEEDED', 'PROVIDER_RETRY_WAIT_EXCEEDED',
                'PROVIDER_DEADLINE_EXCEEDED', 'PROVIDER_REQUEST_LIMIT', 'PROVIDER_REQUEST_UNRESOLVED'}
            self._circuit.finish(probe, failure=not admission and (failure.retryable or failure.outcome == 'UNKNOWN'
                or getattr(exc, 'code', '') == 'PROVIDER_TRANSPORT_ERROR'))
            raise
        self._circuit.finish(probe, success=True)
        return result
    return guarded


@dataclass
class _GoogleRequest:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    attempt: int = 0
    retries_used: int = 0
    auth_refreshes: int = 0
    authentication_retry: bool = False
    wait_seconds: float = 0.0
    retry_of: str | None = None
    retry_reason: str | None = None

    def metadata(self, waited):
        return {"request_id": self.request_id, "attempt": self.attempt, "format_attempt": 1,
                "transport_attempt": self.attempt, "auth_refreshes": self.auth_refreshes,
                "authentication_retry": self.authentication_retry, "rate_wait_seconds": waited,
                "retry_of": self.retry_of, "retry_reason": self.retry_reason}


class _GoogleRequestPolicy:
    def __init__(self, selected):
        defaults = {"requests_per_second": 0.0, "rate_limit_backoff_seconds": 1.0,
                    "rate_limit_backoff_max_seconds": 120.0, "max_retry_wait_seconds": 120.0}
        self.settings = {}
        for name, default in defaults.items():
            value = selected.get(name, default)
            if (isinstance(value, bool) or not isinstance(value, (float, int)) or
                    not math.isfinite(value) or value < 0 or name != "requests_per_second" and value == 0):
                raise RemoteModelError("INVALID_GOOGLE_REQUEST_POLICY")
            self.settings[name] = float(value)
        if self.settings["rate_limit_backoff_max_seconds"] < self.settings["rate_limit_backoff_seconds"]:
            raise RemoteModelError("INVALID_GOOGLE_REQUEST_POLICY")
        rate = self.settings["requests_per_second"]
        self._interval = 1.0 / rate if rate else 0.0
        if not math.isfinite(self._interval):
            raise RemoteModelError("INVALID_GOOGLE_REQUEST_POLICY")
        self._lock = threading.Lock()
        self._next_send = self._cooldown_until = 0.0
        self._clock, self._sleep, self._wall_clock = time.monotonic, time.sleep, time.time

    def wait(self, state):
        started = self._clock()
        try:
            while True:
                with self._lock:
                    now = self._clock()
                    delay = max(self._next_send, self._cooldown_until) - now
                    if delay <= 0:
                        self._next_send = now + self._interval
                        return
                    if state.wait_seconds + now - started + delay > self.settings["max_retry_wait_seconds"]:
                        raise RemoteModelError("PROVIDER_RATE_LIMIT_WAIT_EXCEEDED", 429)
                self._sleep(delay)
        finally:
            state.wait_seconds += max(0.0, self._clock() - started)

    def retry_after(self, response):
        return retry_after_seconds(response.headers.get("Retry-After"), wall_clock=self._wall_clock)

    def defer(self, retry_number, retry_after):
        delay = min(self.settings["rate_limit_backoff_max_seconds"],
                    self.settings["rate_limit_backoff_seconds"] * 2 ** retry_number)
        if retry_after is not None:
            delay = max(delay, retry_after)
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + delay)
        return delay


def _google_error_metadata(response, policy):
    metadata = {"http_error_type": "rate_limit" if response.status_code == 429 else
                "authentication" if response.status_code == 401 else
                "server" if response.status_code >= 500 else "client"}
    retry_after = policy.retry_after(response)
    if retry_after is not None:
        metadata["retry_after_seconds"] = retry_after
    try:
        error = response.json().get("error", {})
    except (ValueError, AttributeError):
        return metadata
    if not isinstance(error, dict):
        return metadata
    if isinstance(error.get("status"), str) and error["status"] in {
                              "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED",
                              "NOT_FOUND", "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED",
                              "FAILED_PRECONDITION", "ABORTED", "OUT_OF_RANGE", "UNIMPLEMENTED",
                              "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED"}:
        metadata["provider_error_status"] = error["status"]
    allowed_metrics = {
        "aiplatform.googleapis.com/global_embed_content_requests_per_minute_per_base_model",
        "aiplatform.googleapis.com/global_embed_content_input_tokens_per_minute_per_base_model",
        "aiplatform.googleapis.com/online_prediction_requests",
        "generativelanguage.googleapis.com/embed_content_paid_tier_requests",
        "generativelanguage.googleapis.com/embed_content_free_tier_requests",
        "generativelanguage.googleapis.com/embed_content_paid_tier_input_token_count",
        "generativelanguage.googleapis.com/embed_content_free_tier_input_token_count",
    }
    metrics = set()
    pending = [error.get("details", [])]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for name, value in item.items():
                if name in {"quota_metric", "quotaMetric", "metric"} and isinstance(value, str):
                    if value in allowed_metrics:
                        metrics.add(value)
                elif isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    if metrics:
        metadata["quota_metrics"] = sorted(metrics)
    return metadata


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _read_config(path, section):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    selected = config.get(section, {})
    required = ("base_url", "model", "api_key")
    if section == "google" and selected.get("platform") == "vertex_ai":
        required = ("model", "project_id", "location", "service_account_file")
    if not all(isinstance(selected.get(key), str) and selected[key] for key in required):
        raise RemoteModelError("PROVIDER_CONFIGURATION_MISSING")
    return config, selected


class _Transport:
    def __init__(self, config, selected, provider_id, *, trust_env):
        self.base_url = selected["base_url"].rstrip("/")
        self.model = selected["model"]
        self.provider_id = provider_id
        self.cache_dir = Path(config.get("cache_dir", ".local/remote-cache")) / provider_id
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(config.get("log_path", ".local/provider-calls.jsonl"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = min(max(float(selected.get("timeout_seconds", 180)), 1), 300)
        self.retries = min(max(int(selected.get("retries", 1)), 0), 2)
        self.max_requests = int(selected.get("max_requests", 1000))
        self.cache_mode = selected.get("cache_mode", "read_write")
        if self.cache_mode not in {"read_write", "disabled"}:
            raise RemoteModelError("INVALID_CACHE_MODE")
        self.client = httpx.Client(timeout=self.timeout, trust_env=trust_env)
        self._stats_lock = threading.Lock()
        self._issued_requests = 0
        try:
            self._circuit = ProviderCircuit(selected.get('circuit_failure_threshold', 3),
                                            selected.get('circuit_cooldown_seconds', 30))
        except RuntimePolicyError as exc:
            raise RemoteModelError(exc.code) from None
        self._receipts = RequestReceipts(self.cache_dir / 'request-receipts')

        retry_wait = 120 if provider_id == 'google_gemini' else selected.get('max_retry_wait_seconds', 120)
        if (isinstance(retry_wait, bool) or not isinstance(retry_wait, (int, float))
                or not math.isfinite(retry_wait) or retry_wait <= 0):
            raise RemoteModelError('INVALID_PROVIDER_RETRY_POLICY')
        self.max_retry_wait_seconds = float(retry_wait)
        self._stats = defaultdict(lambda: {"requests": 0, "cache_hits": 0, "tokens": 0,
                                          "prompt_tokens": 0, "completion_tokens": 0,
                                          "errors": 0, "elapsed_seconds": 0.0, "units": 0})

    def _record(self, purpose, key, *, request=False, cached=False, elapsed=0.0,
                error=None, usage=None, units=1, status=None, metadata=None):
        usage = usage or {}
        prompt = usage.get("prompt_tokens", usage.get("promptTokenCount", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("candidatesTokenCount", 0)) or 0
        tokens = usage.get("total_tokens", usage.get("totalTokenCount", prompt + completion)) or 0
        event = {"at": datetime.now(timezone.utc).isoformat(), "provider": self.provider_id,
                 "model": self.model, "purpose": purpose, "request_hash": key,
                 "request": request, "cache_hit": cached, "elapsed_seconds": elapsed,
                 "error": error, "http_status": status, "units": units,
                 "tokens": tokens, "tokens_reported": bool(usage),
                 "prompt_tokens": prompt, "completion_tokens": completion,
                 **(metadata or {})}
        with self._stats_lock:
            item = self._stats[purpose]
            item["requests"] += int(request)
            item["cache_hits"] += int(cached)
            item["errors"] += int(error is not None)
            item["elapsed_seconds"] += elapsed
            item["units"] += units
            item["tokens"] += tokens
            item["prompt_tokens"] += prompt
            item["completion_tokens"] += completion
        with _FILE_LOCK, self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")

    def snapshot_stats(self):
        with self._stats_lock:
            purposes = {key: dict(value) for key, value in self._stats.items()}
        fields = ("requests", "cache_hits", "tokens", "prompt_tokens", "completion_tokens",
                  "errors", "elapsed_seconds", "units")
        return {**{key: sum(item[key] for item in purposes.values()) for key in fields}, "by_purpose": purposes}

    def _cache_get(self, key):
        if self.cache_mode == "disabled":
            return None
        path = self.cache_dir / (key + ".json")
        if not path.is_file():
            return None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("key") == key and saved.get("value_hash") == _digest(saved["value"]):
                return saved["value"]
        except (ValueError, KeyError, OSError):
            pass
        raise RemoteModelError("PROVIDER_CACHE_CORRUPT")

    def _cache_put(self, key, value):
        if self.cache_mode == "disabled":
            return
        target = self.cache_dir / (key + ".json")
        temporary = self.cache_dir / (key + "." + uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"key": key, "value": value, "value_hash": _digest(value)},
                                        ensure_ascii=False), encoding="utf-8")
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                self._cache_get(key)
        finally:
            temporary.unlink(missing_ok=True)

    def _send_attempt(self, path, payload, headers, key, metadata):

        logical_id = metadata['request_id']
        deadline = metadata.get('deadline_monotonic')
        execution = current_execution()
        if execution is not None:
            if execution.cancellation.is_set():
                raise RemoteModelError('PROVIDER_CANCELLED')
            deadline = min(deadline, execution.deadline) if deadline is not None else execution.deadline
            metadata['deadline_monotonic'] = deadline
        remaining = deadline - time.monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise RemoteModelError('PROVIDER_DEADLINE_EXCEEDED')
        with self._stats_lock:
            if self._issued_requests >= self.max_requests:
                raise RemoteModelError('PROVIDER_REQUEST_LIMIT')
            try:
                physical_id = self._receipts.begin(logical_id, key, metadata)
            except RuntimePolicyError as exc:
                raise RemoteModelError(exc.code) from None
            self._issued_requests += 1
        metadata['physical_request_id'] = physical_id
        started = time.perf_counter()
        try:
            timeout = min(self.timeout, remaining) if remaining is not None else self.timeout
            from .agent_runtime import send_deadline

            with send_deadline(deadline):
                response = self._request_response(path, payload, headers, timeout)
        except BaseException as exc:
            failure = classify_failure(exc)
            state = failure.outcome if isinstance(exc, httpx.HTTPError) or failure.category == 'admission' else 'UNKNOWN'
            self._receipts.finish(logical_id, physical_id, state,
                                  error_type=type(exc).__name__)
            metadata.update(request_state=state, failure_category=failure.category if state != 'UNKNOWN' else 'outcome_unknown')
            raise
        elapsed = time.perf_counter() - started
        state = 'COMPLETE' if response.is_success else 'ERROR'
        self._receipts.finish(logical_id, physical_id, state, http_status=response.status_code)
        metadata['request_state'] = state
        return response, elapsed

    def _request_response(self, path, payload, headers, timeout):
        return self.client.post(self.base_url + path, json=payload, headers=headers, timeout=timeout)

    @_provider_guard
    def _post(self, path, payload, headers, purpose, key, units=1, *, trace=None):
        trace = trace if trace is not None else {'request_id': uuid4().hex, 'attempt': 0}
        waited = 0.0
        previous_id = trace.get('last_physical_request_id')
        previous_reason = 'format_correction' if previous_id and trace.get('format_attempt', 1) > 1 else None
        for attempt in range(self.retries + 1):
            with self._stats_lock:
                if self._issued_requests >= self.max_requests:
                    raise RemoteModelError("PROVIDER_REQUEST_LIMIT")
            trace["attempt"] += 1
            metadata = {**trace, 'transport_attempt': attempt + 1,
                        'retry_of': previous_id, 'retry_reason': previous_reason}
            started = time.perf_counter()
            try:
                response, elapsed = self._send_attempt(path, payload, headers, key, metadata)
                trace['last_physical_request_id'] = metadata['physical_request_id']
            except RemoteModelError:
                if 'physical_request_id' not in metadata:
                    trace['attempt'] -= 1
                raise
            except httpx.HTTPError as exc:
                trace['last_physical_request_id'] = metadata['physical_request_id']
                self._record(purpose, key, request=True, elapsed=time.perf_counter() - started,
                             error=type(exc).__name__, units=units, metadata=metadata)
                failure = classify_failure(exc)
                if failure.retryable and attempt < self.retries:
                    delay = .5 * 2 ** attempt
                    if waited + delay > self.max_retry_wait_seconds:
                        raise RemoteModelError('PROVIDER_RETRY_WAIT_EXCEEDED') from None
                    if metadata.get('deadline_monotonic', math.inf) - time.monotonic() <= delay:
                        raise RemoteModelError('PROVIDER_DEADLINE_EXCEEDED') from None
                    previous_id, previous_reason = metadata['physical_request_id'], failure.category
                    time.sleep(delay)
                    waited += delay
                    continue
                if failure.outcome == 'UNKNOWN':
                    raise RemoteModelError('PROVIDER_OUTCOME_UNKNOWN') from None
                raise RemoteModelError("PROVIDER_TRANSPORT_ERROR") from None
            except BaseException:
                if 'physical_request_id' not in metadata:
                    trace['attempt'] -= 1
                raise
            if not response.is_success:
                failure = classify_failure(status=response.status_code)
                retry_after = retry_after_seconds(response.headers.get('Retry-After'))
                metadata.update(failure_category=failure.category)
                if retry_after is not None:
                    metadata['retry_after_seconds'] = retry_after
                self._record(purpose, key, request=True, elapsed=elapsed, error="HTTP_ERROR",
                             status=response.status_code, units=units, metadata=metadata)
                if failure.retryable and attempt < self.retries:
                    delay = max(.5 * 2 ** attempt, retry_after or 0)
                    if waited + delay > self.max_retry_wait_seconds:
                        raise RemoteModelError('PROVIDER_RETRY_WAIT_EXCEEDED', response.status_code)
                    if metadata.get('deadline_monotonic', math.inf) - time.monotonic() <= delay:
                        raise RemoteModelError('PROVIDER_DEADLINE_EXCEEDED')
                    previous_id, previous_reason = metadata['physical_request_id'], failure.category
                    time.sleep(delay)
                    waited += delay
                    continue
                raise RemoteModelError("PROVIDER_HTTP_ERROR", response.status_code,
                                       retry_after_seconds=metadata.get('retry_after_seconds'))
            try:
                result = response.json()
            except ValueError:
                self._record(purpose, key, request=True, elapsed=elapsed, error="INVALID_RESPONSE_JSON",
                             status=response.status_code, units=units, metadata=metadata)
                raise RemoteModelError("PROVIDER_INVALID_RESPONSE_JSON") from None
            if not isinstance(result, dict):
                self._record(purpose, key, request=True, elapsed=elapsed, error="INVALID_RESPONSE_OBJECT",
                             status=response.status_code, units=units, metadata=metadata)
                raise RemoteModelError("PROVIDER_INVALID_RESPONSE_OBJECT")
            if trace is not None:
                metadata.update(_completion_metadata(result))
            self._record(purpose, key, request=True, elapsed=elapsed,
                         usage=result.get("usage", result.get("usageMetadata")), units=units,
                         status=response.status_code, metadata=metadata)
            return result

    def close(self):
        self.client.close()


def _completion_metadata(result):
    choices = result.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    finish = choice.get("finish_reason")
    return {"finish_reason": finish if finish is None or isinstance(finish, str) and finish in {
                "stop", "length", "content_filter", "tool_calls", "function_call"} else "other",
            "content_type": type(content).__name__,
            "content_length": len(content) if isinstance(content, (str, list)) else None}


class LunaClient(_Transport):
    provider_id = "magpie_luna"
    compile_purposes = frozenset({"wiki_compile", "graph_extract", "raptor_summarize"})
    contract_purposes = compile_purposes | {"ocr"}

    def _request_response(self, path, payload, headers, timeout):
        if path != '/chat/completions':
            raise RemoteModelError('INVALID_LUNA_ENDPOINT')
        return chat_response(self.client, self.base_url, headers, payload, timeout)

    def __init__(self, config_path=".local/providers.json"):
        config, selected = _read_config(config_path, "luna")
        super().__init__(config, selected, self.provider_id, trust_env=False)
        self._headers = {"Authorization": "Bearer " + selected["api_key"]}
        diagnostic_directory = selected.get("failed_response_dir")
        if diagnostic_directory is not None and (not isinstance(diagnostic_directory, str) or not diagnostic_directory.strip()):
            raise RemoteModelError("INVALID_FAILED_RESPONSE_DIRECTORY")
        self.failed_response_dir = Path(diagnostic_directory) if diagnostic_directory is not None else None
        self.manifest = {"provider": self.provider_id, "model": self.model, "base_url": self.base_url,
                         "protocol": "chat_completions", "adapter": "remote-models/0.1",
                         "transport": dict(TRANSPORT_MANIFEST),
                         "compile_format_recovery": "one_correction/2",
                         "compile_contract_cache": "validate_before_cache/1"}

    def _save_failed_response(self, result, metadata):
        if self.failed_response_dir is None:
            return {}
        temporary = None
        try:
            self.failed_response_dir.mkdir(parents=True, exist_ok=True)
            filename = f'{metadata["request_id"]}-{metadata["attempt"]}-{uuid4().hex}.json'
            target = self.failed_response_dir / filename
            temporary = target.with_suffix(".tmp")
            raw = json.dumps({"metadata": metadata, "response": result}, ensure_ascii=False).encode("utf-8")
            with temporary.open("xb") as stream:
                stream.write(raw)
            os.link(temporary, target)
            return {"failed_response_file": filename, "failed_response_sha256": hashlib.sha256(raw).hexdigest()}
        except OSError:
            return {"failed_response_write_error": True}
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _complete(self, system, user, purpose, images=None, json_output=False,
                  validator=None, contract_version=None):
        if not isinstance(system, str) or not isinstance(user, str) or not purpose:
            raise RemoteModelError("INVALID_COMPLETION_INPUT")
        if validator is not None or contract_version is not None:
            if (not callable(validator) or not isinstance(contract_version, str) or not contract_version.strip()
                    or not json_output or purpose not in self.contract_purposes):
                raise RemoteModelError("INVALID_COMPLETION_CONTRACT")
        if len(system) + len(user) > 200_000:
            raise RemoteModelError("COMPLETION_INPUT_TOO_LARGE")
        if json_output:
            system += "\nReturn one valid JSON object only. Do not include markdown fences or explanatory prose."
        content = [{"type": "text", "text": user}]
        for data in images or []:
            if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024:
                raise RemoteModelError("INVALID_IMAGE_INPUT")
            mime = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if data.startswith(b"\xff\xd8") else None
            if not mime:
                raise RemoteModelError("UNSUPPORTED_IMAGE_TYPE")
            content.append({"type": "image_url", "image_url": {
                "url": "data:" + mime + ";base64," + base64.b64encode(data).decode(), "detail": "high"}})
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                                                     {"role": "user", "content": content if images else user}]}
        semantic_key = _digest({"endpoint": self.base_url, "payload": payload, "purpose": purpose,
                                "json_output": json_output, "adapter": self.manifest["adapter"],
                                "transport": self.manifest["transport"]})
        key = _digest({"semantic_key": semantic_key, "contract_version": contract_version}) if validator is not None else semantic_key
        trace = {"request_id": uuid4().hex, "attempt": 0, "format_attempt": 0,
                 "semantic_request_hash": semantic_key, "contract_version": contract_version}
        saved = self._cache_get(key)
        if saved is not None:
            if validator is not None:
                try:
                    validator(saved)
                except ValueError:
                    self._record(purpose, key, error="PROVIDER_CACHE_CONTRACT_INVALID", units=0, metadata=trace)
                    raise RemoteModelError("PROVIDER_CACHE_CONTRACT_INVALID") from None
            self._record(purpose, key, cached=True, metadata=trace)
            return saved
        format_attempts = 2 if json_output and purpose in self.contract_purposes else 1
        for format_attempt in range(format_attempts):
            trace["format_attempt"] = format_attempt + 1
            trace["payload_hash"] = _digest(payload)
            result = self._post("/chat/completions", payload, self._headers, purpose, key, trace=trace)
            metadata = {**trace, **_completion_metadata(result), "event": "model_output"}
            validating_contract = False
            try:
                text = result["choices"][0]["message"]["content"]
                if isinstance(text, list):
                    text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("EMPTY_OR_NON_TEXT_CONTENT")
                if json_output:
                    text = text.strip()
                    if text.startswith("```") and text.endswith("```"):
                        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    value = json.loads(text)
                    if not isinstance(value, dict):
                        raise ValueError("NON_OBJECT_JSON")
                else:
                    value = text
                if validator is not None:
                    validating_contract = True
                    try:
                        validator(value)
                    except ValueError as exc:
                        code = str(exc)
                        error_type = TerminalContractError if isinstance(exc, TerminalContractError) else ValueError
                        raise error_type("CONTRACT_" + code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code)
                                         else "CONTRACT_VALIDATION_FAILED") from None
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                if validating_contract and not isinstance(exc, ValueError):
                    raise
                if isinstance(exc, json.JSONDecodeError):
                    problem = {"reason": "INVALID_JSON_SYNTAX", "message": exc.msg,
                               "position": exc.pos, "line": exc.lineno, "column": exc.colno}
                elif isinstance(exc, ValueError):
                    problem = {"reason": str(exc)}
                else:
                    problem = {"reason": "INVALID_COMPLETION_STRUCTURE", "exception_type": type(exc).__name__}
                metadata["parse_error"] = problem
                metadata.update(self._save_failed_response(result, {"request_hash": key, "purpose": purpose, **metadata}))
                self._record(purpose, key, error="INVALID_MODEL_OUTPUT", units=0, metadata=metadata)
                if format_attempt + 1 == format_attempts or isinstance(exc, TerminalContractError):
                    raise RemoteModelError("INVALID_MODEL_OUTPUT") from None
                correction = ("\nThe previous response could not be parsed: " + problem["reason"] +
                              ". Produce a fresh response for the original input and preserve all original requirements. "
                              "Return exactly one syntactically valid JSON object. Escape quotes and control characters "
                              "inside JSON strings. Do not include markdown fences or prose outside the object.")
                payload = {**payload, "messages": [{"role": "system", "content": system + correction}, payload["messages"][1]]}
                if purpose == "graph_extract":
                    payload["response_format"] = _GRAPH_CORRECTION_FORMAT
                    trace["correction_format"] = "graph_relations_json_schema/1"
                elif purpose == "raptor_summarize":
                    payload["response_format"] = _RAPTOR_CORRECTION_FORMAT
                    trace["correction_format"] = "raptor_summary_json_schema/1"
                continue
            self._record(purpose, key, units=0, metadata={**metadata, "format_recovered": format_attempt > 0})
            self._cache_put(key, value)
            return value

    def complete_json(self, system, user, *, purpose, images=None, validator=None, contract_version=None):
        return self._complete(system, user, purpose, images=images, json_output=True,
                              validator=validator, contract_version=contract_version)

    def complete_text(self, system, user, *, purpose):
        return self._complete(system, user, purpose)


class GoogleEmbeddingProvider(_Transport):
    provider_id = "google_gemini"

    @property
    def max_batch_size(self):

        return 16 if self.platform == 'gemini_api' else 1

    def __init__(self, config_path=".local/providers.json"):
        config, selected = _read_config(config_path, "google")
        self.platform = selected.get("platform", "gemini_api")
        if self.platform not in {"gemini_api", "vertex_ai"}:
            raise EmbeddingError("Unsupported Google API platform")
        self._auth_session = None
        if self.platform == "vertex_ai":
            if (selected["model"] != "gemini-embedding-2" or
                    not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", selected["project_id"]) or
                    selected["location"] not in {"us", "eu"}):
                raise EmbeddingError("Invalid Vertex embedding model, project or location")
            self.project_id, self.location = selected["project_id"], selected["location"]
            selected = {**selected, "base_url": f"https://aiplatform.{self.location}.rep.googleapis.com/v1"}
            self._init_vertex_auth(selected["service_account_file"])
            self.vertex_workers = int(selected.get("workers", 2))
            if not 1 <= self.vertex_workers <= 32:
                raise EmbeddingError("Vertex workers must be between 1 and 32")
            self._vertex_slots = _FifoSlots(self.vertex_workers)
        super().__init__(config, selected, self.provider_id, trust_env=bool(selected.get("trust_env", True)))
        self._request_policy = _GoogleRequestPolicy(selected)
        if self.model not in ("gemini-embedding-2", "gemini-embedding-001"):
            raise EmbeddingError("Unsupported fixed Google embedding model")
        self.dimension = int(selected.get("dimension", 768))
        if self.dimension not in (768, 1536, 3072):
            raise EmbeddingError("Unsupported Google embedding dimension")
        self._headers = {"x-goog-api-key": selected["api_key"]} if self.platform == "gemini_api" else {}
        self.manifest = {"provider": self.provider_id, "model": self.model, "dimension": self.dimension,
                         "base_url": self.base_url, "normalization": "l2", "adapter": "remote-embedding/0.1",
                         "document_format": "title: none | text: {text}" if self.model.endswith("-2") else "RETRIEVAL_DOCUMENT",
                         "query_format": "task: search result | query: {text}" if self.model.endswith("-2") else "RETRIEVAL_QUERY",
                         "max_input_characters": 12000, "execution": "google_api"}
        if self.platform == "vertex_ai":
            self.manifest.update(platform="vertex_ai", project_id=self.project_id, location=self.location,
                                 protocol="embedContent", adapter="vertex-embedding/0.1")
            self._vertex_path = (f"/projects/{self.project_id}/locations/{self.location}/publishers/google/models/"
                                 + self.model + ":embedContent")
            self.manifest["endpoint"] = self.base_url + self._vertex_path
        self.document_count = 0
        self.query_count = 0

    def snapshot_stats(self):
        return {**super().snapshot_stats(), "execution": {**self._request_policy.settings,
                "sharing_scope": "client", "retries": self.retries,
                "max_attempts_with_auth_refresh": self.retries + 2,
                "elapsed_includes_rate_wait": False}}

    @_provider_guard
    def _post(self, path, payload, headers, purpose, key, units=1, *, trace=None):
        state = trace if trace is not None else _GoogleRequest()
        policy = self._request_policy
        while True:
            with self._stats_lock:
                if self._issued_requests >= self.max_requests:
                    raise RemoteModelError("PROVIDER_REQUEST_LIMIT")
            before_wait = state.wait_seconds
            try:
                policy.wait(state)
            except RemoteModelError as exc:
                self._record(purpose, key, error=exc.code, units=0, status=exc.status,
                             metadata={**state.metadata(state.wait_seconds - before_wait), "event": "request_admission"})
                raise
            with self._stats_lock:
                if self._issued_requests >= self.max_requests:
                    raise RemoteModelError("PROVIDER_REQUEST_LIMIT")
            state.attempt += 1
            metadata = state.metadata(state.wait_seconds - before_wait)
            state.authentication_retry = False
            started = time.perf_counter()
            try:
                response, elapsed = self._send_attempt(path, payload, headers, key, metadata)
            except RemoteModelError:
                if 'physical_request_id' not in metadata:
                    state.attempt -= 1
                raise
            except httpx.HTTPError as exc:
                self._record(purpose, key, request=True, elapsed=time.perf_counter() - started,
                             error=type(exc).__name__, units=units, metadata=metadata)
                failure = classify_failure(exc)
                if failure.retryable and state.retries_used < self.retries:
                    policy.defer(state.retries_used, None)
                    state.retries_used += 1
                    state.retry_of, state.retry_reason = metadata['physical_request_id'], failure.category
                    continue
                if failure.outcome == 'UNKNOWN':
                    raise RemoteModelError('PROVIDER_OUTCOME_UNKNOWN') from None
                raise RemoteModelError("PROVIDER_TRANSPORT_ERROR") from None
            except BaseException:
                if 'physical_request_id' not in metadata:
                    state.attempt -= 1
                raise
            if not response.is_success:
                metadata.update(_google_error_metadata(response, policy))
                retryable = response.status_code in (429, 500, 502, 503, 504)
                if retryable:
                    metadata["rate_limit_cooldown_seconds"] = policy.defer(
                        state.retries_used, metadata.get("retry_after_seconds"))
                self._record(purpose, key, request=True, elapsed=elapsed, error="HTTP_ERROR",
                             status=response.status_code, units=units, metadata=metadata)
                if retryable and state.retries_used < self.retries:
                    state.retries_used += 1
                    state.retry_of, state.retry_reason = metadata['physical_request_id'], metadata['http_error_type']
                    continue
                state.retry_of, state.retry_reason = metadata['physical_request_id'], metadata['http_error_type']
                raise RemoteModelError("PROVIDER_HTTP_ERROR", response.status_code,
                                       retry_after_seconds=metadata.get('retry_after_seconds'))
            try:
                result = response.json()
            except ValueError:
                self._record(purpose, key, request=True, elapsed=elapsed, error="INVALID_RESPONSE_JSON",
                             status=response.status_code, units=units, metadata=metadata)
                raise RemoteModelError("PROVIDER_INVALID_RESPONSE_JSON") from None
            if not isinstance(result, dict):
                self._record(purpose, key, request=True, elapsed=elapsed, error="INVALID_RESPONSE_OBJECT",
                             status=response.status_code, units=units, metadata=metadata)
                raise RemoteModelError("PROVIDER_INVALID_RESPONSE_OBJECT")
            self._record(purpose, key, request=True, elapsed=elapsed,
                         usage=result.get("usage", result.get("usageMetadata")), units=units,
                         status=response.status_code, metadata=metadata)
            return result

    def _init_vertex_auth(self, credential_path):
        try:
            import requests
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            info = json.loads(Path(credential_path).read_text(encoding="utf-8-sig"))
            if (info.get("type") != "service_account" or info.get("project_id") != self.project_id or
                    info.get("token_uri") != "https://oauth2.googleapis.com/token" or
                    info.get("universe_domain", "googleapis.com") != "googleapis.com"):
                raise ValueError()
            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._auth_session = requests.Session()
            self._auth_session.trust_env = False
            self._auth_request = Request(session=self._auth_session)
            self._auth_lock = threading.Lock()
        except Exception:
            raise EmbeddingError("Invalid Google service-account configuration") from None

    def _vertex_headers(self, purpose, key, rejected_authorization=None):
        with self._auth_lock:
            try:
                current = "Bearer " + (self._credentials.token or "")
                if not self._credentials.valid or current == rejected_authorization:
                    def bounded_request(*args, **kwargs):
                        return self._auth_request(*args, **{**kwargs, "timeout": self.timeout})
                    self._credentials.refresh(bounded_request)
                if not isinstance(self._credentials.token, str) or not self._credentials.token:
                    raise ValueError()
                return {"Authorization": "Bearer " + self._credentials.token,
                        "x-goog-user-project": self.project_id}
            except Exception:
                self._record(purpose, key, error="GOOGLE_AUTHENTICATION_FAILED", units=0)
                raise RemoteModelError("GOOGLE_AUTHENTICATION_FAILED") from None

    def close(self):
        super().close()
        if self._auth_session is not None:
            self._auth_session.close()

    def _request_item(self, text, task):
        if not isinstance(text, str) or not text.strip() or len(text) > self.manifest["max_input_characters"]:
            raise EmbeddingError("Embedding text is empty or exceeds the explicit character budget")
        if self.model.endswith("-2"):
            text = ("title: none | text: " if task == "document" else "task: search result | query: ") + text
        result = {"model": "models/" + self.model, "content": {"parts": [{"text": text}]},
                  "outputDimensionality": self.dimension}
        if self.platform == "vertex_ai":
            result.pop("model")
        if self.model.endswith("-001"):
            result["taskType"] = "RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY"
        return result

    def _validated_vector(self, vector):
        if (not isinstance(vector, list) or len(vector) != self.dimension or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in vector)):
            raise EmbeddingError("Google returned an invalid embedding vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0 or not math.isfinite(norm):
            raise EmbeddingError("Google returned an invalid embedding norm")
        return [float(value / norm) for value in vector]

    def _embed(self, texts, task):
        requests = [self._request_item(text, task) for text in texts]
        keys = [_digest({"manifest": self.manifest, "request": item}) for item in requests]
        output, missing = [None] * len(texts), []
        purpose = "embedding_" + task
        for index, key in enumerate(keys):
            cached = self._cache_get(key)
            if cached is None:
                missing.append(index)
            else:
                output[index] = self._validated_vector(cached)
                self._record(purpose, key, cached=True)
        if self.platform == "vertex_ai":
            def embed_one(index):
                key, payload = keys[index], requests[index]
                trace = _GoogleRequest()
                with self._vertex_slots:
                    try:
                        headers = self._vertex_headers(purpose, key)
                        try:
                            response = self._post(self._vertex_path, payload, headers, purpose, key, trace=trace)
                        except RemoteModelError as exc:
                            if exc.status != 401:
                                raise
                            headers = self._vertex_headers(purpose, key, headers["Authorization"])
                            trace.auth_refreshes = 1
                            trace.authentication_retry = True
                            response = self._post(self._vertex_path, payload, headers, purpose, key, trace=trace)
                        vector = self._validated_vector(response.get("embedding", {}).get("values"))
                    except RemoteModelError as exc:
                        raise _embedding_error(exc) from None
                    except (EmbeddingError, AttributeError):
                        self._record(purpose, key, error="INVALID_EMBEDDING_OUTPUT", units=0)
                        raise EmbeddingError("Google returned invalid embeddings") from None
                    self._cache_put(key, vector)
                    return index, vector
            with ThreadPoolExecutor(max_workers=self.vertex_workers) as executor:


                futures = [executor.submit(copy_context().run, embed_one, index) for index in missing]
                for future in futures:
                    index, vector = future.result()
                    output[index] = vector
            return output
        for start in range(0, len(missing), 16):
            indices = missing[start:start + 16]
            payload = {"requests": [requests[index] for index in indices]}
            request_key = _digest({"manifest": self.manifest, "payload": payload})
            try:
                response = self._post("/models/" + self.model + ":batchEmbedContents", payload,
                                      self._headers, purpose, request_key, units=len(indices))
                items = response.get("embeddings")
                if not isinstance(items, list) or len(items) != len(indices):
                    raise EmbeddingError("Google returned an incorrect embedding count")
                vectors = [self._validated_vector(item.get("values")) for item in items]
            except RemoteModelError as exc:
                raise _embedding_error(exc) from None
            except (EmbeddingError, AttributeError):
                self._record(purpose, request_key, error="INVALID_EMBEDDING_OUTPUT", units=0)
                raise EmbeddingError("Google returned invalid embeddings") from None
            for index, vector in zip(indices, vectors):
                self._cache_put(keys[index], vector)
                output[index] = vector
        return output

    def documents(self, texts: list[str]) -> list[list[float]]:
        result = self._embed(texts, "document")
        self.document_count += len(texts)
        return result

    def query(self, query: str) -> list[float]:
        result = self._embed([query], "query")[0]
        self.query_count += 1
        return result
