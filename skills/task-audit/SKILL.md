---
name: task-audit
description: Audit an ergon task tracker for stale work — tasks already done by later commits, made moot by retired components, citing files or lines that moved, missing a companion-repo marker, or blocked on dependencies that can never clear — then fix briefs and hand the human the close/drop commands. Use when asked to "audit the tasks", "what's out of date", "what can be closed or dropped", "clean up the backlog", or "review the epics".
---

# Task audit

Finds tasks the world has moved past and brings the tracker back in line. The mechanical half is `ergon audit`; the judgment half (is it really done? what's the brief now?) is yours. Read the `ergon` skill first: the one rule still holds. You never change `status:` or `approved:`, and `reject` / `close` / `approve` are the human's.

## 1. Mechanical pass

```bash
ergon audit            # whole tracker
ergon audit --epic E-004
```

It reports leads, not verdicts:

| Section | Means | Usual fix |
|---|---|---|
| Dependencies | dep is dropped/missing (never clears); `ready` waiting on a `proposed` dep; `proposed` with every dep done; `blocked` with nothing open | edit `depends:`; tell the human which deps to approve |
| Commits cite it | an open task is named in a commit message | read the commit: done → human `close --as done`; partial or just a note → leave |
| touches: | a companion repo's code is cited but `touches:` doesn't name it (dispatch would block), or a path that no longer exists | add the companion name first; fix the path |
| Citations | `path:line` past the end of the file, or a file that's gone | re-find the behaviour, cite the current line |
| Retired nouns | names something in the project's `retired` file | narrow the brief, or drop if that was the whole task |
| source: lines | `<log>:<n>` past the end of the log | fix the line |
| Epics | complete-but-open, no tasks, nothing approved in weeks | `finish`, break down, or ask the human |

When the project retires something, add a row to its `retired` file (`term | gate | note`; gate `-` or the task ID that retires it).

## 2. Judgment pass

The mechanical pass misses tasks whose *premise* changed (a decision reversed, a date passed, a live fix that was logged but not committed). For each open task, check its concrete claim against:

- **Code**: this repo and each companion checkout (`.ergon.toml`). Line numbers drift; look for the behaviour.
- **History**: `git log --oneline --grep=T-NNN`, and the newest entries in the project's log (`log` in `.ergon.toml`). A log entry often records a live fix with no commit.
- **Decisions**: plans, design docs, map cards, epic Constraints. Dates in a brief (go/no-go, deadlines) that have passed.

Classify each task:

| Verdict | Action |
|---|---|
| Done (a commit or log line proves it) | human: `close --as done` (approved) or `reject` (proposed), reason = the evidence |
| Moot (thing removed, decision reversed, date passed) | human: `close --as dropped` / `reject`, reason = why |
| Partly stale | rewrite `## What` / `## Done when` to what's left |
| Citations stale | update `file:line`, add the companion name to `touches:` |
| Still valid | leave it |
| Needs a live check | name the exact read-only command and where to run it; run it only if it's read-only and you have access; otherwise list it for the human |

For a big tracker, fan out by epic to parallel agents (read-only triage), then verify the claims you'll act on yourself (`git log -1 <sha>`, a grep) before editing anything. Agents' verdicts are leads too.

## 3. Apply

- **Briefs:** Edit tool only (the guard hook refuses shell writes to task files). Append `YYYY-MM-DD audit: <what changed>` to `## Notes`. Editing an approved (`ready`) task's brief is allowed when the user asked for the audit, but say which ones you changed: the human approved the old text.
- **Dependencies:** remove deps on dropped tasks; if a dep is being closed as done in this audit, leave it.
- **Epics:** edit Goal / Out of scope / Constraints to match what's left; never change an epic's `status:` yourself.
- **Close/drop commands:** write them to `<state>/runs/audit-YYYY-MM-DD.sh` (gitignored) for the human to run. Each `reject` / `close` asks for confirmation on the terminal:

```bash
ergon reject T-018 --reason "done: exporter already deployed (5820fa2)"
ergon close T-007 --as done --reason "90cb58d; variable gone"
ergon close T-006 --as dropped --reason "moot: nothing reads that setting any more"
ergon finish E-009
ergon set E-008 status=dropped     # an epic; only after its tasks are dropped
```

- **Map cards:** a card bullet ending `→ task T-NNN` for a task you're closing must go in the same change, or `ergon check` fails once the human runs the script. List them for the human if the card edit should wait.
- Re-run `ergon audit` and `ergon check` at the end and report what's left.

## Report

Group by verdict, one line per task with its evidence (commit sha, `file:line`, `<log>:<n>`). Then: the script path, which `ready` briefs changed, live checks still owed, and epic-level changes. Don't edit the project's log unless asked.
