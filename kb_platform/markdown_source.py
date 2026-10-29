"""Markdown structure mapped back to immutable source-codepoint slices."""
from dataclasses import dataclass
import re

from markdown_it import MarkdownIt


PARSER_VERSION = "native-text/0.3.0-markdown-it"


@dataclass(frozen=True)
class SourceBlock:
    start: int
    end: int
    first_line: int
    last_line: int
    kind: str | None = None
    heading_level: int = 0
    unclosed: bool = False


def source_blocks(text: str, *, markdown: bool = True):
    # Only CR/LF delimit Markdown lines; str.splitlines also splits source characters
    # such as U+2028, which would make token maps point at the wrong evidence.
    starts = [0, *(m.end() for m in re.finditer(r"\r\n|\r|\n", text))]
    if starts[-1] != len(text):
        starts.append(len(text))
    lines = [text[a:b] for a, b in zip(starts, starts[1:])]
    tokens = MarkdownIt("commonmark").enable("table").disable("inline").parse(text)
    structures = {}
    protected = set()
    tables = {}
    for token in tokens:
        if token.map is None:
            continue
        first, end = token.map
        if token.type in ("fence", "code_block", "heading_open", "html_block"):
            protected.update(range(first, end))
            if markdown and token.type != "html_block":
                # Fence content excludes its opening/closing lines. Counting only
                # LF also works for nested containers and an unterminated last line.
                content_lines = token.content.count("\n") + bool(token.content and not token.content.endswith("\n"))
                unclosed = token.type == "fence" and end - first == content_lines + 1
                level = int(token.tag[1:]) if token.type == "heading_open" else 0
                structures[first] = (end, "Section" if level else "Code", level, unclosed)
        elif markdown and token.type == "table_open":
            tables[first] = end

    # Evidence policy, not Markdown syntax: also retain separatorless legacy pipe
    # tables and their immediately preceding prose caption as one original unit.
    first = 0
    while first < len(lines):
        if first in protected or not lines[first].lstrip().startswith("|"):
            first += 1
            continue
        end = first + 1
        while end < len(lines) and end not in protected and lines[end].lstrip().startswith("|"):
            end += 1
        if end - first >= 2:
            tables[first] = max(end, tables.get(first, end))
        first = end
    groups = {}
    table_lines = {line for first, end in tables.items() for line in range(first, end)}
    for first, end in sorted(tables.items()):
        caption = first - 1
        if (caption >= 0 and caption not in protected and caption not in table_lines
                and lines[caption].strip() and not lines[caption].lstrip().startswith("#")):
            first = caption
        groups[first] = end

    first = 0
    while first < len(lines):
        end, kind, level, unclosed = structures.get(first, (groups.get(first, first + 1), None, 0, False))
        start_offset, end_offset = starts[first], starts[end]
        if not unclosed:
            end_offset = start_offset + len(text[start_offset:end_offset].rstrip("\r\n"))
        if text[start_offset:end_offset].strip():
            yield SourceBlock(start_offset, end_offset, first + 1, end, kind, level, unclosed)
        first = end
