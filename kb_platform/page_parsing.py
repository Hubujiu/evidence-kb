"""Page-wise PDF routing, exact dependency cache, and original-PDF assembly."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import os
from pathlib import Path
import re

from .pdf_layout import pdf_layout_features
from .rag_testing.cache import StageCache, StageKey
from .rag_testing.contracts import digest

VERSION = "page-layout-local/3"


def page_routes(data, policy, native):
    from .parsing_backends import RUNTIMES

    route = os.environ.get("EVIDENCE_KB_PARSE_ROUTE", "auto").strip().lower()
    if route not in {"auto", "native", *RUNTIMES}:
        raise ValueError("invalid configured document parser route: " + route)
    backend = os.environ.get("EVIDENCE_KB_LAYOUT_BACKEND", "docling").strip().lower()
    if backend not in RUNTIMES:
        raise ValueError("invalid configured layout backend: " + backend)
    features = pdf_layout_features(data)
    spans = {s["evidence_id"]: s for s in native["evidence_spans"]}
    anchors = {a["anchor_id"]: a for a in native["anchors"]}
    table_pages = {anchors[a]["page_index"] for element in native["elements"] if element["kind"] == "Table"
                   for eid in element["evidence_ids"] for a in spans[eid]["anchor_ids"]
                   if "page_index" in anchors[a]}
    warnings = native["quality_report"]["warnings"]
    ocr_codes = {"ocr_not_executed", "vector_only_page_requires_review", "native_text_quality_requires_review"}
    risks = {}
    for warning in warnings:
        if warning["code"] in ocr_codes and "page_index" in warning:
            risks.setdefault(warning["page_index"], set()).add(warning["code"])
    rows = []
    for index, feature in enumerate(features["pages"]):
        chosen = "native"
        reasons = sorted(risks.get(index, set()))
        needs_ocr = bool(reasons) or feature["ocr_needed"]
        blocked = needs_ocr and not policy.get("ocr", False)
        explained = index in table_pages and feature["column_risk"]
        hybrid = bool(feature["words"] and needs_ocr and (not feature["column_risk"] or explained))
        if route != "native" and not blocked:
            if route != "auto":
                chosen = route
            elif needs_ocr or feature["column_risk"] and not explained:
                chosen = "docling_ocr" if needs_ocr and backend == "docling" else backend
        rows.append({"page_index": index, "page_label": str(index + 1), "backend": chosen,
                     "native_grid_explains_column_gaps": explained, "ocr_blocked_by_policy": blocked,
                     "features": feature, "ocr_allowed": bool(policy.get("ocr", False)),
                     "ocr_reasons": reasons, "assembly": "native_with_ocr_supplement" if hybrid else "replace_page",
                     "backends": ["native", chosen] if hybrid and chosen != "native" else [chosen]})
    return rows


def page_binary(pdf, index, original):
    """Deterministic real PDF page bytes; only generated metadata is normalized.

    Changing equal-length metadata IDs leaves PDF xref byte offsets unchanged.
    Original source bytes are never modified. The actual derivative hash is
    recorded independently and verified by the model worker.
    """
    import pypdfium2

    if len(pdf) == 1:
        return original
    one = pypdfium2.PdfDocument.new()
    one.import_pages(pdf, [index])
    stream = io.BytesIO()
    one.save(stream)
    one.close()
    data = stream.getvalue()
    data = re.sub(rb"/ID\s*\[[^\]]+\]", lambda m: re.sub(rb"<[0-9A-Fa-f]+>",
                  lambda h: b"<" + b"0" * (len(h[0]) - 2) + b">", m[0]), data)
    return re.sub(rb"/(?:CreationDate|ModDate)\s*\([^)]*\)", lambda m: re.sub(
        rb"D:\d{14}", b"D:20000101000000", m[0]), data)


def _cache_key(page_sha, backend, policy, identity):
    authorization = policy.get("_parse_authorization", {})
    # The cached artifact is external parser output. Routing/assembly changes
    # must not invalidate unchanged model inference; actual derivative bytes,
    # worker code, packages, weights and processing settings are its dependencies.
    implementation = digest({"contract": "external-parser-records/1",
                             "worker": identity.get("worker_sha256"), "packages": identity.get("packages")})
    return StageKey(stage="document-page-parsing", implementation=implementation,
        upstream=(page_sha,), config_hash=digest({"backend": backend, "ocr": bool(policy.get("ocr", False)),
        "include_hidden": bool(policy.get("include_hidden", False)), "protocol": "page-layout-local/2"}),
        corpus_hash=page_sha, index_snapshot="pre-index-page-bytes", principal=authorization["principal"],
        acl_revision=authorization["acl_revision"], model_revision=digest(identity),
        sample_id=policy.get("_parse_sample_id", "deterministic-page-parser/1"))


def _append_external(bundle, parsed, page_index, backend, dimensions):
    width, height = dimensions
    for index, record in enumerate(parsed["records"]):
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        location = {"selector_type": "object_path", "object_path": f"page/{page_index}/{backend}/block/{index}",
                    "page_index": page_index, "page_label": str(page_index + 1),
                    "coordinate_quality": "model_estimated", "granularity": "block"}
        provenance = record.get("provenance")
        if backend in {"docling", "docling_ocr"} and provenance:
            box = provenance[0]["bbox"]
            rectangle = ([box["l"], height - box["t"], box["r"], height - box["b"]]
                         if "BOTTOMLEFT" in str(box.get("coord_origin", "")).upper()
                         else [box["l"], box["t"], box["r"], box["b"]])
            location.update(selector_type="page_region", bbox=rectangle, rects=[rectangle],
                            dimensions=[width, height], coordinate_system="pdfplumber_top_left_points")
        elif backend == "rapidocr" and provenance and provenance.get("bbox_pdf"):
            rectangle = provenance["bbox_pdf"]
            location.update(selector_type="page_region", bbox=rectangle, rects=[rectangle],
                            dimensions=[width, height], coordinate_system="pdfplumber_top_left_points")
        grid = record.get("grid")
        if record.get("kind") == "Table" and isinstance(grid, list) and grid:
            cells = [[{**location, "object_path": location["object_path"] + f"/row/{r}/cell/{c}",
                       "coordinate_quality": "block_only", "granularity": "cell"}
                      for c, _ in enumerate(row)] for r, row in enumerate(grid)]
            bundle.table(grid, {**location, "granularity": "table"}, cells)
            bundle.result["elements"][-1]["extraction_method"] = backend
        else:
            bundle.element(text, location, record.get("kind", "Paragraph"), extraction_method=backend)
        bundle.result["evidence_spans"][-1].update(extraction_method=backend, native_text_spans=[])


def _bad_text(text):
    return "\ufffd" in text or "(cid:" in text


def _supplement_records(parsed, native_elements, native_anchors, height):
    """Keep exact clean native facts and model content outside native tables.

    Native and OCR coordinates stay separate; native table cells never inherit
    model-estimated boxes. This is source assembly, not annotation-aware repair.
    """
    native_text = re.sub(r"\s+", "", "\n".join(e["raw_text"] for e in native_elements))
    table_boxes = [a["bbox"] for a in native_anchors if a.get("granularity") == "table" and a.get("bbox")]
    records = []
    for record in parsed["records"]:
        normalized = re.sub(r"\s+", "", record.get("text", ""))
        if normalized and normalized in native_text:
            continue
        provenance = record.get("provenance")
        if provenance and isinstance(provenance, list):
            box = provenance[0].get("bbox", {})
            if all(k in box for k in ("l", "r", "t", "b")):
                rectangle = ([box["l"], height - box["t"], box["r"], height - box["b"]]
                    if "BOTTOMLEFT" in str(box.get("coord_origin", "")).upper()
                    else [box["l"], box["t"], box["r"], box["b"]])
                area = max(0, rectangle[2] - rectangle[0]) * max(0, rectangle[3] - rectangle[1])
                if record.get("kind") == "Table" and area and any(max(0, min(rectangle[2], table[2]) - max(rectangle[0], table[0])) *
                    max(0, min(rectangle[3], table[3]) - max(rectangle[1], table[1])) / area >= .8 for table in table_boxes):
                    continue
        records.append(record)
    return {**parsed, "records": records}


def assemble_pdf_pages(data, filename, policy, native):
    import pypdfium2
    from .documents import _Bundle
    from .ocr import _replace_ids
    from .parsing_backends import external_worker

    plan = page_routes(data, policy, native)
    external_pages = [row for row in plan if row["backend"] != "native"]
    report = native["quality_report"]
    report["parser_route"] = {"version": VERSION, "route": os.environ.get("EVIDENCE_KB_PARSE_ROUTE", "auto"), "pages": plan}
    if not external_pages:
        report["parser_route"]["selected"] = "native"
        return native
    authorization = policy.get("_parse_authorization", {})
    cache = (StageCache(Path(os.environ.get("EVIDENCE_KB_PAGE_CACHE_ROOT", ".local/parser-page-cache")))
             if authorization.get("principal") and authorization.get("acl_revision") else None)
    pdf = pypdfium2.PdfDocument(data)
    parsed_pages, dimensions, identities = {}, {}, {}
    for index in range(len(pdf)):
        page = pdf[index]
        dimensions[index] = list(map(float, page.get_size()))
        page.close()
    for backend in dict.fromkeys(row["backend"] for row in external_pages):
        rows = [row for row in external_pages if row["backend"] == backend]
        try:
            identity = external_worker({"mode": "identity", "backend": backend}, backend)
            identities[backend] = identity
            jobs = []
            for row in rows:
                page_data = page_binary(pdf, row["page_index"], data)
                page_sha = hashlib.sha256(page_data).hexdigest()
                row["page_binary_sha256"] = page_sha
                row["page_binary_protocol"] = "pdfium-page-fixed-generated-metadata/2"
                row["model_revision"] = digest(identity)
                row["cache_hit"] = False
                key = _cache_key(page_sha, backend, policy, identity) if cache else None
                cached = cache.get(key, authorize=lambda: hashlib.sha256(page_data).hexdigest() == page_sha and bool(
                    policy.get("_parse_authorization", {}).get("principal")), mode=policy.get("_parse_stage_mode", "live"),
                    under_test=policy.get("_parse_under_test", False)) if cache and identity["fixed_model_dependencies"] else None
                if cached is not None:
                    if cached.get("original_sha256") != page_sha:
                        raise ValueError("cached page source integrity mismatch")
                    parsed_pages[row["page_index"]] = cached
                    row.update(cache_hit=True, status="COMPLETE")
                else:
                    jobs.append((row, page_data, key))
            batch_size = max(1, int(os.environ.get("EVIDENCE_KB_PAGE_PARSE_BATCH_SIZE", "16")))
            for start in range(0, len(jobs), batch_size):
                batch = jobs[start:start + batch_size]
                result = external_worker({"backend": backend, "jobs": [
                    {"data": base64.b64encode(page_data).decode(), "ocr_allowed": bool(policy.get("ocr", False))}
                    for _, page_data, _ in batch]}, backend)
                if len(result.get("results", [])) != len(batch):
                    raise ValueError("page batch identity/count mismatch")
                actual_identity = result["identity"]
                identities[backend] = actual_identity
                for (row, _, _), parsed in zip(batch, result["results"]):
                    if parsed.get("error"):
                        row.update(status="FAILED", diagnostic=parsed["error"])
                        continue
                    if parsed.get("original_sha256") != row["page_binary_sha256"]:
                        raise ValueError("external page source hash mismatch")
                    if cache and actual_identity["fixed_model_dependencies"]:
                        cache.put(_cache_key(row["page_binary_sha256"], backend, policy, actual_identity), parsed)
                    row.update(status="COMPLETE", model_revision=digest(actual_identity))
                    conversion = parsed.get("details", {}).get("conversion_status", "")
                    if conversion and conversion.split(".")[-1] != "SUCCESS":
                        row["partial_conversion"] = conversion
                    parsed_pages[row["page_index"]] = parsed
        except Exception as exc:
            for row in rows:
                if row["page_index"] not in parsed_pages:
                    row.update(status="FAILED", diagnostic=type(exc).__name__ + ": " + str(exc))
    # A picture-classified cover can have real line OCR but no exported Docling
    # text items. Only that explicit empty-output failure authorizes this
    # separately audited direct OCR branch; successful page dependencies stay
    # unchanged. Never accept an empty result as a successful blank page.
    empty_pages = [row for row in external_pages if row["backend"] == "docling_ocr" and
                   row.get("status") == "FAILED" and "external parser returned no observable text" in row.get("diagnostic", "")]
    if empty_pages and policy.get("ocr", False):
        fallback = "rapidocr"
        try:
            identity = external_worker({"mode": "identity", "backend": fallback}, fallback)
            identities[fallback] = identity
            for row in empty_pages:
                row["attempts"] = [{"backend": row["backend"], "status": "FAILED", "diagnostic": row["diagnostic"]}]
                page_data = page_binary(pdf, row["page_index"], data)
                key = _cache_key(row["page_binary_sha256"], fallback, policy, identity) if cache else None
                parsed = cache.get(key, authorize=lambda: hashlib.sha256(page_data).hexdigest() == row["page_binary_sha256"] and bool(
                    policy.get("_parse_authorization", {}).get("principal")), mode=policy.get("_parse_stage_mode", "live"),
                    under_test=policy.get("_parse_under_test", False)) if cache and identity["fixed_model_dependencies"] else None
                cache_hit = parsed is not None
                if parsed is None:
                    response = external_worker({"backend": fallback, "jobs": [{"data": base64.b64encode(page_data).decode(),
                        "ocr_allowed": True}]}, fallback)
                    if len(response.get("results", [])) != 1:
                        raise ValueError("direct OCR fallback result count mismatch")
                    parsed, identity = response["results"][0], response["identity"]
                    identities[fallback] = identity
                    if parsed.get("error"):
                        raise ValueError(parsed["error"])
                if parsed.get("original_sha256") != row["page_binary_sha256"] or not parsed.get("records"):
                    raise ValueError("direct OCR fallback source mismatch or empty output")
                if not cache_hit and cache and identity["fixed_model_dependencies"]:
                    cache.put(_cache_key(row["page_binary_sha256"], fallback, policy, identity), parsed)
                parsed_pages[row["page_index"]] = parsed
                row.update(primary_backend="docling_ocr", backend=fallback, status="COMPLETE", cache_hit=cache_hit,
                           model_revision=digest(identity), fallback_reason="docling_empty_layout_export")
                row["attempts"].append({"backend": fallback, "status": "COMPLETE"})
                row["backends"] = ["native", fallback] if row["assembly"] == "native_with_ocr_supplement" else [fallback]
                row.pop("diagnostic", None)
        except Exception as exc:
            for row in empty_pages:
                if row["page_index"] not in parsed_pages:
                    row.update(status="FAILED", diagnostic="direct_ocr_fallback_failed:" + type(exc).__name__ + ": " + str(exc))
    pdf.close()
    replaced = {row["page_index"] for row in external_pages}
    hybrid = {row["page_index"] for row in external_pages if row["assembly"] == "native_with_ocr_supplement"}
    result = copy.deepcopy(native)
    anchor_by_id = {row["anchor_id"]: row for row in result["anchors"]}
    span_by_id = {row["evidence_id"]: row for row in result["evidence_spans"]}
    removed_spans = {eid for element in result["elements"] for eid in element["evidence_ids"]
        if any(anchor_by_id[a].get("page_index") in replaced and
            (anchor_by_id[a].get("page_index") not in hybrid or _bad_text(element["raw_text"]))
            for a in span_by_id[eid]["anchor_ids"])}
    result["elements"] = [e for e in result["elements"] if not set(e["evidence_ids"]) & removed_spans]
    result["evidence_spans"] = [s for s in result["evidence_spans"] if s["evidence_id"] not in removed_spans]
    retained_anchor_ids = {a for s in result["evidence_spans"] for a in s["anchor_ids"]}
    result["anchors"] = [a for a in result["anchors"] if a["anchor_id"] in retained_anchor_ids]
    bundle = _Bundle(data, filename, policy)
    for row in external_pages:
        parsed = parsed_pages.get(row["page_index"])
        if parsed is not None:
            if row["page_index"] in hybrid:
                page_anchors = [a for a in result["anchors"] if a.get("page_index") == row["page_index"]]
                ids = {a["anchor_id"] for a in page_anchors}
                eids = {s["evidence_id"] for s in result["evidence_spans"] if ids.intersection(s["anchor_ids"])}
                page_elements = [e for e in result["elements"] if eids.intersection(e["evidence_ids"])]
                parsed = _supplement_records(parsed, page_elements, page_anchors, dimensions[row["page_index"]][1])
                row["native_elements_retained"] = len(page_elements)
                row["ocr_supplement_records"] = len(parsed["records"])
            _append_external(bundle, parsed, row["page_index"], row["backend"], dimensions[row["page_index"]])
    for collection in ("elements", "evidence_spans", "anchors"):
        result[collection].extend(bundle.result[collection])
    completed = {index for index, parsed in parsed_pages.items() if not any(_bad_text(r.get("text", "")) for r in parsed["records"])
                 and not next(row for row in external_pages if row["page_index"] == index).get("partial_conversion")}
    missing_codes = {"ocr_not_executed", "vector_only_page_requires_review", "native_text_quality_requires_review"}
    quality = result["quality_report"]
    quality["warnings"] = [w for w in quality["warnings"] if not (w.get("page_index") in completed and w["code"] in missing_codes)]
    remaining_warning_codes = {w["code"] for w in quality["warnings"]}
    quality["missing_capabilities"] = [c for c in quality["missing_capabilities"]
                                       if c not in missing_codes or c in remaining_warning_codes]
    failures = [row for row in external_pages if row.get("status") != "COMPLETE"]
    if failures:
        quality["missing_capabilities"].append("selected_external_page_parser_failed")
        quality["warnings"].extend({"code": "selected_external_parser_failed", "page_index": row["page_index"],
                                    "diagnostic": row.get("diagnostic", "no result")} for row in failures)
    if any(row.get("partial_conversion") for row in external_pages):
        quality["missing_capabilities"].append("external_page_partial_conversion")
    quality["parser_route"] = {"version": VERSION, "selected": "page_ensemble", "pages": plan,
                               "cache_hits": sum(row.get("cache_hit", False) for row in external_pages),
                               "external_page_executions": sum(row.get("status") == "COMPLETE" and not row.get("cache_hit", False) for row in external_pages),
                               "external_page_failures": len(failures),
                               "recovered_primary_failures": sum(bool(row.get("fallback_reason")) for row in external_pages),
                               "source_sha256": hashlib.sha256(data).hexdigest(), "backend_identities": identities}
    quality.update(local_model_inference=bool(parsed_pages), remote_model_calls=0,
                   source_coordinates_verified_by_human=False)
    result["status"] = ("PARTIAL" if result["elements"] else "FAILED") if failures else (
        "PARTIAL" if quality["missing_capabilities"] else "READY")
    if not failures:
        for index in completed:
            quality["ocr_decisions"].append({"page_index": index, "decision": "LOCAL_PAGE_PARSE_COMPLETED"})
    snapshot_plan = [{k: v for k, v in row.items() if k not in {"cache_hit", "diagnostic", "status"}} for row in plan]
    snapshot = digest({"native_snapshot": native["parse_snapshot_id"], "protocol": VERSION,
                       "pages": snapshot_plan, "outputs": {str(i): digest(parsed) for i, parsed in parsed_pages.items()}})
    _replace_ids(result, snapshot)
    spans = {s["evidence_id"]: s for s in result["evidence_spans"]}
    anchors = {a["anchor_id"]: a for a in result["anchors"]}
    result["elements"].sort(key=lambda e: (anchors[spans[e["evidence_ids"][0]]["anchor_ids"][0]].get("page_index", 0),
        0 if e.get("extraction_method") == "native" else 1, e["ordinal"]))
    for index, element in enumerate(result["elements"]):
        element["ordinal"] = index
    result["parser_version"] += "+" + VERSION
    return result
