from __future__ import annotations

import os

import pytest

from swarm import policy as pol


@pytest.mark.parametrize("text", ["approved", "Approved.", "yes", "yes, go", "go ahead", "ship it", "LGTM", "do it"])
def test_explicit_approvals_pass(text):
    assert pol.approval_problem(text) is None


@pytest.mark.parametrize("text", [
    "should be in I think", "approved?", "maybe go", "not approved", "no", "I guess so", "yes but hold off",
    "", "looks interesting", "go, unless legal objects",
])
def test_ambiguous_or_negative_replies_are_refused(text):
    assert pol.approval_problem(text) is not None


def test_paths_stay_in_the_workspace(cfg):
    for bad in ["../x", "/etc/passwd", "a/../../x", ""]:
        with pytest.raises(pol.PolicyError):
            pol.resolve(cfg, bad)
    target, norm = pol.resolve(cfg, "research/./a.md")
    assert norm == "research/a.md" and target.parent.name == "research"


def test_symlinks_cannot_escape(cfg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, cfg.workspace / "link")
    with pytest.raises(pol.PolicyError):
        pol.resolve(cfg, "link/secret.txt")


def test_private_paths_are_unreachable(cfg):
    with pytest.raises(pol.PolicyError):
        pol.resolve(cfg, ".env")
    with pytest.raises(pol.PolicyError):
        pol.resolve(cfg, "keys/deploy.pem")


def test_write_scope_and_structural_paths(cfg):
    writer = cfg.seats["writer"]
    pol.check_write(cfg, writer, "drafts/nested/a.md")
    with pytest.raises(pol.PolicyError, match="outside your lane"):
        pol.check_write(cfg, writer, "research/a.md")
    assert pol.is_structural_path(cfg.policy, "outbound/post.md")
    assert not pol.is_structural_path(cfg.policy, "drafts/post.md")


def test_command_allowlist_matches_whole_tokens(cfg):
    from dataclasses import replace

    seat = replace(cfg.seats["writer"], commands=("python3 -m pytest", "git push"))
    pol.check_command(seat, pol.parse_command("python3 -m pytest -q tests"))
    for bad in ["python3 -c 'import os'", "python3 -m pytestevil", "rm -rf /", "git pushx"]:
        with pytest.raises(pol.PolicyError):
            pol.check_command(seat, pol.parse_command(bad))
    assert pol.is_structural_command(cfg.policy, pol.parse_command("git push origin main"))


def test_secret_scanner(cfg):
    text = "ok\napi_key = 'abcdefghijklmnopqrstu'\nAKIAABCDEFGHIJKLMNOP\n-----BEGIN RSA PRIVATE KEY-----\nghp_" + "a" * 36
    kinds = {f.kind for f in pol.scan_secrets(text, "f")}
    assert kinds == {"credential assignment", "AWS access key", "private key", "GitHub token"}
    assert pol.scan_secrets("nothing to see; tokens are counted in usage", "f") == []


def test_pii_scanner_with_allowlist_and_luhn():
    text = "a@acme.com b@example.com 415-555-0132 123-45-6789 4111 1111 1111 1111 4111 1111 1111 1112 2026-10-20"
    kinds = sorted(f.kind for f in pol.scan_pii(text, "f", (r"@example\.com$",)))
    assert kinds == ["US SSN", "card number", "email address", "phone number"]
