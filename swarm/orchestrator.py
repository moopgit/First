"""The router: plans a goal into tasks, runs them in parallel, gates every
result, routes judgment to an independent reviewer or the governor, and
resumes paused work when the event it waits on happens.

The router never does the work itself, and it never holds state the ledger
doesn't: every decision below re-reads the projection.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from . import policy as pol
from .agent import Budget, SeatResult, Transcript, run_seat
from .config import TIERS, Config, Seat
from .ledger import Ledger, _canonical
from .prompts import _handoff, plan_brief, review_brief, seat_system, synth_brief, work_brief
from .provenance import build, invoker, principal
from .providers import CredentialMismatch, Provider, ProviderError, ProviderFatal, Turn
from .state import TERMINAL, DecisionView, RunView, TaskView, project
from .tools import Executor, Hooks, run_argv, write_text

HARNESS = "harness"


class HarnessError(RuntimeError):
    pass


class AmbiguousApproval(HarnessError):
    pass


class ConfigDrift(HarnessError):
    pass


class RunBusy(HarnessError):
    pass


def _num(task_id: str) -> int:
    return int(task_id.lstrip("T"))


class Orchestrator:
    def __init__(self, cfg: Config, provider: Provider | None = None, *, ledger: Ledger | None = None):
        self.cfg = cfg
        self.provider = provider
        self.ledger = ledger or Ledger(cfg.state_dir)
        self._run_cost: dict[str, float] = {}
        self._build_logged: set[str] = set()  # runs whose ledger already holds this process's build

    # --- views ----------------------------------------------------------------

    def runs(self) -> dict[str, RunView]:
        return project(self.ledger.events())

    def view(self, run_id: str) -> RunView:
        run = project(self.ledger.events(run_id)).get(run_id)
        if run is None:
            raise HarnessError(f"no run {run_id!r}")
        return run

    def task(self, run_id: str, task_id: str) -> TaskView:
        return self.view(run_id).tasks[task_id]

    def run_dir(self, run_id: str) -> Path:
        return self.cfg.state_dir / "runs" / run_id

    # --- governor surface -----------------------------------------------------

    def start(self, goal: str, requested_by: str) -> str:
        if self.provider is None:
            raise HarnessError("starting a run needs a model provider")
        if not goal.strip():
            raise HarnessError("the goal is empty")
        if not requested_by.strip():
            raise HarnessError("every run needs an authorizing source (requested_by)")
        run_id = self.ledger.allocate("R")
        rd = self.run_dir(run_id)
        rd.mkdir(parents=True, exist_ok=True)
        # Spanning work finishes under the rules it started with: resume uses this snapshot.
        (rd / "config.yaml").write_text(self.cfg.source_text)
        self.ledger.append(
            "run_started", run=run_id, actor=requested_by, goal=goal.strip(), requested_by=requested_by,
            config_hash=self.cfg.hash, provider=self.provider.name, credential=self.provider.credential_id(),
            base_dir=str(self.cfg.base_dir), build=build(),
        )
        self._build_logged.add(run_id)
        self._create_task(run_id, actor=HARNESS, seat=self.cfg.router.name, mode="plan", goal=goal.strip(),
                          acceptance=[], deps=[], tier="routine", content_class="general", requested_by=requested_by)
        return run_id

    def adopt_config(self, run_id: str, provenance: dict | None = None) -> None:
        run = self.view(run_id)
        if run.config_hash != self.cfg.hash:
            current = self.run_dir(run_id) / "config.yaml"
            if current.exists():  # keep the rules earlier work ran under, named by their hash, for the audit
                shutil.copyfile(current, current.with_name(f"config.{run.config_hash[:16]}.yaml"))
            current.write_text(self.cfg.source_text)
            self.ledger.append("config_adopted", run=run_id, old=run.config_hash, new=self.cfg.hash, **self._act(provenance))

    def adopt_credential(self, run_id: str, provenance: dict | None = None) -> None:
        """Deliberately move a run to the credential this shell would bill."""
        run = self.view(run_id)
        new = self.provider.credential_id() if self.provider else ""
        if new and run.credential != new:
            self.ledger.append("credential_adopted", run=run_id, old=run.credential, new=new, **self._act(provenance))

    def _act(self, provenance: dict | None) -> dict:
        """Ledger fields for a governor act: who actually ran it, and the role it was run as."""
        who = provenance or invoker()
        return {"actor": principal(who), "acting_as": self.cfg.governor, "provenance": who}

    def find_decision(self, decision_id: str) -> tuple[RunView, DecisionView]:
        for run in self.runs().values():
            if decision_id in run.decisions:
                return run, run.decisions[decision_id]
        raise HarnessError(f"no decision {decision_id!r}")

    def action_payload(self, run_id: str, decision_id: str) -> dict | None:
        path = self.run_dir(run_id) / "actions" / f"{decision_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    async def decide(self, decision_id: str, *, granted: bool, text: str, provenance: dict | None = None) -> DecisionView:
        """Record the governor's call. Approvals must be explicit; a granted action runs exactly as queued.

        Every event records who actually ran the command (provenance); the harness can't authenticate a person.
        A refused attempt is recorded as `decision_refused`, never as a decision.
        """
        run, d = self.find_decision(decision_id)
        if d.status != "pending":
            raise HarnessError(f"{decision_id} was already {d.status}")
        self._check_config(run)
        text = (text or "").strip()
        act = self._act(provenance)
        refusal: HarnessError | None = None
        problem = pol.approval_problem(text) if granted and d.kind != "wait" else None
        if problem:
            refusal = AmbiguousApproval(f"refused: {problem}. Approve with explicit words, e.g. \"approved\" or \"yes, go\".")
        elif not text:
            problem = "no reason or answer given"
            refusal = HarnessError("give a reason or an answer; the seat uses your words to continue")
        if refusal:
            self.ledger.append("decision_refused", run=run.id, task=d.task, decision=d.id, granted=granted, text=text,
                               problem=problem, **act)
            raise refusal
        gov = self.cfg.governor
        self.ledger.append("decided", run=run.id, task=d.task, decision=d.id, granted=granted, text=text, build=build(), **act)
        if d.kind == "action" and granted:
            await self._execute_action(run.id, d)
        elif d.kind == "review":
            self.ledger.append("review", run=run.id, task=d.task, reviewer=gov, verdict="pass" if granted else "fail",
                               criteria=[], feedback=text, **act)
            if granted:
                self._complete(run.id, d.task)
            else:
                self._record_fix_cycle(run.id, d.task, f"{gov} sent it back: {text}")
        elif d.kind == "escalation":
            if granted:
                self.ledger.append("retry", run=run.id, task=d.task, note=text, **act)
            else:
                self.ledger.append("parked", run=run.id, task=d.task, reason=text, **act)
        return self.view(run.id).decisions[d.id]

    # --- the run loop ---------------------------------------------------------

    async def drive(self, run_id: str) -> RunView:
        run = self.view(run_id)
        if run.finished:
            return run
        if self.provider is None:
            raise HarnessError("driving a run needs a model provider")
        self._check_config(run)
        current = self.provider.credential_id()
        if run.credential and current != run.credential:
            raise CredentialMismatch(
                f"{run_id} started on credential {run.credential}, but this shell would bill {current}. Export the run's "
                f"key in this shell, or run `swarm resume {run_id} --adopt-credential` to move the run deliberately."
            )
        with self._driver_lock(run_id):
            return await self._drive(run_id)

    @contextmanager
    def _driver_lock(self, run_id: str):
        """One driver per run, so a second process never mistakes live tasks for crashed ones."""
        path = self.run_dir(run_id) / "driver.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RunBusy(f"{run_id} is being driven by another process, which picks up new decisions itself") from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    async def _drive(self, run_id: str) -> RunView:
        run = self.view(run_id)
        self._run_cost[run_id] = run.cost
        if run_id not in self._build_logged or run.paused or any(t.status in ("running", "review") for t in run.tasks.values()):
            # Every process that drives a run records the code it is running.
            self.ledger.append("run_resumed", run=run_id, actor=HARNESS, build=build())
            self._build_logged.add(run_id)
        running: dict[str, asyncio.Task] = {}
        try:
            while True:
                run = self.view(run_id)
                if run.finished:
                    return run
                if self._run_cost[run_id] < self.cfg.budgets.run_cost_usd:
                    for t in self._launchable(run, running):
                        if len(running) >= self.cfg.concurrency:
                            break
                        running[t.id] = asyncio.create_task(self._work(run_id, t.id), name=t.id)
                if running:
                    done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
                    for fut in done:
                        tid = fut.get_name()
                        del running[tid]
                        exc = fut.exception()
                        if isinstance(exc, ProviderFatal):
                            raise exc
                        if exc is not None:
                            self._escalate(run_id, tid, f"harness error: {type(exc).__name__}: {exc}")
                    continue
                if not self._idle_step(run_id):
                    return self.view(run_id)
        except BaseException as e:
            for fut in running.values():
                fut.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)
            if isinstance(e, ProviderFatal):
                self._pause(self.view(run_id), f"model unavailable: {e}")
            raise

    def _launchable(self, run: RunView, running: dict) -> list[TaskView]:
        out = []
        for t in sorted(run.tasks.values(), key=lambda t: _num(t.id)):
            if t.id in running:
                continue
            if t.status == "pending" and all(run.tasks[d].status == "done" for d in t.deps):
                out.append(t)
            elif t.status == "waiting" and t.triggers and all(run.trigger_resolved(x) for x in t.triggers):
                out.append(t)
            elif t.status in ("running", "review"):
                out.append(t)  # interrupted by an earlier process; resumes from its checkpoint
        return out

    def _idle_step(self, run_id: str) -> bool:
        """Nothing is running or launchable. Returns True if there is new work to loop on."""
        run = self.view(run_id)
        if self._run_cost[run_id] >= self.cfg.budgets.run_cost_usd:
            self._pause(run, f"run cost cap reached (${self._run_cost[run_id]:.2f} of ${self.cfg.budgets.run_cost_usd:.2f}); "
                             "raise budgets.run_cost_usd and `swarm resume --adopt-config`")
            return False
        if run.inbox:
            self._pause(run, "waiting on the governor: " + ", ".join(f"{d.id} ({d.kind})" for d in run.inbox))
            return False
        plan, synth = run.task_by_mode("plan"), run.task_by_mode("synth")
        if (plan and plan.status == "parked") or (synth and synth.status == "parked"):
            self._finish(run_id, "partial", "")
            return False
        stuck = [t for t in run.work if t.status not in TERMINAL and not self._blocked(run, t)]
        if not stuck and plan and plan.status == "done" and not synth:
            self._create_task(run_id, actor=HARNESS, seat=self.cfg.router.name, mode="synth", goal=run.goal,
                              acceptance=[], deps=[], tier="routine", content_class="general", requested_by=run.requested_by)
            return True
        # Not moving and no named wait is stalled by definition: surface it rather than idle silently.
        self._pause(run, "stalled: " + (", ".join(f"{t.id} ({t.status})" for t in stuck) or "no runnable work"))
        return False

    def _blocked(self, run: RunView, t: TaskView) -> bool:
        return t.status == "pending" and any(
            run.tasks[d].status == "parked" or self._blocked(run, run.tasks[d]) for d in t.deps
        )

    # --- one task ---------------------------------------------------------------

    async def _work(self, run_id: str, task_id: str) -> None:
        run = self.view(run_id)
        t = run.tasks[task_id]
        seat = self.cfg.seats[t.seat]
        tx = Transcript.load(self.run_dir(run_id) / "transcripts" / f"{task_id}.json")
        resumed = not tx.is_new
        if resumed:
            tx.repair()
            note = self._resume_note(run, t)
            if note:
                tx.add_user(note)
        else:
            tx.add_user(self._brief(run, t))
        self.ledger.append("task_started", run=run_id, task=task_id, actor=seat.name, resumed=resumed)
        budget = Budget(
            turn_cap=self.cfg.budgets.task_turns, cost_cap=self.cfg.budgets.task_cost_usd,
            run_cost_cap=self.cfg.budgets.run_cost_usd, turns=t.turns - t.budget_base[0], cost=t.cost - t.budget_base[1],
            run_cost=lambda: self._run_cost[run_id],
        )
        executor = self._executor(run_id, t, seat, t.mode)
        try:
            while True:
                result = await self._seat_loop(run_id, t, seat, t.mode, tx, executor, budget)
                if result.kind == "pause":
                    self.ledger.append("waiting", run=run_id, task=task_id, actor=seat.name,
                                       triggers=result.triggers, reason=result.data.get("reason", ""))
                    return
                if result.kind == "escalate":
                    self._escalate(run_id, task_id, result.data["reason"])
                    return
                if result.kind == "plan":
                    self._accept_plan(run_id, t, result.data)
                    self.ledger.append("task_done", run=run_id, task=task_id, actor=seat.name)
                    return
                if not await self._handle_submission(run_id, task_id, seat, result, tx, budget):
                    return
        except ProviderError as e:
            self._escalate(run_id, task_id, f"model call failed: {e}")

    async def _handle_submission(self, run_id, task_id, seat: Seat, result: SeatResult, tx: Transcript, budget: Budget) -> bool:
        """Gate, then judge. Returns True if the seat should keep working (rework)."""
        sub = result.data
        t = self.task(run_id, task_id)
        n = len(t.submissions) + 1
        self._snapshot(run_id, t, n, sub)
        self.ledger.append("submitted", run=run_id, task=task_id, actor=seat.name, n=n, summary=sub["summary"], artifacts=sub["artifacts"])

        problems = await self._mechanical_gates(run_id, t, seat, sub)
        if problems:
            return self._rework(run_id, task_id, "Mechanical checks failed", problems, tx)
        if t.mode == "synth":
            self._finish_synth(run_id, t, sub)
            return False
        if t.tier == "consequential":
            did = self._request_decision(run_id, task_id, "review", f"review {task_id} ({seat.name}): {sub['summary'][:300]}")
            self.ledger.append("waiting", run=run_id, task=task_id, actor=seat.name, triggers=[f"decision:{did}"],
                               reason=f"{self.cfg.governor} reviews consequential work")
            return False
        verdict = await self._peer_review(run_id, task_id, seat, sub, budget)
        if verdict is None:
            return False
        if verdict["verdict"] == "pass":
            self._complete(run_id, task_id)
            return False
        return self._rework(run_id, task_id, f"Review by {seat.reviewers[0]} failed", [verdict["feedback"]], tx)

    async def _seat_loop(self, run_id, t: TaskView, seat: Seat, mode: str, tx: Transcript, executor: Executor,
                         budget: Budget, extra_meta: dict | None = None) -> SeatResult:
        run_goal = self.view(run_id).goal

        def on_turn(turn: Turn) -> float:
            cost = self._cost(turn)
            self._run_cost[run_id] += cost
            u = turn.usage
            self.ledger.append("turn", run=run_id, task=t.id, actor=seat.name, mode=mode, model=turn.model,
                               input=u.input_tokens, output=u.output_tokens, cache_read=u.cache_read,
                               cache_write=u.cache_write, cost=round(cost, 6), stop=turn.stop_reason)
            return cost

        meta = {
            "run": run_id, "task": t.id, "seat": seat.name, "mode": mode,
            "goal": run_goal if mode in ("plan", "synth") else t.goal,
            "writes": list(seat.writes), "acceptance": list(t.acceptance),
            "workers": [s.name for s in self.cfg.workers], **(extra_meta or {}),
        }
        return await run_seat(
            self.provider, model=seat.model, effort=seat.effort, max_tokens=self.cfg.max_tokens,
            system=seat_system(self.cfg, seat), transcript=tx, executor=executor, budget=budget,
            meta=meta, on_turn=on_turn,
        )

    def _executor(self, run_id: str, t: TaskView, seat: Seat, mode: str, acceptance: list[str] | None = None) -> Executor:
        hooks = Hooks(
            queue_action=lambda action, summary: self._queue_action(run_id, t.id, action, summary),
            open_wait=lambda trigger, reason: self._request_decision(run_id, t.id, "wait", f"{trigger} ({reason})" if reason else trigger),
            delegate=lambda to, goal, acc: self._delegate(run_id, t.id, to, goal, acc),
            plan_problems=self._plan_problems,
            log=lambda tool, ok, summary: self.ledger.append("tool", run=run_id, task=t.id, actor=seat.name, tool=tool, ok=ok, summary=summary),
        )
        return Executor(self.cfg, seat, mode, hooks, acceptance=acceptance)

    def _brief(self, run: RunView, t: TaskView) -> str:
        if t.mode == "plan":
            return plan_brief(self.cfg, run)
        if t.mode == "synth":
            return synth_brief(self.cfg, run)
        return work_brief(self.cfg, t, [run.tasks[d] for d in t.deps])

    def _resume_note(self, run: RunView, t: TaskView) -> str:
        gov = self.cfg.governor
        if t.status in ("running", "review"):
            return "The harness restarted mid-task. Check the workspace state, then continue."
        if t.status == "pending" and t.escalation_decision:
            d = run.decisions[t.escalation_decision]
            return f"{gov} reviewed your escalation and says: {d.text}\nYour fix-cycle count is reset. Continue the task."
        notes = []
        for trig in t.triggers:
            kind, _, ref = trig.partition(":")
            if kind == "task":
                sub = run.tasks[ref]
                notes.append(f"Subtask {ref} ({sub.seat}) finished with status {sub.status}.\n{_handoff(sub)}")
                continue
            d = run.decisions[ref]
            if d.kind == "action":
                if d.status == "granted":
                    ran = d.executed or {}
                    notes.append(f"{gov} approved {d.id} (\"{d.text}\"). The harness ran it: {ran.get('result', 'no result recorded')}")
                else:
                    notes.append(f"{gov} denied {d.id} ({d.summary}): {d.text}\nDon't repeat that action unless asked; adapt.")
            elif d.kind == "wait":
                notes.append(f"{gov} resolved your wait ({d.summary}): {d.text}")
            elif d.kind == "review":
                notes.append(f"{gov} reviewed your submission and sent it back: {d.text}\nFix this and submit again.")
        return "\n\n".join(notes)

    # --- gates and judgment -------------------------------------------------------

    async def _mechanical_gates(self, run_id: str, t: TaskView, seat: Seat, sub: dict) -> list[str]:
        """Mechanical blocks first. Work only reaches a judge after these pass."""
        problems: list[str] = []
        texts = [("summary", sub["summary"])]
        for a in sub["artifacts"]:
            try:
                target, norm = pol.resolve(self.cfg, a)
            except pol.PolicyError as e:
                problems.append(str(e))
                continue
            if not target.is_file():
                problems.append(f"artifact {norm} does not exist")
                continue
            try:
                texts.append((norm, target.read_text(encoding="utf-8")))
            except UnicodeDecodeError:
                pass  # binary artifacts skip the text scanners
        self._gate(run_id, t, "artifacts", problems)

        secrets = [f for where, text in texts for f in pol.scan_secrets(text, where)]
        problems += self._gate(run_id, t, "secret_scan", [f"possible secret - {f}" for f in secrets])
        if self.cfg.policy.pii_scan and t.content_class not in self.cfg.policy.redline_classes:
            pii = [f for where, text in texts for f in pol.scan_pii(text, where, self.cfg.policy.pii_allow)]
            problems += self._gate(run_id, t, "pii_scan", [f"possible PII - {f}" for f in pii])
        for check in seat.checks:
            code, out = await run_argv(self.cfg, pol.parse_command(check))
            problems += self._gate(run_id, t, f"check: {check}", [] if code == 0 else [f"`{check}` exited {code}:\n{out[-2000:]}"])
        return problems

    def _gate(self, run_id: str, t: TaskView, name: str, findings: list[str]) -> list[str]:
        self.ledger.append("gate", run=run_id, task=t.id, actor=HARNESS, gate=name, passed=not findings, findings=findings[:20])
        return findings

    async def _peer_review(self, run_id: str, task_id: str, builder: Seat, sub: dict, budget: Budget) -> dict | None:
        reviewer = self.cfg.seats[builder.reviewers[0]]
        if reviewer.name == builder.name:  # config validation forbids this; never trust it at runtime either
            raise HarnessError(f"{builder.name} cannot review its own work")
        t = self.task(run_id, task_id)
        n = len(t.reviews) + 1
        self.ledger.append("review_started", run=run_id, task=task_id, actor=reviewer.name, reviewer=reviewer.name, n=n)
        # A fresh transcript per review keeps the judge independent of earlier rounds.
        tx = Transcript(self.run_dir(run_id) / "transcripts" / f"{task_id}.review{n}.json")
        tx.add_user(review_brief(t, builder.name, sub))
        executor = self._executor(run_id, t, reviewer, "review", acceptance=t.acceptance)
        result = await self._seat_loop(run_id, t, reviewer, "review", tx, executor, budget, {"artifacts": sub["artifacts"]})
        if result.kind != "verdict":
            self._escalate(run_id, task_id, f"review by {reviewer.name} did not finish: {result.data.get('reason', result.kind)}")
            return None
        self.ledger.append("review", run=run_id, task=task_id, actor=reviewer.name, reviewer=reviewer.name, **result.data)
        return result.data

    def _rework(self, run_id: str, task_id: str, source: str, problems: list[str], tx: Transcript) -> bool:
        feedback = f"{source}:\n- " + "\n- ".join(problems)
        if not self._record_fix_cycle(run_id, task_id, feedback):
            return False
        tx.add_user(f"{feedback}\n\nFix this and submit again.")
        return True

    def _record_fix_cycle(self, run_id: str, task_id: str, feedback: str) -> bool:
        """Count a failed verify->fix cycle. At the cap the task escalates instead of looping."""
        self.ledger.append("fix_cycle", run=run_id, task=task_id, actor=HARNESS, feedback=feedback)
        t = self.task(run_id, task_id)
        if t.fix_cycles >= self.cfg.budgets.max_fix_cycles:
            self._escalate(run_id, task_id, f"{t.fix_cycles} verify->fix cycles failed. Last feedback:\n{feedback}")
            return False
        return True

    def _complete(self, run_id: str, task_id: str) -> None:
        self.ledger.append("task_done", run=run_id, task=task_id, actor=HARNESS)
        t = self.task(run_id, task_id)
        if len(t.submissions) < 2:
            return
        if t.content_class in self.cfg.policy.redline_classes:
            self.ledger.append("trace_skipped", run=run_id, task=task_id, actor=HARNESS, reason=f"red-line class {t.content_class}")
            return
        # A corrected result is training signal: keep original, final, and why it changed.
        trace = {
            "trace": f"{run_id}.{task_id}", "run": run_id, "task": task_id, "seat": t.seat, "goal": t.goal,
            "original": t.submissions[0], "final": t.submissions[-1], "why": t.feedback,
            "snapshots": str(self.run_dir(run_id) / "snapshots" / task_id),
            "stamp": {"harness": __version__, "config": self.cfg.hash[:12], "model": self.cfg.seats[t.seat].model},
        }
        with open(self.cfg.state_dir / "traces.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")
        self.ledger.append("trace", run=run_id, task=task_id, actor=HARNESS, trace=trace["trace"])

    def _snapshot(self, run_id: str, t: TaskView, n: int, sub: dict) -> None:
        """Keep each submission's artifacts as they were, so later edits can't rewrite history."""
        if t.content_class in self.cfg.policy.redline_classes:
            return
        dest = self.run_dir(run_id) / "snapshots" / t.id / str(n)
        for a in sub["artifacts"]:
            try:
                src, norm = pol.resolve(self.cfg, a)
            except pol.PolicyError:
                continue
            if src.is_file() and src.stat().st_size <= 1_000_000:
                (dest / norm).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest / norm)

    # --- planning and delegation ----------------------------------------------------

    def _plan_problems(self, tasks: list[dict]) -> list[str]:
        p: list[str] = []
        workers = ", ".join(s.name for s in self.cfg.workers)
        if not tasks:
            p.append("the plan has no tasks")
        if len(tasks) > self.cfg.budgets.max_tasks:
            p.append(f"{len(tasks)} tasks is over the cap of {self.cfg.budgets.max_tasks}")
        keys = [x["key"].strip() for x in tasks]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            p.append(f"duplicate keys: {', '.join(dupes)}")
        for i, x in enumerate(tasks, 1):
            label = x["key"].strip() or f"task {i}"
            if not x["key"].strip():
                p.append(f"task {i}: key is empty")
            seat = self.cfg.seats.get(x["seat"])
            if seat is None or seat.role != "worker":
                p.append(f"{label}: {x['seat']!r} is not a worker seat (choose from {workers})")
            if not x["goal"].strip():
                p.append(f"{label}: goal is empty")
            if not 1 <= len(x["acceptance"]) <= 6 or not all(c.strip() for c in x["acceptance"]):
                p.append(f"{label}: give 1-6 non-empty acceptance criteria")
            for dep in x["depends_on"]:
                if dep == x["key"]:
                    p.append(f"{label}: depends on itself")
                elif dep not in keys:
                    p.append(f"{label}: depends on unknown key {dep!r}")
            if x["tier"] not in TIERS:
                p.append(f"{label}: tier must be routine or consequential")
            if x["content_class"] not in self.cfg.policy.content_classes:
                p.append(f"{label}: content_class must be one of {', '.join(self.cfg.policy.content_classes)}")
        if not p:
            graph = {x["key"]: x["depends_on"] for x in tasks}
            state: dict[str, int] = {}

            def cyclic(k: str) -> bool:
                if state.get(k) == 1:
                    return True
                if state.get(k) == 2:
                    return False
                state[k] = 1
                if any(cyclic(d) for d in graph[k]):
                    return True
                state[k] = 2
                return False

            if any(cyclic(k) for k in graph):
                p.append("dependencies form a cycle")
        return p

    def _accept_plan(self, run_id: str, plan_task: TaskView, data: dict) -> None:
        run = self.view(run_id)
        ids = {x["key"]: self.ledger.allocate("T") for x in data["tasks"]}
        for x in data["tasks"]:
            self._create_task(
                run_id, task_id=ids[x["key"]], actor=plan_task.seat, seat=x["seat"], mode="work", goal=x["goal"],
                acceptance=x["acceptance"], deps=[ids[d] for d in x["depends_on"]], tier=x["tier"],
                content_class=x["content_class"], requested_by=run.requested_by, parent=plan_task.id, key=x["key"],
            )
        self.ledger.append("plan", run=run_id, task=plan_task.id, actor=plan_task.seat, rationale=data["rationale"], tasks=list(ids.values()))

    def _delegate(self, run_id: str, parent_id: str, seat_name: str, goal: str, acceptance: list[str]) -> str:
        run = self.view(run_id)
        parent = run.tasks[parent_id]
        seat = self.cfg.seats.get(seat_name)
        if seat is None or seat.role != "worker":
            raise pol.PolicyError(f"{seat_name!r} is not a worker seat (choose from {', '.join(s.name for s in self.cfg.workers)})")
        if seat_name == parent.seat:
            raise pol.PolicyError("you can't delegate to your own seat; do the work or split it differently")
        if parent.depth + 1 > self.cfg.budgets.max_delegation_depth:
            raise pol.PolicyError(f"delegation depth limit ({self.cfg.budgets.max_delegation_depth}) reached")
        if len(run.work) >= self.cfg.budgets.max_tasks:
            raise pol.PolicyError(f"the run's task cap ({self.cfg.budgets.max_tasks}) is reached")
        if not goal.strip() or not 1 <= len(acceptance) <= 6 or not all(c.strip() for c in acceptance):
            raise pol.PolicyError("a subtask needs a goal and 1-6 non-empty acceptance criteria")
        # Authority flows from the original brief: routing work doesn't create a new principal.
        return self._create_task(
            run_id, actor=parent.seat, seat=seat_name, mode="work", goal=goal, acceptance=acceptance, deps=[],
            tier="routine", content_class=parent.content_class, requested_by=parent.requested_by,
            parent=parent_id, depth=parent.depth + 1,
        )

    def _create_task(self, run_id: str, *, actor: str, task_id: str | None = None, **fields) -> str:
        task_id = task_id or self.ledger.allocate("T")
        self.ledger.append("task_created", run=run_id, task=task_id, actor=actor, **fields)
        return task_id

    # --- the governor's queue -------------------------------------------------------

    def _request_decision(self, run_id: str, task_id: str, kind: str, summary: str, action_hash: str = "") -> str:
        did = self.ledger.allocate("D")
        self.ledger.append("decision_requested", run=run_id, task=task_id, actor=HARNESS, decision=did, kind=kind,
                           summary=summary, action_hash=action_hash)
        return did

    def _queue_action(self, run_id: str, task_id: str, action: dict, summary: str) -> str:
        digest = hashlib.sha256(_canonical(action)).hexdigest()
        did = self.ledger.allocate("D")
        path = self.run_dir(run_id) / "actions" / f"{did}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(action, ensure_ascii=False, indent=1))
        self.ledger.append("decision_requested", run=run_id, task=task_id, actor=HARNESS, decision=did, kind="action",
                           summary=summary, action_hash=digest)
        return did

    async def _execute_action(self, run_id: str, d: DecisionView) -> None:
        action = self.action_payload(run_id, d.id)
        if action is None or hashlib.sha256(_canonical(action)).hexdigest() != d.action_hash:
            ok, result = False, "the queued action changed after it was requested, so it was not executed"
        elif action["tool"] == "write_file":
            try:
                target, norm = pol.resolve(self.cfg, action["path"])
                write_text(target, action["content"])
                ok, result = True, f"wrote {norm} ({len(action['content']):,} chars)"
            except pol.PolicyError as e:
                ok, result = False, f"not executed: {e}"
        else:
            code, out = await run_argv(self.cfg, action["argv"])
            ok, result = code == 0, f"exit code {code}\n{out[-2000:]}"
        self.ledger.append("action_executed", run=run_id, task=d.task, actor=HARNESS, decision=d.id,
                           action_hash=d.action_hash, ok=ok, result=result)

    def _escalate(self, run_id: str, task_id: str, reason: str) -> None:
        did = self._request_decision(run_id, task_id, "escalation", reason[:2000])
        self.ledger.append("escalated", run=run_id, task=task_id, actor=HARNESS, reason=reason, decision=did)

    # --- finishing ------------------------------------------------------------------

    def _finish_synth(self, run_id: str, t: TaskView, sub: dict) -> None:
        self.ledger.append("task_done", run=run_id, task=t.id, actor=t.seat)
        run = self.view(run_id)
        rows = "\n".join(
            f"| {w.id} | {w.seat} | {w.status} | {', '.join((w.submission or {}).get('artifacts', [])) or '-'} |" for w in run.work
        )
        body = (
            f"# {run.goal}\n\n_Run {run_id} · requested by {run.requested_by} · ${run.cost:.2f}_\n\n{sub['summary']}\n\n"
            f"## Work behind this answer\n\n| Task | Seat | Status | Artifacts |\n|---|---|---|---|\n{rows}\n"
        )
        path = self.run_dir(run_id) / "RESULT.md"
        path.write_text(body, encoding="utf-8")
        status = "done" if all(w.status == "done" for w in run.work) else "partial"
        self._finish(run_id, status, str(path))

    def _finish(self, run_id: str, status: str, result: str) -> None:
        self.ledger.append("run_finished", run=run_id, actor=HARNESS, status=status, result=result)

    def _pause(self, run: RunView, reason: str) -> None:
        if run.paused != reason:
            self.ledger.append("run_paused", run=run.id, actor=HARNESS, reason=reason)

    def _check_config(self, run: RunView) -> None:
        if run.config_hash != self.cfg.hash:
            raise ConfigDrift(
                f"{run.id} started under a different swarm.yaml. Resume with its snapshot (the default), "
                "or pass --adopt-config to move it to the current rules."
            )

    def _cost(self, turn: Turn) -> float:
        p_in, p_out, p_read, p_write = self.cfg.price(turn.model)
        u = turn.usage
        return (u.input_tokens * p_in + u.output_tokens * p_out + u.cache_read * p_read + u.cache_write * p_write) / 1e6
