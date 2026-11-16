"""`ergon serve`: a read-only web view of the board and of each task.

Standard library only: a ThreadingHTTPServer that renders HTML with its CSS and JS inlined
(serve.css, serve.js next to this file), no external assets.

  /            the board: open work by status then tier, epics with progress, what waits on a
               human (approval, acceptance, answers), and live dispatch state
  /live.json   tasks `doing`: elapsed time, round, spend so far; the board polls it
  /t/T-NNN     one task: its brief, frontmatter, every run and review from the history
               journal, transcript usage, links to the raw logs, `claude --resume` commands
  /runs/NAME   one file directly inside <state>/runs/, as plain text

The read-only guarantee:
- GET and HEAD only. Every other method, known or not, gets 405.
- No route writes anything, runs git or launches claude. Transcript summaries are cached in
  memory, never in <state>/runs/transcripts.json.
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
from . import report as _report
from . import tasks as _tasks
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
    js = f'<script nonce="{nonce}">{asset("serve.js")}</script>' if script else ""
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
        flags.append(esc(t["epic"]))
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
    b = [f'<div class=top><h1>{esc(_config.HOME.name)}</h1><span class=crumb>ergon board · read-only</span></div>']
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
            b.append(f'<h2 id="{status}">{esc(status.upper())} ({len(group)})</h2><ul class=tasks>')
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
            b.append(f'<tr><td class="mono id">{esc(e["id"])}</td><td>{esc(e.get("status"))}</td>'
                     f'<td class=num>{bar(done, len(kids))} {done}/{len(kids)}</td>'
                     f'<td class="num hide-narrow">{pending or ""}</td><td>{esc(e.get("title"))}{note}</td></tr>')
        b.append("</tbody></table></div>")
    by_tier = {tier: sum(t.get("tier") == tier for t in open_) for tier in _tasks.TIERS}
    b.append("<footer>open by tier: " + esc("  ".join(f"{k}:{v}" for k, v in by_tier.items())) + "</footer>")
    return page(f"ergon · {_config.HOME.name}", "".join(b), nonce, script=True)


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
            raw = self.path.split("?", 1)[0].split("#", 1)[0]
            if not raw.startswith("/"):
                return self._text(404, "404: not found", head)
            path = urllib.parse.unquote(raw)
            nonce = secrets.token_urlsafe(16)
            if path == "/":
                return self._send(200, board_page(nonce).encode(), "text/html; charset=utf-8", head, nonce)
            if path == "/live.json":
                tasks = _tasks.load(_config.ITEMS)
                data = {"now_ts": time.time(), "counts": counts(tasks), "doing": live(tasks, _history.read())}
                return self._send(200, json.dumps(data).encode(), "application/json", head)
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
