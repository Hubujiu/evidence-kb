"""Provider-neutral decisions accepted by the native model/tool loop."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Answer:
    value: object
