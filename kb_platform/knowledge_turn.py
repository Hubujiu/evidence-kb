from __future__ import annotations
from copy import copy, deepcopy
import math
import time
from .agent_protocol import ANSWER_SCHEMA, VERDICT_SCHEMA, object_schema
from .agent_tool_registry import advanced_tools
from .session_store import digest
from .agent_builtin_tools import BuiltinTools
from .agent_call_budget import CallBudget
from .agent_work_state import begin_work_state, finish_work_state
from .core import PlatformError
from .knowledge_policy import VERSION, CONFIGS, SYSTEM, CLAIM_FIDELITY, _active_evidence_limit, source_policy
from .knowledge_workflow import PROTOCOL as GRAPH_PROTOCOL
from .agent_engine import current_execution

from .knowledge_model import KnowledgeModelCalls
from .knowledge_planning import KnowledgePlanning
from .knowledge_answer import KnowledgeAnswer


class KnowledgeTurn(KnowledgeModelCalls, KnowledgePlanning, KnowledgeAnswer):
    def __init__(self, service):
        self.service = service

    def run(
        self,
        actor,
        session_id,
        *,
        message,
        request_id,
        expected_revision,
        policy=None,
        constraints=None,
        workflow_state=None,
    ):
        self.actor = actor
        self.session_id = session_id
        self.message = message
        self.request_id = request_id
        if not isinstance(self.message, str) or not self.message.strip() or len(self.message) > 20000:
            raise PlatformError("INVALID_MESSAGE", "Message must contain 1 to 20000 characters", 422)
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 100:
            raise PlatformError("INVALID_REQUEST_ID", "Request id required", 422)
        if constraints is not None and (
            not isinstance(constraints, list)
            or len(constraints) > 20
            or any((not isinstance(x, str) or not 1 <= len(x) <= 500 for x in constraints))
        ):
            raise PlatformError(
                "INVALID_CONSTRAINTS", "Constraints must be short explicit user statements", 422
            )
        if workflow_state is not None:
            if (
                not isinstance(workflow_state, dict)
                or set(workflow_state) - {"current_step", "facts"}
                or (not isinstance(workflow_state.get("current_step", ""), str))
                or (len(workflow_state.get("current_step", "")) > 200)
                or (not isinstance(workflow_state.get("facts", {}), dict))
                or (len(workflow_state.get("facts", {})) > 20)
                or any(
                    (
                        not isinstance(key, str)
                        or not key
                        or len(key) > 100
                        or (type(value) not in (str, bool, int, float))
                        or (len(str(value)) > 500)
                        or (type(value) is float and (not math.isfinite(value)))
                        for key, value in workflow_state.get("facts", {}).items()
                    )
                )
            ):
                raise PlatformError("INVALID_WORKFLOW_STATE", "Supply explicit user workflow state", 422)
        current = self.service.store.load(self.actor, self.session_id)
        self.authority = self.service.gateway.authorize(self.actor, current["scope_id"])
        self.semantic_enabled = bool(
            CONFIGS[current["state"]["config_name"]].get("semantic_pages")
            and self.service._semantic_available(self.actor, current["scope_id"])
        )
        self.max_active_evidence = _active_evidence_limit(CONFIGS[current["state"]["config_name"]])
        validated_policy = (
            self.service._pin(self.actor, current["scope_id"], source_policy(policy))
            if policy is not None
            else None
        )
        request_content = {
            "message": self.message,
            "policy": validated_policy,
            "constraints": constraints,
            "runtime_protocol": self.service.runtime.protocol,
        }
        if self.service.plugins.names():
            request_content["plugins"] = self.service.plugins.manifest()
        if workflow_state is not None:
            request_content["workflow_state"] = workflow_state
        begin = self.service.store.begin(
            self.actor, self.session_id, self.request_id, expected_revision, digest(request_content)
        )
        if "cached" in begin:
            self.service.gateway.revalidate(
                self.actor,
                current["scope_id"],
                begin["cached"].get("evidence", []),
                current["state"]["source_policy"],
            )
            return begin["cached"]
        self.token = begin["token"]
        session = begin["session"]
        self.state = deepcopy(session["state"])
        self.config = CONFIGS[self.state["config_name"]]
        self.execution_limits = {
            "max_model_calls": self.config["max_model_calls"],
            "max_turn_seconds": self.config["max_turn_seconds"],
            "max_active_evidence": self.max_active_evidence,
        }
        inherited = current_execution()
        if inherited is not None:
            self.execution_limits["max_turn_seconds"] = min(
                self.execution_limits["max_turn_seconds"], inherited.remaining_seconds
            )
        self.call_budget = CallBudget(
            self.execution_limits["max_model_calls"],
            bool(self.config["verify"]),
            getattr(self.service.model, "max_attempts",
                    getattr(getattr(self.service.model, "provider", None), "max_attempts", 1)),
        )
        self.scope = session["scope_id"]
        self.started = time.perf_counter()
        self.turn_lineage = {}
        self.trace = {
            "version": VERSION,
            "config_name": self.state["config_name"],
            "config": self.config,
            "authority": deepcopy(self.authority),
            "execution_limits": self.execution_limits,
            "calls": [],
            "tool_calls": [],
            "route": None,
            "compactions": [],
            "source_policy": None,
            "stop_reason": None,
            "budget": {
                "input_tokens": 0,
                "limit": self.service.context_policy.effective_input_budget,
                "output_reserved": self.service.context_policy.output_reserve,
                "counting_method": "conservative_utf8_estimate",
                "strict_tokens": False,
            },
        }
        try:
            old_policy = self.state["source_policy"]
            if validated_policy is not None:
                self.state["source_policy"] = validated_policy
            elif self.state["turn_number"] and old_policy["lifetime"] == "turn":
                self.state["source_policy"] = source_policy()
            policy_changed = old_policy != self.state["source_policy"]
            auth_changed = self.state["authority"] is not None and self.state["authority"] != self.authority
            if policy_changed or auth_changed:
                self.state.pop("workflow_state", None)
                pinned = []
                if not policy_changed and self.state["source_policy"]["version_policy"] == "pinned":
                    try:
                        pinned = self.service.gateway.revalidate(
                            self.actor,
                            self.scope,
                            list(self.state["evidence"].values()),
                            self.state["source_policy"],
                        )
                    except PlatformError:
                        pass
                self.state["source_epoch"] += 1
                self.state["checkpoints"] = {}
                self.state["last_answer"] = None
                self.state["evidence"] = {x["citation_id"]: x for x in pinned}
                self.event(
                    "context_invalidated",
                    {
                        "policy_changed": policy_changed,
                        "authority_changed": auth_changed,
                        "source_epoch": self.state["source_epoch"],
                    },
                )
            self.state["authority"] = self.authority
            if constraints is not None:
                self.state["constraints"] = [
                    {"text": x, "origin_request_id": self.request_id} for x in constraints
                ]
            if workflow_state is not None:
                self.state["workflow_state"] = {
                    **deepcopy(workflow_state),
                    "origin_request_id": self.request_id,
                    "origin": "explicit_user_state",
                    "source_epoch": self.state["source_epoch"],
                }
            if self.state["evidence"]:
                try:
                    self.state["evidence"] = {
                        x["citation_id"]: {**x, "evidence_id": x["citation_id"]}
                        for x in self.service.gateway.revalidate(
                            self.actor,
                            self.scope,
                            list(self.state["evidence"].values()),
                            self.state["source_policy"],
                        )
                    }
                except PlatformError:
                    self.state["evidence"], self.state["checkpoints"], self.state["last_answer"] = (
                        {},
                        {},
                        None,
                    )
                    self.state["source_epoch"] += 1
                    auth_changed = True
                    self.event("context_invalidated", {"reason": "evidence_revalidation_failed"})
            if not self.config["reuse"]:
                self.state["evidence"], self.state["last_answer"] = ({}, None)
            self.historical = self.service.store.events(self.actor, self.session_id)
            if auth_changed or policy_changed:
                self.historical = []
            else:
                self.historical = [
                    e
                    for e in self.historical
                    if e["kind"] in {"user", "assistant"}
                    and e["payload"].get("source_epoch") == self.state["source_epoch"]
                ]
            if not self.config["reuse"]:
                self.historical = [
                    e for e in self.historical if e["kind"] == "user" or not e["payload"].get("evidence_ids")
                ]
            self.seq = self.event("user", {"text": self.message, "source_epoch": self.state["source_epoch"]})
            self.trace["source_policy"] = deepcopy(self.state["source_policy"])
            self.state["turn_number"] += 1
            if self.config.get("work_state"):
                self.state["work_state"] = begin_work_state(
                    self.state.get("work_state"),
                    self.message,
                    self.state["constraints"],
                    self.state["source_epoch"],
                )
            self.local_tool_results = []
            self.seen_actions = set()
            self.seen_turn_evidence = set()
            self.model_evidence = {}
            self.model_user_statements = {}
            self.format_repair_used = False
            self.advanced = advanced_tools(self.config, self.service.gateway)
            turn_gateway = copy(self.service.gateway)

            class BudgetedReranker:
                provider_id = getattr(self.service.gateway, 'provider_id', 'magpie_luna')
                manifest = {
                    "protocol": "agent-same-candidate-rerank/1",
                    "model": getattr(self.service.model, 'manifest',
                                     getattr(getattr(self.service.model, "provider", None), "manifest", {})),
                }

                def rank(inner, query, candidates):
                    ids = [item["citation_id"] for item in candidates]
                    schema = object_schema(
                        {
                            "ranking": {
                                "type": "array",
                                "minItems": len(ids),
                                "maxItems": len(ids),
                                "items": object_schema(
                                    {
                                        "citation_id": {"type": "string", "enum": ids},
                                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                                    }
                                ),
                            }
                        }
                    )

                    def validate(value):
                        if (
                            not isinstance(value, dict)
                            or not isinstance(value.get("ranking"), list)
                            or sorted((item.get("citation_id", "") for item in value["ranking"]))
                            != sorted(ids)
                        ):
                            raise ValueError("INVALID_RERANK_OUTPUT")

                    ranked = self.call(
                        "rerank",
                        SYSTEM
                        + "\nScore every provided candidate for relevance to the query. Return exactly the supplied citation identities; do not answer the question or invent evidence.",
                        schema,
                        validate,
                        question=query,
                        evidence_override=candidates,
                    )
                    return ranked["ranking"]

            turn_gateway.pipeline_reranker = BudgetedReranker()
            if "search_pipeline" in self.advanced and hasattr(turn_gateway, "pipeline_capabilities"):
                admitted = turn_gateway.pipeline_capabilities(
                    self.actor, self.scope, self.state["source_policy"]
                )
                self.trace["pipeline_capabilities"] = admitted
                requested_channels = set(self.service.retrieval_config.get("channels", ["bm25"]))
                if not requested_channels <= set(admitted["channels"]) or (
                    self.service.retrieval_config.get("rerank")
                    and (not admitted["rerank_all_sources_admitted"])
                ):
                    self.advanced.remove("search_pipeline")
            decision = {"intent": "knowledge", "action": "search", "query": self.message}
            self.fast_greeting = self.config.get(
                "explicit_completion"
            ) and self.message.strip().casefold() in {"你好", "您好", "hello", "hi", "嗨", "谢谢", "谢谢你"}
            self.fast_calculation = None
            if self.config.get("explicit_completion"):
                import re

                literal = re.fullmatch(
                    "(?:计算|calculate)\\s*[:：]?\\s*([0-9+*/().% _\\-]+)", self.message.strip(), re.I
                )
                if literal:
                    self.fast_calculation = literal.group(1)
            self.evidence = []
            self.fidelity = CLAIM_FIDELITY if self.config.get("claim_fidelity") else ""
            self.answer_schema = ANSWER_SCHEMA
            if self.config.get("explicit_completion"):
                self.answer_schema = deepcopy(ANSWER_SCHEMA)
                self.answer_schema["required"] += ["completion", "unresolved"]
                self.fidelity += "\nReport completion explicitly: complete only when all requested parts are supported; partial when supported parts can be answered but others remain unresolved; insufficient when no requested fact can be established; conflict when applicable source claims disagree. List unresolved subquestions; missing years/entities require clarification. Do not call a TopK list exhaustive or infer nonexistence from missed retrieval. A tool failure or truncated output never proves no answer exists. Tool calculations support arithmetic only, and workflow_state records the user assertion, not an independently verified fact. Preserve these origins in the answer."
            self.candidate_to_repair = None
            self.citation_repair_used = False
            self.verdict_schema = VERDICT_SCHEMA
            if self.config.get("explicit_completion"):
                self.verdict_schema = deepcopy(VERDICT_SCHEMA)
                self.verdict_schema["properties"].update(
                    task_complete={"type": "boolean"},
                    missing_information={
                        "type": "array",
                        "maxItems": 12,
                        "items": {"type": "string", "maxLength": 500},
                    },
                )
                self.verdict_schema["required"] += ["task_complete", "missing_information"]
            self.builtin_tools = BuiltinTools(
                actor=self.actor,
                scope=self.scope,
                session_id=self.session_id,
                request_id=self.request_id,
                state=self.state,
                config=self.config,
                max_active_evidence=self.max_active_evidence,
                trace=self.trace,
                seen_turn_evidence=self.seen_turn_evidence,
                invoke_tool=self.invoke_tool,
                save_evidence=self.save_evidence,
                turn_gateway=turn_gateway,
                seq=self.seq,
                gateway=self.service.gateway,
                semantic_pages=self.service.semantic_pages,
                retrieval_config=self.service.retrieval_config,
                store=self.service.store,
            )
            self.trace["graph"] = {"protocol": GRAPH_PROTOCOL, "nodes": [], "automatic_replay": False}
            program = self.service.workflow_factory(
                plan=self.plan_node,
                tool=self.tool_node,
                compose=self.compose_node,
                verify=self.verify_node,
                repair=self.repair_node,
                deliver=self.deliver_node,
                step_limit=self.config["steps"],
                can_repair=bool(self.config.get("repair")),
                decision=decision,
            )
            self.check_execution()
            response = self.service.runtime.run(
                program,
                before_operation=self.before_node,
                max_operations=2 * self.config["steps"] + 8,
                deadline_seconds=self.execution_limits["max_turn_seconds"]
                - (time.perf_counter() - self.started),
            )


            self.check_execution()
            if self.service.gateway.authorize(self.actor, self.scope) != self.authority:
                raise PlatformError("SCOPE_CHANGED", "Authority changed before answer commit", 409)
            self.service.gateway.revalidate(
                self.actor, self.scope, response["evidence"], self.state["source_policy"]
            )
            self.check_execution()

            committed_state = deepcopy(self.state)
            evidence_ids = [item["citation_id"] for item in response["evidence"]]
            committed_state["last_answer"] = {
                "text": response["answer"],
                "evidence_ids": evidence_ids,
                "evidence": response["evidence"],
                "request_id": self.request_id,
                "user_statements": response.get("user_statements", []),
            }
            if self.config.get("work_state"):
                finish_work_state(committed_state["work_state"], response["status"], response["claims"])
            assistant_event = {
                "text": response["answer"],
                "evidence_ids": evidence_ids,
                "source_epoch": committed_state["source_epoch"],
                "user_statements": response.get("user_statements", []),
            }
            self.check_execution()
            return self.service.store.finish(
                self.actor,
                self.session_id,
                self.token,
                committed_state,
                response,
                assistant_event=assistant_event,
            )
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if code == "AGENT_DEADLINE_EXCEEDED":
                code = "AGENT_BUDGET_EXHAUSTED"
            self.trace.update(stop_reason=code, elapsed_seconds=time.perf_counter() - self.started)
            self.trace["tool_calls"] = [
                {key: value for key, value in item.items() if key not in {"args", "result"}}
                for item in self.trace["tool_calls"]
            ]
            self.trace.pop("verification", None)
            for field in ("navigation_spans", "retrieval_stages", "verification_after_repair", "repair"):
                self.trace.pop(field, None)
            return self.service.store.fail(
                self.actor,
                self.session_id,
                self.token,
                self.state,
                {
                    "answer": "",
                    "claims": [],
                    "evidence": [],
                    "trace": self.trace,
                    "status": "error",
                    "completion": "error",
                    "unresolved": ["本轮执行未完成。"],
                    "stop_reason": code,
                },
            )

    def event(self, kind, payload, group=None, agent="orchestrator"):
        if group is None:
            group = self.request_id
        return self.service.store.append(self.actor, self.session_id, self.token, kind, payload, group, agent)

    def save_evidence(self, items):
        if items:
            artifact = self.service.store.artifact(
                self.actor, self.session_id, self.token, {"evidence": items}
            )
            self.event("evidence_saved", {**artifact, "evidence_ids": [x["citation_id"] for x in items]})
            for item in items:
                key = item["citation_id"]
                item = {**item, "evidence_id": key}
                self.state["evidence"][key] = item
                self.turn_lineage[key] = item
                self.state["evidence_catalog"][key] = {
                    **artifact,
                    "source_id": item["source_id"],
                    "source_version_id": item["source_version_id"],
                }
                self.state["evidence_order"] = [cid for cid in self.state["evidence_order"] if cid != key] + [
                    key
                ]
                if self.state["source_policy"]["version_policy"] == "pinned":
                    self.state["source_policy"]["versions"].setdefault(
                        item["source_id"], item["source_version_id"]
                    )
            incoming = list(dict.fromkeys((item["citation_id"] for item in items)))
            retained = incoming[: self.max_active_evidence]
            prior = [cid for cid in self.state["evidence_order"] if cid not in incoming]
            spare = self.max_active_evidence - len(retained)
            active = retained + (prior[-spare:] if spare else [])
            if set(self.state["evidence"]) - set(active):
                self.state["checkpoints"] = {}
            self.state["evidence"] = {
                key: self.state["evidence"][key] for key in active if key in self.state["evidence"]
            }
        return items

    def check_execution(self):
        execution = current_execution()
        if execution is not None:
            execution.check()
        if time.perf_counter() - self.started > self.execution_limits["max_turn_seconds"]:
            raise PlatformError("AGENT_BUDGET_EXHAUSTED", "Turn execution budget exhausted", 409)

    def before_node(self, name):
        self.check_execution()
        if self.service.gateway.authorize(self.actor, self.scope) != self.authority:
            raise PlatformError("SCOPE_CHANGED", "Authority changed before graph node", 409)
        self.event("graph_node_started", {"node": name, "protocol": GRAPH_PROTOCOL})
        self.trace["graph"]["nodes"].append(name)
