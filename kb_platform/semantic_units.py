"""Original-span semantic unit contracts, independent of document pagination.

These helpers bind representation inputs to immutable parse text. They do not
assign page numbers to Office/text documents or execute an embedding provider.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path

from .embeddings import EmbeddingError
from .rag_testing.contracts import digest

SOURCE_FIELDS = ("source_id", "source_version_id", "parse_snapshot_id", "source_text_sha256")


def validate_source_identity(identity, text):
    if (not isinstance(text, str) or not isinstance(identity, dict)
            or any(not isinstance(identity.get(k), str) or not identity[k] for k in SOURCE_FIELDS)
            or hashlib.sha256(text.encode()).hexdigest() != identity["source_text_sha256"]):
        raise EmbeddingError("Immutable semantic source identity/text differs")
    return {k: identity[k] for k in SOURCE_FIELDS}


def unit_bindings(units, source_text):
    """Validate ordered exact intervals; locators are preserved, never invented."""
    if not isinstance(units, (list, tuple)) or not units or not isinstance(source_text, str):
        raise EmbeddingError("Nonempty canonical semantic units and source text required")
    result, seen, previous = [], set(), -1
    for item in units:
        if not isinstance(item, dict):
            raise EmbeddingError("Canonical semantic unit must be an object")
        unit_id, kind, start, end, text = (item.get(k) for k in ("unit_id", "kind", "start", "end", "text"))
        if (not isinstance(unit_id, str) or not unit_id or unit_id in seen
                or not isinstance(kind, str) or not kind
                or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(source_text) or start < previous
                or not isinstance(text, str) or source_text[start:end] != text
                or not isinstance(item.get("anchors", []), list)):
            raise EmbeddingError("Canonical semantic unit identity/order/range/text differs")
        # Overlap is allowed: e.g. a Table and its parent context are distinct
        # legitimate retrieval representations of the same original interval.
        result.append({"unit_id": unit_id, "kind": kind, "start": start, "end": end,
                       "anchors": deepcopy(item.get("anchors", [])),
                       "parent_id": item.get("parent_id"),
                       "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
        seen.add(unit_id)
        previous = start
    return result


def normalized_vectors(vectors, count, dimension):
    if not isinstance(vectors, (list, tuple)) or len(vectors) != count:
        raise EmbeddingError("Semantic vector count differs from original unit inventory")
    result = []
    for vector in vectors:
        if (not isinstance(vector, (list, tuple)) or len(vector) != dimension
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector)
                or not math.isclose(math.sqrt(sum(v*v for v in vector)), 1.0, abs_tol=1e-4)):
            raise EmbeddingError("Finite L2-normalized vectors with admitted dimensions required")
        result.append([float(v) for v in vector])
    return result


def authorize_current(callback):
    if not callable(callback) or callback() is not True:
        raise EmbeddingError("Current semantic processing authorization required")


@dataclass(frozen=True)
class SemanticCapsuleImport:
    vectors: tuple[tuple[float, ...], ...]
    manifest: dict


def write_unit_vector_capsule(path, *, source_identity, units, source_text, vectors,
                              embedding_manifest, provenance, authorize):
    authorize_current(authorize)
    source = validate_source_identity(source_identity, source_text)
    ordered = unit_bindings(units, source_text)
    if not isinstance(embedding_manifest, dict) or not isinstance(provenance, dict) or not provenance:
        raise EmbeddingError("Admitted embedding manifest and observed provenance required")
    matrix = normalized_vectors(vectors, len(ordered), embedding_manifest["dimension"])
    payload = {"schema": "canonical-semantic-unit-vector-capsule/1", "source": source,
               "embedding_manifest": deepcopy(embedding_manifest), "units": ordered,
               "vectors": matrix, "provenance": deepcopy(provenance)}
    payload["content_sha256"] = digest(payload)
    authorize_current(authorize)
    path = Path(path)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    authorize_current(authorize)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "unit_count": len(ordered), "new_embedding_calls": 0}


def import_unit_vector_capsule(path, *, expected_sha256, source_identity, units, source_text,
                               embedding_manifest, authorize):
    authorize_current(authorize)
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise EmbeddingError("Semantic capsule file differs from admitted external SHA256")
    value = json.loads(raw)
    checksum = value.pop("content_sha256", None)
    ordered = unit_bindings(units, source_text)
    if (value.get("schema") != "canonical-semantic-unit-vector-capsule/1" or digest(value) != checksum
            or value.get("source") != validate_source_identity(source_identity, source_text)
            or value.get("units") != ordered or value.get("embedding_manifest") != embedding_manifest
            or not value.get("provenance")):
        raise EmbeddingError("Semantic capsule source/units/model/provenance differs")
    matrix = normalized_vectors(value.get("vectors"), len(ordered), embedding_manifest["dimension"])
    authorize_current(authorize)
    return SemanticCapsuleImport(tuple(tuple(v) for v in matrix), {
        "capsule_sha256": expected_sha256, "source": value["source"],
        "embedding_manifest_sha256": digest(embedding_manifest), "unit_count": len(ordered),
        "ordered_units_sha256": digest(ordered), "vectors_sha256": digest(matrix),
        "provenance": value["provenance"], "new_embedding_calls": 0})
