"""Load and validate swarm.yaml.

Every rule the harness enforces is declared here and checked at load time, so a
bad config fails before a run starts instead of halfway through one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

DEFAULT_MODEL = "claude-opus-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ROLES = ("router", "worker", "reviewer")
TIERS = ("routine", "consequential")
WORK_TOOLS = ("read_file", "list_files", "search_files", "write_file", "run_command", "delegate")
DEFAULT_TOOLS = ("read_file", "list_files", "search_files", "write_file")

# USD per million tokens: input, output, cache read, cache write (5-minute TTL).
# Estimates for budgeting; override under `prices:` in swarm.yaml.
DEFAULT_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
# Unknown models are priced at the most expensive known rate so cost caps stay conservative.
FALLBACK_PRICE = DEFAULT_PRICES["claude-fable-5-1"]

DEFAULT_CONTENT_CLASSES = ("general", "research", "product", "process", "pii", "job-search", "calibration-private")
DEFAULT_REDLINE_CLASSES = ("pii", "job-search", "calibration-private")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Seat:
    name: str
    lane: str
    charter: str = ""
    role: str = "worker"
    model: str = DEFAULT_MODEL
    effort: str = "high"
    tools: tuple[str, ...] = DEFAULT_TOOLS
    writes: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Budgets:
    task_turns: int = 60
    task_cost_usd: float = 3.0
    run_cost_usd: float = 20.0
    max_tasks: int = 20
    max_fix_cycles: int = 3
    max_delegation_depth: int = 2


@dataclass(frozen=True)
class Policy:
    structural_paths: tuple[str, ...] = ()
    structural_commands: tuple[str, ...] = ()
    private_paths: tuple[str, ...] = (".env", "*.pem", "*.key")
    secret_scan: bool = True
    pii_scan: bool = True
    pii_allow: tuple[str, ...] = ()
    content_classes: tuple[str, ...] = DEFAULT_CONTENT_CLASSES
    redline_classes: tuple[str, ...] = DEFAULT_REDLINE_CLASSES


@dataclass(frozen=True)
class Config:
    governor: str
    workspace: Path
    state_dir: Path
    base_dir: Path
    seats: dict[str, Seat]
    budgets: Budgets = field(default_factory=Budgets)
    policy: Policy = field(default_factory=Policy)
    concurrency: int = 4
    max_tokens: int = 16000
    fallbacks: bool = True
    command_timeout_s: int = 300
    prices: dict[str, tuple[float, float, float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))
    source_text: str = ""

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.source_text.encode()).hexdigest()

    @property
    def router(self) -> Seat:
        return next(s for s in self.seats.values() if s.role == "router")

    @property
    def workers(self) -> list[Seat]:
        return [s for s in self.seats.values() if s.role == "worker"]

    def price(self, model: str) -> tuple[float, float, float, float]:
        return self.prices.get(model, FALLBACK_PRICE)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"{path} not found - run `swarm init` to create one")
    text = path.read_text()
    return parse_config(text, base_dir=path.parent)


def parse_config(text: str, *, base_dir: Path) -> Config:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ConfigError("swarm.yaml must be a mapping")
    r = _Reader(raw, "swarm.yaml")
    governor = r.str("governor", required=True)
    default_model = r.str("model", DEFAULT_MODEL)
    default_effort = r.choice("effort", EFFORTS, "high")
    workspace = (base_dir / r.str("workspace", "workspace")).resolve()
    state_dir = (base_dir / r.str("state_dir", ".swarm")).resolve()
    if _within(state_dir, workspace) or _within(workspace, state_dir):
        raise ConfigError("workspace and state_dir must not contain each other (seats would read the ledger)")

    b = _Reader(r.map("budgets"), "budgets")
    budgets = Budgets(
        task_turns=b.int("task_turns", Budgets.task_turns, minimum=1),
        task_cost_usd=b.float("task_cost_usd", Budgets.task_cost_usd),
        run_cost_usd=b.float("run_cost_usd", Budgets.run_cost_usd),
        max_tasks=b.int("max_tasks", Budgets.max_tasks, minimum=1),
        max_fix_cycles=b.int("max_fix_cycles", Budgets.max_fix_cycles, minimum=1),
        max_delegation_depth=b.int("max_delegation_depth", Budgets.max_delegation_depth, minimum=0),
    )
    b.done()

    p = _Reader(r.map("policy"), "policy")
    policy = Policy(
        structural_paths=_globs(p.strs("structural_paths", Policy.structural_paths), "policy.structural_paths"),
        structural_commands=p.strs("structural_commands", Policy.structural_commands),
        private_paths=_globs(p.strs("private_paths", Policy.private_paths), "policy.private_paths"),
        secret_scan=p.bool("secret_scan", True),
        pii_scan=p.bool("pii_scan", True),
        pii_allow=p.strs("pii_allow", ()),
        content_classes=p.strs("content_classes", DEFAULT_CONTENT_CLASSES),
        redline_classes=p.strs("redline_classes", DEFAULT_REDLINE_CLASSES),
    )
    p.done()
    for rx in policy.pii_allow:
        try:
            re.compile(rx)
        except re.error as e:
            raise ConfigError(f"policy.pii_allow: {rx!r} is not a valid regular expression ({e})") from None
    if not policy.secret_scan:
        # Secret leaks are the one failure with no undo; the scan has no off switch.
        raise ConfigError("policy.secret_scan cannot be disabled")
    if "general" not in policy.content_classes:
        raise ConfigError("policy.content_classes must include 'general' (the default class)")
    unknown = set(policy.redline_classes) - set(policy.content_classes)
    if unknown:
        raise ConfigError(f"policy.redline_classes not in content_classes: {sorted(unknown)}")

    seats: dict[str, Seat] = {}
    raw_seats = r.map("seats")
    if not raw_seats:
        raise ConfigError("seats: define at least a router and one worker")
    for name, spec in raw_seats.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"seats.{name} must be a mapping")
        seats[str(name)] = _parse_seat(str(name), spec, default_model, default_effort)

    prices = dict(DEFAULT_PRICES)
    for model, row in r.map("prices").items():
        if not (isinstance(row, list) and len(row) == 4 and all(isinstance(x, (int, float)) for x in row)):
            raise ConfigError(f"prices.{model} must be [input, output, cache_read, cache_write] per million tokens")
        prices[str(model)] = tuple(float(x) for x in row)  # type: ignore[assignment]

    cfg = Config(
        governor=governor,
        workspace=workspace,
        state_dir=state_dir,
        base_dir=base_dir.resolve(),
        seats=seats,
        budgets=budgets,
        policy=policy,
        concurrency=r.int("concurrency", 4, minimum=1),
        max_tokens=r.int("max_tokens", 16000, minimum=1024),
        fallbacks=r.bool("fallbacks", True),
        command_timeout_s=r.int("command_timeout_s", 300, minimum=1),
        prices=prices,
        source_text=text,
    )
    r.done()
    _validate_team(cfg)
    return cfg


def _parse_seat(name: str, spec: dict, default_model: str, default_effort: str) -> Seat:
    s = _Reader(spec, f"seats.{name}")
    role = s.choice("role", ROLES, "worker")
    tools = s.strs("tools", DEFAULT_TOOLS if role == "worker" else ("read_file", "list_files", "search_files"))
    bad = set(tools) - set(WORK_TOOLS)
    if bad:
        raise ConfigError(f"seats.{name}.tools: unknown {sorted(bad)} (choose from {', '.join(WORK_TOOLS)})")
    seat = Seat(
        name=name,
        lane=s.str("lane", required=True),
        charter=s.str("charter", ""),
        role=role,
        model=s.str("model", default_model),
        effort=s.choice("effort", EFFORTS, default_effort),
        tools=tools,
        writes=_globs(s.strs("writes", ()), f"seats.{name}.writes"),
        commands=s.strs("commands", ()),
        checks=s.strs("checks", ()),
        reviewers=s.strs("reviewers", ()),
    )
    s.done()
    if "run_command" in seat.tools and not seat.commands:
        raise ConfigError(f"seats.{name}: run_command needs an allowlist under `commands`")
    if "write_file" in seat.tools and not seat.writes:
        raise ConfigError(f"seats.{name}: write_file needs lane paths under `writes`")
    return seat


def _validate_team(cfg: Config) -> None:
    routers = [s.name for s in cfg.seats.values() if s.role == "router"]
    if len(routers) != 1:
        raise ConfigError(f"exactly one seat must have role: router (found {routers or 'none'})")
    if not cfg.workers:
        raise ConfigError("at least one seat must have role: worker")
    for seat in cfg.seats.values():
        for rv in seat.reviewers:
            if rv == seat.name:
                raise ConfigError(f"seats.{seat.name}: a seat cannot review its own work")
            if rv not in cfg.seats:
                raise ConfigError(f"seats.{seat.name}.reviewers: unknown seat {rv!r}")
            if cfg.seats[rv].role == "router":
                raise ConfigError(f"seats.{seat.name}.reviewers: the router dispatches work and cannot grade it")
        if seat.role == "worker" and not seat.reviewers:
            raise ConfigError(f"seats.{seat.name}: workers need at least one reviewer (the builder never owns the verdict)")


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _globs(globs: tuple[str, ...], where: str) -> tuple[str, ...]:
    for g in globs:
        parts = PurePosixPath(g).parts
        if g.startswith("/") or ".." in parts:
            raise ConfigError(f"{where}: {g!r} must be relative to the workspace")
    return globs


class _Reader:
    """Typed access to one YAML mapping that rejects unknown keys (typos fail loud)."""

    def __init__(self, data: Any, where: str):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{where} must be a mapping")
        self.data, self.where, self.seen = data, where, set()

    def _get(self, key: str, default: Any, required: bool = False) -> Any:
        self.seen.add(key)
        if key not in self.data or self.data[key] is None:
            if required:
                raise ConfigError(f"{self.where}.{key} is required")
            return default
        return self.data[key]

    def str(self, key: str, default: str = "", required: bool = False) -> str:
        v = self._get(key, default, required)
        if not isinstance(v, str) or (required and not v.strip()):
            raise ConfigError(f"{self.where}.{key} must be a non-empty string")
        return v

    def int(self, key: str, default: int, minimum: int = 0) -> int:
        v = self._get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
            raise ConfigError(f"{self.where}.{key} must be an integer >= {minimum}")
        return v

    def float(self, key: str, default: float) -> float:
        v = self._get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ConfigError(f"{self.where}.{key} must be a positive number")
        return float(v)

    def bool(self, key: str, default: bool) -> bool:
        v = self._get(key, default)
        if not isinstance(v, bool):
            raise ConfigError(f"{self.where}.{key} must be true or false")
        return v

    def choice(self, key: str, options: tuple[str, ...], default: str) -> str:
        v = self._get(key, default)
        if v not in options:
            raise ConfigError(f"{self.where}.{key} must be one of {', '.join(options)}")
        return v

    def strs(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        v = self._get(key, list(default))
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigError(f"{self.where}.{key} must be a list of non-empty strings")
        return tuple(v)

    def map(self, key: str) -> dict:
        v = self._get(key, {})
        if not isinstance(v, dict):
            raise ConfigError(f"{self.where}.{key} must be a mapping")
        return v

    def done(self) -> None:
        unknown = set(self.data) - self.seen
        if unknown:
            raise ConfigError(f"{self.where}: unknown keys {sorted(unknown)}")
