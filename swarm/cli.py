"""Command line: the governor's controls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from importlib import resources
from pathlib import Path

from . import report
from .audit import audit
from .config import Config, ConfigError, load_config, parse_config
from .ledger import Ledger, LedgerError
from .orchestrator import HarnessError, Orchestrator, RunBusy
from .providers import AnthropicProvider, DemoProvider, ProviderFatal


def _provider(kind: str, cfg: Config):
    if kind == "demo":
        return DemoProvider()
    if kind == "anthropic":
        return AnthropicProvider(fallbacks=cfg.fallbacks)
    raise HarnessError(f"runs made with the {kind!r} provider can't be continued from the command line")


def _for_run(cfg: Config, run_id: str, *, adopt: bool = False, adopt_credential: bool = False) -> Orchestrator:
    """An orchestrator bound to the rules the run started under (or the current rules, if adopted)."""
    probe = Orchestrator(cfg)
    run = probe.view(run_id)
    if adopt:
        orch = Orchestrator(cfg, _provider(run.provider, cfg))
        orch.adopt_config(run_id)
    else:
        snap = probe.run_dir(run_id) / "config.yaml"
        run_cfg = parse_config(snap.read_text(), base_dir=cfg.base_dir) if snap.exists() else cfg
        orch = Orchestrator(run_cfg, _provider(run.provider, run_cfg))
    if adopt_credential:
        orch.adopt_credential(run_id)
    return orch


def _latest(orch: Orchestrator) -> str:
    runs = orch.runs()
    if not runs:
        raise HarnessError("no runs yet")
    return list(runs)[-1]


async def _drive_and_show(orch: Orchestrator, run_id: str) -> int:
    run = await orch.drive(run_id)
    print(report.status(run, orch.cfg.governor))
    return 0


def cmd_init(args) -> int:
    target = Path(args.config)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)")
        return 1
    text = resources.files("swarm").joinpath(f"templates/{args.template}.yaml").read_text()
    target.write_text(text)
    cfg = load_config(target)
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    print(f"Wrote {target} ({args.template} team: {', '.join(cfg.seats)}). Workspace: {cfg.workspace}")
    print("Try it offline:  swarm run --demo \"Outline a launch plan\"")
    return 0


def cmd_run(args, cfg: Config) -> int:
    provider = DemoProvider() if args.demo else AnthropicProvider(fallbacks=cfg.fallbacks)
    orch = Orchestrator(cfg, provider)
    run_id = orch.start(args.goal, args.by or cfg.governor)
    print(f"Started {run_id}.")
    return asyncio.run(_drive_and_show(orch, run_id))


def cmd_resume(args, cfg: Config) -> int:
    orch = Orchestrator(cfg)
    run_id = args.run or _latest(orch)
    orch = _for_run(cfg, run_id, adopt=args.adopt_config, adopt_credential=args.adopt_credential)
    return asyncio.run(_drive_and_show(orch, run_id))


def cmd_status(args, cfg: Config) -> int:
    orch = Orchestrator(cfg)
    if args.all:
        print(report.run_list(orch.runs()))
        return 0
    print(report.status(orch.view(args.run or _latest(orch)), cfg.governor))
    return 0


def cmd_inbox(args, cfg: Config) -> int:
    orch = Orchestrator(cfg)
    if args.show:
        run, d = orch.find_decision(args.show)
        print(f"{d.id} · {d.kind} · {run.id}/{d.task} · {d.status}\n{d.summary}")
        payload = orch.action_payload(run.id, d.id)
        if payload:
            if payload["tool"] == "write_file":
                print(f"\n--- would write {payload['path']} ---\n{payload['content']}\n--- end ---")
            else:
                print(f"\n--- would run ---\n{' '.join(payload['argv'])}")
        return 0
    print(report.inbox(orch.runs(), cfg.governor))
    return 0


def cmd_decide(args, cfg: Config, granted: bool) -> int:
    probe = Orchestrator(cfg)
    run, _ = probe.find_decision(args.id)
    orch = _for_run(cfg, run.id)
    d = asyncio.run(orch.decide(args.id, granted=granted, text=args.text))
    print(f"{d.id} {d.status}." + (f" Executed: {d.executed['result'].splitlines()[0]}" if d.executed else ""))
    if args.no_resume:
        return 0
    try:
        return asyncio.run(_drive_and_show(orch, run.id))
    except RunBusy as e:
        print(e)
        return 0
    except ProviderFatal as e:
        print(f"swarm: {d.id} is recorded, but this shell can't continue {run.id}: {e}", file=sys.stderr)
        return 1


def cmd_log(args, cfg: Config) -> int:
    ledger = Ledger(cfg.state_dir)
    events = ledger.events(args.run)
    if args.task:
        events = [e for e in events if isinstance(e, dict) and e.get("task") == args.task]
    if args.json:
        print("\n".join(json.dumps(e) for e in events))
    else:
        print(report.log(events))
    return 0


def cmd_audit(args, cfg: Config) -> int:
    checks = audit(Ledger(cfg.state_dir), governor=cfg.governor, max_fix_cycles=cfg.budgets.max_fix_cycles)
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'}  {c.name}")
        for p in c.problems[:20]:
            print(f"        {p}")
    return 0 if all(c.ok for c in checks) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="swarm", description="Run a team of Claude agents under enforced rules.")
    ap.add_argument("-c", "--config", default="swarm.yaml", help="path to swarm.yaml (default: ./swarm.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a starter swarm.yaml")
    p.add_argument("--template", choices=["basic", "fleet"], default="basic")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("run", help="start a run toward a goal")
    p.add_argument("goal")
    p.add_argument("--by", help="who authorized this run (default: the governor)")
    p.add_argument("--demo", action="store_true", help="use the offline demo team instead of Claude (free)")

    p = sub.add_parser("resume", help="continue a paused or interrupted run")
    p.add_argument("run", nargs="?")
    p.add_argument("--adopt-config", action="store_true", help="move the run to the current swarm.yaml")
    p.add_argument("--adopt-credential", action="store_true", help="move the run to the credential this shell would bill")

    p = sub.add_parser("status", help="show a run's board")
    p.add_argument("run", nargs="?")
    p.add_argument("--all", action="store_true", help="list every run")

    p = sub.add_parser("inbox", help="what's waiting on the governor")
    p.add_argument("--show", metavar="ID", help="show one item in full, including the exact queued action")

    for name, help_ in (("approve", "approve an inbox item (explicit words only)"), ("deny", "deny an inbox item with a reason")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("id")
        p.add_argument("text", help='your words, e.g. "approved" or the reason for denying')
        p.add_argument("--no-resume", action="store_true", help="record the decision without continuing the run")

    p = sub.add_parser("log", help="show the event log")
    p.add_argument("run", nargs="?")
    p.add_argument("--task")
    p.add_argument("--json", action="store_true")

    sub.add_parser("audit", help="check the ledger against the harness invariants")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "init":
            return cmd_init(args)
        cfg = load_config(args.config)
        handlers = {
            "run": lambda: cmd_run(args, cfg),
            "resume": lambda: cmd_resume(args, cfg),
            "status": lambda: cmd_status(args, cfg),
            "inbox": lambda: cmd_inbox(args, cfg),
            "approve": lambda: cmd_decide(args, cfg, True),
            "deny": lambda: cmd_decide(args, cfg, False),
            "log": lambda: cmd_log(args, cfg),
            "audit": lambda: cmd_audit(args, cfg),
        }
        return handlers[args.cmd]()
    except (ConfigError, HarnessError, LedgerError, ProviderFatal) as e:
        print(f"swarm: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Work so far is checkpointed; continue with `swarm resume`.", file=sys.stderr)
        return 130
