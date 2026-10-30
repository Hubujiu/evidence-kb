from __future__ import annotations

import hashlib
from pathlib import Path

if __package__:
    from . import parse_external_document as base
else:
    import parse_external_document as base

_PARSERS = {}
_identity = base.backend_identity


def parse_backend(backend, path, *, ocr_allowed=True):
    if backend != "rapidocr" or not ocr_allowed:
        raise ValueError("direct_ocr_blocked_by_document_policy")
    import pypdfium2
    from rapidocr import RapidOCR

    if backend not in _PARSERS:
        _PARSERS[backend] = RapidOCR()
    pdf = pypdfium2.PdfDocument(path)
    records, timings = [], []
    for index in range(len(pdf)):
        page = pdf[index]
        image = page.render(scale=3).to_pil()
        width, height = page.get_size()
        recognized = _PARSERS[backend](image)
        timings.append(recognized.elapse)
        for box, text, score in zip(recognized.boxes if recognized.boxes is not None else [],
                                   recognized.txts or (), recognized.scores or ()):
            x, y = box[:, 0], box[:, 1]
            rectangle = [float(min(x)) * width / image.width, float(min(y)) * height / image.height,
                         float(max(x)) * width / image.width, float(max(y)) * height / image.height]
            records.append({"text": text, "kind": "Paragraph", "page_index": index,
                "provenance": {"bbox_pdf": rectangle, "ocr_polygon_pixels": box.tolist(),
                    "rendered_dimensions": [image.width, image.height], "ocr_score": float(score)}})
        page.close()
    pdf.close()
    if not records:
        raise ValueError("direct official OCR returned no observable text")
    return {"backend": backend, "records": records, "remote_calls": 0,
        "details": {"pipeline": "official-rapidocr-direct-lines/1", "render_scale": 3,
                    "layout_filter_applied": False, "inference_seconds_by_page": timings,
                    "conversion_status": "SUCCESS"}}


def backend_identity(backend):
    result = _identity("docling")
    result["base_worker_sha256"] = result["worker_sha256"]
    result["worker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["pipeline"] = "official-rapidocr-direct-lines/1:pdfium-scale-3"
    return result


if __name__ == "__main__":
    base.parse_backend = parse_backend
    base.backend_identity = backend_identity
    base.main()
