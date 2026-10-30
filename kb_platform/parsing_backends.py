from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ROUTE_VERSION = "page-layout-local/3"
RUNTIMES = {"docling": ".local/rag-quality-cpu/Scripts/python.exe",
            "docling_ocr": ".local/rag-quality-cpu/Scripts/python.exe",
            "rapidocr": ".local/rag-quality-cpu/Scripts/python.exe"}


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


def routed_document(data: bytes, filename: str, policy: dict, native: dict):
    from .page_parsing import assemble_pdf_pages

    if native["status"] == "FAILED" or not policy.get("native_parse", True):
        return native
    try:
        if Path(filename).suffix.lower() == ".pdf":
            return assemble_pdf_pages(data, filename, policy, native)
        route = os.environ.get("EVIDENCE_KB_PARSE_ROUTE", "auto").strip().lower()
        if route not in {"auto", "native", *RUNTIMES}:
            raise ValueError("invalid configured document parser route: " + route)
        native["quality_report"]["parser_route"] = {"version": ROUTE_VERSION, "selected": "native", "route": route}
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
