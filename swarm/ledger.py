"""Append-only, hash-chained event ledger.

The ledger is the only state the harness writes. Task status, budgets, the
governor's inbox, the status board and every count are projections of it
(see state.py), so nothing can drift out of sync with what actually happened.

Concurrency: appends hold an exclusive flock on a sidecar lock file (never
deleted), re-read the tail under the lock, and write the whole line with one
O_APPEND write followed by fsync. Any number of threads or processes can append
safely. Each event carries the previous event's hash, so an edited or deleted
line is detectable by `verify()`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

GENESIS = "0" * 64


class LedgerError(RuntimeError):
    pass


def _canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(event: dict) -> str:
    body = {k: v for k, v in event.items() if k != "hash"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.jsonl"
        self._lock_path = self.dir / "ledger.lock"
        self._counters_path = self.dir / "counters.json"
        self._mutex = threading.Lock()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._mutex:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def append(self, type: str, *, run: str, actor: str, task: str | None = None, **data) -> dict:
        with self._locked():
            seq, prev = self._tail()
            event = {
                "seq": seq + 1,
                "ts": now_iso(),
                "run": run,
                "task": task,
                "type": type,
                "actor": actor,
                "data": data,
                "prev": prev,
            }
            event["hash"] = _digest(event)
            line = _canonical(event) + b"\n"
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                before = os.fstat(fd).st_size
                if os.write(fd, line) != len(line):
                    os.ftruncate(fd, before)
                    raise LedgerError("short write to ledger; rolled back")
                os.fsync(fd)
            finally:
                os.close(fd)
        return event

    def allocate(self, prefix: str) -> str:
        """Next sequential id for a prefix (R1, T1, D1...), safe across processes."""
        with self._locked():
            counters = json.loads(self._counters_path.read_text()) if self._counters_path.exists() else {}
            counters[prefix] = counters.get(prefix, 0) + 1
            tmp = self._counters_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(counters))
            os.replace(tmp, self._counters_path)
            return f"{prefix}{counters[prefix]}"

    def events(self, run: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "rb") as f:
            for n, raw in enumerate(f, 1):
                try:
                    event = json.loads(raw)
                except (ValueError, RecursionError) as e:  # ValueError covers JSONDecodeError and parser limits
                    raise LedgerError(f"ledger line {n} is not valid JSON - run `swarm audit`") from e
                if run is None or (isinstance(event, dict) and event.get("run") == run):
                    out.append(event)
        return out

    def verify(self) -> list[str]:
        """Return every integrity problem found; an empty list means the chain is intact."""
        problems: list[str] = []
        if not self.path.exists():
            return problems
        prev, expected_seq = GENESIS, 1
        with open(self.path, "rb") as f:
            data = f.read()
        if data and not data.endswith(b"\n"):
            problems.append("last line is incomplete (crash mid-write?)")
        for n, raw in enumerate(data.splitlines(), 1):
            try:
                event = json.loads(raw)
            except (ValueError, RecursionError):
                problems.append(f"line {n}: not valid JSON")
                continue
            if not isinstance(event, dict):
                problems.append(f"line {n}: not an event object")
                prev, expected_seq = "", expected_seq + 1
                continue
            seq = event.get("seq")
            if seq != expected_seq:
                problems.append(f"line {n}: seq {seq} (expected {expected_seq})")
            if event.get("prev") != prev:
                problems.append(f"line {n}: chain broken (prev hash mismatch)")
            if event.get("hash") != _digest(event):
                problems.append(f"line {n}: content does not match its hash (edited?)")
            prev = event.get("hash", "")
            expected_seq = (seq if isinstance(seq, int) and not isinstance(seq, bool) else expected_seq) + 1
        return problems

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = 4096
            while True:
                start = max(0, size - chunk)
                f.seek(start)
                buf = f.read(size - start)
                if not buf.endswith(b"\n"):
                    raise LedgerError("ledger tail is incomplete (crash mid-write?) - run `swarm audit`")
                lines = buf[:-1].rsplit(b"\n", 1)
                if len(lines) == 2 or start == 0:
                    last = json.loads(lines[-1])
                    return last["seq"], last["hash"]
                chunk *= 4
