# ergon

A task tracker for repos where AI agents do much of the work. **Humans approve, agents execute.**

- One markdown file per task and per epic. Frontmatter is the state; the body is the brief. The files are local and gitignored, and the commits that cite task IDs are the permanent record.
- Every task is sized by the cheapest model tier that can do it: `haiku`, `sonnet`, `opus` or `human`.
- Agents can propose tasks, but only a human can approve one. This is enforced by the CLI, by a lint, and by a Claude Code hook. It's a speed bump, not a security boundary.
- Each epic gets its own branch, worked in its own git worktree. When its last task closes, the branch merges back with a message listing every task and the commits that cite it.
- `ergon dispatch` runs approved tasks headless, in parallel: one worktree, one `task/<ID>` branch and one `claude -p` worker per task. Finished branches merge into their epic. Nothing is ever pushed. `ergon dispatch --watch --budget-total USD [--interval SEC]` keeps running and picks up tasks you approve meanwhile, until the cap is reached or Ctrl-C (running workers finish; a second Ctrl-C kills them and marks them blocked).
- `ergon import gh#123` turns a GitHub issue into a `proposed` task (see [Importing issues](#importing-issues)). When its epic merges, ergon prints the `gh issue comment` / `gh issue close` commands for you to run after you push. It never runs them.
- **Companion repos**: a separate repo checked out inside this one (say, a public product repo inside a private install). Each task that touches it gets its own worktree of that repo too. You can also refuse commits there that cite tracker IDs.

The guide covers the workflow, schema and rules: [`skills/ergon/SKILL.md`](skills/ergon/SKILL.md), or `ergon guide`.

The only requirements are Python 3.9+ (standard library only) and git. Dispatch also needs the `claude` CLI, and `import` needs the GitHub CLI `gh`.

## What's in the box

| Path | What |
|---|---|
| `ergon.py` | the CLI (`ergon -h`), plus the hook entry points |
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
ergon new epic "Ship the thing"
```

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

## Tests

```bash
python3 -m unittest discover -s tests
```

Each test drives the real CLI against a throwaway git repo. Dispatch is tested with a fake `claude` on `PATH`, and `import` with a fake `gh`.
