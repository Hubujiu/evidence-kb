from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import ConfigDict, Field, TypeAdapter, WithJsonSchema


Text = Annotated[str, Field(max_length=6000)]
ShortText = Annotated[str, Field(max_length=500)]
Offset = Annotated[int, Field(ge=0, le=1000000)]
EventSeq = Annotated[int, Field(ge=1)]
Intent = Literal['knowledge', 'followup', 'correction', 'calculate', 'general', 'clarify']
Action = Literal['search', 'read', 'read_history', 'calculator', 'answer', 'clarify']
Completion = Literal['complete', 'partial', 'insufficient', 'conflict']


class WireObject(TypedDict):
    __pydantic_config__ = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Decision(WireObject):
    intent: Intent
    action: Action
    query: NotRequired[Text]
    citation_ids: NotRequired[Annotated[list[str], Field(max_length=10)]]
    offset: NotRequired[Offset]
    expression: NotRequired[Text]
    clarification: NotRequired[Text]
    topic_changed: NotRequired[bool]
    event_seq: NotRequired[EventSeq]
    subquestions: NotRequired[Annotated[list[ShortText], Field(max_length=6)]]
    missing_information: NotRequired[Annotated[list[ShortText], Field(max_length=6)]]
    sufficiency: NotRequired[Literal['sufficient', 'insufficient', 'unknown']]


class Quote(WireObject):
    evidence_id: Text
    quote: Text


InlineQuote = Annotated[Quote, WithJsonSchema(TypeAdapter(Quote).json_schema())]


class Claim(WireObject):
    text: Text
    evidence_ids: Annotated[list[str], Field(min_length=1)]
    quotes: Annotated[list[InlineQuote], Field(min_length=1)]


class UserStatement(WireObject):
    event_seq: EventSeq
    quote: Text


InlineClaim = Annotated[Claim, WithJsonSchema(TypeAdapter(Claim).json_schema())]
InlineStatement = Annotated[UserStatement, WithJsonSchema(TypeAdapter(UserStatement).json_schema())]


class Answer(WireObject):
    answer: Text
    abstain: bool
    claims: Annotated[list[InlineClaim], Field(max_length=15)]
    user_statements: NotRequired[Annotated[list[InlineStatement], Field(max_length=15)]]
    completion: NotRequired[Completion]
    unresolved: NotRequired[Annotated[list[ShortText], Field(max_length=12)]]


class Verdict(WireObject):
    supported: bool
    reason: Text


class TableFilter(WireObject):
    column: Text
    op: Literal['eq', 'lt', 'lte', 'gt', 'gte']
    value: str | float | bool | None


InlineFilter = Annotated[TableFilter, WithJsonSchema(TypeAdapter(TableFilter).json_schema())]


class AdvancedDecisionFields(WireObject, total=False):
    table_operation: Literal['', 'rows', 'count', 'sum', 'min', 'max']
    column: Annotated[str, Field(max_length=200)] | None
    left_key: Annotated[str, Field(max_length=200)]
    right_key: Annotated[str, Field(max_length=200)]
    join_how: Literal['', 'inner', 'left']
    filters: Annotated[list[InlineFilter], Field(max_length=10)]


TEXT = TypeAdapter(Text).json_schema()
DECISION_SCHEMA = TypeAdapter(Decision).json_schema()
ANSWER_SCHEMA = TypeAdapter(Answer).json_schema()
VERDICT_SCHEMA = TypeAdapter(Verdict).json_schema()
ADVANCED_PROPERTIES = TypeAdapter(AdvancedDecisionFields).json_schema()['properties']


def object_schema(properties, required=None):

    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required, 'additionalProperties': False}
