"""Official Docling full-page OCR worker, isolated from unchanged layout jobs.

Keeping this entry separate preserves exact existing layout inference caches.
No reference text, questions, or evidence annotations enter this worker.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

if __package__:
    from . import parse_external_document as base
else:
    import parse_external_document as base

_PARSERS = {}
_identity = base.backend_identity


def parse_backend(backend, path, *, ocr_allowed=True):
    if backend != "docling_ocr" or not ocr_allowed:
        raise ValueError("full_page_ocr_blocked_by_document_policy")
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions, RapidOcrOptions, OcrMode
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

    if backend not in _PARSERS:
        options = PdfPipelineOptions(do_ocr=True, do_table_structure=True,
            ocr_options=RapidOcrOptions(mode=OcrMode.FULL_PAGE),
            accelerator_options=AcceleratorOptions(num_threads=4, device="cpu"))
        _PARSERS[backend] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
            pipeline_options=options, backend=PyPdfiumDocumentBackend)})
    converted = _PARSERS[backend].convert(path)
    records = []
    for item, _ in converted.document.iterate_items():
        grid = getattr(getattr(item, "data", None), "grid", None)
        text = ("\n".join(" | ".join(str(cell.text).strip() for cell in row) for row in grid)
                if grid is not None else getattr(item, "text", ""))
        if text:
            records.append({"text": text, "kind": "Table" if grid is not None else "Paragraph",
                "grid": [[str(cell.text).strip() for cell in row] for row in grid] if grid is not None else None,
                "provenance": [p.model_dump(mode="json") for p in getattr(item, "prov", [])]})
    if not records:
        raise ValueError("external parser returned no observable text")
    return {"backend": backend, "records": records, "remote_calls": 0,
        "details": {"conversion_status": str(converted.status), "ocr": True, "ocr_mode": "full_page",
            "packages": {p: importlib.metadata.version(p) for p in ("docling", "docling-core", "torch")}}}


def backend_identity(backend):
    result = _identity("docling")
    result["base_worker_sha256"] = result["worker_sha256"]
    result["worker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["pipeline"] = "official-docling-rapidocr-full-page/1"
    return result


if __name__ == "__main__":
    base.parse_backend = parse_backend
    base.backend_identity = backend_identity
    base.main()
