"""Plain-text views for the terminal: status board, inbox, run list, event log."""

from __future__ import annotations

from .state import RunView, check_event

ICON = {"pending": "·", "running": "▶", "review": "◎", "waiting": "⏸", "done": "✓", "escalated": "!", "parked": "–"}


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def status(run: RunView, governor: str) -> str:
    work = run.work
    done = sum(1 for t in work if t.status == "done")
    lines = [
        f"{run.id}  {_clip(run.goal, 80)}",
        f"   status: {run.status}   requested by {run.requested_by}   cost ${run.cost:.2f} over {run.turns} turns   tasks {done}/{len(work)} done",
    ]
    if run.paused and not run.finished:
        lines.append(f"   paused: {run.paused}")
    if run.result:
        lines.append(f"   result: {run.result}")
    lines.append("")
    for t in sorted(run.tasks.values(), key=lambda t: int(t.id[1:])):
        note = ""
        if t.status == "waiting":
            note = "waiting on " + ", ".join(t.triggers)
        elif t.status == "escalated":
            note = f"escalated ({t.escalation_decision}): {_clip(t.escalation, 60)}"
        elif t.status == "pending" and t.deps:
            note = "after " + ", ".join(t.deps)
        elif t.status == "done" and t.reviews:
            note = f"passed review by {t.reviews[-1]['reviewer']}"
        if t.fix_cycles:
            note += f"  [{t.fix_cycles} fix cycle{'s' if t.fix_cycles > 1 else ''}]"
        label = t.mode if t.mode != "work" else _clip(t.goal, 48)
        lines.append(f"  {ICON.get(t.status, '?')} {t.id:<5} {t.status:<9} {t.seat:<12} {label:<48} {note}".rstrip())
    if run.inbox:
        lines += ["", f"Needs {governor}:"] + [f"  {d.id:<5} {d.kind:<10} {d.task:<5}  {_clip(d.summary, 90)}" for d in run.inbox]
        lines.append("  -> swarm inbox --show <id>, then swarm approve <id> \"approved\" or swarm deny <id> \"reason\"")
    return "\n".join(lines)


def run_list(runs: dict[str, RunView]) -> str:
    if not runs:
        return "No runs yet. Start one with: swarm run \"<goal>\""
    rows = [f"  {r.id:<5} {r.status:<15} ${r.cost:>6.2f}  {_clip(r.goal, 70)}" for r in runs.values()]
    return "\n".join(rows)


def inbox(runs: dict[str, RunView], governor: str) -> str:
    items = [(r, d) for r in runs.values() for d in r.inbox]
    if not items:
        return f"Nothing waiting on {governor}."
    lines = [f"Waiting on {governor}:"]
    for r, d in items:
        lines.append(f"  {d.id:<5} {r.id:<5} {d.task:<5} {d.kind:<10} {_clip(d.summary, 90)}")
    return "\n".join(lines)


def _detail(e: dict) -> str:
    d, typ = e["data"], e["type"]
    if typ == "turn":
        return f"{d['mode']} {d['model']} in={d['input']} out={d['output']} cache_read={d['cache_read']} ${d['cost']:.4f}"
    if typ == "tool":
        return f"{d['tool']} {'ok' if d['ok'] else 'REFUSED/ERROR'} {d['summary']}"
    if typ == "gate":
        return f"{d['gate']} {'pass' if d['passed'] else 'FAIL'} {'; '.join(d['findings'])[:160]}"
    if typ == "review":
        return f"{d['reviewer']} -> {d['verdict']} {_clip(d.get('feedback', ''), 100)}"
    if typ == "decision_requested":
        return f"{d['decision']} {d['kind']}: {_clip(d['summary'], 100)}"
    if typ == "decided":
        return f"{d['decision']} {'GRANTED' if d['granted'] else 'DENIED'} \"{_clip(d['text'], 80)}\""
    if typ == "waiting":
        return ", ".join(d["triggers"])
    if typ == "submitted":
        return f"#{d['n']} {_clip(d['summary'], 100)}"
    return _clip(", ".join(f"{k}={v}" for k, v in d.items()), 120)


def log(events: list[dict]) -> str:
    lines = []
    for raw in events:
        e, issues = check_event(raw)
        if e is None:
            lines.append(f"{'?':>5} unreadable event: {'; '.join(issues)}")
            continue
        try:
            detail = _detail(e)
        except (KeyError, TypeError, ValueError):
            detail = _clip(", ".join(f"{k}={v}" for k, v in e["data"].items()), 120)
        if issues:
            detail += f"  [malformed: {'; '.join(issues)}]"
        lines.append(f"{e['seq']:>5} {e['ts'][11:19]} {e['run']:<4} {(e['task'] or ''):<5} {e['actor']:<12} {e['type']:<18} {detail}")
    return "\n".join(lines)
