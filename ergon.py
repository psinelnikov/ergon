#!/usr/bin/env python3
"""ergon: a task tracker where humans approve and agents execute.

One markdown file per task / epic, frontmatter holds the state. Each task is sized by the
cheapest model tier that can do it; no agent starts one until a human approves it. The
workflow and the approval rule: `ergon guide`. Per-project settings: .ergon.toml in the
repo's main checkout (`ergon init` writes one). No dependencies beyond the standard library.

  ergon                        board: open work grouped by status, then tier
  ergon init                   write .ergon.toml, the state folders and .gitignore lines here
  ergon guide                  print the workflow guide (also the plugin's `ergon` skill)
  ergon rules                  print this project's worker rules (worker_rules in .ergon.toml)
  ergon next [--tier T]        approved, unblocked tasks an agent of tier T may take
  ergon review [-i]            everything waiting on human approval (-i: HUMAN ONLY, walk it
                               task by task: approve / reject / retier / edit / skip)
  ergon show ID
  ergon new task "Title" --tier sonnet [--epic E-001] [--touches a,b] [--source LOG.md:42]
  ergon new epic "Title"
  ergon set ID status=doing    agents: ready -> doing -> done | blocked (never approval)
  ergon start E-ID             branch epic/<folder> off the trunk and check it out in its own
                               worktree, <worktrees>/E-ID (also happens on an epic's first `doing`)
  ergon finish E-ID            close the epic and merge its branch into the trunk (also happens
                               when its last task goes done); no checkout needs switching
  ergon approve ID [ID ...]    HUMAN ONLY, real terminal. An epic ID approves its proposed tasks
  ergon reject ID [--reason R] HUMAN ONLY. proposed -> dropped; review -> ready (needs --reason,
                               which the next worker is shown; branch and worktree are kept)
  ergon accept ID [ID ...]     HUMAN ONLY, real terminal. Land a task dispatch held at `review`
                               (merge its branch like dispatch would, close the epic if last).
                               An epic ID accepts its tasks in review
  ergon close ID [ID ...] --as done|dropped --reason R
                               HUMAN ONLY. An approved task whose work landed elsewhere, or that
                               no longer applies; skips the epic-branch check, closes the epic
                               if it was the last open task
  ergon audit [--epic E]       read-only staleness leads: dead or unapproved dependencies,
                               commits already citing open tasks, citations that don't resolve,
                               companion-repo paths a task's touches: doesn't name, retired
                               nouns. Judgment: the task-audit skill
  ergon check                  lint; non-zero exit on any violation
  ergon tidy                   move every task and epic file to where it belongs: an epic's tasks
                               in epics/<epic>/, the rest in items/ (idempotent)
  ergon dispatch [--tier T] [--epic E] [--ids T-1,T-2] [--max N] [--jobs J]
                      [--budget-total USD [--budget-pct P]] [--watch [--interval SEC]]
                      [--dry-run]
                               run ready tasks headless, one worktree + task/<ID> branch each,
                               merge each finished branch into its epic branch (and, for a task
                               touching a companion repo, that repo's local trunk).
                               --watch (needs --budget-total) keeps rescanning for newly ready
                               tasks every --interval seconds (default 30) until the cap is hit
                               or Ctrl-C (stop launching, let workers finish; a second kills them)
  ergon stats [--since DATE] [--epic E]
                               read-only: per tier, runs / done / blocked / block rate / cost /
                               timeouts from <state>/runs/*.json, and tasks that blocked twice+
  ergon hook guard|stop        Claude Code hooks (the plugin wires them); not for humans
"""
import argparse
import datetime
import fcntl
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent  # this ergon checkout: the plugin root
CONFIG_FILE = ".ergon.toml"
GUIDE = HERE / "skills" / "ergon" / "SKILL.md"
PLUGIN_AGENTS = HERE / "agents"
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
# verify = "make test"          # run in the task worktree before landing; non-zero exit blocks the task
#                               # (a task's `verify:` frontmatter overrides it)
# review = ["haiku", "sonnet"]  # worker tiers whose branches a reviewer one tier up must pass before
#                               # landing (opus reviews opus); a fail blocks the task

# Tasks whose finished branch waits at status `review` for a human `ergon accept` instead of landing.
# A task matches a rule if its tier is in tiers (when given) and a touches: entry matches (when
# given). A task's own `accept: yes` opts it in too. Nothing opts a task out.
# [[accept]]
# tiers = ["opus"]
# touches = ["server/**"]       # fnmatch patterns, like [[protect]]

# A separate repo checked out inside this one that tasks may change, e.g. a public product
# repo next to a private install. Each dispatched task touching it gets its own worktree of it.
# [[companion]]
# name = "org/product"          # how tasks name it in touches:
# path = ".product/src"         # its checkout, relative to this file
# trunk = "main"
# forbid_tracker_ids = true     # refuse to land commits there that cite T-/E- IDs
# policy = "docs/boundary.md"   # cited to workers as the reason

# Paths or nouns no task below a tier may touch; `check` fails an open task that does.
# [[protect]]
# touches = ["server/auth/**", "prod-db"]   # fnmatch patterns against a task's touches:
# min_tier = "opus"             # or "human"
# note = "signing keys live here"
"""


PROTECT = []  # [[protect]] rules: {touches, min_tier, note}
ACCEPT = []  # [[accept]] rules: {tiers, touches}


def protect_hit(touch, pattern):
    """Does a task's touches: entry fall under a protect pattern? fnmatch on the entry, on each
    of its leading directories, and on the entry as a directory."""
    touch = touch.strip("/")
    parts = touch.split("/")
    cands = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)] + [touch + "/"]
    return any(fnmatch.fnmatchcase(c, pattern) for c in cands)


class NotConfigured(Exception):
    pass


def mini_toml(text):
    """Enough TOML for .ergon.toml on Pythons before 3.11 (no tomllib): top-level keys,
    [table] and [[array-of-tables]] headers, and one-line string / bool / int / array values."""
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
                raise NotConfigured(f"{CONFIG_FILE}:{n}: can't read {m.group(2)!r} (one-line values only)")
        else:
            raise NotConfigured(f"{CONFIG_FILE}:{n}: can't read {raw!r}")
    return out


def read_config(path):
    try:
        import tomllib
    except ModuleNotFoundError:
        return mini_toml(path.read_text())
    with open(path, "rb") as f:
        return tomllib.load(f)


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
        RETIRED_FILE, WORKER_RULES, COMPANIONS, TASK_FILE, TASK_DIR_IN_CMD, REVIEW, ACCEPT
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
    # Worker tiers whose finished branches a reviewer must pass before they land. Opt-in: none
    # by default. After --escalate, either the task's own tier or the one that finished counts.
    REVIEW = CFG.get("review") or []
    REVIEW = [REVIEW] if isinstance(REVIEW, str) else REVIEW
    if not isinstance(REVIEW, list) or any(x not in WORKER_TIERS for x in REVIEW):
        raise NotConfigured(f"{CONFIG_FILE}: review lists worker tiers to review, from {WORKER_TIERS}")
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
    PROTECT.clear()
    for n, pr in enumerate(CFG.get("protect") or [], 1):
        if pr.get("min_tier") not in TIERS or not pr.get("touches"):
            raise NotConfigured(f"{CONFIG_FILE}: [[protect]] #{n} needs touches and a min_tier in {TIERS}")
        PROTECT.append({"touches": [str(x).strip("/") for x in pr["touches"]],
                        "min_tier": pr["min_tier"], "note": pr.get("note", "")})
    ACCEPT = []
    for n, ac in enumerate(CFG.get("accept") or [], 1):
        tiers, touches = ac.get("tiers") or [], ac.get("touches") or []
        tiers = [tiers] if isinstance(tiers, str) else tiers
        touches = [touches] if isinstance(touches, str) else touches
        if not (tiers or touches) or any(x not in WORKER_TIERS for x in tiers):
            raise NotConfigured(f"{CONFIG_FILE}: [[accept]] #{n} needs tiers (from {WORKER_TIERS}) and/or touches")
        ACCEPT.append({"tiers": list(tiers), "touches": [str(x).strip("/") for x in touches]})
    state_rel = re.escape(STATE.relative_to(HOME).as_posix())
    TASK_FILE = re.compile(rf"(?:^|/){state_rel}/(items/[^/\s]+|epics/[^/\s]+/(EPIC|T-[^/\s]+))\.md$")
    TASK_DIR_IN_CMD = re.compile(rf"(?:^|[\s/\"'=]){state_rel}/(items|epics)\b")


TIERS = ["haiku", "sonnet", "opus", "human"]
TASK_STATUSES = ["proposed", "ready", "doing", "review", "blocked", "done", "dropped"]
EPIC_STATUSES = ["open", "done", "dropped"]
# Statuses a task can only reach after a human approved it.
NEEDS_APPROVAL = {"ready", "doing", "review", "blocked", "done"}
# Transitions `set` allows. proposed -> ready is approve's job, not set's. Only dispatch moves a
# task into `review`, and only a human moves it out: `accept` (-> done) or `reject` (-> ready).
AGENT_MOVES = {
    "ready": {"doing", "blocked"},
    "doing": {"done", "blocked", "ready"},
    "blocked": {"ready", "doing"},
}
TASK_KEYS = ["id", "title", "tier", "status", "epic", "depends", "touches", "source",
             "created", "approved", "tier_why"]
EPIC_KEYS = ["id", "title", "status", "created"]


# ---------- file format ----------

def parse(path):
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if not m:
        raise SystemExit(f"{path}: missing frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        val = re.sub(r"\s+#.*$", "", val).strip()
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip() for v in val[1:-1].split(",") if v.strip()]
        meta[key.strip()] = val
    meta["_path"] = path
    meta["_body"] = m.group(2)
    return meta


def dump(meta, keys):
    lines = ["---"]
    for k in keys + [k for k in meta if k not in keys and not k.startswith("_")]:
        v = meta.get(k, "")
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        lines.append(f"{k}: {v}".rstrip())
    lines.append("---")
    meta["_path"].write_text("\n".join(lines) + "\n" + meta["_body"])


def files(kind):
    """Every file of a kind: ITEMS means all tasks, wherever they're filed; EPICS all epic briefs.
    Other files in an epic's folder (design notes, say) are left alone."""
    if kind == EPICS:
        return sorted(EPICS.glob(f"*/{EPIC_FILE}"))
    return sorted([*ITEMS.glob("*.md"), *EPICS.glob("*/T-*.md")], key=lambda p: p.name)


def load(kind):
    return {m["id"]: m for m in (parse(p) for p in files(kind))}


def task_home(t, epics):
    """The folder a task's file belongs in, going by its `epic:` field."""
    e = epics.get(t.get("epic"))
    return e["_path"].parent if e else ITEMS


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50].rstrip("-")


def next_id(kind, prefix):
    nums = [int(m.group(1)) for p in files(kind)
            if (m := re.match(rf"{prefix}-(\d+)", p.parent.name if kind == EPICS else p.name))]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def today():
    return datetime.date.today().isoformat()


# ---------- views ----------

def deps_done(t, tasks):
    return all(tasks.get(d, {}).get("status") == "done" for d in t.get("depends") or [])


def line(t, tasks):
    flags = []
    if t.get("epic"):
        flags.append(t["epic"])
    if t["status"] == "ready" and not deps_done(t, tasks):
        flags.append("waits on " + ",".join(d for d in t["depends"]
                                             if tasks.get(d, {}).get("status") != "done"))
    tail = f"  ({'; '.join(flags)})" if flags else ""
    return f"  {t['id']}  {t['tier']:<6}  {t['title']}{tail}"


YES = ("yes", "true", "1")


def needs_accept(t, *tiers):
    """Does a finished branch of t wait at `review` for a human `accept`? Its own `accept: yes`, or
    an [[accept]] rule its tier (or any of `tiers`, e.g. the one an --escalate finished at) and
    touches: match."""
    if str(t.get("accept", "")).lower() in YES:
        return True
    have = {t.get("tier"), *tiers}
    for rule in ACCEPT:
        if rule["tiers"] and not have & set(rule["tiers"]):
            continue
        if rule["touches"] and not any(protect_hit(str(x), p) for x in t.get("touches") or []
                                       for p in rule["touches"]):
            continue
        return True
    return False


def board(args):
    tasks, epics = load(ITEMS), load(EPICS)
    shown = [t for t in tasks.values()
             if (not args.tier or t["tier"] == args.tier)
             and (not args.epic or t.get("epic") == args.epic)
             and (args.all or t["status"] not in ("done", "dropped"))]
    for status in TASK_STATUSES:
        group = sorted((t for t in shown if t["status"] == status),
                       key=lambda t: (TIERS.index(t["tier"]), t["id"]))
        if group:
            print(f"\n{status.upper()} ({len(group)})")
            for t in group:
                print(line(t, tasks))
    if epics and not args.tier:
        print("\nEPICS")
        for e in epics.values():
            if args.epic and e["id"] != args.epic:
                continue
            kids = [t for t in tasks.values() if t.get("epic") == e["id"] and t["status"] != "dropped"]
            done = sum(t["status"] == "done" for t in kids)
            pending = sum(t["status"] == "proposed" for t in kids)
            note = f", {pending} awaiting approval" if pending else ""
            note += "  <- needs breakdown" if not kids and e["status"] == "open" else ""
            note += f"  on {epic_branch(e)}" if e["status"] == "open" and branch_exists(epic_branch(e)) else ""
            print(f"  {e['id']}  {e['status']:<7} {done}/{len(kids)} done{note}  {e['title']}")
    counts = {tier: sum(t["tier"] == tier and t["status"] not in ("done", "dropped")
                        for t in tasks.values()) for tier in TIERS}
    print("\nOPEN BY TIER  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))


def nxt(args):
    tasks = load(ITEMS)
    ok = [t for t in tasks.values() if t["status"] == "ready" and deps_done(t, tasks)
          and (not args.tier or t["tier"] == args.tier)]
    for t in sorted(ok, key=lambda t: (TIERS.index(t["tier"]), t["id"])):
        print(line(t, tasks))
    if not ok:
        print("nothing approved and unblocked" + (f" for {args.tier}" if args.tier else ""))


def review(args):
    if args.interactive:
        return review_interactive(args)
    tasks, epics = load(ITEMS), load(EPICS)
    waiting = [t for t in tasks.values() if t["status"] == "proposed"]
    by_epic = {}
    for t in waiting:
        by_epic.setdefault(t.get("epic") or "", []).append(t)
    for eid, group in sorted(by_epic.items()):
        print(f"\n{eid + '  ' + epics[eid]['title'] if eid in epics else '(no epic)'}")
        for t in sorted(group, key=lambda t: t["id"]):
            print(line(t, tasks))
            if t.get("tier_why"):
                print(f"         why {t['tier']}: {t['tier_why']}")
    if not waiting:
        print("nothing awaiting approval")
    else:
        print("\napprove from your own terminal:  ergon approve <ID|E-ID> ...")
    held = sorted((t for t in tasks.values() if t["status"] == "review"), key=lambda t: t["id"])
    if held:
        print(f"\nAWAITING ACCEPTANCE ({len(held)}): finished branches, not merged")
        for t in held:
            print(line(t, tasks) + f"  [{TASK_BRANCH}{t['id']}]")
        print("\nfrom your own terminal:  ergon accept <ID|E-ID> ...   or   ergon reject <ID> --reason R")


def show(args):
    tasks, epics = load(ITEMS), load(EPICS)
    item = tasks.get(args.id) or epics.get(args.id)
    if not item:
        raise SystemExit(f"no such id: {args.id}")
    print(item["_path"].read_text())
    if args.id in epics:
        print("TASKS")
        for t in sorted((t for t in tasks.values() if t.get("epic") == args.id), key=lambda t: t["id"]):
            print(f"  {t['status']:<8}" + line(t, tasks)[1:])


# ---------- writes ----------

TASK_BODY = """
## What

## Done when

## Notes
"""

EPIC_BODY = """
## Goal

## Out of scope

## Constraints
<!-- invariants, hosts that need a human, decisions already made -->
"""


def new(args):
    if args.kind == "task":
        if args.tier not in TIERS:
            raise SystemExit(f"--tier must be one of {TIERS}")
        epics = load(EPICS)
        if args.epic and args.epic not in epics:
            raise SystemExit(f"no such epic: {args.epic}")
        tid = next_id(ITEMS, "T")
        home = epics[args.epic]["_path"].parent if args.epic else ITEMS
        meta = {"id": tid, "title": args.title, "tier": args.tier, "status": "proposed",
                "epic": args.epic or "", "depends": [], "touches": args.touches.split(",") if args.touches else [],
                "source": args.source or "", "created": today(), "approved": "", "tier_why": "",
                "_path": home / f"{tid}-{slug(args.title)}.md", "_body": TASK_BODY}
        dump(meta, TASK_KEYS)
    else:
        eid = next_id(EPICS, "E")
        home = EPICS / f"{eid}-{slug(args.title)}"
        home.mkdir()
        meta = {"id": eid, "title": args.title, "status": "open", "created": today(),
                "_path": home / EPIC_FILE, "_body": EPIC_BODY}
        dump(meta, EPIC_KEYS)
    print(meta["_path"].relative_to(HOME))


# ---------- epic branches ----------
# An epic's work lives on epic/<epic folder name>, cut from main when the epic starts and checked
# out in its own worktree (.claude/worktrees/E-NNN), so several epics can be worked at once.
# It merges back (--no-ff, local only, never pushed) when its last task is done or dropped,
# without switching any checkout but the epic's own.

class GitError(Exception):
    pass


@contextmanager
def state_lock():
    """Exclusive across processes and threads (each call opens its own descriptor). Not reentrant."""
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def git(*args, raw=False, cwd=None):
    r = subprocess.run(["git", "-C", str(cwd or REPO), *args], capture_output=True, text=True)
    if r.returncode:
        raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout if raw else r.stdout.strip()


def epic_branch(e):
    return f"epic/{e['_path'].parent.name}"


def branch_exists(name, repo=None):
    try:
        git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo)
        return True
    except GitError:
        return False


def current_branch():
    return git("branch", "--show-current")


def dirty_paths(cwd=None):
    """Tracked files with uncommitted changes (untracked files don't block a merge)."""
    out = git("status", "--porcelain", "--untracked-files=no", raw=True, cwd=cwd)
    return [l[3:] for l in out.splitlines() if l.strip()]


def worktrees(repo=None):
    """{branch: path} for every checked-out branch in any worktree of this repo (or `repo`)."""
    out, path = {}, None
    for l in git("worktree", "list", "--porcelain", cwd=repo).splitlines():
        if l.startswith("worktree "):
            path = l[len("worktree "):]
        elif l.startswith("branch refs/heads/"):
            out[l[len("branch refs/heads/"):]] = path
    return out


def merge_check(base, branch, repo=None):
    """The tree merging branch into base would give, or GitError naming the conflicts."""
    tree = subprocess.run(["git", "-C", str(repo or REPO), "merge-tree", "--write-tree", base, branch],
                          capture_output=True, text=True)
    if tree.returncode:
        files = sorted(set(re.findall(r"CONFLICT \([^)]*\): .*? in (\S+)", tree.stdout)))
        raise GitError(f"conflicts with {base}" + (f" in {', '.join(files)}" if files else ""))
    return tree.stdout.split()[0]


def merge_into(base, branch, msg, no_ff=False, repo=None):
    """Land branch on base (in this repo, or `repo`) without touching any checkout that isn't
    on base. Raises GitError."""
    tree = merge_check(base, branch, repo)
    where = worktrees(repo).get(base)
    if where:  # base is checked out (e.g. your main checkout): merge there so its tree follows
        r = subprocess.run(["git", "-C", where, "merge", *(["--no-ff"] if no_ff else []),
                            "--no-edit", "-m", msg, branch], capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "-C", where, "merge", "--abort"], capture_output=True)
            raise GitError(f"merge in {where} refused: {(r.stderr or r.stdout).strip()[:300]}")
        return
    old, tip = git("rev-parse", base, cwd=repo), git("rev-parse", branch, cwd=repo)
    fork = git("merge-base", base, branch, cwd=repo)
    if fork == tip:  # already in base
        return
    new = tip if fork == old and not no_ff else \
        git("commit-tree", tree, "-p", old, "-p", tip, "-m", msg, cwd=repo)
    git("update-ref", f"refs/heads/{base}", new, old, cwd=repo)


def epic_complete(eid, tasks):
    kids = [t for t in tasks.values() if t.get("epic") == eid]
    return bool(kids) and all(t["status"] in ("done", "dropped") for t in kids)


def open_epic(e):
    """Cut the epic's branch from main if it has none, and check it out in its own worktree
    unless some checkout already has it. Returns that checkout's path."""
    branch = epic_branch(e)
    if not branch_exists(branch):
        git("branch", branch, MAIN)
        print(f"started {e['id']} on branch {branch} (cut from {MAIN})")
    where = worktrees().get(branch)
    if not where:
        where = WORKTREES / e["id"]
        git("worktree", "add", str(where), branch)
        print(f"checked out {branch} at {where}")
    return Path(where)


def enter_epic(e, starting):
    """Refuse unless this checkout is on the epic's branch. If `starting`, open the epic first,
    so the refusal says where it's checked out."""
    branch = epic_branch(e)
    if current_branch() == branch or not (starting or branch_exists(branch)):
        return
    where = open_epic(e) if starting else worktrees().get(branch)
    raise GitError(f"{e['id']} is worked on {branch}, " + (
        f"checked out at {where}: cd there and run this again" if where
        else f"which isn't checked out: `ergon start {e['id']}` opens a worktree for it"))


def release(branch, where):
    """Move the checkout at `where` off `branch` so the branch can go. Returns what happened."""
    wt = Path(where)
    own = wt.parent == WORKTREES  # the epic's own worktree, not the main checkout
    if own and wt.resolve() != REPO.resolve():  # never remove the checkout we're running from
        try:
            git("worktree", "remove", str(wt))
            return f"removed its worktree {wt}"
        except GitError:
            pass  # untracked files in it: detach it below and leave it for you
    if not own and MAIN not in worktrees():
        git("switch", MAIN, cwd=wt)
        return f"{wt} is now on {MAIN}"
    git("switch", "--detach", MAIN, cwd=wt)
    return f"{wt} is detached at {MAIN}" + (f"; `git worktree remove {wt}` when you're done in it" if own else "")


def start(args):
    epics = load(EPICS)
    e = epics.get(args.id)
    if not e:
        raise SystemExit(f"no such epic: {args.id}")
    if e["status"] != "open":
        raise SystemExit(f"{args.id} is {e['status']}")
    try:
        where = open_epic(e)
    except GitError as err:
        raise SystemExit(str(err))
    print(f"{args.id} is checked out at {where}: work and run `ergon set` there")


def branch_commits(branch):
    """(short hash, subject, full message) for each commit on `branch` not yet on main."""
    # --no-merges: dispatch lands each task/<ID> branch with a "Merge T-NNN" commit.
    out = git("log", "--no-merges", "--format=%h%x1f%s%x1f%B%x1e", f"{MAIN}..{branch}", raw=True)
    return [tuple(c.strip("\n").split("\x1f")) for c in out.split("\x1e") if c.strip()]


def cites(commit, tid):
    return re.search(rf"\b{tid}\b", commit[2]) is not None


def merge_message(e, tasks, commits):
    """The epic's permanent record. Task files are local, so this is what history keeps."""
    lines = [f"Merge {e['id']}: {e['title']}", ""]
    goal = re.search(r"## Goal\n(.*?)(?=\n## |\Z)", e["_body"], re.S)
    if goal and goal.group(1).strip():
        lines += [goal.group(1).strip(), ""]
    for t in sorted((t for t in tasks.values() if t.get("epic") == e["id"]), key=lambda t: t["id"]):
        lines.append(f"{t['id']} {t['title']}" + (" (dropped)" if t["status"] == "dropped" else ""))
        lines += [f"  {c[0]} {c[1]}" for c in commits if cites(c, t["id"])]
    return "\n".join(lines)


def finish_epic(eid):
    """Merge a complete epic's branch into main, then close it. Raises GitError if it can't."""
    tasks, epics = load(ITEMS), load(EPICS)
    e = epics[eid]
    if not epic_complete(eid, tasks):
        raise GitError(f"{eid} still has open tasks")
    branch = epic_branch(e)
    if not branch_exists(branch):
        e["status"] = "done"
        dump(e, EPIC_KEYS)
        print(f"{eid} done (never started on a branch, nothing to merge)")
        return
    where = worktrees().get(branch)
    stray = dirty_paths(where) if where else []
    if stray:
        raise GitError(f"uncommitted changes in {where} would miss the merge; commit them, then run "
                       f"`ergon finish {eid}`:\n  " + "\n  ".join(stray))
    try:
        merge_into(MAIN, branch, merge_message(e, tasks, branch_commits(branch)), no_ff=True)
    except GitError as err:
        raise GitError(f"merging {branch} into {MAIN} failed and changed nothing. Resolve it by hand "
                       f"(merge {MAIN} into {branch}, say), then run `ergon finish {eid}`.\n{err}")
    note = f"; {release(branch, where)}" if where else ""
    git("merge-base", "--is-ancestor", branch, MAIN)  # raises if the merge somehow didn't land
    git("branch", "-D", branch)  # -d would check against this checkout's HEAD, not main
    e["status"] = "done"
    dump(e, EPIC_KEYS)
    print(f"{eid} done: merged {branch} into {MAIN} (not pushed) and deleted the branch{note}")


def finish(args):
    if args.id not in load(EPICS):
        raise SystemExit(f"no such epic: {args.id}")
    try:
        finish_epic(args.id)
    except GitError as err:
        raise SystemExit(str(err))


def setf(args):
    tasks, epics = load(ITEMS), load(EPICS)
    item = tasks.get(args.id) or epics.get(args.id)
    if not item:
        raise SystemExit(f"no such id: {args.id}")
    is_task = args.id in tasks
    epic = epics.get(item.get("epic")) if is_task else None
    new_status = dict(kv.partition("=")[::2] for kv in args.pairs).get("status")
    if new_status and current_branch().startswith(TASK_BRANCH):
        raise SystemExit("you're a dispatched worker: the dispatcher owns status. End your "
                         "reply with `RESULT: done` or `RESULT: blocked — <why>`")
    if not is_task and new_status == "done":
        raise SystemExit(f"close epics with `ergon finish {args.id}` (it merges the epic branch)")
    for kv in args.pairs:
        key, _, val = kv.partition("=")
        if key == "approved":
            raise SystemExit("approval is human-only: use `approve` from a real terminal")
        if is_task and key == "accept" and val.lower() not in YES:
            raise SystemExit("set can only turn `accept:` on (accept=yes); a human clears it by editing the file")
        if is_task and key == "status":
            if val not in AGENT_MOVES.get(item["status"], set()):
                raise SystemExit(f"{args.id}: {item['status']} -> {val} not allowed via set"
                                 + (" (needs human approve)" if item["status"] == "proposed" else "")
                                 + (" (a human accepts or rejects it)" if item["status"] == "review" else ""))
        if is_task and key == "tier" and val not in TIERS:
            raise SystemExit(f"tier must be one of {TIERS}")
        if not is_task and key == "status" and val not in EPIC_STATUSES:
            raise SystemExit(f"epic status must be one of {EPIC_STATUSES}")
        if key in ("depends", "touches"):
            val = [v for v in val.split(",") if v]
        item[key] = val
    if epic and new_status:
        try:
            enter_epic(epic, starting=new_status == "doing")
        except GitError as err:
            raise SystemExit(str(err))
    if is_task and any(kv.partition("=")[0] == "epic" for kv in args.pairs):
        if item["epic"] and item["epic"] not in epics:
            raise SystemExit(f"no such epic: {item['epic']}")
        old, item["_path"] = item["_path"], task_home(item, epics) / item["_path"].name
        if old != item["_path"]:
            old.rename(item["_path"])
    dump(item, TASK_KEYS if is_task else EPIC_KEYS)
    if (epic and new_status == "done" and item["tier"] != "human"
            and branch_exists(epic_branch(epic))
            and not any(cites(c, args.id) for c in branch_commits(epic_branch(epic)))):
        print(f"warning: no commit on {epic_branch(epic)} cites {args.id}; task files are local, "
              f"so put {args.id} in the commit message or the work leaves no record")
    if epic and new_status in ("done", "dropped") and epic_complete(epic["id"], load(ITEMS)):
        try:
            finish_epic(epic["id"])
        except GitError as err:
            print(f"{args.id} is {new_status}; {epic['id']} is complete but not merged: {err}")


def ask(prompt):
    """Speed bump, not security: refuse inside agent shells, ask on the controlling tty."""
    if os.environ.get("CLAUDECODE") or os.environ.get("AI_AGENT"):
        raise SystemExit("refused: running inside an agent shell. A human approves from their own terminal.")
    try:
        with open("/dev/tty", "r") as tin, open("/dev/tty", "w") as tout:
            tout.write(prompt)
            tout.flush()
            return tin.readline().strip()
    except OSError:
        raise SystemExit("refused: no terminal to confirm on.")


def human_gate(prompt):
    return ask(prompt + " [y/N] ").lower() == "y"


def approver():
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
    return name or os.environ.get("USER", "human")


def targets(ids, tasks, epics, status="proposed"):
    """The tasks ids name; an epic ID names its tasks in `status`."""
    out = []
    for i in ids:
        if i in epics:
            out += [t for t in tasks.values() if t.get("epic") == i and t["status"] == status]
        elif i in tasks:
            out.append(tasks[i])
        else:
            raise SystemExit(f"no such id: {i}")
    return out


def approve(args):
    tasks, epics = load(ITEMS), load(EPICS)
    todo = [t for t in targets(args.ids, tasks, epics) if t["status"] == "proposed"]
    if not todo:
        raise SystemExit("nothing in `proposed` among those ids")
    for t in todo:
        print(line(t, tasks))
    if not human_gate(f"Approve these {len(todo)} task(s) as {approver()}?"):
        raise SystemExit("aborted")
    stamp = f"{approver()} {today()}"
    for t in todo:
        t["status"], t["approved"] = "ready", stamp
        dump(t, TASK_KEYS)
    print(f"approved {len(todo)}")


def review_interactive(args):
    """HUMAN ONLY: walk the proposed queue one task at a time. Every answer lands immediately."""
    tasks, epics = load(ITEMS), load(EPICS)
    queue = sorted((t for t in tasks.values() if t["status"] == "proposed"
                    and (not args.epic or t.get("epic") == args.epic)
                    and (not args.tier or t["tier"] == args.tier)),
                   key=lambda t: (t.get("epic") or "~", t["id"]))
    if not queue:
        raise SystemExit("nothing awaiting approval")
    stamp, tally = f"{approver()} {today()}", {"approved": 0, "dropped": 0, "skipped": 0}
    for i, t in enumerate(queue, 1):
        while True:
            epic = epics.get(t.get("epic"))
            print(f"\n{'=' * 78}\n[{i}/{len(queue)}]  {epic['id'] + '  ' + epic['title'] if epic else '(no epic)'}\n")
            print(t["_path"].read_text().rstrip())
            waits = [d for d in t.get("depends") or [] if tasks.get(d, {}).get("status") != "done"]
            if waits:
                print(f"\n(waits on {', '.join(waits)})")
            choice = ask(f"\n{t['id']} [{t['tier']}]  [a]pprove  [r]eject  [t]ier  [e]dit  [s]kip  [q]uit > ").lower()
            if choice == "a":
                t["status"], t["approved"] = "ready", stamp
                dump(t, TASK_KEYS)
                tally["approved"] += 1
                break
            if choice == "r":
                reason = ask("reason (optional): ")
                t["status"] = "dropped"
                if reason:
                    t["_body"] = t["_body"].rstrip() + f"\n\nRejected {today()}: {reason}\n"
                dump(t, TASK_KEYS)
                tally["dropped"] += 1
                break
            if choice == "t":
                tier = ask(f"tier {TIERS}: ").lower()
                if tier not in TIERS:
                    print(f"not a tier: {tier!r}")
                    continue
                t["tier"] = tier
                t["tier_why"] = ask(f"tier_why (enter keeps {t.get('tier_why')!r}): ") or t.get("tier_why", "")
                dump(t, TASK_KEYS)
            elif choice == "e":
                subprocess.run([os.environ.get("EDITOR", "vi"), str(t["_path"])])
                t = parse(t["_path"])
                if t["status"] != "proposed":
                    print(f"{t['id']} is now {t['status']}; moving on")
                    break
            elif choice == "s":
                tally["skipped"] += 1
                break
            elif choice == "q":
                print("  ".join(f"{k} {v}" for k, v in tally.items()))
                return
    print("\n" + "  ".join(f"{k} {v}" for k, v in tally.items()))


def reject(args):
    """HUMAN ONLY. A proposed task is dropped. A task in `review` goes back to `ready` with the
    reason in its Notes; its branch and worktree stay, so the next dispatch picks up from them
    and its worker is told why."""
    tasks, epics = load(ITEMS), load(EPICS)
    named = targets(args.ids, tasks, epics)
    todo = [t for t in named if t["status"] == "proposed"]
    back = [t for t in named if t["status"] == "review"]
    if not todo and not back:
        raise SystemExit("nothing in `proposed` or `review` among those ids")
    if back and not args.reason:
        raise SystemExit("rejecting work in `review` needs --reason: it's what the next worker is told to fix")
    for t in todo + back:
        print(line(t, tasks) + ("  (review -> ready)" if t in back else ""))
    what = " and ".join(x for x in (f"drop {len(todo)} proposed" if todo else "",
                                    f"send {len(back)} in review back to ready" if back else "") if x)
    if not human_gate(f"{what[0].upper() + what[1:]}?"):
        raise SystemExit("aborted")
    for t in todo:
        t["status"] = "dropped"
        if args.reason:
            t["_body"] = t["_body"].rstrip() + f"\n\nRejected {today()}: {args.reason}\n"
        dump(t, TASK_KEYS)
    for t in back:
        t["status"] = "ready"
        add_note(t, f"rejected by {approver()}: {args.reason}", who="review")
        dump(t, TASK_KEYS)
    print(", ".join(x for x in (f"dropped {len(todo)}" if todo else "",
                                f"sent {len(back)} back to ready" if back else "") if x))


def held_branch(t, epics):
    """(base, worktree, branch, companion worktrees) of a task dispatch held at `review`, as
    land() takes them. Raises GitError if its branch is gone."""
    branch = TASK_BRANCH + t["id"]
    if not branch_exists(branch):
        raise GitError(f"{t['id']}: no branch {branch} to land; `ergon reject {t['id']} --reason R` "
                       f"sends it back to ready")
    base = base_branch(t, epics)
    wt = Path(worktrees().get(branch) or WORKTREES / t["id"])
    pubs = []
    for c in COMPANIONS:
        if not companion_touch(t, c):
            continue
        if not branch_exists(branch, c["path"]):
            raise GitError(f"{t['id']}: no {c['name']} branch {branch}, though its touches: names {c['name']}")
        pubs.append((c, Path(worktrees(c["path"]).get(branch) or wt / c["rel"])))
    return base, wt, branch, pubs


def accept(args):
    """HUMAN ONLY: land tasks that dispatch held at `review` the way dispatch would have (companions
    first, then the task branch into its epic branch or the trunk), mark them done, and close an
    epic whose last task this was. A landing that fails marks the task blocked, as in dispatch."""
    tasks, epics = load(ITEMS), load(EPICS)
    todo = [t for t in targets(args.ids, tasks, epics, "review") if t["status"] == "review"]
    if not todo:
        raise SystemExit("nothing in `review` among those ids")
    for t in todo:
        print(line(t, tasks) + f"  [{TASK_BRANCH}{t['id']}]")
    if not human_gate(f"Accept and land these {len(todo)} task(s) as {approver()}?"):
        raise SystemExit("aborted")
    who, failed = f"{approver()} {today()}", 0
    for tid in [t["id"] for t in todo]:
        with state_lock():  # land() and finish_epic() expect it; also keeps dispatch off the trunk
            t, epics = load(ITEMS)[tid], load(EPICS)
            if t["status"] != "review":
                print(f"skip {tid}: now {t['status']}")
                continue
            try:
                base, wt, branch, pubs = held_branch(t, epics)
                stray = [f"{tree}: {p}" for tree in [wt] + [pub for _, pub in pubs]
                         if tree.exists() for p in dirty_paths(tree)]
            except GitError as err:
                print(f"{tid} not landed, still in review: {err}")
                failed += 1
                continue
            if stray:
                print(f"{tid} not landed, still in review: uncommitted changes would miss the merge; "
                      "commit or discard them, then accept again:\n  " + "\n  ".join(stray))
                failed += 1
                continue
            verdict, why = land(t, base, wt, branch, pubs, accepted=who)
            if verdict != "done":
                put_status(tid, "blocked", f"couldn't land it for {who}: {why}", who="accept")
                print(f"  blocked  {tid}  {why}")
                failed += 1
                continue
            put_status(tid, "done", f"accepted by {who}: {why}", who="accept")
            print(f"  done     {tid}  {why}")
            e = epics.get(t.get("epic"))
            if e and e["status"] == "open" and epic_complete(e["id"], load(ITEMS)):
                try:
                    finish_epic(e["id"])
                except GitError as err:
                    print(f"  {e['id']} is complete but not merged: {err}")
    if failed:
        sys.exit(1)


def close(args):
    """HUMAN ONLY: close approved tasks (ready / blocked / doing) whose work already landed
    elsewhere (--as done) or that no longer apply (--as dropped). Skips the epic-branch check
    on purpose: the work isn't happening on the branch. Proposed tasks go through `reject`."""
    tasks, epics = load(ITEMS), load(EPICS)
    todo = []
    for i in args.ids:
        t = tasks.get(i)
        if not t:
            raise SystemExit(f"no such task: {i}")
        if t["status"] == "proposed":
            raise SystemExit(f"{i} is proposed: use `ergon reject {i}` (nobody approved it)")
        if t["status"] in ("done", "dropped"):
            print(f"skip {i}: already {t['status']}")
            continue
        todo.append(t)
    if not todo:
        raise SystemExit("nothing to close")
    for t in todo:
        print(line(t, tasks))
    if not human_gate(f"Close these {len(todo)} task(s) as {args.as_}?"):
        raise SystemExit("aborted")
    for t in todo:
        t["status"] = args.as_
        t["_body"] = t["_body"].rstrip() + f"\n\nClosed {today()} as {args.as_}: {args.reason}\n"
        dump(t, TASK_KEYS)
    print(f"closed {len(todo)} as {args.as_}")
    for eid in sorted({t["epic"] for t in todo if t.get("epic") in epics}):
        if epics[eid]["status"] == "open" and epic_complete(eid, load(ITEMS)):
            try:
                finish_epic(eid)
            except GitError as err:
                print(f"{eid} is complete but not merged: {err}")


# ---------- audit ----------
# Read-only. Finds tasks the world has moved past: dependencies that can never clear, work that
# commits already cite, citations that no longer resolve, companion-repo paths a task's touches:
# doesn't name (the dispatcher needs that to give its worker a checkout), and nouns the project
# has retired (the `retired` file). It only reports and suggests commands; judgment calls (is
# this really done?) are the auditor's, see the plugin's task-audit skill.

CITE = re.compile(r"(?<![\w/.-])(\.?[\w.-]*/[\w./-]*\w|\.?[\w-]+\.(?:md|mdx|bu|example|lock))"
                  r"(?::(\d+)(?:[-–](\d+))?)?")
CITE_EXT = re.compile(r"\.(js|mjs|ts|tsx|jsx|rs|go|py|rb|sh|md|mdx|bu|ya?ml|json|toml|container|service|"
                      r"example|lock|txt|sql|conf|sol|c|h|cpp|java|kt|swift)$")


def retired_terms():
    """`term | gate | note` rows. gate is `-` (retired now) or a task ID (retired once it's done)."""
    rows = []
    if RETIRED_FILE and RETIRED_FILE.exists():
        for raw in RETIRED_FILE.read_text().splitlines():
            if raw.strip() and not raw.lstrip().startswith("#"):
                term, gate, note = (c.strip() for c in (raw.split("|") + ["", ""])[:3])
                rows.append((term, gate or "-", note))
    return rows


SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", ".next", "__pycache__"}


def walk(base):
    out = []
    for d, dirs, names in os.walk(base):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        out += [str(Path(d, n).relative_to(base)) for n in names]
    return out


def tracked_files():
    """Every file a citation can mean: {where: (root, files)}. "here" is what's tracked in this
    repo plus what's inside its nested repos; untracked leftovers in this checkout don't count.
    Each companion with a checkout is its own entry, keyed by its name."""
    here = set(git("ls-files", raw=True).splitlines())
    for g in gitlinks():
        if (REPO / g).is_dir():
            here |= {f"{g}/{f}" for f in walk(REPO / g)}
    index = {"here": (REPO, here)}
    for c in COMPANIONS:
        if has_checkout(c):
            index[c["name"]] = (c["path"], set(walk(c["path"])))
    return index


def resolve(path, index):
    """Where a cited path lives: (where, Path) or (None, None). A path that isn't repo-rooted
    (`server/src/main.rs`) matches when exactly one file ends with it."""
    p = path.strip("/")
    for where, (root, names) in index.items():
        if p in names or any(f.startswith(p + "/") for f in names):
            return where, root / p
    hits = [(where, root / f) for where, (root, names) in index.items() for f in names if f.endswith("/" + p)]
    if len(hits) == 1:
        return hits[0]
    if hits:  # ambiguous: it exists, but don't trust a line check against one of them
        return hits[0][0], None
    return None, None


def audit(args):
    tasks, epics = load(ITEMS), load(EPICS)
    index = tracked_files()
    open_ = {i: t for i, t in tasks.items() if t["status"] not in ("done", "dropped")
             and (not args.epic or t.get("epic") == args.epic)}
    cited = {}
    for row in git("log", "--all", "--format=%h %s", raw=True).splitlines():
        for tid in set(re.findall(r"\bT-\d{3,}\b", row)):
            cited.setdefault(tid, []).append(row)
    log_lines = (REPO / LOG).read_text().count("\n") + 1 if LOG and (REPO / LOG).exists() else 0
    retired = [(term, gate, note) for term, gate, note in retired_terms()
               if gate == "-" or tasks.get(gate, {}).get("status") == "done"]
    every_touch = tuple(k for c in COMPANIONS for k in c["touches"])
    found = {k: [] for k in ("deps", "cited", "touches", "cites", "retired", "source", "epics")}

    for i, t in sorted(open_.items()):
        deps = t.get("depends") or []
        state = {d: tasks.get(d, {}).get("status", "missing") for d in deps}
        dead = [d for d, s in state.items() if s in ("dropped", "missing")]
        waits = [d for d, s in state.items() if s not in ("done", "dropped", "missing")]
        if dead:
            found["deps"].append(f"{i} [{t['status']}] depends on {', '.join(f'{d} ({state[d]})' for d in dead)}"
                                 f": can never clear; edit depends:")
        if t["status"] == "ready" and waits:
            found["deps"].append(f"{i} [ready] waits on {', '.join(f'{d} ({state[d]})' for d in waits)}"
                                 + ("; a dep isn't approved yet" if any(state[d] == "proposed" for d in waits) else ""))
        if t["status"] == "proposed" and deps and not waits and not dead:
            found["deps"].append(f"{i} [proposed] every dependency is done: approvable now")
        if t["status"] == "blocked" and not waits and not dead:
            found["deps"].append(f"{i} [blocked] no open dependency: check its Notes, it may be done or ready")
        if i in cited:
            found["cited"].append(f"{i} [{t['status']}] cited by {len(cited[i])} commit(s), e.g. "
                                  f"{cited[i][0][:90]}")

        uses = set()  # companions whose files the task cites
        touches = [p.strip("/") for p in t.get("touches") or []]
        for p in touches:
            if " " in p or not CITE_EXT.search(p) or p.startswith(every_touch):
                continue  # a host, a map noun, free text, or a directory the task may create
            where, _ = resolve(p, index)
            if where not in (None, "here"):
                uses.add(where)
            elif where is None:
                found["touches"].append(f"{i} touches {p}: no such file here or in a companion checkout")
        body = t["_body"]
        for m in CITE.finditer(body):
            path, lo, hi = m.group(1), m.group(2), m.group(3)
            if not (CITE_EXT.search(path) or "/" in path) or "://" in body[max(0, m.start() - 8):m.end()]:
                continue
            where, real = resolve(path, index)
            if where is None:
                deliverable = any(path.startswith(p + "/") or path == p for p in touches)
                if CITE_EXT.search(path) and not path.startswith("/") and (lo or not deliverable):
                    found["cites"].append(f"{i} cites {path}{':' + lo if lo else ''}: "
                                          f"no such file here or in a companion checkout")
                continue
            if where != "here":
                uses.add(where)
            if lo and real and real.is_file():
                want = int(hi or lo)
                n = real.read_text(errors="replace").count("\n") + 1
                if want > n and "/" not in path:  # a bare name: any same-named file that's long enough
                    alts = [root / f for root, names in index.values() for f in names if f.endswith("/" + path)]
                    if any(a.is_file() and a.read_text(errors="replace").count("\n") + 1 >= want for a in alts):
                        continue
                if want > n:
                    found["cites"].append(f"{i} cites {path}:{lo}{'-' + hi if hi else ''}: file has {n} lines")
        for c in COMPANIONS:
            if c["name"] in uses and not companion_touch(t, c):
                found["touches"].append(f"{i} cites {c['name']} code but touches: doesn't name it; if it "
                                        f"changes that code, add {c['name']} or a dispatched worker stops blocked")
        text = (t.get("title", "") + "\n" + body).lower()
        for term, gate, note in retired:
            if re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", text) or \
                    any(term.lower() == x.lower() for x in t.get("touches") or []):
                found["retired"].append(f"{i} mentions {term}: {note}")
        src = t.get("source", "")
        m = re.match(rf"{re.escape(LOG)}:(\d+)", src) if LOG else None
        if m and int(m.group(1)) > log_lines:
            found["source"].append(f"{i} source {src}: {LOG} has {log_lines} lines")

    for e in sorted(epics.values(), key=lambda e: e["id"]):
        if e["status"] != "open" or (args.epic and e["id"] != args.epic):
            continue
        kids = [t for t in tasks.values() if t.get("epic") == e["id"]]
        live = [t for t in kids if t["status"] not in ("done", "dropped")]
        if not kids:
            found["epics"].append(f"{e['id']} has no tasks yet: break it down or drop it")
        elif not live:
            found["epics"].append(f"{e['id']} every task is done or dropped: `ergon finish {e['id']}`")
        elif all(t["status"] == "proposed" for t in live) and len(live) == len(kids):
            found["epics"].append(f"{e['id']} nothing approved in {len(kids)} task(s) since "
                                  f"{e.get('created', '?')}: still wanted?")

    titles = {"deps": "Dependencies", "cited": "Open, but commits cite it (done? close it)",
              "touches": "touches:", "cites": "Citations that don't resolve", "retired": "Retired nouns",
              "source": "source: lines", "epics": "Epics"}
    total = 0
    for k, rows in found.items():
        if rows:
            total += len(rows)
            print(f"\n{titles[k]} ({len(rows)})")
            for r in rows:
                print(f"  {r}")
    print(f"\n{len(open_)} open task(s) audited, {total} finding(s). Findings are leads, not verdicts: "
          "confirm each before closing or rewriting (task-audit skill).")


def tidy(args):
    """Put every file where it belongs. Only moves files, and never overwrites one."""
    n = 0

    def move(src, dst):
        nonlocal n
        if dst.exists():
            raise SystemExit(f"{dst} already exists; not overwriting it with {src}")
        dst.parent.mkdir(exist_ok=True)
        src.rename(dst)
        n += 1

    for p in sorted(EPICS.glob("*.md")):  # a flat epic file becomes <stem>/EPIC.md
        move(p, EPICS / p.stem / EPIC_FILE)
    epics = load(EPICS)
    for t in load(ITEMS).values():
        dst = task_home(t, epics) / t["_path"].name
        if dst != t["_path"]:
            move(t["_path"], dst)
    print(f"moved {n} file(s)")


def lint():
    tasks, epics = load(ITEMS), load(EPICS)
    errs = []
    for p in EPICS.glob("*.md"):
        errs.append(f"{p.name}: an epic belongs in its own folder; run `ergon tidy`")
    for kind in (ITEMS, EPICS):
        seen = {}
        for p in files(kind):
            i = parse(p)["id"]
            name = p.parent.name if kind == EPICS else p.name
            if not name.startswith(i + "-"):
                errs.append(f"{name}: name doesn't start with its id {i}")
            if i in seen:
                errs.append(f"{i}: duplicate id in {name} and {seen[i]}")
            seen[i] = name
    for t in tasks.values():
        where = t["id"]
        if t["_path"].parent != task_home(t, epics):
            errs.append(f"{where}: filed in {t['_path'].parent.name}/ but epic is "
                        f"{t.get('epic') or 'empty'}; run `ergon tidy`")
        for k in ("title", "tier", "status"):
            if not t.get(k):
                errs.append(f"{where}: missing {k}")
        if t.get("tier") not in TIERS:
            errs.append(f"{where}: tier {t.get('tier')!r} not in {TIERS}")
        if t.get("status") not in TASK_STATUSES:
            errs.append(f"{where}: status {t.get('status')!r} not in {TASK_STATUSES}")
        if t.get("status") in NEEDS_APPROVAL and not t.get("approved"):
            errs.append(f"{where}: status {t['status']} without a human `approved:` stamp")
        if t.get("status") == "review" and not branch_exists(TASK_BRANCH + t["id"]):
            errs.append(f"{where}: in review but branch {TASK_BRANCH}{t['id']} is gone, so there's nothing "
                        f"to accept; a human rejects it back to ready or closes it")
        if t.get("accept") and str(t["accept"]).lower() not in YES:
            errs.append(f"{where}: accept: {t['accept']!r} isn't `yes` (leave it out for no; config "
                        f"[[accept]] rules can't be opted out of)")
        if t.get("epic") and t["epic"] not in epics:
            errs.append(f"{where}: epic {t['epic']} doesn't exist")
        for d in t.get("depends") or []:
            if d not in tasks:
                errs.append(f"{where}: depends on missing {d}")
        if t.get("status") not in ("done", "dropped") and t.get("tier") in TIERS:
            for pr in PROTECT:
                hit = next(((x, p) for x in t.get("touches") or [] for p in pr["touches"]
                            if protect_hit(str(x), p)), None)
                if hit and TIERS.index(t["tier"]) < TIERS.index(pr["min_tier"]):
                    errs.append(f"{where}: touches {hit[0]} (protect {hit[1]}) needs tier "
                                f"{pr['min_tier']}+, is {t['tier']}" + (f": {pr['note']}" if pr["note"] else ""))
    for e in epics.values():
        if e.get("status") not in EPIC_STATUSES:
            errs.append(f"{e['id']}: status {e.get('status')!r} not in {EPIC_STATUSES}")
    # Map cards link gaps to tasks with a trailing "→ task T-012[, T-013]".
    for card in sorted(MAP.rglob("*.md")) if MAP and MAP.is_dir() else []:
        for n, text in enumerate(card.read_text().splitlines(), 1):
            for ref in re.findall(r"→ task ((?:T-\d+(?:, )?)+)", text):
                for tid in re.findall(r"T-\d+", ref):
                    where = f"{card.relative_to(REPO)}:{n}"
                    if tid not in tasks:
                        errs.append(f"{where}: links missing task {tid}")
                    elif tasks[tid]["status"] in ("done", "dropped"):
                        errs.append(f"{where}: links {tid}, which is {tasks[tid]['status']}; fix the card")
    return errs, f"{len(tasks)} tasks, {len(epics)} epics, {len(errs)} problem(s)"


def check(args):
    errs, summary = lint()
    for err in errs:
        print(err)
    print(summary)
    sys.exit(1 if errs else 0)


# ---------- Claude Code hooks ----------
# The plugin wires these (hooks/hooks.json); dispatch also injects the guard into every worker.
# Like human_gate, a speed bump: it stops an agent drifting into approving its own work, not one
# set on getting around it. Any crash fails open so a bug here never wedges a session, and a
# project with no .ergon.toml is left alone entirely.

# Shell writes: in-place editors, redirects (not to /dev/null or another fd), tee, mv/cp/rm.
SHELL_WRITE = re.compile(r"\bsed\s+(-\w+\s+)*-i|\bperl\s+-\w*i|(?<![0-9&>])>(?!\s*/dev/null|&)|"
                         r"\btee\b|\b(mv|cp|rm|truncate|install)\s|python[0-9.]*\s+-c|<<")
HUMAN_CMD = re.compile(r"\bergon(?:\.py)?[\"']?\s+(approve|reject|close|accept)\b|\breview\s+(-i|--interactive)\b")
GUARD_TOOLS = "Edit|Write|MultiEdit|Bash"
GIT_WRITE = re.compile(r"\bgit\b[^;&|]*\s(add|commit|checkout|switch|reset|restore|merge|rebase|"
                       r"cherry-pick|revert|stash|apply|am|pull|push|fetch|branch|tag|worktree|"
                       r"clean|update-ref|commit-tree|notes|gc|prune)\b")


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        key, _, val = line.partition(":")
        out[key.strip()] = re.sub(r"\s+#.*$", "", val).strip()
    return out


def shared_companion_reason(tool, inp):
    """Why a dispatched worker's tool call must not run because it changes a companion's shared
    checkout, or None. Each worker whose task touches a companion has its own worktree of it."""
    for c in COMPANIONS:
        why = (f"{c['path']} is the shared {c['name']} checkout; dispatched workers never change it. "
               f"If your task touches {c['name']}, its worktree is {c['rel']} inside your own "
               "worktree; if it has none, stop blocked")
        if tool == "Bash":
            cmd = inp.get("command", "")
            shared = re.compile(re.escape(str(c["path"])) + r"|(?:\.\./)+" + re.escape(c["rel"]) + r"\b")
            if shared.search(cmd) and (SHELL_WRITE.search(cmd) or GIT_WRITE.search(cmd)):
                return why
            continue
        path = inp.get("file_path", "")
        if not path:
            continue
        try:
            Path(path).resolve().relative_to(c["path"].resolve())
        except ValueError:
            continue
        return why
    return None


def guard_reason(tool, inp, worker=False):
    """Why this tool call must not run, or None."""
    if worker:
        why = shared_companion_reason(tool, inp)
        if why:
            return why
    if tool == "Bash":
        cmd = inp.get("command", "")
        if HUMAN_CMD.search(cmd):
            return ("approve / reject / close / accept / review -i are human-only; ask the user to run them "
                    "in their own terminal")
        if re.search(r"\bunset\b[^;&|]*\b(CLAUDECODE|AI_AGENT)\b|\benv\b[^;&|]*-u\s*(CLAUDECODE|AI_AGENT)|"
                     r"\b(CLAUDECODE|AI_AGENT)=", cmd):
            return "don't change CLAUDECODE / AI_AGENT: they're how ergon knows an agent is running"
        if TASK_DIR_IN_CMD.search(cmd) and SHELL_WRITE.search(cmd):
            return ("don't write task files from the shell: use `ergon new` / `ergon set` for state, "
                    "Edit for a task's body")
        return None
    path = inp.get("file_path", "")
    if not TASK_FILE.search(path):
        return None
    p = Path(path)
    before = p.read_text() if p.exists() else ""
    after = before
    if tool == "Write":
        after = inp.get("content", "")
    else:
        for e in inp.get("edits") or [inp]:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    fb, fa = frontmatter(before), frontmatter(after)
    if fb.get("approved", "") != fa.get("approved", ""):
        return "`approved:` is written by a human only (the one rule: `ergon guide`)"
    if fb.get("status", "") != fa.get("status", "") and not (not before and fa.get("status") in ("proposed", "open")):
        if fb.get("status") == "review":
            return "a task in review leaves it only by a human's `ergon accept` or `ergon reject`, run in their own terminal"
        return "change status with `ergon set ID status=…` (it enforces the allowed moves), not by editing the file"
    if fb.get("accept", "").lower() in YES and fa.get("accept", "").lower() not in YES:
        return "only a human takes `accept:` off a task (it holds the task's landing for their review)"
    return None


def hook(args):
    try:
        event = json.load(sys.stdin)
        try:
            configure(args.project or os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd"))
        except NotConfigured:
            sys.exit(0)  # not an ergon project
        if args.event == "guard":
            why = guard_reason(event.get("tool_name"), event.get("tool_input") or {}, args.worker)
            if why:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": f"ergon guard: {why}"}}))
        elif args.event == "stop" and not event.get("stop_hook_active"):
            errs, summary = lint()
            if errs:
                print(json.dumps({"decision": "block", "reason": (
                    "`ergon check` fails:\n" + "\n".join(errs[:20]) + f"\n{summary}\n"
                    "Fix what your change caused (e.g. a card's `→ task` link to a task you closed). "
                    "If it isn't yours, say so to the user and stop.")}))
    except Exception as err:  # fail open
        print(f"ergon hook error (ignored): {err}", file=sys.stderr)
    sys.exit(0)


# ---------- dispatch ----------
# Each ready task gets a worktree at <worktrees>/<ID> on branch task/<ID>, cut from its epic
# branch (created as a ref, never checked out here) or from the trunk for a task with no epic.
# A headless `claude -p --agent task-<tier>` works it and commits there. The dispatcher alone
# moves status: doing when it launches, then done once the branch merges into its base, or
# blocked (worktree and branch kept for you) on anything else, a merge conflict included.
# A task whose touches: names a companion repo also gets its own worktree of that repo's
# checkout, on a task/<ID> branch there too, which lands on the companion's local trunk first.
# Nothing is ever pushed.

WORKER_PROMPT = """You are dispatched on {id}.

Working tree: {wt} (a git worktree of this repo), on branch {branch}, cut from {base}.
{retry}{nested}{companions}{rules}
Work you find but don't do: end your final message with one line per item, before the RESULT line,
`FOUND: <title> — <why>`. The dispatcher files each as a proposed task.

The task file, read-only to you (it lives in the main checkout):

{text}
"""

COMPANION_OWN = """
The companion repo {name} is yours alone at {pub}: a git worktree of the shared checkout
{shared}, on branch {branch}, cut from its local {ctrunk}. Make and commit every change to it
there, never in {shared}. {ids}Push neither repo; the dispatcher merges {branch} into {name}'s
{ctrunk} and into {base}.
"""

COMPANION_NO_IDS = """Its commit messages never cite tracker IDs{policy}: your commit in {wt}, which
cites {id}, names the {name} commit. """

COMPANION_NONE = """
This task's touches: doesn't name {name}, so it has no checkout of it. You may read {shared},
but never change it: if the task needs a change there, stop blocked.
"""


def gitlinks():
    return [l.split("\t", 1)[1] for l in git("ls-files", "-s", raw=True).splitlines()
            if l.startswith("160000")]


def nested_touch(t, links):
    for p in (x.strip("/") for x in t.get("touches") or []):
        if any(p == g or p.startswith(g + "/") or g.startswith(p + "/") for g in links):
            return p
    return None


def companion_touch(t, c):
    """Does t's touches: name companion c (so its worker needs a checkout of it)?"""
    return any(p == k or p.startswith(k + "/")
               for p in (x.strip("/") for x in t.get("touches") or []) for k in c["touches"])


def has_checkout(c):
    return (c["path"] / ".git").exists()


def add_note(t, note, who="dispatch"):
    body = t["_body"].rstrip()
    t["_body"] = (body if "## Notes" in body else body + "\n\n## Notes") + f"\n- {today()} {who}: {note}\n"


def put_status(tid, status, note=None, who="dispatch"):
    t = load(ITEMS)[tid]
    t["status"] = status
    if note:
        add_note(t, note, who)
    dump(t, TASK_KEYS)


def worker_agents():
    """task-<tier> worker definitions, passed inline so a worktree cut from a base that predates
    them still gets them. A project's own .claude/agents/task-<tier>.md (main checkout) wins over
    the plugin's agents/task-<tier>.md."""
    out = {}
    for tier in WORKER_TIERS:
        path = next(p for p in (AGENTS / f"task-{tier}.md", PLUGIN_AGENTS / f"task-{tier}.md") if p.exists())
        m = parse(path)
        out[f"task-{tier}"] = {"description": m["description"], "model": m.get("model") or tier,
                               "prompt": m["_body"].strip()}
    return out


def worker_settings():
    cmd = f'python3 "{Path(__file__).resolve()}" hook guard --worker --project "{HOME}"'
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": GUARD_TOOLS, "hooks": [{"type": "command", "command": cmd}]}]}})


def worker_rules():
    return WORKER_RULES.read_text().strip() if WORKER_RULES and WORKER_RULES.exists() else ""


def base_branch(t, epics):
    e = epics.get(t.get("epic"))
    if not e:
        return MAIN
    b = epic_branch(e)
    if not branch_exists(b):
        git("branch", b, MAIN)
        print(f"started {e['id']} on branch {b} (cut from {MAIN}, not checked out)")
    return b


def prepare(t, epics):
    """Worktree + branch for t, reusing both if an earlier run left them, and the same in each
    companion t touches. Returns (base, wt, branch, pubs); pubs is [(companion, its worktree)]."""
    base, branch, wt = base_branch(t, epics), TASK_BRANCH + t["id"], WORKTREES / t["id"]
    if worktrees().get(branch):
        wt = Path(worktrees()[branch])
    elif branch_exists(branch):
        git("worktree", "add", str(wt), branch)
    else:
        git("worktree", "add", "-b", branch, str(wt), base)
    pubs = []
    for c in COMPANIONS:
        if not companion_touch(t, c):
            continue
        # Its own worktree of the shared checkout: never the checkout itself, never another
        # task's. Same object store and refs, so the dispatcher merges it without a fetch.
        pub = wt / c["rel"]
        held = worktrees(c["path"]).get(branch)
        if held and Path(held).resolve() != pub.resolve():
            raise GitError(f"{c['name']} branch {branch} is checked out at {held}; free it (`git -C "
                           f"{c['path']} worktree remove {held}`, or switch that checkout off it) and dispatch again")
        if not held:
            pub.parent.mkdir(parents=True, exist_ok=True)
            if branch_exists(branch, c["path"]):
                git("worktree", "add", str(pub), branch, cwd=c["path"])
            else:
                git("worktree", "add", "-b", branch, str(pub), c["trunk"], cwd=c["path"])
        pubs.append((c, pub))
    return base, wt, branch, pubs


KILLED = "ergon: killed"
PROCS, PROCS_LOCK, KILL_ALL = set(), threading.Lock(), threading.Event()


def kill_workers():
    """Second Ctrl-C: kill every running worker (and its process group) and refuse new ones."""
    KILL_ALL.set()
    with PROCS_LOCK:
        for p in list(PROCS):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass


def run_proc(cmd, cwd, timeout):
    """Run a worker in its own session (a terminal Ctrl-C doesn't reach it) so the dispatcher
    decides what an interrupt means. Returns (stdout, stderr); stderr starts with KILLED if
    kill_workers() ended it."""
    with PROCS_LOCK:
        if KILL_ALL.is_set():
            return "", KILLED
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        PROCS.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        err = f"timed out after {timeout // 60} min\n{err or ''}"
    finally:
        with PROCS_LOCK:
            PROCS.discard(p)
    if KILL_ALL.is_set() and p.returncode == -signal.SIGKILL and not err.startswith("timed out"):
        return out or "", KILLED
    return out or "", err or ""


def run_worker(t, base, wt, branch, pubs, args, agents, settings):
    """Run one headless worker. Returns (verdict, reason, run info); never raises."""
    retry = ""
    try:
        if git("rev-list", "--count", f"{base}..{branch}") != "0":
            retry = (f"An earlier attempt left commits on this branch (`git log {base}..HEAD`) and "
                     "notes in the task file. Pick up from there.\n")
        for c, pub in pubs:
            if git("rev-list", "--count", f"{c['trunk']}..{branch}", cwd=c["path"]) != "0":
                retry += (f"An earlier attempt left {c['name']} commits too (`git -C {pub} log "
                          f"{c['trunk']}..HEAD`).\n")
    except GitError:
        pass
    retry += review_retry(t)
    links = gitlinks()
    nested = (f"Nested repos ({', '.join(links)}) are empty in this worktree: if the task needs one, "
              "stop blocked.\n") if links else ""
    own = {c["name"]: pub for c, pub in pubs}
    companions = ""
    for c in COMPANIONS:
        if c["name"] in own:
            ids = COMPANION_NO_IDS.format(policy=f" ({c['policy']})" if c["policy"] else "", wt=wt,
                                          id=t["id"], name=c["name"]) if c["forbid_ids"] else ""
            companions += COMPANION_OWN.format(name=c["name"], pub=own[c["name"]], shared=c["path"],
                                               branch=branch, ctrunk=c["trunk"], ids=ids, base=base)
        else:
            companions += COMPANION_NONE.format(name=c["name"], shared=c["path"])
    rules = worker_rules()
    prompt = WORKER_PROMPT.format(id=t["id"], wt=wt, branch=branch, base=base, retry=retry,
                                  nested=nested, companions=companions,
                                  rules=f"\nThis project's worker rules:\n\n{rules}\n" if rules else "",
                                  text=t["_path"].read_text())
    cmd = ["claude", "-p", "--agents", json.dumps(agents), "--agent", f"task-{t['tier']}",
           "--permission-mode", args.permission_mode, "--settings", settings,
           "--output-format", "json"]
    if args.budget:
        cmd += ["--max-budget-usd", str(args.budget)]
    stem = f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}" + ("-" + t["tier"] if t.get("escalated") else "")
    log = RUNS / f"{stem}.json"
    out, err = run_proc(cmd + [prompt], wt, args.timeout * 60)
    log.write_text(out)
    if err.strip():
        (RUNS / f"{stem}.err").write_text(err)
    info = {"log": log.relative_to(HOME)}
    if err.startswith(KILLED):
        return "blocked", "killed by a second Ctrl-C", info
    try:
        d = json.loads(out)
    except ValueError:
        return "blocked", "worker output wasn't JSON" + (f" ({err.strip().splitlines()[0]})" if err.strip() else ""), info
    info.update(session=d.get("session_id"), cost=d.get("total_cost_usd") or 0)
    text = d.get("result") or ""
    if d.get("is_error"):
        return "blocked", f"worker errored ({d.get('subtype') or d.get('terminal_reason')}): {text[:200]}", info
    info["found"] = [(m[0].strip(" *`"), m[1].strip(" *`")) for m in
                     re.findall(r"^\W*FOUND:\s*(.*?)(?:\s+[—–]\s+(.*))?$", text, re.M) if m[0].strip(" *`")]
    found = re.findall(r"^\W*RESULT:\s*(done|blocked)\b[\s—:–-]*(.*)$", text, re.M)
    if not found:
        return "blocked", "worker ended without a RESULT line", info
    verdict, why = found[-1]
    info["asked"] = verdict == "blocked"  # the worker itself says what a human must do
    return verdict, why.strip(" *`") or "(no reason given)", info


def file_found(t, found, epics):
    """Create each FOUND line a worker emitted as a `proposed` task: same tier, epic and a
    source: of the task that found it. Never ready. Caller holds the state lock."""
    made = []
    for title, why in found:
        tid = next_id(ITEMS, "T")
        e = epics.get(t.get("epic"))
        body = TASK_BODY.replace("## What\n", f"## What\n{why or title}\n", 1)
        meta = {"id": tid, "title": title, "tier": t["tier"], "status": "proposed",
                "epic": t.get("epic") or "", "depends": [], "touches": [],
                "source": t["id"], "created": today(), "approved": "", "tier_why": "",
                "_path": task_home(t, epics) / f"{tid}-{slug(title)}.md", "_body": body}
        dump(meta, TASK_KEYS)
        made.append((tid, title))
    return made


def file_human(t, why, epics):
    """A worker reported `RESULT: blocked`: file a proposed human-tier task for what it asked
    for and make the blocked task depend on it. Caller holds the state lock."""
    tid = next_id(ITEMS, "T")
    title = why if len(why) <= 100 else why[:97].rstrip() + "..."
    body = TASK_BODY.replace("## What\n", f"## What\n{why}\n", 1)
    meta = {"id": tid, "title": title, "tier": "human", "status": "proposed",
            "epic": t.get("epic") or "", "depends": [], "touches": [],
            "source": t["id"], "created": today(), "approved": "", "tier_why": "",
            "_path": task_home(t, epics) / f"{tid}-{slug(title)}.md", "_body": body}
    dump(meta, TASK_KEYS)
    cur = load(ITEMS)[t["id"]]
    cur["depends"] = list(cur.get("depends") or []) + [tid]
    dump(cur, TASK_KEYS)
    return tid, title


def drop_worktree(wt, branch, repo=None):
    """Remove a landed task's worktree, then its branch. Returns what's left for you, or ''."""
    try:
        git("worktree", "remove", str(wt), cwd=repo)
        git("branch", "-D", branch, cwd=repo)  # merged: merge_into landed it
        return ""
    except GitError as err:
        return f"; left {wt} on {branch} for you to remove ({str(err)[:200]})"


VERIFY_TIMEOUT = 15 * 60  # seconds


def run_verify(t, wt):
    """Run the verify command (task `verify:` over config `verify`) in the task worktree.
    Returns None if it passes or none is set, else a reason; full output goes to <state>/runs/."""
    cmd = t.get("verify") or CFG.get("verify")
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        text_of = lambda b: (b.decode(errors="replace") if isinstance(b, bytes) else b) or ""
        out, code = f"{text_of(e.stdout)}{text_of(e.stderr)}\ntimed out after {VERIFY_TIMEOUT // 60} min\n", None
    if code == 0:
        return None
    RUNS.mkdir(parents=True, exist_ok=True)
    log = RUNS / f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}-verify.log"
    log.write_text(out)
    tail = " | ".join(l.strip() for l in out.strip().splitlines()[-5:])[-400:]
    how = "timed out" if code is None else f"failed (exit {code})"
    return f"verify `{cmd}` {how}: {tail} (full output {log.relative_to(HOME)})"


REVIEW_PROMPT = """You are reviewing {id} before it lands.

Working tree: {wt}, on branch {branch}, cut from {base}. A {tier}-tier worker says the task is done.
{companions}{rules}
The task file:

{text}

The diff, `git diff {base}...{branch}`{size}:

{diff}
"""
REVIEW_DIFF_MAX = 100_000  # characters inlined per diff; past that the reviewer runs git diff itself
REVIEW_LINE = re.compile(r"^\W*REVIEW:\s*(pass|fail)\b[\s—:–-]*(.*)$")


def reviewer_tier(tier):
    """Who reviews a tier's work: one tier above, capped at opus (opus reviews opus)."""
    return WORKER_TIERS[min(WORKER_TIERS.index(tier) + 1, len(WORKER_TIERS) - 1)]


def review_agent():
    """The task-review definition (a project's .claude/agents/task-review.md wins over the
    plugin's), minus a model: run_review sets that from reviewer_tier()."""
    path = next(p for p in (AGENTS / "task-review.md", PLUGIN_AGENTS / "task-review.md") if p.exists())
    m = parse(path)
    agent = {"description": m["description"], "prompt": m["_body"].strip()}
    tools = m.get("tools")
    if tools:
        agent["tools"] = tools if isinstance(tools, list) else [x.strip() for x in tools.split(",") if x.strip()]
    return agent


def review_diff(base, branch, repo=None):
    """(diff text for the prompt, size note). Past REVIEW_DIFF_MAX the prompt gets the stat only."""
    diff = git("diff", f"{base}...{branch}", raw=True, cwd=repo)
    if len(diff) <= REVIEW_DIFF_MAX:
        return diff or "(empty)", ""
    stat = git("diff", "--stat", f"{base}...{branch}", raw=True, cwd=repo)
    return stat, f" (too long to inline at {len(diff)} characters: its --stat follows; run it yourself)"


def tree_state(trees, branch, repos):
    """What a read-only reviewer must leave as it found it: each tree's HEAD and tracked changes,
    and where `branch` points in each repo."""
    return ([(git("rev-parse", "HEAD", cwd=x), dirty_paths(x)) for x in trees]
            + [git("rev-parse", branch, cwd=r) for r in repos])


def run_review(t, tier, base, wt, branch, pubs, args, agent, settings):
    """Run the reviewer on a finished task branch. Returns (failure reason or None, cost,
    reviewer tier). Only a last line of `REVIEW: pass` passes: no such line, a fail, output that
    isn't JSON, an error, a kill, a timeout, or a reviewer that changed the branch or worktree
    all block the landing."""
    who = reviewer_tier(tier)
    trees, repos = [wt] + [pub for _, pub in pubs], [None] + [c["path"] for c, _ in pubs]
    before = tree_state(trees, branch, repos)
    diff, size = review_diff(base, branch)
    companions = ""
    for c, pub in pubs:
        cdiff, csize = review_diff(c["trunk"], branch, c["path"])
        companions += (f"\nCompanion repo {c['name']}: worktree {pub}, branch {branch} cut from its "
                       f"{c['trunk']}. Its diff, `git -C {pub} diff {c['trunk']}...{branch}`{csize}:\n\n{cdiff}\n")
    rules = worker_rules()
    prompt = REVIEW_PROMPT.format(id=t["id"], wt=wt, branch=branch, base=base, tier=tier, companions=companions,
                                  rules=f"\nThe worker followed these project rules:\n\n{rules}\n" if rules else "",
                                  text=t["_path"].read_text(), diff=diff, size=size)
    cmd = ["claude", "-p", "--agents", json.dumps({"task-review": dict(agent, model=who)}),
           "--agent", "task-review", "--permission-mode", args.permission_mode, "--settings", settings,
           "--output-format", "json"]
    if args.budget:
        cmd += ["--max-budget-usd", str(args.budget)]
    stem = f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}-review"
    log = RUNS / f"{stem}.json"
    out, err = run_proc(cmd + [prompt], wt, args.timeout * 60)
    log.write_text(out)
    if err.strip():
        (RUNS / f"{stem}.err").write_text(err)
    fail = lambda why, cost=0: (f"review ({who}) failed: {why} (review log {log.relative_to(HOME)})", cost, who)
    if err.startswith(KILLED):
        return fail("killed by a second Ctrl-C")
    try:
        d = json.loads(out)
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        return fail("reviewer output wasn't JSON" + (f" ({err.strip().splitlines()[0]})" if err.strip() else ""))
    cost = d.get("total_cost_usd") or 0
    try:
        changed = tree_state(trees, branch, repos) != before
    except GitError:
        changed = True
    if changed:
        return fail("the reviewer changed the branch or worktree; it must stay read-only", cost)
    text = d.get("result") or ""
    if d.get("is_error"):
        return fail(f"reviewer errored ({d.get('subtype') or d.get('terminal_reason')}): {text[:200]}", cost)
    last = next((l for l in reversed(text.strip().splitlines()) if l.strip()), "")
    m = REVIEW_LINE.match(last.strip(" *`"))
    if not m:
        return fail("reviewer didn't end with a `REVIEW: pass` or `REVIEW: fail` line", cost)
    if m[1] == "pass":
        return None, cost, who
    return fail(m[2].strip(" *`") or "(no reason given)", cost)


def review_retry(t):
    """Retry-prompt lines for a task whose last attempt the reviewer vetoed or a human rejected
    in review, or ''."""
    note = last_note(t)
    human = re.search(r"\breview: rejected by (.*)", note)
    if human:
        return (f"A human reviewed the last attempt's branch and sent it back, not merged: rejected by {human[1]}\n"
                "Fix what they raised on this branch; it waits for their acceptance again before it lands.\n")
    m = re.search(r"\bdispatch: (review \(\w+\) failed: .*)", note)
    if not m:
        return ""
    log = re.search(r"\(review log (\S+?\.json)\)", m[1])
    return (f"The reviewer vetoed the last attempt: {m[1]}\n"
            + (f"Its full review is in {HOME / log[1]}.\n" if log else "")
            + "Fix what it raised on this branch; it reviews again before this lands.\n")


def kept_note(wt, branch, pubs):
    return f"Branch {branch} and worktree {wt} kept" + "".join(f" ({c['name']} worktree {pub})" for c, pub in pubs)


def gate(t, tier, base, wt, branch, pubs, args, reviewer, settings):
    """After a worker says done, before the merge lock: its done claim holds, verify passes,
    then the reviewer passes if `review` names the task's tier or `tier`, the one that finished
    (after --escalate the lower tier's commits are on the branch too). The reviewer is one tier
    above `tier`. Touches
    only the task's own worktrees and branch, so a long verify or review doesn't hold the state
    lock. Returns (failure reason or None, review cost)."""
    for tree in [wt] + [pub for _, pub in pubs]:
        if dirty_paths(tree):
            return f"worker said done but left uncommitted changes in {tree}", 0
    msgs = git("log", "--format=%B%x1e", f"{base}..{branch}", raw=True)
    if not re.search(rf"\b{t['id']}\b", msgs):
        return f"worker said done but no commit on {branch} cites {t['id']}", 0
    kept = kept_note(wt, branch, pubs)
    failed = run_verify(t, wt)
    if failed:
        return f"{failed}. {kept}", 0
    if t["tier"] not in REVIEW and tier not in REVIEW:
        return None, 0
    failed, cost, who = run_review(t, tier, base, wt, branch, pubs, args, reviewer, settings)
    print(f"  review   {t['id']}  {who}: " + ("pass" if not failed else "fail"), flush=True)
    return (f"{failed}. {kept}" if failed else None), cost


def land(t, base, wt, branch, pubs=(), accepted=None):
    """After gate() passes: merge (companions first), clean up. Returns (verdict, reason).
    Nothing merges unless every merge would go through cleanly. Caller holds the state lock.
    `accepted` ("<name> <date>", from `accept`) forces a merge commit into base that records
    who accepted it: task files are local, so history is the only lasting record."""
    kept = kept_note(wt, branch, pubs)
    for c, pub in pubs:
        if c["forbid_ids"]:
            leak = TRACKER_ID.search(git("log", "--format=%B", f"{c['trunk']}..{branch}", raw=True, cwd=c["path"]))
            if leak:
                return "blocked", (f"a {c['name']} commit on {branch} cites {leak.group()}; tracker IDs stay out "
                                   f"of {c['name']}, so reword it in {pub}. {kept}")
        try:
            merge_check(c["trunk"], branch, c["path"])
        except GitError as err:
            return "blocked", f"{c['name']}: {err}. {kept}: merge by hand"
    if pubs:
        try:
            merge_check(base, branch)
        except GitError as err:
            return "blocked", f"{err}. {kept}: merge by hand"
    landed = []
    for c, pub in pubs:
        # A fast-forward writes no message at all; a merge commit cites no tracker ID if forbidden.
        msg = f"Merge {TRACKER_ID.sub('', t['title']).strip()}" if c["forbid_ids"] else f"Merge {t['id']}: {t['title']}"
        try:
            merge_into(c["trunk"], branch, msg, repo=c["path"])
        except GitError as err:
            return "blocked", (f"{c['name']}: {err}. {kept}: merge by hand"
                               + (f". Already landed: {', '.join(landed)}" if landed else ""))
        landed.append(f"{c['name']} {c['trunk']}")
    try:
        merge_into(base, branch, f"Merge {t['id']}: {t['title']}"
                   + (f"\n\nAccepted by {accepted}" if accepted else ""), no_ff=bool(accepted))
    except GitError as err:
        return "blocked", (f"{err}. {kept}: merge by hand"
                           + (f". Already landed: {', '.join(landed)}" if landed else ""))
    # A companion worktree sits inside the task's own, and removing that would take it along
    # and leave the companion a stale entry: remove those first.
    left = ""
    for c, pub in pubs:
        left += drop_worktree(pub, branch, c["path"])
    left += drop_worktree(wt, branch) if not left else f"; left {wt} too"
    return "done", f"merged into {base}" + "".join(f" and {c['name']} {c['trunk']}" for c, _ in pubs) + left


def dispatch(args):
    if args.watch and not args.budget_total:
        raise SystemExit("--watch needs --budget-total (a cap on spend, so an unattended watch can't run away)")
    if args.watch and args.dry_run:
        raise SystemExit("--watch and --dry-run don't combine")
    if args.max is None:
        args.max = float("inf") if args.watch else 6
    if REPO != HOME and not args.dry_run:  # a dry run only reads, so any checkout will do
        raise SystemExit(f"run dispatch from the main checkout ({HOME})")
    if not shutil.which("claude") and not args.dry_run:
        raise SystemExit("no `claude` on PATH")
    if not args.dry_run:
        RUNS.mkdir(exist_ok=True)
        WORKTREES.mkdir(parents=True, exist_ok=True)
        git("worktree", "prune")
        for c in COMPANIONS:  # forget companion worktrees whose directory is gone (a removed task worktree)
            if has_checkout(c):
                git("worktree", "prune", cwd=c["path"])
    links, epics = gitlinks(), load(EPICS)
    ids = set(args.ids.split(",")) if args.ids else None
    agents, settings = worker_agents(), worker_settings()
    reviewer = review_agent() if REVIEW else None
    tried, skipped, results = set(), {}, []
    cap = args.budget_total * args.budget_pct / 100 if args.budget_total else None
    capped = stopped = False

    def spent():
        return sum(r[3].get("cost", 0) for r in results)
    every_touch = {k for c in COMPANIONS for k in c["touches"]}

    def eligible(tasks):
        epics.update(load(EPICS))  # another run or session may have closed one
        for t in sorted(tasks.values(), key=lambda t: (TIERS.index(t["tier"]), t["id"])):
            if (t["id"] in tried or t["status"] != "ready" or not deps_done(t, tasks)
                    or (ids and t["id"] not in ids) or (args.tier and t["tier"] != args.tier)
                    or (args.epic and t.get("epic") != args.epic)):
                continue
            if t["tier"] == "human":
                continue
            nested = nested_touch(t, links)
            if nested:
                skipped[t["id"]] = f"touches nested repo {nested}: work it interactively"
                continue
            missing = [c for c in COMPANIONS if companion_touch(t, c) and not has_checkout(c)]
            if missing:
                skipped[t["id"]] = (f"touches {missing[0]['name']} but there's no checkout at "
                                    f"{missing[0]['path']}: clone the repo there")
                continue
            if t.get("epic") and epics.get(t["epic"], {}).get("status") != "open":
                skipped[t["id"]] = f"epic {t['epic']} isn't open"
                continue
            yield t

    def one(t, base, wt, branch, pubs):
        verdict, why, info = run_worker(t, base, wt, branch, pubs, args, agents, settings)
        up = None
        if (args.escalate and verdict == "blocked" and info.get("asked")
                and t["tier"] in WORKER_TIERS[:-1]):
            # the worker itself reported blocked: retry once a tier up on the same branch
            up = WORKER_TIERS[WORKER_TIERS.index(t["tier"]) + 1]
            print(f"  escalate {t['id']}  {t['tier']}→{up}: {why}")
            first, why1 = t["tier"], why
            v2, why, info2 = run_worker(dict(t, tier=up, escalated=True), base, wt, branch, pubs, args, agents, settings)
            info2["cost"] = info.get("cost", 0) + info2.get("cost", 0)
            info2["found"] = (info.get("found") or []) + (info2.get("found") or [])
            verdict, info = v2, info2
        if verdict == "done":
            try:
                failed, rcost = gate(t, up or t["tier"], base, wt, branch, pubs, args, reviewer, settings)
            except GitError as err:
                failed, rcost = f"{err}. {kept_note(wt, branch, pubs)}", 0
            info["cost"] = info.get("cost", 0) + rcost
            info["review_cost"] = rcost
            if failed:
                verdict, why = "blocked", failed
        with state_lock():  # also serializes every merge into a companion's trunk
            # `t` is the task as it was at launch, so nothing the worker did to its file since
            # (dropping `accept:`, retiering) lets the branch skip a human's acceptance.
            if verdict == "done" and needs_accept(t, *([up] if up else [])):
                verdict, why = "review", (f"waits for a human: `ergon accept {t['id']}` lands it, `ergon reject "
                                          f"{t['id']} --reason R` sends it back. {kept_note(wt, branch, pubs)}")
            elif verdict == "done":
                verdict, why = land(t, base, wt, branch, pubs)
            if verdict in ("done", "review"):
                put_status(t["id"], verdict, "; ".join(x for x in (
                    f"escalated {first}→{up}: {why1}" if up else "", why if verdict == "review" else "") if x) or None)
                if up:
                    t2 = load(ITEMS)[t["id"]]
                    t2["tier"] = up
                    dump(t2, TASK_KEYS)
                print(f"  {verdict:<8} {t['id']}  {why}")
                e = epics.get(t.get("epic"))
                if verdict == "done" and e and epic_complete(e["id"], load(ITEMS)):
                    try:
                        finish_epic(e["id"])
                    except GitError as err:
                        print(f"  {e['id']} is complete but not merged: {err}")
            else:
                put_status(t["id"], "blocked", f"{why} (log {info['log']}"
                           + (f", `claude --resume {info['session']}` in {wt}" if info.get("session") else "") + ")")
                print(f"  blocked  {t['id']}  {why}")
            for fid, title in file_found(t, info.get("found") or [], epics):
                print(f"  proposed {fid}  (found by {t['id']})  {title}")
                info.setdefault("filed", []).append(fid)
            if verdict == "blocked" and info.get("asked"):
                hid, title = file_human(t, why, epics)
                print(f"  proposed {hid}  (human, {t['id']} now depends on it)  {title}")
        return t, verdict, why, info

    def shared(a, b):
        """Paths both tasks name. A companion itself isn't shared: each task gets its own
        worktree of it, so only paths inside it that both name count."""
        return sorted((set(a.get("touches") or []) & set(b.get("touches") or [])) - every_touch)

    def also(t):
        return "".join(f" + {c['name']} {c['trunk']}" for c in COMPANIONS if companion_touch(t, c))

    if args.dry_run:
        tasks = load(ITEMS)
        plan = list(eligible(tasks))
        for t in plan:
            e = epics.get(t.get("epic"))
            print(f"  would run  {t['id']}  {t['tier']:<6}  on {epic_branch(e) if e else MAIN}"
                  + also(t) + (f", reviewed by {reviewer_tier(t['tier'])}" if t["tier"] in REVIEW else "")
                  + (", held for your accept" if needs_accept(t) else "") + f"  {t['title']}")
        if not plan:
            print("nothing to dispatch")
        if cap is not None:
            n = min(len(plan), args.max)
            print(f"  cap        ${cap:.2f} total ({args.budget_pct:g}% of ${args.budget_total:.2f}): "
                  "no new task launches once finished workers' cost reaches it"
                  + (f"; estimated at most ${n * args.budget:.2f} for {n} task(s) at --budget ${args.budget:g}"
                     if args.budget else "; no --budget, so no estimate"))
        # Shared touches: whichever lands second (task into epic, or epic into trunk) may conflict,
        # so a task sharing a path with an earlier one is held until that one lands.
        for i, a in enumerate(plan):
            for b in plan[:i]:
                if shared(a, b):
                    print(f"  held       {a['id']} behind {b['id']}: both touch {', '.join(shared(a, b))}")
                    break
        for a in plan:
            for b in (t for t in tasks.values() if t["status"] in ("doing", "review")):
                if shared(a, b):
                    print(f"  overlap    {a['id']} and {b['id']} ({b['status']}) both touch {', '.join(shared(a, b))}")
    else:
        running, launched, active = {}, 0, {}  # active: running task id -> task
        stops, tick = [], 0.25
        stop_evt = threading.Event()

        def on_sigint(signum, frame):
            stops.append(1)
            if len(stops) == 1:
                print("\n  interrupt: no new launches; waiting for running workers "
                      "(Ctrl-C again kills them and marks them blocked)", flush=True)
            else:
                print("\n  interrupt: killing running workers", flush=True)
                kill_workers()
            stop_evt.set()
        old_handler = signal.signal(signal.SIGINT, on_sigint)
        next_scan, scan = 0.0, True
        try:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                while True:
                    stopping = bool(stops)
                    if scan and not stopping:
                        # the lock is held only for the scan, so `approve` works between scans
                        with state_lock():
                            if cap is not None and spent() >= cap:
                                capped = True
                            queue = [] if launched >= args.max or capped else list(eligible(load(ITEMS)))
                            while queue and len(running) < args.jobs and launched < args.max:
                                t = queue.pop(0)
                                if any(shared(t, o) for o in active.values()):
                                    continue  # held: stays eligible, retried when a running task lands
                                tried.add(t["id"])
                                try:
                                    base, wt, branch, pubs = prepare(t, epics)
                                except GitError as err:
                                    skipped[t["id"]] = f"couldn't make a worktree: {err}"
                                    continue
                                put_status(t["id"], "doing")
                                print(f"  doing    {t['id']}  {t['tier']:<6}  {branch} from {base}"
                                      + also(t) + f"  {t['title']}")
                                running[pool.submit(one, t, base, wt, branch, pubs)] = t["id"]
                                active[t["id"]] = t
                                launched += 1
                        next_scan = time.monotonic() + args.interval
                    scan = False
                    over = stopping or capped or launched >= args.max
                    if not running and (not args.watch or over):
                        break
                    if running:
                        finished, _ = wait(running, timeout=tick, return_when=FIRST_COMPLETED)
                    else:
                        stop_evt.wait(tick)
                        finished = ()
                    for f in finished:
                        active.pop(running.pop(f), None)
                        results.append(f.result())
                        scan = True
                    if args.watch and time.monotonic() >= next_scan:
                        scan = True
        finally:
            signal.signal(signal.SIGINT, old_handler)
        stopped = bool(stops)
    for tid, why in sorted(skipped.items()):
        print(f"  skipped  {tid}  {why}")
    if results:
        cost = sum(r[3].get("cost", 0) for r in results)
        done = sum(r[1] == "done" for r in results)
        rcost = sum(r[3].get("review_cost", 0) for r in results)
        held = sum(r[1] == "review" for r in results)
        print(f"\n{done}/{len(results)} done" + (f", {held} awaiting your accept" if held else "")
              + f", ${cost:.2f}" + (f" (reviews ${rcost:.2f} of it)" if rcost else "")
              + f". Logs in {RUNS.relative_to(HOME)}/")
        filed = [f for r in results for f in r[3].get("filed", [])]
        if filed:
            print(f"{len(filed)} proposed from workers' FOUND lines: {', '.join(filed)}")
    if stopped:
        print("\ninterrupted: stopped launching" + ("; killed running workers" if KILL_ALL.is_set() else ""))
    if capped:
        left = [t["id"] for t in eligible(load(ITEMS))]
        mean = spent() / len(results) if results else 0
        print(f"\nbudget cap ${cap:.2f} reached (spent ${spent():.2f}): stopped launching. "
              f"Not launched: {', '.join(left) or 'none'}"
              + (f". Estimated ~${mean * len(left):.2f} more to run them (mean ${mean:.2f}/task)" if left else ""))
    tasks = load(ITEMS)
    yours = [t for t in tasks.values() if t["tier"] == "human" and t["status"] == "ready" and deps_done(t, tasks)]
    if yours:
        print(f"\nREADY FOR YOU ({len(yours)} human-tier)")
        for t in sorted(yours, key=lambda t: t["id"]):
            print(line(t, tasks))
    held = [t for t in tasks.values() if t["status"] == "review"]
    if held:
        print(f"\nAWAITING YOUR ACCEPT ({len(held)}): `ergon accept <ID>` lands one, "
              "`ergon reject <ID> --reason R` sends it back")
        for t in sorted(held, key=lambda t: t["id"]):
            print(line(t, tasks))


# ---------- setup ----------

GITIGNORE = ["{state}/items/", "{state}/epics/", "{state}/runs/", "{worktrees}/"]


def init(args):
    """Write .ergon.toml (if missing), the state folders, and the .gitignore lines."""
    if REPO != HOME:
        raise SystemExit(f"run init from the main checkout ({HOME})")
    path = HOME / CONFIG_FILE
    if path.exists():
        print(f"{CONFIG_FILE} already exists; leaving it")
    else:
        path.write_text(CONFIG_TEMPLATE)
        print(f"wrote {CONFIG_FILE}")
    configure(HOME)
    for d in (ITEMS, EPICS):
        d.mkdir(parents=True, exist_ok=True)
    gi = HOME / ".gitignore"
    have = gi.read_text().splitlines() if gi.exists() else []
    want = [g.format(state=STATE.relative_to(HOME).as_posix(), worktrees=WORKTREES.relative_to(HOME).as_posix())
            for g in GITIGNORE]
    add = [g for g in want if g not in have]
    if add:
        with gi.open("a") as f:
            f.write(("\n" if have and have[-1].strip() else "")
                    + "# ergon: task state is local; commits that cite task IDs are the record\n"
                    + "\n".join(add) + "\n")
        print(f"added to .gitignore: {' '.join(add)}")
    print("next: enable the ergon plugin for this project (README), then `ergon new epic \"...\"`")


RUN_LOG = re.compile(r"^(?P<id>[A-Z]+-\d+)-(?P<date>\d{4})(?P<m>\d{2})(?P<d>\d{2})-\d{6}(?:-\w+)?\.json$")


def aggregate_stats(runs_dir, tasks, since=None, epic=None):
    """Per-tier numbers from the dispatch logs in runs_dir, joined with tasks (id -> meta).
    since: 'YYYY-MM-DD' (runs started that day or later); epic: only that epic's tasks.
    Returns {"tiers": {tier: {runs, done, blocked, timeouts, cost}}, "repeat": {id: blocked
    count} for tasks blocked more than once, "skipped": logs that were unreadable, not JSON
    (and not a timeout) or whose task is gone}. A reviewer's log (<ID>-<time>-review.json) adds
    its cost to the task's tier but isn't a run. Never raises on a bad log."""
    tiers, per_task, skipped = {}, {}, 0
    for log in sorted(Path(runs_dir).glob("*.json")):
        m = RUN_LOG.match(log.name)
        if not m:
            skipped += 1
            continue
        t = tasks.get(m["id"])
        if t is None:
            skipped += 1
            continue
        if epic and t.get("epic") != epic:
            continue
        if since and f"{m['date']}-{m['m']}-{m['d']}" < since:
            continue
        tier_row = lambda: tiers.setdefault(t.get("tier") or "?", {"runs": 0, "done": 0, "blocked": 0,
                                                                "timeouts": 0, "cost": 0.0})
        if log.stem.endswith("-review"):
            try:
                d = json.loads(log.read_text())
                tier_row()["cost"] += float(d.get("total_cost_usd") or 0) if isinstance(d, dict) else 0
            except (OSError, ValueError, TypeError):
                pass
            continue
        err = log.with_suffix(".err")
        try:
            timed_out = err.exists() and "timed out after" in err.read_text(errors="replace")
        except OSError:
            timed_out = False
        try:
            d = json.loads(log.read_text())
            if not isinstance(d, dict):
                raise ValueError
        except (OSError, ValueError):
            if not timed_out:
                skipped += 1
                continue
            d = {}
        text = d.get("result") or ""
        found = re.findall(r"^\W*RESULT:\s*(done|blocked)\b", text, re.M)
        verdict = "blocked" if d.get("is_error") or not found else found[-1]
        row = tier_row()
        row["runs"] += 1
        row[verdict] += 1
        row["timeouts"] += timed_out
        row["cost"] += float(d.get("total_cost_usd") or 0)
        if verdict == "blocked":
            per_task[t["id"]] = per_task.get(t["id"], 0) + 1
    return {"tiers": tiers, "skipped": skipped,
            "repeat": {k: v for k, v in sorted(per_task.items()) if v > 1}}


def stats(args):
    tasks = load(ITEMS)
    r = aggregate_stats(RUNS, tasks, since=args.since, epic=args.epic)
    print(f"{'tier':<8}{'runs':>5}{'done':>6}{'blocked':>8}{'block%':>8}{'cost':>10}{'mean':>9}{'timeouts':>9}")
    for tier in [*TIERS, *(k for k in r["tiers"] if k not in TIERS)]:
        row = r["tiers"].get(tier)
        if not row:
            continue
        n = row["runs"] or 1  # only reviews: their worker run fell outside --since
        print(f"{tier:<8}{row['runs']:>5}{row['done']:>6}{row['blocked']:>8}{100 * row['blocked'] / n:>7.0f}%"
              f"{'$%.2f' % row['cost']:>10}{'$%.2f' % (row['cost'] / n):>9}{row['timeouts']:>9}")
    if not r["tiers"]:
        print("(no runs)")
    if r["repeat"]:
        print("\nblocked more than once:")
        for tid, n in r["repeat"].items():
            print(f"  {tid} x{n}  {tasks[tid].get('title', '')}")
    if r["skipped"]:
        print(f"\nskipped {r['skipped']} unreadable or unmatched log(s)")


def last_note(t):
    """The last bullet under the task's ## Notes, or ''."""
    _, _, notes = t["_body"].partition("## Notes")
    bullets = [l[2:].strip() for l in notes.splitlines() if l.startswith("- ")]
    return bullets[-1] if bullets else ""


def report(args):
    tasks = load(ITEMS)
    mark = STATE / ".report-last"
    since = args.since
    if since == "last":
        since = mark.read_text().strip() if mark.exists() else None
    cited = {}
    try:
        rows = git("log", "--all", "--format=%h %s", *([f"--since={since}"] if since else []), raw=True)
    except GitError:
        rows = ""
    for row in rows.splitlines():
        for tid in dict.fromkeys(re.findall(r"\bT-\d{3,}\b", row)):
            cited.setdefault(tid, []).append(row)
    width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    clip = lambda text: text if len(text) <= width else text[:width - 1] + "…"
    out = []

    def section(title, lines):
        if lines:
            out.append(f"{title}{' (since ' + since + ')' if since and title == 'Landed' else ''}")
            out.extend(clip(l) for l in lines)
            out.append("")

    landed = []
    for i, t in sorted(tasks.items()):
        if t["status"] == "done" and (not since or i in cited):
            landed.append(f"  {i}  {t['title']}")
            landed.extend(f"      {c}" for c in cited.get(i, [])[:3])
    section("Landed", landed)
    section("Blocked", [f"  {i}  {t['title']}" + (f"\n      {last_note(t)}" if last_note(t) else "")
                        for i, t in sorted(tasks.items()) if t["status"] == "blocked"])
    section("Awaiting acceptance", [f"  {i}  {t['title']}  (`ergon accept {i}` or `ergon reject {i} --reason R`)"
                                    for i, t in sorted(tasks.items()) if t["status"] == "review"])
    out[:] = [l for x in out for l in x.split("\n")]
    groups = {}
    for t in tasks.values():
        if t["status"] == "proposed":
            key = (t.get("epic") or "-", t.get("tier") or "?")
            groups[key] = groups.get(key, 0) + 1
    section("Awaiting approval", [f"  {sum(groups.values())} proposed"]
            + [f"    {e:<8} {tier:<7} {n}" for (e, tier), n in sorted(groups.items())] if groups else [])
    section("Human tasks ready", [f"  {line(t, tasks).strip()}" for t in tasks.values()
                                  if t["status"] == "ready" and t.get("tier") == "human"])
    r = aggregate_stats(RUNS, tasks, since=since[:10] if since else None)
    runs = sum(v["runs"] for v in r["tiers"].values())
    if runs:
        section("Spend", [f"  ${sum(v['cost'] for v in r['tiers'].values()):.2f} over {runs} run(s)"]
                + [f"    {k:<7} ${v['cost']:.2f}  {v['runs']} run(s)" for k, v in r["tiers"].items()])
    print("\n".join(out).rstrip() or "(nothing to report)")
    mark.write_text(datetime.datetime.now().isoformat(timespec="seconds") + "\n")


def guide(args):
    text = GUIDE.read_text()
    print(re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip())


def rules(args):
    text = worker_rules()
    print(text or f"(no worker_rules in {CONFIG_FILE}: follow the project's CLAUDE.md / AGENTS.md)")


def main():
    ap = argparse.ArgumentParser(prog="ergon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("init", "guide", "rules"):
        sub.add_parser(name)
    b = sub.add_parser("board")
    b.add_argument("--tier", choices=TIERS)
    b.add_argument("--epic")
    b.add_argument("--all", action="store_true", help="include done and dropped")
    n = sub.add_parser("next")
    n.add_argument("--tier", choices=TIERS)
    rv = sub.add_parser("review")
    rv.add_argument("-i", "--interactive", action="store_true", help="HUMAN ONLY: decide task by task")
    rv.add_argument("--epic")
    rv.add_argument("--tier", choices=TIERS)
    s = sub.add_parser("show")
    s.add_argument("id")
    nw = sub.add_parser("new")
    nw.add_argument("kind", choices=["task", "epic"])
    nw.add_argument("title")
    nw.add_argument("--tier")
    nw.add_argument("--epic")
    nw.add_argument("--touches")
    nw.add_argument("--source")
    st = sub.add_parser("set")
    st.add_argument("id")
    st.add_argument("pairs", nargs="+", metavar="key=value")
    for name in ("start", "finish"):
        sub.add_parser(name).add_argument("id")
    a = sub.add_parser("approve")
    a.add_argument("ids", nargs="+")
    sub.add_parser("accept").add_argument("ids", nargs="+")
    r = sub.add_parser("reject")
    r.add_argument("ids", nargs="+")
    r.add_argument("--reason")
    c = sub.add_parser("close")
    c.add_argument("ids", nargs="+")
    c.add_argument("--as", dest="as_", required=True, choices=["done", "dropped"])
    c.add_argument("--reason", required=True, help="the evidence: a commit, a log line, or why it's moot")
    au = sub.add_parser("audit")
    au.add_argument("--epic")
    ss = sub.add_parser("stats")
    ss.add_argument("--since", metavar="DATE", help="YYYY-MM-DD: runs started that day or later")
    ss.add_argument("--epic")
    rp = sub.add_parser("report")
    rp.add_argument("--since", metavar="DATE|last", help="YYYY-MM-DD, or `last` for the previous report")
    sub.add_parser("check")
    sub.add_parser("tidy")
    d = sub.add_parser("dispatch")
    d.add_argument("--tier", choices=WORKER_TIERS)
    d.add_argument("--epic")
    d.add_argument("--ids", help="comma-separated task IDs (still only ready ones)")
    d.add_argument("--max", type=int, help="tasks to launch this run (default 6; no limit with --watch)")
    d.add_argument("--jobs", type=int, default=3, help="workers at once (default 3)")
    d.add_argument("--permission-mode", default="auto",
                   choices=["auto", "acceptEdits", "dontAsk", "bypassPermissions"])
    d.add_argument("--timeout", type=int, default=60, help="minutes per worker (default 60)")
    d.add_argument("--budget", type=float, help="max USD per worker (--max-budget-usd)")
    d.add_argument("--budget-total", type=float,
                   help="max USD for the whole run: stop launching once finished workers' cost reaches it")
    d.add_argument("--budget-pct", type=float, default=100,
                   help="use only this %% of --budget-total as the cap (default 100)")
    d.add_argument("--watch", action="store_true",
                   help="keep running: rescan for newly ready tasks until Ctrl-C or --budget-total is reached")
    d.add_argument("--interval", type=float, default=30, help="seconds between rescans with --watch (default 30)")
    d.add_argument("--escalate", action="store_true",
                   help="when a worker reports blocked, retry once one tier up (haiku→sonnet→opus)")
    d.add_argument("--dry-run", action="store_true")
    hk = sub.add_parser("hook")
    hk.add_argument("event", choices=["guard", "stop"])
    hk.add_argument("--worker", action="store_true", help="dispatch sets this: guard companion checkouts too")
    hk.add_argument("--project", help="the project to guard (default: $CLAUDE_PROJECT_DIR, then the event's cwd)")
    args = ap.parse_args()
    if args.cmd == "hook":
        return hook(args)  # configures itself, and stays silent outside an ergon project
    if args.cmd == "guide":
        return guide(args)
    try:
        configure(need_config=args.cmd != "init")
    except NotConfigured as err:
        raise SystemExit(f"ergon: {err}")
    if args.cmd == "init":
        return init(args)
    ITEMS.mkdir(parents=True, exist_ok=True)
    EPICS.mkdir(parents=True, exist_ok=True)
    if args.cmd in (None, "board"):
        if args.cmd is None:
            args.tier = args.epic = None
            args.all = False
        board(args)
    elif args.cmd in ("new", "set", "start", "finish", "close", "tidy"):  # read, then write state: one at a time
        with state_lock():
            {"new": new, "set": setf, "start": start, "finish": finish, "close": close,
             "tidy": tidy}[args.cmd](args)
    else:  # dispatch takes the lock itself, around each step (it runs for hours)
        {"next": nxt, "review": review, "show": show, "approve": approve, "reject": reject, "accept": accept,
         "audit": audit, "stats": stats, "report": report, "check": check, "dispatch": dispatch, "rules": rules}[args.cmd](args)


if __name__ == "__main__":
    main()
