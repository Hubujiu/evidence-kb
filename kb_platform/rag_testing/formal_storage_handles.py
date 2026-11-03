from pathlib import Path
import re


def retention_model_violations(source: str) -> list[str]:
    compact = re.sub(r"\s+", "", source)
    fragments = [
        r"vars == <<active, used, cache, outcomes, finishCount, observed, observedToken>>",
        r"finishCount = [t \in Tokens |-> 0]",
        r"Finish(t) == /\ t \in active /\ active' = active \ {t} /\ finishCount' = [finishCount EXCEPT ![t] = @ + 1]",
        r"Observe(t) == /\ t \in used \ active /\ finishCount[t] < 2",
        r"finishCount' = IF ReplayCachedFinish THEN [finishCount EXCEPT ![t] = @ + 1] ELSE finishCount",
        r"finishCount \in [Tokens -> 0..2]",
        r"NoReplay == \A t \in Tokens : finishCount[t] <= 1",
    ]
    errors = ["storage handle model: actual finish effects must determine replay safety"
              for fragment in fragments if re.sub(r"\s+", "", fragment) not in compact]
    if "replayed" in source or compact.count("finishCount'=") != 2:
        errors.append("storage handle model: replay cannot be a constant verdict or unrelated counter write")
    return errors


def ownership_violations(root: Path) -> list[str]:
    return retention_model_violations((root / 'formal/storage_handles/HandleRetention.tla').read_text('utf-8'))
