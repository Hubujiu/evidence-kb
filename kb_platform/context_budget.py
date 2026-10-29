"""Provider-neutral context accounting; estimates are not verified token limits."""
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
    """Conservative engineering estimate, not a mathematical provider upper bound."""
    content_bytes = len(canonical(messages).encode("utf-8"))
    return {"tokens": content_bytes + 16 * len(messages) + 32,
            "kind": "conservative_utf8_estimate", "exact": False,
            "capability_verified": False, "utf8_bytes": content_bytes,
            "note": "Provider tokenizer and chat framing have not been verified."}
