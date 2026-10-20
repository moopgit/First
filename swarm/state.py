"""Projections of the ledger: runs, tasks and the governor's inbox.

Nothing here is stored. Every view is rebuilt from events, so a count or a
status can never disagree with the record it summarizes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A task is always in exactly one status: pending, running, waiting, review, done, escalated or parked.
TERMINAL = ("done", "parked")


@dataclass
class TaskView:
    id: str
    run: str
    seat: str
    mode: str
    goal: str
    acceptance: list[str]
    deps: list[str]
    tier: str
    content_class: str
    requested_by: str
    created_by: str
    parent: str | None = None
    depth: int = 0
    key: str = ""
    status: str = "pending"
    fix_cycles: int = 0
    turns: int = 0
    cost: float = 0.0
    budget_base: tuple[int, float] = (0, 0.0)  # (turns, cost) already spent when the governor last granted a retry
    triggers: list[str] = field(default_factory=list)
    wait_reason: str = ""
    submissions: list[dict] = field(default_factory=list)
    feedback: list[str] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    escalation: str = ""
    escalation_decision: str | None = None
    started: bool = False
    last_seq: int = 0

    @property
    def submission(self) -> dict | None:
        return self.submissions[-1] if self.submissions else None


@dataclass
class DecisionView:
    """Something only the governor can resolve."""

    id: str
    run: str
    task: str
    kind: str  # action | review | wait | escalation
    summary: str
    action_hash: str = ""
    status: str = "pending"  # pending | granted | denied
    text: str = ""
    by: str = ""
    executed: dict | None = None
    seq: int = 0


@dataclass
class RunView:
    id: str
    goal: str
    requested_by: str
    config_hash: str
    provider: str
    started_at: str
    tasks: dict[str, TaskView] = field(default_factory=dict)
    decisions: dict[str, DecisionView] = field(default_factory=dict)
    cost: float = 0.0
    turns: int = 0
    finished: str = ""  # done | partial
    paused: str = ""
    result: str = ""

    @property
    def inbox(self) -> list[DecisionView]:
        return [d for d in self.decisions.values() if d.status == "pending"]

    @property
    def work(self) -> list[TaskView]:
        return [t for t in self.tasks.values() if t.mode == "work"]

    def task_by_mode(self, mode: str) -> TaskView | None:
        return next((t for t in self.tasks.values() if t.mode == mode), None)

    @property
    def status(self) -> str:
        if self.finished:
            return self.finished
        if self.inbox:
            return "needs-governor"
        if any(t.status in ("running", "review") for t in self.tasks.values()):
            return "running" if not self.paused else "interrupted"
        return "paused" if self.paused else "running"

    def trigger_resolved(self, trigger: str) -> bool:
        kind, _, ref = trigger.partition(":")
        if kind == "decision":
            d = self.decisions.get(ref)
            return bool(d and d.status != "pending")
        if kind == "task":
            t = self.tasks.get(ref)
            return bool(t and t.status in TERMINAL)
        return False


def project(events: list[dict]) -> dict[str, RunView]:
    runs: dict[str, RunView] = {}
    for e in events:
        typ, d, rid = e["type"], e["data"], e["run"]
        if typ == "run_started":
            runs[rid] = RunView(rid, d["goal"], d["requested_by"], d["config_hash"], d.get("provider", ""), e["ts"])
            continue
        run = runs.get(rid)
        if run is None:
            continue
        t = run.tasks.get(e["task"]) if e["task"] else None
        if t:
            t.last_seq = e["seq"]

        if typ == "task_created":
            run.tasks[e["task"]] = TaskView(
                id=e["task"], run=rid, seat=d["seat"], mode=d["mode"], goal=d["goal"], acceptance=d["acceptance"],
                deps=d["deps"], tier=d["tier"], content_class=d["content_class"], requested_by=d["requested_by"],
                created_by=e["actor"], parent=d.get("parent"), depth=d.get("depth", 0), key=d.get("key", ""), last_seq=e["seq"],
            )
        elif typ == "task_started" and t:
            t.status, t.started, t.triggers = "running", True, []
        elif typ == "turn":
            run.cost += d["cost"]
            run.turns += 1
            if t:
                t.cost += d["cost"]
                t.turns += 1
        elif typ == "submitted" and t:
            t.submissions.append(d)
        elif typ == "review_started" and t:
            t.status = "review"
        elif typ == "review" and t:
            t.reviews.append(d)
        elif typ == "fix_cycle" and t:
            if t.status == "review":
                t.status = "running"
            t.fix_cycles += 1
            t.feedback.append(d["feedback"])
        elif typ == "waiting" and t:
            t.status, t.triggers, t.wait_reason = "waiting", d["triggers"], d.get("reason", "")
        elif typ == "task_done" and t:
            t.status = "done"
        elif typ == "escalated" and t:
            t.status, t.escalation, t.escalation_decision = "escalated", d["reason"], d.get("decision")
        elif typ == "retry" and t:
            t.status, t.fix_cycles, t.escalation = "pending", 0, ""
            t.budget_base = (t.turns, t.cost)
            t.feedback.append(d["note"])
        elif typ == "parked" and t:
            t.status = "parked"
        elif typ == "decision_requested":
            run.decisions[d["decision"]] = DecisionView(
                d["decision"], rid, e["task"], d["kind"], d["summary"], d.get("action_hash", ""), seq=e["seq"]
            )
        elif typ == "decided":
            dec = run.decisions.get(d["decision"])
            if dec:
                dec.status = "granted" if d["granted"] else "denied"
                dec.text, dec.by = d["text"], e["actor"]
        elif typ == "action_executed":
            dec = run.decisions.get(d["decision"])
            if dec:
                dec.executed = d
        elif typ == "config_adopted":
            run.config_hash = d["new"]
        elif typ == "run_paused":
            run.paused = d["reason"]
        elif typ == "run_resumed":
            run.paused = ""
        elif typ == "run_finished":
            run.finished, run.result = d["status"], d.get("result", "")
    return runs
