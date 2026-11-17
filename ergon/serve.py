"""`ergon serve`: a read-only web view of the board and of each task.

Standard library only: a ThreadingHTTPServer that renders HTML with its CSS and JS inlined
(serve.css, serve.js next to this file), no external assets.

  /            the board: open work by status then tier, epics with progress, what waits on a
               human (approval, acceptance, answers), and live dispatch state
  /metrics     charts and tables: stats, review, human latency, lead time, tokens; ?epic=&since=&until=
  /review      what waits for approval, read before approving: proposed tasks by tier, a
               dependency map (inline SVG), a card per task, findings computed from files (missing
               citations and touches, empty Done when / tier_why / touches, dead depends), and
               the quiz `ergon quiz` last wrote, minus questions whose task has changed since
  /live.json   tasks `doing`: elapsed time, round, spend so far; the board polls it
  /e/E-NNN     one epic's replay timeline: a lane per task, a bar per phase from the journal,
               human waits hatched, a cost overlay, and a replay player (inline SVG, no libraries)
  /t/T-NNN     one task: its brief, frontmatter, every run and review from the history
               journal, transcript usage, links to the raw logs, `claude --resume` commands
  /runs/NAME   one file directly inside <state>/runs/, as plain text

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

from . import config as _config
from . import history as _history
from . import metrics as _metrics
from . import report as _report
from . import tasks as _tasks
from . import timeline as _timeline
from . import transcripts as _transcripts

DEFAULT_PORT = 4300
LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
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


def tier_key(t):
    tier = t.get("tier")
    return (_tasks.TIERS.index(tier) if tier in _tasks.TIERS else len(_tasks.TIERS), t["id"])


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
    if p.parent.resolve() != _config.RUNS.resolve() or not LOG_NAME.fullmatch(p.name):
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
    """Open human tasks a worker filed when it blocked: what workers wait on a human to answer."""
    return sorted((t for t in tasks.values() if t.get("tier") == "human"
                   and t["status"] not in ("done", "dropped") and TASK_ID.fullmatch(str(t.get("source") or ""))),
                  key=lambda t: t["id"])


def counts(tasks):
    return {"approval": sum(t["status"] == "proposed" for t in tasks.values()),
            "acceptance": sum(t["status"] == "review" for t in tasks.values()),
            "answer": len(asks(tasks))}


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


def task_li(t, tasks, extra=""):
    flags = []
    if t.get("epic"):
        flags.append(f'<a href="/e/{esc(t["epic"])}">{esc(t["epic"])}</a>')
    if t["status"] == "ready" and not _tasks.deps_done(t, tasks):
        waits = [d for d in t.get("depends") or [] if tasks.get(d, {}).get("status") != "done"]
        flags.append("waits on " + link_ids(esc(",".join(waits)), tasks))
    tail = f' <span class=flags>({"; ".join(flags)})</span>' if flags else ""
    return (f'<li><a class=id href="/t/{esc(t["id"])}">{esc(t["id"])}</a>'
            f'<span class=tier>{esc(t.get("tier"))}</span><span class=title>{esc(t.get("title"))}{tail}{extra}</span></li>')


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
    c = counts(tasks)
    now = time.time()
    lv = live(tasks, rows)
    b = [f'<div class=top><h1>{esc(_config.HOME.name)}</h1><span class=crumb>ergon board · read-only · <a href="/metrics">metrics</a></span></div>']
    b.append("<div class=counts>")
    for key, label, anchor in (("approval", "awaiting approval", "#proposed"),
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
    for status in _tasks.TASK_STATUSES:
        group = sorted((t for t in open_ if t["status"] == status), key=tier_key)
        if group:
            more = ' <a class=more href="/review">review them →</a>' if status == "proposed" else ""
            b.append(f'<h2 id="{status}">{esc(status.upper())} ({len(group)}){more}</h2><ul class=tasks>')
            b.extend(task_li(t, tasks) for t in group)
            b.append("</ul>")
    asked = asks(tasks)
    if asked:
        b.append(f'<h2 id=answer>Awaiting answer ({len(asked)})</h2><ul class=tasks>')
        b.extend(task_li(t, tasks, f' <span class=flags>asked by {link_ids(esc(t["source"]), tasks)}</span>')
                 for t in asked)
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
    if not tl["lanes"]:
        b.append("<p class=muted>no task of this epic has a journal yet</p>")
        return page(f"{eid} · {e.get('title', '')}", "".join(b), nonce)
    human, machine = _timeline.totals(tl)
    total = human + machine
    spent = tl["costs"][-1][3] if tl["costs"] else 0
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


def task_page(tid, nonce):
    tasks = _tasks.load(_config.ITEMS)
    t = tasks.get(tid)
    if not t:
        return None
    trail = by_task(_history.read()).get(tid, [])
    b = [f'<div class=top><a href="/">← board</a><span class=crumb>{esc(_config.HOME.name)}</span></div>',
         f'<h1>{esc(tid)} {esc(t.get("title"))}</h1>',
         f'<p><span class="status st-{esc(t["status"])}">{esc(t["status"])}</span> · '
         f'<span class=tier>{esc(t.get("tier"))}</span></p>']

    # frontmatter
    b.append("<h2>Frontmatter</h2><div class=table><table><tbody>")
    keys = _tasks.TASK_KEYS + [k for k in t if k not in _tasks.TASK_KEYS and not k.startswith("_")]
    for k in keys:
        v = t.get(k, "")
        v = ", ".join(v) if isinstance(v, list) else str(v)
        b.append(f"<tr><th>{esc(k)}</th><td class=why>{link_ids(esc(v), tasks)}</td></tr>")
    b.append("</tbody></table></div>")

    # the brief
    b.append(f'<h2>Brief</h2><div class=brief>{markdown(t["_body"], tasks)}</div>')

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


def dep_map(proposed, tasks, flagged):
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
         'aria-label="dependency map of the proposed tasks">']
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
        title = f"{n} · {status}" + (f" · {t.get('title') or ''}" if t else "") \
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


def review_card(t, tasks, found):
    deps = t.get("depends") or []
    dep_html = ", ".join(f'{link_ids(esc(d), tasks)} <span class=muted>({esc(tasks.get(d, {}).get("status", "missing"))})</span>'
                         for d in deps) or '<span class=muted>none</span>'
    touches = ", ".join(f"<code>{esc(p)}</code>" for p in t.get("touches") or []) or '<span class=muted>none</span>'
    body = t.get("_body") or ""
    what, done = section(body, "What"), section(body, "Done when")
    if what is None and done is None:
        brief = markdown(body, tasks)
    else:
        brief = (f"<h3>What</h3>{markdown(what or '', tasks) if not blank(what) else '<p class=muted>empty</p>'}"
                 f"<h3>Done when</h3>{markdown(done or '', tasks) if not blank(done) else '<p class=muted>empty</p>'}")
    flag = (f' <a class=nfind href="#find-{esc(t["id"])}">{len(found)} finding(s)</a>' if found else "")
    return (f'<section class=card id="{esc(t["id"])}"><div class=head><a class=id href="/t/{esc(t["id"])}">{esc(t["id"])}</a>'
            f'<span class=tier>{esc(t.get("tier"))}</span><b class=ctitle>{esc(t.get("title"))}</b>{flag}</div>'
            '<div class=table><table><tbody>'
            f'<tr><th>tier_why</th><td class=why>{link_ids(esc(t.get("tier_why") or ""), tasks) or "<span class=muted>empty</span>"}</td></tr>'
            f'<tr><th>depends</th><td class=why>{dep_html}</td></tr>'
            f'<tr><th>touches</th><td class=why>{touches}</td></tr>'
            + (f'<tr><th>epic</th><td><a href="/e/{esc(t["epic"])}">{esc(t["epic"])}</a></td></tr>' if t.get("epic") else "")
            + (f'<tr><th>source</th><td class=why>{link_ids(esc(t["source"]), tasks)}</td></tr>' if t.get("source") else "")
            + f'</tbody></table></div><div class=brief>{brief}</div></section>')


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
    lowest ID; inside one, dependency layer then ID. The rest are the independent ones, in tier order."""
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
    return out, [t for t in proposed if t["id"] not in in_group]


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
    cmd = " ".join(i for i in sorted(ids) if TASK_ID.fullmatch(i))
    if cmd:
        h.append(f'<p class=muted>For this group:</p><code class=cmd>{esc("ergon approve " + cmd)}</code>')
    return "".join(h)


def review_page(nonce):
    tasks = _tasks.load(_config.ITEMS)
    proposed = sorted((t for t in tasks.values() if t["status"] == "proposed"), key=tier_key)
    b = [f'<div class=top><a href="/">← board</a><span class=crumb>{esc(_config.HOME.name)} · review · read-only</span></div>',
         f"<h1>Proposed tasks ({len(proposed)})</h1>"]
    if not proposed:
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

    b.append(f'<h2>Dependency map</h2><div class=mapwrap>{dep_map(proposed, tasks, flagged)}</div>'
             '<p class=legend>A box links to its card; <b>!</b> marks findings; an arrow points from a '
             'dependency to the task that waits on it.</p>')

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

    ids = [t["id"] for t in proposed if TASK_ID.fullmatch(t["id"])]  # only IDs go in a command to copy
    clean = [i for i in ids if i not in flagged]
    b.append("<h2>Approve</h2><p class=muted>This page approves nothing. From your own terminal:</p>"
             f"<code class=cmd>{esc('ergon approve ' + ' '.join(ids))}</code>")
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
            return self._text(404, "404: not found", head)
        except (Exception, SystemExit) as err:  # noqa: BLE001 - a bad task file is a 500, not a dead thread
            print(f"ergon serve: {self.path}: {err!r}", file=sys.stderr)
            try:
                self._text(500, "500: couldn't read the task state; the server log says why", head)
            except OSError:
                pass

    def _log(self, name, head):
        """A file directly inside <state>/runs/, by bare name; 404 for anything else."""
        runs = _config.RUNS.resolve()
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
            self._send(200, b"", "text/plain; charset=utf-8", True, length=size)
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
