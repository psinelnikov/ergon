"""Task and epic files: parse, dump, load, views, writes, audit, lint."""
import argparse
import datetime
import fcntl
import fnmatch
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
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
from . import history as _history
from . import shared as _shared


TIERS = ["haiku", "sonnet", "opus", "human"]
# feature: new behaviour a user would notice; bugfix: existing behaviour is wrong (security holes
# too); refactor: same behaviour, better code; chore: docs, help text, tests, config, tooling.
# Empty or unknown reads as feature (see category()).
CATEGORIES = ["feature", "bugfix", "refactor", "chore"]


def category(t):
    c = str(t.get("category") or "").strip().lower()
    return c if c in CATEGORIES else "feature"
TASK_STATUSES = ["proposed", "ready", "doing", "review", "blocked", "done", "dropped"]
# An epic starts proposed; a human's `approve E-NNN` approves its brief and opens it.
EPIC_STATUSES = ["proposed", "open", "done", "dropped"]
# Statuses a task can only reach after a human approved it.
NEEDS_APPROVAL = {"ready", "doing", "review", "blocked", "done"}
# Transitions `set` allows. proposed -> ready is approve's job, not set's. Only dispatch moves a
# task into `review`, and only a human moves it out: `accept` (-> done) or `reject` (-> ready).
AGENT_MOVES = {
    "ready": {"doing", "blocked"},
    "doing": {"done", "blocked", "ready"},
    "blocked": {"ready", "doing"},
}
TASK_KEYS = ["id", "title", "tier", "category", "status", "epic", "depends", "touches", "source",
             "created", "approved", "tier_why"]
EPIC_KEYS = ["id", "title", "status", "created", "approved"]


# ---------- file format ----------

def _scalar(val):
    """A frontmatter scalar or flow list: a quoted value loses its quotes (a ` #` inside is text),
    a bare one loses a trailing ` # comment`."""
    val = val.strip()
    m = re.match(r"""(["'])(.*?)\1(?:\s+#.*)?$""", val)
    if m:
        return m.group(2)
    val = re.sub(r"\s+#.*$", "", val).strip()
    if val.startswith("[") and val.endswith("]"):
        return [_scalar(v) for v in val[1:-1].split(",") if v.strip()]
    return val


def parse_fields(block):
    """(fields, bad) of a frontmatter block: `key: value`, flow lists, block lists (`key:` then
    `- item` lines at any indent), quoted scalars. `bad` is [(line number, text)] for lines it
    can't read; those are skipped."""
    meta, bad, listkey = {}, [], None
    for n, line in enumerate(block.splitlines(), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"\s*-(?:\s+(.*))?$", line)
        if item:
            if listkey is None:
                bad.append((n, line))
                continue
            if not isinstance(meta[listkey], list):
                meta[listkey] = []
            meta[listkey].append(_scalar(item.group(1) or ""))
            continue
        if line[0] in " \t" or ":" not in line:
            bad.append((n, line))
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        listkey = None
        if not val or val.startswith("#"):
            meta[key] = ""
            if not val:
                listkey = key
            continue
        meta[key] = _scalar(val)
    return meta, bad


def parse_text(text, where="task file"):
    """Frontmatter fields and `_body` of a task or epic file's text (no `_path`). `_bad` lists
    the frontmatter lines it couldn't read, as (line number, text)."""
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if not m:
        raise SystemExit(f"{where}: missing frontmatter")
    meta, bad = parse_fields(m.group(1))
    meta["_body"] = m.group(2)
    meta["_bad"] = bad
    return meta


def parse(path):
    meta = parse_text(path.read_text(), path)
    meta["_path"] = path
    return meta


def render(meta, keys):
    """The file text for meta: `keys` first (empty when missing), then any other fields."""
    lines = ["---"]
    for k in keys + [k for k in meta if k not in keys and not k.startswith("_")]:
        v = meta.get(k, "")
        if isinstance(v, list):
            v = "[" + ", ".join(v) + "]"
        lines.append(f"{k}: {v}".rstrip())
    lines.append("---")
    return "\n".join(lines) + "\n" + meta["_body"]


def dump(meta, keys):
    meta["_path"].write_text(render(meta, keys))


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
    if _config.SHARED:  # also past what the remote had at the last fetch, so offline clones collide less
        from . import shared as _shared
        nums += [int(i.split("-")[1]) for i in _shared.remote_ids(prefix)]
    return f"{prefix}-{max(nums, default=0) + 1:03d}"


def today():
    return datetime.date.today().isoformat()


# ---------- what an approval approves (shared mode) ----------
# docs/shared-state.md, "Trusting approval stamps". An approval covers the brief: id, title, tier,
# touches, accept and the What and Done when sections (and Options, with its choice, when a task
# has one). status, depends, epic and Notes are left
# out: dispatch moves status and adds dependencies to blocked tasks, and Notes are scratch.
# An epic's approval covers its id, title and the Goal, Out of scope and Constraints sections.

DIGEST_SECTIONS = ("what", "done when")
EPIC_DIGEST_SECTIONS = ("goal", "out of scope", "constraints")
HEADING = re.compile(r"^#{1,2}\s+(.*?)\s*$")


def sections(body, name):
    """The text under every `## <name>` heading of a body (matched case-insensitively), up to the
    next # or ## heading, joined. Every one counts, so a second `## What` lower down is covered too."""
    out, on = [], False
    for l in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = HEADING.match(l)
        if m:
            on = m.group(1).strip().lower() == name
            if on:
                out.append("")
            continue
        if on:
            out.append(l)
    return "\n".join(out)


def normalised(text):
    """Text with trailing spaces, runs of blank lines and blank lines at either end taken out."""
    lines, blank = [], False
    for l in (x.rstrip() for x in text.split("\n")):
        if not l:
            blank = bool(lines)
            continue
        if blank:
            lines.append("")
        lines.append(l)
        blank = False
    return "\n".join(lines)


def is_epic(item):
    return str(item.get("id") or "").startswith("E-")


def brief_digest(t):
    """sha256 (hex) of what approving t (a task or an epic) approves. Notes, status, depends and
    epic don't change it, nor any field a command writes about the brief (cod*, graded, category,
    concepts, tagged). A task's gate-weakening fields (a `verify:` override of the runtime gate,
    `prove: no`) are in it, so writing one after approval un-approves the task. So are a harden
    task's incidents (`harden:`, as a set) and the gate its proof replays them on (`harden_layer:`,
    `static` when unset), since together they are what the task must prove it catches. So are the
    files a task may add a suppression marker to (`suppress_ok:`, as a set, T-160), and the
    `## Options` section with its `Chosen:` line (E-006): a choice is made before approval, and the
    approval covers it. Each goes in only when set (the layer only with incidents, as it does
    nothing without them), so a task that has none of these keeps the digest it had before they
    counted."""
    body = t.get("_body") or ""
    if is_epic(t):
        doc = {"kind": "epic", "id": str(t.get("id") or "").strip(),
               "title": " ".join(str(t.get("title") or "").split())}
        for name in EPIC_DIGEST_SECTIONS:
            doc[name] = normalised(sections(body, name))
        return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    doc = {
        "id": str(t.get("id") or "").strip(),
        "title": " ".join(str(t.get("title") or "").split()),
        "tier": str(t.get("tier") or "").strip(),
        "touches": sorted({str(x).strip().strip("/") for x in t.get("touches") or [] if str(x).strip()}),
        "accept": "yes" if str(t.get("accept") or "").strip().lower() in YES else "",
    }
    verify = t.get("verify")
    verify = " ".join(" ".join(verify) if isinstance(verify, list) else str(verify or "").split())
    if verify:
        doc["verify"] = verify
    if prove_off(t):
        doc["prove"] = "no"
    harden = sorted(set(_dispatch.harden_ids(t)))
    if harden:
        doc["harden"] = harden
        doc["harden_layer"] = _dispatch.harden_layer(t)
    suppress_ok = sorted(set(_dispatch.suppress_ok_paths(t)))
    if suppress_ok:
        doc["suppress_ok"] = suppress_ok
    for name in DIGEST_SECTIONS:
        doc[name] = normalised(sections(body, name))
    opts = normalised(sections(body, "options"))
    if opts:  # the options and the human's choice among them (E-006)
        doc["options"] = opts
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------- a task's options (E-006) ----------
# A proposed feature may carry a `## Options` section: a numbered list of approaches, the first
# recommended. A human picks one with `ergon choose`, which writes a first line
# `Chosen: <N> by <name> <date>` (or `Chosen: own by <name> <date> — <text>`) above the list.
# `approve` refuses a task whose section has no such line; the guard refuses an agent's edit of it.

OPTION_ITEM = re.compile(r"^\s*(\d+)[.)]\s+(.*?)\s*$")
CHOSEN = re.compile(r"^Chosen:\s+(\d+|own)\s+by\s+(.+?)\s+(\d{4}-\d\d-\d\d)(?:\s+—\s+(.*?))?\s*$")
RECOMMENDED = re.compile(r"\s*\((?:recommended)\)\s*$", re.I)


def _body_of(text):
    """A task file's text without its frontmatter (the text itself when it has none)."""
    return re.sub(r"\A---\n.*?\n---\n?", "", text or "", count=1, flags=re.S)


def has_options(t):
    """Has t's body a `## Options` heading?"""
    return any((m := HEADING.match(l)) and m.group(1).strip().lower() == "options"
               for l in (t.get("_body") or "").splitlines())


def chosen_lines(text):
    """The `Chosen:` lines under `## Options` in a task file's text (or body), stripped, in order.
    The guard compares them before and after an agent's edit."""
    return [l.strip() for l in sections(_body_of(text), "options").split("\n") if l.strip().startswith("Chosen:")]


def options(t):
    """(options, chosen) of t's `## Options` section. options: the numbered list's texts in order
    (a trailing `(recommended)` taken off). chosen: the first `Chosen:` line as {"option": N (an
    int) or "own", "text": the chosen option's text or the human's own, "by", "date", "line"},
    or None when there is none. A Chosen: line that doesn't read gives {"option": None, "line"}.
    ([], None) when t has no section."""
    text = sections(t.get("_body") or "", "options")
    opts, chosen = [], None
    for l in text.split("\n"):
        s = l.strip()
        if s.startswith("Chosen:"):
            if chosen is None:
                m = CHOSEN.match(s)
                if not m:
                    chosen = {"option": None, "text": "", "by": "", "date": "", "line": s}
                else:
                    num = int(m[1]) if m[1].isdigit() else "own"
                    chosen = {"option": num, "text": m[4] or "", "by": m[2], "date": m[3], "line": s}
            continue
        m = OPTION_ITEM.match(l)
        if m and m[2]:
            opts.append(RECOMMENDED.sub("", m[2]))
    if chosen and isinstance(chosen["option"], int):
        n = chosen["option"]
        chosen["text"] = opts[n - 1] if 1 <= n <= len(opts) else ""
    return opts, chosen


def options_problems(t):
    """What is wrong with t's `## Options` section, one line each, or [] (also for no section)."""
    if not has_options(t):
        return []
    opts, chosen = options(t)
    out = []
    if len(opts) < 2:
        out.append(f"## Options lists {len(opts)} numbered option(s); it needs at least 2 (`1. …`, `2. …`)")
    if len(chosen_lines(t.get("_body") or "")) > 1:
        out.append("## Options has more than one `Chosen:` line")
    if chosen and chosen["option"] is None:
        out.append(f"## Options has a `Chosen:` line that doesn't read: {chosen['line']!r} (expected "
                   f"`Chosen: <N> by <name> <YYYY-MM-DD>` or `Chosen: own by <name> <YYYY-MM-DD> — <text>`)")
    elif chosen and chosen["option"] == "own" and not chosen["text"]:
        out.append("## Options has `Chosen: own` without its text (`… — <text>`)")
    elif chosen and isinstance(chosen["option"], int) and not 1 <= chosen["option"] <= len(opts):
        out.append(f"## Options has `Chosen: {chosen['option']}`, but the list has {len(opts)} option(s)")
    return out


def choice_problem(t):
    """Why t can't be approved yet for want of a choice, or None: it has a `## Options` section
    with no `Chosen:` line, or one that doesn't name a listed option (or own text). A task with no
    section is approved as before."""
    if is_epic(t) or not has_options(t):
        return None
    chosen = options(t)[1]
    if chosen is None:
        return (f"{t['id']} has options and no choice: a human picks one first with "
                f"`ergon choose {t['id']} <N>` (or `--own \"<text>\"`), in their own terminal")
    if chosen["option"] is None or not chosen["text"]:
        return (f"{t['id']}'s `Chosen:` line doesn't name one of its options (`ergon check` says why): a human "
                f"chooses again with `ergon choose {t['id']} <N>`, in their own terminal")
    return None


def chosen_line(option, by, date, text=""):
    """The `Chosen:` line `ergon choose` writes: option is a number or "own" (with its text)."""
    return f"Chosen: {option} by {by} {date}" + (f" — {text}" if option == "own" else "")


def set_chosen(body, line):
    """body with every `Chosen:` line under `## Options` taken out and `line` written as the
    first line of its first `## Options` section."""
    out, on, done = [], False, False
    for l in body.split("\n"):
        m = HEADING.match(l)
        if m:
            on = m.group(1).strip().lower() == "options"
            out.append(l)
            if on and not done:
                out.append(line)
                done = True
            continue
        if on and l.strip().startswith("Chosen:"):
            continue
        out.append(l)
    return "\n".join(out)


def options_section(opts):
    """The `## Options` section `ergon new task --option` writes, the first option recommended."""
    return "\n## Options\n" + "".join(f"{n}. {o}" + (" (recommended)" if n == 1 else "") + "\n"
                                      for n, o in enumerate(opts, 1))


def print_options(t, indent="  "):
    """Print t's options, the recommended and the chosen one marked."""
    opts, chosen = options(t)
    for n, o in enumerate(opts, 1):
        marks = [m for m, on in (("recommended", n == 1), ("chosen", bool(chosen) and chosen["option"] == n)) if on]
        print(f"{indent}{n}. {o}" + (f"  ({', '.join(marks)})" if marks else ""))
    if chosen and chosen["option"] == "own":
        print(f"{indent}own. {chosen['text']}  (chosen)")


def choice_text(t):
    """One line saying what was chosen for t, or None when nothing is."""
    chosen = options(t)[1]
    if not chosen or chosen["option"] is None:
        return None
    what = f"own: {chosen['text']}" if chosen["option"] == "own" else f"{chosen['option']}. {chosen['text']}"
    return f"{what}  (by {chosen['by']} {chosen['date']})"


PROVE_NO = ("no", "false", "0")  # as dispatch.PROVE_NO reads `prove:`


def prove_off(t):
    """Does t's `prove:` waive the red-on-base proof?"""
    return str(t.get("prove") or "").strip().lower() in PROVE_NO


# ---------- is an epic approved? ----------
# An epic is filed `proposed`; a human's `approve E-NNN` stamps `approved:` and opens it.
# In shared mode an open epic counts as approved only if a signed approval of its brief verifies,
# as a task's does, stamp or no stamp: an open epic with no stamp (from before epics were
# approved, or pushed by a clone without the guard) is refused until a human runs `approve
# E-NNN` on it (T-124). A done epic is checked only if stamped: nothing is broken down or opened
# from one. In local mode a stamp can't be verified, so an epic past proposed counts as approved
# with or without one, which keeps old unstamped epics working; there the guard is what stops an
# agent opening one (`new epic` writes proposed, `set` won't move an epic out of proposed, and the
# guard refuses an agent's status edit or a new EPIC.md that isn't proposed).

def epic_problem(e, apr=None):
    """Why tasks of epic e mustn't be offered or started (its brief isn't approved), or None.
    apr: gitops.approvals() in shared mode. None for no epic (a task without one)."""
    if not e:
        return None
    eid, status = e["id"], e.get("status")
    if status == "proposed":
        return f"epic {eid} is proposed: a human approves its brief with `ergon approve {eid}`"
    if status == "dropped":
        return f"epic {eid} is dropped"
    if not apr:
        return None  # local mode: nothing to verify a stamp against
    if status != "open" and not e.get("approved"):
        return None  # a done epic from before epics were approved
    why = apr.problem(e)
    return f"epic {eid}'s approval doesn't verify: {why}" if why else None


def epic_of(t, epics):
    return epics.get(t.get("epic")) if t.get("epic") else None


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
    from . import priority as _pri
    n = _pri.unblocks(t, tasks)
    flags.append(f"p={_pri.score(t, tasks):.1f}")
    if n > 0:
        flags.append(f"unblocks {n}")
    pin = str(t.get("pin") or "").strip()
    if pin in ("first", "last"):
        flags.append(f"pinned {pin}")
    lim = limited(t)
    if lim:
        from . import limits as _limits
        flags.append(f"limited until {_limits.clock(lim[0])}")
    tail = f"  ({'; '.join(flags)})" if flags else ""
    return f"  {t['id']}  {t['tier']:<6}  {t['title']}{tail}"


YES = ("yes", "true", "1")


def is_asking(t):
    """Is t parked on a worker's question, waiting for a human's `ergon answer`?"""
    return t.get("status") == "blocked" and bool(t.get("asking"))


LIMITED_AT = "%Y-%m-%dT%H:%M:%SZ"


def limited_mark(v):
    """A `limited:` value, `<reset, UTC ISO> <step>`, as (reset epoch seconds, step), or None
    when it doesn't read as one."""
    import datetime
    at, _, step = str(v or "").strip().partition(" ")
    try:
        when = datetime.datetime.strptime(at, LIMITED_AT).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    step = step.strip()
    if not re.fullmatch(r"worker|review|(?:fix|verify|conflict) \d+", step):
        return None
    return when.timestamp(), step


def limited(t):
    """(reset epoch seconds, step) when t is parked by the usage limit (blocked, with a `limited:`
    mark that reads), else None. Dispatch resumes it at a scan at or after that time."""
    return limited_mark(t.get("limited")) if t.get("status") == "blocked" else None


# In shared mode a task dispatch held at `review` has its task/<ID> branch in one clone only (task
# branches are never pushed). Dispatch's Note names that clone: `held: <user>@<host>`.
HELD = re.compile(r"^- \d{4}-\d\d-\d\d dispatch: .*?\bheld: ([^\s)]+)", re.M)


def clone_id():
    """This clone as a `held:` Note names it: <user>@<host>."""
    try:
        user = getpass.getuser()
    except Exception:  # no login name anywhere (some containers)
        user = os.environ.get("USER") or "someone"
    return f"{user}@{socket.gethostname().split('.')[0] or 'localhost'}"


def held_by(t):
    """The clone dispatch held t's branch on, from its latest `held:` Note, or None."""
    hits = HELD.findall(t.get("_body") or "")
    return hits[-1] if hits else None


def held_elsewhere(t):
    """In shared mode, the other clone that holds review task t's branch, or None (local mode,
    held here, or no clone recorded)."""
    if not _config.SHARED or t.get("status") != "review":
        return None
    who = held_by(t)
    return who if who and who != clone_id() else None


def needs_accept(t, *tiers):
    """Does a finished branch of t wait at `review` for a human `accept`? Its own `accept: yes`, or
    an [[accept]] rule its tier (or any of `tiers`, e.g. the one an --escalate finished at) and
    touches: match."""
    if str(t.get("accept", "")).lower() in YES:
        return True
    return any(rule_hit(rule, t, tiers) for rule in _config.ACCEPT)


def needs_evidence(t, *tiers):
    """Does t's done need EVIDENCE: files? Its own `evidence: yes`, or an [[evidence]] rule."""
    if str(t.get("evidence", "")).lower() in YES:
        return True
    return any(rule_hit(rule, t, tiers) for rule in _config.EVIDENCE)


def rule_hit(rule, t, tiers=()):
    """Does t match an [[accept]] or [[two_person]] rule? Its tier (or one of `tiers`) is in the
    rule's tiers, when given, and a touches: entry falls under one of its patterns, when given."""
    if rule["tiers"] and not {t.get("tier"), *tiers} & set(rule["tiers"]):
        return False
    return not rule["touches"] or any(_config.protect_hit(str(x), p) for x in t.get("touches") or []
                                      for p in rule["touches"])


def two_person(t):
    """Does t need approvals from two different approvers ([[two_person]], shared mode only)? It is
    matched against the task as it is now, so retiering it into or out of a rule changes the answer
    (and its digest, so earlier approvals stop counting either way)."""
    return bool(_config.SHARED) and not is_epic(t) and any(rule_hit(rule, t) for rule in _config.TWO_PERSON)


# ---------- approval by inheritance (T-136) ----------
# A worker's FOUND follow-up that isn't a feature is approved by the human approval of the task
# that found it: it goes straight to ready with an `auto` stamp instead of waiting at proposed.
# All of these must hold when it is filed, and `check` holds the stamp to them for as long as
# the task is open:
#   1. category: is bugfix, refactor or chore (empty or unknown reads as feature);
#   2. its tier isn't human;
#   3. its touches: is non-empty and no entry falls under a [[protect]] rule, whatever min_tier;
#   4. its depth, the length of its source: chain of auto-approved tasks back to a human-approved
#      root, is at most follow_up_depth (a follow-up of a human-approved task is depth 1; 0: off);
#   5. local mode: in shared mode an approval is a signed commit, and a parent's signature can't
#      vouch for a task that didn't exist when it was signed;
#   6. its Done when section isn't empty: nothing to work or review against otherwise.
# The guard refuses an agent's edit to category: or touches: of an auto-approved task, so a
# follow-up can't be relabelled or widened after it was approved.

AUTO_CATEGORIES = ("bugfix", "refactor", "chore")
AUTO_STAMP = re.compile(r"^auto (\S+) follow-up of (T-\d+) \(root (T-\d+), (.*)\)$")


def is_auto(t):
    """Was t approved by inheritance (an `auto …` stamp) rather than by a human?"""
    return str(t.get("approved") or "").startswith("auto ")


def auto_problem(t):
    """Why t, as filed, can't be approved by inheritance (conditions 1, 2, 3, 5 and 6), or None."""
    if _config.SHARED:
        return "shared mode: approvals must be signed by an approver"
    cat = str(t.get("category") or "").strip()
    if cat not in AUTO_CATEGORIES:
        return f"category {cat or '(empty)'} is a feature: a human approves it"
    if t.get("tier") not in TIERS or t.get("tier") == "human":
        return f"tier {t.get('tier')}"
    touches = [str(x).strip() for x in t.get("touches") or [] if str(x).strip()]
    if not touches:
        return "no touches: declared"
    for pr in _config.PROTECT:
        hit = next(((x, p) for x in touches for p in pr["touches"] if _config.protect_hit(x, p)), None)
        if hit:
            return f"touches {hit[0]} (protect {hit[1]})"
    if not sections(t.get("_body") or "", "done when").strip():
        return "no Done when: a human writes or approves one"
    return None


def approval_of(t, tasks, _seen=()):
    """How t's approval stands: (root ID, depth, None) when it holds, depth 0 for a human stamp
    and n for an `auto` stamp n follow-ups from its human-approved root; else (None, None, why).
    An auto stamp holds only if the task as filed meets conditions 1-6, it names the task's
    source: as its parent and category: as its category, and that parent's approval holds too."""
    stamp = str(t.get("approved") or "").strip()
    if not stamp:
        return None, None, "not approved"
    if t.get("status") == "proposed":  # unapproved whatever its stamp says
        return None, None, "it is proposed"
    if not is_auto(t):
        return t.get("id"), 0, None
    m = AUTO_STAMP.match(stamp)
    if not m:
        return None, None, f"can't read the auto stamp {stamp!r}"
    if t.get("id") in _seen:
        return None, None, "its source: chain loops"
    cat, parent = m[1], m[2]
    if cat != str(t.get("category") or "").strip():
        return None, None, f"stamped as a {cat} follow-up, but category is {t.get('category') or '(empty)'}"
    if parent != str(t.get("source") or "").strip():
        return None, None, f"stamped as a follow-up of {parent}, but source is {t.get('source') or '(empty)'}"
    why = auto_problem(t)
    if why:
        return None, None, why
    if parent not in tasks:
        return None, None, f"its parent {parent} doesn't exist"
    root, depth, why = approval_of(tasks[parent], tasks, _seen + (t.get("id"),))
    if why:
        return None, None, f"its parent {parent} isn't approved ({why})"
    if depth + 1 > _config.FOLLOW_UP_DEPTH:
        return None, None, f"depth {depth + 1} is past follow_up_depth = {_config.FOLLOW_UP_DEPTH}"
    return root, depth + 1, None


def auto_approval(t, parent, tasks):
    """Is t, a follow-up `parent` found, approved by inheritance as it is filed? (stamp, root ID,
    depth), or None when it waits at proposed for a human. tasks: the ancestors to walk."""
    if auto_problem(t):
        return None
    root, depth, why = approval_of(parent, tasks)
    if why or depth + 1 > _config.FOLLOW_UP_DEPTH:
        return None
    root_task = parent if root == parent.get("id") else tasks.get(root) or {}
    by = re.sub(r"\s*sha256:\S+\s*$", "", str(root_task.get("approved") or "")).strip()
    return f"auto {t['category']} follow-up of {parent['id']} (root {root}, {by})", root, depth + 1


def follow_ups(t, tasks, open_only=True):
    """Tasks whose `source:` chain leads to t, nearest first; by default only those not done or
    dropped (T-137: t is complete when it and every follow-up under it are closed)."""
    kids, seen, out = {}, {t["id"]}, []
    for k in sorted(tasks):
        kids.setdefault(str(tasks[k].get("source") or "").strip(), []).append(tasks[k])
    todo = [t["id"]]
    while todo:
        for c in kids.get(todo.pop(0), []):
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            todo.append(c["id"])
            if not open_only or c["status"] not in ("done", "dropped"):
                out.append(c)
    return out


def status_text(t, tasks):
    """The task's status; a done one with open follow-ups reads `done, N follow-ups open`."""
    n = len(follow_ups(t, tasks)) if t["status"] == "done" else 0
    return f"done, {n} follow-up{'s' if n != 1 else ''} open" if n else t["status"]


def _epic_checkout(e):
    """For an open epic whose branch exists: 'on <branch> at <path>' when a worktree holds it, else
    'on <branch> (branch only: ergon start <id> for a worktree)'. '' otherwise. Read-only."""
    if e["status"] != "open":
        return ""
    branch = _gitops.epic_branch(e)
    if not _gitops.branch_exists(branch):
        return ""
    try:
        path = _gitops.worktrees().get(branch)
    except Exception:
        path = None
    if path:
        return f"on {branch} at {path}"
    return f"on {branch} (branch only: ergon start {e['id']} for a worktree)"


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
            note += "  <- brief awaiting approval" if e["status"] == "proposed" else ""
            where = _epic_checkout(e)
            note += f"  {where}" if where else ""
            print(f"  {e['id']}  {e['status']:<8} {done}/{len(kids)} done{note}  {e['title']}")
    counts = {tier: sum(t["tier"] == tier and t["status"] not in ("done", "dropped")
                        for t in tasks.values()) for tier in TIERS}
    print("\nOPEN BY TIER  " + "  ".join(f"{k}:{v}" for k, v in counts.items()))


def nxt(args):
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    ok = [t for t in tasks.values() if t["status"] == "ready" and deps_done(t, tasks)
          and (not args.tier or t["tier"] == args.tier)]
    apr = _gitops.approvals()  # shared mode: only tasks whose signed approval verifies
    hidden = [t for t in ok if apr and apr.problem(t)]
    ok = [t for t in ok if t not in hidden]
    waiting = [t for t in ok if epic_problem(epic_of(t, epics), apr)]  # its epic's brief isn't approved
    ok = [t for t in ok if t not in waiting]
    from . import priority as _pri
    for t in sorted(ok, key=lambda t: _pri.sort_key(t, tasks)):
        print(line(t, tasks))
    if not ok:
        print("nothing approved and unblocked" + (f" for {args.tier}" if args.tier else ""))
    if hidden:
        print(f"({len(hidden)} ready task(s) left out because their approval doesn't verify: "
              f"{', '.join(sorted(t['id'] for t in hidden))}; `ergon check` says why)")
    if waiting:
        why = {}
        for t in waiting:
            why.setdefault(epic_problem(epic_of(t, epics), apr), []).append(t["id"])
        for w, ids in sorted(why.items()):
            print(f"({len(ids)} ready task(s) left out because {w}: {', '.join(sorted(ids))})")


def review(args):
    if args.interactive:
        return _gates.review_interactive(args)
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    proposed = [t for t in tasks.values() if t["status"] == "proposed"]
    # a task with options and no choice can't be approved yet: it waits on `ergon choose` first
    choosing = sorted((t for t in proposed if choice_problem(t)), key=lambda t: t["id"])
    waiting = [t for t in proposed if t not in choosing]
    briefs = sorted((e for e in epics.values() if e.get("status") == "proposed"), key=lambda e: e["id"])
    if briefs:
        print(f"\nEPICS AWAITING APPROVAL ({len(briefs)}): approving one approves its brief "
              f"(Goal, Out of scope, Constraints) and opens it; its tasks wait until then. "
              f"Rejecting one drops its brief")
        for e in briefs:
            kids = sum(t.get("epic") == e["id"] and t["status"] != "dropped" for t in tasks.values())
            print(f"  {e['id']}  {e['title']}  ({kids} task(s))")
    by_epic = {}
    for t in waiting:
        by_epic.setdefault(t.get("epic") or "", []).append(t)
    # shared mode: who has approved each two-person task so far, and how many more it needs
    apr = _gitops.approvals() if any(two_person(t) for t in waiting) else None
    for eid, group in sorted(by_epic.items()):
        print(f"\n{eid + '  ' + epics[eid]['title'] if eid in epics else '(no epic)'}")
        for t in sorted(group, key=lambda t: t["id"]):
            print(line(t, tasks))
            if t.get("tier_why"):
                print(f"         why {t['tier']}: {t['tier_why']}")
            if choice_text(t):
                print(f"         chosen {choice_text(t)}")
            if apr and two_person(t):
                print(f"         two-person: {apr.tally(t)}")
    if not waiting and not briefs:
        print("nothing awaiting approval")
    else:
        print("\napprove from your own terminal:  ergon approve <ID|E-ID> ...   or drop:  ergon reject <ID|E-ID>")
    if choosing:
        print(f"\nAWAITING A CHOICE ({len(choosing)}): pick how each is built; approving one waits on its choice")
        for t in choosing:
            print(line(t, tasks))
            print_options(t, indent="         ")
        print('\nfrom your own terminal:  ergon choose <ID> <N>   or   ergon choose <ID> --own "<text>"')
    asking = sorted((t for t in tasks.values() if is_asking(t)), key=lambda t: t["id"])
    if asking:
        print(f"\nAWAITING ANSWER ({len(asking)}): a worker asked, its branch is kept")
        for t in asking:
            print(line(t, tasks))
            for n, (q, opts) in enumerate(_dispatch.asked(t)[0], 1):
                print(f"         Q{n} {q}" + (f"  [{' | '.join(opts)}]" if opts else ""))
        print('\nfrom your own terminal:  ergon answer <ID> "<text>"   or   ergon answer <ID>  (pick from the options)')
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
        from . import priority as _pri
        t = tasks[args.id]
        print(f"CATEGORY  {category(t)}" + ("" if t.get("category") else " (not set: reads as feature)"))
        if has_options(t):
            print("OPTIONS  " + (f"chosen {choice_text(t)}" if choice_text(t) else
                                 f"no choice yet: `ergon choose {t['id']} <N>` (a human, in their own terminal)"))
            print_options(t)
        print(f"PRIORITY  cod={t.get('cod', '')}  cod_why={t.get('cod_why', '')}  graded={t.get('graded', '')}"
              f"  unblocks={_pri.unblocks(t, tasks)}  p={_pri.score(t, tasks):.1f}")
        fus = follow_ups(t, tasks, open_only=False)
        if fus:
            print(f"FOLLOW-UPS  {status_text(t, tasks)}")
            for c in fus:
                print(f"  {c['id']}  {c['status']:<8}  {category(c):<8}  {c['title']}")
        _print_run_usage(args.id)
    if args.id in epics:
        where = _epic_checkout(epics[args.id])
        if where:
            print(where.replace("on ", "BRANCH  ", 1))
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
        if args.category and args.category not in CATEGORIES:
            raise SystemExit(f"--category must be one of {CATEGORIES}")
        opts = [o for o in (" ".join(str(x).split()) for x in getattr(args, "option", None) or []) if o]
        if len(opts) == 1:
            raise SystemExit("--option: give at least 2 (the recommended one first), or none: a choice of one "
                             "isn't a choice")
        if _gates.in_agent_shell() and category({"category": args.category}) == "feature" and len(opts) < 2:
            raise SystemExit("refused: in an agent shell a feature (--category feature, or none) needs 2 to 4 "
                             "--option \"<approach>\", the recommended one first, so the human chooses how it is "
                             "built before approving it. If it isn't new behaviour a user would notice, give "
                             "--category bugfix, refactor or chore")
        epics = load(_config.EPICS)
        if args.epic and args.epic not in epics:
            raise SystemExit(f"no such epic: {args.epic}")
        tid = next_id(_config.ITEMS, "T")
        home = epics[args.epic]["_path"].parent if args.epic else _config.ITEMS
        meta = {"id": tid, "title": args.title, "tier": args.tier, "category": args.category or "", "status": "proposed",
                "epic": args.epic or "", "depends": [], "touches": args.touches.split(",") if args.touches else [],
                "source": args.source or "", "created": today(), "approved": "", "tier_why": "",
                "_path": home / f"{tid}-{slug(args.title)}.md",
                "_body": TASK_BODY.replace("\n## Notes", options_section(opts) + "\n## Notes") if opts else TASK_BODY}
        dump(meta, TASK_KEYS)
        _history.record("created", meta, tier=args.tier, title=args.title, source=args.source or None, by="new")
    else:
        eid = next_id(_config.EPICS, "E")
        home = _config.EPICS / f"{eid}-{slug(args.title)}"
        home.mkdir()
        meta = {"id": eid, "title": args.title, "status": "proposed", "created": today(), "approved": "",
                "_path": home / _config.EPIC_FILE, "_body": EPIC_BODY}
        dump(meta, EPIC_KEYS)
        _history.record("created", meta, title=args.title, by="new")
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
    if args.category and args.category not in CATEGORIES:
        raise SystemExit(f"--category must be one of {CATEGORIES}")
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
        meta = {"id": tid, "title": title, "tier": args.tier or "human", "category": args.category or "", "status": "proposed",
                "epic": args.epic or "", "depends": [], "touches": args.touches.split(",") if args.touches else [],
                "source": ref, "created": today(), "approved": "",
                "tier_why": "" if args.tier else "imported, not sized: pick the tier before approving",
                "_path": home / f"{tid}-{slug(title)}.md", "_body": import_body(ref, issue)}
        dump(meta, TASK_KEYS)
        _history.record("created", meta, tier=meta["tier"], title=title, source=ref, by="import")
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


class BranchFile:
    """A file at a branch's tip, read through git; stands in for a Path in the audit."""
    def __init__(self, branch, rel):
        self.branch, self.rel = branch, rel

    def _show(self):
        try:
            return _gitops.git("show", f"{self.branch}:{self.rel}", raw=True)
        except _gitops.GitError:
            return None

    def is_file(self):
        return self._show() is not None

    def read_text(self, errors="strict"):
        return self._show() or ""


class BranchRoot:
    def __init__(self, branch):
        self.branch = branch

    def __truediv__(self, rel):
        return BranchFile(self.branch, str(rel))


def epic_index(index, epic):
    """`index` with "here" read from the epic's branch tip, or `index` when the epic has no branch."""
    if not epic:
        return index
    branch = _gitops.epic_branch(epic)
    if not _gitops.branch_exists(branch):
        return index
    names = set(_gitops.git("ls-tree", "-r", "--name-only", branch, raw=True).splitlines())
    return {**index, "here": (BranchRoot(branch), names)}


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
    base_index = tracked_files()
    epic_indexes = {}
    open_ = {i: t for i, t in tasks.items() if t["status"] not in ("done", "dropped")
             and (not args.epic or t.get("epic") == args.epic)}
    cited = {}
    for row in _gitops.git("log", *_shared.log_all(), "--format=%h %s", raw=True).splitlines():
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

        eid = t.get("epic")
        if eid not in epic_indexes:
            epic_indexes[eid] = epic_index(base_index, epics.get(eid))
        index = epic_indexes[eid]
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
    """Put every file where it belongs, and clear out leftover epic worktrees. Moves files and never
    overwrites one. Also removes a worktree under <worktrees>/E-NNN whose epic isn't open (or doesn't
    exist) and whose HEAD is detached or on a deleted branch, but only if `git status --porcelain`
    there (untracked files too) is empty and the shell isn't inside it; others are listed with why."""
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
    tidy_worktrees(epics)


def tidy_worktrees(epics):
    here = Path.cwd().resolve()
    try:
        wts = _gitops.worktree_list()
    except _gitops.GitError:
        return
    for path, branch in wts:
        wt = Path(path)
        if wt.parent != _config.WORKTREES or not re.fullmatch(r"E-\d+", wt.name):
            continue
        if (epics.get(wt.name) or {}).get("status") == "open":
            continue
        if branch and _gitops.branch_exists(branch):
            continue
        if wt.resolve() == _config.REPO.resolve() or here == wt.resolve() or wt.resolve() in here.parents:
            print(f"kept {wt}: the current shell is inside it")
            continue
        try:
            dirty = _gitops.git("status", "--porcelain", "--untracked-files=all", raw=True, cwd=wt).strip()
            if dirty:
                print(f"kept {wt}: it has uncommitted or untracked changes")
                continue
            _gitops.git("worktree", "remove", str(wt))
        except _gitops.GitError as err:
            print(f"kept {wt}: {str(err).splitlines()[0]}")
            continue
        print(f"removed {wt}: {wt.name} is not open and it was clean")


def protect_problems(t):
    """Why t's tier is below a [[protect]] minimum for a path it touches: one line per rule it
    breaks, or []. `check` runs it on every open task; a breakdown on every TASK block."""
    if t.get("tier") not in TIERS:
        return []
    out = []
    for pr in _config.PROTECT:
        hit = next(((x, p) for x in t.get("touches") or [] for p in pr["touches"]
                    if _config.protect_hit(str(x), p)), None)
        if hit and TIERS.index(t["tier"]) < TIERS.index(pr["min_tier"]):
            out.append(f"touches {hit[0]} (protect {hit[1]}) needs tier {pr['min_tier']}+, is {t['tier']}"
                       + (f": {pr['note']}" if pr["note"] else ""))
    return out


def lint(warns=None):
    """(errors, summary). Warnings that don't fail the check go into `warns`, a list, when given
    (the stop hook passes none: its stdout is JSON)."""
    tasks, epics = load(_config.ITEMS), load(_config.EPICS)
    apr = _gitops.approvals()  # None outside shared mode
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
    for kind in (tasks, epics):
        for t in kind.values():
            for n, line in t.get("_bad", []):
                errs.append(f"{t['_path'].name}:{n}: frontmatter line can't be read: {line.strip()!r}")
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
        if str(t.get("category") or "").strip() and t["category"] not in CATEGORIES:
            errs.append(f"{where}: category {t.get('category')!r} not in {CATEGORIES}")
        if t.get("status") not in TASK_STATUSES:
            errs.append(f"{where}: status {t.get('status')!r} not in {TASK_STATUSES}")
        # shared mode: an open task's approval must verify (a signed commit from an approver
        # approving its current brief); the approved: stamp is only a copy
        why = apr.problem(t) if apr and t.get("status") in _gitops.GATED else None
        if why:
            errs.append(f"{where}: {t['status']}, but its approval doesn't verify: {why}")
        elif t.get("status") in NEEDS_APPROVAL and not t.get("approved"):
            errs.append(f"{where}: status {t['status']} without a human `approved:` stamp")
        elif is_auto(t) and t.get("status") not in ("done", "dropped"):
            why = approval_of(t, tasks)[2]
            if why:
                errs.append(f"{where}: its `approved: auto …` stamp doesn't hold: {why}; a human edits it "
                            f"back to proposed (status and approved:) and approves it, or drops it")
        # shared mode: only the clone that holds the branch can tell it's gone
        if (t.get("status") == "review" and not held_elsewhere(t)
                and not _gitops.branch_exists(_config.TASK_BRANCH + t["id"])):
            errs.append(f"{where}: in review but branch {_config.TASK_BRANCH}{t['id']} is gone, so there's nothing "
                        f"to accept; a human rejects it back to ready or closes it")
        if t.get("asking") and t.get("status") not in ("blocked", "done", "dropped"):
            errs.append(f"{where}: asking: is set but status is {t.get('status')}; a task waiting on an answer "
                        f"is blocked until a human runs `ergon answer {t['id']}`")
        if t.get("limited") and t.get("status") != "blocked":
            errs.append(f"{where}: limited: is set but status is {t.get('status')}; dispatch parks a task the usage "
                        f"limit stopped as blocked, and clears the mark when it resumes it")
        elif t.get("limited") and not limited_mark(t["limited"]):
            errs.append(f"{where}: limited: {t['limited']!r} isn't `<UTC time, {LIMITED_AT}> <step>` (step: worker, "
                        f"review, or fix/verify/conflict and a round number)")
        if t.get("answered") and t.get("status") not in ("ready", "done", "dropped"):
            errs.append(f"{where}: answered: is set but status is {t.get('status')}; `ergon answer` leaves it ready")
        if t.get("pin") and t.get("status") in ("done", "dropped") and warns is not None:
            warns.append(f"{where}: pin: {t['pin']} left on a {t['status']} task; clear it with `ergon pin {t['id']} clear`")
        if t.get("accept") and str(t["accept"]).lower() not in YES:
            errs.append(f"{where}: accept: {t['accept']!r} isn't `yes` (leave it out for no; config "
                        f"[[accept]] rules can't be opted out of)")
        if t.get("evidence") and str(t["evidence"]).lower() not in YES:
            errs.append(f"{where}: evidence: {t['evidence']!r} isn't `yes` (leave it out for no)")
        harden = _dispatch.harden_ids(t)  # a harden task (T-106)
        for i in harden:
            if not _dispatch.HARDEN_ID.fullmatch(i):
                errs.append(f"{where}: harden: {i!r} isn't an incident id (the file stem in <state>/runs/incidents)")
        if t.get("harden_layer") and t["harden_layer"] not in _dispatch.HARDEN_LAYERS:
            errs.append(f"{where}: harden_layer: {t['harden_layer']!r} isn't one of {', '.join(_dispatch.HARDEN_LAYERS)}")
        if t.get("harden_layer") and not harden:
            errs.append(f"{where}: harden_layer: is set but harden: names no incident")
        for x in _dispatch.suppress_ok_paths(t):  # T-160: a file, named on purpose
            if any(ch in x for ch in "*?["):
                errs.append(f"{where}: suppress_ok: {x!r} is a glob; it names files one by one (a glob exempts nothing)")
        if t.get("status") != "dropped":  # E-006: the options and the human's choice among them
            errs += [f"{where}: {why}" for why in options_problems(t)]
            if t.get("status") in NEEDS_APPROVAL and has_options(t) and options(t)[1] is None:
                errs.append(f"{where}: {t['status']}, but its ## Options has no `Chosen:` line: a human chooses "
                            f"(`ergon choose {t['id']} <N>`) before a task is approved, so edit it back to proposed "
                            f"(status and approved:), choose, and approve it again")
        if t.get("epic") and t["epic"] not in epics:
            errs.append(f"{where}: epic {t['epic']} doesn't exist")
        for d in t.get("depends") or []:
            if d not in tasks:
                errs.append(f"{where}: depends on missing {d}")
        if t.get("status") not in ("done", "dropped"):
            errs += [f"{where}: {why}" for why in protect_problems(t)]
    for e in epics.values():
        if e.get("status") not in EPIC_STATUSES:
            errs.append(f"{e['id']}: status {e.get('status')!r} not in {EPIC_STATUSES}")
        # shared mode: an open epic needs a signed approval of its current brief, stamp or no
        # stamp (epic_problem). Local mode has nothing to verify, so an unstamped one passes.
        why = apr.problem(e) if apr and e.get("status") == "open" else None
        if why:
            errs.append(f"{e['id']}: open, but its approval doesn't verify: {why}")
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
    from . import concepts as _concepts  # imports this module
    cerrs, cwarns = _concepts.lint(tasks)
    errs += cerrs
    if warns is not None:  # e.g. a missing depend the map suggests: worth a look, not a failure
        warns += cwarns
    return errs, f"{len(tasks)} tasks, {len(epics)} epics, {len(errs)} problem(s)"


def check(args):
    warns = []
    errs, summary = lint(warns)
    for w in warns:
        print(f"warning: {w}")
    for err in errs:
        print(err)
    for w in _dispatch.policy_warnings(load(_config.ITEMS)):
        print(w)
    print(summary)
    sys.exit(1 if errs else 0)
