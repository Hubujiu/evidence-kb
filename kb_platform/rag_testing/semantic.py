from typing import Literal

from pydantic import Field

from .contracts import Contract, digest


class Verdict(Contract):
    status: Literal["supported", "contradicted", "insufficient", "uncertain"]
    support_quote: str
    reason: str


def quote_choices(actual_text: str) -> list[str]:

    return list(dict.fromkeys([''] + [line[start:start + 96]
        for line in actual_text.splitlines() if line.strip()
        for start in range(0, len(line), 96)]))


def anchored_verdict_schema(actual_text: str) -> dict:
    schema = Verdict.model_json_schema()
    choices = quote_choices(actual_text)
    anchors = {'': '', **{f'q{i}': quote for i, quote in enumerate(choices[1:], 1)}}
    import json
    schema['properties']['support_quote'].update(
        enum=list(anchors), description='Return the anchor ID, not its text. Exact mapping: '
        + json.dumps(anchors, ensure_ascii=False))
    return schema


def validate_anchored_verdict(value, actual_text: str) -> Verdict:
    verdict = Verdict.model_validate(value)
    anchors = anchored_verdict_schema(actual_text)['properties']['support_quote']['enum']
    if verdict.support_quote not in anchors:
        raise ValueError('semantic quote is not a supplied anchor ID')
    if verdict.status == 'supported' and not verdict.support_quote:
        raise ValueError('supported semantic verdict requires an observed anchor')
    return verdict.model_copy(update={'support_quote': quote_choices(actual_text)[anchors.index(verdict.support_quote)]})


class Calibration(Contract):
    judge_revision: str
    rubric_hash: str
    dataset_hash: str
    positive_cases: int = Field(gt=0)
    negative_cases: int = Field(gt=0)
    false_passes: int = Field(ge=0)
    false_failures: int = Field(ge=0)
    max_false_pass_rate: float = Field(ge=0, le=1)
    max_false_fail_rate: float = Field(ge=0, le=1)

    def admitted(self):
        return (self.false_passes / self.negative_cases <= self.max_false_pass_rate
                and self.false_failures / self.positive_cases <= self.max_false_fail_rate)


def review(*, question, fact, actual_text, judge, judge_revision, rubric, calibration, budget,
           reservation_resources):

    if (not calibration.admitted() or calibration.judge_revision != judge_revision
            or calibration.rubric_hash != digest(rubric)):
        raise ValueError("judge calibration does not match the frozen reviewer")
    request = {"question": question, "fact": fact, "actual_text": actual_text,
               "rubric": rubric, "judge_revision": judge_revision}
    reservation = budget.reserve(digest(request), reservation_resources, category="judge")
    received = False
    try:
        raw = judge(request)
        received = True
        result = Verdict.model_validate(raw)
        if result.status == "supported" and (not result.support_quote or result.support_quote not in actual_text):
            raise ValueError("semantic support quote absent from observed text")
    except Exception as exc:
        budget.finish(reservation, status="ERROR" if received else "UNKNOWN",
                      diagnostic=f"{type(exc).__name__}: {exc}")
        raise
    budget.finish(reservation, status="COMPLETE")
    return {"request_hash": digest(request), "calibration_hash": digest(calibration),
            "reservation": reservation, "verdict": result.model_dump(),
            "release_eligible": False, "review_required": True}
