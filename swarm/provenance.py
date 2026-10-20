"""What the harness can observe about who is running it and which code is running.

Neither is authentication: the harness can't prove a person typed a command.
It records what the process can see, so the ledger states facts instead of
stamping a name.
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import os
import socket
import subprocess
from pathlib import Path

from . import __version__

# Environment markers that show a command ran inside an AI agent session. Names only, never values.
AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def invoker() -> dict:
    """The OS user, host, process and terminal this command ran from."""
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    tty = None
    try:
        if os.isatty(0):
            tty = os.ttyname(0)
    except OSError:
        pass
    return {
        "user": user,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "tty": tty,
        "agent_markers": [m for m in AGENT_MARKERS if m in os.environ],
    }


def principal(p: dict) -> str:
    return f"{p.get('user', 'unknown')}@{p.get('host', 'unknown')}"


@functools.lru_cache(maxsize=1)
def build() -> dict:
    """The code actually running: version, a hash of the package's source files, and git state if it's a checkout."""
    pkg = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for f in sorted(pkg.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".yaml") and "__pycache__" not in f.parts:
            digest.update(f.relative_to(pkg).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    out = {"version": __version__, "source_sha256": digest.hexdigest(), "git_head": None, "git_dirty": None}
    try:
        head = subprocess.run(["git", "-C", str(pkg), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if head.returncode == 0:
            out["git_head"] = head.stdout.strip()
            status = subprocess.run(["git", "-C", str(pkg), "status", "--porcelain", "--", "."],
                                    capture_output=True, text=True, timeout=10)
            if status.returncode == 0:
                out["git_dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return out
