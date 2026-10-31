from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Contract):
    id: str = Field(min_length=1)
    kb: str = Field(min_length=1)
    version: str = Field(min_length=1)
    text: str
    sha256: str

    @model_validator(mode="after")
    def checksum(self):
        if text_hash(self.text) != self.sha256:
            raise ValueError("source checksum mismatch")
        return self


class Evidence(Contract):
    id: str = Field(min_length=1)
    source_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    fact: str = Field(min_length=1)
    qualifiers: tuple[str, ...] = ()


class AdapterInput(Contract):
    case_id: str
    question: str = Field(min_length=1)
    kb: str
    principal: str
    acl_revision: str
    allowed_sources: tuple[str, ...]
    as_of: str
    history: tuple[dict, ...] = ()
    context_budget: int = Field(gt=0)
    budget_unit: Literal["unicode_codepoints"] = "unicode_codepoints"


class Case(Contract):
    input: AdapterInput
    family: str
    split: Literal["dev", "holdout", "regression"] = "regression"
    slices: tuple[str, ...] = ()
    expected_behavior: Literal["answer", "abstain", "deny", "clarify"] = "answer"
    any_of: tuple[tuple[str, ...], ...] = ()
    critical: bool = True

    qrels: dict[str, int] = Field(default_factory=dict)
    forbidden_text: tuple[str, ...] = ()


class Suite(Contract):
    schema_version: Literal["1"] = "1"
    id: str
    label_provenance: Literal["synthetic_engineering", "human_reviewed", "public_annotation"]
    sources: tuple[Source, ...]
    evidence: tuple[Evidence, ...]
    cases: tuple[Case, ...]

    @model_validator(mode="after")
    def references(self):
        def unique(values, kind):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {kind}")
        unique([s.id for s in self.sources], "source")
        unique([e.id for e in self.evidence], "evidence")
        unique([c.input.case_id for c in self.cases], "case")
        if not self.cases:
            raise ValueError("empty suite")
        sources = {s.id: s for s in self.sources}
        evidence = {e.id: e for e in self.evidence}
        for e in self.evidence:
            if e.source_id not in sources:
                raise ValueError(f"unknown original source: {e.id}")
            s = sources[e.source_id]
            if e.end <= e.start or s.text[e.start:e.end] != e.quote:
                raise ValueError(f"invalid original anchor: {e.id}")
            if any(not q or q not in e.quote for q in e.qualifiers):
                raise ValueError(f"qualifier absent from original: {e.id}")
        families = {}
        for c in self.cases:
            if c.family in families and families[c.family] != c.split:
                raise ValueError("family crosses dataset splits")
            families[c.family] = c.split
            if not set(c.input.allowed_sources) <= sources.keys():
                raise ValueError("unknown authorized source")
            if any(sources[s].kb != c.input.kb for s in c.input.allowed_sources):
                raise ValueError("authorized source outside knowledge base")
            if c.expected_behavior == "answer" and not c.any_of:
                raise ValueError("answerable case needs sufficient evidence paths")
            for group in c.any_of:
                if not group or len(group) != len(set(group)) or not set(group) <= evidence.keys():
                    raise ValueError("invalid AND/OR evidence path")
                if any(evidence[e].source_id not in c.input.allowed_sources for e in group):
                    raise ValueError("gold evidence is unauthorized")
            if any(s not in sources or grade < 0 for s, grade in c.qrels.items()):
                raise ValueError("invalid qrels")
            if any(not value for value in c.forbidden_text):
                raise ValueError("empty forbidden text")
        return self


class Fragment(Contract):
    source_id: str
    source_sha256: str
    version: str
    kb: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    kind: Literal["extract", "paraphrase"] = "extract"


class RequestEvidenceBinding(Contract):

    message_index: int = Field(ge=0)
    json_path: tuple[str | int, ...]
    fragment_index: int = Field(ge=0)


class Trace(Contract):
    case_id: str
    corpus_hash: str
    implementation: str = Field(min_length=1)
    principal: str
    acl_revision: str
    status: Literal["OK", "ERROR", "TIMEOUT", "NOT_RUN"] = "OK"
    error: str | None = None
    stages: dict[str, tuple[Fragment, ...]] = Field(default_factory=dict)

    final: tuple[Fragment, ...] = ()
    final_context: str | None = None
    request_messages: tuple[dict, ...] = ()


    request_evidence_bindings: tuple[RequestEvidenceBinding, ...] = ()
    capture: Literal["pre_send", "replay", "unobserved"] = "unobserved"
    behavior: Literal["answer", "abstain", "deny", "clarify"] | None = None
    elapsed_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Plan(Contract):
    schema_version: Literal["1"] = "1"
    suite_hash: str
    mode: Literal["replay", "live"] = "replay"
    required_stages: tuple[str, ...] = ("retrieval", "rerank", "assembly")
    minimum_pass_rate: float = Field(default=1, ge=0, le=1)
    minimum_slice_cases: int = Field(default=1, ge=1)
    slice_floors: dict[str, float] = Field(default_factory=dict)
    top_k: int = Field(default=5, gt=0)

    require_answer_behavior: bool = False
    objective: Literal['full', 'evidence'] = 'full'

    @model_validator(mode="after")
    def floors(self):
        if self.objective == 'evidence' and self.require_answer_behavior:
            raise ValueError('evidence objective cannot require generated answer behavior')
        if any(not 0 <= floor <= 1 for floor in self.slice_floors.values()):
            raise ValueError("invalid slice floor")
        if len(self.required_stages) != len(set(self.required_stages)):
            raise ValueError("duplicate required stage")
        return self
