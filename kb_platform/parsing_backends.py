"""Explicit local parser routing at the real document-ingestion boundary.

Backends execute their official installed implementation in isolated runtimes.
Selecting a backend is a commitment: errors produce FAILED, never native success.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .pdf_layout import pdf_layout_features

ROOT = Path(__file__).resolve().parents[1]
ROUTE_VERSION = "page-layout-local/3"
RUNTIMES = {"docling": ".local/rag-quality-cpu/Scripts/python.exe",
            "docling_ocr": ".local/rag-quality-cpu/Scripts/python.exe",
            "rapidocr": ".local/rag-quality-cpu/Scripts/python.exe"}


def selected_backend(data: bytes, filename: str, policy: dict):
    """Document-only routing: never inspect a query, gold, or fixture identity."""
    route = os.environ.get("EVIDENCE_KB_PARSE_ROUTE", "auto").strip().lower()
    if route not in {"auto", "native", *RUNTIMES}:
        raise ValueError("invalid configured document parser route: " + route)
    if route == "native" or Path(filename).suffix.lower() != ".pdf":
        return "native", {"route": route}
    features = pdf_layout_features(data)
    needs_ocr = any(page["ocr_needed"] for page in features["pages"])
    if needs_ocr and not policy.get("ocr", False):
        return "native", {"route": route, "features": features, "ocr_blocked_by_policy": True}
    if route == "auto":
        backend = os.environ.get("EVIDENCE_KB_LAYOUT_BACKEND", "docling").strip().lower()
        if backend not in RUNTIMES:
            raise ValueError("invalid configured layout backend: " + backend)
        backend = backend if features["prefer_layout_ocr"] else "native"
    else:
        backend = route
    return backend, {"route": route, "features": features, "ocr_allowed": bool(policy.get("ocr", False))}


def external_worker(payload, backend):
    default_runtime = os.environ.get("EVIDENCE_KB_DOCLING_PYTHON", str(ROOT / RUNTIMES[backend])) if backend in {"docling_ocr", "rapidocr"} else str(ROOT / RUNTIMES[backend])
    runtime = Path(os.environ.get("EVIDENCE_KB_" + backend.upper() + "_PYTHON", default_runtime))
    if not runtime.is_file():
        raise RuntimeError(f"external_parser_runtime_unavailable:{backend}:{runtime}")
    payload = {**payload, 'backend': backend}
    timeout = float(os.environ.get("EVIDENCE_KB_EXTERNAL_PARSE_TIMEOUT_SECONDS", "180"))
    worker = {"docling_ocr": "parse_forced_document.py", "rapidocr": "parse_direct_ocr.py"}.get(backend, "parse_external_document.py")
    completed = subprocess.run([str(runtime), "-X", "utf8", str(ROOT / "scripts" / worker)],
                               input=json.dumps(payload), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=ROOT)
    if completed.returncode:
        raise RuntimeError("external_parser_worker_failed:" + backend + f":returncode={completed.returncode}:" + completed.stderr[-1200:])
    result = json.loads(completed.stdout)
    if result.get("error"):
        raise RuntimeError("external_parser_failed:" + backend + ":" + result["error"])
    return result


def external_result(data: bytes, filename: str, backend: str, *, ocr_allowed: bool):
    result = external_worker({"backend": backend, "filename": filename, "data": base64.b64encode(data).decode(),
                              "ocr_allowed": ocr_allowed}, backend)
    if result.get("original_sha256") != hashlib.sha256(data).hexdigest():
        raise ValueError("external_parser_source_hash_mismatch")
    if not result.get("records"):
        raise ValueError("external_parser_empty_result")
    return result


def routed_document(data: bytes, filename: str, policy: dict, native: dict):
    from .page_parsing import assemble_pdf_pages

    if native["status"] == "FAILED" or not policy.get("native_parse", True):
        return native
    try:
        if Path(filename).suffix.lower() == ".pdf":
            return assemble_pdf_pages(data, filename, policy, native)
        backend, route = selected_backend(data, filename, policy)
        native["quality_report"]["parser_route"] = {"version": ROUTE_VERSION, "selected": backend, **route}
        return native
    except Exception as exc:
        failed = copy.deepcopy(native)
        failed["status"] = "FAILED"
        failed["elements"], failed["evidence_spans"], failed["anchors"] = [], [], []
        failed["quality_report"]["warnings"].append({"code": "selected_external_parser_failed",
                                                   "diagnostic": type(exc).__name__ + ": " + str(exc)})
        failed["quality_report"]["missing_capabilities"].append("selected_external_parser_failed")
        failed["quality_report"]["parser_route"] = {"version": ROUTE_VERSION, "failed": True,
                                                    "configured": os.environ.get("EVIDENCE_KB_PARSE_ROUTE", "auto")}
        return failed
