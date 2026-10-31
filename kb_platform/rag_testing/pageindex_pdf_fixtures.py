import json
from pathlib import Path
from .contracts import Source, Evidence, Case, AdapterInput, Suite, text_hash, digest
from .runner import write_json


def main():
    root = Path("reports/pageindex-full-20261028")
    output = root / "frozen-01"
    output.mkdir(exist_ok=False)
    pages = {k: json.loads((root / (k + "-pages.json")).read_text("utf-8")) for k in ("huawei", "fed")}
    sources = tuple(
        Source(
            id=f"{key}-p{i + 1}", kb=key, version="official-pypdf2-3.0.1/1", text=text, sha256=text_hash(text)
        )
        for key, rows in pages.items()
        for i, text in enumerate(rows)
    )
    evidence = []

    ranges = {
        "h-fin": ("huawei", 7, 0, pages["huawei"][6].index("注：")),
        "h-business": ("huawei", 17, 0, pages["huawei"][16].index("区域视角")),
        "h-rnd": ("huawei", 49, pages["huawei"][48].rindex("2024 年，研发"), len(pages["huawei"][48])),
        "f-rate": (
            "fed",
            9,
            pages["fed"][8].index("The FOMC has maintained"),
            pages["fed"][8].index("As labor"),
        ),
        "f-payments": ("fed", 62, 0, pages["fed"][61].index("The number of par")),
        "f-budget": (
            "fed",
            153,
            pages["fed"][152].index("2023 Budget P"),
            pages["fed"][152].index("1Before"),
        ),
        "f-capital": ("fed", 157, 0, pages["fed"][156].index("Board of Go vernors Budgets")),
    }
    for eid, (doc, p, start, end) in ranges.items():
        evidence.append(
            Evidence(
                id=eid,
                source_id=f"{doc}-p{p}",
                start=start,
                end=end,
                quote=pages[doc][p - 1][start:end],
                fact=eid + " original section including temporal and unit context",
            )
        )
    specs = [
        (
            "h-x1",
            "huawei",
            "按五年财务概要与研究创新披露，2024年销售收入（人民币百万元）、研发费用（人民币亿元）、研发占收入比例（%）依次是多少？",
            "862072|1797|20.8",
            ["h-fin", "h-rnd"],
        ),
        (
            "h-x2",
            "huawei",
            "结合2024年业务回顾及研发人员披露，ICT基础设施业务收入（人民币百万元）、研发员工数（万人）、研发员工占比（%）依次是多少？",
            "369903|11.3|54.1",
            ["h-business", "h-rnd"],
        ),
        (
            "h-x3",
            "huawei",
            "结合五年财务概要和业务回顾，2024年经营活动现金流与智能汽车解决方案业务收入各是多少人民币百万元？按前者、后者顺序作答。",
            "88417|26353",
            ["h-fin", "h-business"],
        ),
        (
            "h-x4",
            "huawei",
            "结合业务回顾与研究创新披露，2024年终端业务收入同比增长率（%）与研发费用占全年收入比例（%）分别是多少？前者比后者高多少个百分点？依次给出三个数。",
            "38.3|20.8|17.5",
            ["h-business", "h-rnd"],
        ),
        (
            "f-x1",
            "fed",
            "For a briefing combining Monetary Policy and Economic Developments with System Budgets: give the federal funds target range maintained since July 2023 (lower then upper, percent), followed by actual 2023 System net expenses (million dollars).",
            "5.25|5.5|6459.6",
            ["f-rate", "f-budget"],
        ),
        (
            "f-x2",
            "fed",
            "Combine the Payment System review and System Budgets: give the FedNow launch year and month (YYYY-MM), followed by the budgeted 2024 System operating expenses net of revenue and reimbursements (million dollars).",
            "2023-07|7123.7",
            ["f-payments", "f-budget"],
        ),
        (
            "f-x3",
            "fed",
            "For payment operations and infrastructure planning, give the 2023 commercial ACH total cost recovery percentage, followed by the 2024 capital budgets of the Board and Reserve Banks respectively (million dollars).",
            "108.8|389.9|913.8",
            ["f-payments", "f-capital"],
        ),
        (
            "f-x4",
            "fed",
            "Using Monetary Policy and Economic Developments and the 2024 Capital Budgets, give the upper federal funds target rate maintained since July 2023 (percent), then the combined 2024 Board and Reserve Bank capital budgets (million dollars, one decimal place).",
            "5.5|1303.7",
            ["f-rate", "f-capital"],
        ),
        ("h-c1", "huawei", "五年财务概要中2024年销售收入是多少人民币百万元？", "862072", ["h-fin"]),
        ("h-c2", "huawei", "研究创新披露的2024年研发费用占全年收入比例是多少（%）？", "20.8", ["h-rnd"]),
        (
            "f-c1",
            "fed",
            "In the 2024 Capital Budgets, what is the Board capital budget in million dollars?",
            "389.9",
            ["f-capital"],
        ),
        (
            "f-c2",
            "fed",
            "According to the Payment System review, in which year and month did FedNow launch? Use YYYY-MM.",
            "2023-07",
            ["f-payments"],
        ),
    ]
    cases = []
    gold = {}
    for cid, doc, question, answer, ids in specs:
        cases.append(
            Case(
                input=AdapterInput(
                    case_id=cid,
                    question=question,
                    kb=doc,
                    principal="public-pdf-evaluation",
                    acl_revision="public-originals/1",
                    allowed_sources=tuple(s.id for s in sources if s.kb == doc),
                    as_of="2026-10-28",
                    context_budget=118784,
                ),
                family=cid,
                split="dev",
                slices=("cross_chapter" if "-x" in cid else "single_section_control", doc),
                any_of=(tuple(ids),),
                qrels={next(e.source_id for e in evidence if e.id == i): 1 for i in ids},
            )
        )
        gold[cid] = {"answer": answer, "evidence_ids": ids}
    suite = Suite(
        id="official-pageindex-longpdf-dev/1",
        label_provenance="synthetic_engineering",
        sources=sources,
        evidence=tuple(evidence),
        cases=tuple(cases),
    )
    write_json(output / "suite.json", suite.model_dump(mode="json"))
    write_json(output / "gold.json", {"cases": gold})
    write_json(
        output / "provenance.json",
        dict(
            suite_hash=digest(suite),
            nature="author-written public-document development challenges; not independent business gold",
            reuse="original PDFs reused, canonical pages re-extracted by official SDK parser; no previous same-input SDK results",
            gold_rule="requested section passages with original units and dates; no model sees expected answers or evidence IDs",
            pdf_visual_review="huawei-evidence-pages.png and fed-evidence-pages.png",
            deliberate_scope="8 cross-chapter combinations + 4 single-section controls; overlapping facts, only 2 documents",
            context_budget="118784 unicode codepoints for serialized request messages; same transport input limit in both arms",
        ),
    )
    print("FROZEN", len(cases), "cases", len(sources), "pages", digest(suite))


if __name__ == "__main__":
    main()
