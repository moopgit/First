"""`swarm audit`: check the ledger against the harness's invariants.

This is the watcher made mechanical. Each check reads only the ledger, so it
works on any copy of `.swarm/` and can't be talked out of a finding.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .ledger import Ledger
from .policy import approval_problem
from .state import project


@dataclass
class Check:
    name: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def audit(ledger: Ledger, *, governor: str, max_fix_cycles: int) -> list[Check]:
    chain = Check("ledger hash chain is intact", ledger.verify())
    if not chain.ok:
        return [chain]
    events = ledger.events()
    runs = project(events)

    sources = Check("every run and task has an authorizing source")
    for run in runs.values():
        if not run.requested_by.strip():
            sources.problems.append(f"{run.id}: no requested_by")
        for t in run.tasks.values():
            if not t.requested_by.strip():
                sources.problems.append(f"{run.id}/{t.id}: no requested_by")

    builder = Check("the builder never owns the verdict")
    seat_of = {(e["run"], e["task"]): e["data"]["seat"] for e in events if e["type"] == "task_created"}
    for e in events:
        if e["type"] == "review" and e["data"]["reviewer"] == seat_of.get((e["run"], e["task"])):
            builder.problems.append(f"{e['run']}/{e['task']}: reviewed by its own seat ({e['data']['reviewer']}) at seq {e['seq']}")

    verified = Check("done means verified (gates passed, then an independent pass)")
    by_task: dict[tuple, list[dict]] = defaultdict(list)
    for e in events:
        if e["task"]:
            by_task[(e["run"], e["task"])].append(e)
    for key, evs in by_task.items():
        created = next((e for e in evs if e["type"] == "task_created"), None)
        if not created or created["data"]["mode"] == "plan":
            continue
        for i, e in enumerate(evs):
            if e["type"] != "task_done":
                continue
            last_sub = max((j for j in range(i) if evs[j]["type"] == "submitted"), default=None)
            if last_sub is None:
                verified.problems.append(f"{key[0]}/{key[1]}: done without a submission")
                continue
            after = evs[last_sub:i]
            gates = [g for g in after if g["type"] == "gate"]
            if not gates or not all(g["data"]["passed"] for g in gates):
                verified.problems.append(f"{key[0]}/{key[1]}: done without passing mechanical gates")
            if created["data"]["mode"] == "work":
                review = [r for r in after if r["type"] == "review"]
                if not review or review[-1]["data"]["verdict"] != "pass":
                    verified.problems.append(f"{key[0]}/{key[1]}: done without a passing review")

    waits = Check("every wait names its resume trigger")
    for e in events:
        if e["type"] == "waiting":
            trig = e["data"].get("triggers") or []
            if not trig or not all(":" in x and x.split(":", 1)[1] for x in trig):
                waits.problems.append(f"{e['run']}/{e['task']}: wait at seq {e['seq']} has no valid trigger")

    approvals = Check("approvals are explicit and come from the governor")
    kind_of = {e["data"]["decision"]: e["data"]["kind"] for e in events if e["type"] == "decision_requested"}
    for e in events:
        if e["type"] == "decided" and e["actor"] != governor:
            approvals.problems.append(f"{e['data']['decision']}: decided by {e['actor']}, not {governor}")
        if e["type"] == "decided" and e["data"]["granted"] and kind_of.get(e["data"]["decision"]) != "wait":
            problem = approval_problem(e["data"]["text"])
            if problem:
                approvals.problems.append(f"{e['data']['decision']}: approval text {e['data']['text']!r} - {problem}")

    actions = Check("approved actions ran exactly as approved")
    for run in runs.values():
        for d in run.decisions.values():
            if d.executed is None:
                continue
            if d.status != "granted":
                actions.problems.append(f"{d.id}: executed without a granted decision")
            if d.executed["action_hash"] != d.action_hash:
                actions.problems.append(f"{d.id}: executed action hash differs from the approved one")

    cycles = Check(f"no task looped past {max_fix_cycles} fix cycles without escalating")
    for key, evs in by_task.items():
        count = 0
        for e in evs:
            if e["type"] == "fix_cycle":
                count += 1
            elif e["type"] == "retry":
                count = 0
            elif e["type"] in ("task_started", "submitted") and count >= max_fix_cycles:
                cycles.problems.append(f"{key[0]}/{key[1]}: kept working after {count} failed cycles (seq {e['seq']})")
                break

    orphans = Check("no task is left running in a stopped run")
    for run in runs.values():
        if run.finished or run.paused:
            for t in run.tasks.values():
                if t.status in ("running", "review"):
                    orphans.problems.append(f"{run.id}/{t.id}: still '{t.status}' (interrupted - `swarm resume {run.id}`)")

    return [chain, sources, builder, verified, waits, approvals, actions, cycles, orphans]
