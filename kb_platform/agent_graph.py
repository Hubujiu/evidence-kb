"""Sequential agent scheduling; durable authority remains in AgentStore and receipts."""
from collections.abc import Callable
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from langsmith import tracing_context


PROTOCOL = 'bounded-agent-graph/1'


class TurnFlow(TypedDict, total=False):
    step: int
    decision: dict
    halted: bool
    answer: dict
    status: str
    stop: str
    verify: bool
    verdict: dict
    repaired: bool
    response: dict


def run_turn_graph(*, plan: Callable, tool: Callable, compose: Callable, verify: Callable,
                   repair: Callable, deliver: Callable, before_node: Callable,
                   step_limit: int, can_repair: bool, decision: dict):
    if type(step_limit) is not int or step_limit < 1:
        raise ValueError('INVALID_AGENT_STEP_LIMIT')
    graph = StateGraph(TurnFlow)

    def guarded(name, operation):
        def run(flow):
            before_node(name)
            return operation(flow)
        return run

    for name, operation in (('plan', plan), ('tool', tool), ('compose', compose),
                            ('verify', verify), ('repair', repair), ('deliver', deliver)):
        graph.add_node(name, guarded(name, operation), retry_policy=RetryPolicy(max_attempts=1))
    graph.add_edge(START, 'plan')
    graph.add_conditional_edges('plan', lambda flow: 'compose' if flow['decision']['action']
                                in {'answer', 'clarify'} else 'tool', ['compose', 'tool'])
    graph.add_conditional_edges('tool', lambda flow: 'compose' if flow['halted'] or
                                flow['step'] >= step_limit else 'plan', ['compose', 'plan'])
    graph.add_conditional_edges('compose', lambda flow: 'verify' if flow['verify'] else 'deliver',
                                ['verify', 'deliver'])
    graph.add_conditional_edges('verify', lambda flow: 'repair' if can_repair and not flow['repaired']
                                and not flow['verdict']['supported'] else 'deliver', ['repair', 'deliver'])
    graph.add_edge('repair', 'verify')
    graph.add_edge('deliver', END)
    # No checkpoint replay, node cache or ambient tracing may bypass the existing
    # request admission/recovery contracts or export private evidence.
    with tracing_context(enabled=False):
        result = graph.compile(checkpointer=False).invoke(
            {'step': 0, 'decision': decision, 'repaired': False},
            config={'recursion_limit': 2 * step_limit + 8, 'callbacks': []})
    return result['response']
