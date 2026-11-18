"""Paths, settings and .ergon.toml parsing."""
import argparse
import datetime
import fcntl
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

from . import tasks as _tasks


PKG = Path(__file__).resolve().parent  # the ergon/ package folder
HERE = PKG.parent  # this ergon checkout: the plugin root
CONFIG_FILE = ".ergon.toml"
# In a checkout the plugin files sit at HERE; an installed copy bundles them in ergon/_plugin.
PLUGIN = HERE if (HERE / "skills" / "ergon" / "SKILL.md").exists() else PKG / "_plugin"
GUIDE = PLUGIN / "skills" / "ergon" / "SKILL.md"
PLUGIN_AGENTS = PLUGIN / "agents"
EPIC_FILE = "EPIC.md"
TASK_BRANCH = "task/"
WORKER_TIERS = ["haiku", "sonnet", "opus"]
TRACKER_ID = re.compile(r"\b[TE]-\d{3,}\b")

CONFIG_TEMPLATE = """\
# ergon: https://github.com/<you>/ergon. Paths are relative to this file (the main checkout).
trunk = "main"                  # epics branch from and merge back into this
state = "tasks"                 # task files: <state>/items, <state>/epics, dispatch logs <state>/runs
worktrees = ".claude/worktrees" # one per started epic and per dispatched task
# map = "map"                   # cards whose "-> task T-NNN" links `check` keeps honest
# log = "TODO.md"               # the log a task's `source: TODO.md:<line>` cites
# retired = "tasks/audit-retired.txt"   # `term | gate | note` rows for `audit`
# worker_rules = "tasks/WORKER.md"      # inlined into every dispatched worker's prompt
# concepts = "concepts.toml"    # concept map: [[concern]]s and [[component]]s that `ergon tag` tags tasks
#                               # with and `ergon related` links them by (skills/ergon/SKILL.md)
# policies = "tasks/policies.md"        # settled review findings (human-written; inlined into worker and reviewer prompts)
# verify = "make test"          # run in the task worktree before landing; non-zero exit blocks the task
#                               # (a task's `verify:` frontmatter overrides it)
# verify_rounds = 2             # on a red verify, resume the worker with the output and verify again, up to
#                               # this many times; then block (0: block at once). A worker that ends
#                               # `RESULT: blocked — outside: <why>` costs no round: verify reruns once as is
# review = ["haiku", "sonnet"]  # worker tiers whose branches a reviewer one tier up must pass before
#                               # landing (opus reviews opus)
# review_rounds = 2             # on a review fail, hand the findings back to the worker and review
#                               # again, up to this many times; then block (0: block on the first fail)
# conflict_rounds = 1           # a branch that conflicts with its base goes back to its worker to merge the
#                               # base in, this many times; then block for a hand merge (0: block at once)
# review_resume_secs = 3300     # resume the worker's own session only if its last request is younger
# review_resume_tokens = 80000  # and its context smaller than these; else a fresh worker on the branch
# journal = true                # when an epic finishes, have claude write its journal entry (one `claude -p`
#                               # call, shown on /e/E-NNN); false: only `ergon journal` writes them
# learn = true                  # after each dispatch run (and each --watch scan) with a newly caught incident,
#                               # run `ergon learn`: one `claude -p` proposes harden tasks (under --budget-total)
# limit_probe = true            # read the plan's 5-hour window before and after each dispatch (two
#                               # tiny haiku calls) to show each task's cost as a % of the limit
# follow_up_depth = 3           # a worker's FOUND follow-up that is a bugfix, refactor or chore, not
#                               # tier human, with touches: none under [[protect]], goes straight to
#                               # ready, approved by inheritance from the task that found it, up to
#                               # this many follow-ups from a human-approved task (0: off; never in
#                               # shared mode, where approvals are signed). A feature waits for you
# follow_ups = 10               # how many such auto-approved follow-ups one dispatch run works itself (they
#                               # don't count toward --max, they do count toward --budget-total); past it
#                               # they stay ready for the next run

# Layered gates, cheapest first: static (lint, types, import boundaries), then runtime (tests); the
# first red stops the rest. `verify` above is an alias for runtime; a task's `verify:` overrides runtime only.
# Before both, the weakening check: a branch whose diff changes a gate file (protect, fnmatch; a
# pattern with no `/` matches that name in any directory; .ergon.toml always) goes back to its worker
# unless the task's touches: names that file, and so does one that adds a line matching a suppression
# regex (suppress) to any file, named or not, unless the task is a harden task (harden: set) whose
# touches: names that file.
# [gates]
# static = "ruff check ."
# runtime = "make test"
# protect = ["ruff.toml", ".eslintrc*", "tests/arch/**"]
# suppress = ['# noqa', 'eslint-disable', '@pytest\\.mark\\.skip']   # regexes ('literal' strings)
# repros = "tests/harden"       # where a harden task commits <incident id>.patch when an incident's patch is stale
# tests = ["tests/**", "**/*_test.go"]   # the project's test files (fnmatch, like protect). A branch that adds or
#                               # changes one must prove it: on its base with only its test files overlaid,
#                               # runtime goes red, or the worker gets a fix round (a task's `prove: no` opts out)
# prove_tiers = ["sonnet", "opus"]   # worker tiers whose branches get that proof (default: every worker tier)

# Tasks whose finished branch waits at status `review` for a human `ergon accept` instead of landing.
# A task matches a rule if its tier is in tiers (when given) and a touches: entry matches (when
# given). A task's own `accept: yes` opts it in too. Nothing opts a task out.
# [[accept]]
# tiers = ["opus"]
# touches = ["server/**"]       # fnmatch patterns, like [[protect]]

# Tasks whose done needs runtime evidence: the worker ends with `EVIDENCE: <path> — <what it shows>`
# lines (test output, screenshots), copied to <state>/runs/<ID>-evidence/ for the reviewer and the
# task page; a done with none goes to a fix round. Matched like [[accept]]; a task's own
# `evidence: yes` opts it in too.
# [[evidence]]
# tiers = ["sonnet"]
# touches = ["ui/**"]

# A separate repo checked out inside this one that tasks may change, e.g. a public product
# repo next to a private install. Each dispatched task touching it gets its own worktree of it.
# [[companion]]
# name = "org/product"          # how tasks name it in touches:
# path = ".product/src"         # its checkout, relative to this file
# trunk = "main"
# forbid_tracker_ids = true     # refuse to land commits there that cite T-/E- IDs
# policy = "docs/boundary.md"   # cited to workers as the reason

# Share the task queue with a team (docs/shared-state.md): <state>/ becomes a worktree of an
# orphan branch on this remote, kept in step by `ergon sync`. Every brief and Note is published:
# use a private repo if this one is public. Without this table task state stays local.
# [shared]
# remote = "origin"             # a remote name or URL
# branch = "ergon-state"

# Shared mode only: a task matching a rule needs signed approvals of its current brief from two
# different principals in the approvers file before it leaves `proposed`. Matched like [[accept]]:
# its tier is in tiers (when given) and a touches: entry matches (when given).
# [[two_person]]
# tiers = ["opus"]
# touches = ["server/auth/**"]

# Paths or nouns no task below a tier may touch; `check` fails an open task that does.
# [[protect]]
# touches = ["server/auth/**", "prod-db"]   # fnmatch patterns against a task's touches:
# min_tier = "opus"             # or "human"
# note = "signing keys live here"
"""


SHARED = None  # [shared]: {remote, branch}, or None for local-only state
GATE_FILES = [CONFIG_FILE]  # [gates] protect, plus the config itself
SUPPRESS = []  # [gates] suppress, compiled
REPROS = "tests/harden"  # [gates] repros: a harden task's reproductions, <incident id>.patch
TESTS = []  # [gates] tests: the project's test files, for the red-on-base proof
PROVE_TIERS = list(WORKER_TIERS)  # [gates] prove_tiers: worker tiers whose branches get that proof
PROTECT = []  # [[protect]] rules: {touches, min_tier, note}
ACCEPT = []  # [[accept]] rules: {tiers, touches}
EVIDENCE = []  # [[evidence]] rules: {tiers, touches}; a matching task's done needs EVIDENCE: files
TWO_PERSON = []  # [[two_person]] rules: {tiers, touches}; shared mode only
FOLLOW_UP_DEPTH = 3  # follow_up_depth: see tasks.auto_approval
FOLLOW_UPS = 10  # follow_ups: auto-approved follow-ups one dispatch run works


def protect_hit(touch, pattern):
    """Does a task's touches: entry fall under a protect pattern? fnmatch on the entry, on each
    of its leading directories, and on the entry as a directory."""
    touch = touch.strip("/")
    parts = touch.split("/")
    cands = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)] + [touch + "/"]
    return any(fnmatch.fnmatchcase(c, pattern) for c in cands)


class NotConfigured(Exception):
    pass


def mini_toml(text, name=CONFIG_FILE):
    """Enough TOML for .ergon.toml (and the concept map) on Pythons before 3.11 (no tomllib):
    top-level keys, [table] and [[array-of-tables]] headers, and one-line string / bool / int /
    array values. `name` labels the errors."""
    def strip_comment(line):
        q = None
        for i, ch in enumerate(line):
            if q:
                q = None if ch == q and line[i - 1] != "\\" else q
            elif ch in "\"'":
                q = ch
            elif ch == "#":
                return line[:i]
        return line

    def value(v):
        v = re.sub(r"'([^']*)'", lambda m: json.dumps(m.group(1)), v.strip())
        return json.loads(re.sub(r",\s*]$", "]", v))

    out = cur = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw).strip()
        if not line:
            continue
        if m := re.fullmatch(r"\[\[\s*([\w-]+)\s*\]\]", line):
            cur = {}
            out.setdefault(m.group(1), []).append(cur)
        elif m := re.fullmatch(r"\[\s*([\w-]+)\s*\]", line):
            cur = out.setdefault(m.group(1), {})
        elif m := re.fullmatch(r"([\w-]+)\s*=\s*(.+)", line):
            try:
                cur[m.group(1)] = value(m.group(2))
            except ValueError:
                raise NotConfigured(f"{name}:{n}: can't read {m.group(2)!r} (one-line values only)")
        else:
            raise NotConfigured(f"{name}:{n}: can't read {raw!r}")
    return out


def read_config(path, name=CONFIG_FILE):
    """The TOML file at `path`, by tomllib where there is one, else by mini_toml. NotConfigured,
    labelled `name`, when it can't be read."""
    try:
        import tomllib
    except ModuleNotFoundError:
        return mini_toml(path.read_text(), name)
    with open(path, "rb") as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as err:
            if name == CONFIG_FILE:
                raise
            raise NotConfigured(f"{name}: {err}")


def git_toplevels(start):
    """(this checkout, the main checkout) for the git repo containing `start`."""
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode:
        raise NotConfigured(f"{start} isn't inside a git repository")
    wl = subprocess.run(["git", "-C", str(start), "worktree", "list", "--porcelain"],
                        capture_output=True, text=True).stdout
    main = wl.splitlines()[0][len("worktree "):] if wl.startswith("worktree ") else r.stdout.strip()
    return Path(r.stdout.strip()), Path(main)


def configure(start=None, need_config=True):
    """Point every module-level path at the project around `start` (default: the cwd).

    REPO is the checkout we're in (an epic or task worktree, possibly): repo content (map
    cards, the log, worker rules) is read there, and its branch is what `set` checks. HOME is
    the main checkout: task state, worktrees, run logs and companion checkouts live there, so
    every worktree shares one set of task files. Config paths resolve the same way."""
    global REPO, HOME, CFG, STATE, ITEMS, EPICS, LOCK, RUNS, WORKTREES, AGENTS, MAIN, MAP, LOG, \
        RETIRED_FILE, WORKER_RULES, CONCEPTS, POLICIES, COMPANIONS, TASK_FILE, TASK_DIR_IN_CMD, RUNS_PATH, RUNS_IN_CMD, REVIEW, ACCEPT, EVIDENCE, \
        REVIEW_ROUNDS, VERIFY_ROUNDS, CONFLICT_ROUNDS, RESUME_SECS, RESUME_TOKENS, SHARED, TWO_PERSON, JOURNAL, LEARN, \
        FOLLOW_UP_DEPTH, FOLLOW_UPS, GATE_FILES, SUPPRESS, REPROS, TESTS, PROVE_TIERS
    REPO, HOME = git_toplevels(Path(start or os.getcwd()).resolve())
    # This checkout's config wins: it's tracked, so an epic branch that adds or changes it
    # (adopting ergon, say) works before it merges. A worktree cut from a base that predates
    # the config falls back to the main checkout's.
    path = next((p for p in (REPO / CONFIG_FILE, HOME / CONFIG_FILE) if p.exists()), HOME / CONFIG_FILE)
    if not path.exists():
        if need_config:
            raise NotConfigured(f"no {CONFIG_FILE} in {HOME}: run `ergon init` there")
        CFG = {}
    else:
        CFG = read_config(path)
    opt = lambda k: CFG.get(k) or None
    STATE = HOME / CFG.get("state", "tasks")
    ITEMS = STATE / "items"  # tasks with no epic
    EPICS = STATE / "epics"  # one folder per epic: <E-NNN-slug>/EPIC.md and its T-NNN-*.md
    # Held while reading-then-writing task files or moving branches, so that several dispatch
    # runs and sessions (one per epic, say) can work at once. Gitignored with items/.
    LOCK = ITEMS / ".lock"
    RUNS = STATE / "runs"  # dispatch: worker logs
    WORKTREES = HOME / CFG.get("worktrees", ".claude/worktrees")  # <ID> per task, E-NNN per epic
    AGENTS = HOME / ".claude" / "agents"  # project overrides of the plugin's task-<tier>.md
    MAIN = CFG.get("trunk", "main")
    MAP = REPO / opt("map") if opt("map") else None
    LOG = opt("log")  # a path relative to the repo, e.g. TODO.md
    RETIRED_FILE = REPO / opt("retired") if opt("retired") else None
    WORKER_RULES = REPO / opt("worker_rules") if opt("worker_rules") else None
    # The concept map (T-141), tracked repo content like the map cards. Read only by the commands
    # that use it (concepts.load), so a bad map fails `check`, not every command.
    CONCEPTS = REPO / opt("concepts") if opt("concepts") else None
    # Settled review findings, written by a human only (the p answer of the interactive review); inlined into prompts.
    POLICIES = HOME / opt("policies") if opt("policies") else STATE / "policies.md"
    # Worker tiers whose finished branches a reviewer must pass before they land. Opt-in: none
    # by default. After --escalate, either the task's own tier or the one that finished counts.
    REVIEW = CFG.get("review") or []
    REVIEW = [REVIEW] if isinstance(REVIEW, str) else REVIEW
    if not isinstance(REVIEW, list) or any(x not in WORKER_TIERS for x in REVIEW):
        raise NotConfigured(f"{CONFIG_FILE}: review lists worker tiers to review, from {WORKER_TIERS}")
    # A review fail goes back to the worker this many times before the task blocks (T-026). The
    # worker's own session is resumed while its prompt cache is likely alive (an hour's TTL less
    # a margin) and its context is light; otherwise a fresh worker gets the findings.
    def count(key, default):
        v = CFG.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise NotConfigured(f"{CONFIG_FILE}: {key} is a whole number, 0 or more")
        return v
    GATES = CFG.get("gates") or {}
    strings = lambda v: isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    if not isinstance(GATES, dict) or any(
            k not in ("static", "runtime", "protect", "suppress", "repros", "tests", "prove_tiers")
            or not (isinstance(v, str) if k in ("static", "runtime", "repros") else strings(v)) for k, v in GATES.items()):
        raise NotConfigured(f"{CONFIG_FILE}: [gates] takes static and runtime, each a shell command, "
                            f"protect, suppress and tests, each a list of patterns, repros, a directory, "
                            f"and prove_tiers, a list of worker tiers")
    # The weakening check (T-101): gate files a branch may change only where its task's touches:
    # names the file, and markers it may add only there in a harden task (T-118). The config itself
    # is always a gate file.
    GATE_FILES = list(dict.fromkeys([CONFIG_FILE] + [x.strip().strip("/") for x in GATES.get("protect") or []]))
    SUPPRESS = []
    for x in GATES.get("suppress") or []:
        try:
            SUPPRESS.append(re.compile(x))
        except re.error as err:
            raise NotConfigured(f"{CONFIG_FILE}: [gates] suppress {x!r} isn't a regex: {err}")
    # A harden task's reproductions (T-106): a directory in the repo, never outside it.
    REPROS = (GATES.get("repros") or "tests/harden").strip().strip("/")
    if not REPROS or any(x in (".", "..", "") for x in REPROS.split("/")):
        raise NotConfigured(f"{CONFIG_FILE}: [gates] repros is a directory inside the repo")
    # The red-on-base proof (T-102): a branch that adds or changes a test file must go red on its
    # base with only those files overlaid. Off without tests; prove_tiers narrows it (an empty list
    # turns it off for every tier).
    TESTS = list(dict.fromkeys(x.strip().strip("/") for x in GATES.get("tests") or []))
    PROVE_TIERS = list(GATES["prove_tiers"]) if "prove_tiers" in GATES else list(WORKER_TIERS)
    if any(x not in WORKER_TIERS for x in PROVE_TIERS):
        raise NotConfigured(f"{CONFIG_FILE}: [gates] prove_tiers lists worker tiers, from {WORKER_TIERS}")
    REVIEW_ROUNDS = count("review_rounds", 2)
    VERIFY_ROUNDS = count("verify_rounds", 2)  # a red verify goes back to the worker this many times (T-027)
    CONFLICT_ROUNDS = count("conflict_rounds", 1)  # a branch that conflicts with its base goes back to its worker (T-095)
    RESUME_SECS = count("review_resume_secs", 3300)
    RESUME_TOKENS = count("review_resume_tokens", 80000)
    # How many FOUND follow-ups deep a non-feature follow-up is approved by inheritance (T-136);
    # 0 turns that off. tasks.auto_approval has the rule.
    FOLLOW_UP_DEPTH = count("follow_up_depth", 3)
    FOLLOW_UPS = count("follow_ups", 10)  # auto-approved follow-ups one dispatch run works (T-137)
    JOURNAL = CFG.get("journal", True)  # write an epic's journal entry (`ergon journal`) when it finishes
    if not isinstance(JOURNAL, bool):
        raise NotConfigured(f"{CONFIG_FILE}: journal is true or false")
    LEARN = CFG.get("learn", False)  # dispatch runs `ergon learn` when new incidents wait (T-107)
    if not isinstance(LEARN, bool):
        raise NotConfigured(f"{CONFIG_FILE}: learn is true or false")
    COMPANIONS = []
    for c in CFG.get("companion") or []:
        if not c.get("path"):
            raise NotConfigured(f"{CONFIG_FILE}: every [[companion]] needs a path")
        name = c.get("name") or c["path"]
        COMPANIONS.append({
            "name": name, "path": HOME / c["path"], "rel": c["path"].strip("/"),
            "trunk": c.get("trunk", "main"),
            "touches": tuple(t.strip("/") for t in c.get("touches") or [name, c["path"]]),
            "forbid_ids": bool(c.get("forbid_tracker_ids", False)), "policy": c.get("policy", "")})
    # Opt-in shared task state (docs/shared-state.md): <state>/ becomes a git worktree of an
    # orphan branch that `ergon sync` fetches, merges and pushes. None: state is local-only, and
    # nothing touches git for it.
    SHARED = None
    sh = CFG.get("shared")
    if sh is not None:
        if not isinstance(sh, dict) or not isinstance(sh.get("remote"), str) or not sh["remote"].strip():
            raise NotConfigured(f"{CONFIG_FILE}: [shared] needs remote, a remote name or URL")
        branch = sh.get("branch", "ergon-state")
        if (not isinstance(branch, str) or not re.fullmatch(r"[\w.-]+(?:/[\w.-]+)*", branch)
                or branch == MAIN or branch.startswith((TASK_BRANCH, "epic/"))):
            raise NotConfigured(f"{CONFIG_FILE}: [shared] branch must be a plain branch name of its own, "
                                f"not the trunk, task/… or epic/…")
        SHARED = {"remote": sh["remote"].strip(), "branch": branch}
    PROTECT.clear()
    for n, pr in enumerate(CFG.get("protect") or [], 1):
        if pr.get("min_tier") not in _tasks.TIERS or not pr.get("touches"):
            raise NotConfigured(f"{CONFIG_FILE}: [[protect]] #{n} needs touches and a min_tier in {_tasks.TIERS}")
        PROTECT.append({"touches": [str(x).strip("/") for x in pr["touches"]],
                        "min_tier": pr["min_tier"], "note": pr.get("note", "")})
    def match_rules(key, allowed):
        """[[key]] rules as {tiers, touches}: each needs tiers (from `allowed`) and/or touches."""
        out = []
        for n, rule in enumerate(CFG.get(key) or [], 1):
            if not isinstance(rule, dict):
                raise NotConfigured(f"{CONFIG_FILE}: {key} must be a [[{key}]] table")
            tiers, touches = rule.get("tiers") or [], rule.get("touches") or []
            tiers = [tiers] if isinstance(tiers, str) else tiers
            touches = [touches] if isinstance(touches, str) else touches
            if (not (tiers or touches) or not isinstance(tiers, list) or not isinstance(touches, list)
                    or any(x not in allowed for x in tiers)):
                raise NotConfigured(f"{CONFIG_FILE}: [[{key}]] #{n} needs tiers (from {allowed}) and/or touches")
            out.append({"tiers": list(tiers), "touches": [str(x).strip("/") for x in touches]})
        return out
    ACCEPT = match_rules("accept", WORKER_TIERS)
    EVIDENCE = match_rules("evidence", WORKER_TIERS)
    # The two-person rule (docs/shared-state.md) counts verified approvals from distinct
    # principals in the approvers file, which only shared mode has. Without [shared] it would
    # silently mean nothing, so it is an error instead.
    TWO_PERSON = match_rules("two_person", _tasks.TIERS)
    if TWO_PERSON and SHARED is None:
        raise NotConfigured(f"{CONFIG_FILE}: [[two_person]] needs [shared]: it counts signed approvals from "
                            f"different approvers, and only shared mode signs and verifies approvals")
    state_rel = re.escape(STATE.relative_to(HOME).as_posix())
    TASK_FILE = re.compile(rf"(?:^|/){state_rel}/(items/[^/\s]+|epics/[^/\s]+/(EPIC|T-[^/\s]+))\.md$")
    TASK_DIR_IN_CMD = re.compile(rf"(?:^|[\s/\"'=]){state_rel}/(items|epics)\b")
    # Run logs and history.jsonl: written by dispatch and the CLI only, never by an agent's tool call.
    RUNS_PATH = re.compile(rf"(?:^|/){state_rel}/runs(?:/|$)")
    RUNS_IN_CMD = re.compile(rf"(?:^|[\s/\"'=]){state_rel}/runs\b|\bhistory\.jsonl\b")
