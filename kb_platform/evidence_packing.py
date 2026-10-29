from __future__ import annotations

import math
import re
from collections import Counter


def terms(text):
    words = set(re.findall(r"[a-z0-9_]+(?:[-.][a-z0-9_]+)*", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(len(run) - 1))
    return words


def blocks(text, offset=0):

    spans = []
    for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text[offset:]):
        start, end = offset + match.start(), offset + match.end()
        while end > start and text[end - 1].isspace():
            end -= 1
        if end > start:
            spans.append((start, end))
    return spans


def ranked_blocks(text, question, offset=0):
    spans = blocks(text, offset)
    query = terms(question)
    tokenized = [terms(text[start:end]) for start, end in spans]
    frequency = Counter(term for bag in tokenized for term in bag)
    scored = [(sum(math.log1p(len(spans) / frequency[t]) for t in query & bag), start, end)
              for (start, end), bag in zip(spans, tokenized)]

    return sorted((row for row in scored if row[0] > 0), key=lambda row: (-row[0], row[1]))


def rank_texts(texts, question):

    query = terms(question)
    bags = [terms(text) for text in texts]
    frequency = Counter(term for bag in bags for term in bag)
    scores = [sum(math.log1p(len(bags) / frequency[term]) for term in query & bag) for bag in bags]
    return sorted(range(len(texts)), key=lambda index: (-scores[index], index))


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
