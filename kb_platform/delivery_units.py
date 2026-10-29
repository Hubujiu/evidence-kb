from __future__ import annotations

from .retrieval_expansion import retrieval_chunks

PROTOCOL = "pdf-page-line-safe650-table-context/1"
PAGE_PROTOCOL = "pdf-original-page-navigation/1"


def indexed_elements(document, *, include_pages=False):

    elements = document["elements"]
    if document.get("media_type") != "application/pdf":
        return elements
    evidence = {e["evidence_id"]: e for e in document["evidence_spans"]}
    anchors = {a["anchor_id"]: a for a in document["anchors"]}
    groups, cursor = {}, 0
    for element in elements:
        pages = {anchors[a]["page_index"] for eid in element["evidence_ids"] for a in evidence[eid]["anchor_ids"]
                 if "page_index" in anchors[a]}
        if len(pages) != 1:
            raise ValueError("PDF retrieval element needs one immutable original page")
        groups.setdefault(next(iter(pages)), []).append((element, cursor, cursor + len(element["raw_text"])))
        cursor += len(element["raw_text"]) + 1
    output = []
    for page, rows in groups.items():
        text = "\n".join(row[0]["raw_text"] for row in rows)
        base = rows[0][1]
        if any(left[2] + 1 != right[1] for left, right in zip(rows, rows[1:])):
            raise ValueError("PDF page elements must occupy one continuous original span")
        if include_pages:
            page_id = f"page_{page}_{base}_{base + len(text)}"
            output.append({"element_id": page_id, "kind": "Page", "ordinal": rows[0][0]["ordinal"],
                "raw_text": text, "parent_id": None,
                "heading_path": list(dict.fromkeys(h for e, _, _ in rows for h in e.get("heading_path", []))),
                "evidence_ids": list(dict.fromkeys(eid for e, _, _ in rows for eid in e["evidence_ids"])),
                "retrieval_protocol": PAGE_PROTOCOL, "parse_text_start": base,
                "parse_text_end": base + len(text),
                "constituent_element_ids": [e["element_id"] for e, _, _ in rows]})
        for chunk in retrieval_chunks(document["source_version_id"], text, 650):
            start, end = base + chunk.start, base + chunk.end
            members = [e for e, left, right in rows if left < end and right > start]
            element_id = f"retrieval_{page}_{start}_{end}"
            output.append({"element_id": element_id, "kind": "Paragraph", "ordinal": rows[0][0]["ordinal"],
                "raw_text": chunk.text, "parent_id": None,
                "heading_path": list(dict.fromkeys(h for e in members for h in e.get("heading_path", []))),
                "evidence_ids": list(dict.fromkeys(eid for e in members for eid in e["evidence_ids"])),
                "retrieval_protocol": PROTOCOL, "parse_text_start": start, "parse_text_end": end,
                "constituent_element_ids": [e["element_id"] for e in members]})


        output.extend(e for e, _, _ in rows if e["kind"] == "Table")
    return output
