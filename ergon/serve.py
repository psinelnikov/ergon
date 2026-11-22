"""`ergon serve`: a read-only web view of the board and of each task.

Standard library only: a ThreadingHTTPServer that renders HTML with its CSS and JS inlined
(serve.css, serve.js next to this file), no external assets.

  /            the board: the top tasks by priority (score, cod, cod_why) and when they were last
               graded, the newest review verdicts, open work by status then priority, epics with
               progress, what waits on a human (approval, acceptance, answers), and live dispatch state
  /metrics     charts and tables: stats, review, human latency, lead time, tokens; ?epic=&since=&until=
  /review      what waits for approval, read before approving: proposed tasks by tier, their
               priority ranking with each cod_why, a dependency map (inline SVG), a card per task,
               findings computed from files (missing citations and touches, empty Done when /
               tier_why / touches, dead depends), and the quiz `ergon quiz` last wrote, minus questions whose task has changed since
  /live.json   tasks `doing`: elapsed time, round, spend so far; the board polls it
  /e/E-NNN     one epic's replay timeline: a lane per task, a bar per phase from the journal,
               human waits hatched, a cost overlay, and a replay player (inline SVG, no libraries)
  /t/T-NNN     one task: its brief, frontmatter, every run and review from the history
               journal, transcript usage, links to the raw logs, `claude --resume` commands,
               and (with a concept map) the tasks it relates to, as `ergon related` lists them
  /runs/NAME   one file directly inside <state>/runs/, as plain text
  /runs/breakdown/NAME   an epic breakdown's log (listed on the epic page)
  /runs/T-NNN-evidence/NAME   a worker's evidence file (images by type, the rest plain text)

The read-only guarantee:
- GET and HEAD only. Every other method, known or not, gets 405.
- No route writes anything, runs git or launches claude. Transcript summaries are cached in
  memory, never in <state>/runs/transcripts.json. /review's quiz is written beforehand by
  `ergon quiz` (cli.py); the page only reads that file and scores answers in the browser.
- Files are served from <state>/runs/ only, by bare name (no slash, no leading dot), and only
  when the resolved path is still a regular file directly in that folder. Anything else is 404.
- Task text is untrusted: every value is HTML-escaped, and the brief's markdown subset
  (headings, lists, code, bold) is applied to already-escaped text. No raw HTML passes through.
  Pages carry a CSP whose script and style sources are a per-response nonce, so an escaping
  slip still can't run a script.
- The Host header must be an IP literal or `localhost`, so a DNS-rebinding page can't read the
  board through the browser of someone who has it open.
- Loopback by default. Any other --bind prints a warning: there is no login.
"""
import datetime
import hashlib
import html
import http.server
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import concepts as _concepts
from . import config as _config
from . import dispatch as _dispatch
from . import gitops as _gitops
from . import history as _history
from . import limits as _limits
from . import metrics as _metrics
from . import priority as _priority
from . import report as _report
from . import tasks as _tasks
from . import timeline as _timeline
from . import transcripts as _transcripts

DEFAULT_PORT = 4300
LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EV_NAME = LOG_NAME
EV_DIR = re.compile(r"T-\d{3,}-evidence")
TASK_ID = re.compile(r"T-\d{3,}")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}")
COST_EVENTS = ("worker_end", "review")
WARNING = ("ergon serve: WARNING: listening on {bind}, not loopback. There is no login: anyone who can "
           "reach this address reads every task brief, run log, cost and session ID. Bind only an "
           "interface you trust (a tailnet IP), never a public or shared network.")

_memo = {}  # transcript summaries by (path, size, mtime), for the life of the process
_memo_lock = threading.Lock()


# ---------- small helpers ----------

def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def is_loopback(host):
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_ok(header):
    """Is a Host header one a browser sends only when it was pointed at us directly: an IP
    literal or localhost, with or without a port? A DNS name is what a rebinding attack uses."""
    h = (header or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):
        name, _, rest = h[1:].partition("]")
        if rest and not re.fullmatch(r":\d{1,5}", rest):
            return False
    else:
        name, _, port = h.partition(":") if h.count(":") == 1 else (h, "", "")
        if port and not port.isdigit():
            return False
    name = name.rstrip(".")
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def cat_span(t):
    """The task's category as a badge; empty or unknown reads as feature."""
    return f'<span class="tier cat">{esc(_tasks.category(t))}</span>'


def tier_key(t):
    tier = t.get("tier")
    return (_tasks.TIERS.index(tier) if tier in _tasks.TIERS else len(_tasks.TIERS), t["id"])


def prio_key(t, tasks):
    """_priority.sort_key, falling back to tier order for a task whose tier has no size."""
    try:
        return _priority.sort_key(t, tasks)
    except (KeyError, ValueError):
        return (3, 0, *tier_key(t))


def prio_score(t, tasks):
    try:
        return _priority.score(t, tasks)
    except KeyError:
        return None


GRADE_FILE = re.compile(r"grade-\d{8}-\d{6}\.json")
PRIORITY_TOP = 5
RECENT_REVIEWS = 8


def latest_grade():
    """(name, record) of the newest `ergon grade` record under <state>/runs/grade/, or (None, None)."""
    folder = _config.RUNS / "grade"
    try:
        names = sorted(p.name for p in folder.iterdir() if GRADE_FILE.fullmatch(p.name) and p.is_file())
    except OSError:
        return None, None
    for n in reversed(names):
        try:
            d = json.loads((folder / n).read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return n, d
    return None, None


def grade_line():
    """A muted line saying when the newest grade ran, by which model, over how many tasks, at
    what cost; or how to run one."""
    name, d = latest_grade()
    if d is None:
        return '<p class=muted>nothing graded yet: <code>ergon grade</code> sets each open task\'s cod</p>'
    scores = d.get("written") if isinstance(d.get("written"), (list, dict)) else d.get("scores")
    n = len(scores) if isinstance(scores, (list, dict)) else None
    cost = d.get("cost")
    return (f'<p class=muted>last graded {esc(d.get("generated"))}'
            + (f' by {esc(d.get("model"))}' if d.get("model") else "")
            + (f' · {n} task(s)' if n is not None else "")
            + (f' · {money(cost)}' if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "")
            + f' · <code>{esc((_config.RUNS / "grade" / name).relative_to(_config.HOME).as_posix())}</code>'
            + ' · <code>ergon grade</code> grades new and changed tasks</p>')


def priority_table(ts, tasks, anchor=False):
    """Tasks ranked as `ergon next` would take them: score, cod (or ungraded), unblocks, cod_why.
    anchor: link each ID to its card on the same page instead of its task page."""
    o = ['<div class=table><table class=prio><thead><tr><th>#</th><th>task</th><th>tier</th><th>score</th>'
         '<th>cod</th><th class=hide-narrow>unblocks</th><th>why it matters</th></tr></thead><tbody>']
    for i, t in enumerate(ts, 1):
        sc = prio_score(t, tasks)
        graded = t.get("cod") not in (None, "")
        cod = f"{_priority.cod(t):g}" if graded else '<span class=muted>—</span>'
        pin = str(t.get("pin") or "").strip()
        why = (link_ids(esc(str(t.get("cod_why") or "")), tasks) or '<span class=muted>no cod_why</span>'
               if graded else '<span class=muted>not graded (neutral 50)</span>')
        href = f'#{esc(t["id"])}' if anchor else f'/t/{esc(t["id"])}'
        o.append(f'<tr><td class=num>{i}</td><td class="id mono"><a href="{href}">{esc(t["id"])}</a></td>'
                 f'<td><span class=tier>{esc(t.get("tier"))}</span></td>'
                 f'<td class=num>{"—" if sc is None else f"{sc:.1f}"}</td><td class=num>{cod}</td>'
                 f'<td class="num hide-narrow">{_priority.unblocks(t, tasks) or ""}</td>'
                 f'<td class=why><b>{esc(t.get("title"))}</b>'
                 + (f' <span class="pin">pin: {esc(pin)}</span>' if pin else "")
                 + f'<br>{why}</td></tr>')
    o.append("</tbody></table></div>")
    return "".join(o)


def last_review(trail):
    """' · review <verdict> (round N)' for a task's newest review row, or ''."""
    r = next((r for r in reversed(trail) if r.get("event") == "review"), None)
    if not r:
        return ""
    v = str(r.get("verdict") or "")
    return (f' <span class=flags>last review <b class="v-{esc(v)}">{esc(v)}</b>'
            f' (round {esc(r.get("round") or 1)})</span>')


def reviews_table(rows, tasks, limit=RECENT_REVIEWS):
    """The newest `review` journal rows, newest first: what the reviewer said about each round."""
    revs = [r for r in rows if r.get("event") == "review"][-limit:][::-1]
    if not revs:
        tiers = ", ".join(_config.REVIEW)
        return ('<p class=muted>no review has run yet: dispatch reviews a task\'s branch when '
                '<code>review</code> in .ergon.toml names its tier'
                + (f' (now: {esc(tiers)})' if tiers else " (now: none)") + '</p>')
    o = ['<div class=table><table><thead><tr><th>at</th><th>task</th><th>round</th><th>verdict</th>'
         '<th class=hide-narrow>reviewer</th><th>reason</th><th class=hide-narrow>log</th></tr></thead><tbody>']
    for r in revs:
        v = str(r.get("verdict") or "")
        tid = str(r.get("task") or "")
        task = (f'<a href="/t/{esc(tid)}">{esc(tid)}</a>' if tid in tasks else esc(tid))
        title = tasks.get(tid, {}).get("title")
        reason = link_ids(esc(r.get("reason") or ""), tasks) or '<span class=muted>—</span>'
        o.append(f'<tr><td class="muted mono">{esc(r.get("at"))}</td><td class="id mono">{task}</td>'
                 f'<td class=num>{esc(r.get("round") or 1)}</td><td class="v-{esc(v)}"><b>{esc(v)}</b></td>'
                 f'<td class=hide-narrow>{esc(r.get("reviewer"))}</td>'
                 f'<td class=why>{"<b>" + esc(title) + "</b><br>" if title else ""}{reason}</td>'
                 f'<td class=hide-narrow>{log_link(r.get("log"))}</td></tr>')
    o.append("</tbody></table></div>")
    return "".join(o)


def parse_at(at):
    try:
        return datetime.datetime.fromisoformat(str(at)).timestamp()
    except (TypeError, ValueError):
        return None


def money(x):
    try:
        return f"${float(x or 0):.2f}"
    except (TypeError, ValueError):
        return "$?"


def secs(x):
    try:
        s = int(float(x))
    except (TypeError, ValueError):
        return ""
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s" if m else f"{s}s"


def link_ids(text_html, tasks):
    """T-NNN in already-escaped text, linked when the task exists."""
    return TASK_ID.sub(lambda m: f'<a href="/t/{m[0]}">{m[0]}</a>' if m[0] in tasks else m[0], text_html)


def log_href(path):
    """The /runs/ URL of a history `log` path (relative to the main checkout), or None when it
    isn't a file directly in <state>/runs/."""
    if not path:
        return None
    p = _config.HOME / str(path)
    parent = p.parent.resolve()
    if parent == (_config.RUNS / "breakdown").resolve() and LOG_NAME.fullmatch(p.name):
        return "/runs/breakdown/" + urllib.parse.quote(p.name)
    if parent != _config.RUNS.resolve() or not LOG_NAME.fullmatch(p.name):
        return None
    return "/runs/" + urllib.parse.quote(p.name)


def log_link(path):
    href = log_href(path)
    name = Path(str(path)).name if path else ""
    return f'<a href="{esc(href)}">{esc(name)}</a>' if href else esc(name)


def worktree_of(rows, tid):
    """The task worktree the latest `dispatched` row names, else the default place."""
    for r in reversed(rows):
        if r.get("event") == "dispatched" and r.get("worktree"):
            return _config.HOME / str(r["worktree"])
    return _config.WORKTREES / tid


def summary(session, wt):
    """Transcript usage of one session run in wt (its real path tried too), or None."""
    if not SESSION.fullmatch(str(session or "")):  # it names a file: no path parts from the journal
        return None
    with _memo_lock:
        for cwd in dict.fromkeys([str(wt), os.path.realpath(str(wt))]):
            s = _transcripts.session_summary(session, cwd, _memo)
            if s:
                return s
    return None


def newest_session(wt, since_ts):
    """The session of the newest transcript in wt's project folder written since since_ts."""
    best = None
    for cwd in dict.fromkeys([str(wt), os.path.realpath(str(wt))]):
        d = os.path.join(_transcripts.root(), _transcripts.project_name(cwd))
        try:
            names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
        except OSError:
            continue
        for n in names:
            try:
                m = os.stat(os.path.join(d, n)).st_mtime
            except OSError:
                continue
            if (since_ts is None or m >= since_ts - 5) and (best is None or m > best[0]):
                best = (m, n[:-len(".jsonl")])
    return best[1] if best else None


# ---------- the data ----------

def asks(tasks):
    """Tasks parked on a worker's question, waiting for a human's reply: what the CLI review lists."""
    return sorted((t for t in tasks.values() if _tasks.is_asking(t)), key=lambda t: t["id"])


def needs_choice(t):
    """A proposed task whose `## Options` has no usable choice yet: approve waits on a human's pick."""
    return t["status"] == "proposed" and _tasks.choice_problem(t) is not None


def choices(tasks):
    return sorted((t for t in tasks.values() if needs_choice(t)), key=lambda t: t["id"])


def counts(tasks):
    return {"approval": sum(t["status"] == "proposed" for t in tasks.values()),
            "choice": len(choices(tasks)),
            "acceptance": sum(t["status"] == "review" for t in tasks.values()),
            "answer": len(asks(tasks))}


def options_html(t):
    """A task's `## Options` as a numbered list, none recommended: every chosen option marked with
    who and when, a lens-format section showing its crux and each option's fields in LENSES order,
    and commands to copy (one per lens, a combination, `--own`) while nothing is chosen.
    '' without options. Read-only: the page takes no input."""
    if not _tasks.has_options(t):
        return ""
    opts, chosen = _tasks.options(t)
    tid = str(t["id"])
    o = ["<h3>Options</h3>"]
    if opts.crux is not None:
        o.append(f"<p class=crux>Crux: {esc(opts.crux)}</p>")
    o.append("<ol class=options>")
    for n, text in enumerate(opts, 1):
        is_chosen = bool(chosen) and bool(chosen["text"]) and (
            text.lens in chosen["lenses"] if isinstance(text, _tasks.LensOption) else chosen["option"] == n)
        o.append(f'<li class="option{" chosen" if is_chosen else ""}">{esc(text)}'
                 + (f' <span class="tier chosen-mark">chosen by {esc(chosen["by"])} {esc(chosen["date"])}</span>'
                    if is_chosen else ""))
        if isinstance(text, _tasks.LensOption):
            o.append("<ul class=fields>" + "".join(f"<li>{label}: {esc(getattr(text, key))}</li>"
                                                  for key, label in _tasks.LENS_FIELDS) + "</ul>")
        o.append("</li>")
    o.append("</ol>")
    if chosen and chosen["option"] == "own":
        o.append(f'<p class="option chosen">own: {esc(chosen["text"])} '
                 f'<span class="tier chosen-mark">chosen by {esc(chosen["by"])} {esc(chosen["date"])}</span></p>')
    if chosen is None and t["status"] == "proposed" and TASK_ID.fullmatch(tid):
        o.append("<p class=muted>This page chooses nothing. From your own terminal:</p>")
        if opts.crux is not None:
            cmds = [f"ergon choose {tid} {x.lens}" for x in opts]
            if len(opts) > 1:
                cmds.append(f"ergon choose {tid} {opts[0].lens} {opts[1].lens}")
        else:
            cmds = [f"ergon choose {tid} {n}" for n in range(1, len(opts) + 1)]
        cmds.append(f'ergon choose {tid} --own "<your words>"')
        o += [f"<code class=cmd>{esc(c)}</code>" for c in cmds]
    return "".join(o)


def by_task(rows):
    out = {}
    for r in rows:
        if r.get("task"):
            out.setdefault(r["task"], []).append(r)
    return out


def current_dispatch(rows):
    """A task's rows since its latest first-round `dispatched`: the dispatch now running."""
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("event") == "dispatched" and not rows[i].get("round"):
            return rows[i:]
    return []


def live(tasks, rows):
    """One dict per `doing` task: since_ts (when its current worker or round started), round,
    spent (finished workers' and reviewers' cost in this dispatch), tokens (its newest
    transcript so far, or None), last (the latest journal event)."""
    mine = by_task(rows)
    out = []
    for t in sorted((t for t in tasks.values() if t["status"] == "doing"), key=lambda t: t["id"]):
        trail = mine.get(t["id"], [])
        cur = current_dispatch(trail)
        starts = [r for r in cur if r.get("event") in ("dispatched", "resumed")]
        if not starts:  # moved to doing by hand
            starts = [r for r in trail if r.get("event") == "status" and r.get("to") == "doing"][-1:]
        since = parse_at(starts[-1].get("at")) if starts else None
        rnd = max([int(r.get("round") or 1) for r in cur if isinstance(r.get("round") or 1, int)] or [1])
        spent = 0.0
        for r in cur:
            if r.get("event") in COST_EVENTS:
                try:
                    spent += float(r.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
        tokens = None
        if cur:
            last_start = starts[-1] if starts else {}
            session = last_start.get("session") if last_start.get("event") == "resumed" else None
            session = session or newest_session(worktree_of(trail, t["id"]), since)
            s = summary(session, worktree_of(trail, t["id"])) if session else None
            if s:
                tokens = sum(s["tokens"].get(k, 0) for k in ("input", "output", "cache_read", "cache_write"))
        last = cur[-1] if cur else (trail[-1] if trail else None)
        out.append({"id": t["id"], "title": t.get("title", ""), "tier": t.get("tier", ""),
                    "epic": t.get("epic") or None, "since_ts": since, "round": rnd, "spent": round(spent, 4),
                    "tokens": tokens,
                    "last": (last["event"] + (f" {last['verdict']}" if last.get("verdict") else "")) if last else None})
    return out


# ---------- markdown, the safe subset ----------

def inline(text, tasks):
    """One line of untrusted text: escaped, then `code`, **bold** and task links."""
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]+`)", text)):
        if i % 2:
            out.append(f"<code>{esc(part[1:-1])}</code>")
        else:
            s = esc(part)
            s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
            out.append(link_ids(s, tasks))
    return "".join(out)


def markdown(text, tasks):
    """The brief as HTML: headings, bullet and numbered lists, fenced code, paragraphs. Every
    character of the input is escaped first; no tag, attribute or URL from it survives."""
    out, para, items, kind, code = [], [], [], None, None

    def flush():
        nonlocal para, items, kind
        if para:
            out.append("<p>" + "<br>".join(inline(l, tasks) for l in para) + "</p>")
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{inline(i, tasks)}</li>" for i in items) + f"</{kind}>")
        para, items, kind = [], [], None

    for line in text.splitlines():
        if code is not None:
            if line.strip().startswith("```"):
                out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush()
            code = []
            continue
        h = re.match(r"(#{1,6})\s+(.*)", line)
        b = re.match(r"\s*[-*+]\s+(.*)", line)
        n = re.match(r"\s*\d+[.)]\s+(.*)", line)
        if h:
            flush()
            lvl = min(len(h[1]) + 1, 6)
            out.append(f"<h{lvl}>{inline(h[2], tasks)}</h{lvl}>")
        elif b or n:
            want = "ul" if b else "ol"
            if para or (kind and kind != want):
                flush()
            kind = want
            items.append((b or n)[1])
        elif not line.strip():
            flush()
        elif items and line[:1].isspace():
            items[-1] += " " + line.strip()
        else:
            if items:
                flush()
            para.append(line)
    if code is not None:
        out.append("<pre><code>" + esc("\n".join(code)) + "</code></pre>")
    flush()
    return "\n".join(out)


# ---------- pages ----------

def asset(name):
    return (_config.PKG / name).read_text()


def page(title, body, nonce, script=False):
    """script: False, True (serve.js, the board's live table) or the name of another asset."""
    js = f'<script nonce="{nonce}">{asset("serve.js" if script is True else script)}</script>' if script else ""
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            '<meta name=viewport content="width=device-width, initial-scale=1">'
            '<meta name=color-scheme content="light dark">'
            f"<title>{esc(title)}</title><style nonce=\"{nonce}\">{asset('serve.css')}</style></head>"
            f"<body><main>{body}</main>{js}</body></html>")


def bar(done, total):
    """A progress bar as SVG: its sizes are attributes, which the CSP allows, unlike a style."""
    w = round(90 * done / total) if total else 0
    return (f'<svg class=bar width=90 height=8 viewBox="0 0 90 8" aria-hidden=true><rect class=bg width=90 height=8 rx=4 />'
            f'<rect class=fg width={w} height=8 rx=4 /></svg>')


def park_mark(t):
    """`limited until HH:MM` / `asking` for a task dispatch parked, else ""."""
    lim = _tasks.limited(t)
    if lim:
        return f"limited until {_limits.clock(lim[0])}"
    return "asking" if _tasks.is_asking(t) else ""


def task_li(t, tasks, extra=""):
    flags = []
    if park_mark(t):
        flags.append(f'<span class=park>{esc(park_mark(t))}</span>')
    if t.get("epic"):
        flags.append(f'<a href="/e/{esc(t["epic"])}">{esc(t["epic"])}</a>')
    if who := _tasks.held_elsewhere(t):
        flags.append(f"held on {esc(who)}")
    if t["status"] == "ready" and not _tasks.deps_done(t, tasks):
        waits = [d for d in t.get("depends") or [] if tasks.get(d, {}).get("status") != "done"]
        flags.append("waits on " + link_ids(esc(",".join(waits)), tasks))
    tail = f' <span class=flags>({"; ".join(flags)})</span>' if flags else ""
    sc = prio_score(t, tasks)
    if sc is not None:
        n = _priority.unblocks(t, tasks)
        pin = str(t.get("pin") or "").strip()
        cod = f"cod {_priority.cod(t):g}" if t.get("cod") not in (None, "") else "not graded"
        why = str(t.get("cod_why") or "").strip()
        tail += (f' <span class="score" title="{esc(why or cod)}">p {sc:.0f}</span>'
                 f' <span class=flags>{esc(cod)}</span>'
                 + (f' <span class=flags>unblocks {n}</span>' if n > 0 else "")
                 + (f' <span class="pin">pin: {esc(pin)}</span>' if pin else ""))
    return (f'<li><a class=id href="/t/{esc(t["id"])}">{esc(t["id"])}</a>'
            f'<span class=tier>{esc(t.get("tier"))}</span>{cat_span(t)}<span class=title>{esc(t.get("title"))}{tail}{extra}</span></li>')


def live_rows(rows, now):
    out = []
    for r in rows:
        el = secs(now - r["since_ts"]) if r["since_ts"] else "—"
        tok = "—" if r["tokens"] is None else format(r["tokens"], ",")
        out.append(f'<tr><td class=id><a href="/t/{esc(r["id"])}">{esc(r["id"])}</a></td><td>{esc(r["tier"])}</td>'
                   f'<td class=num>{esc(el)}</td><td class=num>{r["round"]}</td><td class=num>{money(r["spent"])}</td>'
                   f'<td class="num hide-narrow">{tok}</td>'
                   f'<td class=hide-narrow>{esc(r["last"] or "—")}</td><td class=why>{esc(r["title"])}</td></tr>')
    return "".join(out)


def board_page(nonce):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    rows = _history.read()
    trails = by_task(rows)
    c = counts(tasks)
    now = time.time()
    lv = live(tasks, rows)
    b = [f'<div class=top><h1>{esc(_config.HOME.name)}</h1><span class=crumb>ergon board · read-only · <a href="/metrics">metrics</a></span></div>']
    b.append("<div class=counts>")
    for key, label, anchor in (("approval", "awaiting approval", "#proposed"),
                               ("choice", "awaiting choice", "#choice"),
                               ("acceptance", "awaiting acceptance", "#review"),
                               ("answer", "awaiting answer", "#answer")):
        b.append(f'<a class="count{" hot" if c[key] else ""}" href="{anchor}"><b id="count-{key}">{c[key]}</b>{label}</a>')
    b.append("</div>")
    b.append('<h2>Live <span id=live-at class=muted></span></h2>'
             f'<p id=live-empty{" hidden" if lv else ""}>no task is doing</p>'
             f'<div class=table><table id=live-table{"" if lv else " hidden"}><thead><tr><th>task</th><th>tier</th>'
             '<th>elapsed</th><th>round</th><th>spent</th><th class=hide-narrow>tokens</th>'
             '<th class=hide-narrow>last</th><th>title</th></tr></thead>'
             f'<tbody id=live-body>{live_rows(lv, now)}</tbody></table></div>')
    open_ = [t for t in tasks.values() if t["status"] not in ("done", "dropped")]
    waiting = sorted((t for t in open_ if t["status"] in ("proposed", "ready", "blocked")
                      and prio_score(t, tasks) is not None), key=lambda t: prio_key(t, tasks))
    if waiting:
        top = waiting[:PRIORITY_TOP]
        b.append(f'<h2 id=priority>Priority (top {len(top)} of {len(waiting)} not started)</h2>'
                 + priority_table(top, tasks) + grade_line())
    b.append(f'<h2 id=reviews>Reviews</h2>{reviews_table(rows, tasks)}')
    for status in _tasks.TASK_STATUSES:
        group = sorted((t for t in open_ if t["status"] == status), key=lambda t: prio_key(t, tasks))
        if group:
            more = ' <a class=more href="/review">review them →</a>' if status == "proposed" else ""
            b.append(f'<h2 id="{status}">{esc(status.upper())} ({len(group)}){more}</h2><ul class=tasks>')
            b.extend(task_li(t, tasks, last_review(trails.get(t["id"], [])) if status == "review" else "")
                     for t in group)
            b.append("</ul>")
    parents = [(t, _tasks.follow_ups(t, tasks)) for t in sorted(tasks.values(), key=lambda t: t["id"])
               if t["status"] == "done"]
    parents = [(t, f) for t, f in parents if f]
    if parents:
        b.append(f'<h2 id=followups>Done, follow-ups open ({len(parents)})</h2><ul class=tasks>')
        b.extend(task_li(t, tasks, f' <span class=flags>{esc(_tasks.status_text(t, tasks))}</span>') for t, _ in parents)
        b.append("</ul>")
    chose = choices(tasks)
    if chose:
        b.append(f'<h2 id=choice>Awaiting choice ({len(chose)})</h2><ul class=tasks>')
        b.extend(task_li(t, tasks, ' <span class=flags>options, no choice yet</span>') for t in chose)
        b.append("</ul>")
    asked = asks(tasks)
    if asked:
        b.append(f'<h2 id=answer>Awaiting answer ({len(asked)})</h2><ul class=tasks>')
        for t in asked:
            qs = "".join(f"<br>Q{n} {esc(q)}" + (f' <span class=muted>[{esc(" | ".join(o))}]</span>' if o else "")
                         for n, (q, o) in enumerate(_dispatch.asked(t)[0], 1))
            b.append(task_li(t, tasks, f"<span class=flags>{qs}</span>" if qs else ""))
        b.append("</ul>")
    if epics:
        b.append('<h2>Epics</h2><div class=table><table><thead><tr><th>epic</th><th>status</th><th>progress</th>'
                 '<th class=hide-narrow>proposed</th><th>title</th></tr></thead><tbody>')
        for e in epics.values():
            kids = [t for t in tasks.values() if t.get("epic") == e["id"] and t["status"] != "dropped"]
            done = sum(t["status"] == "done" for t in kids)
            pending = sum(t["status"] == "proposed" for t in kids)
            note = " <span class=flags>needs breakdown</span>" if not kids and e.get("status") == "open" else ""
            b.append(f'<tr><td class="mono id"><a href="/e/{esc(e["id"])}">{esc(e["id"])}</a></td><td>{esc(e.get("status"))}</td>'
                     f'<td class=num>{bar(done, len(kids))} {done}/{len(kids)}</td>'
                     f'<td class="num hide-narrow">{pending or ""}</td><td>{esc(e.get("title"))}{note}</td></tr>')
        b.append("</tbody></table></div>")
    by_tier = {tier: sum(t.get("tier") == tier for t in open_) for tier in _tasks.TIERS}
    b.append("<footer>open by tier: " + esc("  ".join(f"{k}:{v}" for k, v in by_tier.items())) + "</footer>")
    return page(f"ergon · {_config.HOME.name}", "".join(b), nonce, script=True)


def journal_block(eid, tasks):
    """The epic's newest journal entry as HTML (model output: escaped like task text), with a
    muted line saying when, by which model and at what cost; or where to make one."""
    _, d = _gitops.latest_journal(eid)
    if not d:
        return f'<p class=muted>no journal entry yet: <code>ergon journal {esc(eid)}</code></p>'
    cost = d.get("cost")
    return (f'<div class=journal>{markdown(d["body"], tasks)}</div>'
            f'<p class=muted>journal generated {esc(d.get("generated"))} by {esc(d.get("model"))}'
            + (f' · {money(cost)}' if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "") + "</p>")


def breakdown_block(eid):
    """The epic's breakdown runs (history `breakdown` rows): verdict, cost, time, log link. '' if none."""
    rows = [r for r in _history.read() if r.get("event") == "breakdown" and r.get("epic") == eid]
    if not rows:
        return ""
    cost = sum(r["cost"] for r in rows if isinstance(r.get("cost"), (int, float)) and not isinstance(r["cost"], bool))
    items = "".join(
        f'<li><span class="v-{esc(r.get("verdict"))}">{esc(r.get("verdict"))}</span> · {money(r.get("cost"))} · '
        f'{esc(secs(r.get("seconds")))} · <span class=muted>{esc(r.get("at"))}</span>'
        + (f' · log {log_link(r["log"])}' if r.get("log") else "") + "</li>" for r in rows)
    return f"<h2>Breakdown runs</h2><p>{len(rows)} run(s), {money(cost)}</p><ul>{items}</ul>"


def epic_page(eid, nonce, now=None):
    epics = _tasks.load(_config.EPICS)
    e = epics.get(eid)
    if not e:
        return None
    tasks = _tasks.load(_config.ITEMS)
    rows = _history.read()
    trails = by_task(rows)

    def tokens(r):
        s = summary(r.get("session"), worktree_of(trails.get(r.get("task"), []), r.get("task")))
        return sum(s["tokens"].get(k, 0) for k in ("input", "output", "cache_read", "cache_write")) if s else None

    tl = _timeline.build(rows, tasks, eid, now or time.time(), tokens)
    b = [f'<div class=top><a href="/">← board</a><span class=crumb>{esc(_config.HOME.name)}</span></div>',
         f'<h1>{esc(eid)} {esc(e.get("title"))}</h1>']
    jb = journal_block(eid, tasks) + breakdown_block(eid)
    if not tl["lanes"]:
        b.append(jb)
        b.append("<p class=muted>no task of this epic has a journal yet</p>")
        return page(f"{eid} · {e.get('title', '')}", "".join(b), nonce)
    human, machine = _timeline.totals(tl)
    total = human + machine
    spent = tl["costs"][-1][3] if tl["costs"] else 0
    b.append(jb)
    b.append(f'<p>{len(tl["lanes"])} task(s) · {esc(secs(tl["t1"] - tl["t0"]))} end to end · {money(spent)} · '
             f'waiting on a human {esc(secs(human))}'
             + (f' ({round(100 * human / total)}% of lane time)' if total else "") + "</p>")
    b.append('<div class=player><button id=play type=button>▶ replay</button>'
             '<label>speed <select id=speed><option value=1>×1<option value=2>×2<option value=4>×4'
             '<option value=8>×8<option value=16>×16</select></label>'
             '<input id=scrub type=range min=0 max=1000 value=1000 aria-label="position">'
             '<span id=clock class=muted></span></div>'
             '<div class=muted>×1 plays the whole epic in a minute.</div>')
    b.append(f'<div class=tlwrap>{_timeline.svg(tl)}</div><div id=tip class=tip hidden></div>')
    b.append('<p class=legend><span class="sw k-worker"></span>worker <span class="sw k-verify"></span>verify '
             '<span class="sw k-review"></span>review <span class="sw o-fail"></span>failed '
             '<span class="sw o-ok"></span>passed / landed <span class="sw human"></span>waiting on a human '
             '(approval, answer, accept, blocked) <span class="sw k-queued"></span>queued</p>')
    return page(f"{eid} · {e.get('title', '')}", "".join(b), nonce, script="epic.js")


def run_card(label, r, where, tasks):
    """One run: tier, verdict, cost, time, reason, log, transcript usage, resume command."""
    v = str(r.get("verdict") or "")
    head = (f'<div class=head><b>{esc(label)}</b><span class=tier>{esc(r.get("tier") or "?")}</span>'
            f'<span class="v-{esc(v)}">{esc(v)}</span><span>{money(r.get("cost"))}</span>'
            f'<span>{esc(secs(r.get("seconds")))}</span><span class=muted>{esc(r.get("at"))}</span></div>')
    body = ""
    if r.get("reason"):
        body += f'<div class=why>{link_ids(esc(r["reason"]), tasks)}</div>'
    if r.get("log"):
        body += f"<div>log {log_link(r['log'])}</div>"
    sid = r.get("session")
    if sid:
        s = summary(sid, where)
        if s:
            body += "<ul>" + "".join(f"<li>{esc(l)}</li>" for l in _transcripts.describe(s)) + "</ul>"
        else:
            body += "<div class=muted>transcript not found</div>"
        cmd = f"cd {shlex.quote(str(where))} && claude --resume {shlex.quote(str(sid))}"
        gone = "" if Path(where).is_dir() else " <span class=muted>(worktree since removed)</span>"
        body += f"<code class=cmd>{esc(cmd)}</code>{gone}"
    return f"<div class=run>{head}{body}</div>"


def unjournaled(tid, trail, tier=None):
    """Runs of tid whose <state>/runs/<ID>-<time>[-suffix].json log no journal row names: runs
    from before the history existed. Read from the log itself: verdict, cost, session."""
    named = {Path(str(r["log"])).name for r in trail if r.get("log")}
    out = []
    for p in sorted(_config.RUNS.glob(f"{tid}-*.json")):
        m = _report.RUN_LOG.match(p.name)
        if not m or m["id"] != tid or p.name in named:
            continue
        kind = (p.stem.rsplit("-", 1)[1] if re.search(r"-(review\d*|fix\d+)$", p.stem) else "worker")
        try:
            d = json.loads(p.read_text(errors="replace"))
        except (OSError, ValueError):
            d = None
        if not isinstance(d, dict):
            d = {}
        text = str(d.get("result") or "")
        if kind.startswith("review"):
            last = next((l for l in reversed(text.strip().splitlines()) if l.strip()), "")
            got = re.match(r"^\W*REVIEW:\s*(pass|fail)\b", last.strip(" *`"))
            verdict = got[1] if got and not d.get("is_error") else "fail"
        else:
            got = re.findall(r"^\W*RESULT:\s*(done|blocked)\b", text, re.M)
            verdict = got[-1] if got and not d.get("is_error") else "blocked"
        suffix = p.stem.rsplit("-", 1)[1]
        out.append({"kind": kind, "verdict": verdict, "cost": d.get("total_cost_usd"),
                    "tier": suffix if suffix in _config.WORKER_TIERS else None if kind.startswith("review") else tier,
                    "session": d.get("session_id") if not kind.startswith("review") else None,
                    "at": f"{m['date']}-{m['m']}-{m['d']}", "log": p.relative_to(_config.HOME).as_posix()})
    return out


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}  # not .svg: it can carry script


def evidence_section(tid, trail):
    """The worker's evidence from the latest `evidence` row: images inline, other files linked
    through /runs/; every caption and name escaped. '' when there is none."""
    rows = [r for r in trail if r.get("event") == "evidence"]
    if not rows:
        return ""
    files, refused = rows[-1].get("files") or [], rows[-1].get("refused") or []
    d = _config.RUNS / f"{tid}-evidence"
    out = []
    for f in files:
        name = str(f.get("name") or "") if isinstance(f, dict) else ""
        if not name or not (d / name).is_file() or not EV_NAME.fullmatch(name):
            continue
        href = f"/runs/{urllib.parse.quote(d.name)}/{urllib.parse.quote(name)}"
        cap = esc(f.get("caption") or "(no caption)")
        if Path(name).suffix.lower() in IMAGE_TYPES:
            out.append(f'<figure><a href="{href}"><img src="{href}" alt="{cap}" style="max-width:100%"></a>'
                       f"<figcaption>{cap}</figcaption></figure>")
        else:
            out.append(f'<p><a href="{href}">{esc(name)}</a> · {cap}</p>')
    out += [f'<p class=muted>refused: {esc(r)}</p>' for r in refused]
    return "<h2>Evidence</h2>" + "".join(out) if out else ""


def related_block(t, tasks):
    """The task page's Related section (concepts.links), as HTML parts: nothing without a
    `concepts` key, a note when the map doesn't load or the task has no tags."""
    if not _config.CONCEPTS:
        return []
    cmap, errs = _concepts.read_map()
    b = ["<h2>Related</h2>"]
    if errs:
        return b + [f'<p class=muted>the concept map does not load ({esc(errs[0])}); `ergon check` lists why</p>']
    ls = _concepts.links(t, tasks, cmap)
    note = _concepts.stamp_note(t, cmap)
    if not ls["components"]:
        return b + [f'<p class=muted>no concepts: run <code>ergon tag {esc(t["id"])}</code></p>']
    b.append(f'<p>concepts: <code>{esc(", ".join(ls["components"]))}</code>'
             + (f' <span class=muted>(stale: {esc(note)})</span>' if note else "") + "</p>")

    def li(o, extra):
        return (f'<li><a class=id href="/t/{esc(o["id"])}">{esc(o["id"])}</a>'
                f'<span class="status st-{esc(o.get("status"))}">{esc(o.get("status"))}</span>'
                f'<span class=title>{esc(o.get("title"))} <span class=flags>{esc(extra)}</span></span></li>')

    for head, rows in (("Open, sharing a component", [li(o, ", ".join(c)) for o, c in ls["sharing"]]),
                       ("Open, sharing only a concern", [li(o, ", ".join(c)) for o, c in ls["concern"]]),
                       ("Builds on (done, by component overlap)",
                        [li(o, f"{j:.2f}: " + ", ".join(c)) for o, j, c in ls["builds_on"]])):
        b.append(f'<p class=muted>{esc(head)}</p>')
        b.append('<ul class=tasks>' + "".join(rows) + "</ul>" if rows else "<p>none</p>")
    if ls["missing"]:
        b.append('<p class=muted>Missing depends</p><ul class=tasks>')
        for c, intro in ls["missing"]:
            o = tasks[intro]
            b.append(li(o, f"introduces {c}, which this task uses, but isn't in its depends:"))
        b.append("</ul>")
    return b


def task_page(tid, nonce):
    tasks = _tasks.load(_config.ITEMS)
    t = tasks.get(tid)
    if not t:
        return None
    trail = by_task(_history.read()).get(tid, [])
    b = [f'<div class=top><a href="/">← board</a><span class=crumb>{esc(_config.HOME.name)}</span></div>',
         f'<h1>{esc(tid)} {esc(t.get("title"))}</h1>',
         f'<p><span class="status st-{esc(t["status"])}">{esc(t["status"])}</span> · '
         f'<span class=tier>{esc(t.get("tier"))}</span> <span class=tier>{esc(_tasks.category(t))}</span>'
         + (f' · <span class=park>{esc(park_mark(t))}</span>' if park_mark(t) else "")
         + (f' · <span class=flags>held on {esc(who)}</span>' if (who := _tasks.held_elsewhere(t)) else "") + '</p>']

    if _tasks.is_asking(t):
        b.append("<h2>Open questions</h2>")
        for n, (q, o) in enumerate(_dispatch.asked(t)[0], 1):
            b.append(f"<p>Q{n} {esc(q)}</p>")
            if o:
                b.append("<ul class=options>" + "".join(f"<li>{esc(x)}</li>" for x in o) + "</ul>")

    fus = _tasks.follow_ups(t, tasks, open_only=False)
    if fus:
        b.append(f'<h2>Follow-ups</h2><p>{esc(_tasks.status_text(t, tasks))}</p><ul class=tasks>')
        for c in fus:
            b.append(f'<li><a class=id href="/t/{esc(c["id"])}">{esc(c["id"])}</a>'
                     f'<span class="status st-{esc(c["status"])}">{esc(c["status"])}</span>{cat_span(c)}'
                     f'<span class=title>{esc(c.get("title"))}</span></li>')
        b.append("</ul>")

    b += related_block(t, tasks)

    # priority
    sc = prio_score(t, tasks)
    if sc is not None:
        n = _priority.unblocks(t, tasks)
        size = _priority.SIZE[t.get("tier")]
        graded = str(t.get("graded") or "").strip()
        pin = str(t.get("pin") or "").strip()
        has_cod = t.get("cod") not in (None, "")
        parts = (f'cod {_priority.cod(t):g}' if has_cod else "not graded (neutral 50)")
        parts += f", unblocks {n}, size {size}"
        row_ = [f"<tr><th>score</th><td>{sc:.1f} <span class=muted>({esc(parts)})</span></td></tr>"]
        if t.get("cod_why"):
            row_.append(f'<tr><th>cod_why</th><td class=why>{link_ids(esc(str(t["cod_why"])), tasks)}</td></tr>')
        if graded:
            row_.append(f"<tr><th>graded</th><td>{esc(graded)}</td></tr>")
        if pin:
            row_.append(f"<tr><th>pin</th><td>{esc(pin)}</td></tr>")
        b.append("<h2>Priority</h2><div class=table><table class=prio><tbody>" + "".join(row_) + "</tbody></table></div>")

    # frontmatter
    b.append("<h2>Frontmatter</h2><div class=table><table><tbody>")
    keys = _tasks.TASK_KEYS + [k for k in t if k not in _tasks.TASK_KEYS and not k.startswith("_")]
    for k in keys:
        v = t.get(k, "")
        v = ", ".join(v) if isinstance(v, list) else str(v)
        b.append(f"<tr><th>{esc(k)}</th><td class=why>{link_ids(esc(v), tasks)}</td></tr>")
    b.append("</tbody></table></div>")

    # the brief
    b.append(f'<h2>Brief</h2><div class=brief>{markdown(no_options(t["_body"]), tasks)}</div>')
    b.append(options_html(t))

    # totals
    cost = 0.0
    for r in trail:
        if r.get("event") in COST_EVENTS:
            try:
                cost += float(r.get("cost") or 0)
            except (TypeError, ValueError):
                pass
    reviews = [r for r in trail if r.get("event") == "review"]
    runs = [r for r in trail if r.get("event") == "worker_end"]
    old = unjournaled(tid, trail, t.get("tier"))
    b.append(f"<h2>Runs</h2><p>{len(runs)} worker run(s), {len(reviews)} review(s), {money(cost)} in the journal"
             + (f'; last review <span class="v-{esc(reviews[-1].get("verdict"))}">{esc(reviews[-1].get("verdict"))}</span>'
                f' (round {esc(reviews[-1].get("round") or 1)})' if reviews else "")
             + (f"; {len(old)} older run log(s) the journal doesn't cover" if old else "") + "</p>")

    # each worker run, with its session
    wt = None
    for r in trail:
        if r.get("event") == "dispatched" and r.get("worktree"):
            wt = _config.HOME / str(r["worktree"])
        if r.get("event") == "worker_end":
            b.append(run_card(f"round {r.get('round') or 1}", r, wt or _config.WORKTREES / tid, tasks))
    for r in old:
        b.append(run_card(r["kind"], r, _config.WORKTREES / tid, tasks))

    if reviews:
        b.append('<h2>Reviews</h2><div class=table><table><thead><tr><th>round</th><th>verdict</th><th>reviewer</th>'
                 '<th>cost</th><th class=hide-narrow>time</th><th>reason</th><th>log</th></tr></thead><tbody>')
        for r in reviews:
            v = str(r.get("verdict") or "")
            b.append(f'<tr><td class=num>{esc(r.get("round") or 1)}</td><td class="v-{esc(v)}">{esc(v)}</td>'
                     f'<td>{esc(r.get("reviewer"))}</td><td class=num>{money(r.get("cost"))}</td>'
                     f'<td class="num hide-narrow">{esc(secs(r.get("seconds")))}</td>'
                     f'<td class=why>{link_ids(esc(r.get("reason") or ""), tasks)}</td><td>{log_link(r.get("log"))}</td></tr>')
        b.append("</tbody></table></div>")

    if trail:
        b.append('<h2>Journal</h2><div class=table><table><thead><tr><th>at</th><th>event</th><th>round</th>'
                 '<th>detail</th></tr></thead><tbody>')
        skip = {"at", "task", "epic", "event", "round", "log"}
        for r in trail:
            detail = "; ".join(f"{k}={json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}"
                               for k, v in r.items() if k not in skip and v not in (None, "", [], {}))
            log = f" · {log_link(r['log'])}" if r.get("log") else ""
            b.append(f'<tr><td class="muted mono">{esc(r.get("at"))}</td><td>{esc(r.get("event"))}</td>'
                     f'<td class=num>{esc(r.get("round") or "")}</td><td class=why>{link_ids(esc(detail), tasks)}{log}</td></tr>')
        b.append("</tbody></table></div>")

    b.append(evidence_section(tid, trail))
    logs = sorted(p.name for p in _config.RUNS.glob(f"{tid}-*") if p.is_file() and LOG_NAME.fullmatch(p.name))
    if logs:
        b.append("<h2>Logs</h2><ul>" + "".join(
            f'<li><a href="/runs/{esc(urllib.parse.quote(n))}">{esc(n)}</a></li>' for n in logs) + "</ul>")
    return page(f"{tid} · {t.get('title', '')}", "".join(b), nonce)


# ---------- /review: what a human reads before `ergon approve` ----------
# Everything here is computed from files at request time. The quiz is the one part serve can't
# make (it would mean launching claude): `ergon quiz` writes it to <state>/runs/quiz/, and this
# page only reads the newest file, keeping the questions whose task is still proposed and whose
# brief hashes the same as when the question was written.

QUIZ_FILE = re.compile(r"quiz-\d{8}-\d{6}\.json")
MAP_W, MAP_H, MAP_GX, MAP_GY, MAP_PAD = 150, 36, 46, 12, 8


def quiz_dir():
    return _config.RUNS / "quiz"


def brief_hash(t):
    """A short hash of what a quiz question about t was written from: its title, tier, tier_why,
    depends, touches and body. Any edit to those makes its old questions stale."""
    parts = [t.get("title") or "", t.get("tier") or "", t.get("tier_why") or "",
             list(t.get("depends") or []), list(t.get("touches") or []), t.get("_body") or ""]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:16]


def section(body, name):
    """The text under a `## name` heading of a brief, up to the next `##` heading; None if absent."""
    m = re.search(rf"^##[ \t]+{re.escape(name)}[ \t]*$(.*?)(?=^##[ \t]|\Z)", body, re.M | re.S | re.I)
    return m[1].strip("\n") if m else None


def no_options(body):
    """body without its `## Options` sections: options_html shows them, in lens order and unmarked."""
    return re.sub(r"^##[ \t]+Options[ \t]*$.*?(?=^##[ \t]|\Z)", "", body or "", flags=re.M | re.S | re.I)


def blank(text):
    return not re.sub(r"<!--.*?-->", "", text or "", flags=re.S).strip()


def in_checkout(rel):
    """Path rel inside the main checkout or a companion checkout, if it exists there and stays
    there once resolved; else None. Absolute paths and `..` escapes never resolve."""
    rel = str(rel).strip()
    if not rel or rel.startswith(("/", "~")) or "\\" in rel:
        return None
    roots = [_config.HOME] + [c["path"] for c in _config.COMPANIONS]
    for c in _config.COMPANIONS:  # `org/product/src/x.rs` or `.product/src/x.rs` names the companion's file
        for prefix in c["touches"]:
            if rel.startswith(prefix + "/"):
                roots.append((c["path"], rel[len(prefix) + 1:]))
    for root in roots:
        root, sub = root if isinstance(root, tuple) else (root, rel)
        base = Path(root).resolve()
        p = (base / sub).resolve()
        if (p == base or base in p.parents) and p.exists():
            return p
    return None


def findings(t, tasks):
    """What a reviewer should look at before approving t, from files only: citations and
    touches that don't exist, empty Done when / tier_why / touches, dead dependencies."""
    out = []
    body = t.get("_body") or ""
    seen = set()
    for m in _tasks.CITE.finditer(body):
        path, lo, hi = m[1], m[2], m[3]
        if not lo or "://" in body[max(0, m.start() - 8):m.end()] or (path, lo, hi) in seen:
            continue
        seen.add((path, lo, hi))
        if "/" not in path and not (_config.HOME / path).exists():
            continue  # a bare name may live anywhere; without git's index we can't tell
        p = in_checkout(path)
        if p is None:
            out.append(f"cites {path}:{lo}: no such file in the main checkout")
        elif p.is_file():
            try:
                with open(p, "rb") as f:
                    n = sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 16), b"")) + 1
            except OSError:
                continue
            want = int(hi or lo)
            if want > n:
                out.append(f"cites {path}:{lo}{'-' + hi if hi else ''}: the file has {n} lines")
    every_touch = tuple(k for c in _config.COMPANIONS for k in c["touches"])
    touches = [str(p).strip() for p in t.get("touches") or [] if str(p).strip()]
    for p in touches:
        q = p.rstrip("/")
        if " " in q or not ("/" in q or _tasks.CITE_EXT.search(q)) or re.search(r"[*?\[]", q) \
                or q in every_touch or "://" in q:
            continue  # a host, a noun, a glob or a companion repo's name
        if in_checkout(q) is None:
            out.append(f"touches {p}: not in the main checkout (a new file, or a typo?)")
    if t.get("status") == "proposed" and t.get("cod") in (None, ""):
        out.append("not graded: no cod:, so it ranks at the neutral 50")
    if t.get("status") == "proposed" and _tasks.category(t) == "feature":
        if not _tasks.has_options(t):
            out.append("a proposed feature with no ## Options: nothing offers the human a choice")
        elif _tasks.choice_problem(t):
            out.append("has ## Options and no choice yet: a human runs ergon choose first")
    if blank(section(body, "Done when")):
        out.append("Done when is empty: nothing says when the task is finished")
    if blank(t.get("tier_why")):
        out.append("tier_why is empty: nothing says why this tier")
    if not touches:
        out.append("touches is empty: nothing says what it changes")
    for d in t.get("depends") or []:
        s = tasks.get(d, {}).get("status")
        if s is None:
            out.append(f"depends on {d}: no such task")
        elif s == "dropped":
            out.append(f"depends on {d}, which was dropped: it can never clear")
        elif s == "proposed":
            out.append(f"depends on proposed {d}: approving this alone leaves it waiting.")
    return out


def valid_question(q):
    """A quiz question as `ergon quiz` writes it, or None if anything is off."""
    if not isinstance(q, dict):
        return None
    opts = q.get("options")
    ans = q.get("answer")
    if not (isinstance(q.get("task"), str) and TASK_ID.fullmatch(q["task"])
            and isinstance(q.get("question"), str) and q["question"].strip()
            and isinstance(opts, list) and 2 <= len(opts) <= 6
            and all(isinstance(o, str) and o.strip() for o in opts)
            and isinstance(ans, int) and not isinstance(ans, bool) and 0 <= ans < len(opts)):
        return None
    return {"task": q["task"], "hash": str(q.get("hash") or ""), "question": q["question"].strip(),
            "options": [o.strip() for o in opts], "answer": ans,
            "explanation": str(q.get("explanation") or "").strip()}


def latest_quiz():
    """(file name, its data) for the newest <state>/runs/quiz/quiz-<time>.json, or (None, None)."""
    try:
        names = sorted(n for n in os.listdir(quiz_dir()) if QUIZ_FILE.fullmatch(n))
    except OSError:
        return None, None
    for name in reversed(names):
        p = quiz_dir() / name
        try:
            if p.is_symlink() or not p.is_file():
                continue
            d = json.loads(p.read_text(errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return name, d
    return None, None


def current_questions(quiz, tasks):
    """(questions still about a proposed task's current brief, how many were left out)."""
    raw = quiz.get("questions") if isinstance(quiz, dict) else None
    keep, stale = [], 0
    for q in raw if isinstance(raw, list) else []:
        q = valid_question(q)
        if q is None:
            continue
        t = tasks.get(q["task"])
        if t and t["status"] == "proposed" and q["hash"] == brief_hash(t):
            keep.append(q)
        else:
            stale += 1
    return keep, stale


def dep_layers(nodes, deps):
    """Column of each node: 0 with no dependency on the map, else one past its deepest one."""
    col = {}

    def depth(n, seen):
        if n in col:
            return col[n]
        if n in seen:  # a cycle: cut it here
            return 0
        seen = seen | {n}
        col[n] = max([depth(d, seen) + 1 for d in deps.get(n, []) if d in nodes] or [0])
        return col[n]

    for n in nodes:
        depth(n, frozenset())
    return col


def dep_map(proposed, tasks, flagged, label="the proposed tasks"):
    """Proposed tasks and their depends: as inline SVG. A proposed task's box links to its card
    below; a dependency that isn't proposed links to its task page (a missing one, nowhere)."""
    ids = [t["id"] for t in proposed]
    deps = {t["id"]: [d for d in t.get("depends") or [] if isinstance(d, str)] for t in proposed}
    nodes = list(dict.fromkeys(ids + [d for i in ids for d in deps[i]]))
    col = dep_layers(set(nodes), deps)
    rows = {}
    for n in sorted(nodes, key=lambda n: (col[n], n)):
        rows.setdefault(col[n], []).append(n)
    pos = {n: (MAP_PAD + c * (MAP_W + MAP_GX), MAP_PAD + r * (MAP_H + MAP_GY))
           for c, ns in rows.items() for r, n in enumerate(ns)}
    width = MAP_PAD * 2 + (max(rows) + 1) * (MAP_W + MAP_GX) - MAP_GX
    height = MAP_PAD * 2 + max(len(ns) for ns in rows.values()) * (MAP_H + MAP_GY) - MAP_GY
    o = [f'<svg class=depmap viewBox="0 0 {width} {height}" width="{width}" height="{height}" role=img '
         f'aria-label="dependency map of {esc(label)}">']
    for i in ids:
        x2, y2 = pos[i]
        for d in deps[i]:
            x1, y1 = pos[d]
            sx, sy, ex, ey = x1 + MAP_W, y1 + MAP_H / 2, x2, y2 + MAP_H / 2
            mx = (sx + ex) / 2
            o.append(f'<path class=edge d="M{sx} {sy:.0f}C{mx:.0f} {sy:.0f} {mx:.0f} {ey:.0f} {ex} {ey:.0f}" />')
            o.append(f'<path class=arrow d="M{ex} {ey:.0f}l-6 -4v8z" />')
    for n in nodes:
        x, y = pos[n]
        t = tasks.get(n)
        status = t["status"] if t else "missing"
        cls = ["box", f"st-{status}"] + (["flag"] if n in flagged else [])
        title = f"{n} · {status}" + (f" · {t.get('epic')}" if t and t.get("epic") else "") \
            + (f" · {t.get('title') or ''}" if t else "") \
            + (f" · {len(flagged[n])} finding(s)" if n in flagged else "")
        label = f"{t.get('tier') or '?'} · {status}" if t else "no such task"
        g = (f'<g class="{" ".join(cls)}"><title>{esc(title)}</title>'
             f'<rect x={x} y={y} width={MAP_W} height={MAP_H} rx=6 />'
             f'<text class=bid x={x + 8} y={y + 15}>{esc(n)}</text>'
             f'<text class=bsub x={x + 8} y={y + 29}>{esc(label)}</text>'
             + (f'<circle class=mark cx={x + MAP_W - 12} cy={y + 12} r=7 /><text class=marktext x={x + MAP_W - 12} '
                f'y={y + 16} text-anchor=middle>!</text>' if n in flagged else "") + "</g>")
        if status == "proposed":
            g = f'<a href="#{esc(n)}">{g}</a>'
        elif t:
            g = f'<a href="/t/{esc(n)}">{g}</a>'
        o.append(g)
    o.append("</svg>")
    return "".join(o)


def epic_maps(proposed, tasks, epics, flagged):
    """One dependency map per epic of the proposed tasks, in epic order, then the tasks with no epic.
    A dependency outside the epic still shows as a box in that epic's map."""
    groups = {}
    for t in proposed:
        groups.setdefault(str(t["epic"]) if t.get("epic") else None, []).append(t)
    keys = sorted(k for k in groups if k) + ([None] if None in groups else [])
    if keys == [None]:
        return f"<div class=mapwrap>{dep_map(proposed, tasks, flagged)}</div>"
    h = []
    for k in keys:
        ts, n = groups[k], len(groups[k])
        count = f' <span class=muted>({n} task{"s" if n != 1 else ""})</span>'
        if k is None:
            h.append(f"<h3 class=mhead id=map-none>No epic{count}</h3>")
            label = "the proposed tasks with no epic"
        else:
            title = (epics.get(k) or {}).get("title")
            h.append(f'<h3 class=mhead id="map-{esc(k)}"><a href="/e/{esc(k)}">{esc(k)}</a>'
                     + (f" {esc(title)}" if title else "") + f"{count}</h3>")
            label = f"the proposed tasks of {k}"
        h.append(f"<div class=mapwrap>{dep_map(ts, tasks, flagged, label)}</div>")
    return "".join(h)


def review_card(t, tasks, found):
    deps = t.get("depends") or []
    dep_html = ", ".join(f'{link_ids(esc(d), tasks)} <span class=muted>({esc(tasks.get(d, {}).get("status", "missing"))})</span>'
                         for d in deps) or '<span class=muted>none</span>'
    touches = ", ".join(f"<code>{esc(p)}</code>" for p in t.get("touches") or []) or '<span class=muted>none</span>'
    body = no_options(t.get("_body") or "")
    what, done = section(body, "What"), section(body, "Done when")
    if what is None and done is None:
        brief = markdown(body, tasks)
    else:
        brief = (f"<h3>What</h3>{markdown(what or '', tasks) if not blank(what) else '<p class=muted>empty</p>'}"
                 f"<h3>Done when</h3>{markdown(done or '', tasks) if not blank(done) else '<p class=muted>empty</p>'}")
    flag = (f' <a class=nfind href="#find-{esc(t["id"])}">{len(found)} finding(s)</a>' if found else "")
    sc = prio_score(t, tasks)
    prio = ""
    if sc is not None:
        graded = t.get("cod") not in (None, "")
        parts = (f"cod {_priority.cod(t):g}" if graded else "not graded (neutral 50)") \
            + f", unblocks {_priority.unblocks(t, tasks)}, size {_priority.SIZE[t.get('tier')]}"
        prio = (f'<tr><th>priority</th><td>{sc:.1f} <span class=muted>({esc(parts)})</span></td></tr>'
                + (f'<tr><th>cod_why</th><td class=why>{link_ids(esc(str(t["cod_why"])), tasks)}</td></tr>'
                   if t.get("cod_why") else ""))
    return (f'<section class=card id="{esc(t["id"])}"><div class=head><a class=id href="/t/{esc(t["id"])}">{esc(t["id"])}</a>'
            f'<span class=tier>{esc(t.get("tier"))}</span>{cat_span(t)}<b class=ctitle>{esc(t.get("title"))}</b>{flag}</div>'
            f'<div class=table><table><tbody>{prio}'
            f'<tr><th>tier_why</th><td class=why>{link_ids(esc(t.get("tier_why") or ""), tasks) or "<span class=muted>empty</span>"}</td></tr>'
            f'<tr><th>depends</th><td class=why>{dep_html}</td></tr>'
            f'<tr><th>touches</th><td class=why>{touches}</td></tr>'
            + (f'<tr><th>epic</th><td><a href="/e/{esc(t["epic"])}">{esc(t["epic"])}</a></td></tr>' if t.get("epic") else "")
            + (f'<tr><th>source</th><td class=why>{link_ids(esc(t["source"]), tasks)}</td></tr>' if t.get("source") else "")
            + f'</tbody></table></div><div class=brief>{brief}</div>{options_html(t)}</section>')


def quiz_html(tasks):
    name, data = latest_quiz()
    if data is None:
        return ('<p class=muted>No quiz yet. Run <code>ergon quiz</code>: it asks claude (sonnet) for questions '
                'on these briefs and saves them under <code>' + esc(quiz_dir().relative_to(_config.HOME).as_posix())
                + '/</code>. This page never runs it.</p>')
    qs, stale = current_questions(data, tasks)
    note = (f' {stale} older question(s) hidden: their task changed or is no longer proposed.' if stale else "")
    missing = sorted(i for i, t in tasks.items() if t["status"] == "proposed" and i not in {q["task"] for q in qs})
    if missing:
        note += " No current question on " + link_ids(esc(", ".join(missing)), tasks) + "."
    head = (f'<p class=muted>From {esc(name)}{" (" + esc(data.get("model")) + ")" if data.get("model") else ""}.'
            f'{note} Run <code>ergon quiz</code> again for the rest. Scored here in the page; nothing is sent.</p>')
    if not qs:
        return head
    o = [head, f'<div id=quiz class=quiz data-total="{len(qs)}"><p class=score id=quiz-score>0 of {len(qs)} answered</p>']
    for n, q in enumerate(qs, 1):
        tid = esc(q["task"])
        o.append(f'<div class=q data-answer="{q["answer"]}"><p class=qtext><span class=muted>{n}.</span> '
                 f'<a href="#{tid}">{tid}</a> {esc(q["question"])}</p><div class=opts>')
        o.extend(f'<button type=button class=opt data-i="{i}">{esc(opt)}</button>' for i, opt in enumerate(q["options"]))
        o.append(f'</div><p class=explain hidden><b class=verdict></b> {esc(q["explanation"])} '
                 f'<a href="#{tid}">Read {tid}</a> · <a href="/t/{tid}">its page</a></p></div>')
    o.append("</div>")
    return "".join(o)


OPEN_STATUSES = ("proposed", "ready", "doing", "review", "blocked")


def review_groups(proposed, tasks):
    """(groups, independent). A group is a list of 2+ proposed tasks joined through depends: links,
    each taken both ways, where every task on a link is open. Groups come largest first, then by
    lowest ID; inside one, dependency layer then ID. The rest are the independent ones, by priority score."""
    is_open = lambda i: tasks.get(i, {}).get("status") in OPEN_STATUSES
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for t in tasks.values():
        if t["status"] not in OPEN_STATUSES:
            continue
        find(t["id"])
        for d in t.get("depends") or []:
            if is_open(d):
                parent[find(t["id"])] = find(d)
    comps = {}
    for t in proposed:
        comps.setdefault(find(t["id"]), []).append(t)
    groups = [ts for ts in comps.values() if len(ts) >= 2]
    in_group = {t["id"] for ts in groups for t in ts}
    out = []
    for ts in groups:
        col = dep_layers({t["id"] for t in ts}, {t["id"]: t.get("depends") or [] for t in ts})
        out.append(sorted(ts, key=lambda t: (col[t["id"]], t["id"])))
    out.sort(key=lambda ts: (-len(ts), min(t["id"] for t in ts)))
    return out, sorted((t for t in proposed if t["id"] not in in_group), key=lambda t: prio_key(t, tasks))


def group_header(ts, tasks):
    ids = {t["id"] for t in ts}
    col = dep_layers(ids, {t["id"]: t.get("depends") or [] for t in ts})
    layers = {}
    for t in ts:
        layers.setdefault(col[t["id"]], []).append(t["id"])
    chain = " → ".join(", ".join(sorted(v)) for _, v in sorted(layers.items()))
    h = [f'<h3 class=ghead id="g-{esc(min(ids))}">{link_ids(esc(chain), tasks)}</h3>']
    waits = sorted({d for t in ts for d in t.get("depends") or []
                    if d not in ids and tasks.get(d, {}).get("status") in OPEN_STATUSES})
    notes = []
    if waits:
        notes.append("waits on " + ", ".join(f'{link_ids(esc(d), tasks)} ({esc(tasks[d]["status"])})' for d in waits))
    epics = {t.get("epic") for t in ts}
    epic = epics.pop() if len(epics) == 1 else None
    if epic:
        notes.append(f'epic <a href="/e/{esc(epic)}">{esc(epic)}</a>')
    elif len(ts) >= 3:
        notes.append(f"{len(ts)} linked tasks, no epic")
    if notes:
        h.append(f'<p class=muted>{"; ".join(notes)}</p>')
    cmd = " ".join(i for i in sorted(ids) if TASK_ID.fullmatch(i) and not needs_choice(tasks[i]))
    if cmd:
        h.append(f'<p class=muted>For this group:</p><code class=cmd>{esc("ergon approve " + cmd)}</code>')
    first = [i for i in sorted(ids) if needs_choice(tasks[i])]
    if first:
        h.append(f'<p class=muted>Choose first: {link_ids(esc(", ".join(first)), tasks)}</p>')
    return "".join(h)


EPIC_ID = re.compile(r"E-\d{3,}")


def epic_briefs(epics):
    """Epics whose brief awaits a human: proposed, or (shared mode) open with no signed approval that verifies, stamped or not."""
    apr = _gitops.approvals()  # None outside shared mode; never raises
    return sorted((e for e in epics.values()
                   if e.get("status") not in ("dropped", "done") and _tasks.epic_problem(e, apr)
                   and EPIC_ID.fullmatch(str(e.get("id")))), key=lambda e: e["id"])


def epic_cards(briefs):
    b = [f"<h2 id=epics>Proposed epics ({len(briefs)})</h2>",
         "<p class=muted>Approving an epic approves its brief and opens it; its tasks wait until then. "
         "This page approves nothing. From your own terminal:</p>",
         f"<code class=cmd>{esc('ergon approve ' + ' '.join(e['id'] for e in briefs))}</code>"]
    for e in briefs:
        b.append(f'<section class=card id="{esc(e["id"])}"><h3>{esc(e["id"])} {esc(e.get("title"))}'
                 f' <span class=tier>{esc(e.get("status"))}</span></h3>')
        for name in ("Goal", "Out of scope", "Constraints"):
            text = section(e.get("_body") or "", name)
            b.append(f"<h4>{name}</h4>" + (f"<pre>{esc(text)}</pre>" if text and not blank(text) else "<p class=muted>empty</p>"))
        b.append("</section>")
    return b


def review_page(nonce):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    briefs = epic_briefs(epics)
    proposed = sorted((t for t in tasks.values() if t["status"] == "proposed"), key=tier_key)
    b = [f'<div class=top><a href="/">← board</a><span class=crumb>{esc(_config.HOME.name)} · review · read-only</span></div>',
         f"<h1>Proposed tasks ({len(proposed)})</h1>"]
    if briefs:
        b.extend(epic_cards(briefs))
    if not proposed:
        if not briefs:
            b.append("<p class=muted>Nothing is waiting for approval.</p>")
        return page("ergon · review", "".join(b), nonce)
    found = {t["id"]: findings(t, tasks) for t in proposed}
    flagged = {i: f for i, f in found.items() if f}

    by_tier = {tier: [t for t in proposed if t.get("tier") == tier] for tier in _tasks.TIERS}
    other = [t for t in proposed if t.get("tier") not in _tasks.TIERS]
    met = [t for t in proposed if _tasks.deps_done(t, tasks)]
    b.append('<h2>Summary</h2><div class=counts>')
    for tier, ts in list(by_tier.items()) + ([("other", other)] if other else []):
        if ts:
            b.append(f'<span class=count><b>{len(ts)}</b>{esc(tier)}</span>')
    b.append("</div>")
    b.append(f"<p>{len(met)} of {len(proposed)} have every dependency done; "
             f"{len(flagged)} have findings ({sum(map(len, flagged.values()))} in all).</p>")

    ranked = sorted((t for t in proposed if prio_score(t, tasks) is not None), key=lambda t: prio_key(t, tasks))
    if ranked:
        b.append('<h2 id=priority>Priority</h2><p class=muted>In the order dispatch would take them once '
                 'approved: score = (cod + 60 × tasks it unblocks) ÷ tier size.</p>'
                 + priority_table(ranked, tasks, anchor=True) + grade_line())

    b.append(f'<h2>Dependency map</h2>{epic_maps(proposed, tasks, epics, flagged)}'
             '<p class=legend>One map per epic. A box links to its card; <b>!</b> marks findings; an arrow '
             'points from a dependency to the task that waits on it; a dependency in another epic is drawn '
             'in this one too.</p>')

    b.append("<h2>Tasks</h2>")
    groups, alone = review_groups(proposed, tasks)
    for ts in groups:
        b.append(group_header(ts, tasks))
        b.extend(review_card(t, tasks, found[t["id"]]) for t in ts)
    if alone:
        if groups:
            b.append("<h3 class=ghead id=g-independent>Independent</h3>")
        b.extend(review_card(t, tasks, found[t["id"]]) for t in alone)

    b.append(f"<h2 id=findings>Findings ({sum(map(len, flagged.values()))})</h2>")
    if flagged:
        b.append("<p class=muted>Computed from the task files and the main checkout, now. Leads, not verdicts.</p>")
        for t in proposed:
            if t["id"] in flagged:
                b.append(f'<div class=finding id="find-{esc(t["id"])}"><a href="#{esc(t["id"])}">{esc(t["id"])}</a> '
                         f'{esc(t.get("title"))}<ul>' + "".join(f"<li>{link_ids(esc(f), tasks)}</li>" for f in flagged[t["id"]])
                         + "</ul></div>")
    else:
        b.append("<p class=muted>none</p>")

    b.append("<h2>Quiz</h2>" + quiz_html(tasks))

    all_ids = [t["id"] for t in proposed if TASK_ID.fullmatch(t["id"])]  # only IDs go in a command to copy
    first = [i for i in all_ids if needs_choice(tasks[i])]
    ids = [i for i in all_ids if i not in first]  # options with no choice yet wait on `ergon choose`
    clean = [i for i in ids if i not in flagged]
    b.append("<h2>Approve</h2><p class=muted>This page approves nothing. From your own terminal:</p>"
             + (f"<code class=cmd>{esc('ergon approve ' + ' '.join(ids))}</code>" if ids
                else "<p class=muted>nothing to approve yet</p>"))
    if first:
        b.append("<p class=muted>Choose first, then approve: " + link_ids(esc(", ".join(first)), tasks) + "</p>")
    if flagged and clean:
        b.append(f"<p class=muted>Only those without findings:</p><code class=cmd>{esc('ergon approve ' + ' '.join(clean))}</code>")
    return page("ergon · review", "".join(b), nonce, script=True)


def metrics_page(query, nonce):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    q = urllib.parse.parse_qs(query)
    one = lambda k, ok: next((v for v in q.get(k, []) if ok(v)), None)
    date = lambda v: re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
    f = {"epic": one("epic", lambda v: v in epics), "since": one("since", date), "until": one("until", date),
         "epics": sorted(epics)}
    rows = _history.read()
    m = _metrics.compute(tasks, rows, _config.RUNS, f["since"], f["until"], f["epic"], summary, worktree_of)
    return page("ergon · metrics", _metrics.render(m, esc, money, f), nonce)


# ---------- HTTP ----------

def csp(nonce=None):
    if nonce is None:
        return "default-src 'none'; frame-ancestors 'none'; sandbox"
    return (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "ergon"
    sys_version = ""

    def __getattr__(self, name):
        # BaseHTTPRequestHandler looks up do_<METHOD>: every method we don't define is a 405,
        # not the 501 it would otherwise send.
        if name.startswith("do_"):
            return self._not_allowed
        raise AttributeError(name)

    def _not_allowed(self):
        self.close_connection = True  # the request body, if any, is never read
        self._send(405, b"405: ergon serve is read-only (GET and HEAD only)\n", "text/plain; charset=utf-8",
                   head=False, extra={"Allow": "GET, HEAD", "Connection": "close"})

    def do_GET(self):
        self._route(head=False)

    def do_HEAD(self):
        self._route(head=True)

    def _send(self, code, data, ctype, head, nonce=None, extra=None, length=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data) if length is None else length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp(nonce))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head and data:
            self.wfile.write(data)

    def _text(self, code, msg, head):
        self._send(code, (msg + "\n").encode(), "text/plain; charset=utf-8", head)

    def _route(self, head):
        try:
            if not host_ok(self.headers.get("Host")):
                return self._text(403, "403: open this board by IP address or localhost, not a host name", head)
            raw, _, query = self.path.split("#", 1)[0].partition("?")
            if not raw.startswith("/"):
                return self._text(404, "404: not found", head)
            path = urllib.parse.unquote(raw)
            nonce = secrets.token_urlsafe(16)
            if path == "/":
                return self._send(200, board_page(nonce).encode(), "text/html; charset=utf-8", head, nonce)
            if path == "/review":
                return self._send(200, review_page(nonce).encode(), "text/html; charset=utf-8", head, nonce)
            if path == "/metrics":
                return self._send(200, metrics_page(query, nonce).encode(), "text/html; charset=utf-8", head, nonce)
            if path == "/live.json":
                tasks = _tasks.load(_config.ITEMS)
                data = {"now_ts": time.time(), "counts": counts(tasks), "doing": live(tasks, _history.read())}
                return self._send(200, json.dumps(data).encode(), "application/json", head)
            m = re.fullmatch(r"/e/(E-\d{3,})", path)
            if m:
                body = epic_page(m[1], nonce)
                if body is None:
                    return self._text(404, f"404: no epic {m[1]}", head)
                return self._send(200, body.encode(), "text/html; charset=utf-8", head, nonce)
            m = re.fullmatch(r"/t/(T-\d{3,})", path)
            if m:
                body = task_page(m[1], nonce)
                if body is None:
                    return self._text(404, f"404: no task {m[1]}", head)
                return self._send(200, body.encode(), "text/html; charset=utf-8", head, nonce)
            m = re.fullmatch(r"/runs/([^/]+)", path)
            if m:
                return self._log(m[1], head)
            m = re.fullmatch(r"/runs/(breakdown|T-\d{3,}-evidence)/([^/]+)", path)
            if m:
                return self._log(m[2], head, m[1])
            return self._text(404, "404: not found", head)
        except (Exception, SystemExit) as err:  # noqa: BLE001 - a bad task file is a 500, not a dead thread
            print(f"ergon serve: {self.path}: {err!r}", file=sys.stderr)
            try:
                self._text(500, "500: couldn't read the task state; the server log says why", head)
            except OSError:
                pass

    def _log(self, name, head, sub=None):
        """A file directly inside <state>/runs/ (or its <ID>-evidence/ directory), by bare name;
        404 for anything else. Evidence images get their image type, the rest is plain text."""
        runs = _config.RUNS.resolve()
        if sub:
            runs = (runs / sub).resolve()
            if runs.parent != _config.RUNS.resolve():
                return self._text(404, "404: not found", head)
        if not LOG_NAME.fullmatch(name):
            return self._text(404, "404: not found", head)
        p = (runs / name).resolve()
        if p.parent != runs or not p.is_file():
            return self._text(404, "404: not found", head)
        try:
            f = open(p, "rb")
        except OSError:
            return self._text(404, "404: not found", head)
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = (IMAGE_TYPES.get(p.suffix.lower()) if sub else None) or "text/plain; charset=utf-8"
            self._send(200, b"", ctype, True, length=size)
            if not head:
                shutil.copyfileobj(f, self.wfile)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_server(bind, port):
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    cls = type("BoundServer", (Server,), {"address_family": family})
    return cls((bind, port), Handler)


def serve(args):
    bind = args.bind
    if not is_loopback(bind):
        print(WARNING.format(bind=bind), file=sys.stderr, flush=True)
    try:
        srv = make_server(bind, args.port)
    except OSError as err:
        raise SystemExit(f"ergon serve: can't listen on {bind}:{args.port}: {err}")
    host, port = srv.server_address[:2]
    shown = f"[{host}]" if ":" in host else host
    print(f"ergon serve: http://{shown}:{port}/  (read-only; Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
