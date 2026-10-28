"""One runner for replay and SDK adapters; emits JSON, JUnit and escaped HTML."""
from __future__ import annotations

import html
import hashlib
import json
import time
from pathlib import Path
from typing import Protocol
from xml.etree.ElementTree import Element, SubElement, ElementTree

from . import VERSION
from .contracts import AdapterInput, Plan, Source, Suite, Trace, digest
from .scoring import corpus_hash, evaluate


class Adapter(Protocol):
    """Only input and authorized corpus enter the SUT. No gold or expected answer."""
    implementation: str

    def run(self, request: AdapterInput, sources: tuple[Source, ...], corpus: str) -> Trace: ...


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def reports(directory: Path, report: dict):
    write_json(directory / "report.json", report)
    root = Element("testsuite", name=VERSION, tests=str(report["total"]),
                   failures=str(report["counts"]["FAIL"]), errors=str(report["counts"]["ERROR"]),
                   skipped=str(report["counts"]["NOT_RUN"] + report["counts"]["UNCERTAIN"]))
    body = []
    for row in report["rows"]:
        case = SubElement(root, "testcase", name=row["case_id"], classname=row["family"])
        outcome = row["outcome"]
        if outcome != "PASS":
            tag = "failure" if outcome == "FAIL" else "error" if outcome == "ERROR" else "skipped"
            SubElement(case, tag, message=outcome).text = "; ".join(row["reasons"])
        detail = html.escape(json.dumps(row, ensure_ascii=False, indent=2))
        body.append(f"<details><summary>{html.escape(row['case_id'])}: {outcome}</summary><pre>{detail}</pre></details>")
    ElementTree(root).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
    (directory / "report.html").write_text(
        '<!doctype html><html lang="zh"><meta charset="utf-8"><title>RAG evidence regression</title>'
        '<style>body{max-width:1100px;margin:40px auto;font:16px system-ui}pre{white-space:pre-wrap}'
        'details{padding:12px;border-bottom:1px solid #ddd}</style>'
        f"<h1>{report['decision']} · {report['counts']['PASS']}/{report['total']}</h1>"
        f"<p>模式：{report['mode']}。回放耗时不代表线上延迟；工程夹具不代表生产质量。</p>"
        + "".join(body) + "</html>", encoding="utf-8")


def run(suite: Suite, plan: Plan, output: Path, *, adapter: Adapter | None = None,
        traces: list[Trace] | None = None):
    if digest(suite) != plan.suite_hash:
        raise ValueError("suite differs from frozen plan")
    if (adapter is None) == (traces is None):
        raise ValueError("provide exactly one adapter or trace collection")
    if adapter is not None and plan.mode != "live":
        raise ValueError("adapter execution requires live plan")
    if traces is not None and plan.mode != "replay":
        raise ValueError("recorded traces require replay plan")
    # Reject malformed replay collections before creating any output artifacts.
    if traces is not None:
        evaluate(suite, traces, plan)
    output.mkdir(parents=True, exist_ok=False)  # no overwrites or duplicate runners
    write_json(output / "plan.json", plan.model_dump(mode="json"))
    write_json(output / "suite.json", suite.model_dump(mode="json"))
    write_json(output / "state.json", {"status": "RUNNING", "version": VERSION})
    (output / "traces").mkdir()
    if adapter is not None:
        traces = []
        for case in suite.cases:
            start = time.perf_counter()
            try:
                # Detached models prevent accidental mutation of scorer-owned data.
                request = AdapterInput.model_validate_json(case.input.model_dump_json())
                sources = tuple(Source.model_validate_json(s.model_dump_json()) for s in suite.sources
                                if s.id in request.allowed_sources)
                trace = Trace.model_validate(adapter.run(request, sources, corpus_hash(suite)))
                if trace.case_id != request.case_id:
                    raise ValueError("adapter returned wrong case identity")
            except Exception as exc:
                trace = Trace(case_id=case.input.case_id, corpus_hash=corpus_hash(suite),
                              implementation=adapter.implementation, principal=case.input.principal,
                              acl_revision=case.input.acl_revision, status="ERROR",
                              error=f"{type(exc).__name__}: {exc}",
                              elapsed_ms=(time.perf_counter() - start) * 1000)
            traces.append(trace)
            write_json(output / "traces" / (digest(trace.case_id) + ".json"), trace.model_dump(mode="json"))
    else:
        for trace in traces:
            write_json(output / "traces" / (digest(trace.case_id) + ".json"), trace.model_dump(mode="json"))
    report = evaluate(suite, traces, plan)
    report["framework_files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(Path(__file__).parent.glob("*.py"))}
    report["framework_hash"] = digest(report["framework_files"])
    report["trace_hashes"] = {t.case_id: digest(t) for t in traces}
    reports(output, report)
    files = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.rglob("*")) if p.is_file() and p.name != "state.json"}
    write_json(output / "manifest.json", files)
    write_json(output / "state.json", {"status": "COMPLETE", "decision": report["decision"]})
    return report
