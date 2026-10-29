"""Synthetic source fixtures: parser/evidence contracts, not production RAG quality."""
import hashlib
import unittest
from unittest.mock import patch

from kb_platform.documents import parse_native_document


class MarkdownContracts(unittest.TestCase):
    def parse(self, text, filename="source.md"):
        result = parse_native_document(text.encode("utf-8"), filename)
        self.assertEqual(result["status"], "READY", result["quality_report"])
        self.assertEqual(result["original_sha256"], hashlib.sha256(text.encode()).hexdigest())
        source = text.removeprefix("\ufeff")
        self.assertEqual(len(result["elements"]), len(result["anchors"]))
        self.assertEqual(len(result["elements"]), len(result["evidence_spans"]))
        previous_end = 0
        for element, anchor, span in zip(result["elements"], result["anchors"], result["evidence_spans"]):
            self.assertEqual(source[anchor["text_start"]:anchor["text_end"]], element["raw_text"])
            self.assertEqual(anchor["quote"], element["raw_text"])
            self.assertGreaterEqual(anchor["text_start"], previous_end)
            self.assertFalse(source[previous_end:anchor["text_start"]].strip())
            previous_end = anchor["text_end"]
            self.assertEqual(anchor["source_version_id"], result["original_sha256"])
            self.assertEqual(span["parse_snapshot_id"], result["parse_snapshot_id"])
            self.assertEqual(span["anchor_ids"], [anchor["anchor_id"]])
            self.assertEqual(span["element_ids"], [element["element_id"]])
            self.assertEqual(element["evidence_ids"], [span["evidence_id"]])
            self.assertEqual(element["normalization_map"][-1], len(element["raw_text"]))
        self.assertFalse(source[previous_end:].strip())
        return result

    def texts(self, result):
        return [e["raw_text"] for e in result["elements"]]

    def test_original_unicode_offsets_and_mixed_newlines(self):
        result = self.parse("\ufeff# 中文😀\r\n第一行\r第二行\n末行")
        self.assertEqual(self.texts(result), ["# 中文😀", "第一行", "第二行", "末行"])
        self.assertEqual([(a["text_start"], a["text_end"]) for a in result["anchors"]],
                         [(0, 5), (7, 10), (11, 14), (15, 17)])

    def test_unicode_separator_is_not_a_markdown_line(self):
        result = self.parse("甲\u2028乙\v丙\n# 标题\n正文")
        self.assertEqual(self.texts(result), ["甲\u2028乙\v丙", "# 标题", "正文"])
        self.assertEqual(result["anchors"][1]["object_path"], "line/2")

    def test_headings_keep_markers_and_parent(self):
        result = self.parse("  # 一级 ###\n正文\n\n二级\n---\n下文")
        self.assertEqual(self.texts(result), ["  # 一级 ###", "正文", "二级\n---", "下文"])
        self.assertEqual([e.get("heading_level") for e in result["elements"]], [1, None, 2, None])
        self.assertEqual(result["elements"][-1]["heading_path"], ["  # 一级 ###", "二级\n---"])
        self.assertEqual(result["elements"][-1]["parent_id"], result["elements"][2]["element_id"])

    def test_fences_use_matching_marker_and_length(self):
        code = "````python\n```\n~~~\n# literal\n`````"
        result = self.parse(code + "\n正文")
        self.assertEqual(self.texts(result), [code, "正文"])
        self.assertEqual(result["elements"][0]["kind"], "Code")
        self.assertEqual(result["quality_report"]["warnings"], [])

    def test_fence_closure_and_unclosed_warning(self):
        for code, closed in [("```", False), ("```\n", False), ("```\nx", False),
                             ("```\nx\n", False), ("```\n\n", False),
                             ("```\n```", True), ("~~~\nx\n~~~\n", True),
                             ("```\nx\n``` trailing", False)]:
            with self.subTest(code=code):
                result = self.parse(code)
                self.assertEqual(self.texts(result), [code.rstrip("\n") if closed else code])
                self.assertEqual(bool(result["quality_report"]["warnings"]), not closed)

    def test_unclosed_nested_fence_stops_at_container(self):
        result = self.parse("> ```\n> private code\n\nOutside\n# Title")
        self.assertEqual(result["elements"][0]["kind"], "Code")
        self.assertIn("Outside", self.texts(result))
        self.assertEqual(result["elements"][-1]["kind"], "Section")
        self.assertEqual(result["quality_report"]["warnings"], [{"code": "unclosed_code_fence"}])

    def test_nested_code_keeps_container_prefixes(self):
        for code in ["> ```\n> x\n> ```", "- ```\n  x\n  ```", "    x\n    y", "\tx\n\ty"]:
            with self.subTest(code=code):
                result = self.parse(code)
                self.assertEqual(self.texts(result), [code])
                self.assertEqual(result["elements"][0]["kind"], "Code")
                self.assertEqual(result["quality_report"]["warnings"], [])

    def test_code_does_not_create_headings_or_tables(self):
        code = "```\n# Heading\n| a | b |\n| c | d |\n```"
        result = self.parse(code + "\nAfter")
        self.assertEqual(self.texts(result), [code, "After"])
        self.assertEqual(result["elements"][1]["heading_path"], [])

    def test_both_table_dialects_keep_caption_and_escaped_pipes(self):
        for table in ["| 编码 | 价格 |\n| A | 13 |", "编码 | 价格\n--- | ---\nA\\|B | 13",
                      "| 编码 | 价格 |\n| --- | --- |\n| A | 13 |"]:
            with self.subTest(table=table):
                body = "单位：CNY。\n" + table
                self.assertEqual(self.texts(self.parse("前文\n\n" + body + "\n\n后文")), ["前文", body, "后文"])

    def test_table_does_not_absorb_heading_or_code(self):
        table = "| a | b |\n| c | d |"
        for prefix in ["# Section", "Title\n====", "```\nx\n```"]:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.texts(self.parse(prefix + "\n" + table)), [prefix, table])

    def test_html_reference_and_inline_markup_are_not_rendered_or_dropped(self):
        text = "<div>\n# literal\n</div>\n\n[x]: https://example.com\n\n**bold** &amp; [x] ![a](x.png)"
        result = self.parse(text)
        self.assertEqual(self.texts(result), ["<div>", "# literal", "</div>",
                                            "[x]: https://example.com", "**bold** &amp; [x] ![a](x.png)"])
        self.assertNotIn("Section", [e["kind"] for e in result["elements"]])

    def test_plain_text_remains_line_evidence_without_markdown_kinds(self):
        result = self.parse("# Title\n```\nx\n```\n单位\n| a | b |\n| c | d |", "source.txt")
        self.assertEqual(self.texts(result), ["# Title", "```", "x", "```", "单位\n| a | b |\n| c | d |"])
        self.assertEqual({e["kind"] for e in result["elements"]}, {"Paragraph"})

    def test_normalization_map_retains_crlf_source_positions(self):
        result = self.parse("~~~\r\n中😀\r\n~~~")
        element = result["elements"][0]
        self.assertEqual(element["normalized_text"], "~~~\n中😀\n~~~")
        self.assertEqual(element["normalization_map"], [0, 1, 2, 3, 5, 6, 7, 9, 10, 11, 12])

    def test_hash_and_snapshot_and_anchor_identity(self):
        first, same, changed = self.parse("# Title"), self.parse("# Title"), self.parse("# title")
        self.assertEqual(first, same)
        self.assertNotEqual(first["original_sha256"], changed["original_sha256"])
        self.assertNotEqual(first["anchors"][0]["anchor_id"], changed["anchors"][0]["anchor_id"])
        self.assertIn("markdown-it-py", first["quality_report"]["dependency_versions"])
        with patch("kb_platform.documents.version", return_value="future-parser"):
            upgraded = self.parse("# Title")
        self.assertEqual(first["original_sha256"], upgraded["original_sha256"])
        self.assertNotEqual(first["parse_snapshot_id"], upgraded["parse_snapshot_id"])
        self.assertNotEqual(first["anchors"][0]["anchor_id"], upgraded["anchors"][0]["anchor_id"])

    def test_parser_failure_and_element_limit_discard_partial_evidence(self):
        for context in [patch("kb_platform.markdown_source.MarkdownIt.parse", side_effect=RuntimeError("broken")),
                        patch("kb_platform.documents.MAX_ELEMENTS", 1)]:
            with context:
                result = parse_native_document(b"one\ntwo", "source.md")
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["elements"], [])
            self.assertEqual(result["anchors"], [])
            self.assertEqual(result["evidence_spans"], [])

    def test_policy_blocks_parser(self):
        with patch("kb_platform.markdown_source.MarkdownIt.parse", side_effect=AssertionError("must not run")):
            result = parse_native_document(b"# source", "source.md", {"native_parse": False})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["elements"], [])

    def test_other_formats_do_not_depend_on_markdown_version(self):
        original = parse_native_document(b"name,price\nA,13", "price.csv")
        with patch("kb_platform.documents.TEXT_PARSER_VERSION", "future-text-parser"), \
                patch("kb_platform.documents.version", return_value="future-dependency"):
            updated = parse_native_document(b"name,price\nA,13", "price.csv")
        self.assertEqual(original["status"], "READY")
        self.assertEqual(original, updated)
