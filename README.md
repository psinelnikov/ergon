# ergon

A task tracker for repos where AI agents do much of the work. **Humans approve, agents execute.**

- One markdown file per task and per epic. Frontmatter is the state; the body is the brief. The files are local and gitignored, and the commits that cite task IDs are the permanent record.
- Every task is sized by the cheapest model tier that can do it: `haiku`, `sonnet`, `opus` or `human`.
- Agents can propose tasks, but only a human can approve one. This is enforced by the CLI, by a lint, and by a Claude Code hook. It's a speed bump, not a security boundary.
- Each epic gets its own branch, worked in its own git worktree. When its last task closes, the branch merges back with a message listing every task and the commits that cite it.
- `ergon dispatch` runs approved tasks headless, in parallel: one worktree, one `task/<ID>` branch and one `claude -p` worker per task. Finished branches merge into their epic. Nothing is ever pushed.
- **Companion repos**: a separate repo checked out inside this one (say, a public product repo inside a private install). Each task that touches it gets its own worktree of that repo too. You can also refuse commits there that cite tracker IDs.

The guide covers the workflow, schema and rules: [`skills/ergon/SKILL.md`](skills/ergon/SKILL.md), or `ergon guide`.

The only requirements are Python 3.9+ (standard library only) and git. Dispatch also needs the `claude` CLI.

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

[[companion]]                      # repeatable
name = "org/product"               # how tasks name it in touches:
path = ".product/src"              # its checkout
trunk = "main"
touches = ["org/product", ".product/src"]  # default: [name, path]
forbid_tracker_ids = true
policy = "docs/public-boundary.md" # cited to workers as the reason
```

To override a worker agent for one project, put your own `.claude/agents/task-<tier>.md` in the main checkout. Dispatch uses it instead of the plugin's.

## Tests

```bash
python3 -m unittest discover -s tests
```

Each test drives the real CLI against a throwaway git repo. Dispatch is tested with a fake `claude` on `PATH`.
