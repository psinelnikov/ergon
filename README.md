# ergon

A task tracker for repos where AI agents do much of the work. **Humans approve, agents execute.**

- One markdown file per task and per epic. Frontmatter is the state; the body is the brief. The files are local and gitignored by default (a team can [share them](#shared-task-state-for-teams)), and the commits that cite task IDs are the permanent record.
- Every task is sized by the cheapest model tier that can do it: `haiku`, `sonnet`, `opus` or `human`.
- Agents can propose tasks, but only a human can approve one. This is enforced by the CLI, by a lint, by a Claude Code hook and, if you opt in, by a git hook that covers every other agent too (see [Agents other than Claude Code](#agents-other-than-claude-code)). It's a speed bump, not a security boundary.
- One exception: a follow-up that a dispatched worker reports on a `FOUND:` line, and that isn't a feature, is approved by inheritance from the task that found it and filed `ready`. All five conditions must hold when it is filed: its category is `bugfix`, `refactor` or `chore` (empty counts as feature); its tier isn't `human`; it declares `touches:` and none of them falls under a `[[protect]]` rule; it is at most `follow_up_depth` follow-ups (default 3, `0` turns this off) from a human-approved task; and state is local (in shared mode follow-ups stay `proposed`, since approvals are signed). The run that found it must also have finished, past its reviewer if one is set. Its stamp names it: `approved: auto bugfix follow-up of T-095 (root T-095, Pavel Sinelnikov 2026-11-17)`. `ergon check` fails one whose stamp no longer holds, and the guard refuses an agent's change to its `category:` or `touches:`. Features always wait for you.
- A task is one change or one human action that can land on its own. An epic is a goal that takes several tasks that should land together (see [Epic or task?](skills/ergon/SKILL.md#epic-or-task)).
- Each epic gets its own branch, worked in its own git worktree. When its last task closes, the branch merges back with a message listing every task and the commits that cite it.
- `ergon dispatch` runs approved tasks headless, in parallel: one worktree, one `task/<ID>` branch and one `claude -p` worker per task. Finished branches merge into their epic. A run also picks up tasks you approve while workers run (rescanning every `--interval` seconds) and runs them in parallel when they share no `touches:` path with a running task; an overlapping one waits for its blocker. No code is ever pushed; the only thing ergon pushes is the task state branch, and only in opt-in [shared mode](#shared-task-state-for-teams). `ergon dispatch --watch --budget-total USD [--interval SEC]` keeps running and picks up tasks you approve meanwhile, until the cap is reached or Ctrl-C (running workers finish; a second Ctrl-C kills them and marks them blocked). With `review` set, a reviewer one tier up must pass each branch before it lands; on a fail, dispatch hands the findings back to the worker (resuming its session while the prompt cache is warm, else a fresh one) and reviews again, up to `review_rounds` times (default 2), before it blocks the task with every round's reason in Notes. A branch that conflicts with its base, because the base moved meanwhile, goes back to its worker the same way to merge the base in, up to `conflict_rounds` times (default 1), before it blocks for a hand merge. A worker that needs a decision ends with `QUESTION:` lines (and optional `OPTIONS:`) and `RESULT: asking`. Dispatch keeps its branch and worktree and parks the task `blocked` under AWAITING ANSWER. You reply with `ergon answer <ID>` (human only, like `approve`), and the next dispatch resumes that worker with your answer. A bugfix, refactor or chore follow-up a worker files is approved by inheritance and worked in the same run (not counted in `--max`, but in `--budget-total`; `follow_ups`, default 10, bounds them per run); the board, `/t/T-NNN`, `ergon show` and `ergon report` show a done task as `done, N follow-ups open` until they close.
- `ergon import gh#123` turns a GitHub issue into a `proposed` task (see [Importing issues](#importing-issues)). When its epic merges, ergon prints the `gh issue comment` / `gh issue close` commands for you to run after you push. It never runs them.
- `ergon journal [E-NNN ...] [--all] [--model sonnet] [--budget USD]` has claude write a short journal entry per epic: what it set out to do, what landed and how the work went (fix rounds, vetoes, red verifies, questions, blocks, time waiting on a human, cost). Entries live in `<state>/runs/journal/` and show on the epic's page. The `journal` key in `.ergon.toml` (default `true`) also writes one whenever an epic finishes (see [Web board](#web-board-ergon-serve)).
- `ergon serve` is a read-only web board for a browser or a phone: open work, what waits on you, live dispatch state, and each task's runs, reviews, costs and logs (see [Web board](#web-board-ergon-serve)). Loopback only unless you `--bind` elsewhere.
- **Concept map** (optional): `concepts = "concepts.toml"` in `.ergon.toml` names a map of the project's concerns and components. `ergon tag` has claude tag each task's `concepts:` with component ids from it (nothing else is accepted, and tagging never un-approves a task). `ergon related T-NNN` and the task page then list the open tasks that share a component or a concern, the done work a task builds on, and depends it seems to be missing, which `ergon check` warns about (see [Concept map](skills/ergon/SKILL.md#concept-map-optional)).
- **Companion repos**: a separate repo checked out inside this one (say, a public product repo inside a private install). Each task that touches it gets its own worktree of that repo too. You can also refuse commits there that cite tracker IDs.

**Priority.** Open work is ordered by `(cod + 25 × unblocks) / size`: the cost of delay (`cod:`, 0-100, set by `ergon grade`), plus 25 for each open task it unblocks, divided by the tier's size (haiku 1, sonnet 3, opus 8, human 3). `next`, `dispatch` and the board all use that order. A human can pin a task to the front or back with `ergon pin`, which overrides the score. Grading never un-approves a task.

Commands for the ordering:

```bash
ergon grade [--all] [--epic E] [--per 4] [--model sonnet] [--budget USD] [--dry-run]
                        # one claude -p compares tasks in pairs and writes cod:, cod_why: and graded:.
                        # Only stale tasks (new, or brief changed) unless --all
ergon fill [ID ...] [--epic E] [--model sonnet] [--dry-run]
                        # claude (read-only tools, in this checkout) fills the blank What, Done when,
                        # touches: and tier of proposed briefs; never replaces what's written
ergon pin ID first|last|clear   # HUMAN ONLY: pin a task first or last in the order, or clear the pin
```

The guide covers the workflow, schema and rules: [`skills/ergon/SKILL.md`](skills/ergon/SKILL.md), or `ergon guide`.

The only requirements are Python 3.9+ (standard library only) and git. Dispatch also needs the `claude` CLI, and `import` needs the GitHub CLI `gh`.

## What's in the box

| Path | What |
|---|---|
| `ergon/` | the CLI package (`ergon -h`; `cli.py` is the entry point), plus the hook entry points |
| `hooks/hooks.json` | PreToolUse guard and Stop-time `check`. Both do nothing in a repo without `.ergon.toml` |
| `agents/task-{haiku,sonnet,opus}.md` | worker agents: one task, commit citing its ID, end with `RESULT: done/blocked/asking` |
| `skills/ergon/` | the workflow guide, as a skill |
| `skills/task-audit/` | finds stale tasks and hands the human the close/drop commands |

## Install

```bash
# 1. The plugin: hooks, agents, skills
claude plugin marketplace add ~/code/ergon        # or the git URL
claude plugin install ergon@ergon                 # user scope; hooks stay silent in repos without .ergon.toml

# 2. The CLI on your PATH (humans and agents both call `ergon`)
uv tool install -e ~/code/ergon                   # or: pipx install -e ~/code/ergon
```

Then, in a project:

```bash
ergon init            # writes .ergon.toml, tasks/items, tasks/epics and the .gitignore lines
ergon init --hooks    # optional, any time: also the git commit-msg hook (next section)
ergon new epic "Ship the thing"
```

## Agents other than Claude Code

The PreToolUse guard (`ergon hook guard`) only runs inside Claude Code. Codex, Cursor and other agents that read `AGENTS.md` meet only the CLI's own checks. `ergon init --hooks` adds a git `commit-msg` hook. From then on, git refuses any commit whose message cites a task (`T-NNN`) that has no human `approved:` stamp: a `proposed` task, one rejected to `dropped`, one hand-edited to `ready`, an ID nobody filed, or an approved task whose epic's brief isn't approved (`proposed`, dropped, or a stamp that doesn't verify). Commits citing approved tasks of approved epics, tasks without an epic, epic IDs or nothing at all go through as before.

- **Opt-in, per clone.** Without `--hooks` nothing changes. The hook lives in `.git/hooks/`, so every worktree of that clone shares it, and it isn't committed. It does nothing in a checkout without `.ergon.toml`. To turn it off, delete `.git/hooks/commit-msg`. Ergon never overwrites a `commit-msg` hook it didn't write, and never installs one when `core.hooksPath` is set (husky and the like). It prints the line to add to that hook instead: `ergon hook commit-msg "$1" || exit 1`.
- **Merges aren't checked.** A merge only brings in commits that were checked when they were made, and an epic's merge message lists its dropped tasks too.
- **A speed bump.** `git commit --no-verify` skips it, as does deleting the hook. The hook fails open if ergon crashes. It catches an agent that drifts into working an unapproved task and cites it the way the workflow asks. It doesn't stop one that is set on getting around it, and it doesn't catch work that cites no task at all.

Why a git hook and not an MCP server exposing `next`/`show`/`set`/`new` without `approve`:

- **An MCP server only binds agents that choose to use it.** These agents still have a shell, so the CLI would be just as reachable as before. Every agent's work lands through git, so a git hook is on the one path all of it takes, whichever tool made it.
- **It costs less.** A small shell script plus under a hundred lines of the ergon package, still standard library only. An MCP server is a long-running process with a protocol to keep up with, and every agent tool needs its own setup for it.
- **It checks the outcome, not the request.** The hook doesn't care how a task got to look approved, only whether the commit cites one that is. Its weakness is the same: it only sees commits.

It deliberately doesn't run the full `ergon check`. A problem in some unrelated task file would then block every commit, humans' included. The Claude Code Stop hook keeps running `check` as before.

## Configure: `.ergon.toml`

This file lives in the main checkout, and every path in it is relative to that checkout. Every key is optional:

```toml
trunk = "main"                     # epics branch from and merge back into this
state = "tasks"                    # <state>/items, <state>/epics, <state>/runs (dispatch logs)
worktrees = ".claude/worktrees"    # <worktrees>/E-NNN per epic, <worktrees>/T-NNN per dispatched task
map = "map"                        # cards whose "→ task T-NNN" links `check` keeps honest
log = "TODO.md"                    # the log that `source: TODO.md:<line>` cites (audit checks the line)
retired = "tasks/audit-retired.txt"  # `term | gate | note` rows: nouns that make a task stale
worker_rules = "tasks/WORKER.md"   # inlined into every dispatched prompt; `ergon rules` prints it
verify = "make test"               # run in the task worktree before landing; non-zero exit blocks (a task's `verify:` field overrides)
verify_rounds = 2                   # a red verify goes back to the worker this many times first; 0 blocks at once
review = ["haiku", "sonnet"]       # worker tiers whose branches a reviewer one tier up must pass before landing
policies = "tasks/policies.md"      # settled review findings, human-written in the interactive review (p); inlined into worker and reviewer prompts
review_rounds = 2                  # a review fail goes back to the worker, then review again, this many times; then the task blocks (0: at once)
conflict_rounds = 1                # a branch that conflicts with its base goes back to its worker this many times; then it blocks for a hand merge (0: at once)
                                   #   the worker merges the base in its worktree, resolves only the conflicted hunks so both sides' intent
                                   #   survives, and commits the merge; dispatch checks the tip is a merge commit with the base's tip as a
                                   #   parent, no <<<<<<< / >>>>>>> lines are left, the worktree is clean and the merge is now clean; then
                                   #   the done checks, verify and review run again
review_resume_secs = 3300          # a fix round resumes the worker's session only if its last request is younger than this
review_resume_tokens = 80000       # and its context smaller than this; otherwise a fresh worker on the same branch
limit_probe = true                 # read the plan's 5-hour window around each dispatch to show each task's share of it
journal = true                     # when an epic finishes, claude writes its journal entry (shown on /e/E-NNN); false: only `ergon journal` does

[[companion]]                      # repeatable
name = "org/product"               # how tasks name it in touches:
path = ".product/src"              # its checkout
trunk = "main"
touches = ["org/product", ".product/src"]  # default: [name, path]
forbid_tracker_ids = true
policy = "docs/public-boundary.md" # cited to workers as the reason

[[accept]]                         # repeatable; matching tasks stop at `review` until a human runs `ergon accept`
tiers = ["opus"]                   # and/or touches = ["server/**"]; a task's own `accept: yes` also opts in

[[protect]]                        # repeatable; `check` fails an open task below min_tier that touches these
touches = ["server/auth/**", "prod-db"]  # fnmatch patterns against a task's touches:
min_tier = "opus"                  # or "human"
note = "signing keys live here"    # optional, shown in the error
```

To override a worker agent for one project, put your own `.claude/agents/task-<tier>.md` in the main checkout. Dispatch uses it instead of the plugin's.

## Shared task state for teams

By default task state is local: each person has their own queue and their own `T-001`. A team can opt in to one shared queue. Add `[shared]` to `.ergon.toml`:

```toml
[shared]
remote = "origin"            # a remote name or URL
branch = "ergon-state"       # orphan branch holding items/ and epics/
# approvers = ".ergon/approvers"
```

`<state>/` then becomes a git worktree of that branch, so every command still reads and writes the same files. Every command that changes state makes a local commit there. `ergon sync` fetches, merges and pushes. **No code is ever pushed; the state branch is the only thing ergon pushes.** The design and its reasons are in [docs/shared-state.md](docs/shared-state.md).

**Setup.**

1. One person runs `ergon sync --init`. It warns if the remote is the trunk's own remote or can be read without logging in, shows how many files it will publish and asks before pushing. Then commit `[shared]` to the trunk.
2. Everyone else pulls and runs `ergon sync --join`. It needs an `<state>/` with no task files (it refuses otherwise: move them out, join, and file them again with `ergon new`).
3. Each approver sets up SSH commit signing in their clone (git 2.34+): `git config gpg.format ssh` and `git config user.signingkey ~/.ssh/id_ed25519.pub`. Use a key that needs a touch or a confirmation (a hardware key, `ssh-add -c`, an agent that prompts). A key sitting unlocked in an `ssh-agent` lets an agent in your shell sign for you, and then the signature proves nothing.
4. Add `.ergon/approvers` to the trunk: a git `allowed_signers` file, one line per approver (`you@example.com namespaces="git" ssh-ed25519 AAAA…`). Ergon reads it from the remote's trunk, never from your working tree, so a change to it has to be merged upstream first. Protect it with a rule so no task below `human` can touch it:

   ```toml
   [[protect]]
   touches = [".ergon/approvers"]
   min_tier = "human"
   ```

5. Keep the policies file on the trunk. In local mode it defaults to `<state>/policies.md`, which is yours alone. In shared mode only `items/` and `epics/` travel on the state branch, and policies are inlined into every worker's and reviewer's prompt, so they don't go on the unsigned state branch. Set `policies = ".ergon/policies.md"` (or any trunk path) and commit the file through normal code review. `ergon sync --init` and `--join` print a hint while it is unset or points into `<state>/`.

**Privacy.** Syncing publishes every brief and every Note. If the code repo is public, an `ergon-state` branch on the same remote is public too. Set `remote` to the URL of a separate private repo.

**Who syncs.** Only humans and the dispatcher. `ergon sync` refuses inside an agent shell and on a `task/` branch, and the guard blocks git writes in the state worktree. An agent's `new` or `set` is committed locally and goes out at your next sync. `approve`, `accept`, `reject` and `close` sync before and after. Run logs (`runs/`) stay local, so `stats` and `report` are per machine.

`ergon stats` also prints an `approver signals` section (from the event history, falling back to the task file's `approved:` stamp): for the tasks an approver approved, how many landed, escalated, blocked (not `outside:` blocks, Ctrl-C or budget stops), asked or came back from review, fix rounds per landed task and the tier mix, with a per-week trend (`(n<3)` marks thin weeks). By default it shows only your own record (`--approver NAME` for another, `--approvers` for all); these are signals, not verdicts.

**IDs.** A new task gets the next ID over local and last-fetched remote state. If two people filed the same ID offline, sync renumbers the one that was never published (file name, `id:`, `depends:`, `source:`) and prints each change, plus any map card or log line that names the old ID. `approve` and `start` sync an unpublished task first and refuse if its ID changed, so an ID that a commit cites never changes. An approved task whose ID collides is reported and left for you to resolve.

**Concurrent edits.** Merging is per task and per field. `status` is compare-and-set: if the remote already moved the task, the remote wins and your move is dropped with a Note. `## Notes` merge as a union of lines. Other fields and the brief merge three-way; on a real conflict the remote wins and your version is saved to `<state>/conflicts/`. Every loss is printed. Dispatch claims a task by pushing `ready → doing` before it launches a worker, so two dispatchers never run the same one. It also claims an epic's breakdown by pushing a `breakdown:` field on the epic before the agent launches, so two dispatchers never break down the same epic. A task held at `review` has its `task/<ID>` branch in one clone only, and the Note says which (`held: user@host`); only that clone can `accept` it.

**Signed approvals.** In shared mode `approve` makes an SSH-signed commit with an `Ergon-Approve: T-NNN sha256:…` trailer, and the digest covers the brief. A task counts as approved only if a commit signed by a key in `.ergon/approvers` approves its current digest. Editing an approved brief means approving it again. `dispatch`, `next` and `check` verify this; the `approved:` field is a display copy that `sync` rewrites. This stops a teammate's agent or a compromised clone from pushing `status: ready` with a made-up stamp. It doesn't stop an agent that can use your signing key, which is why the key should need a touch.

**Two-person rule.** `[[two_person]]` rules (`tiers` and/or `touches`, matched like `[[accept]]`) need signed approvals of the current brief from two different people in the approvers file before a task leaves `proposed`. One person with two keys counts once. The first approval adds a Note (`approved by Pavel (1 of 2)`) and the task stays `proposed`; `ergon review` shows who is still needed. It needs `[shared]`.

Epic branches are code and still move through your normal git flow, so `set`'s check that an epic's status changes only from its branch can only see local branches.

## Working with Obsidian

`ergon obsidian [--dry-run]` turns tagged checkbox lines in an Obsidian vault into `proposed` tasks. It reads the vault and never writes to it. It needs no plugin and no Obsidian running.

```toml
[obsidian]
vault = "~/Notes"        # a path, ~ expanded, may be outside the repo; it must exist
tag = "agent"            # default
exclude = ["Archive"]    # fnmatch patterns of vault folders to skip; dot-folders (.obsidian, .trash) are always skipped
write_back = false       # default; acted on from T-156. With true, lines are still imported with hash keys, and a warning says so
```

Without the table, `ergon obsidian` says it isn't configured and does nothing.

**The line format** is the Tasks plugin's: an unticked checkbox (`- [ ]` or `* [ ]`, any indent) carrying the tag as a whole tag, such as `- [ ] Compare the health plans #agent 📅 2026-12-01`. Ticked lines, untagged lines and `#agents` don't match. Each new line becomes a task with `tier: human` and `source: obsidian:<note path>#<key>`. The title is the line without the checkbox, the tag, a trailing `^block-id` and the plugin's emoji fields (📅 ⏳ 🛫 ➕ ✅ 🔁 ⏫ 🔼 🔽 and their values), cut to 70 characters. What quotes the line and names the note, and the 📅 and ⏳ dates go into Notes as `due:` and `scheduled:` lines. Vault text is untrusted, like an issue body: a task is never approved by import, and you rewrite it as a brief before approving. The command prints how many lines it found, filed and skipped as already imported. `--dry-run` prints what it would file and files nothing.

**The key** is `sha256(note path + line text)[:10]`. Editing an imported line's text (or moving it to another note) changes the key, so the link to its task is lost while `write_back` is off, and the edited line imports again as a new task. Edit the task in ergon instead, or leave the line alone.

**A view of the tasks in Obsidian.** Symlink `tasks/` into the vault (`ln -s /path/to/repo/tasks ~/Notes/ergon-tasks`) and query it with a Bases view: task files keep their state in YAML frontmatter, including block-style lists. `ergon obsidian` doesn't follow symlinked folders, so it never scans the linked task files.

## Importing issues

```bash
ergon import gh#123 [--epic E-001] [--tier sonnet] [--touches a,b]   # this checkout's GitHub repo
ergon import gh:acme/app#123                                          # another repo
```

This creates a `proposed` task. Its title is the issue's title, its What quotes the issue body (cut at 4,000 characters) and its `source:` is `gh#123`. Importing the same issue twice is refused. Then, as with any proposed task, you rewrite the brief, size it and approve it. When the task's epic merges, `finish` prints one `gh issue comment` and one `gh issue close` per issue its done tasks cite. You run them after you push.

The design choices:

- **It shells out to `gh` and doesn't call the API with `urllib`.** Both would keep ergon standard-library only. `gh` already holds your login, so ergon never reads or stores a token. It also knows GitHub Enterprise hosts and which repo this checkout's remote points at. Like `git` and `claude`, it's a tool on `PATH`, not a Python dependency. Without it, `import` exits with a pointer to `gh auth login`.
- **GitHub only for now.** Linear has no official CLI, so supporting it would mean handling an API key inside ergon. `linear:ABC-12` is refused with a hint to use `ergon new task … --source linear:ABC-12`.
- **Every task has a category:** `feature` (new behaviour a user would notice), `bugfix` (existing behaviour is wrong, security holes included), `refactor` (same behaviour, better code) or `chore` (docs, help text, tests, config, tooling). Set it with `ergon new --category`, `ergon import --category` or `ergon set T-NNN category=…`; a worker's FOUND line can add `CATEGORY: …`. Empty or unknown reads as `feature`. It isn't part of the approved brief, so changing it never un-approves a task. `ergon fill` writes a blank one on any task, `ergon show`, the board, `/review` and the task page show it, and `ergon stats` counts done tasks per category.
- **The tier is not guessed.** An imported task gets `tier: human` unless you pass `--tier`, and `tier_why` says it's unsized. An issue body is text from outside the repo that anyone can write. Dispatch skips `human` tasks, so even approving a whole epic at once can't send an unread issue to a headless worker. To make it worker-ready, give it a tier with `ergon set T-NNN tier=…` or `review -i`.
- **Nothing is closed or commented on automatically.** Ergon never pushes code, so when an epic merges the fix isn't public yet. A closed issue pointing at a commit nobody can see would be wrong. Printing the commands keeps that step with whoever pushes.

## Web board: `ergon serve`

```bash
ergon serve                          # http://127.0.0.1:4300/
ergon serve --port 8080
ergon serve --bind 100.101.102.103   # your tailscale IP, to read it from your phone
```

A small read-only web view of the same state, served by the standard library's `http.server` with its CSS and JS inlined in each page. It loads nothing from elsewhere. It follows the system's light or dark setting and works at phone width.

- **The board** (`/`) shows open work by status, then tier, the way `ergon` prints it. Above it are three counts: tasks awaiting approval (`proposed`), awaiting acceptance (`review`), and awaiting an answer (open `human` tasks that a blocked worker filed). Below are the epics with their progress. A live table lists every `doing` task with its elapsed time, round, spend so far (the finished workers and reviewers of this dispatch, from the history) and the tokens of its newest transcript. The page polls `/live.json` every four seconds to keep that table current.
- **The review page** (`/review`, linked from the board's PROPOSED heading) is the thing to read before `ergon approve`. It groups the proposed tasks by dependency (linked tasks together in dependency order, each group with its own command to copy, the rest under "Independent" by tier) and shows how many have every dependency done; a dependency map (inline SVG) where each box links to its task's card; one card per task with its tier, `tier_why`, depends, touches, What and Done when; and findings worked out from the files at request time: `file:line` citations and path-like `touches:` entries missing from the main checkout (or a line past the file's end), an empty Done when, `tier_why` or `touches:`, and dependencies on missing or dropped tasks. At the bottom is the `ergon approve …` command to copy. The page itself approves nothing.
- **The quiz** on `/review` is multiple choice: one try per question, then the explanation and a link to the task. It's scored in the page and nothing is sent back. Since `serve` never launches claude, the questions come from `ergon quiz`. That runs one `claude -p` (sonnet by default, no tools, from an empty folder) over the proposed briefs and writes `<state>/runs/quiz/quiz-<time>.json`. Each question stores a hash of its task's brief (title, tier, `tier_why`, depends, touches, body). The page reads the newest file and hides a question once its task has been edited or is no longer `proposed`. Running `ergon quiz` again keeps the questions that are still current and only asks about tasks that have none (`--all` asks about every task again; `--per N` sets questions per task, `--model`, `--budget USD`). With no quiz file, the section tells you to run it.
- **An epic** (`/e/E-NNN`) shows its journal entry above the replay timeline, with a muted line giving when it was generated, the model and the cost. Since `serve` never launches claude, entries come from `ergon journal [E-NNN ...] [--all] [--model sonnet] [--budget USD]`: one `claude -p` per epic (no tools, from an empty folder) over its EPIC.md (Goal, Out of scope, Constraints), its tasks, its history rows and the commits that cite its tasks. Each lands in `<state>/runs/journal/E-NNN-<time>.json` as `{generated, model, cost, epic, body}`; older ones are kept and the newest wins. With no IDs it writes the epics that have no entry yet or have history rows newer than their latest entry; `--all` rewrites every epic, and named IDs always rewrite. When an epic finishes (`ergon finish`, or its last task landing through `set`, `accept`, `close` or dispatch) its entry is written too, after the state lock is released; set `journal = false` in `.ergon.toml` to turn that off. A failing or missing `claude` prints one warning and the epic still merges. With no entry the page says `no journal entry yet: ergon journal E-NNN`. The entry's text is escaped like task text.
- **A task** (`/t/T-NNN`) shows its frontmatter and the brief, rendered. Each worker run from `history.jsonl` gets a card with its round, tier, verdict, cost, time, reason, log, tokens, top tools and permission denials (from Claude Code's transcript), plus the `cd <worktree> && claude --resume <session>` command to pick the session up. Below that come the review verdicts by round, the full journal, and links to every raw log of the task. Run logs from before the history existed still show, read from the log file itself.

It is read-only by construction:

- Only GET and HEAD are served. Every other method gets `405`. No route writes a file (transcript summaries are cached in memory, not in `runs/transcripts.json`), runs git, or launches `claude`.
- Only files directly inside `<state>/runs/` are served, by bare name. A name with a slash, a leading dot, `..` (encoded or not), or a symlink that resolves elsewhere gets `404`. Logs are sent as `text/plain` with `nosniff`, so a log full of HTML is still shown as text.
- Task text is untrusted. Every value is HTML-escaped. The brief's markdown is a small subset (headings, lists, code, bold, task links) applied after escaping, so no raw HTML, attribute or link from a task gets through. Pages carry a Content-Security-Policy that allows only that response's own inline script and style, by nonce.
- The `Host` header must be an IP address or `localhost`. That stops a DNS-rebinding web page from reading the board through your browser. It also means you open the board by IP, not by a MagicDNS or other host name.

**The bind warning.** By default it listens on `127.0.0.1` only. Any other address has to be given with `--bind`, and ergon then prints a warning: **there is no login.** Anyone who can reach that address and port can read every task brief, run log, cost and session ID. Bind a tailnet IP or another interface only you can reach. Never bind `0.0.0.0` on a network you share, and never expose the port to the internet.

## Tests

```bash
python3 -m unittest discover -s tests
```

Each test drives the real CLI against a throwaway git repo. Dispatch is tested with a fake `claude` on `PATH`, and `import` with a fake `gh`.
