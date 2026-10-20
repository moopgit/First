"""`swarm audit`: check the ledger against the harness's invariants.

This is the watcher made mechanical. It reads only `.swarm/` (the ledger, plus
the config snapshots the ledger names by hash), so it works on any copy of it
and can't be talked out of a finding. What it proves is limited: the recorded
history is internally consistent and untampered, under the assumption the
harness wrote it.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, ConfigError, parse_config
from .ledger import Ledger
from .policy import approval_problem, expected_gates
from .state import check_event, project


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
    raw = ledger.events()
    structure = Check("every event is structurally complete")
    runs = project(raw, structure.problems)
    events = [e for e in (check_event(r)[0] for r in raw) if e is not None]

    sources = Check("every run and task has an authorizing source")
    for run in runs.values():
        if not run.requested_by.strip():
            sources.problems.append(f"{run.id}: no requested_by")
        for t in run.tasks.values():
            if not t.requested_by.strip():
                sources.problems.append(f"{run.id}/{t.id}: no requested_by")

    phantoms = Check("every event belongs to a started run, and every task event to a created task")
    started: set[str] = set()
    created: set[tuple[str, str]] = set()
    for e in events:
        if e["type"] == "run_started":
            if e["run"] in started:
                phantoms.problems.append(f"{e['run']}: run_started again at seq {e['seq']}")
            started.add(e["run"])
        elif e["run"] not in started:
            phantoms.problems.append(f"{e['run']}: {e['type']} at seq {e['seq']} for a run with no run_started")
        elif e["type"] == "task_created":
            if (e["run"], e["task"]) in created:
                phantoms.problems.append(f"{e['run']}/{e['task']}: task_created again at seq {e['seq']}")
            created.add((e["run"], e["task"]))
        elif e["task"] and (e["run"], e["task"]) not in created:
            phantoms.problems.append(f"{e['run']}/{e['task']}: {e['type']} at seq {e['seq']} for a task with no task_created")

    builder = Check("the builder never owns the verdict")
    seat_of = {(e["run"], e["task"]): e["data"]["seat"] for e in events if e["type"] == "task_created"}
    for e in events:
        if e["type"] == "review" and e["data"]["reviewer"] == seat_of.get((e["run"], e["task"])):
            builder.problems.append(f"{e['run']}/{e['task']}: reviewed by its own seat ({e['data']['reviewer']}) at seq {e['seq']}")

    by_task: dict[tuple, list[dict]] = defaultdict(list)
    for e in events:
        if e["task"]:
            by_task[(e["run"], e["task"])].append(e)
    verified = Check("done means verified (every configured gate passed, then an independent pass)")
    _check_gates_and_reviews(ledger.dir, events, by_task, verified.problems)

    waits = Check("every wait names its resume trigger")
    for e in events:
        if e["type"] == "waiting":
            trig = e["data"]["triggers"]
            if not trig or not all(isinstance(x, str) and ":" in x and x.split(":", 1)[1] for x in trig):
                waits.problems.append(f"{e['run']}/{e['task']}: wait at seq {e['seq']} has no valid trigger")

    approvals = Check("approvals are explicit and record who ran them")
    kind_of = {e["data"]["decision"]: e["data"]["kind"] for e in events if e["type"] == "decision_requested"}
    for e in events:
        if e["type"] != "decided":
            continue
        d = e["data"]
        who = d.get("provenance")
        if not isinstance(who, dict) or not who.get("user"):
            approvals.problems.append(f"{d['decision']}: no record of who ran the command (seq {e['seq']})")
        if d.get("acting_as") != governor:
            approvals.problems.append(f"{d['decision']}: run as {d.get('acting_as')!r}, not as the governor ({governor})")
        if d["granted"] and kind_of.get(d["decision"]) != "wait":
            problem = approval_problem(d["text"])
            if problem:
                approvals.problems.append(f"{d['decision']}: approval text {d['text']!r} - {problem}")

    actions = Check("approved actions ran exactly as approved")
    for run in runs.values():
        for dv in run.decisions.values():
            if dv.executed is None:
                continue
            if dv.status != "granted":
                actions.problems.append(f"{dv.id}: executed without a granted decision")
            if dv.executed["action_hash"] != dv.action_hash:
                actions.problems.append(f"{dv.id}: executed action hash differs from the approved one")

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

    return [chain, structure, sources, phantoms, builder, verified, waits, approvals, actions, cycles, orphans]


def _check_gates_and_reviews(state_dir: Path, events: list[dict], by_task: dict, problems: list[str]) -> None:
    """Check 4. Every gate event is self-consistent; in each post-submission window the configured
    gate set runs before any review; and a task is done only after all of them passed and, for
    work, an independent review passed."""
    for e in events:
        if e["type"] == "gate" and e["data"]["passed"] != (not e["data"]["findings"]):
            problems.append(f"{e['run']}/{e['task']}: gate {e['data']['gate']!r} at seq {e['seq']} records "
                            f"passed={e['data']['passed']} with {len(e['data']['findings'])} finding(s)")

    # The config in force over time, per run: run_started's hash, then each adoption.
    timeline: dict[str, list[tuple[int, str]]] = defaultdict(list)
    bases: dict[str, str] = {}
    for e in events:
        if e["type"] == "run_started":
            timeline[e["run"]].append((e["seq"], e["data"]["config_hash"]))
            bases[e["run"]] = str(e["data"].get("base_dir") or state_dir.parent)
        elif e["type"] == "config_adopted":
            timeline[e["run"]].append((e["seq"], e["data"]["new"]))
    configs: dict[tuple[str, str], Config | str] = {}

    def config_at(run: str, seq: int) -> Config | str:
        """The config in force at `seq`, or why it can't be known."""
        hashes = [h for s, h in timeline.get(run, []) if s < seq]
        if not hashes:
            return "no run_started before it"
        h = hashes[-1]
        if (run, h) not in configs:
            configs[(run, h)] = _load_snapshot(state_dir / "runs" / run, h, bases.get(run, str(state_dir.parent)))
        return configs[(run, h)]

    for (run, task), evs in by_task.items():
        created = next((e for e in evs if e["type"] == "task_created"), None)
        if not created or created["data"]["mode"] == "plan":
            continue  # phantom tasks are the phantom check's; plans have no gates
        label = f"{run}/{task}"
        mode, seat, cls = created["data"]["mode"], created["data"]["seat"], created["data"]["content_class"]
        subs = [i for i, e in enumerate(evs) if e["type"] == "submitted"]
        first = subs[0] if subs else len(evs)
        for e in evs[:first]:
            if e["type"] in ("gate", "review", "task_done"):
                problems.append(f"{label}: {e['type']} at seq {e['seq']} before any submission")
        for k, start in enumerate(subs):
            window = evs[start + 1: subs[k + 1] if k + 1 < len(subs) else len(evs)]
            cfg = config_at(run, evs[start]["seq"])
            if isinstance(cfg, str):
                problems.append(f"{label}: can't check submission at seq {evs[start]['seq']} against its configured gates ({cfg})")
                expected = None
            elif seat not in cfg.seats:
                problems.append(f"{label}: seat {seat!r} is not in the config in force at seq {evs[start]['seq']}")
                expected = None
            else:
                expected = expected_gates(cfg, cfg.seats[seat], cls)
            results: dict[str, bool] = {}  # gate name -> every run of it in this window passed
            reviewed = False
            last_verdict = None
            for e in window:
                if e["type"] == "gate":
                    name = e["data"]["gate"]
                    if reviewed:
                        problems.append(f"{label}: gate {name!r} at seq {e['seq']} comes after the review")
                    results[name] = results.get(name, True) and e["data"]["passed"]
                elif e["type"] == "review":
                    reviewed, last_verdict = True, e["data"]["verdict"]
                    gap = _gate_gap(expected, results)
                    if gap:
                        problems.append(f"{label}: review at seq {e['seq']} before the configured gates passed ({gap})")
                elif e["type"] == "task_done":
                    gap = _gate_gap(expected, results)
                    if gap:
                        problems.append(f"{label}: done at seq {e['seq']} without the configured gates passing ({gap})")
                    if mode == "work" and last_verdict != "pass":
                        problems.append(f"{label}: done at seq {e['seq']} without a passing review")


def _gate_gap(expected: list[str] | None, results: dict[str, bool]) -> str:
    if expected is None:
        return "configured gate set unknown"
    missing = [g for g in expected if g not in results]
    failed = [g for g in expected if results.get(g) is False]
    return "; ".join(part for part in (
        f"missing {', '.join(missing)}" if missing else "",
        f"failed {', '.join(failed)}" if failed else "",
    ) if part)


def _load_snapshot(run_dir: Path, config_hash: str, base_dir: str) -> Config | str:
    """The run's config snapshot whose text hashes to `config_hash`, or why there isn't one."""
    for path in (run_dir / "config.yaml", run_dir / f"config.{config_hash[:16]}.yaml"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
            if hashlib.sha256(text.encode()).hexdigest() != config_hash:
                continue
            return parse_config(text, base_dir=Path(base_dir))
        except (ConfigError, ValueError, OSError) as e:
            return f"snapshot for config {config_hash[:12]} can't be read: {e}"
    return f"no snapshot of config {config_hash[:12] or '(none recorded)'} in {run_dir}/"
