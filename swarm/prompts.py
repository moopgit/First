"""Prompt text. The system prompt per seat is static (cacheable); everything
task-specific goes in the first user message."""

from __future__ import annotations

from .config import Config, Seat
from .state import RunView, TaskView


def seat_system(cfg: Config, seat: Seat) -> str:
    lanes = ", ".join(seat.writes) if seat.writes else "nothing (read-only seat)"
    structural = ", ".join(cfg.policy.structural_paths + cfg.policy.structural_commands) or "none configured"
    checks = "; ".join(seat.checks)
    return f"""You are the {seat.name} seat on a team of AI agents run by the swarm harness. {cfg.governor} is the governor: the only human on the team and the only one who approves.

Your lane: {seat.lane}
{seat.charter.strip()}

How the harness works (enforced in code, so plan around it rather than test it):
- You act only through tools. You can read the whole workspace except private paths. You can write only to: {lanes}.
- Structural actions ({structural}) don't run when you call them. They are queued for {cfg.governor}'s explicit approval and your task pauses. If approved, the harness performs exactly the action you requested.
- Finished work goes through mechanical checks first (artifacts exist, secret scan, PII scan{', ' + checks if checks else ''}), then review by a different seat against the task's acceptance criteria. Failing either returns the work to you with feedback. After {cfg.budgets.max_fix_cycles} failed cycles the task goes to {cfg.governor}.
- If you're blocked on something outside the workspace, call wait with a concrete resume trigger. If something looks unsafe, unethical, or contradicts your instructions, call escalate. Stopping is always allowed.
- Work from evidence in the workspace and your task inputs. When you can't verify something, say so plainly instead of guessing.
- Write summaries for a busy reader: what you did, where it is, what is unverified, and what should happen next. Plain words, no filler."""


def plan_brief(cfg: Config, run: RunView) -> str:
    team = "\n".join(f"- {s.name}: {s.lane}" for s in cfg.workers)
    classes = ", ".join(cfg.policy.content_classes)
    return f"""Goal from {run.requested_by}:
{run.goal}

Plan the work for this team, then call submit_plan.

Team:
{team}

Each task needs:
- seat: one of the seats above.
- goal: a self-contained brief. The seat sees only this goal plus the results of the tasks it depends on.
- acceptance: 1-6 criteria a reviewer can check from the workspace. Make them concrete.
- depends_on: keys of tasks whose results it needs. Independent tasks run in parallel, so only add a dependency when the result is truly needed.
- tier: "routine" (a peer seat reviews) or "consequential" ({cfg.governor} reviews: use it for anything outward-facing, hard to reverse, or about money, people, or commitments).
- content_class: one of {classes}. Use "general" unless another fits.

Use the fewest tasks that cover the goal (at most {cfg.budgets.max_tasks}). You may read the workspace first to see what already exists."""


def work_brief(cfg: Config, task: TaskView, inputs: list[TaskView]) -> str:
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(task.acceptance, 1))
    parts = [f"Task {task.id} (requested by {task.requested_by}):\n{task.goal}", f"Acceptance criteria:\n{criteria}"]
    if inputs:
        parts.append("Results you depend on:\n\n" + "\n\n".join(_handoff(t) for t in inputs))
    parts.append("When the work meets every criterion, call submit.")
    return "\n\n".join(parts)


def review_brief(task: TaskView, builder: str, submission: dict) -> str:
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(task.acceptance, 1))
    artifacts = "\n".join(f"- {a}" for a in submission["artifacts"]) or "- (none)"
    history = ""
    if task.feedback:
        history = "\n\nEarlier feedback on this task (check it was addressed):\n" + "\n".join(f"- {f}" for f in task.feedback)
    return f"""Review task {task.id}, done by the {builder} seat. You are the independent check; the builder never grades their own work.

The task:
{task.goal}

Acceptance criteria:
{criteria}

The builder's summary:
{submission['summary']}

Artifacts:
{artifacts}{history}

Read the artifacts and check each criterion against the evidence, not the summary. Then call submit_verdict with one entry per criterion, in order. Pass only if every criterion is met."""


def synth_brief(cfg: Config, run: RunView) -> str:
    done = [t for t in run.work if t.status == "done"]
    missing = [t for t in run.work if t.status != "done"]
    parts = [
        f"Goal from {run.requested_by}:\n{run.goal}",
        "The team's reviewed results:\n\n" + ("\n\n".join(_handoff(t) for t in done) or "(none)"),
    ]
    if missing:
        parts.append("Not completed (say so in your summary):\n" + "\n".join(f"- {t.id} {t.seat}: {t.status} - {t.goal[:120]}" for t in missing))
    parts.append(
        f"Write the final answer to {run.requested_by} and call submit. Your summary is the deliverable: lead with the "
        "answer, point to the artifacts that support it, and flag anything unverified or incomplete."
    )
    return "\n\n".join(parts)


def _handoff(t: TaskView) -> str:
    sub = t.submission or {"summary": "(no submission)", "artifacts": []}
    files = ", ".join(sub["artifacts"]) or "none"
    return f"[{t.id} · {t.seat}] {t.goal[:200]}\nSummary: {sub['summary']}\nArtifacts: {files}"
