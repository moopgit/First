from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from swarm.config import Config, load_config
from swarm.orchestrator import Orchestrator
from swarm.providers import ScriptedProvider

BASE = """
governor: Andy
workspace: ws
state_dir: state
concurrency: 4
budgets: {max_fix_cycles: 3, run_cost_usd: 50, task_cost_usd: 10, task_turns: 30}
policy:
  structural_paths: ["outbound/*"]
  structural_commands: ["git push"]
  pii_allow: ['@example\\.com$']
seats:
  lead:
    role: router
    lane: Routing
    tools: [read_file, list_files, search_files, write_file]
    writes: ["final/*"]
  research:
    lane: Research
    tools: [read_file, list_files, search_files, write_file, delegate]
    writes: ["research/*"]
    reviewers: [checker]
  writer:
    lane: Writing
    writes: ["drafts/*", "outbound/*"]
    reviewers: [checker]
  checker:
    role: reviewer
    lane: Review
"""


def make_cfg(tmp_path: Path, text: str = BASE) -> Config:
    path = tmp_path / "swarm.yaml"
    path.write_text(text)
    cfg = load_config(path)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_cfg(tmp_path)


# --- script helpers ----------------------------------------------------------------


def task(key, seat, goal="do the thing", deps=(), tier="routine", cls="general", acceptance=("criterion one",)):
    return {"key": key, "seat": seat, "goal": goal, "acceptance": list(acceptance), "depends_on": list(deps),
            "tier": tier, "content_class": cls}


def plan(*tasks):
    return {"tool": "submit_plan", "input": {"rationale": "because", "tasks": list(tasks)}}


def write(path, content):
    return {"tool": "write_file", "input": {"path": path, "content": content}}


def submit(summary="done", artifacts=()):
    return {"tool": "submit", "input": {"summary": summary, "artifacts": list(artifacts)}}


def verdict(meta, ok=True, feedback=""):
    crit = [{"criterion": c, "met": ok, "evidence": "looked"} for c in meta["acceptance"]]
    return {"tool": "submit_verdict", "input": {"verdict": "pass" if ok else "fail", "criteria": crit, "feedback": feedback}}


def turns(messages) -> int:
    return sum(1 for m in messages if m["role"] == "assistant")


def last_results(messages) -> list[dict]:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            return m["content"]
    return []


def transcript_text(messages) -> str:
    out = []
    for m in messages:
        if isinstance(m["content"], str):
            out.append(m["content"])
        else:
            out += [str(b.get("content", "")) + str(b.get("text", "")) + str(b.get("input", "")) for b in m["content"]]
    return "\n".join(out)


def orch(cfg, script, delay=0.0) -> Orchestrator:
    return Orchestrator(cfg, ScriptedProvider(script, delay=delay))


def run(o: Orchestrator, goal="ship it", by="Andy"):
    rid = o.start(goal, by)
    return rid, asyncio.run(o.drive(rid))


def events(o: Orchestrator, typ=None, task=None):
    return [e for e in o.ledger.events() if (typ is None or e["type"] == typ) and (task is None or e["task"] == task)]
