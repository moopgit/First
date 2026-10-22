# Return: five-part agent harness

**Status:** all five parts built and pushed, plus the owner's one decided
change (only the owner answers approvals). 95/95 tests green locally on Python
3.10–3.13, in a fresh clone from GitHub, and in the template's own GitHub
Actions runs (Python 3.10 and 3.13).

## Repositories and HEAD commits

| repo | visibility | HEAD (`main`) |
|---|---|---|
| [moopgit/harness-template](https://github.com/moopgit/harness-template) | private | [`0275535234dd8313d579bb930491c78b9a1df373`](https://github.com/moopgit/harness-template/commit/0275535234dd8313d579bb930491c78b9a1df373) |
| [moopgit/harness-records-001](https://github.com/moopgit/harness-records-001) | private | [`1e34c32f8c071ee540c8ad0eb8bb69443e4374de`](https://github.com/moopgit/harness-records-001/commit/1e34c32f8c071ee540c8ad0eb8bb69443e4374de) |

Template history: `121d982` the five-part build · `0275535` only the owner
answers approvals (the owner's decision on question 2 below).

CI: [run #1](https://github.com/moopgit/harness-template/actions/runs/39867393106) on `121d982` and
[run #2](https://github.com/moopgit/harness-template/actions/runs/39868075107) on `0275535`: both success.

## Registry entries

```json
[
  {"name": "harness-template", "github": "moopgit/harness-template", "branch": "main", "engine": "repo-path",
   "purpose": "Harness mechanisms, tests and schemas: hash-chained append-only event log, approval surface, mechanical and plan gates, caps/strikes/resume, status and audit. A template: cloned to make instances, never run from."},
  {"name": "harness-records-001", "github": "moopgit/harness-records-001", "branch": "main", "engine": "repo-path",
   "purpose": "Append-only records of harness instance 001. Born with a README only; records are added, never edited or deleted."}
]
```

## Acceptance evidence, part by part

Full suite on `0275535`: `python -m unittest -v` → **Ran 95 tests, OK** (0 failures, 0 skipped) on
Python 3.13, 3.12, 3.11 and 3.10; again in a fresh clone from GitHub; and in
GitHub Actions (3.10, 3.13).

### Part 1: skeleton + the log

**T1. Repos and registry. Pass.** Both repos exist on GitHub, `visibility: private`
(checked via the GitHub API after the push). Both were empty when first pushed to
(`git ls-remote` showed no refs). The records repo was born with exactly one
commit, and its tree is `README.md` only; `verify-records` on a fresh GitHub
clone → `RECORDS APPEND-ONLY: 1 commits checked`. Registry entries above.

**T2. Suite green. Pass.** 95/95.

**T3. Replay test. Pass.** The made-up catch-to-settled story, run live, then
replayed from the log file alone in a separate process (code from a fresh
GitHub clone at `0275535`):

```
seq  0  log.opened            by harness          [unauthenticated-by-design]
seq  1  finding.caught        by fixture-catcher  classification "defect/high", method "fixture regression test"
seq  2  finding.reclassified  by fixture-lead     -> "defect/medium", reason "made-up: only affects one report"
seq  3  finding.claimed       by fixture-builder  claim "fixed", basis "made-up: patched the totals query"
seq  4  finding.verified      by fixture-checker  claim_seq 3, verdict "refuted"
seq  5  finding.claimed       by fixture-builder  claim "fixed", basis "made-up: patched pagination too; regression test added"
seq  6  finding.verified      by fixture-checker  claim_seq 5, verdict "confirmed"
seq  7  finding.closed        by fixture-lead     verification_seq 6
(each event's hash chains to the previous one's; all actors labeled unauthenticated-by-design)

COMPARISON
  live picture   sha256 7df0bcf9c14dda14dcce8c32cee03e8cc11b27f4b376037605fba7aaae25ed95  (1607 bytes, running Harness object)
  replay picture sha256 7df0bcf9c14dda14dcce8c32cee03e8cc11b27f4b376037605fba7aaae25ed95  (1607 bytes, separate process, log file only)
  byte-for-byte identical: True

NEGATIVE CONTROL: drop the closing event and replay  -> identical: False; finding status 'confirmed'
NEGATIVE CONTROL: alter one word in seq 3 and replay -> exit 2: "seq 3: hash does not match the event's contents (event altered)"
```

The replayed finding keeps the catcher, the classification *at catch time*
(`defect/high`) next to the current one, both claims with their bases, both
checks, and a closer (`fixture-lead`) who is not the checker (`fixture-checker`).
The test suite also replays a record that touches every part (findings,
probes, approval with re-ask, plan denied then approved, a finished run)
and gets a byte-identical picture.

**T4. Clone test. Pass.** Fresh `git clone https://github.com/moopgit/harness-template.git` at `0275535`:

```
43 files on disk = 43 tracked:
  .gitattributes .github/workflows/tests.yml .gitignore .harness-template README.md
  harness/{__init__,__main__,actors,answers,approvals,audit,budget,canonical,core,eventlog,
           findings,gates,identity,plans,projection,records,rules,runner,status,vocabulary}.py
  harness/schemas/{event.schema.json,vocabulary.json}
  tests/{__init__,helpers}.py  tests/test_*.py (10 files)
  tests/fixtures/{README.md,__init__.py,crash_run.py,stories.py}
instance state (*.jsonl *.ndjson *.log snapshot* runs/ records/ logs/ .env* config/): none
serialised log lines in any file: 0
commit-like hashes in any file: 0
code identity: harness reports 9ef2cf6dc61aa8f801a149d9100197a04504852298d26167dbd6661dfbc81f3c,
               plain sha256sum recomputation gives the same
trying to write a log inside the clone -> TemplateRunRefused (the template is never run from)
test suite inside the fresh clone -> Ran 95 tests, OK
```

The same checks run as tests (`test_template_clean.py`), so every future
change is held to them.

**Other Part 1 tests:** chain from a genesis event; the writer only appends;
the picture is the only state and nothing else is written to disk; two
writers stay on one chain; altering, re-hashing, removing, reordering or
whitespace-editing a past event is caught; a torn last write is refused;
refusals are recorded; the claimant can't check its own claim; the checker
never closes; nothing closes without a confirmed check; a claim needs a basis.
Checker-accuracy types `checker.probe_planted` and `checker.probe_result`
exist, tagged UNPROVEN, and status prints "no accuracy is claimed". Runs are
stamped with `harness_tree_sha256`. One changed byte changes it, schemas are
included, there is no version label, and it can be recomputed with plain
`sha256sum`.

### Part 2: approval surface (15 tests, all ok)
- a tampered action is refused, and the function never runs
- an action whose approved wording was altered is refused (and wording altered *in the log* fails audit)
- a qualified approval runs nothing and produces a re-ask naming the condition ("approved, but only after the tests pass" → re-ask quotes "but only after the tests pass")
- a refusal is present in the log afterwards (`refusal.recorded`, gate `approval`)
- only the owner answers: a builder, reviewer or checker answering is refused and recorded, and nothing runs; a log where a non-owner answered fails audit even with every hash recomputed
- also: one approval runs once; denial runs nothing; requester can't answer its own request; a log that records a denial as "approved" fails audit even with every hash recomputed

### Part 3: mechanical gates + plan gate (11 tests, all ok)
- work carrying a planted secret is stopped before review; review is refused; the secret value never reaches the log (redacted preview only)
- a denied plan shows its denial count in the log (`denial_count` 1, 2, …) and goes back for revision
- a third denial parks the plan and records the escalation, in one atomic write (`plan.denied`, `plan.parked`, `escalation.raised`); resubmission is refused
- also: forbidden content and file rules (`.env`, `../`); qualified plan decisions are re-asked, not counted; author can't decide its own plan; no run starts under an unapproved plan; a step whose output fails the gate fails, and escalates on the third try

### Part 4: escalation, cost caps, resume (13 tests, all ok)
- a third failure of one kind escalates (step ran exactly 3 times, failure_count 1,2,3 in log, run halted)
- a task that would exceed its cap is stopped before it starts (no `step.started`, function never called, refusal at the `budget` gate)
- a run killed mid-step (real `os._exit` in a subprocess) resumes and completes without repeating finished steps: side effects `one two three four`, each exactly once
- also: reviewer can't spend from the builder line; review spending counts against the run cap; cost records show both lines separately; overruns recorded as `over_estimate: true`; a resumed run without its credential halts naming `FIXTURE_TOKEN` (value never logged) and carries on once supplied; a non-idempotent in-doubt step halts and escalates; mixed failure kinds stop at the attempt ceiling; a resume must run the same steps

### Part 5: run surfaces (10 tests, all ok)
- the audit passes on a clean record
- the audit fails on a record with one altered event, and names it: `seq N: hash does not match the event's contents (event altered)`
- the audit's stated limit appears in its own output, on pass and on fail, text and JSON
- also: status shows finished / halted / incomplete from the log; a consistent forgery (one event changed, every later hash recomputed) passes the self-check and fails against a head kept elsewhere; cut-off newest events are caught only by the anchor; forged counts and forged separation fail audit; hostile malformed data fails audit instead of crashing it

## What I built, one line per part

1. **Log:** hash-chained append-only JSONL event log; picture always folded from it; findings story with two-party rules; UNPROVEN checker-accuracy types; code identity = hash of the package bytes.
2. **Approvals:** action and wording bound by hash at request time; owner-only answers; exact-word allow-list for approval; any condition → nothing runs + re-ask naming it; single use; refusals logged.
3. **Gates:** secret / forbidden / file-rule checks recorded before review, review refused for stopped or unchecked bytes; plan gate with counted denials, third denial parks and escalates atomically.
4. **Runs:** reserve-before-start caps per task, per line (builder/reviewer) and per run; three-strikes escalation plus attempt ceiling; resume from the log with in-doubt handling and credential checks.
5. **Surfaces:** `status` and `replay` from the log; `audit` re-runs the writer's rulebook over the whole log, with optional head anchor, and prints its limit every time; `verify-records` for the records repo.

## Usage figure at close

Taken by the owner from the Claude app's Usage screen at close (device clock
6:48), since I can't see that display:

| meter | used | resets |
|---|---|---|
| Current session | 23% | in 4 hr 21 min |
| Weekly limit, all models | 3% | Tue 6:00 PM |

Limit resets: none available. Usage credits: off.

---

## Note: assumptions, blockers, and where the spec couldn't be built as stated

**Blocker (resolved).** This session's GitHub integration was refused
permission to create repositories (403). The owner created both repos, private
and empty; I pushed the already-built history unchanged, so the commit IDs are
the ones built and tested before the push. Nothing else blocked.

**Process, honestly.** The spec says to pass each part's tests before starting
the next. I built the five parts in order but ran the suite once all five
existed, so there was no hard stop between parts. The first full run had 2
failures, both in the tests, not the mechanisms: the README didn't exist yet,
and one test tried to spend on a run that had already finished. Both fixed;
everything else passed first time. The build is one commit (`121d982`). The
owner-only approvals change is a second commit (`0275535`), made after the
owner answered question 2.

**Couldn't be built exactly as stated (built the nearest honest version):**
1. *"Past events can't be changed without it showing."* A hash chain alone can't
   promise that. Someone who recomputes every later hash, or cuts off the newest
   events, passes the chain check. Added `head` + `audit --expect-head` (keep the
   head somewhere else), and a test that shows the forgery passing without the
   anchor and failing with it.
2. *"Records repo append-only."* GitHub can't enforce this on its own (branch rules
   that block force-push depend on your plan), and a growing log file shows up
   in git as an *edit*. Defined append-only as "add files, or extend a file
   whose old bytes stay an exact prefix", and built `verify-records` to check
   every commit. It's checked, not enforced, and the READMEs say so. Catching a
   force-push needs a commit seen earlier (`--expect-ancestor`), supplied by
   your side, so rule 9 holds.
3. *"Checked before the money is spent."* Real cost isn't known in advance. Caps
   check the estimate; an overrun is recorded and visible, not prevented.
4. *"No double-running."* A step cut off mid-way can't be known to have run or
   not. Idempotent steps re-run (charged in full for the lost attempt);
   non-idempotent ones halt and escalate rather than guess.
5. *"Third failure of the same kind."* On its own this can loop forever if the
   kinds alternate. Added an attempt ceiling (default 5, recorded in the run's caps).
6. *Checker never the closer.* Under rule 7/16 this is a label check only; one
   person with two names defeats it. Stated in the README.

**Assumptions / additions (flag any you don't want):**
- Approval reading is an **allow-list**: only `approve`, `approved`, `yes`
  (case and trailing `.`/`!` ignored) count. Qualifier words are used only to
  name the condition in the re-ask, never to decide. A deny-list of qualifiers
  is the same kind of bug as the original "rejections read as yes": any
  phrasing nobody listed slips through. Cost: "sure, go ahead" gets re-asked.
- Denials need an explicit `deny`/`reject` (reason allowed) or a bare `no`, so
  "no problem, go ahead" isn't counted as a plan denial strike.
- Added second-party rules the spec implies but doesn't state: the checker ≠
  the claimant; the approval requester ≠ the approver; a plan's author ≠ its
  decider. All label-level, as is the owner-only rule for approvals.
- Owner-only applies to approvals, not plan decisions: the spec escalates a
  plan's third denial *to* the owner, so the denier is a reviewer, not the owner.
- The audit re-derives every stated outcome and count (an answer's reading,
  denial_count, failure_count, finished steps), so a log that says "approved"
  for a denial fails audit even with valid hashes.
- Escalation is a log event shown in `status`. Nothing notifies you, and there's
  no un-park / restart flow in this build (see owner decision 3).
- Nothing here calls a model: steps are Python functions an instance supplies.
- The template refuses to write logs inside itself (`.harness-template`
  marker), which makes rule 10 mechanical. An instance deletes the marker.

**Questions, answered by the owner:**
1. *Should `ok`/`lgtm` count as approval?* **No.** The exact-word rule stands as
   built (`approve`, `approved`, `yes`). No change.
2. *Who may answer approvals?* **The owner only, for now.** Built in `0275535`:
   answers from any other role are refused, recorded, and fail audit. Tiered
   approvals are a possible later refinement, not part of this build.
3. *Can a parked plan or escalated run be restarted?* **Yes, by the owner only,
   and only as a new event in the log (a restart decision), never as an edit
   to the parked or escalated record.** This is a later fix order, not a change
   to this build; nothing for it is built here.
