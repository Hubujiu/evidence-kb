"""Canonical paragraph chunking with exact original-text offsets."""
from __future__ import annotations

from dataclasses import dataclass
import re


from .evidence_packing import blocks


@dataclass(frozen=True)
class Extract:
    source_id: str
    start: int
    end: int
    text: str


def original_blocks(source_id, text):
    return [Extract(source_id, start, end, text[start:end]) for start, end in blocks(text)]


def retrieval_chunks(source_id, text, chunk_characters=650):
    """Paragraph chunks with line-safe cuts for PDF text lacking blank lines.

    Markdown tables remain indivisible. A single overlong line is retained
    whole. No question or annotation controls the boundaries.
    """
    result = []
    for paragraph in original_blocks(source_id, text):
        if len(paragraph.text) <= chunk_characters or '|' in paragraph.text:
            result.append(paragraph)
            continue
        start = paragraph.start
        for match in re.finditer(r'\n', paragraph.text):
            end = paragraph.start+match.end()
            if end-start >= chunk_characters:
                result.append(Extract(source_id, start, end, text[start:end]))
                start = end
        if start < paragraph.end:
            result.append(Extract(source_id, start, paragraph.end, text[start:paragraph.end]))
    return result

