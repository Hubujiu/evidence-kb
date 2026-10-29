from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from kb_platform.knowledge_service import KnowledgeService as AgentService
from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.agent_model import CompletionEnvelope
from kb_platform.agent_schema import compile_schema, validate_output
from kb_platform.retrieval_pipeline import PipelineConfig
from .product_fixture import Authority, Gateway


class ScriptedModel:
    def __init__(self, action="search", query="Harbor Guide"):
        self.calls = []
        self.action = action
        self.query = query

    def complete(self, messages, *, role, schema, validator, **kwargs):
        self.calls.append(dict(role=role, messages=deepcopy(messages)))
        if role in ("decision", "retrieval"):
            value = dict(action=self.action if role == "decision" else "answer", intent="knowledge",
                         query=self.query, citation_ids=[], expression="", clarification="", topic_changed=False)
        elif role == "answer":
            projection = json.loads(messages[-1]["content"])
            item = next(row for row in projection["evidence"] if row.get("text"))
            value = dict(answer=item["text"], abstain=False, completion="complete", unresolved=[],
                         claims=[dict(text=item["text"], evidence_ids=[item["citation_id"]],
                             quotes=[dict(evidence_id=item["citation_id"], quote=item["text"])])])
        elif role == "verifier":
            value = dict(supported=True, reason="scripted control", task_complete=True, missing_information=[])
        else:
            raise AssertionError(role)
        validate_output(value, compile_schema(schema))
        validator(value)
        return CompletionEnvelope(value, kwargs['call_context']['call_id'],
            dict(available=True, total_tokens=10, prompt_tokens=7, completion_tokens=3),
            1, 'stop', 'none', 'synthetic-request')


class DefaultPathContracts(unittest.TestCase):
    def test_legacy_line_table_navigation_keeps_rows(self):
        from unittest.mock import patch
        docs = [('入口.md', '§委托：查《目标》§报价内的编码。'),
                ('目标.md', '§报价：单位CNY。\n| 编码 | 单价 |\n| A | 13 |')]
        from kb_platform.markdown_source import SourceBlock

        def historical_lines(text, **kwargs):
            offset = 0
            for number, line in enumerate(text.splitlines(keepends=True), 1):
                yield SourceBlock(offset, offset + len(line.rstrip("\r\n")), number, number)
                offset += len(line)

        with patch('kb_platform.documents.source_blocks', side_effect=historical_lines):
            a, g = self.make(docs)
        a.backend.search = lambda *args, **kwargs: [{**a.units[0], 'score': 1.0}]
        result = g.search_pipeline(a.actor, a.scope, '委托编码', {})
        texts = [h['text'] for h in result['hits']]
        self.assertIn('| 编码 | 单价 |', texts)
        self.assertIn('| A | 13 |', texts)
        g.revalidate(a.actor, a.scope, result['hits'], {})

    def test_pipe_table_caption_and_rows_are_one_original_unit(self):
        from kb_platform.documents import parse_native_document
        body = '§报价：单位CNY。\n| 编码 | 单价 |\n| A | 13 |'
        parsed = parse_native_document(('前文。\n\n' + body + '\n\n后文。').encode(), 'price.md')
        self.assertEqual([e['raw_text'] for e in parsed['elements']], ['前文。', body, '后文。'])
        code = '```\n| a | b |\n| c | d |\n```'
        parsed = parse_native_document(code.encode(), 'code.md')
        self.assertEqual(parsed['elements'][0]['kind'], 'Code')
        self.assertEqual(parsed['elements'][0]['raw_text'], code)

    def make(self, docs):
        authority = Authority(docs)
        return authority, Gateway(authority)

    def test_default_enabled_and_opt_out(self):
        self.assertTrue(PipelineConfig.parse().title_navigation)
        self.assertFalse(PipelineConfig.parse({"title_navigation": False}).title_navigation)

    def test_ordinary_query_does_not_append_unrelated_sections(self):
        a, g = self.make([("Guide.md", "# Main\nNEEDLE rule.\nCondition applies.\n# Other\nUnrelated.")])
        a.backend.search = lambda *args, **kwargs: [next({**u, "score": 1.0} for u in a.units if u["text"] == "NEEDLE rule.")]
        old = g.search_pipeline(a.actor, a.scope, "NEEDLE", {}, {"parent": "section", "title_navigation": False})
        new = g.search_pipeline(a.actor, a.scope, "NEEDLE", {}, {"parent": "section"})
        self.assertEqual(old["hits"], new["hits"])

    def test_navigation_never_replaces_ranked_original(self):
        a, g = self.make([("Main.md", "Important original."), ("Harbor Guide.md", "Other original.")])
        a.backend.search = lambda *args, **kwargs: [{**a.units[0], "score": 1.0}]
        new = g.search_pipeline(a.actor, a.scope, "Harbor Guide", {}, {"top_k": 1})
        self.assertEqual(new["hits"][0]["source_id"], "s0")
        self.assertIn("s1", [h["source_id"] for h in new["hits"]])
        limited = g.search_pipeline(a.actor, a.scope, "Harbor Guide", {}, {"top_k": 1, "max_expanded_units": 1})
        self.assertEqual([h["source_id"] for h in limited["hits"]], ["s0"])
        self.assertTrue(any(r["status"] == "OMITTED_UNIT_BUDGET" for r in limited["trace"]["expansion"]))

    def test_oversized_parent_keeps_actual_hit(self):
        a, g = self.make([("Guide.md", "# Main\nNEEDLE rule.\nCondition applies.")])
        a.backend.search = lambda *args, **kwargs: [next({**u, "score": 1.0} for u in a.units if u["text"] == "NEEDLE rule.")]
        new = g.search_pipeline(a.actor, a.scope, "NEEDLE", {}, {"parent": "section", "max_expanded_units": 1})
        self.assertEqual([h["text"] for h in new["hits"]], ["NEEDLE rule."])
        self.assertEqual(new["trace"]["expansion"][0]["status"], "CHILD_ONLY_UNIT_BUDGET")

    def test_navigation_admits_whole_units_when_whole_source_cannot_fit(self):
        a, g = self.make([("Harbor Guide.md", "# First\nOriginal one.\n# Second\nOriginal two.")])
        a.backend.search = lambda *args, **kwargs: []
        new = g.search_pipeline(a.actor, a.scope, "Harbor Guide", {}, {"max_expanded_units": 2})
        self.assertEqual(len(new["hits"]), 2)
        self.assertEqual(new["trace"]["expansion"][0]["status"], "PARTIAL_UNIT_BUDGET")
        self.assertTrue(new["trace"]["expansion"][0]["omitted_citation_ids"])
        g.revalidate(a.actor, a.scope, new["hits"], {})

    def test_unmatched_bridge_can_use_spare_context_budget(self):
        for body, budget in (("Bridge evidence with condition. " * 70, 4000),
                             ("A bridge with original facts. " * 90, 12000)):
            with self.subTest(length=len(body), budget=budget):
                records = [dict(citation_id="bridge", source_id="bridge", text=body),
                           dict(citation_id="small", source_id="small", text="small")]
                prepared = ContextManager(ContextPolicy(
                    input_budget=budget, evidence_strategy="query_blocks")).prepare(
                        "answer", "system", {}, [], records, "zzzzzz")
                self.assertEqual(prepared.projection["evidence"][0]["text"], body)
                self.assertLessEqual(prepared.estimate["tokens"], budget)

    def test_service_default_search_and_explicit_pipeline_reach_model_messages(self):
        for action in ("search", "search_pipeline"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                a, g = self.make([("Harbor Guide.md", "Harbor Guide requires Bridge Guide."),
                                  ("Bridge Guide.md", "Valid only until 2030.")])


                g.authorize = lambda actor, scope: dict(scope_id=scope, manifest_id="build", authority_revision=a.revision)
                g.sources = lambda actor, scope: [dict(source_id=r["source_id"], doc_id=r["source_id"],
                    source_version_id=r["source_version_id"], title=r["filename"]) for r in a.rows]
                model = ScriptedModel(action)
                service = AgentService(None, model, root=Path(folder), gateway=g,
                    retrieval_config={"channels": ["bm25"], "rerank": False})
                session = service.create(a.actor, a.scope)
                result = service.turn(a.actor, session["session_id"], message="What does Harbor Guide require?",
                                      request_id="default-path", expected_revision=0)
                self.assertNotEqual(result["status"], "error", result)
                self.assertTrue(result["trace"]["retrieval_stages"][0]["config"]["title_navigation"])
                call = next(c for c in model.calls if c["role"] == "answer")
                projection = json.loads(call["messages"][-1]["content"])
                self.assertTrue(any("2030" in e.get("text", "") for e in projection["evidence"]))
                self.assertTrue(any("Harbor Guide" in e.get("text", "") for e in projection["evidence"]))
                self.assertEqual(a.embedding_provider.queries, [])


if __name__ == "__main__":
    unittest.main()
