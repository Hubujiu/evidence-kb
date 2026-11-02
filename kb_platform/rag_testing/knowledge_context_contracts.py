from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from kb_platform.agent_context import ContextManager, ContextPolicy
from kb_platform.knowledge_context import KnowledgeContext, LocalHistorySource
from kb_platform.session_identity import Actor, PlatformError


class HistorySource:
    def __init__(self):
        self.requests = []
        self.snapshot = {'revision': 3, 'scope': 'scope-a', 'events': [
            {'seq': 1, 'kind': 'user', 'payload': {'text': 'Previous question', 'source_epoch': 1}},
            {'seq': 2, 'kind': 'assistant', 'payload': {
                'text': 'Allowed answer', 'source_epoch': 1, 'evidence_ids': ['doc-1']}},
            {'seq': 3, 'kind': 'assistant', 'payload': {
                'text': 'Removed answer', 'source_epoch': 1, 'evidence_ids': ['doc-2']}},
            {'seq': 4, 'kind': 'user', 'payload': {'text': 'Old scope', 'source_epoch': 0}},
            {'seq': 5, 'kind': 'tool_call', 'payload': {'source_epoch': 1}},
            {'seq': 6, 'kind': 'user', 'payload': {'text': 'Current question', 'source_epoch': 1}},
        ]}

    def read(self, request):
        self.requests.append(deepcopy(request))
        return deepcopy(self.snapshot)


class EvidenceSource:
    def __init__(self):
        self.requests = []
        self.authority = {'acl': 1, 'manifest': 'current'}
        self.authorizations = 0
        self.result = None

    def authorize(self, request):
        self.authorizations += 1
        return deepcopy(self.authority)

    def revalidate(self, request):
        self.requests.append(deepcopy(request))
        return deepcopy(request['evidence'] if self.result is None else self.result)

    def sources(self, request):
        self.requests.append(deepcopy(request))
        return [{'source_id': 'source-a', 'title': 'Visible original'}]


class KnowledgeContextContracts(unittest.TestCase):
    def fixture(self):
        history, evidence = HistorySource(), EvidenceSource()
        assembler = Mock(wraps=ContextManager(ContextPolicy(input_budget=12000)))
        context = KnowledgeContext(history, evidence, assembler=assembler)
        request = {
            'actor': {'tenant_id': 'tenant', 'principal_id': 'owner'}, 'scope': 'scope-a',
            'session_id': 'session-a', 'expected_revision': 3, 'authority': deepcopy(evidence.authority),
            'evidence': [{'citation_id': 'doc-1', 'text': 'Visible original evidence.'}],
            'source_policy': {'mode': 'AUTO', 'source_ids': []}, 'evidence_offsets': {'doc-1': 2},
            'source_epoch': 1, 'history_invalidated': False, 'reuse': True, 'before_seq': 6,
            'role': 'answer', 'system': 'Use authorized evidence.', 'state': {},
            'question': 'Current question', 'checkpoint': None, 'selection_query': 'original',
            'aliases': {}, 'rerank_payload': None,
        }
        return SimpleNamespace(context=context, history=history, evidence=evidence,
                               assembler=assembler, request=request)

    def assert_code(self, code, operation):
        with self.assertRaises(PlatformError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_context_reads_history_and_revalidates_evidence_through_replacement_ports(self):
        case = self.fixture()
        before = deepcopy(case.request)
        result = case.context.prepare_request(case.request)
        self.assertEqual(case.request, before)
        self.assertEqual(case.history.requests, [{
            'actor': case.request['actor'], 'session_id': 'session-a', 'expected_revision': 3}])
        self.assertEqual(case.evidence.requests, [{
            'actor': case.request['actor'], 'scope': 'scope-a', 'evidence': case.request['evidence'],
            'source_policy': case.request['source_policy']}])
        self.assertEqual([row['seq'] for row in result['prepared']['projection']['history']], [1, 2])
        self.assertEqual(result['evidence'][0]['context_offset'], 2)
        self.assertEqual(case.evidence.authorizations, 2)

    def test_disallowed_evidence_and_disabled_reuse_remove_dependent_assistant_history(self):
        for drop_evidence in (False, True):
            with self.subTest(drop_evidence=drop_evidence):
                case = self.fixture()
                if drop_evidence:
                    case.evidence.result = []
                else:
                    case.request['reuse'] = False
                result = case.context.prepare_request(case.request)
                self.assertEqual([row['seq'] for row in result['prepared']['projection']['history']], [1])

    def test_planner_aliases_are_applied_after_source_validation(self):
        case = self.fixture()
        case.request['aliases'] = {'doc-1': 'E1'}
        result = case.context.prepare_request(case.request)
        self.assertEqual(result['evidence'][0]['citation_id'], 'doc-1')
        self.assertEqual(result['prepared']['projection']['evidence'][0]['citation_id'], 'E1')
        self.assertEqual(result['prepared']['projection']['history'][1]['payload']['evidence_ids'], ['E1'])

    def test_authority_change_during_retrieval_rejects_before_assembly(self):
        case = self.fixture()
        read = case.history.read

        def changed(request):
            case.evidence.authority['acl'] = 2
            return read(request)

        case.history.read = changed
        self.assert_code('SCOPE_CHANGED', lambda: case.context.prepare_request(case.request))
        case.assembler.prepare.assert_not_called()

    def test_history_revision_and_scope_must_match_the_active_turn(self):
        for key, value in (('revision', 4), ('scope', 'scope-other')):
            with self.subTest(key=key):
                case = self.fixture()
                case.history.snapshot[key] = value
                self.assert_code('SESSION_REVISION_CONFLICT', lambda: case.context.prepare_request(case.request))
                case.assembler.prepare.assert_not_called()

    def test_evidence_source_cannot_add_duplicate_or_unrequested_evidence(self):
        for evidence in ([{'citation_id': 'other'}], [{'citation_id': 'doc-1'}] * 2, [{'text': 'missing id'}]):
            with self.subTest(evidence=evidence):
                case = self.fixture()
                case.evidence.result = evidence
                self.assert_code('CONTEXT_EVIDENCE_SOURCE_INVALID', lambda: case.context.prepare_request(case.request))
                self.assertEqual(case.history.requests, [])
                case.assembler.prepare.assert_not_called()

    def test_context_wire_request_rejects_untyped_service_objects_and_unknown_fields(self):
        for change in ({'history_source': object()}, {'question': object()}, {'expected_revision': True}):
            with self.subTest(change=change):
                case = self.fixture()
                case.request.update(change)
                self.assert_code('INVALID_CONTEXT_REQUEST', lambda: case.context.prepare_request(case.request))
                self.assertEqual(case.evidence.requests, [])
                self.assertEqual(case.history.requests, [])

    def test_history_invalidation_and_reranking_do_not_restore_old_conversation(self):
        for rerank in (False, True):
            with self.subTest(rerank=rerank):
                case = self.fixture()
                if rerank:
                    case.request['rerank_payload'] = {'query': 'original', 'documents': [{
                        'ref': 'R001', 'text': case.request['evidence'][0]['text']}]}
                else:
                    case.request['history_invalidated'] = True
                result = case.context.prepare_request(case.request)
                self.assertEqual(result['prepared']['projection']['history'], [])
                self.assertEqual(case.history.requests, [])
                self.assertEqual(len(case.evidence.requests), 1)

    def test_local_history_adapter_checks_revision_before_and_after_read(self):
        for revisions in ((3, 3), (2,), (3, 4)):
            with self.subTest(revisions=revisions):
                case = self.fixture()
                store = Mock()
                store.load.side_effect = [{'revision': revision, 'scope_id': 'scope-a'} for revision in revisions]
                store.events.return_value = case.history.snapshot['events']
                source = LocalHistorySource(store)
                request = {'actor': case.request['actor'], 'session_id': 'session-a', 'expected_revision': 3}
                if revisions == (3, 3):
                    self.assertEqual(source.read(request), case.history.snapshot)
                    store.events.assert_called_once_with(Actor('tenant', 'owner'), 'session-a')
                else:
                    self.assert_code('SESSION_REVISION_CONFLICT', lambda: source.read(request))
                    if revisions == (2,):
                        store.events.assert_not_called()
