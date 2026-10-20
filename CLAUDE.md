# CLAUDE.md

This file provides guidance to AI assistants (Claude Code and others) working in this repository.

## Repository Overview

**Repository**: moopgit/First
**Status**: Holds `swarm`, a code-enforced harness for running a team of Claude agents. It rebuilds the operating model from `moopgit/fleet` so its rules are enforced by code instead of prose. See README.md.

---

## Repository Structure

```
First/
├── swarm/             # the harness package (entry point: swarm/cli.py, `swarm` command)
│   └── templates/     # starter swarm.yaml files (basic, fleet)
├── tests/             # pytest suite; runs offline against scripted providers
├── pyproject.toml     # package metadata and dependencies
├── README.md          # what it is, how to use it, design
└── CLAUDE.md          # this file
```

## Language, Dependencies, Tests

- Python 3.10+. Runtime deps: `anthropic` (1.x SDK), `pyyaml`. Dev: `pytest`.
- Install: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- Tests: `.venv/bin/python -m pytest -q` (no network or API key needed).
- Offline smoke test: `swarm init && swarm run --demo "<goal>" && swarm audit` in a scratch directory.

## Key Conventions

- The ledger (`swarm/ledger.py`) is the only state the harness writes. Status, counts and the inbox are projections (`swarm/state.py`). Never add stored state that duplicates the ledger.
- A rule the harness enforces is declared and validated in `swarm/config.py`, enforced in `swarm/tools.py` or `swarm/orchestrator.py`, and checked by `swarm/audit.py` where the ledger can show it held. Add a test in `tests/test_orchestrator.py` for every new rule.
- Agents act only through the dedicated tools in `swarm/tools.py`. Don't add a general shell tool.
- Model calls go through `swarm/providers.py` only. Transcripts are append-only: never edit or drop earlier messages.

---

## Development Branch

Active development branch: `claude/add-claude-documentation-Mcpy3`

- All changes must be developed on the designated feature branch.
- Never push directly to `main` or `master` without explicit instruction.
- Use `git push -u origin <branch-name>` when pushing.

---

## Git Workflow

1. **Check out** the correct branch before starting work.
2. **Make focused commits** — one logical change per commit.
3. **Write descriptive commit messages** that explain the *why*, not just the *what*.
4. **Push** completed work with `git push -u origin <branch-name>`.
5. **Never force-push** to shared branches without explicit permission.
6. **Never skip hooks** (`--no-verify`) without being instructed to.

Commit message format:
```
<type>: <short summary>

<optional body explaining motivation and context>
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

---

## Working with AI Assistants

### General Principles

- Read files before modifying them.
- Do not create files unless necessary — prefer editing existing ones.
- Do not add features, refactoring, or "improvements" beyond what was asked.
- Do not add comments or docstrings to code you did not change.
- Do not add error handling for scenarios that cannot happen.
- Match the scope of changes to what was actually requested.

### Security

- Never introduce command injection, XSS, SQL injection, or other OWASP Top 10 vulnerabilities.
- Validate input only at system boundaries (user input, external APIs).
- Never commit secrets, credentials, or `.env` files.

### Reversibility

Before taking destructive or hard-to-reverse actions (deleting files, force-pushing, dropping data), confirm with the user unless explicitly pre-authorized.

---

## Adding to This Document

As this project evolves, keep CLAUDE.md current by updating:

- **Repository Structure** — add new top-level directories and their purpose.
- **Language & Runtime** — document the language, version, and runtime requirements.
- **Dependencies** — list the package manager and how to install deps (e.g., `npm install`, `pip install -r requirements.txt`).
- **Build** — document how to build the project.
- **Tests** — document how to run tests and where test files live.
- **Linting / Formatting** — document linters and how to run them.
- **CI/CD** — document the pipeline and any required checks before merging.
- **Environment Variables** — list required env vars and point to `.env.example`.
- **Key Conventions** — naming conventions, file organization patterns, architectural decisions.
