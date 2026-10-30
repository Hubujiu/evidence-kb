from __future__ import annotations
from contextlib import nullcontext
from dataclasses import asdict
from .agent_context import ContextManager, ContextPolicy
from .knowledge_sessions import KnowledgeSessions
from .session_store import SessionStore
from .agent_tools import EvidenceGateway
from .core import PlatformError
from .knowledge_policy import VERSION, RECOMMENDED, CONFIGS, _active_evidence_limit, source_policy
from .knowledge_plugin_tools import KnowledgeToolPlugins
from .agent_engine import Runtime

from .knowledge_turn import KnowledgeTurn
from .knowledge_workflow import KnowledgeWorkflow

_AUTO_SEMANTIC = object()


class KnowledgeService:
    def __init__(
        self,
        platform,
        model,
        *,
        research=None,
        root=".local/agentic-v5",
        store=None,
        gateway=None,
        context_policy=None,
        semantic_pages=_AUTO_SEMANTIC,
        retrieval_config=None,
        capture=None,
        tool_context=None,
        tool_plugins=(),
        tool_executor=None,
        context_manager=None,
        runtime=None,
        workflow_factory=None,
    ):
        self.store = store if store is not None else KnowledgeSessions(SessionStore(root))
        self.gateway = gateway or EvidenceGateway(platform, research, provider_id="magpie_luna")
        self.model = model
        self.context_policy = context_policy or ContextPolicy(
            input_budget=24000, evidence_strategy="query_blocks"
        )
        self.context = context_manager if context_manager is not None else ContextManager(self.context_policy)
        self.plugins = KnowledgeToolPlugins(tool_plugins)
        self.runtime = runtime if runtime is not None else Runtime()
        self.workflow_factory = workflow_factory if workflow_factory is not None else KnowledgeWorkflow
        self.retrieval_config = retrieval_config or {
            "channels": ["bm25", "dense"],
            "rerank": True,
            "parent": "paragraph",
            "top_k": 5,
        }
        self.capture = capture
        self.tool_context = tool_context or (lambda metadata: nullcontext())
        from .provider_runtime import ToolExecutor

        self.tool_executor = tool_executor if tool_executor is not None else ToolExecutor(max_attempts=2)
        if semantic_pages is _AUTO_SEMANTIC:
            from .semantic_page_runtime import from_environment

            semantic_pages = from_environment(platform, self.gateway)
        self.semantic_pages = semantic_pages

    def _semantic_available(self, actor, scope_id):
        if self.semantic_pages is None:
            return False
        try:
            self.semantic_pages.current_manifest(actor, scope_id)
            return True
        except PlatformError as exc:
            if exc.code != "SEMANTIC_PAGE_INDEX_UNAVAILABLE" or (
                exc.__cause__ is not None and (not isinstance(exc.__cause__, FileNotFoundError))
            ):
                raise
            return False

    def create(self, actor, scope_id, policy=None, config_name="recommended"):
        self.gateway.authorize(actor, scope_id)
        config_name = RECOMMENDED if config_name == "recommended" else config_name
        if config_name not in CONFIGS:
            raise PlatformError("INVALID_AGENT_CONFIG", "Unknown agent configuration", 422)
        _active_evidence_limit(CONFIGS[config_name])
        return self.store.create(
            actor, scope_id, self._pin(actor, scope_id, source_policy(policy)), config_name
        )

    def _pin(self, actor, scope_id, policy):
        if policy["version_policy"] == "pinned" and (not policy["versions"]):
            rows = self.gateway.sources(actor, scope_id)
            policy["versions"] = {
                x["source_id"]: x["source_version_id"]
                for x in rows
                if not policy["source_ids"] or x["source_id"] in policy["source_ids"]
            }
            if not policy["versions"] or set(policy["source_ids"]) - set(policy["versions"]):
                raise PlatformError("SOURCE_UNAVAILABLE", "Cannot pin the selected sources", 409)
        return policy

    def _allowed_evidence(self, actor, session):
        state = session["state"]
        lineage = {
            **state["evidence"],
            **{x["citation_id"]: x for x in (state.get("last_answer") or {}).get("evidence", [])},
        }
        return (
            self.gateway.revalidate(
                actor, session["scope_id"], list(lineage.values()), state["source_policy"]
            )
            if lineage
            else []
        )

    def get(self, actor, session_id):
        session = self.store.load(actor, session_id)
        self.gateway.authorize(actor, session["scope_id"])
        try:
            self._allowed_evidence(actor, session)
        except PlatformError:
            session["state"] = {
                **session["state"],
                "evidence": {},
                "checkpoints": {},
                "last_answer": None,
                "evidence_catalog": {},
                "evidence_order": [],
                "evidence_offsets": {},
                "work_state": None,
                "history_redacted": True,
            }
        return session

    def history(self, actor, session_id):
        session = self.store.load(actor, session_id)
        self.gateway.authorize(actor, session["scope_id"])
        events = self.store.events(actor, session_id)
        visible = []
        for event in events:
            payload = event["payload"]
            if event["kind"] == "user":
                visible.append(event)
            elif event["kind"] == "turn_completed":
                try:
                    self.gateway.revalidate(
                        actor,
                        session["scope_id"],
                        payload.get("evidence", []),
                        session["state"]["source_policy"],
                    )
                    visible.append(event)
                except PlatformError:
                    visible.append(
                        {**event, "payload": {"status": "redacted", "reason": "source_unavailable"}}
                    )
        return {"session_id": session_id, "events": visible}

    def artifact(self, actor, session_id, artifact_id):
        session = self.store.load(actor, session_id)
        self.gateway.authorize(actor, session["scope_id"])
        body = self.store.read_artifact(actor, session_id, artifact_id)
        self.gateway.revalidate(
            actor, session["scope_id"], body.get("evidence", []), session["state"]["source_policy"]
        )
        return body

    def capabilities(self):
        tools = ["search", "search_pages", "read", "read_history", "calculator"]
        tools += [
            name
            for name in [
                "search_pipeline",
                "search_tables",
                "table",
                "table_join",
                "navigate_references",
                "workflow_next",
            ]
            if hasattr(self.gateway, name)
        ]
        if self.semantic_pages is not None:
            tools.append("search_semantic_pages")
        return {
            "version": VERSION,
            "recommended": RECOMMENDED,
            "configurations": CONFIGS,
            "tools": tools + self.plugins.names(),
            "context_policy": asdict(self.context_policy),
            "token_counting": "conservative_utf8_estimate",
            "strict_token_guarantee": False,
        }

    def turn(
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
        return KnowledgeTurn(self).run(
            actor,
            session_id,
            message=message,
            request_id=request_id,
            expected_revision=expected_revision,
            policy=policy,
            constraints=constraints,
            workflow_state=workflow_state,
        )
