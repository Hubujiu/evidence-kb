"""Knowledge answer policy expressed as an opaque program for the Agent runtime."""

from .agent_engine import Runtime
from .harness_graph import GraphProgram


PROTOCOL = Runtime.protocol


class KnowledgeWorkflow(GraphProgram):
    def __init__(self, *, plan, tool, compose, verify, repair, deliver, step_limit, can_repair, decision):
        if type(step_limit) is not int or step_limit < 1:
            raise ValueError("INVALID_AGENT_STEP_LIMIT")
        self.step_limit = step_limit
        self.can_repair = can_repair
        super().__init__(
            operations=dict(plan=plan, tool=tool, compose=compose, verify=verify, repair=repair, deliver=deliver),
            edges={
                "plan": {"compose": "compose", "tool": "tool"},
                "tool": {"compose": "compose", "plan": "plan"},
                "compose": {"verify": "verify", "deliver": "deliver"},
                "verify": {"repair": "repair", "deliver": "deliver"},
                "repair": {"verify": "verify"},
                "deliver": {"complete": None},
            },
            start="plan", route=self._choose_route,
            flow={"step": 0, "decision": decision, "repaired": False},
        )

    def _choose_route(self, stage, flow):
        if stage == "plan":
            return "compose" if flow["decision"]["action"] in {"answer", "clarify"} else "tool"
        if stage == "tool":
            return "compose" if flow["halted"] or flow["step"] >= self.step_limit else "plan"
        if stage == "compose":
            return "verify" if flow["verify"] else "deliver"
        if stage == "verify":
            return (
                "repair"
                if self.can_repair and not flow["repaired"] and not flow["verdict"]["supported"]
                else "deliver"
            )
        if stage == "repair":
            return "verify"
        return "complete"
