"""Task and epic files: parse, dump, load, views, writes, audit, lint."""
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

from . import config as _config
from . import dispatch as _dispatch
from . import gates as _gates
from . import gitops as _gitops


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
    if kind == _config.EPICS:
        return sorted(_config.EPICS.glob(f"*/{_config.EPIC_FILE}"))
    return sorted([*_config.ITEMS.glob("*.md"), *_config.EPICS.glob("*/T-*.md")], key=lambda p: p.name)


def load(kind):
    return {m["id"]: m for m in (parse(p) for p in files(kind))}


def task_home(t, epics):
    """The folder a task's file belongs in, going by its `epic:` field."""
    e = epics.get(t.get("epic"))
    return e["_path"].parent if e else _config.ITEMS


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50].rstrip("-")


def next_id(kind, prefix):
    nums = [int(m.group(1)) for p in files(kind)
            if (m := re.match(rf"{prefix}-(\d+)", p.parent.name if kind == _config.EPICS else p.name))]
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
    for rule in _config.ACCEPT:
        if rule["tiers"] and not have & set(rule["tiers"]):
            continue
        if rule["touches"] and not any(_config.protect_hit(str(x), p) for x in t.get("touches") or []
                                       for p in rule["touches"]):
            continue
        return True
    return False


def board(args):
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
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
            note += f"  on {_gitops.epic_branch(e)}" if e["status"] == "open" and _gitops.branch_exists(_gitops.epic_branch(e)) else ""
            print(f"  {e['id']}  {e['status']:<7} {done}/{len(kids)} done{note}  {e['title']}")
    counts = {tier: sum(t["tier"] == tier and t["status"] not in ("done", "dropped")
                        for t in tasks.values()) for tier in TIERS}
    print("\nOPEN BY TIER  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))


def nxt(args):
    tasks = load(_config.ITEMS)
    ok = [t for t in tasks.values() if t["status"] == "ready" and deps_done(t, tasks)
          and (not args.tier or t["tier"] == args.tier)]
    for t in sorted(ok, key=lambda t: (TIERS.index(t["tier"]), t["id"])):
        print(line(t, tasks))
    if not ok:
        print("nothing approved and unblocked" + (f" for {args.tier}" if args.tier else ""))


def review(args):
    if args.interactive:
        return _gates.review_interactive(args)
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
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
            print(line(t, tasks) + f"  [{_config.TASK_BRANCH}{t['id']}]")
        print("\nfrom your own terminal:  ergon accept <ID|E-ID> ...   or   ergon reject <ID> --reason R")


def _print_run_usage(tid):
    """Tokens, top tools and denials of each run of tid whose transcript still exists."""
    from . import transcripts
    branch = _config.TASK_BRANCH + tid
    try:
        wt = _gitops.worktrees().get(branch)
    except Exception:
        wt = None
    wt = str(wt or _config.WORKTREES / tid)
    rows = []
    for log in sorted(_config.RUNS.glob(f"{tid}-*.json")):
        try:
            d = json.loads(log.read_text())
        except (OSError, ValueError):
            continue
        sid = d.get("session_id") if isinstance(d, dict) else None
        s = None
        for cwd in dict.fromkeys([wt, os.path.realpath(wt)]):
            s = transcripts.session_summary(sid, cwd, str(_config.RUNS / "transcripts.json"))
            if s:
                break
        if s:
            rows.append((log.stem, s))
    if rows:
        print("RUNS")
        for stem, s in rows:
            print(f"  {stem}")
            for l in transcripts.describe(s):
                print(f"    {l}")


def show(args):
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    item = tasks.get(args.id) or epics.get(args.id)
    if not item:
        raise SystemExit(f"no such id: {args.id}")
    print(item["_path"].read_text())
    if args.id in tasks:
        _print_run_usage(args.id)
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
        epics = load(_config.EPICS)
        if args.epic and args.epic not in epics:
            raise SystemExit(f"no such epic: {args.epic}")
        tid = next_id(_config.ITEMS, "T")
        home = epics[args.epic]["_path"].parent if args.epic else _config.ITEMS
        meta = {"id": tid, "title": args.title, "tier": args.tier, "status": "proposed",
                "epic": args.epic or "", "depends": [], "touches": args.touches.split(",") if args.touches else [],
                "source": args.source or "", "created": today(), "approved": "", "tier_why": "",
                "_path": home / f"{tid}-{slug(args.title)}.md", "_body": TASK_BODY}
        dump(meta, TASK_KEYS)
    else:
        eid = next_id(_config.EPICS, "E")
        home = _config.EPICS / f"{eid}-{slug(args.title)}"
        home.mkdir()
        meta = {"id": eid, "title": args.title, "status": "open", "created": today(),
                "_path": home / _config.EPIC_FILE, "_body": EPIC_BODY}
        dump(meta, EPIC_KEYS)
    print(meta["_path"].relative_to(_config.HOME))


# ---------- issue import ----------
# `ergon import gh#123` turns a GitHub issue into a proposed task. It shells out to the `gh` CLI
# rather than calling the API with urllib: gh already holds the user's login (ergon never sees a
# token), knows GHES hosts and which repo this checkout's remote is, and is a tool on PATH like
# git and claude, not a Python dependency. Only GitHub for now: Linear has no official CLI, so it
# would mean handling an API key here. The tier isn't guessed. An imported task is `human` until
# someone sizes it, so an issue body written outside the repo can't reach a dispatched worker
# without a human reading it first. Closing the issue is never done here (nothing is pushed, so
# the fix isn't public yet): finish_epic prints the `gh issue comment/close` commands instead.

GH_REF = re.compile(r"^gh(?::(?P<repo>\w[\w.-]*/\w[\w.-]*))?#(?P<num>\d+)$")
IMPORT_BODY_MAX = 4000  # characters of the issue body quoted into What


def gh_ref(source):
    """(number, repo or None) for a `gh#N` / `gh:OWNER/REPO#N` source, else None."""
    m = GH_REF.match(str(source or "").strip())
    return (m.group("num"), m.group("repo")) if m else None


def fetch_issue(ref):
    num, repo = gh_ref(ref)
    if not shutil.which("gh"):
        raise SystemExit("import needs the GitHub CLI `gh` on PATH, logged in (`gh auth login`): https://cli.github.com")
    r = subprocess.run(["gh", "issue", "view", num, *(["-R", repo] if repo else []),
                        "--json", "number,title,body,url,state,labels"],
                       cwd=_config.REPO, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"gh issue view {num} failed: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise SystemExit(f"gh issue view {num} didn't return JSON: {r.stdout[:200]}")


def import_body(ref, issue):
    body = (issue.get("body") or "").strip().replace("\r\n", "\n")
    if len(body) > IMPORT_BODY_MAX:
        body = body[:IMPORT_BODY_MAX] + f"\n… (cut at {IMPORT_BODY_MAX} characters; the rest is on the issue)"
    # Quoted, so its own `## ` headings can't pass for this file's sections.
    quoted = "\n".join(f"> {l}".rstrip() for l in body.splitlines()) or "> (no description)"
    labels = ", ".join(l.get("name", "") for l in issue.get("labels") or [] if isinstance(l, dict))
    return (f"\n## What\nImported from {ref} ({issue.get('url', '')}). Its text was written outside this repo: "
            f"rewrite it as a brief, size it and fill in Done when before approving.\n\n{quoted}\n"
            f"\n## Done when\n\n## Notes\n" + (f"Issue labels: {labels}\n" if labels else ""))


def import_issue(args):
    ref = args.ref.strip()
    if ref.startswith("linear:"):
        raise SystemExit("Linear isn't supported yet (no official CLI to shell out to); create the task "
                         "with `ergon new task ... --source linear:ABC-12`")
    if not gh_ref(ref):
        raise SystemExit(f"expected gh#123 or gh:OWNER/REPO#123, got {ref!r}")
    if args.tier and args.tier not in TIERS:
        raise SystemExit(f"--tier must be one of {TIERS}")
    def duplicate(tasks):
        dup = next((t["id"] for t in tasks.values() if t.get("source") == ref), None)
        if dup:
            raise SystemExit(f"{ref} is already {dup}")
    duplicate(load(_config.ITEMS))
    issue = fetch_issue(ref)  # outside the lock: the network can be slow
    if str(issue.get("state", "")).upper() == "CLOSED":
        print(f"note: {ref} is closed on GitHub")
    title = re.sub(r"\s+", " ", issue.get("title") or f"Issue {ref}").strip()
    title = re.sub(r"(?<=\s)#", "", title)  # ` #` starts a comment in frontmatter
    if title.startswith("[") and title.endswith("]"):  # would parse as a list
        title = title[1:-1].strip()
    if len(title) > 70:
        title = title[:69].rstrip() + "…"
    with _gitops.state_lock():
        tasks, epics = load(_config.ITEMS), load(_config.EPICS)
        duplicate(tasks)  # again: another import may have landed while gh ran
        if args.epic and args.epic not in epics:
            raise SystemExit(f"no such epic: {args.epic}")
        tid = next_id(_config.ITEMS, "T")
        home = epics[args.epic]["_path"].parent if args.epic else _config.ITEMS
        meta = {"id": tid, "title": title, "tier": args.tier or "human", "status": "proposed",
                "epic": args.epic or "", "depends": [], "touches": args.touches.split(",") if args.touches else [],
                "source": ref, "created": today(), "approved": "",
                "tier_why": "" if args.tier else "imported, not sized: pick the tier before approving",
                "_path": home / f"{tid}-{slug(title)}.md", "_body": import_body(ref, issue)}
        dump(meta, TASK_KEYS)
    print(f"{meta['_path'].relative_to(_config.HOME)}  ({tid}, proposed, tier {meta['tier']})")


def issue_commands(eid, tasks, merge=None):
    """The gh commands that report an epic's finished tasks on the issues they came from. Printed,
    never run: the merge is local until a human pushes it."""
    by_issue = {}
    for t in sorted((t for t in tasks.values() if t.get("epic") == eid and t["status"] == "done"),
                    key=lambda t: t["id"]):
        if gh_ref(t.get("source")):
            by_issue.setdefault(t["source"], []).append(t)
    cmds = []
    for ref, ts in by_issue.items():
        num, repo = gh_ref(ref)
        r = ["-R", repo] if repo else []
        done = "; ".join(f"{t['id']} {t['title']}" for t in ts)
        body = f"Done in {eid}" + (f" (merge {merge})" if merge else "") + f": {done}"
        cmds.append(shlex.join(["gh", "issue", "comment", num, *r, "--body", body]))
        cmds.append(shlex.join(["gh", "issue", "close", num, *r]))
    return cmds


def print_issue_commands(eid, tasks, merge=None):
    cmds = issue_commands(eid, tasks, merge)
    if cmds:
        print(f"{eid} finished issues it was imported from. After you push, report it (ergon never does):")
        print("\n".join("  " + c for c in cmds))



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
    if _config.RETIRED_FILE and _config.RETIRED_FILE.exists():
        for raw in _config.RETIRED_FILE.read_text().splitlines():
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
    here = set(_gitops.git("ls-files", raw=True).splitlines())
    for g in _dispatch.gitlinks():
        if (_config.REPO / g).is_dir():
            here |= {f"{g}/{f}" for f in walk(_config.REPO / g)}
    index = {"here": (_config.REPO, here)}
    for c in _config.COMPANIONS:
        if _dispatch.has_checkout(c):
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
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    index = tracked_files()
    open_ = {i: t for i, t in tasks.items() if t["status"] not in ("done", "dropped")
             and (not args.epic or t.get("epic") == args.epic)}
    cited = {}
    for row in _gitops.git("log", "--all", "--format=%h %s", raw=True).splitlines():
        for tid in set(re.findall(r"\bT-\d{3,}\b", row)):
            cited.setdefault(tid, []).append(row)
    log_lines = (_config.REPO / _config.LOG).read_text().count("\n") + 1 if _config.LOG and (_config.REPO / _config.LOG).exists() else 0
    retired = [(term, gate, note) for term, gate, note in retired_terms()
               if gate == "-" or tasks.get(gate, {}).get("status") == "done"]
    every_touch = tuple(k for c in _config.COMPANIONS for k in c["touches"])
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
        for c in _config.COMPANIONS:
            if c["name"] in uses and not _dispatch.companion_touch(t, c):
                found["touches"].append(f"{i} cites {c['name']} code but touches: doesn't name it; if it "
                                        f"changes that code, add {c['name']} or a dispatched worker stops blocked")
        text = (t.get("title", "") + "\n" + body).lower()
        for term, gate, note in retired:
            if re.search(rf"(?<![\w-]){re.escape(term.lower())}(?![\w-])", text) or \
                    any(term.lower() == x.lower() for x in t.get("touches") or []):
                found["retired"].append(f"{i} mentions {term}: {note}")
        src = t.get("source", "")
        m = re.match(rf"{re.escape(_config.LOG)}:(\d+)", src) if _config.LOG else None
        if m and int(m.group(1)) > log_lines:
            found["source"].append(f"{i} source {src}: {_config.LOG} has {log_lines} lines")

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

    for p in sorted(_config.EPICS.glob("*.md")):  # a flat epic file becomes <stem>/EPIC.md
        move(p, _config.EPICS / p.stem / _config.EPIC_FILE)
    epics = load(_config.EPICS)
    for t in load(_config.ITEMS).values():
        dst = task_home(t, epics) / t["_path"].name
        if dst != t["_path"]:
            move(t["_path"], dst)
    print(f"moved {n} file(s)")


def lint():
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    errs = []
    for p in _config.EPICS.glob("*.md"):
        errs.append(f"{p.name}: an epic belongs in its own folder; run `ergon tidy`")
    for kind in (_config.ITEMS, _config.EPICS):
        seen = {}
        for p in files(kind):
            i = parse(p)["id"]
            name = p.parent.name if kind == _config.EPICS else p.name
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
        if t.get("status") == "review" and not _gitops.branch_exists(_config.TASK_BRANCH + t["id"]):
            errs.append(f"{where}: in review but branch {_config.TASK_BRANCH}{t['id']} is gone, so there's nothing "
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
            for pr in _config.PROTECT:
                hit = next(((x, p) for x in t.get("touches") or [] for p in pr["touches"]
                            if _config.protect_hit(str(x), p)), None)
                if hit and TIERS.index(t["tier"]) < TIERS.index(pr["min_tier"]):
                    errs.append(f"{where}: touches {hit[0]} (protect {hit[1]}) needs tier "
                                f"{pr['min_tier']}+, is {t['tier']}" + (f": {pr['note']}" if pr["note"] else ""))
    for e in epics.values():
        if e.get("status") not in EPIC_STATUSES:
            errs.append(f"{e['id']}: status {e.get('status')!r} not in {EPIC_STATUSES}")
    # Map cards link gaps to tasks with a trailing "→ task T-012[, T-013]".
    for card in sorted(_config.MAP.rglob("*.md")) if _config.MAP and _config.MAP.is_dir() else []:
        for n, text in enumerate(card.read_text().splitlines(), 1):
            for ref in re.findall(r"→ task ((?:T-\d+(?:, )?)+)", text):
                for tid in re.findall(r"T-\d+", ref):
                    where = f"{card.relative_to(_config.REPO)}:{n}"
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
