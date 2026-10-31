from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from threading import Lock
from uuid import uuid4

from .embeddings import EmbeddingError
from .rag_testing.budget import BudgetLedger
from .rag_testing.contracts import digest
from .remote import GoogleEmbeddingProvider, RemoteModelError
from .semantic_units import (SOURCE_FIELDS, SemanticCapsuleImport, authorize_current,
                             normalized_vectors, validate_source_identity)

ROOT = Path(__file__).resolve().parents[1]


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pages(pages, source_text):
    if not isinstance(pages, (list, tuple)) or not pages:
        raise EmbeddingError("Nonempty original PDF Page inventory required")
    ordered, previous_index, previous_end = [], -1, -1
    for page in pages:
        index, start, end, text = (page.get(k) for k in ("page_index", "start", "end", "text"))
        if (type(index) is not int or index <= previous_index or type(start) is not int
                or type(end) is not int or not 0 <= start < end <= len(source_text)
                or start < previous_end or source_text[start:end] != text):
            raise EmbeddingError("Original Page order/range/text differs")
        ordered.append({"page_index": index, "start": start, "end": end,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        previous_index, previous_end = index, end
    return ordered


def write_google_page_capsule(path, *, source_identity, pages, source_text, vectors,
                               embedding_manifest, provenance, authorize):
    authorize_current(authorize)
    source = validate_source_identity(source_identity, source_text)
    ordered = _pages(pages, source_text)
    if (not isinstance(embedding_manifest, dict) or embedding_manifest.get("provider") != "google_gemini"
            or embedding_manifest.get("adapter") != "observed-google-semantic/1"
            or not isinstance(provenance, dict) or not provenance):
        raise EmbeddingError("Observed Google manifest and physical provenance required")
    matrix = normalized_vectors(vectors, len(ordered), embedding_manifest["dimension"])
    value = {"schema": "google-canonical-page-vector-capsule/1", "source": source,
             "embedding_manifest": deepcopy(embedding_manifest), "pages": ordered,
             "vectors": matrix, "provenance": deepcopy(provenance)}
    value["content_sha256"] = digest(value)
    authorize_current(authorize)
    path = Path(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    authorize_current(authorize)
    return {"path": str(path), "sha256": _sha(path), "pages": len(ordered),
            "ordered_pages_sha256": digest(ordered), "vectors_sha256": digest(matrix),
            "new_embedding_calls": 0}


class GoogleSemanticProvider:
    provider_id = "google_gemini"
    call_resource = "remote_embedding_calls"
    max_batch_size = 1

    def __init__(self, config_path=".local/providers.json", *, ledger=None, audit_dir=None,
                 remote_factory=GoogleEmbeddingProvider):
        if (not isinstance(ledger, BudgetLedger)
                or set(ledger.caps) != {self.call_resource, "input_tokens"}
                or any(type(v) is not int or v < 1 for v in ledger.caps.values())
                or audit_dir is None):
            raise EmbeddingError("Google requires the finite unified remote Embedding ledger and audit directory")
        self.remote = remote_factory(config_path)
        self.ledger, self.audit_dir = ledger, Path(audit_dir).resolve()
        self._lock = Lock()
        self.remote.retries = 0
        self.remote.cache_mode = "disabled"
        self._remote_manifest = deepcopy(self.remote.manifest)
        self.dimension = self.remote.dimension
        self.query_count = self.document_count = 0
        try:
            self.manifest = self._current_manifest()
        except Exception:
            self.remote.close()
            raise
        self.last_observation = None

    def _current_manifest(self):
        remote = self.remote.manifest
        if (remote.get("provider") != self.provider_id or remote.get("normalization") != "l2"
                or remote.get("model") not in {"gemini-embedding-2", "gemini-embedding-001"}
                or remote.get("dimension") != self.dimension):
            raise EmbeddingError("Explicit fixed Google model/dimension/task protocol required")
        from . import remote as remote_module, semantic_units
        return {"provider": self.provider_id, "model": remote["model"], "dimension": self.dimension,
                "max_tokens": 8192 if remote["model"] == "gemini-embedding-2" else 2048,
                "max_input_characters": remote["max_input_characters"],
                "normalization": "l2", "document_format": remote["document_format"],
                "query_format": remote["query_format"], "remote": deepcopy(remote),
                "adapter": "observed-google-semantic/1", "execution": "google_api",
                "input_admission": "formatted-text-utf8-byte-upper-bound/no-truncation",
                "usage_source": "provider-usageMetadata.promptTokenCount",
                "automatic_retries": 0, "embedding_cache": "disabled",
                "physical_batch_size": 1,
                "implementation_sha256": {Path(p).name: _sha(p) for p in
                    (__file__, remote_module.__file__, semantic_units.__file__)}}

    def _unchanged(self):
        if (self.remote.manifest != self._remote_manifest or self._current_manifest() != self.manifest
                or self.remote.retries != 0 or self.remote.cache_mode != "disabled"):
            raise EmbeddingError("Google embedding dependencies or execution policy changed; no fallback")

    def import_capsule(self, path, *, expected_sha256, source_identity, pages, source_text, authorize):
        authorize_current(authorize)
        self._unchanged()
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise EmbeddingError("Google capsule differs from admitted external SHA256")
        value = json.loads(raw)
        checksum = value.pop("content_sha256", None)
        ordered = _pages(pages, source_text)
        if (value.get("schema") != "google-canonical-page-vector-capsule/1" or digest(value) != checksum
                or value.get("source") != validate_source_identity(source_identity, source_text)
                or value.get("pages") != ordered or value.get("embedding_manifest") != self.manifest
                or not value.get("provenance")):
            raise EmbeddingError("Google capsule source/Page/model/provenance differs")
        matrix = normalized_vectors(value.get("vectors"), len(ordered), self.dimension)
        authorize_current(authorize)
        return SemanticCapsuleImport(tuple(tuple(v) for v in matrix), {
            "capsule_sha256": expected_sha256, "source": value["source"],
            "embedding_manifest_sha256": digest(self.manifest), "page_count": len(ordered),
            "ordered_pages_sha256": digest(ordered), "page_bindings": ordered,
            "vectors_sha256": digest(matrix), "provenance": value["provenance"], "new_embedding_calls": 0})

    def _observed(self, texts, *, task, request_id, audit_context, authorize):
        authorize_current(authorize)
        self._unchanged()
        if self.ledger.summary()["pending_or_unknown"]:
            raise EmbeddingError("Unresolved physical outcome requires inspection before another embedding request")
        sources = audit_context.get("sources") if isinstance(audit_context, dict) else None
        if (not isinstance(audit_context, dict)
                or any(not audit_context.get(k) for k in ("tenant_id", "kb_id", "build_id", "principal", "acl_revision"))
                or not isinstance(sources, list) or not sources
                or any(not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k]
                    for k in SOURCE_FIELDS) for s in sources)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id)
                or not isinstance(texts, list) or len(texts) != 1):
            raise EmbeddingError("One original input and explicit source/build/ACL/physical identity required")
        payload = self.remote._request_item(texts[0], task)
        formatted = payload["content"]["parts"][0]["text"]
        if len(formatted.encode("utf-8")) > self.manifest["max_tokens"]:
            raise EmbeddingError("Formatted input exceeds conservative native token admission; no truncation")
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        target = self.audit_dir / (request_id + ".json")
        if target.exists():
            raise EmbeddingError("Existing physical request capture must not be overwritten or resent")
        reserved = {self.call_resource: 1, "input_tokens": self.manifest["max_tokens"]}
        record = {"request_id": request_id, "physical_request_id": request_id, "task": task,
                  "texts": texts, "text_sha256": [hashlib.sha256(texts[0].encode()).hexdigest()],
                  "request_payload_sha256": digest(payload), "provider_manifest": deepcopy(self.manifest),
                  "manifest_sha256": digest(self.manifest), "audit_context": deepcopy(audit_context),
                  "reserved": reserved, "status": "STARTED", "automatic_retries": 0}
        self.ledger.reserve(request_id, reserved, "google_canonical_" + task, require_settled=True)
        usage, response_received, started, issued_before = None, False, False, None
        try:
            with target.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            with self._lock:
                authorize_current(authorize)
                self._unchanged()
                if self.remote.platform == "vertex_ai":
                    path = self.remote._vertex_path
                    headers = self.remote._vertex_headers("embedding_" + task, digest(payload))
                else:
                    path = "/models/" + self.remote.model + ":embedContent"
                    headers = self.remote._headers


                issued_before = self.remote._issued_requests
                started = True
                response = self.remote._post(path, payload, headers, "embedding_" + task, digest(payload))
            response_received = True
            record["response_sha256"] = digest(response)
            metadata = response.get("usageMetadata")
            tokens = metadata.get("promptTokenCount") if isinstance(metadata, dict) else None
            usage = {self.call_resource: 1}
            if type(tokens) is not int or not 0 < tokens <= reserved["input_tokens"]:
                raise EmbeddingError("Google did not report bounded actual input token usage")
            usage["input_tokens"] = tokens
            record["provider_usage"] = deepcopy(metadata)
            matrix = [self.remote._validated_vector(response.get("embedding", {}).get("values"))]
            matrix = normalized_vectors(matrix, 1, self.dimension)
            authorize_current(authorize)
            self._unchanged()
            record.update(vectors=matrix, vectors_sha256=digest(matrix), input_tokens=tokens,
                          status="COMPLETE", usage=usage, physical_embedding_attempts=1)
            self.ledger.finish(request_id, status="COMPLETE", usage=usage)
            self.last_observation = deepcopy(record)
            return matrix, deepcopy(record)
        except Exception as exc:
            issued = self.remote._issued_requests - issued_before if issued_before is not None else 0
            admission_blocked = getattr(exc, 'code', None) == 'RAG_PHYSICAL_REQUEST_ADMISSION_FAILED'
            if admission_blocked:
                record['native_attempts_before_admission'] = issued
                issued = 0
            record["physical_embedding_attempts"] = issued
            if not started or issued == 0:
                usage = {self.call_resource: 0, "input_tokens": 0}
            elif usage is None:
                usage = {self.call_resource: 1}
            known = (response_received or not started or issued == 0
                     or (isinstance(exc, RemoteModelError) and (exc.status is not None
                         or exc.code in {"PROVIDER_INVALID_RESPONSE_JSON", "PROVIDER_INVALID_RESPONSE_OBJECT"})))
            status = "ERROR" if known else "UNKNOWN"

            diagnostic = type(exc).__name__
            self.ledger.finish(request_id, status=status, usage=usage, diagnostic=diagnostic)
            record.update(status=status, usage=usage, error=diagnostic)
            raise
        finally:
            target.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False), "utf-8")

    def documents_observed(self, texts, *, request_id, audit_context, authorize):
        matrix, record = self._observed(texts, task="document", request_id=request_id,
                                       audit_context=audit_context, authorize=authorize)
        self.document_count += len(texts)
        return {**record, "vectors": matrix}

    def query_observed(self, text, *, request_id, audit_context, authorize):
        matrix, record = self._observed([text], task="query", request_id=request_id,
                                       audit_context=audit_context, authorize=authorize)
        self.query_count += 1
        return {**record, "vector": matrix[0], "vector_sha256": digest(matrix[0])}

    def query(self, text, *, request_id=None, audit_context=None, authorize=None):
        return self.query_observed(text, request_id=request_id or uuid4().hex,
            audit_context=audit_context, authorize=authorize)["vector"]

    def documents(self, texts, *, request_id=None, audit_context=None, authorize=None):
        return self.documents_observed(texts, request_id=request_id or uuid4().hex,
            audit_context=audit_context, authorize=authorize)["vectors"]

    def close(self):
        self.remote.close()
