from __future__ import annotations
from copy import deepcopy
from hashlib import sha256
import time
from uuid import uuid4
from .agent_model import schema_instruction
from .context_budget import canonical
from .session_store import digest
from .agent_tool_view import project_tool_results
from .core import PlatformError
from .knowledge_policy import _visible_user_statements
from .planning_references import (bind_planning_schema, planning_bindings, project_references,
                                  resolve_planning_decision)


class KnowledgeModelCalls:
    def call(self, role, system, schema, validator, question=None, evidence_override=None, *, model=None):
        self.check_execution()
        selected_model = self.service.model if model is None else model
        if question is None:
            question = self.message
        if role in {"decision", "retrieval", "rerank"} and (
            not self.call_budget.can_explore(self.trace["calls"], max_attempts=
                getattr(getattr(selected_model, 'provider', None), 'max_attempts', 1))
        ):
            raise PlatformError("AGENT_BUDGET_EXHAUSTED", "Answer and verification budget reserved", 409)
        if (
            time.perf_counter() - self.started > self.execution_limits["max_turn_seconds"]
            or len(self.trace["calls"]) >= self.execution_limits["max_model_calls"]
        ):
            raise PlatformError("AGENT_BUDGET_EXHAUSTED", "Turn execution budget exhausted", 409)
        if self.service.gateway.authorize(self.actor, self.scope) != self.authority:
            raise PlatformError("SCOPE_CHANGED", "Authorization or manifest changed during turn", 409)
        evidence = self.service.gateway.revalidate(
            self.actor,
            self.scope,
            list(self.state["evidence"].values()) if evidence_override is None else evidence_override,
            self.state["source_policy"],
        )
        self.turn_lineage.update({item["citation_id"]: item for item in evidence})
        evidence = [
            {**item, "context_offset": self.state["evidence_offsets"].get(item["citation_id"], 0)}
            for item in evidence
        ]
        safe_history = [
            e
            for e in self.historical
            if e["kind"] == "user" or set(e["payload"].get("evidence_ids", [])) <= set(self.state["evidence"])
        ]
        projection_state = {
            key: deepcopy(self.state[key])
            for key in ("source_policy", "constraints", "topic", "corrections", "source_epoch")
        }
        projection_state["tool_results"] = project_tool_results(
            self.local_tool_results, budget_bytes=self.service.context_policy.effective_input_budget // 4
        )
        if question.startswith(self.message):
            projection_state["current_user_event"] = {
                "event_seq": self.seq,
                "question_start": 0,
                "question_end": len(self.message),
                "text_sha256": sha256(self.message.encode("utf-8")).hexdigest(),
            }
            if self.state['turn_number'] > 1:
                projection_state['current_user_event']['text'] = self.message
                system += '\nThe current user message is visible in state.current_user_event with its exact event_seq and text, and in the corresponding question slice. It may not yet appear in history. Treat it as a visible user statement when checking user_statements; absence from older history is not missing evidence. User statements establish only what the user said, never a document fact. Apply the latest explicit user correction when describing the user-provided value.'
        if self.state['turn_number'] > 1 and self.source_policy_updated:
            selected_ids = set(self.state['source_policy']['source_ids'])
            projection_state['source_policy_update'] = {
                'applied': True, 'origin': 'validated_request_control', 'source_epoch': self.state['source_epoch'],
                'selected_sources': [row for row in self.service.gateway.sources(self.actor, self.scope)
                                     if row['source_id'] in selected_ids]}
            system += '\nThe application has already validated and applied the source selection supplied with this turn through request controls. state.source_policy is the NEW active selection, not the previous lock. selected_sources identifies the chosen documents. LOCKED now restricts retrieval to this new selection; it does not mean the source change is still pending. Document titles are metadata, not evidence about their contents: a generic filename or a mismatch with the user\'s description does not establish that the requested fact is absent or that the selection is wrong. Search/read the selected originals before asking the user to select a different document. Continue the requested task using that selection; do not ask for the same authorization again or claim that the old lock still applies. User prose alone still cannot alter permissions.'
        if self.config.get("advanced_tools") and self.state.get("workflow_state"):
            projection_state["workflow_state"] = deepcopy(self.state["workflow_state"])
        planning = role in {'decision', 'retrieval'}
        if self.config.get("work_state") and planning:
            projection_state["work_state"] = deepcopy(self.state["work_state"])
        if self.config.get("canonical_ids"):
            system += "\nCitation protocol: claims.evidence_ids and quotes.evidence_id MUST use each visible evidence record's top-level citation_id (also its top-level evidence_id). The record's nested evidence_ids array contains raw parser span IDs and MUST NOT be cited as a claim reference. Copy the canonical identifier exactly."
        aliases = planning_bindings(set(self.state['evidence']) | set(self.state['evidence_catalog'])) if planning else {}
        wire_schema = bind_planning_schema(schema, aliases) if planning else schema
        if planning:
            system += '\nPlanner references are call-local short handles (E1, E2, ...). Use only the current citation_ids enum; never copy or combine raw source/span identifiers. Handles are resolved by the application before tools execute. They do not carry across calls. answer/clarify need no citation_ids; the later answer role selects its own citations.'
        context = self.service.context
        prepared = context.prepare(
            role,
            system + "\n" + schema_instruction(wire_schema),
            project_references(projection_state, aliases) if planning else projection_state,
            project_references(safe_history, aliases) if planning else safe_history,
            project_references(evidence, aliases) if planning else evidence,
            question,
            self.state["checkpoints"].get(role) if self.config["compact"] and not planning else None,
            selection_query=self.state.get("work_state", {}).get("resolved_query")
            if self.config.get("work_state") and planning
            else self.message,
        )
        self.model_evidence[role] = {
            item["citation_id"]: item.get("text", "") for item in prepared.projection["evidence"]
        }
        self.model_user_statements[role] = _visible_user_statements(prepared.projection)
        history_compacted = bool(prepared.projection.get("omitted_event_refs"))
        if history_compacted and (not self.config["compact"]):
            raise PlatformError(
                "CONTEXT_UNFIT", "History exceeds the noncompacting configuration budget", 422
            )
        if history_compacted:
            checkpoint = prepared.projection.get("checkpoint")
            if checkpoint:
                self.state["checkpoints"][role] = checkpoint
                self.trace["compactions"].append(
                    {
                        "agent": role,
                        "covered_from": checkpoint["covered_from"],
                        "covered_through": checkpoint["covered_through"],
                    }
                )
                self.event("checkpoint", checkpoint, agent=role)
        self.trace["budget"] = {
            "input_tokens": max(self.trace["budget"]["input_tokens"], prepared.estimate["tokens"]),
            "limit": prepared.budget["input_limit"],
            "output_reserved": self.service.context_policy.output_reserve,
            "counting_method": "conservative_utf8_estimate",
            "strict_tokens": False,
        }
        call_id = uuid4().hex
        self.event(
            "model_started",
            {
                "call_id": call_id,
                "role": role,
                "prompt_hash": digest(prepared.messages),
                "budget": self.trace["budget"],
            },
            agent=role,
        )
        try:
            if self.service.capture is not None:
                self.service.capture(
                    {
                        "session_id": self.session_id,
                        "turn_id": self.request_id,
                        "call_id": call_id,
                        "role": role,
                        "prepared": prepared,
                        "evidence": evidence,
                    }
                )
            self.check_execution()
            envelope = selected_model.complete(
                prepared.messages,
                role=role,
                schema=wire_schema,
                call_context={
                    "run_id": self.session_id,
                    "session_id": self.session_id,
                    "turn_id": self.request_id,
                    "call_id": call_id,
                    "agent_id": role,
                    "max_physical_calls": self.execution_limits["max_model_calls"],
                    "remaining_seconds": self.execution_limits["max_turn_seconds"]
                    - (time.perf_counter() - self.started),
                },
                output_limit=self.service.context_policy.output_reserve,
                validator=(lambda value: validator(resolve_planning_decision(value, aliases))) if planning else validator,
            )
        except Exception as exc:
            failed = {
                **getattr(exc, "call_record", {}),
                "call_id": call_id,
                "role": role,
                "code": getattr(exc, "code", type(exc).__name__),
            }
            self.trace["calls"].append(failed)
            self.event("model_failed", failed, agent=role)
            reason = failed.get("validator_reason")
            if (
                (self.config.get("format_repair") or self.config.get("robust_tools"))
                and (not self.format_repair_used)
                and (failed.get("code") == "INVALID_AGENT_OUTPUT")
                and (failed.get("finish_reason") == "stop")
                and (reason in {"DUPLICATE_JSON_KEY", "INVALID_JSON_SYNTAX", "AGENT_SCHEMA_MISMATCH"})
            ):
                self.format_repair_used = True
                repair = {
                    "failed_call_id": call_id,
                    "reason": reason,
                    "attempts": 1,
                    "kind": "new_generation_after_received_invalid_format",
                }
                self.trace.setdefault("format_repairs", []).append(repair)
                repair_system = (
                    system
                    + "\nThe previous response was received but its JSON format was invalid ("
                    + reason
                    + "). Return exactly one valid JSON object. Every object key must appear once and every value must match its schema. Omit unused optional fields; do not use zero for an unused event_seq. Do not change source permissions, evidence or the requested task. Repair only the current role: "
                    + role
                    + ". Allowed top-level keys: "
                    + ", ".join(schema.get("properties", {}))
                    + "."
                )
                if role in {"decision", "retrieval"}:
                    repair_system += ' This role only routes the next action. action="answer" means the evidence is ready for a later answer role; it does not ask you to write that answer now. Return the planner intent/action and applicable citation_ids. Do not emit answer, claims, quotes, evidence_ids or final-answer prose. Preserve the current planner schema rather than switching to an answer schema.'
                if failed.get('validator_issue'):
                    repair_system += '\nValidation location and constraint: ' + canonical(failed['validator_issue'])
                    repair_system += '. Fix this specific constraint. Group related subquestions when the list exceeds its limit, preserving the full task scope. Return only required fields and fields needed for the chosen action; omit other optional fields.'
                with self.service.tool_context(
                    {
                        "session_id": self.session_id,
                        "turn_id": self.request_id,
                        "repair_of_call_id": call_id,
                        "repair_reason": reason,
                    }
                ):
                    try:
                        return self.call(role, repair_system, schema, validator, question, evidence_override,
                                         model=selected_model)
                    finally:
                        repair["new_call_id"] = self.trace["calls"][-1]["call_id"]
            raise
        record = envelope.to_dict()
        value = resolve_planning_decision(envelope.value, aliases) if planning else envelope.value
        record['value'] = value
        record.update(role=role, call_id=call_id, context_estimate=prepared.estimate)
        self.trace["calls"].append({key: value for key, value in record.items() if key != "value"})
        self.event("model_completed", record, agent=role)
        return value
