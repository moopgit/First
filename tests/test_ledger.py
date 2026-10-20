from __future__ import annotations

import multiprocessing as mp

from swarm.ledger import Ledger


def test_chain_verifies_and_detects_edits(tmp_path):
    led = Ledger(tmp_path)
    for i in range(5):
        led.append("note", run="R1", actor="t", i=i)
    assert led.verify() == []
    lines = led.path.read_text().splitlines()
    lines[2] = lines[2].replace('"i":2', '"i":9')
    led.path.write_text("\n".join(lines) + "\n")
    assert any("line 3" in p and "edited" in p for p in led.verify())


def test_deleted_line_breaks_the_chain(tmp_path):
    led = Ledger(tmp_path)
    for i in range(4):
        led.append("note", run="R1", actor="t", i=i)
    lines = led.path.read_text().splitlines()
    del lines[1]
    led.path.write_text("\n".join(lines) + "\n")
    problems = led.verify()
    assert any("chain broken" in p for p in problems) and any("seq" in p for p in problems)


def _writer(path, n):
    led = Ledger(path)
    for i in range(n):
        led.append("note", run="R1", actor="proc", i=i)
        led.allocate("T")


def test_concurrent_processes_never_lose_or_corrupt_records(tmp_path):
    procs = [mp.Process(target=_writer, args=(tmp_path, 40)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    led = Ledger(tmp_path)
    assert len(led.events()) == 160
    assert led.verify() == []
    assert led.allocate("T") == "T161"  # every allocation was unique


def test_events_filter_by_run(tmp_path):
    led = Ledger(tmp_path)
    led.append("a", run="R1", actor="x")
    led.append("b", run="R2", actor="x")
    assert [e["type"] for e in led.events("R2")] == ["b"]
