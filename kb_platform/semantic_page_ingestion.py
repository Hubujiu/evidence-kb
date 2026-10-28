"""Explicit authorized canonical PDF Page encoding, capsule creation and build.

The provider owns physical reservations/usage. This bridge records and verifies
those receipts; it never retries, downloads assets, or reads research indices.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .core import PlatformError
from .rag_testing.budget import BudgetLedger
from .rag_testing.contracts import digest
from .semantic_pages_v2 import _vector

PROTOCOL = "authorized-canonical-page-document-encoding/1"
SOURCE_FIELDS = ("source_id", "source_version_id", "parse_snapshot_id", "source_text_sha256")


def _sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _ledger_row(ledger, request_id):
    with sqlite3.connect(Path(ledger.path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM calls WHERE id=?", (request_id,)).fetchone()
    if row is None:
        raise PlatformError("DOCUMENT_EMBEDDING_RECEIPT_INVALID", "Physical ledger row is required", 503)
    result = dict(row)
    for key in ("reserved", "usage"):
        result[key] = json.loads(result[key]) if result[key] is not None else None
    return result


def _registered(ledger, attempts):
    with sqlite3.connect(Path(ledger.path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM calls")}
    return [rid for rid in attempts if rid in ids]


def _remote_calls(ledger, attempts):
    rows = [_ledger_row(ledger, rid) for rid in _registered(ledger, attempts)]
    return sum((row["usage"] or {}).get("remote_embedding_calls",
               row["reserved"].get("remote_embedding_calls", 0)) for row in rows)


class CanonicalPageIngestion:
    def __init__(self, authority, provider, capsule_writer, ledger):
        if (
            not isinstance(ledger, BudgetLedger)
            or getattr(provider, "ledger", None) is not ledger
            or set(ledger.caps) not in ({"local_embedding_calls", "input_tokens"},
                                        {"remote_embedding_calls", "input_tokens"})
            or any(type(v) is not int or v < 1 for v in ledger.caps.values())
        ):
            raise ValueError("Provider must own the same finite unified document Embedding ledger")
        manifest = provider.manifest
        if (
            not isinstance(provider.provider_id, str)
            or not provider.provider_id
            or type(provider.dimension) is not int
            or not 1 <= provider.dimension <= 65536
            or not isinstance(manifest, dict)
            or manifest.get("provider") != provider.provider_id
            or manifest.get("dimension") != provider.dimension
            or type(manifest.get("max_tokens")) is not int
            or manifest["max_tokens"] < 1
        ):
            raise ValueError("Explicit provider identity/dimension/native window required")
        if not callable(capsule_writer) or not callable(getattr(provider, "documents_observed", None)):
            raise ValueError("Observed document encoder and capsule writer are required")
        self.authority, self.provider, self.writer, self.ledger = authority, provider, capsule_writer, ledger
        self.call_resource = next(k for k in ledger.caps if k != "input_tokens")
        if getattr(provider, "call_resource", self.call_resource) != self.call_resource:
            raise ValueError("Provider call resource differs from the frozen physical budget")
        self.manifest = deepcopy(manifest)

    def _receipt(self, result, request_id, texts, context):
        if not isinstance(result, dict):
            raise PlatformError(
                "DOCUMENT_EMBEDDING_RECEIPT_INVALID", "Observed document receipt required", 503
            )
        row = _ledger_row(self.ledger, request_id)
        vectors = result.get("vectors")
        if not isinstance(vectors, (list, tuple)) or len(vectors) != len(texts):
            raise PlatformError("DOCUMENT_EMBEDDING_RECEIPT_INVALID", "Document vector count differs", 503)
        vectors = [_vector(v, self.provider.dimension) for v in vectors]
        if (
            result.get("request_id") != request_id
            or result.get("physical_request_id") != request_id
            or result.get("task") != "document"
            or result.get("status") != "COMPLETE"
            or result.get("texts") != texts
            or result.get("text_sha256") != [hashlib.sha256(t.encode()).hexdigest() for t in texts]
            or result.get("audit_context") != context
            or result.get("manifest_sha256") != digest(self.manifest)
            or result.get("provider_manifest") != self.manifest
            or result.get("vectors_sha256") != digest(vectors)
            or row["status"] != "COMPLETE"
            or row["usage"] != result.get("usage")
            or row["reserved"] != result.get("reserved")
            or not isinstance(row["usage"], dict)
            or set(row["usage"]) != set(self.ledger.caps)
            or any(type(v) is not int or v < 0 for v in row["usage"].values())
            or any(row["usage"][k] > row["reserved"][k] for k in self.ledger.caps)
            or type(result.get("input_tokens")) is not int
            or result["input_tokens"] < 1
            or result["input_tokens"] != row["usage"]["input_tokens"]
            or row["usage"][self.call_resource] < 1
        ):
            raise PlatformError(
                "DOCUMENT_EMBEDDING_RECEIPT_INVALID", "Document input/vector/usage identity differs", 503
            )
        if self.provider.manifest != self.manifest:
            raise PlatformError(
                "DOCUMENT_EMBEDDING_PROVIDER_CHANGED", "Provider changed during encoding", 409
            )
        return vectors, {
            "physical_request_id": request_id,
            "receipt_sha256": digest(result),
            "ledger_row_sha256": digest(row),
            "usage": row["usage"],
            "text_sha256": result["text_sha256"],
            "vectors_sha256": digest(vectors),
        }

    def run(
        self,
        actor,
        kb_id,
        expected_state,
        expected_sources,
        output,
        *,
        execute=False,
        expected_provider_manifest_sha256,
        batch_size=2,
        build_service=None,
    ):
        """No permission mutation; every selected source must match a frozen plan.

        `expected_sources` is source-keyed {source_identity, original_sha256}.
        The writer may implement its provider's capsule format. Import readback
        and an optional existing build independently revalidate canonical data.
        """
        if not execute:
            raise ValueError("Explicit execute=True is required; nothing was encoded")
        output = Path(output).resolve()
        if output.exists():
            raise ValueError("Use a new output; inspect prior physical outcomes before a new attempt")
        if (
            type(batch_size) is not int
            or not 1 <= batch_size <= 64
            or batch_size > getattr(self.provider, "max_batch_size", 64)
            or not isinstance(expected_sources, dict)
            or not expected_sources
            or any(
                not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", k)
                for k in expected_sources
            )
            or digest(self.manifest) != expected_provider_manifest_sha256
            or not isinstance(expected_state, dict)
            or set(expected_state) != {"scope_id", "manifest_id", "authority_revision"}
            or expected_state["scope_id"] != kb_id
            or type(expected_state["authority_revision"]) is not int
            or self.ledger.summary()["pending_or_unknown"]
        ):
            raise ValueError(
                "Explicit fixed source/state/provider/batch plan and clear finite ledger required"
            )
        output.mkdir(parents=True, exist_ok=False)
        job_id, request_ids, capsules = uuid4().hex, [], {}
        _write(
            output / "plan.json",
            {
                "protocol": PROTOCOL,
                "job_id": job_id,
                "tenant_id": actor.tenant_id,
                "principal_id": actor.principal_id,
                "kb_id": kb_id,
                "expected_state": expected_state,
                "expected_sources": expected_sources,
                "provider_manifest": self.manifest,
                "provider_manifest_sha256": digest(self.manifest),
                "dimension": self.provider.dimension,
                "batch_size": batch_size,
                "caps": self.ledger.caps,
                "ledger_path": str(self.ledger.path),
                "automatic_retries": 0,
                "new_query_embedding_calls": 0,
                "remote_calls": 0,
                "execution_call_resource": self.call_resource,
                "optional_formal_build": build_service is not None,
            },
        )
        try:
            with self.authority.locked(actor, kb_id, manage=True) as snapshot:
                if snapshot["state"] != expected_state:
                    raise PlatformError(
                        "DOCUMENT_EMBEDDING_STALE", "Current build/authority differs from plan", 409
                    )
                rows = [r for r in snapshot["rows"] if r["source_id"] in expected_sources]
                if len(rows) != len(expected_sources) or {r["source_id"] for r in rows} != set(
                    expected_sources
                ):
                    raise PlatformError(
                        "ACCESS_DENIED", "Selected sources must be uniquely currently readable", 403
                    )
                if any(
                    not r["policy"].get("embedding")
                    or self.provider.provider_id not in r["policy"].get("allowed_providers", [])
                    for r in rows
                ):
                    raise PlatformError(
                        "PROCESSING_DENIED", "Explicit current provider Embedding grant required", 403
                    )
                catalog = self.authority.catalog(actor, kb_id, snapshot, rows)
                if set(catalog) != set(expected_sources):
                    raise PlatformError("DOCUMENT_EMBEDDING_STALE", "Canonical source catalog differs", 409)
                for source_id in sorted(catalog):
                    item = catalog[source_id]
                    expected = expected_sources[source_id]
                    if (
                        set(expected) != {"source_identity", "original_sha256"}
                        or set(expected["source_identity"]) != set(SOURCE_FIELDS)
                        or expected["source_identity"] != item["source_identity"]
                        or item["source_identity"]["source_id"] != source_id
                        or expected["original_sha256"] != item["original_sha256"]
                    ):
                        raise PlatformError(
                            "DOCUMENT_EMBEDDING_STALE", "Source/parse/hash differs from plan", 409
                        )
                    pages = [{k: p[k] for k in ("page_index", "start", "end", "text")} for p in item["pages"]]
                    text = item["source_text"]
                    if (
                        not pages
                        or hashlib.sha256(text.encode()).hexdigest()
                        != item["source_identity"]["source_text_sha256"]
                        or [p["page_index"] for p in pages] != sorted({p["page_index"] for p in pages})
                        or any(
                            type(p["page_index"]) is not int
                            or p["page_index"] < 0
                            or type(p["start"]) is not int
                            or type(p["end"]) is not int
                            or not 0 <= p["start"] < p["end"] <= len(text)
                            or text[p["start"] : p["end"]] != p["text"]
                            for p in pages
                        )
                    ):
                        raise PlatformError(
                            "DOCUMENT_EMBEDDING_STALE", "Canonical page order/range/text differs", 503
                        )
                    if any(left["end"] > right["start"] for left, right in zip(pages, pages[1:])):
                        raise PlatformError("DOCUMENT_EMBEDDING_STALE", "Canonical page ranges overlap", 503)
                    _write(
                        output / (source_id + "-canonical.json"),
                        {k: v for k, v in item.items() if k != "pages"}
                        | {
                            "ordered_pages": [
                                {k: p[k] for k in ("page_index", "start", "end")}
                                | {"text_sha256": hashlib.sha256(p["text"].encode()).hexdigest()}
                                for p in pages
                            ]
                        },
                    )
                    vectors, receipts = [], []
                    for offset in range(0, len(pages), batch_size):
                        if not snapshot["authorize"]():
                            raise PlatformError(
                                "SCOPE_CHANGED", "Authorization changed before document encoding", 409
                            )
                        batch = pages[offset : offset + batch_size]
                        request_id = uuid4().hex
                        request_ids.append(request_id)
                        texts = [p["text"] for p in batch]
                        context = {
                            "tenant_id": actor.tenant_id,
                            "kb_id": kb_id,
                            "build_id": expected_state["manifest_id"],
                            "principal": actor.principal_id,
                            "acl_revision": str(expected_state["authority_revision"]),
                            **item["source_identity"],
                            "sources": [item["source_identity"]],
                            "document_ingestion": {
                                "protocol": PROTOCOL,
                                "job_id": job_id,
                                "batch_offset": offset,
                                "ordered_pages": [
                                    {k: p[k] for k in ("page_index", "start", "end")} for p in batch
                                ],
                            },
                        }
                        _write(
                            output / (request_id + "-input.json"),
                            {
                                "request_id": request_id,
                                "texts": texts,
                                "audit_context": context,
                                "provider_manifest_sha256": digest(self.manifest),
                            },
                        )
                        result = self.provider.documents_observed(
                            texts,
                            request_id=request_id,
                            audit_context=context,
                            authorize=snapshot["authorize"],
                        )
                        _write(output / (request_id + "-receipt.json"), result)
                        matrix, receipt = self._receipt(result, request_id, texts, context)
                        if not snapshot["authorize"]():
                            raise PlatformError(
                                "SCOPE_CHANGED", "Authorization changed after document encoding", 409
                            )
                        vectors.extend(matrix)
                        receipts.append(receipt)
                    provenance = {
                        "protocol": PROTOCOL,
                        "job_id": job_id,
                        "current_authorization": expected_state,
                        "tenant_id": actor.tenant_id,
                        "principal_id": actor.principal_id,
                        "source_binding": {
                            k: v for k, v in item.items() if k not in ("source_text", "pages")
                        },
                        "physical_receipts": receipts,
                        "new_document_embedding_requests": len(receipts),
                        "private_research_index_used": False,
                    }
                    target = output / (source_id + ".capsule.json")
                    written = self.writer(
                        target,
                        source_identity=item["source_identity"],
                        pages=pages,
                        source_text=text,
                        vectors=vectors,
                        embedding_manifest=self.manifest,
                        provenance=provenance,
                        authorize=snapshot["authorize"],
                    )
                    if Path(written["path"]).resolve() != target or written["sha256"] != _sha(target):
                        raise PlatformError(
                            "DOCUMENT_CAPSULE_INVALID", "Writer capsule path/SHA differs", 503
                        )
                    imported = self.provider.import_capsule(
                        target,
                        expected_sha256=written["sha256"],
                        source_identity=item["source_identity"],
                        pages=pages,
                        source_text=text,
                        authorize=snapshot["authorize"],
                    )
                    if [list(v) for v in imported.vectors] != vectors or imported.manifest.get(
                        "new_embedding_calls"
                    ) != 0:
                        raise PlatformError(
                            "DOCUMENT_CAPSULE_INVALID",
                            "Capsule readback differs from actual document vectors",
                            503,
                        )
                    capsules[source_id] = {"path": str(target), "sha256": written["sha256"]}
                if not snapshot["authorize"]():
                    raise PlatformError(
                        "SCOPE_CHANGED", "Authorization changed before capsule completion", 409
                    )
            _write(output / "capsules.json", capsules)
            # Reacquire the existing build's authority lock; never nest it.
            built = (
                build_service.build(actor, kb_id, capsules, expected_state=expected_state)
                if build_service
                else None
            )
            result = {
                "status": "COMPLETE",
                "protocol": PROTOCOL,
                "capsules": capsules,
                "expected_state": expected_state,
                "document_attempt_ids": request_ids,
                "document_physical_request_ids": _registered(self.ledger, request_ids),
                "ledger": self.ledger.summary(),
                "formal_build": built,
                "new_query_embedding_calls": 0,
                "remote_calls": _remote_calls(self.ledger, request_ids),
                "automatic_retries": 0,
                "capsules_input_sha256": _sha(output / "capsules.json"),
            }
            _write(output / "result.json", result)
            return result
        except Exception as exc:
            _write(
                output / "result.json",
                {
                    "status": "ERROR",
                    "code": exc.code if isinstance(exc, PlatformError) else "DOCUMENT_INGESTION_FAILED",
                    "exception": type(exc).__name__,
                    "document_attempt_ids": request_ids,
                    "document_physical_request_ids": _registered(self.ledger, request_ids),
                    "ledger": self.ledger.summary(),
                    "remote_calls": _remote_calls(self.ledger, request_ids),
                    "completed_capsules": capsules,
                    "automatic_retries": 0,
                    "retry": "Inspect original physical ledger/captures and authority before a new explicit run",
                },
            )
            raise
