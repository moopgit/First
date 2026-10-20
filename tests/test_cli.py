from __future__ import annotations

from swarm.cli import main


def test_demo_run_end_to_end(tmp_path, capsys):
    cfg = str(tmp_path / "swarm.yaml")
    assert main(["-c", cfg, "init", "--template", "fleet"]) == 0
    assert main(["-c", cfg, "run", "--demo", "Draft a launch note"]) == 0
    out = capsys.readouterr().out
    assert "status: done" in out and "passed review by" in out
    assert main(["-c", cfg, "status", "--all"]) == 0
    assert main(["-c", cfg, "inbox"]) == 0
    assert "Nothing waiting on Andy" in capsys.readouterr().out
    assert main(["-c", cfg, "audit"]) == 0
    assert "FAIL" not in capsys.readouterr().out
    assert main(["-c", cfg, "log", "--task", "T1"]) == 0
    assert (tmp_path / ".swarm" / "runs" / "R1" / "RESULT.md").exists()


def test_errors_are_reported_not_raised(tmp_path, capsys):
    assert main(["-c", str(tmp_path / "missing.yaml"), "status"]) == 1
    assert "swarm init" in capsys.readouterr().err
