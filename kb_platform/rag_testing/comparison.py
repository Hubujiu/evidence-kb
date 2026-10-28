"""Paired complete-denominator comparisons; family-cluster bootstrap is diagnostic."""
import random
import statistics


def compare(baseline, candidate, *, seed=20261024, resamples=2000):
    if baseline["suite_hash"] != candidate["suite_hash"] or baseline["plan_hash"] != candidate["plan_hash"]:
        raise ValueError("comparison requires identical frozen suite and plan")
    if baseline.get("framework_hash") != candidate.get("framework_hash"):
        raise ValueError("rescore both runs with the same framework before comparing")
    left = {r["case_id"]: r for r in baseline["rows"]}
    right = {r["case_id"]: r for r in candidate["rows"]}
    if left.keys() != right.keys() or len(left) != len(baseline["rows"]) or len(right) != len(candidate["rows"]):
        raise ValueError("paired denominator mismatch")
    if resamples < 100:
        raise ValueError("at least 100 bootstrap draws required")
    pairs, groups = [], {}
    for qid, a in left.items():
        b = right[qid]
        delta = int(b["outcome"] == "PASS") - int(a["outcome"] == "PASS")
        if a["family"] != b["family"]:
            raise ValueError("family identity mismatch")
        pairs.append({"case_id": qid, "baseline": a["outcome"], "candidate": b["outcome"], "delta": delta})
        groups.setdefault(a["family"], []).append(delta)
    randomizer = random.Random(seed)
    blocks = list(groups.values())
    samples = sorted(statistics.mean(v for block in randomizer.choices(blocks, k=len(blocks)) for v in block)
                     for _ in range(resamples))
    return {"total": len(pairs), "wins": sum(p["delta"] > 0 for p in pairs),
            "ties": sum(p["delta"] == 0 for p in pairs), "losses": sum(p["delta"] < 0 for p in pairs),
            "mean_delta": statistics.mean(p["delta"] for p in pairs), "families": len(blocks),
            "family_bootstrap_95_interval": [samples[int(resamples * .025)], samples[int(resamples * .975)]],
            "quality_claim_eligible": all(r["outcome"] in ("PASS", "FAIL") for r in [*left.values(), *right.values()]),
            "limitation": "Failures, errors and missing cases remain in operational denominator; "
                          "template families may not be independent business samples.", "pairs": pairs}
