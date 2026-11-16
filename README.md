# ergon

A task tracker for repos where AI agents do much of the work. **Humans approve, agents execute.**

- One markdown file per task and per epic. Frontmatter is the state; the body is the brief. The files are local and gitignored, and the commits that cite task IDs are the permanent record.
- Every task is sized by the cheapest model tier that can do it: `haiku`, `sonnet`, `opus` or `human`.
- Agents can propose tasks, but only a human can approve one. This is enforced by the CLI, by a lint, by a Claude Code hook and, if you opt in, by a git hook that covers every other agent too (see [Agents other than Claude Code](#agents-other-than-claude-code)). It's a speed bump, not a security boundary.
- Each epic gets its own branch, worked in its own git worktree. When its last task closes, the branch merges back with a message listing every task and the commits that cite it.
- `ergon dispatch` runs approved tasks headless, in parallel: one worktree, one `task/<ID>` branch and one `claude -p` worker per task. Finished branches merge into their epic. Nothing is ever pushed. `ergon dispatch --watch --budget-total USD [--interval SEC]` keeps running and picks up tasks you approve meanwhile, until the cap is reached or Ctrl-C (running workers finish; a second Ctrl-C kills them and marks them blocked). With `review` set, a reviewer one tier up must pass each branch before it lands; on a fail, dispatch hands the findings back to the worker (resuming its session while the prompt cache is warm, else a fresh one) and reviews again, up to `review_rounds` times (default 2), before it blocks the task with every round's reason in Notes.
- `ergon import gh#123` turns a GitHub issue into a `proposed` task (see [Importing issues](#importing-issues)). When its epic merges, ergon prints the `gh issue comment` / `gh issue close` commands for you to run after you push. It never runs them.
- `ergon serve` is a read-only web board for a browser or a phone: open work, what waits on you, live dispatch state, and each task's runs, reviews, costs and logs (see [Web board](#web-board-ergon-serve)). Loopback only unless you `--bind` elsewhere.
- **Companion repos**: a separate repo checked out inside this one (say, a public product repo inside a private install). Each task that touches it gets its own worktree of that repo too. You can also refuse commits there that cite tracker IDs.

The guide covers the workflow, schema and rules: [`skills/ergon/SKILL.md`](skills/ergon/SKILL.md), or `ergon guide`.

The only requirements are Python 3.9+ (standard library only) and git. Dispatch also needs the `claude` CLI, and `import` needs the GitHub CLI `gh`.

## What's in the box

| Path | What |
|---|---|
| `ergon/` | the CLI package (`ergon -h`; `cli.py` is the entry point), plus the hook entry points |
| `hooks/hooks.json` | PreToolUse guard and Stop-time `check`. Both do nothing in a repo without `.ergon.toml` |
| `agents/task-{haiku,sonnet,opus}.md` | worker agents: one task, commit citing its ID, end with `RESULT: done/blocked` |
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

The PreToolUse guard (`ergon hook guard`) only runs inside Claude Code. Codex, Cursor and other agents that read `AGENTS.md` meet only the CLI's own checks. `ergon init --hooks` adds a git `commit-msg` hook. From then on, git refuses any commit whose message cites a task (`T-NNN`) that has no human `approved:` stamp: a `proposed` task, one rejected to `dropped`, one hand-edited to `ready`, or an ID nobody filed. Commits citing approved tasks, epic IDs or nothing at all go through as before.

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
review_resume_secs = 3300          # a fix round resumes the worker's session only if its last request is younger than this
review_resume_tokens = 80000       # and its context smaller than this; otherwise a fresh worker on the same branch

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

## Importing issues

```bash
ergon import gh#123 [--epic E-001] [--tier sonnet] [--touches a,b]   # this checkout's GitHub repo
ergon import gh:acme/app#123                                          # another repo
```

This creates a `proposed` task. Its title is the issue's title, its What quotes the issue body (cut at 4,000 characters) and its `source:` is `gh#123`. Importing the same issue twice is refused. Then, as with any proposed task, you rewrite the brief, size it and approve it. When the task's epic merges, `finish` prints one `gh issue comment` and one `gh issue close` per issue its done tasks cite. You run them after you push.

The design choices:

- **It shells out to `gh` and doesn't call the API with `urllib`.** Both would keep ergon standard-library only. `gh` already holds your login, so ergon never reads or stores a token. It also knows GitHub Enterprise hosts and which repo this checkout's remote points at. Like `git` and `claude`, it's a tool on `PATH`, not a Python dependency. Without it, `import` exits with a pointer to `gh auth login`.
- **GitHub only for now.** Linear has no official CLI, so supporting it would mean handling an API key inside ergon. `linear:ABC-12` is refused with a hint to use `ergon new task … --source linear:ABC-12`.
- **The tier is not guessed.** An imported task gets `tier: human` unless you pass `--tier`, and `tier_why` says it's unsized. An issue body is text from outside the repo that anyone can write. Dispatch skips `human` tasks, so even approving a whole epic at once can't send an unread issue to a headless worker. To make it worker-ready, give it a tier with `ergon set T-NNN tier=…` or `review -i`.
- **Nothing is closed or commented on automatically.** Ergon never pushes, so when an epic merges the fix isn't public yet. A closed issue pointing at a commit nobody can see would be wrong. Printing the commands keeps that step with whoever pushes.

## Web board: `ergon serve`

```bash
ergon serve                          # http://127.0.0.1:4300/
ergon serve --port 8080
ergon serve --bind 100.101.102.103   # your tailscale IP, to read it from your phone
```

A small read-only web view of the same state, served by the standard library's `http.server` with its CSS and JS inlined in each page. It loads nothing from elsewhere. It follows the system's light or dark setting and works at phone width.

- **The board** (`/`) shows open work by status, then tier, the way `ergon` prints it. Above it are three counts: tasks awaiting approval (`proposed`), awaiting acceptance (`review`), and awaiting an answer (open `human` tasks that a blocked worker filed). Below are the epics with their progress. A live table lists every `doing` task with its elapsed time, round, spend so far (the finished workers and reviewers of this dispatch, from the history) and the tokens of its newest transcript. The page polls `/live.json` every four seconds to keep that table current.
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
