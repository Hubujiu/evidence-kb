from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Lock

from kb_platform.plugin_host import PluginHost, PluginSpec


class Fixture:
    def __init__(self, config):
        self.config = config
        self.lock = Lock()
        self.host = None
        self.record('created', pid=os.getpid())

    def record(self, event, **fields):
        with self.lock:
            with Path(self.config['events']).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'event': event, **fields}) + '\n')

    def close(self):
        if self.host is not None:
            self.host.close()
        self.record('closed')


class Generic(Fixture):
    def __init__(self, config):
        super().__init__(config)
        fixture = self

        def base(ctx, config):
            class Arithmetic:
                def double(self, value):
                    return value * config['factor']
            ctx.provide('arithmetic', Arithmetic())
            ctx.defer(lambda: fixture.record('disposed', plugin=ctx.plugin_id))

        def dependent(ctx, config):
            ctx.require('arithmetic')
            class Nested:
                def calculate(self, value):
                    return ctx.host.call('arithmetic', 'double', value) + 1
            ctx.provide('nested', Nested())
            ctx.defer(lambda: fixture.record('disposed', plugin=ctx.plugin_id))

        self.catalog = [PluginSpec('fixture.arithmetic', ('arithmetic',), (), base),
                        PluginSpec('fixture.nested', ('nested',), ('arithmetic',), dependent)]
        self.host = PluginHost({'version': 1, 'plugins': [
            {'id': 'base', 'use': 'fixture.arithmetic', 'config': {'factor': 2}},
            {'id': 'dependent', 'use': 'fixture.nested'}]}, self.catalog)

    def run(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            values = list(pool.map(lambda value: self.host.call('nested', 'calculate', value), range(16)))
        child = self.host.scope({'version': 1, 'plugins': [
            {'id': 'override', 'use': 'fixture.arithmetic', 'config': {'factor': 3}, 'overrides': ['arithmetic']},
            {'id': 'child-dependent', 'use': 'fixture.nested', 'overrides': ['nested']}]}, self.catalog)
        try:
            scoped = child.call('nested', 'calculate', 5)
            inherited = self.host.call('nested', 'calculate', 5)
        finally:
            child.close()
        snapshot = self.host.inspect()
        return {'values': values, 'scoped': scoped, 'parent': inherited,
                'leases': snapshot['leases'], 'worker_pid': os.getpid()}


class Agent(Fixture):
    def __init__(self, config):
        super().__init__(config)
        from kb_platform.agent_decisions import Answer, ToolCall
        from kb_platform.agent_plugins import ToolPlugin, ToolResult
        from kb_platform.harness_profile import create_agent_application, load_agent_profile

        self.requests, self.effects = [], []

        def decide(messages, tools):
            self.requests.append(deepcopy(messages))
            return (ToolCall('echo', {'text': 'receipt'}) if messages[-1]['content'] == 'first'
                    else Answer('answer'))

        def effect(context, arguments):
            self.effects.append(context.call_id)
            return ToolResult(arguments)

        profile = load_agent_profile(environment={})
        next(row for row in profile['plugins'] if row['id'] == 'store')['config']['root'] = config['store']
        tool = ToolPlugin('echo', 'Echo text', {'type': 'object', 'properties': {'text': {'type': 'string'}},
            'required': ['text'], 'additionalProperties': False}, effect)
        self.application, self.host = create_agent_application(decide=decide, tools=(tool,),
                                                                profile=profile, environment={})

    def run(self):
        from kb_platform.session_identity import Actor

        actor = Actor('fixture', 'owner')
        session = self.application.create(actor)['session_id']
        first = self.application.turn(actor, session, message='first', request_id='one', expected_revision=0)
        replay = self.application.turn(actor, session, message='first', request_id='one', expected_revision=0)
        second = self.application.turn(actor, session, message='continue', request_id='two', expected_revision=1)
        return {'first': first, 'replay': replay, 'second': second, 'effects': self.effects,
                'requests': self.requests, 'worker_pid': os.getpid()}


class Failure(Fixture):
    def run(self):
        self.record('run')
        if self.config['phase'] == 'run':
            raise RuntimeError('HOST_FIXTURE_RUN_FAILURE')
        return {'ok': True}

    def close(self):
        super().close()
        if self.config['phase'] == 'close':
            raise RuntimeError('HOST_FIXTURE_CLOSE_FAILURE')


class SharedModel(Fixture):
    def __init__(self, config):
        super().__init__(config)
        import httpx
        from kb_platform.model_http import OpenAICompatibleAdapter
        from kb_platform.model_plugins import model_plugins
        from kb_platform.model_service import Capability
        from .budget import BudgetLedger

        self.ledger = BudgetLedger(Path(config['ledger']), {'requests': 2})
        self.sent = []

        def reply(request):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"text":"scripted"}'},
                'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}})

        def provider(ctx, config):
            client = httpx.Client(transport=httpx.MockTransport(reply))
            ctx.defer(client.close)
            adapter = OpenAICompatibleAdapter(provider='fixture', model='shared',
                endpoint='https://synthetic.invalid/v1', api_key='synthetic', capabilities=[Capability.CHAT],
                ledger=self.ledger, reservation={'requests': 1}, client=client)
            dispose = ctx.require('models').register('generation', adapter,
                registration_id=ctx.registration_id(kind='model_route'), owner_identity=ctx.identity,
                owner_lease=ctx.lease)
            ctx.defer(dispose)
            ctx.provide('fixture.provider.ready', True)

        def consumer(ctx, config):
            model = ctx.require('llm')
            class Consumer:
                def generate(self):
                    result = model.complete([{'role': 'user', 'content': config['purpose']}], role='answer',
                        schema={'type': 'object', 'properties': {'text': {'type': 'string'}},
                                'required': ['text'], 'additionalProperties': False}, call_context={})
                    return result.value
            ctx.provide(ctx.spec.provides[0], Consumer())

        catalog = (*model_plugins(), PluginSpec('fixture.provider', ('fixture.provider.ready',), ('models',), provider),
            PluginSpec('fixture.title', ('title',), ('llm', 'fixture.provider.ready'), consumer),
            PluginSpec('fixture.compact', ('compact',), ('llm', 'fixture.provider.ready'), consumer))
        self.host = PluginHost({'version': 1, 'plugins': [
            {'id': 'models', 'use': 'models.service'}, {'id': 'provider', 'use': 'fixture.provider'},
            {'id': 'llm', 'use': 'models.structured'},
            {'id': 'title', 'use': 'fixture.title', 'config': {'purpose': 'title'}},
            {'id': 'compact', 'use': 'fixture.compact', 'config': {'purpose': 'compact'}}]}, catalog)

    def run(self):
        return {'title': self.host.call('title', 'generate'), 'compact': self.host.call('compact', 'generate'),
                'requests': self.sent, 'ledger': self.ledger.summary()}


class ProcessLLM(SharedModel):
    def __init__(self, config):
        super().__init__(config)
        from kb_platform.plugin_process import ProcessPackages

        root = Path(config['workspace'])
        manager = ProcessPackages(root / 'installed')
        installed = manager.install(Path(__file__).resolve().parents[2] / 'examples/process-llm')
        spec = manager.catalog(installed['package'], journal=root / 'journal')
        try:
            self.consumer = self.host.scope({'version': 1, 'plugins': [{'id': 'writer', 'use': spec.key}]}, [spec])
        except BaseException:
            self.host.close()
            raise

    def run(self):
        result = self.consumer.call('external.writer', 'compose', 'Write a short title')
        return {'completion': result, 'requests': self.sent, 'ledger': self.ledger.summary(),
                'remaining_leases': self.host.inspect()['leases'],
                'model_routes': self.host.service('models').inspect()}


class Cancellation(Fixture):
    def __init__(self, config):
        super().__init__(config)
        self.entered = Event()
        self.operation = None
        fixture = self

        def mount(ctx, config):
            class Wait:
                def wait(self):
                    call = ctx.host.current_call
                    fixture.operation = call['operation']
                    fixture.record('running')
                    fixture.entered.set()
                    if not call['cancel_signal'].wait(15):
                        raise RuntimeError('HOST_FIXTURE_CANCEL_TIMEOUT')
                    fixture.record('cancelled')
                    return 'cancelled'
            ctx.provide('wait', Wait())
            ctx.defer(lambda: fixture.record('disposed', plugin='wait'))

        self.host = PluginHost({'version': 1, 'plugins': [{'id': 'wait', 'use': 'fixture.wait'}]},
                               [PluginSpec('fixture.wait', ('wait',), (), mount)])

    def run(self):
        return {'value': self.host.call('wait', 'wait')}

    def stop(self):
        self.record('stop')
        if not self.entered.wait(5):
            raise RuntimeError('HOST_FIXTURE_NOT_RUNNING')
        self.host.cancel(self.operation)


class Knowledge(Fixture):
    def run(self):
        from .contracts import Plan, Suite, digest
        from .default_path_replay import ProductAdapter, service_wire_suite
        from .runner import run
        from .scoring import corpus_hash

        original = Suite.model_validate_json(Path(self.config['suite']).read_text('utf-8'))
        case = service_wire_suite(original).cases[0]
        selected = tuple(source for source in original.sources if source.id in case.input.allowed_sources)
        suite = original.model_copy(update={'id': original.id + '/host-exact-control/service-wire-24000',
                                            'cases': (case,), 'sources': selected})
        catalog = [{'id': source.id, 'title': source.id, 'text': source.text} for source in selected]
        adapter = ProductAdapter(catalog, True, service=True)
        trace = adapter.run(case.input, selected, corpus_hash(suite))
        output = Path(self.config['scoring_output'])
        report = run(suite, Plan(suite_hash=digest(suite), objective='evidence', mode='replay',
            required_stages=('retrieval', 'assembly')), output, traces=[trace])
        (output / 'product-observations.json').write_text(json.dumps(adapter.products, ensure_ascii=False), 'utf-8')
        return {'decision': report['decision'], 'counts': report['counts'], 'total': report['total'],
                'capture': trace.capture, 'messages': len(trace.request_messages), 'new_provider_calls': 0}


class ProcessDependency(Fixture):
    def run(self):
        from kb_platform.plugin_process import ProcessPackages
        from .plugin_process_integration import dependency_calls_case

        root = Path(self.config['workspace'])
        manager = ProcessPackages(root / 'installed')
        installed = manager.install(Path(__file__).resolve().parents[2] / 'examples/process-dependency')
        result = dependency_calls_case(manager, installed, root)
        self.record('dependency_calls_finished')
        return {**result, 'worker_pid': os.getpid()}


def generic(config):
    return Generic(config)


def agent(config):
    return Agent(config)


def failure(config):
    if config['phase'] == 'startup':
        raise RuntimeError('HOST_FIXTURE_STARTUP_FAILURE')
    if config['phase'] == 'mount':
        fixture = Fixture(config)
        def mount(ctx, config):
            fixture.record('mounting')
            ctx.defer(lambda: fixture.record('rolled_back'))
            raise RuntimeError('HOST_FIXTURE_MOUNT_FAILURE')
        fixture.host = PluginHost({'version': 1, 'plugins': [{'id': 'fail', 'use': 'fixture.fail'}]},
                                   [PluginSpec('fixture.fail', (), (), mount)])
        return fixture
    return Failure(config)


def cancellation(config):
    return Cancellation(config)


class SignalApplication(Fixture):
    def __init__(self, config):
        super().__init__(config)
        self.stopping = Event()

    def run(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.record('running', worker=os.getpid(), descendant=child.pid)
        self.stopping.wait(30)
        return {'stopped': self.stopping.is_set()}

    def stop(self):
        self.record('stop')
        self.stopping.set()

    def close(self):
        self.record('closing')
        if self.config.get('hang_close'):
            Event().wait(30)
        super().close()


def signal_application(config):
    return SignalApplication(config)


def knowledge(config):
    return Knowledge(config)


def shared_model(config):
    return SharedModel(config)


def process_dependency(config):
    return ProcessDependency(config)


def process_llm(config):
    return ProcessLLM(config)


def process_tree_worker():
    receipt, phase = Path(sys.argv[1]), sys.argv[2]
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    receipt.write_text(json.dumps({'worker': os.getpid(), 'descendant': child.pid}), 'utf-8')
    if phase == 'malformed':
        print('invalid-protocol', flush=True)
        Event().wait(30)
    elif phase == 'timeout':
        Event().wait(30)
    else:
        for line in sys.stdin:
            request = json.loads(line)
            result = {'handshake': {'runtime': 'plugin-runtime/1'}, 'boot': {'ready': True},
                      'run': {'ok': True}, 'shutdown': {}}[request['method']]
            print(json.dumps({'protocol': 'plugin-host/1', 'kind': 'response',
                'id': request['id'], 'ok': True, 'result': result}), flush=True)
            if request['method'] == 'shutdown':
                return


if __name__ == '__main__':
    process_tree_worker()
