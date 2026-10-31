from copy import deepcopy

from .context_budget import ContextBudget, ContextBudgetError, canonical, estimate_messages


def estimate_request(messages, tools):

    return estimate_messages([{'role': 'user', 'content': canonical({'messages': messages, 'tools': tools})}])


def _turns(messages):
    prefix, groups, pending = [], [], None
    for message in messages:
        if not isinstance(message, dict):
            raise ContextBudgetError('INVALID_CONTEXT_MESSAGES')
        role = message.get('role')
        if role == 'system' and not groups and pending is None:
            prefix.append(message)
            continue
        if role == 'user':
            if pending is not None:
                raise ContextBudgetError('CONTEXT_UNPAIRED_TOOL')
            groups.append([message])
        elif role == 'assistant':
            if not groups or pending is not None:
                raise ContextBudgetError('CONTEXT_UNPAIRED_TOOL')
            call = message.get('tool_call')
            if call is not None:
                if not isinstance(call, dict) or not isinstance(call.get('name'), str) or not call['name']:
                    raise ContextBudgetError('INVALID_CONTEXT_TOOL_CALL')
                pending = call['name']
            groups[-1].append(message)
        elif role == 'tool':
            if pending is None or message.get('name') != pending:
                raise ContextBudgetError('CONTEXT_UNPAIRED_TOOL')
            groups[-1].append(message)
            pending = None
        else:
            raise ContextBudgetError('INVALID_CONTEXT_MESSAGES')
    if not groups or pending is not None:
        raise ContextBudgetError('CONTEXT_UNPAIRED_TOOL' if pending is not None else 'CONTEXT_REQUIRES_USER')
    return prefix, groups


class RecentTurns:


    def select_start(self, groups, fits):
        if fits(0):
            return 0
        start = len(groups) - 1
        if not fits(start):
            raise ContextBudgetError('CONTEXT_REQUIRED_STATE_TOO_LARGE')
        while start and fits(start - 1):
            start -= 1
        return start


class HistoryContext:
    def __init__(self, *, system='', policy=None, compaction=None, measure=None):
        if not isinstance(system, str):
            raise ValueError('INVALID_CONTEXT_SYSTEM')
        self.system = system
        self.policy = policy if policy is not None else ContextBudget()
        self.compaction = compaction if compaction is not None else RecentTurns()
        self.measure = measure if measure is not None else estimate_request

    def prepare(self, messages, tools, context):
        if self.policy.strict_tokens:
            raise ContextBudgetError('TOKEN_CAPABILITY_UNVERIFIED')
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ContextBudgetError('INVALID_CONTEXT_INPUT')

        messages, tools = deepcopy(messages), deepcopy(tools)
        canonical({'messages': messages, 'tools': tools})
        prefix, groups = _turns(messages)
        if self.system:
            prefix.insert(0, {'role': 'system', 'content': self.system})

        def selected(start):
            return prefix + [message for group in groups[start:] for message in group]

        def measure(start):
            estimate = self.measure(deepcopy(selected(start)), deepcopy(tools))
            if not isinstance(estimate, dict) or type(estimate.get('tokens')) is not int or estimate['tokens'] < 0:
                raise ContextBudgetError('INVALID_CONTEXT_ESTIMATE')
            return estimate

        def fits(start):
            return measure(start)['tokens'] <= self.policy.effective_input_budget


        start = self.compaction.select_start(deepcopy(groups), fits)
        if type(start) is not int or not 0 <= start < len(groups):
            raise ContextBudgetError('INVALID_COMPACTION_BOUNDARY')
        estimate = measure(start)
        if estimate['tokens'] > self.policy.effective_input_budget:
            raise ContextBudgetError('CONTEXT_REQUIRED_STATE_TOO_LARGE')
        return {'messages': selected(start), 'tools': tools, 'context_projection': {
            'protocol': 'harness-context/1', 'omitted_turns': start,
            'retained_turns': len(groups) - start, 'history_rewritten': False,
            'input_budget': self.policy.effective_input_budget, 'estimate': estimate,
        }}
