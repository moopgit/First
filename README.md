# swarm

A harness for running a team of Claude agents ("seats") under rules that are enforced in code.

It's a rebuild of the operating model in [`moopgit/fleet`](https://github.com/moopgit/fleet). The fleet's rules are good, but they live in about 2,200 markdown files and are mostly enforced by agents remembering to follow them. The fleet's own 2026-10-11 hard-rule audit ([`fleet/hard-rule-audit.md`](https://github.com/moopgit/fleet/blob/main/fleet/hard-rule-audit.md), line 31) counts 4 of 13 structural rules enforced, two of them built that day. Here, the rules that govern agent work (lanes, approvals, review, escalation, budgets) are code paths an agent can't skip. `swarm audit` proves the recorded history is internally consistent and untampered — under the assumption the harness wrote it.

## What it does

You give it a goal. A router seat breaks the goal into tasks. Worker seats run in parallel, each inside its own lane. Every result goes through mechanical checks, then an independent reviewer. Anything structural waits for your explicit yes. When the work is done, the router writes the final answer and lists the reviewed work behind it.

```
goal ─▶ router plans ─▶ seats work in parallel ─▶ mechanical gates ─▶ independent review ─▶ router synthesizes
                              │                         │ fail              │ fail
                              │                         └──── feedback ◀────┘   (3 failed cycles → you)
                              └─ structural action / blocked / unsafe ─▶ your inbox ─▶ approve / deny ─▶ resumes
```

## What's different from the fleet's current harness

| Fleet incident or rule (source) | Before: prose + vigilance | Now: mechanism |
|---|---|---|
| Main chat is a serial bottleneck (AGENTS.md, sanity check) | One chat relays everything | Router plans; independent tasks run concurrently (`concurrency:`) |
| "Should be in I think" treated as approval (dispatch-ledger case study) | Rule says ask first | Hedged or questioning replies are refused and logged as refusals; nothing executes until you use explicit words |
| Structural actions need Andy's yes (AGENTS.md) | Agent decides what's structural | Writes/commands matching `structural_*` don't run; they queue, and approval executes the exact queued action (hash-checked) |
| Builder never owns the verdict (S9) | Script checks after the fact | A seat can't be configured to review itself; reviewer runs in a fresh transcript |
| 3 failed verify→fix cycles escalate (S8, unbuilt) | Counter nobody kept | Counted per task; the 3rd failure lands in your inbox |
| Dispatch needs an authorizing source (S13, unbuilt) | Convention | Every run and task carries `requested_by`; empty is refused |
| Concurrent writers lost 76 records (S6) | flock specimen in components-lab | Hash-chained ledger with flock + atomic appends; tested with 4 processes |
| Counts and indexes rot (README, roster 9/11/12/14/15/16) | Hand-maintained | Nothing is hand-maintained: status, counts and the inbox are projections of the ledger |
| PR reviewer calibration sat 4 days untouched (case study) | Passive tracker | A task that isn't moving and has no named wait is flagged "stalled" |
| Waits need a resume trigger (DR-01) | STATE.md entry | `wait()` requires a trigger; the task pauses and resumes on that event |
| Gates: mechanical first, judgment last (DR-05) | Standard doc | Secret scan, PII scan, artifact checks and configured commands run before any reviewer sees the work |
| Rule changes mid-flight (DR-06) | Retraction notices | A run keeps the config snapshot it started under; moving to new rules is an explicit `--adopt-config` |
| Trace capture with red lines (DR-54) | Ledger shell script | Corrected work saves original / final / why; red-line classes never produce traces or snapshots |
| Secrets never publish (S12, S1) | Secret gate at publish | Secret scan can't be turned off, and secret findings can't be approved through |

What it deliberately does not carry over: the decision-record process, calibration ladders, and the meeting-shaped review rounds. Those are governance for a team of chat windows. Here, the config file is the governance, and the ledger is the record.

## Quick start

```bash
pip install -e .                 # Python 3.10+
swarm init                        # writes swarm.yaml (use --template fleet for the fleet's seats)
swarm run --demo "Outline a launch plan"   # offline scripted team, free: shows the whole flow
```

For real work, give it Claude credentials (`export ANTHROPIC_API_KEY=...` or `ant auth login`), put source material in `workspace/`, and drop `--demo`:

```bash
swarm run "Compare our three pricing options and recommend one, with the risks"
```

## Your controls

| Command | What it does |
|---|---|
| `swarm status [RUN]` / `swarm status --all` | The board: every task, its state, cost, and what's waiting on you |
| `swarm inbox` | Everything that needs you: approvals, consequential reviews, waits, escalations |
| `swarm inbox --show D3` | One item in full, including the exact file content or command that would run |
| `swarm approve D3 "approved"` | Grants it and continues the run. Needs explicit words (approved / yes / go / do it / ship it); "I think so?" is refused |
| `swarm deny D3 "not public yet"` | Denies it; the seat gets your reason and adapts. Denying an escalation parks the task |
| `swarm resume [RUN]` | Continues a paused or interrupted run from its checkpoints, on the credential it started with (`--adopt-credential` switches deliberately) |
| `swarm log [RUN] [--task T4]` | Every event, in order |
| `swarm audit` | Checks the whole ledger against the invariants below. Exit code 1 if any fail |

For a wait (a seat blocked on something only you have), `approve` takes your answer as plain text: `swarm approve D5 "Q3 pricing is $49/seat"`.

## The rules, in `swarm.yaml`

```yaml
governor: Andy                     # the only human, the only approver
concurrency: 4
budgets:
  task_cost_usd: 3.00              # per task, builder + reviewer
  run_cost_usd: 20.00              # whole run; work stops and pauses at the cap
  max_fix_cycles: 3                # failed gate/review cycles before escalation
policy:
  structural_paths: ["outbound/*"] # writes here wait for your approval
  structural_commands: ["git push"]
  private_paths: [".env", "private/*"]   # invisible to every seat
  redline_classes: [pii, job-search, calibration-private]
seats:
  writing:
    lane: Copy and outward drafts
    writes: ["drafts/*", "outbound/*"]   # the only paths this seat can write
    reviewers: [strategy]                # never itself, never the router
```

Typos and contradictions fail at load time (unknown keys, a seat reviewing itself, a worker with no reviewer, `run_command` without an allowlist). Full examples: [`swarm/templates/basic.yaml`](swarm/templates/basic.yaml) and [`swarm/templates/fleet.yaml`](swarm/templates/fleet.yaml).

Tiers: the planner marks each task `routine` (a peer seat reviews) or `consequential` (you review: anything outward-facing, hard to reverse, or about money, people or commitments).

## What `swarm audit` checks

`swarm audit` proves the recorded history is internally consistent and untampered — under the assumption the harness wrote it. It checks:

1. The ledger's hash chain is intact (no edited or deleted events).
2. Every event is structurally complete. Missing or mistyped fields are findings, not crashes.
3. Every run and task has an authorizing source.
4. Every event belongs to a started run, and every task event to a created task (no phantom tasks).
5. No task was reviewed by the seat that built it.
6. Every finished task ran the full configured gate set (artifacts, secret scan, PII scan unless the task is red-line, and the seat's checks) and passed all of it before an independent review passed, in its last submission window. In every window, gates come before the review, and each gate's `passed` agrees with its findings. The gate set comes from the run's config snapshot, verified against the hash the ledger recorded; with no matching snapshot, the check fails.
7. Every wait names a resume trigger.
8. Every approval was explicit and records who ran the command and the role it was run as.
9. Every approved action ran exactly as approved.
10. No task kept working past the fix-cycle cap.
11. No task is left "running" in a stopped run.

## How it's built

```
swarm/
  config.py        swarm.yaml schema and validation
  ledger.py        append-only, hash-chained, multi-process-safe event log
  state.py         runs, tasks and the inbox, projected from the ledger
  policy.py        sandboxing, lanes, structural actions, configured gates, approval wording, secret/PII scanners
  provenance.py    who ran each command, and which build of the code was running
  tools.py         the tools seats act through, and the policy-enforcing executor
  agent.py         one seat's tool-use loop, checkpointed transcript, budget
  orchestrator.py  planning, parallel scheduling, gates, review, inbox, resume
  providers.py     Claude (Anthropic SDK), scripted (tests), demo (offline)
  audit.py         the invariant checks
  cli.py           the governor's commands
```

- **Model calls:** Claude Opus 5.5 by default (`model:` per seat to change it), adaptive thinking, effort set per seat, prompt caching on the static prefix and the conversation tail, and server-side refusal fallbacks (`fallbacks: "default"`) on models that support them. Tool schemas are strict.
- **Agents act only through dedicated tools** (read, list, search, write, run allowlisted command, delegate, wait, escalate, submit), so every action can be gated and logged. Commands run without a shell, in the workspace, with credential-like environment variables stripped.
- **History is append-only.** Transcripts are checkpointed after every step, so a crash or a pause resumes exactly where it stopped, and the prompt cache stays warm.
- **State lives in `.swarm/`** next to `swarm.yaml`: `ledger.jsonl`, `traces.jsonl`, and `runs/<id>/` (config snapshots, transcripts, queued actions, submission snapshots, `RESULT.md`).

## Status and limits

- 109 tests cover the rules above with a scripted team, including adversarial ledger constructions for the audit. The Claude request shape is checked against the Anthropic SDK (1.15) at the HTTP layer, but it hasn't been run against the live API from this repo yet. Do a small paid run before relying on it.
- Costs are estimates from token counts and the price table in `config.py` (override under `prices:`).
- `run_command` is as powerful as the commands you allow. Allowing `python3 -m pytest` lets a seat run any test file it writes.
- The PII scanner is pattern-based (emails, US phone numbers, SSNs, card numbers). It catches the obvious cases, not everything.
- Decisions record who ran the command (OS user, host, terminal, and whether an AI agent session's environment markers were present) and the code build. That's provenance, not authentication: anyone with shell access to `.swarm/` can run `swarm approve`.
- A run is bound to the credential it started with. Resuming from a shell that would bill a different one, or has none, stops with a named error instead of escalating every task.
- One machine, one ledger. There's no web UI; `swarm status` is the board.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```
