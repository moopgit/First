"""Policy checks: workspace sandboxing, lane scopes, structural actions, approval
wording, and the secret/PII scanners used by the mechanical gates."""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import Config, Policy, Seat


class PolicyError(Exception):
    """An action the policy refuses. The message is shown to the seat."""


def _match(rel: str, globs: tuple[str, ...]) -> bool:
    # fnmatch's `*` also crosses `/`, so "drafts/*" covers nested files too.
    return any(fnmatch.fnmatchcase(rel, g) or fnmatch.fnmatchcase(rel, g.rstrip("/*") + "/*") for g in globs)


def resolve(cfg: Config, rel: str) -> tuple[Path, str]:
    """Resolve a seat-supplied path inside the workspace; return (absolute path, normalized relative path)."""
    if not isinstance(rel, str) or not rel.strip():
        raise PolicyError("path is empty")
    pure = PurePosixPath(rel.strip())
    if pure.is_absolute() or ".." in pure.parts:
        raise PolicyError(f"{rel!r}: paths must be relative to the workspace and may not contain '..'")
    root = cfg.workspace.resolve()
    target = (root / pure).resolve()
    try:
        norm = target.relative_to(root).as_posix()
    except ValueError:
        raise PolicyError(f"{rel!r} resolves outside the workspace") from None
    if norm == ".":
        norm = ""
    if norm and _match(norm, cfg.policy.private_paths):
        raise PolicyError(f"{rel!r} is a private path")
    return target, norm


def check_write(cfg: Config, seat: Seat, rel: str) -> tuple[Path, str]:
    target, norm = resolve(cfg, rel)
    if not norm:
        raise PolicyError("cannot write to the workspace root itself")
    if not _match(norm, seat.writes):
        lanes = ", ".join(seat.writes) or "nothing"
        raise PolicyError(f"{norm!r} is outside your lane - you may write to: {lanes}")
    return target, norm


def is_structural_path(policy: Policy, norm: str) -> bool:
    return _match(norm, policy.structural_paths)


def parse_command(command: str) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise PolicyError(f"could not parse command: {e}") from None
    if not argv:
        raise PolicyError("command is empty")
    return argv


def _has_prefix(argv: list[str], prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        p = shlex.split(prefix)
        if argv[: len(p)] == p:
            return True
    return False


def check_command(seat: Seat, argv: list[str]) -> None:
    if not _has_prefix(argv, seat.commands):
        allowed = "; ".join(seat.commands) or "none"
        raise PolicyError(f"command not on your allowlist (allowed prefixes: {allowed})")


def is_structural_command(policy: Policy, argv: list[str]) -> bool:
    return _has_prefix(argv, policy.structural_commands)


def expected_gates(cfg: Config, seat: Seat, content_class: str) -> list[str]:
    """The mechanical gates every submission of this seat's work must pass, as configured."""
    names = ["artifacts", "secret_scan"]
    if cfg.policy.pii_scan and content_class not in cfg.policy.redline_classes:
        names.append("pii_scan")
    return names + [f"check: {c}" for c in seat.checks]


# --- approval wording ---------------------------------------------------------
# Only an unambiguous imperative approves. "should be in I think" is a question
# or a thought, not a yes: the harness refuses it and asks again.

APPROVE_WORDS = ("approved", "approve", "yes", "go", "do it", "ship it", "proceed", "confirmed", "lgtm", "go ahead")
HEDGES = (
    "i think", "maybe", "probably", "perhaps", "should", "might", "could", "not sure", "unsure",
    "if you", "unless", "i guess", "kind of", "sort of", "wait", "hold", "later", "no", "not", "don't", "dont",
)


def approval_problem(text: str) -> str | None:
    """None if `text` is an explicit approval, else why it isn't."""
    norm = " ".join(re.sub(r"[^a-z' ]+", " ", (text or "").lower()).split())
    if not norm:
        return "approval text is empty"
    if "?" in (text or ""):
        return "that reads as a question, not an approval"
    padded = f" {norm} "
    hedge = next((h for h in HEDGES if f" {h} " in padded), None)
    if hedge:
        return f"contains {hedge!r}, which makes it ambiguous"
    if not any(f" {w} " in padded for w in APPROVE_WORDS):
        return "no explicit approval word (approved / yes / go / do it / ship it)"
    return None


# --- scanners -------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str
    where: str
    line: int
    excerpt: str

    def __str__(self) -> str:
        return f"{self.kind} in {self.where} line {self.line}: {self.excerpt}"


SECRET_PATTERNS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "GitHub token": r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}\b",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    "Google API key": r"\bAIza[0-9A-Za-z\-_]{35}\b",
    "private key": r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",
    "credential assignment": r"(?i)\b(?:api[_-]?key|secret|password|passwd|auth[_-]?token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
}
PII_PATTERNS = {
    "email address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "phone number": r"(?<![\d-])(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?![\d-])",
    "US SSN": r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])",
    "card number": r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)",
}
_SECRET_RE = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}
_PII_RE = {k: re.compile(v) for k, v in PII_PATTERNS.items()}


def _redact(s: str) -> str:
    s = s.strip()
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "…"


def _luhn(digits: str) -> bool:
    nums = [int(c) for c in digits][::-1]
    total = sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums))
    return total % 10 == 0


def scan_secrets(text: str, where: str) -> list[Finding]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in _SECRET_RE.items():
            for m in rx.finditer(line):
                found.append(Finding(kind, where, n, _redact(m.group(0))))
    return found


def scan_pii(text: str, where: str, allow: tuple[str, ...] = ()) -> list[Finding]:
    allow_re = [re.compile(a) for a in allow]
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in _PII_RE.items():
            for m in rx.finditer(line):
                hit = m.group(0)
                if kind == "card number":
                    digits = re.sub(r"\D", "", hit)
                    if not (13 <= len(digits) <= 19 and _luhn(digits)):
                        continue
                if any(a.search(hit) for a in allow_re):
                    continue
                found.append(Finding(kind, where, n, _redact(hit)))
    return found
