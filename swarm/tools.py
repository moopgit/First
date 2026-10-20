"""The tools seats act through, and the executor that enforces policy on every call.

Each action is a dedicated tool (not a shell) so the harness can gate it:
writes are checked against the seat's lane, structural actions are queued for
the governor instead of running, commands run without a shell from an
allowlist, and finishing tools validate their own inputs.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from . import policy as pol
from .config import TIERS, Config, Seat

MAX_READ = 200_000
MAX_LIST = 500
MAX_MATCHES = 100
MAX_OUTPUT = 8_000
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.I)

STR = {"type": "string"}
STRS = {"type": "array", "items": {"type": "string"}}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


PLAN_TASK = _obj({
    "key": {"type": "string", "description": "Short unique key, e.g. 'research'"},
    "seat": STR,
    "goal": {"type": "string", "description": "Self-contained brief: the seat sees only this plus its dependencies' results"},
    "acceptance": {**STRS, "description": "1-6 criteria a reviewer can check from the workspace"},
    "depends_on": {**STRS, "description": "Keys of tasks whose results this task needs"},
    "tier": {"type": "string", "enum": list(TIERS)},
    "content_class": STR,
})
CRITERION = _obj({"criterion": STR, "met": {"type": "boolean"}, "evidence": STR})

SPECS: dict[str, tuple[str, dict]] = {
    "read_file": ("Read a UTF-8 text file from the workspace. Paths are relative to the workspace root.", _obj({"path": STR})),
    "list_files": ("List files under a workspace directory, recursively (up to 500). Use '.' for the root.", _obj({"path": STR})),
    "search_files": (
        "Search workspace text files under `path` for a regular expression. Returns up to 100 lines as path:line: text.",
        _obj({"pattern": STR, "path": STR}),
    ),
    "write_file": (
        "Create or overwrite a text file. Only paths in your lane are writable. Writes to structural paths are queued for "
        "the governor's approval instead of running, and your task pauses until they decide.",
        _obj({"path": STR, "content": STR}),
    ),
    "run_command": (
        "Run an allowlisted command in the workspace. There is no shell: pipes, redirects and && are passed as literal "
        "arguments. Returns the exit code and the last 8,000 characters of output.",
        _obj({"command": STR}),
    ),
    "delegate": (
        "Hand a self-contained subtask to another seat. Your task pauses after this turn and resumes with the "
        "subtask's result.",
        _obj({"seat": STR, "goal": STR, "acceptance": STRS}),
    ),
    "wait": (
        "Pause until something outside the workspace happens. Give a concrete resume trigger, e.g. 'Andy shares the "
        "Q3 pricing sheet'. The governor resolves the wait.",
        _obj({"trigger": STR, "reason": STR}),
    ),
    "escalate": (
        "Stop and hand this task to the governor: a safety or ethics concern, contradictory instructions, or a "
        "blocker no tool can solve. Stopping is always allowed.",
        _obj({"reason": STR}),
    ),
    "submit": (
        "Finish the task. `artifacts` lists every workspace file that holds the deliverable (may be empty). The "
        "submission goes through mechanical checks, then review against the acceptance criteria.",
        _obj({"summary": STR, "artifacts": STRS}),
    ),
    "submit_plan": (
        "Submit the task plan. Invalid plans come back with the problems listed so you can fix them.",
        _obj({"rationale": STR, "tasks": {"type": "array", "items": PLAN_TASK}}),
    ),
    "submit_verdict": (
        "Submit your review. Give one entry per acceptance criterion, in order. 'pass' requires every criterion met; "
        "on 'fail', `feedback` must say exactly what to change.",
        _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "criteria": {"type": "array", "items": CRITERION}, "feedback": STR}),
    ),
}
ORDER = list(SPECS)  # fixed tool order keeps the cached prompt prefix stable
READ_TOOLS = ("read_file", "list_files", "search_files")
FINISHERS = {
    "plan": ("submit_plan", "escalate"),
    "work": ("submit", "wait", "escalate"),
    "review": ("submit_verdict", "escalate"),
    "synth": ("submit", "escalate"),
}


def tools_for(seat: Seat, mode: str) -> list[str]:
    if mode == "plan":
        names = [*READ_TOOLS]
    elif mode == "review":
        names = [*READ_TOOLS, *(["run_command"] if "run_command" in seat.tools else [])]
    elif mode == "synth":
        names = [t for t in seat.tools if t in (*READ_TOOLS, "write_file")]
    else:
        names = list(seat.tools)
    names += FINISHERS[mode]
    return [n for n in ORDER if n in names]


def definitions(names: list[str]) -> list[dict]:
    return [{"name": n, "description": SPECS[n][0], "input_schema": SPECS[n][1], "strict": True} for n in names]


class InputError(Exception):
    pass


def validate(schema: dict, value: Any, where: str = "input") -> None:
    """Check a tool input against our (small) JSON-schema subset. Model output is untrusted."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise InputError(f"{where} must be an object")
        missing = [k for k in schema["required"] if k not in value]
        extra = [k for k in value if k not in schema["properties"]]
        if missing or extra:
            raise InputError(f"{where}: missing {missing} / unexpected {extra}".replace("missing [] / ", "").replace(" / unexpected []", ""))
        for k, sub in schema["properties"].items():
            validate(sub, value[k], f"{where}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            raise InputError(f"{where} must be a list")
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{where}[{i}]")
    elif t == "string":
        if not isinstance(value, str):
            raise InputError(f"{where} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise InputError(f"{where} must be one of {schema['enum']}")
    elif t == "boolean" and not isinstance(value, bool):
        raise InputError(f"{where} must be true or false")


@dataclass
class Control:
    """A tool call that ends the seat's turn loop."""

    kind: str  # submit | plan | verdict | pause | escalate
    data: dict = field(default_factory=dict)
    triggers: list[str] = field(default_factory=list)


@dataclass
class Outcome:
    content: str
    is_error: bool = False
    control: Control | None = None


@dataclass
class Hooks:
    """Callbacks into the orchestrator for tools with side effects beyond the workspace."""

    queue_action: Callable[[dict, str], str]  # (action, summary) -> decision id
    open_wait: Callable[[str, str], str]  # (trigger, reason) -> decision id
    delegate: Callable[[str, str, list[str]], str]  # (seat, goal, acceptance) -> task id
    plan_problems: Callable[[list[dict]], list[str]]
    log: Callable[[str, bool, str], None]  # (tool, ok, summary)


class Executor:
    def __init__(self, cfg: Config, seat: Seat, mode: str, hooks: Hooks, *, acceptance: list[str] | None = None):
        self.cfg, self.seat, self.mode, self.hooks = cfg, seat, mode, hooks
        self.acceptance = acceptance or []
        self.allowed = tools_for(seat, mode)

    def definitions(self) -> list[dict]:
        return definitions(self.allowed)

    @property
    def finishers(self) -> tuple[str, ...]:
        return FINISHERS[self.mode]

    async def execute(self, name: str, args: Any) -> Outcome:
        if name not in self.allowed:
            out = Outcome(f"Tool {name!r} is not available here. Available: {', '.join(self.allowed)}", is_error=True)
        else:
            try:
                validate(SPECS[name][1], args)
                out = await getattr(self, f"_{name}")(**args)
            except (pol.PolicyError, InputError) as e:
                out = Outcome(f"Refused: {e}", is_error=True)
        self.hooks.log(name, not out.is_error, _summarize(name, args, out))
        return out

    # --- workspace -----------------------------------------------------------

    async def _read_file(self, path: str) -> Outcome:
        target, norm = pol.resolve(self.cfg, path)
        if not target.is_file():
            return Outcome(f"{norm or '.'} is not a file", is_error=True)
        data = target.read_bytes()
        try:
            text = data[:MAX_READ].decode("utf-8")
        except UnicodeDecodeError:
            return Outcome(f"{norm} is not UTF-8 text", is_error=True)
        if len(data) > MAX_READ:
            text += f"\n\n[truncated: showing the first {MAX_READ:,} of {len(data):,} bytes]"
        return Outcome(text)

    def _walk(self, path: str):
        target, norm = pol.resolve(self.cfg, path)
        if not target.is_dir():
            raise pol.PolicyError(f"{norm or '.'} is not a directory")
        root = self.cfg.workspace.resolve()
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames.sort()
            for f in sorted(filenames):
                rel = os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")
                try:
                    pol.resolve(self.cfg, rel)
                except pol.PolicyError:
                    continue  # private paths stay invisible
                yield rel

    async def _list_files(self, path: str) -> Outcome:
        files = []
        for rel in self._walk(path):
            files.append(rel)
            if len(files) >= MAX_LIST:
                files.append(f"[stopped at {MAX_LIST} entries]")
                break
        return Outcome("\n".join(files) or "(no files)")

    async def _search_files(self, pattern: str, path: str) -> Outcome:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return Outcome(f"invalid regular expression: {e}", is_error=True)
        hits = []
        for rel in self._walk(path):
            fp = self.cfg.workspace / rel
            if fp.stat().st_size > 1_000_000:
                continue
            try:
                lines = fp.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(lines, 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {line[:300]}")
                    if len(hits) >= MAX_MATCHES:
                        return Outcome("\n".join(hits) + f"\n[stopped at {MAX_MATCHES} matches]")
        return Outcome("\n".join(hits) or "(no matches)")

    async def _write_file(self, path: str, content: str) -> Outcome:
        target, norm = pol.check_write(self.cfg, self.seat, path)
        if pol.is_structural_path(self.cfg.policy, norm):
            leaks = pol.scan_secrets(content, norm)
            if leaks:
                # Secret findings are never approval-eligible: refuse outright.
                raise pol.PolicyError("content contains secrets and cannot be queued: " + "; ".join(map(str, leaks)))
            action = {"tool": "write_file", "path": norm, "content": content}
            did = self.hooks.queue_action(action, f"write {norm} ({len(content):,} chars)")
            return Outcome(
                f"{norm} is a structural path, so nothing was written. Queued for {self.cfg.governor}'s approval as "
                f"{did}; your task pauses after this turn. If approved, the harness writes exactly this content.",
                control=Control("pause", {"reason": f"approval {did}"}, [f"decision:{did}"]),
            )
        write_text(target, content)
        return Outcome(f"wrote {norm} ({len(content):,} chars)")

    async def _run_command(self, command: str) -> Outcome:
        argv = pol.parse_command(command)
        pol.check_command(self.seat, argv)
        if pol.is_structural_command(self.cfg.policy, argv):
            did = self.hooks.queue_action({"tool": "run_command", "argv": argv}, f"run `{command}`")
            return Outcome(
                f"`{command}` is a structural command, so it did not run. Queued for {self.cfg.governor}'s approval as "
                f"{did}; your task pauses after this turn.",
                control=Control("pause", {"reason": f"approval {did}"}, [f"decision:{did}"]),
            )
        code, output = await run_argv(self.cfg, argv)
        return Outcome(f"exit code {code}\n{output}", is_error=code != 0)

    # --- coordination ---------------------------------------------------------

    async def _delegate(self, seat: str, goal: str, acceptance: list[str]) -> Outcome:
        tid = self.hooks.delegate(seat, goal, acceptance)
        return Outcome(
            f"Delegated to {seat} as {tid}. Your task pauses after this turn and resumes with its result.",
            control=Control("pause", {"reason": f"subtask {tid}"}, [f"task:{tid}"]),
        )

    async def _wait(self, trigger: str, reason: str) -> Outcome:
        if not trigger.strip():
            raise InputError("a wait needs a concrete resume trigger")
        did = self.hooks.open_wait(trigger.strip(), reason)
        return Outcome(f"Waiting on: {trigger} ({did}).", control=Control("pause", {"reason": trigger}, [f"decision:{did}"]))

    async def _escalate(self, reason: str) -> Outcome:
        if not reason.strip():
            raise InputError("say why you are escalating")
        return Outcome("Escalated to the governor.", control=Control("escalate", {"reason": reason}))

    # --- finishers -----------------------------------------------------------

    async def _submit(self, summary: str, artifacts: list[str]) -> Outcome:
        if not summary.strip():
            raise InputError("summary is empty")
        normalized, missing = [], []
        for a in artifacts:
            target, norm = pol.resolve(self.cfg, a)
            (normalized if target.is_file() else missing).append(norm or a)
        if missing:
            return Outcome(f"These artifacts don't exist: {', '.join(missing)}. Fix the list and submit again.", is_error=True)
        return Outcome("Submitted.", control=Control("submit", {"summary": summary, "artifacts": normalized}))

    async def _submit_plan(self, rationale: str, tasks: list[dict]) -> Outcome:
        problems = self.hooks.plan_problems(tasks)
        if problems:
            return Outcome("The plan was not accepted:\n- " + "\n- ".join(problems), is_error=True)
        return Outcome("Plan accepted.", control=Control("plan", {"rationale": rationale, "tasks": tasks}))

    async def _submit_verdict(self, verdict: str, criteria: list[dict], feedback: str) -> Outcome:
        if len(criteria) != len(self.acceptance):
            return Outcome(f"Give exactly one entry per acceptance criterion ({len(self.acceptance)}), in order.", is_error=True)
        unmet = [c["criterion"] for c in criteria if not c["met"]]
        if verdict == "pass" and unmet:
            return Outcome(f"Inconsistent verdict: 'pass' with unmet criteria ({'; '.join(unmet)}). Fix one or the other.", is_error=True)
        if verdict == "fail" and not feedback.strip():
            return Outcome("A 'fail' verdict needs feedback that says exactly what to change.", is_error=True)
        return Outcome("Verdict recorded.", control=Control("verdict", {"verdict": verdict, "criteria": criteria, "feedback": feedback}))


def write_text(target, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.swarm-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


async def run_argv(cfg: Config, argv: list[str]) -> tuple[int, str]:
    """Run without a shell, in the workspace, with credentials stripped from the environment."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cfg.workspace, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError as e:
        return 127, f"could not start {argv[0]!r}: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=cfg.command_timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {cfg.command_timeout_s}s"
    text = out.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT:
        text = f"[showing the last {MAX_OUTPUT:,} characters]\n" + text[-MAX_OUTPUT:]
    return proc.returncode or 0, text


def _summarize(name: str, args: Any, out: Outcome) -> str:
    a = args if isinstance(args, dict) else {}
    target = a.get("path") or a.get("command") or a.get("seat") or a.get("trigger") or ""
    first = out.content.splitlines()[0] if out.content else ""
    return f"{target} -> {first}"[:300] if target else first[:300]
