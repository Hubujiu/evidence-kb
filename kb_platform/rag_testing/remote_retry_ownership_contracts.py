from concurrent.futures import Future
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread, current_thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from kb_platform.embeddings import EmbeddingError
from kb_platform.remote import GoogleEmbeddingProvider, RemoteModelError, _FifoSlots, _GoogleRequest, _GoogleRequestPolicy
from kb_platform.rag_testing import luna_sdk_contracts
from kb_platform.rag_testing.budget import BudgetLedger
from kb_platform.rag_testing.provider_capture import observe_client


@contextmanager
def google_retry_fixture(statuses):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        config = root / 'providers.json'
        config.write_text(json.dumps({'google': {'platform': 'vertex_ai', 'model': 'gemini-embedding-2',
            'project_id': 'fixture-project', 'location': 'us', 'service_account_file': 'unused',
            'dimension': 768, 'workers': 1, 'trust_env': False, 'retries': 1, 'cache_mode': 'disabled'},
            'cache_dir': str(root / 'cache'), 'log_path': str(root / 'events.jsonl')}))
        credentials = SimpleNamespace(valid=True, token='synthetic-old')

        def refresh(request):
            credentials.token = 'synthetic-new'

        credentials.refresh = Mock(side_effect=refresh)

        def init(provider, path):
            provider._credentials, provider._auth_lock = credentials, Lock()
            provider._auth_request = Mock(side_effect=AssertionError('external OAuth forbidden'))

        with patch.object(GoogleEmbeddingProvider, '_init_vertex_auth', init):
            provider = GoogleEmbeddingProvider(config)
        sent, outcomes, sleeps, now = [], iter(statuses), [], [0.]

        def handle(request):
            sent.append(request)
            status = next(outcomes)
            body = ({'embedding': {'values': [1.] + [0.] * 767}, 'usageMetadata': {'promptTokenCount': 1}}
                    if status == 200 else {})
            return httpx.Response(status, json=body)

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        try:
            ledger = BudgetLedger(root / 'physical.sqlite', {'remote_embedding_calls': 10,
                'embedding_input_tokens': 100000, 'model': 0, 'input_tokens': 0, 'output_tokens': 0})
            observe_client(provider, ledger, root / 'capture', category='synthetic-auth-composition',
                           inner=httpx.MockTransport(handle))
            provider._request_policy._clock = lambda: now[0]
            provider._request_policy._sleep = sleep
            yield SimpleNamespace(provider=provider, ledger=ledger, credentials=credentials, sent=sent,
                                  sleeps=sleeps, now=now)
        finally:
            provider.close()


class RemoteRetryOwnershipContracts(unittest.TestCase):
    def luna_fixture(self, outcomes, **options):
        fixture = luna_sdk_contracts.LunaSDKContracts()
        self.addCleanup(fixture.doCleanups)
        return fixture, fixture.setup_provider(outcomes, **options)

    def test_transport_and_format_retry_counts_links_usage_and_payload_are_separate(self):
        completion = luna_sdk_contracts.completion
        fixture, state = self.luna_fixture([httpx.Response(503, json={}), completion('invalid'),
            httpx.Response(429, headers={'Retry-After': '.5'}, json={}), completion('{"summary":"original"}')],
            retries=1, max_retry_wait_seconds=.75)
        sleeps = []
        with patch('kb_platform.remote.time.sleep', side_effect=sleeps.append):
            self.assertEqual(state.provider.complete_json('Summarize', 'original', purpose='raptor_summarize'),
                             {'summary': 'original'})
        receipts = fixture.receipts(state)
        requests = [json.loads(row) for row in state.provider.log_path.read_text().splitlines()
                    if json.loads(row)['request']]
        self.assertEqual([row['attempt'] for row in receipts], [1, 2, 3, 4])
        self.assertEqual([row['format_attempt'] for row in requests], [1, 1, 2, 2])
        self.assertEqual([row['transport_attempt'] for row in requests], [1, 2, 1, 2])
        self.assertEqual(len({row['physical_request_id'] for row in receipts}), 4)
        self.assertTrue(all(b['retry_of'] == a['physical_request_id'] for a, b in zip(receipts, receipts[1:])))
        self.assertEqual([row['retry_reason'] for row in receipts],
                         [None, 'unavailable', 'format_correction', 'rate_limit'])
        self.assertEqual([row['status'] for row in receipts], ['ERROR', 'COMPLETE', 'ERROR', 'COMPLETE'])
        self.assertEqual(sleeps, [.5, .5])
        payloads = [json.loads(request.content) for request in state.requests]
        self.assertTrue(all(payload['messages'][1] == payloads[0]['messages'][1] for payload in payloads))
        self.assertNotIn('response_format', payloads[0])
        self.assertTrue(payloads[2]['response_format']['json_schema']['strict'])
        self.assertEqual(state.ledger.summary()['physical_requests'], 4)
        self.assertEqual(state.ledger.summary()['unknown_usage_requests'], 2)

    def test_unknown_effect_cannot_retry_or_trigger_format_correction(self):
        effects = []

        def uncertain(request):
            effects.append('provider-side effect')
            raise httpx.ReadTimeout('result unknown', request=request)

        fixture, state = self.luna_fixture([uncertain])
        with self.assertRaises(RemoteModelError) as caught:
            state.provider.complete_json('system', 'original', purpose='ocr')
        self.assertEqual(caught.exception.code, 'PROVIDER_OUTCOME_UNKNOWN')
        self.assertEqual(len(state.requests), 1)
        self.assertEqual(effects, ['provider-side effect'])
        self.assertEqual([row['status'] for row in fixture.receipts(state)], ['UNKNOWN'])
        self.assertEqual(state.ledger.summary()['unknown_usage_requests'], 1)

    def test_http_error_does_not_undo_effect_and_large_hint_denies_next_dispatch(self):
        effects, sleeps = [], []

        def received(request):
            effects.append('provider-side effect')
            return httpx.Response(429, headers={'Retry-After': '100'}, json={})

        fixture, state = self.luna_fixture([received], max_retry_wait_seconds=2)
        with patch('kb_platform.remote.time.sleep', side_effect=sleeps.append), \
                self.assertRaises(RemoteModelError) as caught:
            state.provider.complete_text('system', 'original', purpose='probe')
        self.assertEqual((caught.exception.code, caught.exception.status), ('PROVIDER_RETRY_WAIT_EXCEEDED', 429))
        self.assertEqual((len(state.requests), effects, sleeps), (1, ['provider-side effect'], []))
        self.assertEqual([row['status'] for row in fixture.receipts(state)], ['ERROR'])
        self.assertEqual(state.ledger.summary()['unknown_usage_requests'], 1)

    def test_google_authentication_continuation_shares_transport_retry_budget(self):
        for statuses in ([401, 503, 200], [503, 401, 200], [503, 401, 503], [401, 401]):
            with self.subTest(statuses=statuses), google_retry_fixture(statuses) as state:
                if statuses[-1] == 200:
                    self.assertEqual(state.provider.query('original'), [1.] + [0.] * 767)
                else:
                    with self.assertRaises(EmbeddingError) as caught:
                        state.provider.query('original')
                    self.assertEqual(caught.exception.status, statuses[-1])
                receipts = sorted((json.loads(path.read_text()) for path in
                    state.provider._receipts.directory.glob('*.json')), key=lambda row: row['attempt'])
                events = [json.loads(row) for row in state.provider.log_path.read_text().splitlines()]
                sent = [row for row in events if row['request']]
                self.assertEqual(len(state.sent), len(statuses))
                self.assertEqual([row['attempt'] for row in receipts], list(range(1, len(statuses) + 1)))
                self.assertEqual(len({row['physical_request_id'] for row in receipts}), len(statuses))
                self.assertTrue(all(b['retry_of'] == a['physical_request_id'] for a, b in zip(receipts, receipts[1:])))
                self.assertEqual(state.credentials.refresh.call_count, 1)
                self.assertEqual(sum(row['authentication_retry'] for row in sent), 1)
                self.assertEqual(max(row['auth_refreshes'] for row in sent), 1)
                self.assertEqual(state.ledger.summary()['unknown_usage_requests'], sum(status != 200 for status in statuses))
                self.assertEqual(state.sleeps, [1.] if 503 in statuses else [])

    def test_failed_credential_refresh_is_not_a_completed_auth_stage_or_model_attempt(self):
        with google_retry_fixture([401]) as state:
            trace = _GoogleRequest()
            state.credentials.refresh.side_effect = RuntimeError('synthetic authentication failure')
            with patch('kb_platform.remote._GoogleRequest', return_value=trace), self.assertRaises(EmbeddingError) as caught:
                state.provider.query('original')
            self.assertEqual(caught.exception.code, 'GOOGLE_AUTHENTICATION_FAILED')
            self.assertEqual((len(state.sent), trace.attempt, trace.auth_refreshes), (1, 1, 0))
            self.assertEqual(state.credentials.refresh.call_count, 1)
            self.assertEqual(state.ledger.summary()['physical_requests'], 1)

    def test_concurrent_rejected_tokens_adopt_current_credentials_without_second_refresh(self):
        entered, release, second_started = Event(), Event(), Event()
        credentials = SimpleNamespace(valid=True, token='synthetic-old')

        def refresh(request):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('AUTH_FIXTURE_BARRIER_EXPIRED')
            credentials.token = 'synthetic-new'

        credentials.refresh = Mock(side_effect=refresh)
        provider = object.__new__(GoogleEmbeddingProvider)
        provider._credentials, provider._auth_lock = credentials, Lock()
        provider._auth_request = Mock(side_effect=AssertionError('external OAuth forbidden'))
        provider.timeout, provider.project_id = 30, 'fixture-project'
        provider._record = Mock(side_effect=AssertionError('unexpected authentication failure'))
        results = [Future(), Future()]

        def run(index):
            if index:
                second_started.set()
            try:
                results[index].set_result(provider._vertex_headers('probe', 'key', 'Bearer synthetic-old'))
            except BaseException as error:
                results[index].set_exception(error)

        threads = [Thread(target=run, args=(index,)) for index in range(2)]
        threads[0].start()
        try:
            self.assertTrue(entered.wait(5))
            threads[1].start()
            self.assertTrue(second_started.wait(5))
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results[0].result(), results[1].result())
        self.assertEqual(credentials.refresh.call_count, 1)
        provider._auth_request.assert_not_called()

    def test_shared_cooldown_extension_is_rechecked_and_not_clamped_to_wait_budget(self):
        policy = _GoogleRequestPolicy({'requests_per_second': 1, 'max_retry_wait_seconds': 4})
        state, now, sleeps = _GoogleRequest(), [0.], []
        policy._clock = lambda: now[0]
        policy._next_send = 1.

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay
            if len(sleeps) == 1:
                policy.defer(0, 2.)
                policy.defer(0, 1.)

        policy._sleep = sleep
        policy.wait(state)
        self.assertEqual((sleeps, state.wait_seconds, policy._next_send), ([1., 2.], 3., 4.))
        self.assertEqual(policy.defer(0, 100.), 100.)
        with self.assertRaises(RemoteModelError) as caught:
            policy.wait(state)
        self.assertEqual(caught.exception.code, 'PROVIDER_RATE_LIMIT_WAIT_EXCEEDED')
        self.assertEqual(sleeps, [1., 2.])

    def test_shared_wait_records_actual_oversleep_without_claiming_hard_realtime_cap(self):
        policy, state, now = _GoogleRequestPolicy({'max_retry_wait_seconds': 2}), _GoogleRequest(), [0.]
        policy._clock = lambda: now[0]
        policy._next_send = 1.
        policy._sleep = lambda delay: now.__setitem__(0, 3.)
        policy.wait(state)
        self.assertEqual(state.wait_seconds, 3.)

    def test_fifo_claim_waits_for_actual_owner_exit_and_retains_enqueue_order(self):
        slots, order = _FifoSlots(1), []
        queued = {name: Event() for name in ('first', 'second')}
        entered, release = Event(), Event()
        original_wait = slots._cond.wait
        outcomes = {name: Future() for name in queued}

        def wait(timeout=None):
            queued[current_thread().name].set()
            return original_wait(timeout)

        def run(name):
            try:
                with slots:
                    order.append(name)
                    if name == 'first':
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('SLOT_FIXTURE_BARRIER_EXPIRED')
                outcomes[name].set_result(True)
            except BaseException as error:
                outcomes[name].set_exception(error)

        threads = [Thread(target=run, args=(name,), name=name) for name in queued]
        try:
            with patch.object(slots._cond, 'wait', wait), slots:
                for thread in threads:
                    thread.start()
                    self.assertTrue(queued[thread.name].wait(5))
                self.assertEqual(order, [])
            self.assertTrue(entered.wait(5))
            self.assertEqual(order, ['first'])
            self.assertEqual(slots._available, 0)
        finally:
            release.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertTrue(all(future.result() for future in outcomes.values()))
        self.assertEqual((order, slots._available, list(slots._waiters)), (['first', 'second'], 1, []))

    def test_interrupted_fifo_waiter_is_removed_without_consuming_a_slot(self):
        slots = _FifoSlots(1)
        interrupted = KeyboardInterrupt('synthetic condition interruption')
        with slots, patch.object(slots._cond, 'wait', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                slots.__enter__()
            self.assertIs(caught.exception, interrupted)
            self.assertEqual((slots._available, list(slots._waiters)), (0, []))
        with slots:
            self.assertEqual(slots._available, 0)
        self.assertEqual(slots._available, 1)
