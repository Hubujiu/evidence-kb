"""Knowledge answer policy expressed as an opaque program for the Agent runtime."""

from .agent_engine import Complete, Operation, Runtime


PROTOCOL = Runtime.protocol


class KnowledgeWorkflow:
    def __init__(self, *, plan, tool, compose, verify, repair, deliver, step_limit, can_repair, decision):
        if type(step_limit) is not int or step_limit < 1:
            raise ValueError("INVALID_AGENT_STEP_LIMIT")
        self.operations = dict(
            plan=plan, tool=tool, compose=compose, verify=verify, repair=repair, deliver=deliver
        )
        self.step_limit = step_limit
        self.can_repair = can_repair
        self.flow = {"step": 0, "decision": decision, "repaired": False}
        self.stage = "plan"

    def next(self):
        if self.stage is None:
            return Complete(self.flow["response"])
        return Operation(name=self.stage, kind="host")

    def execute(self, operation):
        if operation.name != self.stage:
            raise ValueError("KNOWLEDGE_OPERATION_OUT_OF_ORDER")
        return self.operations[operation.name](self.flow)

    def accept(self, operation, result):
        if operation.name != self.stage or not isinstance(result, dict):
            raise ValueError("INVALID_KNOWLEDGE_OPERATION_RESULT")
        self.flow.update(result)
        if self.stage == "plan":
            self.stage = "compose" if self.flow["decision"]["action"] in {"answer", "clarify"} else "tool"
        elif self.stage == "tool":
            self.stage = "compose" if self.flow["halted"] or self.flow["step"] >= self.step_limit else "plan"
        elif self.stage == "compose":
            self.stage = "verify" if self.flow["verify"] else "deliver"
        elif self.stage == "verify":
            self.stage = (
                "repair"
                if self.can_repair and not self.flow["repaired"] and not self.flow["verdict"]["supported"]
                else "deliver"
            )
        elif self.stage == "repair":
            self.stage = "verify"
        else:
            self.stage = None
