import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import time

from .budget_batches import LinkedBudgetLedger, freeze_predecessor
from .contracts import digest
from .runner import write_json
from .sdk_gateway import SDKGateway

PDFS = {
    "huawei": Path(".local/rag-expansion-corpus/huawei-2024-cn.pdf"),
    "fed": Path("output/pageindex-upstream-20261028/examples/documents/2023-annual-report.pdf"),
}
CAPS = dict(
    model=300, input_tokens=4000000, output_tokens=800000, remote_embedding_calls=0, embedding_input_tokens=0
)


def main():
    parser = argparse.ArgumentParser(description='Run unchanged installed official PageIndex SDK PDF indexing with bounded capture.')
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--providers", type=Path, default=Path(".local/providers.json"))
    parser.add_argument("--recover", type=Path, action="append")
    parser.add_argument("--total-model-cap", type=int, choices=(300, 350), default=300)
    args = parser.parse_args()
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from pageindex import PageIndexClient
    import pageindex

    parents = [freeze_predecessor(Path("reports/pageindex-20261028/live-01/physical-calls.sqlite3"))]
    caps = CAPS.copy()
    caps["model"] = args.total_model_cap
    recovery_protocol = None
    if args.recover:
        for directory in args.recover:
            prior = freeze_predecessor(directory / "physical-calls.sqlite3")
            parents.append(prior)
            caps = {k: caps[k] - prior["summary"]["effective_usage_or_reserved"][k] for k in CAPS}
        recovery_protocol = json.loads((args.recover[0] / "protocol.json").read_text("utf-8"))
    args.output.mkdir(parents=True, exist_ok=False)
    installed = Path(pageindex.__file__).parent
    write_json(
        args.output / "protocol.json",
        dict(
            upstream_commit="b81465d9ad3d80ee1df344508a85d1f2cdfb8a26",
            installed_code_hash=digest(
                {
                    str(p.relative_to(installed)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in installed.rglob("*.py")
                }
            ),
            dependencies={d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
            pdfs={
                k: dict(path=str(p.resolve()), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                for k, p in PDFS.items()
            },
            caps=caps,
            total_model_cap=args.total_model_cap,
            supplemental_reason=(
                "Complete 27 remaining upper-section summaries and document description; "
                "at most 50 new calls, all successful prefixes reused"
            )
            if args.total_model_cap == 350
            else None,
            mode="official SDK default flash",
            optimize="full",
            summary=True,
            summary_concurrency=1,
            authority="user requested official full PDF workflow and cost/time comparison; public local PDFs",
            remaining_new_budget=caps,
            tokenizer_verified=False,
            max_output_tokens_per_request=4096,
            repeated_payload_policy="blocked within operation; no repeated physical sends",
            monetary_cost="unverified provider rate; report measured tokens only",
        ),
    )
    write_json(args.output / "predecessors.json", parents)
    snapshot = args.output / "source-snapshot"
    snapshot.mkdir()
    for p in (Path(__file__), Path(__file__).with_name("sdk_gateway.py")):
        (snapshot / p.name).write_bytes(p.read_bytes())
    current_protocol = json.loads((args.output / "protocol.json").read_text("utf-8"))
    if recovery_protocol:
        for key in (
            "installed_code_hash",
            "pdfs",
            "mode",
            "optimize",
            "summary",
            "summary_concurrency",
            "max_output_tokens_per_request",
        ):
            if recovery_protocol[key] != current_protocol[key]:
                raise ValueError("RECOVERY_DEPENDENCY_CHANGED")
        for item in recovery_protocol["pdfs"].values():
            if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("RECOVERY_SOURCE_CHANGED")
    ledger = LinkedBudgetLedger(args.output / "physical-calls.sqlite3", caps, predecessors=parents)
    gateway = SDKGateway(
        args.providers,
        ledger,
        args.output,
        recovery=args.recover,
        dependencies=recovery_protocol or current_protocol,
    )
    model = "openai/" + gateway.provider.model
    client = PageIndexClient(
        index_model=model,
        chat_model=model,
        summary_concurrency=1,
        storage_path=str((args.output / "store").resolve()),
        index_backend={**gateway.backend, "max_retries": 0},
        chat_backend=gateway.backend,
    )
    results = {}
    try:
        for key, path in PDFS.items():
            gateway.context = dict(stage="index", document=key)
            started = time.perf_counter()
            try:
                result = client.submit_document(str(path.resolve()))
                results[key] = dict(
                    status="COMPLETE", result=result, elapsed_seconds=time.perf_counter() - started
                )
                write_json(
                    args.output / (key + "-tree.json"), client.get_tree(result["doc_id"], node_summary=True)
                )
            except Exception as exc:
                results[key] = dict(
                    status="ERROR", error=str(exc), elapsed_seconds=time.perf_counter() - started
                )
            write_json(args.output / "results.json", results)
            if gateway.blocked:
                break
    finally:
        gateway.close()
        write_json(args.output / "budget-after.json", ledger.cumulative_summary())


if __name__ == "__main__":
    main()
