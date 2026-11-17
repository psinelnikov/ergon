"""Run-log stats and the status report."""
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
from . import gitops as _gitops
from . import history as _history
from . import limits as _limits
from . import shared as _shared
from . import tasks as _tasks


RUN_LOG = re.compile(r"^(?P<id>[A-Z]+-\d+)-(?P<date>\d{4})(?P<m>\d{2})(?P<d>\d{2})-\d{6}(?:-\w+)?\.json$")


def aggregate_stats(runs_dir, tasks, since=None, epic=None, until=None):
    """Per-tier numbers from the dispatch logs in runs_dir, joined with tasks (id -> meta).
    since / until: 'YYYY-MM-DD' (runs started that day or later / that day or earlier); epic: only that epic's tasks.
    Returns {"tiers": {tier: {runs, done, blocked, timeouts, cost}}, "repeat": {id: blocked
    count} for tasks blocked more than once, "skipped": logs that were unreadable, not JSON
    (and not a timeout) or whose task is gone}. A reviewer's log (<ID>-<time>-review[N].json) and
    a fix round's worker log (<ID>-<time>-fix<N>.json) add their cost to the task's tier but
    aren't runs. A run that ended `RESULT: asking` is a run, neither done nor blocked. Never raises on a bad log."""
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
        day = f"{m['date']}-{m['m']}-{m['d']}"
        if (since and day < since) or (until and day > until):
            continue
        tier_row = lambda: tiers.setdefault(t.get("tier") or "?", {"runs": 0, "done": 0, "blocked": 0,
                                                                "timeouts": 0, "cost": 0.0})
        if re.search(r"-(review\d*|fix\d+)$", log.stem):
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
        found = re.findall(r"^\W*RESULT:\s*(done|blocked|asking)\b", text, re.M)
        verdict = "blocked" if d.get("is_error") or not found else found[-1]
        row = tier_row()
        row["runs"] += 1
        if verdict != "asking":  # a question parks the task; it isn't a block
            row[verdict] += 1
        row["timeouts"] += timed_out
        row["cost"] += float(d.get("total_cost_usd") or 0)
        if verdict == "blocked":
            per_task[t["id"]] = per_task.get(t["id"], 0) + 1
    return {"tiers": tiers, "skipped": skipped,
            "repeat": {k: v for k, v in sorted(per_task.items()) if v > 1}}


def stats(args):
    tasks = _tasks.load(_config.ITEMS)
    r = aggregate_stats(_config.RUNS, tasks, since=args.since, epic=args.epic)
    per, measured = _limits.rate()  # percentage points of the 5-hour limit per $, once dispatch has measured it
    print(f"{'tier':<8}{'runs':>5}{'done':>6}{'blocked':>8}{'block%':>8}{'cost':>10}{'mean':>9}{'timeouts':>9}"
          + (f"{'5h%':>8}{'5h%/run':>9}" if per is not None else ""))
    for tier in [*_tasks.TIERS, *(k for k in r["tiers"] if k not in _tasks.TIERS)]:
        row = r["tiers"].get(tier)
        if not row:
            continue
        n = row["runs"] or 1  # only reviews: their worker run fell outside --since
        print(f"{tier:<8}{row['runs']:>5}{row['done']:>6}{row['blocked']:>8}{100 * row['blocked'] / n:>7.0f}%"
              f"{'$%.2f' % row['cost']:>10}{'$%.2f' % (row['cost'] / n):>9}{row['timeouts']:>9}"
              + (f"{_limits.pct(row['cost'], per):>8}{_limits.pct(row['cost'] / n, per):>9}" if per is not None else ""))
    if not r["tiers"]:
        print("(no runs)")
    if per is not None and r["tiers"]:
        print(f"\n5h%: share of the 5-hour limit at {per:.1f}% per $, measured by dispatch over its last "
              f"{measured} run{'s' if measured != 1 else ''} (an upper bound: other use of the plan counts too)")
    if r["repeat"]:
        print("\nblocked more than once:")
        for tid, n in r["repeat"].items():
            print(f"  {tid} x{n}  {tasks[tid].get('title', '')}")
    if r["skipped"]:
        print(f"\nskipped {r['skipped']} unreadable or unmatched log(s)")
    approver_section(tasks, args)


MIX = {"haiku": "h", "sonnet": "s", "opus": "o", "human": "hu"}


def _mix(tiers):
    order = [*_tasks.TIERS, *(k for k in tiers if k not in _tasks.TIERS)]
    return " / ".join(f"{MIX.get(k, k)} {tiers[k]}" for k in order if tiers.get(k)) or "-"


def approver_section(tasks, args):
    """Print each approver's signals (own record by default). Prints nothing without approvals."""
    from . import gates as _gates  # gates imports this module
    sig = _history.approver_signals(tasks, _history.read(), since=args.since, epic=args.epic)
    if not getattr(args, "approvers", False):
        name = getattr(args, "approver", None) or _gates.approver()
        sig = {name: sig[name]} if name in sig else {}
    if not sig:
        return
    pc = lambda n, d: f"{100 * n / d:.0f}%" if d else "-"
    print("\napprover signals (not verdicts: a block or veto can come from the environment, the code "
          "or the worker, not only the brief)")
    w = max(8, *(len(n) for n in sig)) + 2
    print(f"{'approver':<{w}}{'approved':>9}{'landed':>7}{'esc%':>6}{'block%':>8}{'asked%':>8}"
          f"{'fixes/landed':>13}{'sent back%':>11}  tier mix")
    for name, a in sorted(sig.items()):
        n = a["approved"]
        print(f"{name:<{w}}{n:>9}{a['landed']:>7}{pc(a['escalated'], n):>6}{pc(a['blocked'], n):>8}"
              f"{pc(a['asked'], n):>8}{(a['fails'] / a['landed'] if a['landed'] else 0):>13.2f}"
              f"{pc(a['sent_back'], n):>11}  {_mix(a['tiers'])}")
    for name, a in sorted(sig.items()):
        if not a["weeks"]:
            continue
        print(f"\n{name}, by ISO week of landing")
        for wk, v in sorted(a["weeks"].items()):
            print(f"  {wk}  landed {v['landed']}  esc {pc(v['escalated'], v['landed'])}  "
                  f"fixes/landed {v['fails'] / v['landed']:.2f}  {_mix(v['tiers'])}"
                  + ("  (n<3)" if v["landed"] < 3 else ""))


def last_note(t):
    """The last bullet under the task's ## Notes, or ''."""
    _, _, notes = t["_body"].partition("## Notes")
    bullets = [l[2:].strip() for l in notes.splitlines() if l.startswith("- ")]
    return bullets[-1] if bullets else ""


def report(args):
    tasks = _tasks.load(_config.ITEMS)
    mark = _config.STATE / ".report-last"
    since = args.since
    if since == "last":
        since = mark.read_text().strip() if mark.exists() else None
    cited = {}
    try:
        rows = _gitops.git("log", *_shared.log_all(), "--format=%h %s", *([f"--since={since}"] if since else []), raw=True)
    except _gitops.GitError:
        rows = ""
    for row in rows.splitlines():
        for tid in dict.fromkeys(re.findall(r"\bT-\d{3,}\b", row)):
            cited.setdefault(tid, []).append(row)
    width = min(shutil.get_terminal_size((80, 24)).columns, 100)
    clip = lambda text: text if len(text) <= width else text[:width - 1] + "…"
    out = []

    def section(title, lines, full=()):
        """full: indexes of lines printed whole, not clipped to the terminal (a question)."""
        if lines:
            out.append(f"{title}{' (since ' + since + ')' if since and title == 'Landed' else ''}")
            out.extend(l if n in full else clip(l) for n, l in enumerate(lines))
            out.append("")

    landed = []
    for i, t in sorted(tasks.items()):
        if t["status"] == "done" and (not since or i in cited):
            landed.append(f"  {i}  {t['title']}")
            landed.extend(f"      {c}" for c in cited.get(i, [])[:3])
    section("Landed", landed)
    asking, full = [], set()
    for i, t in sorted(tasks.items()):
        if _tasks.is_asking(t):  # a worker's question: show it, not the last note
            asking.append(f"  {i}  {t['title']}  (`ergon answer {i}`)")
            qs = _dispatch.asked(t)[0]
            for n, (q, opts) in enumerate(qs, 1):
                full.add(len(asking))
                asking.append(f"      Q{n} {q}" + (f"  [{' | '.join(opts)}]" if opts else ""))
            if not qs:
                asking.append("      (no question found in its Notes)")
    section("Awaiting your answer", asking, full)
    blocked = []
    for i, t in sorted(tasks.items()):
        if t["status"] == "blocked" and not _tasks.is_asking(t):
            blocked.append(f"  {i}  {t['title']}")
            if last_note(t):
                blocked.append(f"      {last_note(t)}")
    section("Blocked", blocked)
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
    section("Human tasks ready", [f"  {_tasks.line(t, tasks).strip()}" for t in tasks.values()
                                  if t["status"] == "ready" and t.get("tier") == "human"])
    r = aggregate_stats(_config.RUNS, tasks, since=since[:10] if since else None)
    runs = sum(v["runs"] for v in r["tiers"].values())
    if runs:
        section("Spend", [f"  ${sum(v['cost'] for v in r['tiers'].values()):.2f} over {runs} run(s)"]
                + [f"    {k:<7} ${v['cost']:.2f}  {v['runs']} run(s)" for k, v in r["tiers"].items()])
    print("\n".join(out).rstrip() or "(nothing to report)")
    mark.write_text(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
