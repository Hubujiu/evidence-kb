from dataclasses import dataclass
from hashlib import sha256
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


class ContextBudgetError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class ContextBudget:
    input_budget: int = 12000
    output_reserve: int = 1500
    model_window: int = 32768
    safety_reserve: int = 1024
    strict_tokens: bool = False

    def __post_init__(self):
        for key in ("input_budget", "output_reserve", "model_window", "safety_reserve"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "safety_reserve" else 1):
                raise ValueError("INVALID_CONTEXT_POLICY")
        if not isinstance(self.strict_tokens, bool):
            raise ValueError("INVALID_CONTEXT_POLICY")
        if self.model_window <= self.output_reserve + self.safety_reserve:
            raise ValueError("INVALID_CONTEXT_POLICY")

    @property
    def effective_input_budget(self):
        return min(self.input_budget, self.model_window - self.output_reserve - self.safety_reserve)


def estimate_messages(messages):

    content_bytes = len(canonical(messages).encode("utf-8"))
    return {"tokens": content_bytes + 16 * len(messages) + 32,
            "kind": "conservative_utf8_estimate", "exact": False,
            "capability_verified": False, "utf8_bytes": content_bytes,
            "note": "Provider tokenizer and chat framing have not been verified."}





@dataclass(frozen=True)
class RequestBudget:
    limit: int
    unit: str = 'unicode_codepoints'
    scope: str = 'messages'

    def __post_init__(self):
        if (type(self.limit) is not int or self.limit < 1 or self.unit != 'unicode_codepoints'
                or self.scope not in {'messages', 'evidence'}):
            raise ContextBudgetError('INVALID_REQUEST_BUDGET')

    @classmethod
    def parse(cls, value):
        if value is None:
            return None
        if (not isinstance(value, dict) or not {'limit', 'unit'} <= set(value)
                or set(value) - {'limit', 'unit', 'scope'}):
            raise ContextBudgetError('INVALID_REQUEST_BUDGET')
        return cls(**value)

    def measure_text(self, text):
        return len(text)

    def measure_messages(self, messages):
        if self.scope == 'messages':
            return self.measure_text(json.dumps(messages, ensure_ascii=False))
        try:
            projection = json.loads(messages[-1]['content'])
            evidence = projection.get('evidence', projection.get('documents'))
            if not isinstance(evidence, list):
                raise ValueError('missing evidence array')
            return self.measure_text(canonical(evidence))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
            raise ContextBudgetError('BUDGET_EVIDENCE_UNOBSERVED') from error

    def validate_messages(self, messages):
        if self.measure_messages(messages) > self.limit:
            raise ContextBudgetError('REQUEST_CONTEXT_OVERFLOW')


class ContextCapacity:
    def __init__(self, policy, request_budget=None):
        self.limit = policy.effective_input_budget
        self.request = RequestBudget.parse(request_budget)

    def cost(self, messages):
        native = estimate_messages(messages)['tokens']
        if self.request is None:
            return native
        characters = self.request.measure_messages(messages)
        scaled = (characters * self.limit + self.request.limit - 1) // self.request.limit
        return max(native, scaled)

    def validate(self, messages):
        if self.cost(messages) > self.limit:
            raise ContextBudgetError('CONTEXT_REQUIRED_STATE_TOO_LARGE')

    def observation(self, messages):
        return ({'limit': self.request.limit, 'unit': self.request.unit,
                 'scope': self.request.scope, 'measured': self.request.measure_messages(messages)}
                if self.request is not None else None)
