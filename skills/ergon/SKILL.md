---
name: ergon
description: The ergon task-tracker workflow (humans approve, agents execute). Use in any repo with an .ergon.toml when asked to add, pick up, work, break down or dispatch tasks or epics, to "break down E-NNN", "what's next", "file a task for this", or before touching a task file. Covers the approval rule, tiers, task file schema, epic branches and headless dispatch.
---

# ergon: open work, sized by which model can do it

One markdown file per task and per epic. Each epic is a folder, `<state>/epics/E-NNN-slug/`, holding its brief (`EPIC.md`) and all of its tasks (`T-NNN-slug.md`), done and dropped ones included. Tasks with no epic live in `<state>/items/`. `<state>` is `state` in the project's `.ergon.toml` (default `tasks`). Frontmatter is the state; the body is the brief. The `ergon` CLI reads and writes it: run `ergon -h`.

The `epic:` field decides the folder. `ergon set T-NNN epic=E-NNN` moves the file (`epic=` moves it back to `items/`). After a hand edit, `check` fails until `ergon tidy` re-files it. Other files in an epic's folder, such as design notes, are ignored by the tracker.

**Task state is local.** `items/`, `epics/` and `runs/` are gitignored (`ergon init` adds the lines). Git keeps the record of what was done: every commit for a task cites its ID (`… (T-128)`), and an epic's merge commit lists its goal, each task, and the commits that cite it. Anything that should outlive your machine goes in a commit message, not in a task file.

The project's own rules (tier signals that are specific to it, hosts that need a human, what workers must never touch) live in its `CLAUDE.md`/`AGENTS.md` and in `worker_rules` (`ergon rules` prints them). Read them before working a task.

## The one rule: humans approve, agents execute

- Every task an agent creates starts `status: proposed`. That includes tasks from breaking down an epic.
- Only a human moves a task to `ready`: either `ergon approve <ID|E-ID>` from their own terminal (it refuses inside agent shells and asks for confirmation), or by hand-editing `status: ready` + `approved: <name> <date>`.
- **Agents never write `approved:`, never set `status: ready` on a proposed task, and never work a task that isn't `ready`.** `ergon set` enforces this, and `ergon check` fails on any task past `proposed` that has no stamp. The plugin's PreToolUse hook (`ergon hook guard`) also refuses agent edits that change a task file's `status:` or `approved:`, shell writes to task files, `approve`/`reject`/`close`/`review -i`, and touching `CLAUDECODE`/`AI_AGENT`. Its Stop hook runs `check` once before a session ends. All of it is a speed bump, not security. Bypassing it breaks this rule.
- Reviewing a long queue: `ergon review -i [--epic E-00N] [--tier T]` walks it one task at a time (approve / reject / change tier / open in `$EDITOR` / skip). Human only, like `approve`.
- Pick work with `ergon next --tier <your tier>`. Take only tasks at your tier or below. To run many at once, see Dispatch below.

## Tiers: the cheapest one that will get it right

| Tier | Give it | Signals |
|---|---|---|
| `haiku` | Mechanical and fully specified | One file or a few. The exact change is already written in the task. Checking it is trivial (a grep, a lint, a doc row). No judgment about behavior |
| `sonnet` | Scoped engineering | One app or subsystem, clear acceptance criteria, has to read code and run tests. Most bug fixes, doc-drift passes, config fixes |
| `opus` | Cross-cutting, ambiguous, or security-sensitive | Spans subsystems or repos. Touches auth, signing, secrets or a stated invariant. Root cause unknown. Needs a design decision first |
| `human` | Things no model should or can do | Live production changes, third-party consoles, credentials, on-chain or legal actions, risk or policy calls |

A project can pin minimums with `[[protect]]` in `.ergon.toml` (`touches = ["server/auth/**", "prod-db"]`, `min_tier = "opus"` or `"human"`, optional `note`). `ergon check` fails any open task whose `touches:` match a pattern (fnmatch, also against parent directories) but whose tier is below the minimum. Done and dropped tasks are ignored.

When in doubt, go one tier up. Put the reason in `tier_why:` in one line. If a task has a human step and a code step, split it into two tasks and link them with `depends:`.

## Lifecycle

```
proposed --(human approve)--> ready --> doing --> done
    \--(human reject)--> dropped     \-> blocked -/
```

Agents move tasks through `ready → doing → done|blocked` with `ergon set T-012 status=doing`. When you finish one, the commit message says what changed and cites the task ID. `## Notes` is local scratch. For a `human` task with no code change, record the evidence in the epic's next commit or in the project's log.

`ergon close ID --as done|dropped --reason R` (human only) closes an approved task whose work landed elsewhere or that no longer applies.

## Map links (optional)

If `.ergon.toml` sets `map`, cards under it can link a known gap to the task that fixes it with a trailing `→ task T-NNN`. When you mark that task done, remove the bullet (or the link) in the same change: `ergon check` fails on links to missing or closed tasks. When you create a task for a gap a card already describes, add the link there.

## Task file

```yaml
---
id: T-012
title: Imperative, ≤70 chars
tier: sonnet            # haiku | sonnet | opus | human
status: proposed        # proposed | ready | doing | blocked | done | dropped
epic: E-002             # optional
depends: [T-010]        # must be done before this is pickable
touches: [server/auth, prod-db]   # paths, hosts, nouns, companion repo names
source: TODO.md:1425    # where it came from (the `log` file, an issue, a doc)
created: 2026-10-04
approved:               # "<name> <date>", written by a human only
tier_why: one line
---
## What
## Done when
## Notes
```

The body sections stay short. Cite `file:line`. Don't restate the spec.

## Epics → tasks

1. A human writes the epic: `ergon new epic "Title"`, then fills in Goal / Out of scope / Constraints.
2. When asked to "break down E-00N", an agent (opus) reads the epic and the source. It then creates tasks with `ergon new task … --epic E-00N`, one change or one human action each. It fills in What / Done when / `tier`/`tier_why`/`depends`/`touches` and **stops**. It doesn't start any of them.
3. The human runs `ergon review`, edits or rejects what's wrong, and runs `ergon approve E-00N` to approve the rest at once.
4. When every task is done or dropped, the epic closes and its branch merges into the trunk (see below).

Re-breaking an epic later only adds new `proposed` tasks. It never edits approved ones.

## One branch per epic

- **Start.** `ergon start E-00N` cuts `epic/<epic folder name>` from the trunk (`trunk` in `.ergon.toml`, default `main`) and checks it out in its own worktree, `<worktrees>/E-00N` (default `.claude/worktrees`). Your checkout stays on whatever branch it's on. Moving an epic's first task to `doing` with `set` does the same, then refuses and tells you to `cd` into that worktree and run it again. If the branch is already checked out somewhere, `start` just tells you where.
- **Work.** Work an epic in the checkout that has its branch. Task files are shared by every worktree (they live in the main checkout). Every commit for the epic's tasks goes on that branch, including nested-repo gitlink bumps, and cites the task ID. Once the branch exists, `set` refuses status changes on the epic's tasks from any other branch. Marking a non-`human` task `done` warns if no commit on the branch cites it.
- **Several epics at once.** One worktree per epic, one session (or dispatch run) per worktree. `new`, `set`, `start`, `finish` and each dispatch step take a lock on `<state>/items/.lock`. Epics that touch the same files will conflict when the second one merges; `dispatch --dry-run` lists tasks whose `touches:` overlap.
- **Finish.** When `set` marks the epic's last open task `done` or `dropped`, it merges the epic branch into the trunk with `--no-ff` without switching any checkout. The merge message is `Merge E-00N: <title>`, followed by the epic's Goal and each task with the commits that cite it. Then it removes the epic's worktree (or detaches it if you're inside it or it holds untracked files), deletes the branch, and sets the epic `done`. It merges locally and **never pushes**.
- **If the merge can't run** (uncommitted changes in the epic's checkout, a conflict, or local changes where the trunk is checked out), nothing changes and `set` says why. Fix it (for a conflict, merge the trunk into the epic branch), then run `ergon finish E-00N`. `set E-00N status=done` is refused, so an epic can't close with its branch unmerged.
- Nested repos (gitlinks) aren't branched. Their commits land on their own default branch; the epic branch only carries the bumps.

## Dispatch: headless workers, one worktree each

`ergon dispatch` runs `ready`, unblocked tasks without anyone watching. Run it from the main checkout.

```
ergon dispatch --dry-run               # what would run
ergon dispatch                         # up to 6 tasks, 3 at a time
ergon dispatch --epic E-012 --tier sonnet --jobs 2 --budget 5 --budget-total 20 --budget-pct 80   # $5/worker, stop launching at 80% of $20
ergon dispatch --ids T-202,T-188
ergon dispatch --watch --budget-total 20 --interval 30   # keep running; pick up tasks you approve meanwhile
```

- **`--watch`** keeps the dispatcher alive, rescanning for newly `ready` tasks every `--interval` seconds (default 30) until the cap is reached or Ctrl-C. It requires `--budget-total`, and `--max` doesn't limit it unless given. The state lock is held only during a scan, so `approve` works between scans. Ctrl-C once: no new launches, running workers finish. Ctrl-C twice: workers are killed and their tasks marked `blocked`.
- **Per task:** a worktree at `<worktrees>/<ID>` on branch `task/<ID>`, cut from the epic branch (created as a ref if needed, never checked out) or from the trunk. A headless `claude -p --agent task-<tier>` works the task there: model = tier, permission mode `auto` by default. The worker definitions are the plugin's `agents/task-<tier>.md`; a project overrides one with its own `.claude/agents/task-<tier>.md`. The project's `worker_rules` file is inlined into every worker's prompt.
- **Companion repos.** A `[[companion]]` in `.ergon.toml` is a separate repo checked out inside this one (a public product repo next to a private install, say). A task whose `touches:` names it gets its own git worktree of that checkout, at `<task worktree>/<companion path>`, on a `task/<ID>` branch cut from the companion's local trunk. The worker commits there, never in the shared checkout; the `--worker` guard refuses edits, shell writes and mutating `git` aimed at the shared checkout. With `forbid_tracker_ids = true`, companion commits must not cite task IDs; the worker's own commit cites the task and names the companion commit. A task that doesn't name the companion gets no checkout of it. Tasks naming a companion with no checkout are skipped.
- **Status is the dispatcher's.** It sets `doing` at launch. Workers never run `ergon set` (it refuses on a `task/` branch); they end with `RESULT: done` or `RESULT: blocked — <why>`. Work they find but don't do goes on `FOUND: <title> — <why>` lines before the RESULT line; after the run (done or blocked) each becomes a `proposed` task (same tier and epic, `source:` the finding task), listed in the run summary. Never `ready`: you approve them like any other.
- **Verify.** An optional `verify = "<shell command>"` in `.ergon.toml` (a task's `verify:` frontmatter overrides it) runs in the task worktree after the done checks and before any merge, with a 15-minute timeout. A non-zero exit or timeout blocks the task: the last lines of output go in Notes, the full output in `<state>/runs/<ID>-<time>-verify.log`, and the worktree and branch stay. No key, no check. Keep `#` out of a task's `verify:` value (frontmatter treats ` #` as a comment).
- **Review.** Opt-in: `review = ["haiku", "sonnet"]` in `.ergon.toml` lists the worker tiers whose branches a reviewer has to pass before they land. No key means no review. After `--escalate`, a task is reviewed if either its own tier or the one that finished is listed, since the lower tier's commits are still on the branch. The reviewer is one tier above the tier that finished, capped at opus: haiku→sonnet, sonnet→opus, opus→opus. It runs after verify and before any merge: `claude -p --agent task-review` in the task worktree, read-only (the plugin's `agents/task-review.md`; a project's `.claude/agents/task-review.md` overrides it, and dispatch always sets the model). Its prompt holds the task file, the worker rules, and `git diff <base>...task/<ID>` plus each companion's diff (only the `--stat` past 100k characters).
  - Only a last line of `REVIEW: pass` lets the task land. `REVIEW: fail — <why>` blocks it, and so does anything malformed: no such last line, output that isn't JSON, an error, a timeout or kill, or a reviewer that moved the branch or left tracked changes.
  - A fail blocks the task the way verify does: the reason and the review log (`<state>/runs/<ID>-<time>-review.json`) go in Notes, the worktree and branch stay, no human task is filed, and `--escalate` doesn't retry it. To retry, move it back to `ready`. The new worker's prompt quotes the veto and points at the review log, and the reviewer runs again.
  - Each review counts toward `--budget` (per call), `--budget-total`, the run summary (`$X (reviews $Y of it)`) and the spend in `stats` and `report`, under the task's tier. It isn't counted as a run.
  - The reviewer is a second opinion, not a security boundary: it reads text the worker wrote, so a worker can try to talk it into a pass. Verify is the mechanical check.
  - The done checks, verify and review touch only the task's own worktree and branch, so they run outside the state lock. A slow one doesn't hold up other tasks landing.
- **Done** means the worker said so, left no uncommitted changes (in any tree), a commit on `task/<ID>` cites the ID, and verify and review (where set) passed. Companion branches are checked first (no tracker ID if forbidden, clean merges everywhere), then merged into each companion's local trunk, then the task branch into its base. Then the worktrees and branches are deleted (companions first), the task goes `done`, and if it was the epic's last task the epic closes. Nothing is pushed.
- **Anything else is `blocked`,** with a line in `## Notes` giving the reason, the log in `<state>/runs/`, and the `claude --resume <session>` to inspect it. The worktrees and branches stay. Fix it (or merge by hand and set it `done`), or move it back to `ready`; the next dispatch reuses the branches and tells the worker an earlier attempt exists. If a companion merge landed but a later merge failed, the note says which landed.
- **Skipped:** `human` tasks (listed at the end as yours), and tasks whose `touches:` include a nested repo, which a worktree doesn't contain. Work those interactively.
- **Overlap.** A task whose `touches:` share a path with a running one (companion names themselves don't count) is held, not started, until that one lands or blocks, so the second never conflicts with the first. `--dry-run` prints `held  T-2 behind T-1` for these, and `overlap` for a shared path with a task already `doing` elsewhere.
- Dependencies chain within one run: when a task lands, anything that was waiting on it gets picked up.
