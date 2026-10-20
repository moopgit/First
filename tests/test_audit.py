"""Adversarial ledger constructions: histories a buggy or dishonest writer could produce,
each with a valid hash chain. `swarm audit` must flag every one and still pass an honest one."""

from __future__ import annotations

import asyncio

import pytest
from conftest import BASE, make_cfg, orch, plan, run, submit, task, turns, verdict, write

from swarm.audit import audit
from swarm.cli import main
from swarm.ledger import Ledger


def new_run(tmp_path, text=BASE):
    cfg = make_cfg(tmp_path, text)
    led = Ledger(cfg.state_dir)
    rd = cfg.state_dir / "runs" / "R1"
    rd.mkdir(parents=True)
    (rd / "config.yaml").write_text(cfg.source_text)
    led.append("run_started", run="R1", actor="Andy", goal="g", requested_by="Andy", config_hash=cfg.hash,
               provider="scripted", credential="scripted", base_dir=str(cfg.base_dir), build={})
    return cfg, led


def created(led, t="T2", seat="research", **over):
    fields = dict(seat=seat, mode="work", goal="g", acceptance=["c"], deps=[], tier="routine",
                  content_class="general", requested_by="Andy")
    fields.update(over)
    led.append("task_created", run="R1", task=t, actor="lead", **{k: v for k, v in fields.items() if v is not None})


def submitted(led, t="T2"):
    led.append("submitted", run="R1", task=t, actor="research", n=1, summary="done", artifacts=[])


def gate(led, name, passed=True, findings=(), t="T2"):
    led.append("gate", run="R1", task=t, actor="harness", gate=name, passed=passed, findings=list(findings))


def review(led, reviewer="checker", t="T2"):
    led.append("review", run="R1", task=t, actor=reviewer, reviewer=reviewer, verdict="pass", criteria=[], feedback="ok")


def done(led, t="T2"):
    led.append("task_done", run="R1", task=t, actor="harness")


ALL_GATES = ("artifacts", "secret_scan", "pii_scan")


def honest(led, t="T2"):
    created(led, t)
    submitted(led, t)
    for g in ALL_GATES:
        gate(led, g, t=t)
    review(led, t=t)
    done(led, t)


def failing(led, cfg) -> dict[str, list[str]]:
    return {c.name: c.problems for c in audit(led, governor=cfg.governor, max_fix_cycles=3) if not c.ok}


# --- the constructions --------------------------------------------------------------


def construct_L1_phantom_task(tmp_path):
    cfg, led = new_run(tmp_path)
    submitted(led, "T9")
    for g in ALL_GATES:
        gate(led, g, t="T9")
    review(led, t="T9")
    done(led, "T9")
    return cfg, led


def construct_L2_artifacts_gate_only(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led)
    submitted(led)
    gate(led, "artifacts")
    review(led)
    done(led)
    return cfg, led


def construct_L3_passed_gate_with_secret_finding(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led)
    submitted(led)
    gate(led, "artifacts")
    gate(led, "secret_scan", passed=True, findings=["possible secret - Anthropic API key in research/a.md line 1: sk-a…xx"])
    gate(led, "pii_scan")
    review(led)
    done(led)
    return cfg, led


def construct_L6_review_before_gates(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led)
    submitted(led)
    review(led)
    for g in ALL_GATES:
        gate(led, g)
    done(led)
    return cfg, led


def construct_honest(tmp_path):
    cfg, led = new_run(tmp_path)
    honest(led)
    return cfg, led


def construct_tampered(tmp_path):
    cfg, led = construct_honest(tmp_path)
    lines = led.path.read_text().splitlines()
    lines[-2] = lines[-2].replace('"verdict":"pass"', '"verdict":"fail"')
    led.path.write_text("\n".join(lines) + "\n")
    return cfg, led


def construct_self_review(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led)
    submitted(led)
    for g in ALL_GATES:
        gate(led, g)
    review(led, reviewer="research")
    done(led)
    return cfg, led


def construct_run_missing_config_hash(tmp_path):
    cfg = make_cfg(tmp_path)
    led = Ledger(cfg.state_dir)
    led.append("run_started", run="R1", actor="Andy", goal="g", requested_by="Andy", provider="scripted")
    honest(led)
    return cfg, led


def construct_task_missing_fields(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led, goal=None, acceptance=None, deps=None)
    submitted(led)
    for g in ALL_GATES:
        gate(led, g)
    review(led)
    done(led)
    return cfg, led


CONSTRUCTIONS = {
    "L1 phantom task": (construct_L1_phantom_task, "every event belongs to a started run, and every task event to a created task"),
    "L2 artifacts gate only": (construct_L2_artifacts_gate_only, "done means verified (every configured gate passed, then an independent pass)"),
    "L3 passed gate naming a secret": (construct_L3_passed_gate_with_secret_finding, "done means verified (every configured gate passed, then an independent pass)"),
    "L6 review before gates": (construct_L6_review_before_gates, "done means verified (every configured gate passed, then an independent pass)"),
    "run_started missing config_hash": (construct_run_missing_config_hash, "every event is structurally complete"),
    "task_created missing goal/acceptance/deps": (construct_task_missing_fields, "every event is structurally complete"),
    "control: post-hoc tamper": (construct_tampered, "ledger hash chain is intact"),
    "control: self-review": (construct_self_review, "the builder never owns the verdict"),
}


@pytest.mark.parametrize("name", list(CONSTRUCTIONS))
def test_each_construction_fails_the_check_it_targets(tmp_path, name):
    build, check = CONSTRUCTIONS[name]
    cfg, led = build(tmp_path)
    assert check in failing(led, cfg)


@pytest.mark.parametrize("name", list(CONSTRUCTIONS))
def test_the_cli_reports_findings_without_crashing(tmp_path, name, capsys):
    CONSTRUCTIONS[name][0](tmp_path)
    assert main(["-c", str(tmp_path / "swarm.yaml"), "audit"]) == 1
    assert "FAIL" in capsys.readouterr().out
    assert main(["-c", str(tmp_path / "swarm.yaml"), "log"]) == 0
    assert main(["-c", str(tmp_path / "swarm.yaml"), "status"]) == 0


def test_an_honest_history_passes(tmp_path):
    cfg, led = construct_honest(tmp_path)
    assert failing(led, cfg) == {}


def test_check_4_findings_name_what_is_wrong(tmp_path):
    for sub in ("l2", "l3", "l6"):
        (tmp_path / sub).mkdir()
    cfg, led = construct_L2_artifacts_gate_only(tmp_path / "l2")
    assert any("missing secret_scan, pii_scan" in p for p in failing(led, cfg)[CONSTRUCTIONS["L2 artifacts gate only"][1]])
    cfg, led = construct_L3_passed_gate_with_secret_finding(tmp_path / "l3")
    assert any("passed=True with 1 finding" in p for p in failing(led, cfg)[CONSTRUCTIONS["L3 passed gate naming a secret"][1]])
    cfg, led = construct_L6_review_before_gates(tmp_path / "l6")
    problems = failing(led, cfg)[CONSTRUCTIONS["L6 review before gates"][1]]
    assert any("before the configured gates passed" in p for p in problems)
    assert any("comes after the review" in p for p in problems)


def test_configured_checks_are_part_of_the_required_set(tmp_path):
    text = BASE.replace('    writes: ["research/*"]', '    writes: ["research/*"]\n    checks: ["true"]')
    cfg, led = new_run(tmp_path, text)
    honest(led)  # three gates, but the seat's configured check never ran
    assert any("missing check: true" in p for p in failing(led, cfg)[CONSTRUCTIONS["L2 artifacts gate only"][1]])


def test_a_missing_config_snapshot_fails_closed(tmp_path):
    cfg, led = construct_honest(tmp_path)
    (cfg.state_dir / "runs" / "R1" / "config.yaml").unlink()
    assert any("no snapshot of config" in p for p in failing(led, cfg)[CONSTRUCTIONS["L2 artifacts gate only"][1]])


def test_an_edited_config_snapshot_fails_closed(tmp_path):
    cfg, led = construct_honest(tmp_path)
    snap = cfg.state_dir / "runs" / "R1" / "config.yaml"
    snap.write_text(snap.read_text().replace("checker:\n    role: reviewer", "checker:\n    role: reviewer\n    checks: [\"true\"]"))
    assert CONSTRUCTIONS["L2 artifacts gate only"][1] in failing(led, cfg)


def test_events_for_a_run_that_never_started_are_flagged(tmp_path):
    cfg, led = construct_honest(tmp_path)
    led.append("task_done", run="R7", task="T1", actor="harness")
    assert any("R7: task_done" in p for p in failing(led, cfg)[CONSTRUCTIONS["L1 phantom task"][1]])


def test_a_task_created_twice_is_flagged(tmp_path):
    cfg, led = construct_honest(tmp_path)
    created(led, seat="writer", content_class="job-search")  # a second identity for T2 would change its checks
    assert any("task_created again" in p for p in failing(led, cfg)[CONSTRUCTIONS["L1 phantom task"][1]])


def test_mistyped_fields_are_findings_not_crashes(tmp_path):
    cfg, led = new_run(tmp_path)
    created(led, acceptance="not a list", deps=7)
    led.append("turn", run="R1", task="T2", actor="research", cost="lots")
    led.append("waiting", run="R1", task="T2", actor="research", triggers=[3])
    problems = failing(led, cfg)
    assert any("wrong type" in p for p in problems["every event is structurally complete"])
    assert "every wait names its resume trigger" in problems


def test_each_window_is_checked_against_the_config_in_force(tmp_path):
    """Adopting a config mid-run changes the gate set; earlier work is still judged by the rules it ran under."""
    cfg = make_cfg(tmp_path)

    def worker(meta, messages):
        if turns(messages) == 0:
            return write("outbound/post.md", "hello")
        return submit("posted", ["outbound/post.md"])

    def script(meta, messages):
        if meta["mode"] == "plan":
            return plan(task("a", "research"), task("b", "writer"))
        if meta["mode"] == "review":
            return verdict(meta)
        if meta["mode"] == "synth":
            return submit("final")
        if meta["seat"] == "research":
            return write("research/a.md", "x") if turns(messages) == 0 else submit("done", ["research/a.md"])
        return worker(meta, messages)

    o = orch(cfg, script)
    rid, view = run(o)  # research finishes under pii_scan; writer pauses on its outbound write
    cfg2 = make_cfg(tmp_path, BASE.replace("  pii_allow:", "  pii_scan: false\n  pii_allow:"))
    o2 = type(o)(cfg2, o.provider)
    o2.adopt_config(rid)
    asyncio.run(o2.decide(view.inbox[0].id, granted=True, text="approved"))
    assert asyncio.run(o2.drive(rid)).status == "done"
    assert failing(o2.ledger, cfg2) == {}
