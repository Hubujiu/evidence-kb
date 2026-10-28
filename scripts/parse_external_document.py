"""One JSON worker protocol for real, separately installed document parsers."""
from __future__ import annotations

import base64
from contextlib import redirect_stdout
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
import hashlib

_PARSERS = {}

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".local/rag-quality-models"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def parse_backend(backend, path, *, ocr_allowed=True):
    records, details = [], {}
    if backend == "docling":
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, AcceleratorOptions
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

        options = PdfPipelineOptions(do_ocr=ocr_allowed, do_table_structure=True,
                                     accelerator_options=AcceleratorOptions(num_threads=4, device="cpu"))
        parser_key = (backend, ocr_allowed)
        if parser_key not in _PARSERS:
            _PARSERS[parser_key] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
                pipeline_options=options, backend=PyPdfiumDocumentBackend)})
        converted = _PARSERS[parser_key].convert(path)
        document = converted.document
        for item, _ in document.iterate_items():
            grid = getattr(getattr(item, "data", None), "grid", None)
            text = "\n".join(" | ".join(str(cell.text).strip() for cell in row) for row in grid) if grid is not None else getattr(item, "text", "")
            if text:
                records.append({"text": text, "kind": "Table" if grid is not None else "Paragraph",
                                "grid": [[str(cell.text).strip() for cell in row] for row in grid] if grid is not None else None,
                                "provenance": [p.model_dump(mode="json") for p in getattr(item, "prov", [])]})
        details = {"conversion_status": str(converted.status), "ocr": ocr_allowed,
                   "packages": {p: importlib.metadata.version(p) for p in ("docling", "docling-core", "torch")}}
    else:
        raise ValueError("unknown external parser backend")
    if not records:
        raise ValueError("external parser returned no observable text")
    return {"backend": backend, "records": records, "details": details, "remote_calls": 0}


def backend_identity(backend):
    if backend == "docling":
        packages = ("docling", "docling-core", "docling-ibm-models", "rapidocr", "onnxruntime", "torch", "pypdfium2")
        roots = list((Path(os.environ["HF_HOME"]) / "hub").glob("models--docling*"))
        roots += [Path(sys.prefix) / "Lib/site-packages/rapidocr/models", Path.home() / ".cache/docling/models"]
    else:
        raise ValueError("unknown external parser backend")
    versions = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    weights = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in {".onnx", ".bin", ".safetensors", ".pdiparams", ".gguf"}:
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    while block := stream.read(8 * 1024 * 1024):
                        hasher.update(block)
                weights[str(path)] = hasher.hexdigest()
    return {"packages": versions, "model_sha256": weights, "worker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fixed_model_dependencies": len(weights) >= (2 if backend == "docling" else 1)}


def main():
    payload = json.load(sys.stdin)
    if payload.get("mode") == "identity":
        with redirect_stdout(sys.stderr):
            output = backend_identity(payload["backend"])
        print(json.dumps(output, ensure_ascii=True))
        return
    if "jobs" in payload:
        results = []
        with tempfile.TemporaryDirectory(prefix="evidence-parser-batch-") as directory:
            for index, job in enumerate(payload["jobs"]):
                data = base64.b64decode(job["data"])
                path = Path(directory) / f"page-{index}.pdf"
                path.write_bytes(data)
                try:
                    with redirect_stdout(sys.stderr):
                        result = parse_backend(payload["backend"], path, ocr_allowed=job.get("ocr_allowed", False))
                    result["original_sha256"] = hashlib.sha256(data).hexdigest()
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    result = {"error": type(exc).__name__ + ": " + str(exc), "backend": payload["backend"]}
                results.append(result)
        print(json.dumps({"results": results, "identity": backend_identity(payload["backend"])}, ensure_ascii=True))
        return
    data = base64.b64decode(payload["data"])
    try:
        # ASCII temporary input avoids native-library failures on Chinese paths.
        with tempfile.TemporaryDirectory(prefix="evidence-parser-") as directory:
            path = Path(directory) / ("source" + Path(payload["filename"]).suffix.lower())
            path.write_bytes(data)
            with redirect_stdout(sys.stderr):
                output = parse_backend(payload["backend"], path, ocr_allowed=payload.get("ocr_allowed", True))
        output["original_sha256"] = __import__("hashlib").sha256(data).hexdigest()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        output = {"error": type(exc).__name__ + ": " + str(exc), "backend": payload["backend"]}
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
