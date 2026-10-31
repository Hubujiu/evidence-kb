from __future__ import annotations
from copy import deepcopy
import time
from .agent_tool_registry import TOOLS, bind_decision_evidence, decision_schema as planning_schema, tool_arguments
from .session_store import digest
from .agent_work_state import record_decision, record_receipt
from .core import PlatformError
from .knowledge_policy import DECISION_SYSTEM
from .knowledge_plugin_tools import KnowledgeToolContext as ToolContext


class KnowledgePlanning:
    def invoke_tool(self, action, args, function, *, empty=None):
        identity = digest({"action": action, "args": args})
        if not self.config.get("robust_tools"):
            return function()
        if self.service.gateway.authorize(self.actor, self.scope) != self.authority:
            raise PlatformError("SCOPE_CHANGED", "Authority changed before tool execution", 409)
        with self.service.tool_context(
            {
                "session_id": self.session_id,
                "turn_id": self.request_id,
                "tool": action,
                "tool_identity": identity,
            }
        ):
            outcome = self.service.tool_executor.execute(
                self.session_id + ":" + self.request_id + ":" + identity,
                action,
                args,
                function,
                read_only=self.service.plugins[action].retry_safe
                if action in self.service.plugins
                else TOOLS[action].retry_safe,
                is_empty=empty,
                deadline_seconds=self.execution_limits["max_turn_seconds"]
                - (time.perf_counter() - self.started),
            )
        self.trace.setdefault("tool_execution", []).append(
            {key: value for key, value in outcome.to_dict().items() if key != "value"}
        )
        if outcome.status not in {"COMPLETE", "EMPTY"}:
            raise PlatformError(
                outcome.error_code or "TOOL_EXECUTION_FAILED", "Tool execution did not complete", 503
            )
        return outcome.value

    def validate_decision(self, value):
        action = value["action"]
        if action in self.service.plugins:
            self.service.plugins.validate_arguments(action, value.get("plugin_args", {}))
        if action in {"search", "search_pages", "search_semantic_pages", "search_pipeline", "search_tables"}:
            if not value.get("query", "").strip():
                raise ValueError("INVALID_SEARCH_QUERY")
        if action in {"read", "table", "table_join", "workflow_next", "navigate_references"}:
            refs = value.get("citation_ids", [])
            if not refs or any(ref not in self.state["evidence"] and ref not in self.state["evidence_catalog"]
                               for ref in refs):
                raise ValueError("UNKNOWN_EVIDENCE_REFERENCE")
            if action in {"table", "workflow_next", "navigate_references"} and len(refs) > (
                    3 if action == "navigate_references" else 1):
                raise ValueError("UNKNOWN_EVIDENCE_REFERENCE")
            if action == "table_join" and (len(refs) != 2 or refs[0] == refs[1]):
                raise ValueError("UNKNOWN_EVIDENCE_REFERENCE")
        if action == "table_join":
            if any(not value.get(field, "").strip() for field in ("left_key", "right_key")):
                raise ValueError("INVALID_JOIN_KEY")
            if value.get("join_how", "inner") == "":
                raise ValueError("INVALID_JOIN_MODE")
        if action == "table" and value.get("table_operation", "rows") == "":
            raise ValueError("INVALID_TABLE_OPERATION")
        if action == "calculator" and "expression" not in value:
            raise ValueError("INVALID_CALCULATION")

    def plan_node(self, flow):
        step, decision = (flow["step"], flow["decision"])
        if (
            not self.fast_greeting
            and (not self.fast_calculation)
            and self.trace["calls"]
            and (not self.call_budget.can_explore(self.trace["calls"]))
        ):
            self.trace["stop_reason"] = "exploration_budget"
            return {"decision": {**decision, "action": "answer"}}
        if self.fast_greeting:
            decision = {"intent": "general", "action": "answer"}
        elif self.fast_calculation:
            decision = {"intent": "calculate", "action": "calculator", "expression": self.fast_calculation}
        elif self.config["route"]:
            planning_system = DECISION_SYSTEM + "\n" + self.service.plugins.instructions()
            decision_schema = self.service.plugins.schema(
                planning_schema(self.config, self.advanced, self.semantic_enabled)
            )
            decision_schema = bind_decision_evidence(
                decision_schema, set(self.state['evidence']) | set(self.state['evidence_catalog']))
            planning_system += '\nOnly actions admitted by the current schema are executable. Before using a source-dependent tool, retrieve its evidence first. Omit optional fields unrelated to the chosen action; a read needs citation_ids, not empty table, join or calculator arguments.'
            if self.advanced or self.config.get("explicit_completion"):
                planning_system += (
                    "\nThis is a selected-knowledge-base assistant. Use out_of_domain+answer for a clearly unrelated request; use clarify for missing essential user parameters, not for a temporary tool error. Never infer that a fact does not exist from TopK misses. For all/count/none requests, distinguish complete scope enumeration from partial search. When sources disagree preserve the conflict and effective dates, never select authority by similarity. Available additional actions: "
                    + ", ".join(sorted(self.advanced))
                    + ". search_pipeline(query) performs actual child hybrid retrieval, same-pool reranking and parent expansion. When search_pipeline is available, prefer it for original-document questions; use search_tables for typed-table tasks and search as the lexical fallback. search_tables(query) finds typed tables. table(citation_ids=[one table],table_operation,column,filters) performs deterministic rows/count/sum/min/max. Do not guess column names; read the source first. table_join(citation_ids=[left table,right table],left_key,right_key,join_how) joins two typed original tables by exact equal keys and preserves both row lineages. navigate_references(citation_ids=[up to three]) follows explicit source titles and sections. workflow_next(citation_ids=[one typed workflow table]) uses only explicit user workflow_state; you cannot invent the current step or branch facts. Inspect tool result completion and missing_info."
                )
            if self.config.get("page_navigation"):
                planning_system += "\nsearch_pages(query) searches complete authorized original PDF pages using a separate page index. Use it for cross-chapter themes, relationships, qualifications or missing surrounding context after block search; search(query) remains the precise block/table-context channel. Both return original citations. Use separate queries for missing subquestions, and read full supporting context before claiming a theme is complete. A page hit is not proof of the answer."
            if self.semantic_enabled:
                planning_system += "\nsearch_semantic_pages(query) finds authorized original pages by semantic similarity through an independently built index. It complements lexical search and search_pages when wording differs or a required part remains unsupported. Keep the evidence already collected, use focused queries for missing subquestions, and compare both channels for broad themes before declaring every part supported. Similarity scores and retrieved pages do not themselves prove an answer."
            if self.config.get("work_state"):
                planning_system += "\nMaintain the explicit work_state. Resolve pronouns and corrections in query; preserve entities, dates, negation and source constraints. List required subquestions and missing_information. On later steps inspect actual tool receipts and evidence; declare sufficiency sufficient only when every required part is supported. Search a missing part or read an original window when insufficient, and stop if no new evidence is available. Use read_history for prior user/assistant/tool events in this session; optional event_seq and offset expand an earlier truncated event. Never treat assistant history as document proof."
            decision = self.call(
                "decision" if step == 0 else "retrieval",
                planning_system,
                decision_schema,
                self.validate_decision,
            )
        if self.config.get("work_state"):
            record_decision(self.state["work_state"], decision)
        if step == 0:
            self.trace["route"] = decision["intent"]
            self.event("intent", decision, agent="decision")
            if decision["intent"] == "correction":
                self.state["corrections"].append({"text": self.message, "event_seq": self.seq})
                self.state["corrections"] = self.state["corrections"][-10:]
                self.state["last_answer"] = None
            if decision.get("topic_changed") and self.state["source_policy"]["lifetime"] == "topic":
                decision = {
                    **decision,
                    "action": "clarify",
                    "clarification": "话题已变化。请在来源设置中确认是否继续使用当前锁定来源。",
                }
        return {"decision": decision}

    def tool_node(self, flow):
        step, decision = (flow["step"], flow["decision"])
        action = decision["action"]
        args = (
            self.service.plugins.arguments(decision)
            if action in self.service.plugins
            else tool_arguments(action, decision, self.message, self.config)
        )
        identity = digest({"action": action, "args": args})
        if identity in self.seen_actions:
            self.trace["stop_reason"] = "repeated_action"
            return {"decision": decision, "step": step + 1, "halted": True}
        self.seen_actions.add(identity)
        group = self.request_id + ":tool:" + str(step)
        tool_start = time.perf_counter()
        self.event(
            "tool_call",
            {"call_id": group, "name": action, "args": args},
            group,
            "retrieval" if action != "calculator" else "tools",
        )
        tool_lineage = dict(self.state["evidence"])
        if action == "search_pipeline" and (not self.call_budget.can_explore(self.trace["calls"])):
            self.builtin_tools.retrieval_config = {**self.service.retrieval_config, "rerank": False}
            self.trace["rerank_skipped"] = "answer_and_verification_budget_reserved"
        try:
            if action in self.service.plugins:
                plugin_context = ToolContext(
                    actor=self.actor,
                    scope_id=self.scope,
                    source_policy=deepcopy(self.state["source_policy"]),
                    session_id=self.session_id,
                    turn_id=self.request_id,
                    call_id=group,
                )
                plugin_result = self.invoke_tool(
                    action, args, lambda: self.service.plugins.execute(action, plugin_context, args)
                )
                items = self.service.gateway.revalidate(
                    self.actor, self.scope, plugin_result.evidence, self.state["source_policy"]
                )
                self.save_evidence(items)
                self.seen_turn_evidence.update((item["citation_id"] for item in items))
                result = {**plugin_result.data, "evidence_ids": [item["citation_id"] for item in items]}
            else:
                result = self.builtin_tools.execute(action, args, group)
            output = {
                "name": action,
                "args": args,
                "result": result,
                "status": "ok",
                "seconds": time.perf_counter() - tool_start,
            }
        except Exception as exc:
            output = {
                "name": action,
                "args": args,
                "result": None,
                "status": "error",
                "error": getattr(exc, "code", type(exc).__name__),
                "seconds": time.perf_counter() - tool_start,
            }
        artifact = self.service.store.artifact(
            self.actor,
            self.session_id,
            self.token,
            {"tool": output, "evidence": list({**tool_lineage, **self.state["evidence"]}.values())},
        )
        self.event(
            "tool_result",
            {"call_id": group, **output, **artifact, "source_epoch": self.state["source_epoch"]},
            group,
            "retrieval" if action != "calculator" else "tools",
        )
        self.trace["tool_calls"].append(output)
        self.local_tool_results.append({**output, "artifact_id": artifact["artifact_id"]})
        if self.config.get("work_state"):
            record_receipt(self.state["work_state"], output)
        if output["status"] == "error":
            self.trace["stop_reason"] = "tool_failure"
            return {"decision": decision, "step": step + 1, "halted": True}
        if action == "workflow_next" and result.get("status") in {"clarify", "ambiguous"}:
            decision = {
                **decision,
                "action": "clarify",
                "clarification": "请补充或核对当前流程步骤及分支条件："
                + "；".join(result.get("missing_info", []) or ["当前流程存在多个可行分支，不能自动选择。"]),
            }
            self.trace["stop_reason"] = "workflow_state_required"
            return {"decision": decision, "step": step + 1, "halted": True}
        if self.fast_calculation:
            return {"decision": decision, "step": step + 1, "halted": True}
        return {"decision": decision, "step": step + 1, "halted": False}
