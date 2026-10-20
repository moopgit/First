"""End-to-end runs against a scripted team. Each test pins one rule the harness enforces."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import (
    BASE, events, last_results, make_cfg, orch, plan, run, submit, task, transcript_text, turns, verdict, write,
)

from swarm.audit import audit
from swarm.orchestrator import AmbiguousApproval, ConfigDrift, HarnessError, Orchestrator, RunBusy
from swarm.provenance import build, invoker, principal
from swarm.providers import AnthropicProvider, CredentialMismatch, MissingCredentials, ProviderFatal, ScriptedProvider


def audit_ok(o: Orchestrator) -> None:
    failing = [c for c in audit(o.ledger, governor=o.cfg.governor, max_fix_cycles=o.cfg.budgets.max_fix_cycles) if not c.ok]
    assert not failing, [(c.name, c.problems) for c in failing]


def simple_worker(meta, messages):
    lane = meta["writes"][0].split("*")[0]
    if turns(messages) == 0:
        return write(f"{lane}{meta['task']}.md", f"work for {meta['goal']}")
    return submit(f"finished {meta['task']}", [f"{lane}{meta['task']}.md"])


def passing_reviewer(meta, messages):
    return verdict(meta)


def team(plan_turn, worker=simple_worker, reviewer=passing_reviewer):
    def script(meta, messages):
        if meta["mode"] == "plan":
            return plan_turn(meta, messages) if callable(plan_turn) else plan_turn
        if meta["mode"] == "review":
            return reviewer(meta, messages)
        if meta["mode"] == "synth":
            return submit("final answer")
        return worker(meta, messages)
    return script


# --- parallelism and dependencies -------------------------------------------------------


def test_independent_tasks_run_in_parallel_and_dependents_get_handoffs(cfg):
    active, peak = [0], [0]
    briefs = {}

    async def script(meta, messages):
        if meta["mode"] == "plan":
            return plan(task("a", "research"), task("b", "writer"), task("c", "writer", deps=["a", "b"]))
        if meta["mode"] == "work" and turns(messages) == 0:
            briefs[meta["task"]] = messages[0]["content"]
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.02)
        active[0] -= 1
        return team(None)(meta, messages)

    o = orch(cfg, script)
    rid, view = run(o)
    assert view.status == "done"
    assert peak[0] >= 2, "independent tasks should overlap"
    a, b, c = (t for t in view.work)
    started_c = next(e["seq"] for e in events(o, "task_started", c.id))
    assert all(next(e["seq"] for e in events(o, "task_done", t.id)) < started_c for t in (a, b))
    assert f"finished {a.id}" in briefs[c.id] and f"finished {b.id}" in briefs[c.id]
    assert (cfg.state_dir / "runs" / rid / "RESULT.md").read_text().startswith("# ship it")
    audit_ok(o)


def test_invalid_plan_is_returned_to_the_router_with_problems(cfg):
    def planner(meta, messages):
        if turns(messages) == 0:
            return plan(task("x", "nobody"), task("y", "research", deps=["z"]), task("p", "writer", deps=["q"]), task("q", "writer", deps=["p"]))
        return plan(task("x", "research"))

    o = orch(cfg, team(planner))
    _, view = run(o)
    first = o.provider.calls[1]["messages"]
    err = last_results(first)[0]
    assert err["is_error"] and "'nobody' is not a worker seat" in err["content"] and "unknown key 'z'" in err["content"]
    assert view.status == "done" and len(view.work) == 1


def test_cycle_in_plan_is_rejected(cfg):
    o = orch(cfg, lambda m, msgs: None)
    assert o._plan_problems([task("p", "writer", deps=["q"]), task("q", "writer", deps=["p"])]) == ["dependencies form a cycle"]


# --- gates and judgment -------------------------------------------------------------


def test_review_failure_returns_feedback_then_pass_records_a_trace(cfg):
    def reviewer(meta, messages):
        prior = [e for e in events(o, "review", meta["task"])]
        return verdict(meta, ok=bool(prior), feedback="cite a source for the market size")

    o = orch(cfg, team(plan(task("a", "research")), reviewer=reviewer))
    rid, view = run(o)
    t = view.work[0]
    assert view.status == "done" and t.fix_cycles == 1 and len(t.submissions) == 2
    builder = json.loads((cfg.state_dir / "runs" / rid / "transcripts" / f"{t.id}.json").read_text())["messages"]
    assert "cite a source for the market size" in transcript_text(builder)
    trace = json.loads((cfg.state_dir / "traces.jsonl").read_text().splitlines()[0])
    assert trace["original"]["n"] == 1 and trace["final"]["n"] == 2 and "cite a source" in trace["why"][0]
    assert (cfg.state_dir / "runs" / rid / "snapshots" / t.id / "1").is_dir()
    audit_ok(o)


def test_three_failed_cycles_escalate_and_governor_guidance_resets_them(cfg):
    def worker(meta, messages):
        text = transcript_text(messages)
        lane = "research/"
        if "2025 data" in text and "2025 data used" not in text:
            return write(f"{lane}a.md", "2025 data used")
        if turns(messages) == 0:
            return write(f"{lane}a.md", "draft")
        return submit("2025 data used" if "2025 data used" in text else "draft", [f"{lane}a.md"])

    def reviewer(meta, messages):
        sub = [e for e in events(o, "submitted", meta["task"])][-1]["data"]["summary"]
        return verdict(meta, ok="2025" in sub, feedback="numbers are stale")

    o = orch(cfg, team(plan(task("a", "research")), worker=worker, reviewer=reviewer))
    rid, view = run(o)
    t = view.work[0]
    assert t.status == "escalated" and t.fix_cycles == 3 and view.status == "needs-governor"
    (d,) = view.inbox
    assert d.kind == "escalation" and "3 verify->fix cycles failed" in d.summary
    asyncio.run(o.decide(d.id, granted=True, text="go ahead, use the 2025 data"))
    view = asyncio.run(o.drive(rid))
    assert view.status == "done" and view.work[0].status == "done"
    audit_ok(o)


def test_secret_in_an_artifact_fails_the_gate_before_any_reviewer_sees_it(cfg):
    key = "sk-ant-api03-" + "x" * 32

    def worker(meta, messages):
        n = turns(messages)
        if n == 0:
            return write("research/a.md", f"config: {key}")
        if n == 2:
            return write("research/a.md", "config: read from the environment")
        return submit("done", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    _, view = run(o)
    assert view.status == "done"
    gates = [e["data"] for e in events(o, "gate") if e["data"]["gate"] == "secret_scan"]
    assert gates[0]["passed"] is False and "Anthropic API key" in gates[0]["findings"][0] and key not in gates[0]["findings"][0]
    assert len(events(o, "review_started")) == 1, "the leaked version never reached judgment"


def test_pii_gate_flags_real_addresses_but_honors_the_allowlist(cfg):
    def worker(meta, messages):
        n = turns(messages)
        if n == 0:
            return write("research/a.md", "contact jane.doe@acme.com or ops@example.com")
        if n == 2:
            return write("research/a.md", "contact ops@example.com")
        return submit("done", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    _, view = run(o)
    pii = [e["data"] for e in events(o, "gate") if e["data"]["gate"] == "pii_scan"]
    assert not pii[0]["passed"] and len(pii[0]["findings"]) == 1 and pii[1]["passed"]


def test_redline_class_skips_pii_scan_traces_and_snapshots(cfg):
    def reviewer(meta, messages):
        return verdict(meta, ok=bool(events(o, "review", meta["task"])), feedback="tighten it")

    def worker(meta, messages):
        if turns(messages) in (0, 2):
            return write("research/a.md", "candidate email: me@acme.com")
        return submit("done", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research", cls="job-search")), worker=worker, reviewer=reviewer))
    rid, view = run(o)
    assert view.status == "done"
    assert not [e for e in events(o, "gate", view.work[0].id) if e["data"]["gate"] == "pii_scan"]
    assert events(o, "trace_skipped") and not (cfg.state_dir / "traces.jsonl").exists()
    assert not (cfg.state_dir / "runs" / rid / "snapshots").exists()


def test_consequential_work_is_judged_by_the_governor_not_a_peer(cfg):
    o = orch(cfg, team(plan(task("a", "writer", tier="consequential"))))
    rid, view = run(o)
    (d,) = view.inbox
    assert d.kind == "review" and not events(o, "review_started")
    asyncio.run(o.decide(d.id, granted=False, text="add a pricing table"))
    view = asyncio.run(o.drive(rid))
    t = view.work[0]
    assert t.fix_cycles == 1 and "add a pricing table" in transcript_text(o.provider.calls[-1]["messages"])
    (d2,) = view.inbox
    asyncio.run(o.decide(d2.id, granted=True, text="approved"))
    view = asyncio.run(o.drive(rid))
    assert view.status == "done" and view.work[0].reviews[-1]["reviewer"] == "Andy"
    audit_ok(o)


# --- the governor's explicit yes ---------------------------------------------------------


def test_structural_write_waits_for_an_explicit_yes_and_runs_exactly_as_queued(cfg):
    def worker(meta, messages):
        text = transcript_text(messages)
        if turns(messages) == 0:
            return write("outbound/post.md", "Hello, world")
        assert "The harness ran it: wrote outbound/post.md" in text
        return submit("posted", ["outbound/post.md"])

    o = orch(cfg, team(plan(task("a", "writer")), worker=worker))
    rid, view = run(o)
    target = cfg.workspace / "outbound" / "post.md"
    assert not target.exists() and view.status == "needs-governor"
    (d,) = view.inbox
    assert d.kind == "action" and o.action_payload(rid, d.id) == {"tool": "write_file", "path": "outbound/post.md", "content": "Hello, world"}

    for hedged in ("should be in I think", "approved?", "maybe go", "not approved", ""):
        with pytest.raises(AmbiguousApproval):
            asyncio.run(o.decide(d.id, granted=True, text=hedged))
    assert not events(o, "decided"), "an ambiguous reply is never recorded as a decision"
    refused = events(o, "decision_refused")
    assert [e["data"]["text"] for e in refused] == ["should be in I think", "approved?", "maybe go", "not approved", ""]
    assert all(e["data"]["problem"] and e["data"]["provenance"]["user"] for e in refused)

    asyncio.run(o.decide(d.id, granted=True, text="approved"))
    assert target.read_text() == "Hello, world"
    view = asyncio.run(o.drive(rid))
    assert view.status == "done"
    with pytest.raises(HarnessError):
        asyncio.run(o.decide(d.id, granted=True, text="approved"))
    audit_ok(o)


def test_tampered_action_is_not_executed(cfg):
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/post.md", "safe text")))
    rid, view = run(o)
    (d,) = view.inbox
    path = cfg.state_dir / "runs" / rid / "actions" / f"{d.id}.json"
    path.write_text(json.dumps({"tool": "write_file", "path": "outbound/post.md", "content": "swapped"}))
    asyncio.run(o.decide(d.id, granted=True, text="approved"))
    assert not (cfg.workspace / "outbound" / "post.md").exists()
    assert events(o, "action_executed")[0]["data"]["ok"] is False


def test_denied_action_tells_the_seat_why(cfg):
    def worker(meta, messages):
        text = transcript_text(messages)
        if turns(messages) == 0:
            return write("outbound/post.md", "Hello")
        if "not public yet" in text and turns(messages) == 1:
            return write("drafts/post.md", "Hello")
        return submit("kept as a draft", ["drafts/post.md"])

    o = orch(cfg, team(plan(task("a", "writer")), worker=worker))
    rid, view = run(o)
    with pytest.raises(HarnessError):
        asyncio.run(o.decide(view.inbox[0].id, granted=False, text=""))
    asyncio.run(o.decide(view.inbox[0].id, granted=False, text="not public yet"))
    view = asyncio.run(o.drive(rid))
    assert view.status == "done" and not (cfg.workspace / "outbound" / "post.md").exists()


# --- sandbox, lanes, waits, delegation -------------------------------------------------------


def test_lanes_and_sandbox_are_enforced(cfg):
    (cfg.workspace / ".env").write_text("SECRET=1")
    attempts = [write("drafts/x.md", "no"), write("../escape.md", "no"), write("/etc/x", "no"),
                {"tool": "read_file", "input": {"path": ".env"}}, {"tool": "read_file", "input": {"path": "../swarm.yaml"}}]

    def worker(meta, messages):
        n = turns(messages)
        if n < len(attempts):
            return attempts[n]
        if n == len(attempts):
            return write("research/ok.md", "fine")
        return submit("done", ["research/ok.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    _, view = run(o)
    refused = [e for e in events(o, "tool") if not e["data"]["ok"]]
    assert len(refused) == len(attempts)
    assert view.status == "done"
    assert not (cfg.workspace / "drafts").exists() and not (cfg.base_dir / "escape.md").exists()


def test_list_files_hides_private_paths(cfg):
    (cfg.workspace / ".env").write_text("x")
    (cfg.workspace / "notes.md").write_text("x")
    seen = {}

    def worker(meta, messages):
        if turns(messages) == 0:
            return {"tool": "list_files", "input": {"path": "."}}
        seen["listing"] = last_results(messages)[0]["content"]
        return {"tool": "escalate", "input": {"reason": "test over"}}

    run(orch(cfg, team(plan(task("a", "research")), worker=worker)))
    assert "notes.md" in seen["listing"] and ".env" not in seen["listing"]


def test_delegation_pauses_the_parent_and_resumes_it_with_the_result(cfg):
    def worker(meta, messages):
        n, text = turns(messages), transcript_text(messages)
        if meta["seat"] == "research":
            if n == 0:
                return {"tool": "delegate", "input": {"seat": "research", "goal": "self", "acceptance": ["x"]}}
            if n == 1:
                assert "can't delegate to your own seat" in text
                return {"tool": "delegate", "input": {"seat": "writer", "goal": "draft the intro", "acceptance": ["has an intro"]}}
            assert "finished with status done" in text
            if n == 2:
                return write("research/a.md", "uses the intro")
            return submit("done", ["research/a.md"])
        return simple_worker(meta, messages)

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    _, view = run(o)
    parent, child = view.work
    assert view.status == "done" and child.parent == parent.id and child.depth == 1 and child.requested_by == "Andy"
    assert any("task:" + child.id in e["data"]["triggers"] for e in events(o, "waiting", parent.id))


def test_wait_needs_the_governor_and_resumes_with_their_words(cfg):
    def worker(meta, messages):
        n = turns(messages)
        if n == 0:
            return {"tool": "wait", "input": {"trigger": "Andy shares the Q3 pricing sheet", "reason": "pricing unknown"}}
        assert "it's $49/seat" in transcript_text(messages)
        return write("research/a.md", "priced at $49/seat") if n == 1 else submit("done", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    rid, view = run(o)
    (d,) = view.inbox
    assert d.kind == "wait" and view.work[0].status == "waiting"
    asyncio.run(o.decide(d.id, granted=True, text="the sheet says it's $49/seat"))  # an answer, not an approval
    assert asyncio.run(o.drive(rid)).status == "done"


# --- budgets, refusals, crashes -----------------------------------------------------------


def test_turn_budget_escalates_instead_of_looping(tmp_path):
    cfg = make_cfg(tmp_path, BASE.replace("task_turns: 30", "task_turns: 3"))
    o = orch(cfg, team(plan(task("a", "research")), worker=lambda m, msgs: {"tool": "list_files", "input": {"path": "."}}))
    _, view = run(o)
    assert view.work[0].status == "escalated" and "turn budget" in view.work[0].escalation


def test_retry_after_budget_escalation_gets_a_fresh_window(tmp_path):
    cfg = make_cfg(tmp_path, BASE.replace("task_turns: 30", "task_turns: 3"))

    def worker(meta, messages):
        text = transcript_text(messages)
        if "use research/a.md" not in text:
            return {"tool": "list_files", "input": {"path": "."}}
        if "wrote research/a.md" not in text:
            return write("research/a.md", "x")
        return submit("ok", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    rid, view = run(o)
    assert view.work[0].status == "escalated" and "turn budget" in view.work[0].escalation
    asyncio.run(o.decide(view.inbox[0].id, granted=True, text="go - use research/a.md"))
    view = asyncio.run(o.drive(rid))
    assert view.status == "done" and view.work[0].turns > 3


def test_run_cost_cap_stops_new_work(tmp_path):
    text = BASE.replace("run_cost_usd: 50", "run_cost_usd: 2") + "prices:\n  claude-opus-5-5: [1000, 0, 0, 0]\n"
    cfg = make_cfg(tmp_path, text)
    o = orch(cfg, team(plan(task("a", "research"), task("b", "writer"))))
    _, view = run(o)
    assert "run cost cap reached" in view.paused
    assert all("run cost cap reached" in t.escalation for t in view.work)
    assert 2.0 <= view.cost <= 3.0  # turns already in flight finish; nothing new starts


def test_seat_that_never_calls_a_finisher_is_nudged_then_escalated(cfg):
    o = orch(cfg, team(plan(task("a", "research")), worker=lambda m, msgs: {"text": "thinking out loud"}))
    _, view = run(o)
    assert view.work[0].status == "escalated" and "without calling" in view.work[0].escalation
    assert len([c for c in o.provider.calls if c["meta"]["mode"] == "work"]) == 3


def test_model_refusal_escalates(cfg):
    o = orch(cfg, team(plan(task("a", "research")), worker=lambda m, msgs: {"text": "", "stop_reason": "refusal", "category": "cyber"}))
    _, view = run(o)
    assert "declined" in view.work[0].escalation and "cyber" in view.work[0].escalation


def test_max_tokens_cutoff_never_runs_a_truncated_tool_call(cfg):
    def worker(meta, messages):
        n = turns(messages)
        if n == 0:
            return {**write("research/a.md", "trunc"), "stop_reason": "max_tokens"}
        return write("research/a.md", "whole") if n == 1 else submit("done", ["research/a.md"])

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    _, view = run(o)
    assert len([e for e in events(o, "tool") if e["data"]["tool"] == "write_file"]) == 1
    assert (cfg.workspace / "research" / "a.md").read_text() == "whole" and view.status == "done"


def test_interrupted_run_resumes_from_checkpoints(cfg):
    boom = {"armed": True}

    def worker(meta, messages):
        if turns(messages) == 1 and boom["armed"]:
            boom["armed"] = False
            raise ProviderFatal("credentials expired")
        return simple_worker(meta, messages)

    o = orch(cfg, team(plan(task("a", "research")), worker=worker))
    rid = o.start("ship it", "Andy")
    with pytest.raises(ProviderFatal):
        asyncio.run(o.drive(rid))
    stuck = o.view(rid)
    assert stuck.work[0].status == "running" and "model unavailable" in stuck.paused
    fresh = Orchestrator(cfg, o.provider)
    view = asyncio.run(fresh.drive(rid))
    assert view.status == "done"
    audit_ok(fresh)


def test_only_one_process_drives_a_run(cfg):
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x")))
    rid, _ = run(o)
    with o._driver_lock(rid):
        with pytest.raises(RunBusy):
            asyncio.run(Orchestrator(cfg, o.provider).drive(rid))


def test_config_change_mid_run_needs_explicit_adoption(tmp_path):
    cfg = make_cfg(tmp_path)
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x")))
    rid, _ = run(o)
    cfg2 = make_cfg(tmp_path, BASE.replace("concurrency: 4", "concurrency: 2"))
    o2 = Orchestrator(cfg2, o.provider)
    with pytest.raises(ConfigDrift):
        asyncio.run(o2.drive(rid))
    o2.adopt_config(rid)
    assert events(o2, "config_adopted")
    asyncio.run(o2.drive(rid))  # no longer drifts


def test_every_run_needs_an_authorizing_source(cfg):
    with pytest.raises(HarnessError):
        orch(cfg, lambda m, msgs: None).start("goal", "  ")


def test_audit_catches_an_edited_ledger(cfg):
    o = orch(cfg, team(plan(task("a", "research"))))
    run(o)
    lines = o.ledger.path.read_text().splitlines()
    lines[3] = lines[3].replace('"lead"', '"writer"', 1)
    o.ledger.path.write_text("\n".join(lines) + "\n")
    (chain,) = audit(o.ledger, governor="Andy", max_fix_cycles=3)
    assert not chain.ok and "edited" in chain.problems[0]


def test_audit_flags_a_self_review(cfg):
    o = orch(cfg, team(plan(task("a", "research"))))
    rid, view = run(o)
    t = view.work[0]
    o.ledger.append("review", run=rid, task=t.id, actor="research", reviewer="research", verdict="pass", criteria=[], feedback="")
    checks = {c.name: c for c in audit(o.ledger, governor="Andy", max_fix_cycles=3)}
    assert not checks["the builder never owns the verdict"].ok


# --- who ran it, what ran, and which credential pays -------------------------------------


def test_decisions_record_who_actually_ran_them(cfg, monkeypatch):
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x")))
    rid, view = run(o)
    monkeypatch.setenv("CLAUDECODE", "1")
    asyncio.run(o.decide(view.inbox[0].id, granted=True, text="approved"))
    (decided,) = events(o, "decided")
    who = decided["data"]["provenance"]
    assert decided["actor"] == principal(invoker()) != "Andy"
    assert decided["data"]["acting_as"] == "Andy" and who["user"] and "CLAUDECODE" in who["agent_markers"]
    assert decided["data"]["build"] == build()
    stated = {"user": "alice", "host": "laptop", "pid": 1, "ppid": 0, "tty": "/dev/pts/3", "agent_markers": []}
    o2 = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/y.md", "y")))
    rid2, view2 = run(o2)
    asyncio.run(o2.decide(view2.inbox[0].id, granted=False, text="not yet", provenance=stated))
    assert events(o2, "decided")[-1]["actor"] == "alice@laptop"


def test_the_recorded_user_is_the_process_owner_not_an_environment_claim(cfg, monkeypatch):
    import pwd, os
    monkeypatch.setenv("LOGNAME", "Andy")
    monkeypatch.setenv("USER", "Andy")
    who = invoker()
    assert who["user"] == pwd.getpwuid(os.geteuid()).pw_name and who["uid"] == os.geteuid()
    assert who["env_user"] == "Andy" or who["user"] == "Andy"


def test_git_state_is_only_claimed_for_a_tracked_package(tmp_path):
    import shutil, subprocess, sys
    from pathlib import Path
    import swarm
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("vendored/\n")
    (repo / "README").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    shutil.copytree(Path(swarm.__file__).parent, repo / "vendored" / "swarm", ignore=shutil.ignore_patterns("__pycache__"))
    out = subprocess.run([sys.executable, "-c", "from swarm.provenance import build; b = build(); print(b['git_head'], b['git_dirty'])"],
                         cwd=repo / "vendored", capture_output=True, text=True, check=True).stdout.split()
    assert out == ["None", "None"], "an ignored copy must not claim the checkout's commit"


def test_audit_flags_a_decision_with_no_record_of_who_ran_it(cfg):
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x")))
    rid, view = run(o)
    o.ledger.append("decided", run=rid, task=view.inbox[0].task, actor="Andy", decision=view.inbox[0].id,
                    granted=True, text="approved")
    checks = {c.name: c for c in audit(o.ledger, governor="Andy", max_fix_cycles=3)}
    assert any("no record of who ran" in p for p in checks["approvals are explicit and record who ran them"].problems)


def test_every_process_that_drives_a_run_records_its_build(cfg):
    o = orch(cfg, team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x")))
    rid, _ = run(o)
    started = events(o, "run_started")[0]["data"]["build"]
    assert len(started["source_sha256"]) == 64 and started["version"]
    assert not events(o, "run_resumed"), "the starting process already recorded its build"
    asyncio.run(Orchestrator(cfg, o.provider).drive(rid))
    assert events(o, "run_resumed")[-1]["data"]["build"] == started


class _KeyedProvider(ScriptedProvider):
    def __init__(self, script, credential):
        super().__init__(script)
        self.name, self._credential = "scripted", credential

    def credential_id(self):
        return self._credential


def test_resuming_with_a_different_credential_is_a_named_failure(cfg):
    script = team(plan(task("a", "writer")), worker=lambda m, msgs: write("outbound/x.md", "x") if turns(msgs) == 0 else submit("ok", ["outbound/x.md"]))
    o = Orchestrator(cfg, _KeyedProvider(script, "ANTHROPIC_API_KEY#aaaaaaaaaaaa"))
    rid, view = run(o)
    asyncio.run(o.decide(view.inbox[0].id, granted=True, text="approved"))
    other = Orchestrator(cfg, _KeyedProvider(script, "stored-login"))
    turns_before = len(events(o, "turn"))
    with pytest.raises(CredentialMismatch, match="stored-login"):
        asyncio.run(other.drive(rid))
    assert not events(o, "escalated") and len(events(o, "turn")) == turns_before, "nothing ran on the wrong credential"
    other.adopt_credential(rid)
    assert events(o, "credential_adopted")[0]["data"]["new"] == "stored-login"
    assert asyncio.run(other.drive(rid)).status == "done"


class _NoCredentialClient:
    class beta:
        class messages:
            @staticmethod
            async def create(**kwargs):
                raise TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."')


def test_no_credential_in_the_shell_pauses_the_run_instead_of_escalating_every_task(cfg):
    o = Orchestrator(cfg, AnthropicProvider(_NoCredentialClient()))
    rid = o.start("ship it", "Andy")
    with pytest.raises(MissingCredentials, match="no Anthropic credential"):
        asyncio.run(o.drive(rid))
    view = o.view(rid)
    assert "model unavailable" in view.paused and not events(o, "escalated")
