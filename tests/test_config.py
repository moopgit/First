from __future__ import annotations

import pytest
from conftest import BASE, make_cfg

from swarm.config import ConfigError


@pytest.mark.parametrize("old,new,message", [
    ("reviewers: [checker]\n  writer", "reviewers: [research]\n  writer", "cannot review its own"),
    ("reviewers: [checker]\n  writer", "reviewers: [lead]\n  writer", "cannot grade it"),
    ("reviewers: [checker]\n  writer", "reviewers: [ghost]\n  writer", "unknown seat"),
    ("    writes: [\"drafts/*\", \"outbound/*\"]\n    reviewers: [checker]", "    writes: [\"drafts/*\"]", "need at least one reviewer"),
    ("  checker:\n    role: reviewer", "  checker:\n    role: router", "exactly one seat"),
    ("concurrency: 4", "concurency: 4", "unknown keys"),
    ("structural_commands", "secret_scan: false\n  structural_commands", "cannot be disabled"),
    ("workspace: ws", "workspace: state/ws", "must not contain each other"),
    ("writes: [\"research/*\"]", "writes: [\"../research/*\"]", "relative to the workspace"),
    ("pii_allow: ['@example\\.com$']", "pii_allow: ['(']", "not a valid regular expression"),
])
def test_bad_configs_fail_loud(tmp_path, old, new, message):
    assert old in BASE, old
    with pytest.raises(ConfigError, match=message):
        make_cfg(tmp_path, BASE.replace(old, new, 1))


def test_run_command_requires_an_allowlist(tmp_path):
    text = BASE.replace("tools: [read_file, list_files, search_files, write_file, delegate]", "tools: [read_file, run_command]")
    with pytest.raises(ConfigError, match="needs an allowlist"):
        make_cfg(tmp_path, text)


def test_config_hash_tracks_the_text(tmp_path):
    a = make_cfg(tmp_path)
    b = make_cfg(tmp_path, BASE + "\n# comment\n")
    assert a.hash != b.hash and a.router.name == "lead" and [s.name for s in a.workers] == ["research", "writer"]
