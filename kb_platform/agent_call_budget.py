from dataclasses import dataclass


@dataclass(frozen=True)
class CallBudget:
    limit: int
    verify: bool
    max_attempts: int = 1

    @property
    def final_calls(self):
        return 1 + int(self.verify)

    def can_explore(self, calls, *, max_attempts=None):
        physical = sum(row.get('attempts', 1) for row in calls)
        attempts = self.max_attempts if max_attempts is None else max_attempts
        return (len(calls) + 1 + self.final_calls <= self.limit
                and physical + attempts + self.max_attempts * self.final_calls <= self.limit)
