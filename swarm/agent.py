"""One seat working one task: the tool-use loop, its transcript, and its budget."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .providers import Provider, Turn
from .tools import Control, Executor

MAX_NUDGES = 2


class Transcript:
    """A seat's conversation for one task, checkpointed to disk after every step.

    History is append-only: earlier turns are never edited, which keeps the
    prompt cache warm and thinking blocks valid across resumes.
    """

    def __init__(self, path: Path, messages: list[dict] | None = None):
        self.path = path
        self.messages: list[dict] = messages or []

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        if path.exists():
            return cls(path, json.loads(path.read_text())["messages"])
        return cls(path)

    @property
    def is_new(self) -> bool:
        return not self.messages

    def add_user(self, content: str | list) -> None:
        self.messages.append({"role": "user", "content": content})
        self.save()

    def add_assistant(self, content: list[dict]) -> None:
        self.messages.append({"role": "assistant", "content": content})
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": self.messages}, ensure_ascii=False))
        os.replace(tmp, self.path)

    def repair(self) -> None:
        """After a crash between a tool call and its result, answer the orphaned calls honestly."""
        if self.messages and self.messages[-1]["role"] == "assistant":
            calls = [b for b in self.messages[-1]["content"] if b.get("type") == "tool_use"]
            if calls:
                self.add_user([
                    {"type": "tool_result", "tool_use_id": c["id"], "is_error": True,
                     "content": "The harness restarted before this call's result was recorded. Check the workspace before retrying."}
                    for c in calls
                ])


@dataclass
class Budget:
    turn_cap: int
    cost_cap: float
    run_cost_cap: float
    turns: int = 0
    cost: float = 0.0
    run_cost: Callable[[], float] = field(default=lambda: 0.0)

    def exhausted(self) -> str | None:
        if self.turns >= self.turn_cap:
            return f"turn budget used up ({self.turns}/{self.turn_cap} turns)"
        if self.cost >= self.cost_cap:
            return f"task cost budget used up (${self.cost:.2f} of ${self.cost_cap:.2f})"
        if self.run_cost() >= self.run_cost_cap:
            return f"run cost cap reached (${self.run_cost():.2f} of ${self.run_cost_cap:.2f})"
        return None


@dataclass
class SeatResult:
    kind: str  # submit | plan | verdict | pause | escalate
    data: dict = field(default_factory=dict)
    triggers: list[str] = field(default_factory=list)


async def run_seat(
    provider: Provider,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    system: str,
    transcript: Transcript,
    executor: Executor,
    budget: Budget,
    meta: dict,
    on_turn: Callable[[Turn], float],
) -> SeatResult:
    nudges = 0
    tools = executor.definitions()
    finishers = " or ".join(executor.finishers)
    while True:
        reason = budget.exhausted()
        if reason:
            return SeatResult("escalate", {"reason": reason})
        turn = await provider.complete(
            model=model, effort=effort, max_tokens=max_tokens, system=system,
            messages=transcript.messages, tools=tools, meta=meta,
        )
        budget.turns += 1
        budget.cost += on_turn(turn)
        transcript.add_assistant(turn.content)

        if turn.stop_reason == "refusal":
            return SeatResult("escalate", {"reason": f"the model declined this task (category: {turn.refusal_category or 'unspecified'})"})

        calls = turn.tool_calls
        if turn.stop_reason == "max_tokens":
            note = "Your response hit the output limit and was cut off. Continue in smaller steps."
            if calls:  # a cut-off tool call may have truncated input; never run it
                transcript.add_user([{"type": "tool_result", "tool_use_id": c.id, "is_error": True, "content": note} for c in calls])
            else:
                transcript.add_user(note)
            continue
        if not calls:
            nudges += 1
            if nudges > MAX_NUDGES:
                return SeatResult("escalate", {"reason": f"the seat ended {nudges} turns without calling {finishers}"})
            transcript.add_user(f"Continue the task. When you're done, call {finishers}.")
            continue

        results, controls = [], []
        for call in calls:
            out = await executor.execute(call.name, call.input)
            block = {"type": "tool_result", "tool_use_id": call.id, "content": out.content}
            if out.is_error:
                block["is_error"] = True
            results.append(block)
            if out.control:
                controls.append(out.control)
        transcript.add_user(results)
        if controls:
            return _resolve(controls)


def _resolve(controls: list[Control]) -> SeatResult:
    """Several finishing calls in one turn: escalation wins, then pauses (all triggers), then the first finisher."""
    for c in controls:
        if c.kind == "escalate":
            return SeatResult("escalate", c.data)
    pauses = [c for c in controls if c.kind == "pause"]
    if pauses:
        triggers = [t for c in pauses for t in c.triggers]
        return SeatResult("pause", {"reason": "; ".join(c.data.get("reason", "") for c in pauses)}, triggers)
    c = controls[0]
    return SeatResult(c.kind, c.data)
