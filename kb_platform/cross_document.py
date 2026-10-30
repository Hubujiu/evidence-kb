from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation
import re


def explicit_title_targets(documents, seeds, *, limit=2, exclude_source_ids=()):

    if type(limit) is not int or not 1 <= limit <= 2:
        raise ValueError("Invalid title target limit")
    titles = defaultdict(list)
    for document in documents:
        title = document["title"].strip()
        if title:
            titles[title.casefold()].append(document)
    patterns = []
    for key, matches in titles.items():
        if len(matches) != 1:
            continue
        title = matches[0]["title"].strip()

        if len(title) >= 4 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._\-]*", title):
            patterns.append(
                (
                    matches[0]["source_id"],
                    re.compile(r"(?<![A-Za-z0-9_])" + re.escape(title) + r"(?![A-Za-z0-9_])", re.I),
                )
            )
    found, seen = [], set()
    for seed in seeds[:3]:
        text = seed["text"][:4000]
        mentions = []
        for match in re.finditer(r'《([^》\n]{1,200})》|“([^”\n]{1,200})”|"([^"\n]{1,200})"', text):
            key = next(value for value in match.groups() if value is not None).strip().casefold()
            matches = titles.get(key, [])
            if len(matches) == 1:
                mentions.append((match.start(), match.end(), matches[0]["source_id"]))
        for source_id, pattern in patterns:
            match = pattern.search(text)
            if match:
                mentions.append((match.start(), match.end(), source_id))
        for start, end, source_id in sorted(mentions):
            if source_id == seed["source_id"] or source_id in seen or source_id in exclude_source_ids:
                continue
            seen.add(source_id)
            found.append(
                {
                    "seed_citation_id": seed["citation_id"],
                    "target_source_id": source_id,
                    "mention_start": start,
                    "mention_end": end,
                    "mention": seed["text"][start:end],
                }
            )
            if len(found) == limit:
                return found
    return found


def source_links(documents):

    titles = defaultdict(list)
    for d in documents:
        titles[d["title"]].append(d["id"])
    links = []
    unresolved = []
    for d in documents:
        for match in re.finditer(r"《([^》]+)》", d["text"]):
            target = match.group(1)
            if target == d["title"]:
                continue
            start = d["text"].rfind("\n", 0, match.start()) + 1
            end = d["text"].find("\n", match.end())
            end = len(d["text"]) if end < 0 else end
            quote = d["text"][start:end]
            candidates = titles.get(target, [])
            if len(candidates) != 1:
                unresolved.append(
                    {
                        "source": d["id"],
                        "target_title": target,
                        "reason": "missing" if not candidates else "ambiguous",
                        "quote": quote,
                    }
                )
                continue
            kind = (
                "supersedes"
                if re.search("替代|废止|旧版|修订", quote)
                else "join"
                if re.search("关联|连接|编号|代码", quote)
                else "conditional"
                if re.search("如果|若|否则|仅当|条件", quote)
                else "next"
                if re.search("下一步|之后|随后|继续|转入", quote)
                else "reference"
            )
            links.append(
                {
                    "source": d["id"],
                    "target": candidates[0],
                    "kind": kind,
                    "quote": quote,
                    "start": start,
                    "end": end,
                }
            )
    return links, unresolved


class DocumentGraph:
    def __init__(self, documents):
        self.links, self.unresolved = source_links(documents)
        self.adjacency = defaultdict(list)
        for link in self.links:
            self.adjacency[link["source"]].append(link)

    def expand(self, seeds, hops=3, limit=20):
        queue = deque((seed, 0) for seed in seeds)
        seen = set()
        result = []
        trace = []
        while queue and len(result) < limit:
            node, depth = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            result.append(node)
            if depth >= hops:
                continue
            for edge in self.adjacency[node]:
                trace.append(edge)
                queue.append((edge["target"], depth + 1))
        return result, trace


def navigate_source_references(documents, seeds, *, hops=3, document_limit=16, fragment_limit=60):

    if (
        not isinstance(documents, (list, tuple))
        or not documents
        or any(
            not isinstance(d, dict)
            or not isinstance(d.get("id"), str)
            or not d["id"]
            or not isinstance(d.get("title"), str)
            or not d["title"]
            or not isinstance(d.get("text"), str)
            or not d["text"].strip()
            for d in documents
        )
        or len({d["id"] for d in documents}) != len(documents)
    ):
        raise ValueError("Navigation requires unique authorized source documents")
    if any(type(value) is not int or value < 1 for value in (hops, document_limit, fragment_limit)):
        raise ValueError("Navigation limits must be positive integers")
    by_id = {d["id"]: d for d in documents}
    if not isinstance(seeds, (list, tuple)) or not seeds or any(seed not in by_id for seed in seeds):
        raise ValueError("Navigation seeds must belong to the authorized document set")
    graph = DocumentGraph(documents)
    reached, edges = graph.expand(list(dict.fromkeys(seeds)), hops=hops, limit=document_limit)
    reached_set = set(reached)
    spans, seen = [], set()

    def append(doc_id, start, end):
        key = (doc_id, start, end)
        if key not in seen and len(spans) < fragment_limit:
            seen.add(key)
            spans.append(
                {"doc_id": doc_id, "start": start, "end": end, "text": by_id[doc_id]["text"][start:end]}
            )

    for edge in edges:

        if edge["source"] not in reached_set or edge["target"] not in reached_set:
            continue
        append(edge["source"], edge["start"], edge["end"])
        target = by_id[edge["target"]]
        after = edge["quote"].split("《" + target["title"] + "》", 1)[-1]


        headings = set(re.findall(r"(?m)^\s*§([^：:\s\n]+)[：:]", target["text"]))
        references = [heading for heading in headings if after.lstrip().startswith("§" + heading)]
        if references:
            marker = "§" + max(references, key=lambda heading: (len(heading), heading))
            if target.get("paragraph_spans"):
                for start, end in target["paragraph_spans"]:
                    if not (
                        type(start) is int and type(end) is int and 0 <= start < end <= len(target["text"])
                    ):
                        raise ValueError("Invalid original paragraph bounds")
                    if re.search(re.escape(marker) + r"(?=[：:\s])", target["text"][start:end]):
                        append(target["id"], start, end)
                continue
            for match in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", target["text"], re.S):
                if re.search(re.escape(marker) + r"(?=[：:\s])", match.group()):
                    append(target["id"], match.start(), match.end())
    return {
        "fragments": spans,
        "visited_documents": reached,
        "unresolved_references": graph.unresolved,
        "model_calls": 0,
    }


def safe_prefix(text, token_limit, tokenizer):

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(tokenizer.encode(text[:mid], disallowed_special=())) <= token_limit:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def parse_pipe_tables(documents):
    tables = []
    for d in documents:
        group = []
        start = 0
        offset = 0

        def finish():
            if len(group) < 2:
                return
            rows = [[v.strip() for v in line.strip().strip("|").split("|")] for line in group]
            tables.append(
                {
                    "doc_id": d["id"],
                    "table": sum(t["doc_id"] == d["id"] for t in tables),
                    "headers": rows[0],
                    "rows": rows[1:],
                    "raw": "".join(group),
                    "start": start,
                    "end": start + sum(len(x) for x in group),
                }
            )

        for line in d["text"].splitlines(keepends=True):
            if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
                if not group:
                    start = offset
                group.append(line)
            elif group:
                finish()
                group = []
            offset += len(line)
        if group:
            finish()
    return tables


def evidence_join(plan, tables, visible_ids):


    def table(which):
        item = plan[which]
        if item["doc_id"] not in visible_ids:
            raise ValueError("TABLE_NOT_RETRIEVED")
        candidates = [
            t for t in tables if t["doc_id"] == item["doc_id"] and t["table"] == item.get("table", 0)
        ]
        if len(candidates) != 1:
            raise ValueError("TABLE_ID_AMBIGUOUS")
        return candidates[0]

    left, right = table("left"), table("right")
    lk = left["headers"].index(plan["left"]["key"])
    rk = right["headers"].index(plan["right"]["key"])
    right_index = {}
    for row in right["rows"]:
        if row[rk] in right_index:
            raise ValueError("AMBIGUOUS_JOIN_KEY")
        right_index[row[rk]] = row
    selected = []
    lineage = []
    filters = plan.get("left_equals", {})
    for i, row in enumerate(left["rows"]):
        if any(row[left["headers"].index(k)] != str(v) for k, v in filters.items()):
            continue
        other = right_index.get(row[lk])
        if other is None:
            continue
        merged = {"left." + h: v for h, v in zip(left["headers"], row)} | {
            "right." + h: v for h, v in zip(right["headers"], other)
        }
        selected.append(merged)
        lineage.append(
            {"left_doc": left["doc_id"], "left_row": i + 1, "right_doc": right["doc_id"], "key": row[lk]}
        )
    result = {"rows": selected, "lineage": lineage, "operation": "source_bound_equi_join"}
    if plan.get("operation") == "sum_product":
        fields = plan["multiply"]
        if len(fields) != 2 or any(not re.fullmatch(r"(left|right)\.[^\n]+", f) for f in fields):
            raise ValueError("INVALID_PRODUCT_COLUMNS")
        total = Decimal(0)
        for row in selected:
            values = []
            for key in fields:
                try:
                    value = Decimal(row[key].replace(",", ""))
                except (InvalidOperation, KeyError) as exc:
                    raise ValueError("INVALID_NUMERIC_CELL") from exc
                if not value.is_finite():
                    raise ValueError("NONFINITE_NUMERIC_CELL")
                values.append(value)
            total += values[0] * values[1]
        result["value"] = str(total)
        result["unit"] = plan.get("unit")
    return result
