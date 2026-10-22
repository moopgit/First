# CLAUDE.md

This file provides guidance to AI assistants (Claude Code and others) working in this repository.

## Repository Overview

**Repository**: moopgit/First
**Status**: Freshly initialized — no source files committed yet.

This repository was created with a feature branch (`claude/add-claude-documentation-Mcpy3`) and contains no application code as of the initial setup. This CLAUDE.md serves as the foundational guide for AI assistants contributing to this project.

---

## Repository Structure

As of initial setup, only this `CLAUDE.md` exists. Update this section as the project grows:

```
First/
├── CLAUDE.md          # This file — AI assistant guidance
└── harness-build/     # Staging for the harness-template / harness-records-001 repos (bundles + return package)
```

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
