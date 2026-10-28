"""Line-safe context windows over immutable prose; preserve whole tabular blocks."""
from __future__ import annotations

import math
import re
from collections import Counter

from .evidence_packing import terms

PROTOCOL = 'context-prose-line-safe650/1'


def blocks(text, offset=0):
    spans = []
    for match in re.finditer(r'\S[\s\S]*?(?=\n\s*\n|\Z)', text[offset:]):
        start, end = offset + match.start(), offset + match.end()
        while end > start and text[end - 1].isspace():
            end -= 1
        if end <= start:
            continue
        paragraph = text[start:end]
        if len(paragraph) <= 650 or '|' in paragraph or '\t' in paragraph:
            spans.append((start, end))
            continue
        cursor = start
        for newline in re.finditer(r'\n', paragraph):
            boundary = start + newline.end()
            if boundary - cursor >= 650:
                spans.append((cursor, boundary))
                cursor = boundary
        if cursor < end:
            spans.append((cursor, end))
    return spans


def ranked_blocks(text, question, offset=0):
    spans = blocks(text, offset)
    query = terms(question)
    bags = [terms(text[start:end]) for start, end in spans]
    frequencies = Counter(term for bag in bags for term in bag)
    scored = [(sum(math.log1p(len(spans) / frequencies[term]) for term in query & bag), start, end)
              for (start, end), bag in zip(spans, bags)]
    return sorted((row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]))


def select_window(text, question, offset, fits, max_characters=2000):
    ranked = ranked_blocks(text, question, offset)
    if not ranked:
        return None
    spans = blocks(text, offset)
    for _, start, end in ranked:
        if end - start > max_characters or not fits(start, end):
            continue
        left = right = spans.index((start, end))
        while True:
            choices = []
            if left:
                choices.append((spans[left - 1][0], end, left - 1, right))
            if right + 1 < len(spans):
                choices.append((start, spans[right + 1][1], left, right + 1))
            choices.sort(key=lambda row: (row[1] - row[0], row[0]))
            accepted = next((row for row in choices
                if row[1] - row[0] <= max_characters and fits(row[0], row[1])), None)
            if accepted is None:
                return start, end
            start, end, left, right = accepted
    return (offset, offset)
