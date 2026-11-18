"""The ordered event log: one JSON object per line in <state>/runs/history.jsonl.

Every row has `at` (ISO-8601, local time with its offset, to the second), `task` (T-NNN or
null), `epic` (E-NNN or null) and `event`; the rest depends on the event (the table in
skills/ergon/SKILL.md). The file only grows. Fields are only ever added, never renamed or
given a new meaning, so a reader must skip events and fields it doesn't know.

Rows are written from several processes (dispatch runs, `approve` in another terminal) and
from dispatch's worker threads, often outside the state lock, which isn't reentrant. So each
row is one os.write() on an O_APPEND descriptor, made under an flock on the history file
itself: no two rows interleave, and a reader never sees half a row except while it's written.
Recording never raises: a history that can't be written warns on stderr and the command goes on.
"""
import datetime
import fcntl
import json
import os
import re
import sys

from . import config as _config


def path():
    return _config.RUNS / "history.jsonl"


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def row(event, item=None, **fields):
    """The row for `event` about `item`, a task or epic dict (or None). Paths become strings
    relative to the main checkout."""
    tid = eid = None
    if item:
        if str(item.get("id", "")).startswith("E-"):
            eid = item["id"]
        else:
            tid, eid = item.get("id"), item.get("epic") or None
    out = {"at": now(), "task": tid, "epic": eid, "event": event}
    for k, v in fields.items():
        if hasattr(v, "relative_to"):  # a Path
            try:
                v = v.relative_to(_config.HOME)
            except ValueError:
                pass
            v = v.as_posix()
        out[k] = v
    return out


def record(event, item=None, **fields):
    """Append one event row. Never raises."""
    try:
        data = (json.dumps(row(event, item, **fields), ensure_ascii=False, default=str) + "\n").encode()
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            while data:  # one write() in practice; a short write still finishes under the lock
                data = data[os.write(fd, data):]
        finally:
            os.close(fd)  # drops the flock too
    except Exception as err:  # noqa: BLE001 - history is a record, never a reason to stop
        print(f"ergon: couldn't record {event} in {path()}: {err}", file=sys.stderr)


def read(p=None):
    """Every well-formed row, in order. A torn or garbled line is skipped, not fatal."""
    p = p or path()
    out = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and "event" in r:
                    out.append(r)
    except OSError:
        pass
    return out


_OUTSIDE = re.compile(r"^outside:|Ctrl-C|dispatch was interrupted|past the \$[\d.]+ cap", re.I)
_STAMP = re.compile(r"^(?P<who>.*?)\s+(?P<day>\d{4}-\d{2}-\d{2})\s*$")


def _week(at):
    try:
        y, w, _ = datetime.date.fromisoformat(str(at or "")[:10]).isocalendar()
    except ValueError:
        return None
    return f"{y}-W{w:02d}"


LAYERS = ("guard", "static", "runtime", "prove", "weaken", "review", "human")


def catches(rows, since=None, epic=None, until=None):
    """Where mistakes were caught, from the history: {"weeks": {week: {layer: n}}, "totals": {layer: n},
    "answers": n, "late": share caught by review + human (None when nothing was caught)}.
    A failed `verify` row counts under its `layer` (an old row with none is `runtime`); a failed
    `review` is `review`; a failed `prove` row (tests green on the base) is `prove`; a `rejected` row
    going from review is `human`; a `guard` row (a refused tool call) is `guard`. Answers are counted apart:
    they're a human's input, not a caught mistake. Layers this reader doesn't know are kept as
    they come. Never raises on odd rows."""
    weeks, totals, answers = {}, {}, 0
    for r in rows:
        d = str(r.get("at") or "")[:10]
        if (since and d < since) or (until and d > until) or (epic and r.get("epic") != epic):
            continue
        ev = r.get("event")
        if ev == "verify" and r.get("ok") is False:
            layer = str(r.get("layer") or "runtime")
        elif ev == "review" and r.get("verdict") == "fail":
            layer = "review"
        elif ev == "prove" and r.get("ok") is False:
            layer = "prove"
        elif ev == "rejected" and r.get("from") == "review":
            layer = "human"
        elif ev == "guard":
            layer = "guard"
        elif ev == "answered":
            answers += 1
            continue
        else:
            continue
        wk = _week(r.get("at"))
        if not wk:
            continue
        w = weeks.setdefault(wk, {})
        w[layer] = w.get(layer, 0) + 1
        totals[layer] = totals.get(layer, 0) + 1
    n = sum(totals.values())
    late = (totals.get("review", 0) + totals.get("human", 0)) / n if n else None
    return {"weeks": dict(sorted(weeks.items())), "totals": totals, "answers": answers, "late": late}


def prove_stats(rows, since=None, epic=None, until=None):
    """What the red-on-base proof (T-102) did and cost, from the history: {"runs", "red", "green"
    (green on the base, or timed out there), "waived", "seconds" (the proof runs' own time),
    "fixes", "fix_cost", "fix_seconds" (the worker rounds it sent back: worker_end rows with
    after "prove")}. Never raises on odd rows."""
    out = {"runs": 0, "red": 0, "green": 0, "waived": 0, "seconds": 0.0, "fixes": 0, "fix_cost": 0.0,
           "fix_seconds": 0.0}
    num = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    for r in rows:
        d = str(r.get("at") or "")[:10]
        if (since and d < since) or (until and d > until) or (epic and r.get("epic") != epic):
            continue
        if r.get("event") == "prove":
            if r.get("waived"):
                out["waived"] += 1
                continue
            out["runs"] += 1
            out["red" if r.get("ok") is True else "green"] += 1
            out["seconds"] += num(r.get("seconds"))
        elif r.get("event") == "worker_end" and r.get("after") == "prove":
            out["fixes"] += 1
            out["fix_cost"] += num(r.get("cost"))
            out["fix_seconds"] += num(r.get("seconds"))
    return out


def approver_signals(tasks, rows, since=None, epic=None, until=None):
    """How the tasks each approver approved went, as counts of tasks:
    {name: {"approved", "tiers", "landed", "escalated", "blocked", "asked", "fails", "sent_back",
    "weeks": {week: {"landed", "tiers", "escalated", "fails"}}}}; `fails` counts the review fails
    of landed tasks. Rows outside since/until/epic are left out. A task with no `approved` row
    is attributed from its `approved:` stamp; one nobody can be found for goes under "(unknown)".
    Never raises on odd rows."""
    keep = []
    for r in rows:
        d = str(r.get("at") or "")[:10]
        if (since and d < since) or (until and d > until) or (epic and r.get("epic") != epic):
            continue
        keep.append(r)
    by = {}
    for r in keep:
        by.setdefault(r.get("task"), []).append(r)
    has_row = {r.get("task") for r in rows if r.get("event") == "approved"}
    who_of = {}  # task -> (names, tier)
    for tid, trail in by.items():
        ap = [r for r in trail if r["event"] == "approved"]
        if ap:
            r = ap[-1]
            names = r.get("approvers") if isinstance(r.get("approvers"), list) and r["approvers"] else [r.get("by")]
            who_of[tid] = ([str(n) for n in names if n] or ["(unknown)"], r.get("tier"))
    for tid, t in tasks.items():
        if tid in has_row or not t.get("approved"):
            continue
        if epic and t.get("epic") != epic:
            continue
        m = _STAMP.match(re.sub(r"\s*sha256:\S+\s*$", "", str(t["approved"])).strip())
        if m and ((since and m["day"] < since) or (until and m["day"] > until)):
            continue
        names = [n.strip() for n in m["who"].split(" and ") if n.strip()] if m else []
        who_of[tid] = (names or ["(unknown)"], None)
    out = {}
    for tid, (names, tier) in who_of.items():
        trail = by.get(tid, [])
        ev = lambda e: [r for r in trail if r["event"] == e]
        tier = tier or (tasks.get(tid) or {}).get("tier") or "?"
        landed = ev("landed")
        fails = sum(r.get("verdict") == "fail" for r in ev("review"))
        blocked = any(not r.get("asked") and not _OUTSIDE.search(str(r.get("reason") or "")) for r in ev("blocked"))
        esc = bool(ev("escalated"))
        week = _week(landed[0]["at"]) if landed else None
        for name in dict.fromkeys(names):
            a = out.setdefault(name, {"approved": 0, "tiers": {}, "landed": 0, "escalated": 0, "blocked": 0,
                                      "asked": 0, "fails": 0, "sent_back": 0, "weeks": {}})
            a["approved"] += 1
            a["tiers"][tier] = a["tiers"].get(tier, 0) + 1
            a["landed"] += bool(landed)
            a["escalated"] += esc
            a["blocked"] += blocked
            a["asked"] += bool(ev("question"))
            a["fails"] += fails if landed else 0
            a["sent_back"] += any(r.get("from") == "review" and r.get("to") == "ready" for r in ev("rejected"))
            if week:
                w = a["weeks"].setdefault(week, {"landed": 0, "tiers": {}, "escalated": 0, "fails": 0})
                w["landed"] += 1
                w["tiers"][tier] = w["tiers"].get(tier, 0) + 1
                w["escalated"] += esc
                w["fails"] += fails
    return out
