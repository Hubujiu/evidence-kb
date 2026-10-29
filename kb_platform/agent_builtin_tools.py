"""Built-in tool adapters. The service owns evidence persistence and turn transitions."""

from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Callable

from .agent_tools import bounded_calculate
from .agent_tool_registry import TOOLS


@dataclass
class BuiltinTools:
    actor: object
    scope: str
    session_id: str
    request_id: str
    state: dict
    config: dict
    max_active_evidence: int
    trace: dict
    seen_turn_evidence: set[str]
    invoke_tool: Callable
    save_evidence: Callable
    turn_gateway: object
    seq: int
    gateway: object
    semantic_pages: object
    retrieval_config: dict
    store: object

    def execute(self, action, args, group):
        handler = getattr(self, TOOLS[action].handler)
        return handler(action, args, group)

    def search(self, action, args, group):
        excluded = self.seen_turn_evidence
        if self.config.get("exclude_active_evidence_only"):
            excluded = self.seen_turn_evidence.intersection(self.state["evidence"])
        search_options = (
            {"exclude_citation_ids": tuple(excluded)} if self.config.get("novelty_search") else {}
        )
        if action == "search_semantic_pages":
            items = self.invoke_tool(
                action,
                args,
                lambda: self.semantic_pages.query(
                    self.actor,
                    self.scope,
                    args["query"],
                    self.state["source_policy"],
                    limit=5,
                    call_context={
                        "session_id": self.session_id,
                        "turn_id": self.request_id,
                        "tool_call_id": group,
                    },
                    **search_options,
                ),
            )
        elif action == "search" and hasattr(self.turn_gateway, "search_pipeline"):
            search_result = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.search_pipeline(
                    self.actor,
                    self.scope,
                    args["query"],
                    self.state["source_policy"],
                    {"channels": ["bm25"], "rerank": False, "max_expanded_units": self.max_active_evidence},
                    **search_options,
                ),
                empty=lambda value: not value["hits"],
            )
            items = search_result["hits"]
            self.trace.setdefault("retrieval_stages", []).append(
                {"tool_call_id": group, "tool": action, **search_result["trace"]}
            )
        else:
            search_tool = self.gateway.search_pages if action == "search_pages" else self.gateway.search
            items = self.invoke_tool(
                action,
                args,
                lambda: search_tool(
                    self.actor,
                    self.scope,
                    args["query"],
                    self.state["source_policy"],
                    limit=5,
                    **search_options,
                ),
            )
        self.save_evidence(items)
        self.seen_turn_evidence.update((x["citation_id"] for x in items))
        result = {
            "evidence_ids": [x["citation_id"] for x in items],
            "doc_ids": [x.get("doc_id", x["source_id"]) for x in items],
        }
        if action == "search" and hasattr(self.turn_gateway, "search_pipeline"):
            result["evidence_delivery"] = [
                row for row in search_result["trace"].get("expansion", []) if row.get("status") != "COMPLETE"
            ]
        if action == "search_semantic_pages" and getattr(items, "receipt", None):
            result["semantic_query"] = deepcopy(items.receipt)
        return result

    def advanced(self, action, args, group):
        if action == "search_pipeline":
            excluded = self.seen_turn_evidence
            if self.config.get("exclude_active_evidence_only"):
                excluded = self.seen_turn_evidence.intersection(self.state["evidence"])
            pipeline_options = (
                {"exclude_citation_ids": tuple(excluded)} if self.config.get("novelty_search") else {}
            )
            retrieval_config = {
                **self.retrieval_config,
                "max_expanded_units": min(
                    self.retrieval_config.get("max_expanded_units", 50), self.max_active_evidence
                ),
            }
            result = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.search_pipeline(
                    self.actor,
                    self.scope,
                    args["query"],
                    self.state["source_policy"],
                    retrieval_config,
                    **pipeline_options,
                ),
                empty=lambda value: not value["hits"],
            )
            items = result.pop("hits")
        elif action == "search_tables":
            items = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.search_tables(
                    self.actor, self.scope, args["query"], self.state["source_policy"], limit=5
                ),
            )
            result = {}
        elif action == "navigate_references":
            result = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.navigate_references(
                    self.actor, self.scope, args["citation_ids"], self.state["source_policy"]
                ),
                empty=lambda value: not value["hits"],
            )
            items = result.pop("hits")
        elif action == "table":
            result = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.table(
                    self.actor,
                    self.scope,
                    args["citation_ids"][0],
                    self.state["source_policy"],
                    args["operation"],
                    args["column"],
                    args["filters"],
                ),
            )
            items = [result.pop("evidence")]
        elif action == "table_join":
            result = self.invoke_tool(
                action,
                args,
                lambda: self.turn_gateway.table_join(
                    self.actor,
                    self.scope,
                    args["citation_ids"],
                    self.state["source_policy"],
                    left_key=args["left_key"],
                    right_key=args["right_key"],
                    how=args["how"],
                ),
            )
            items = result.pop("evidence")
        else:
            explicit = self.state.get("workflow_state", {})
            target = args["citation_ids"][0]
            if explicit.get("bound_citation_id") not in (None, target):
                originals = self.turn_gateway.read(
                    self.actor, self.scope, [target], self.state["source_policy"]
                )
                result = {
                    "status": "clarify",
                    "next_steps": [],
                    "missing_info": ["已切换流程，请重新确认该流程的当前步骤与条件。"],
                    "evidence": originals[0],
                }
            else:
                if explicit:
                    explicit["bound_citation_id"] = target
                result = self.invoke_tool(
                    action,
                    args,
                    lambda: self.turn_gateway.workflow_next(
                        self.actor,
                        self.scope,
                        target,
                        self.state["source_policy"],
                        current_step=explicit.get("current_step"),
                        facts=explicit.get("facts"),
                    ),
                )
            items = [result.pop("evidence")]
        if "trace" in result:
            result["evidence_delivery"] = [
                row for row in result["trace"].get("expansion", []) if row.get("status") != "COMPLETE"
            ]
            self.trace.setdefault("retrieval_stages", []).append(
                {"tool_call_id": group, "tool": action, **result.pop("trace")}
            )
        if "fragments" in result:
            fragments = result.pop("fragments")
            self.trace.setdefault("navigation_spans", []).extend(fragments)
            result["resolved_fragment_count"] = len(fragments)
        self.save_evidence(items)
        self.seen_turn_evidence.update((item["citation_id"] for item in items))
        result["evidence_ids"] = [item["citation_id"] for item in items]
        if action == "workflow_next" and result.get("status") == "clarify":
            self.state.setdefault("work_state", {})["missing_information"] = result.get("missing_info", [])
        return result

    def read(self, action, args, group):
        items = self.invoke_tool(
            action,
            args,
            lambda: self.gateway.read(
                self.actor, self.scope, args["citation_ids"], self.state["source_policy"]
            ),
        )
        self.save_evidence(items)
        self.seen_turn_evidence.update((x["citation_id"] for x in items))
        for item in items:
            self.state["evidence_offsets"][item["citation_id"]] = min(
                args["offset"], max(0, len(item["text"]) - 1)
            )
        result = {"evidence_ids": [x["citation_id"] for x in items]}
        return result

    def history(self, action, args, group):
        from .agent_history import restore_events

        restored = restore_events(
            self.store.events(self.actor, self.session_id), args, self.state, self.seq, self.config
        )
        result = {"events": restored}
        return result

    def calculate(self, action, args, group):
        result = {"expression": args["expression"], "value": str(bounded_calculate(args["expression"]))}
        return result
