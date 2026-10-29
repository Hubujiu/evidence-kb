from dataclasses import dataclass


@dataclass(frozen=True)
class CallBudget:
    limit: int
    verify: bool
    max_attempts: int = 1

    @property
    def final_calls(self):
        return 1 + int(self.verify)

    def can_explore(self, calls):
        physical = sum(row.get('attempts', 1) for row in calls)
        return (len(calls) + 1 + self.final_calls <= self.limit
                and physical + self.max_attempts * (1 + self.final_calls) <= self.limit)
