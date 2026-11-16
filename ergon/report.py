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
from . import gitops as _gitops
from . import tasks as _tasks


RUN_LOG = re.compile(r"^(?P<id>[A-Z]+-\d+)-(?P<date>\d{4})(?P<m>\d{2})(?P<d>\d{2})-\d{6}(?:-\w+)?\.json$")


def aggregate_stats(runs_dir, tasks, since=None, epic=None):
    """Per-tier numbers from the dispatch logs in runs_dir, joined with tasks (id -> meta).
    since: 'YYYY-MM-DD' (runs started that day or later); epic: only that epic's tasks.
    Returns {"tiers": {tier: {runs, done, blocked, timeouts, cost}}, "repeat": {id: blocked
    count} for tasks blocked more than once, "skipped": logs that were unreadable, not JSON
    (and not a timeout) or whose task is gone}. A reviewer's log (<ID>-<time>-review[N].json) and
    a fix round's worker log (<ID>-<time>-fix<N>.json) add their cost to the task's tier but
    aren't runs. Never raises on a bad log."""
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
    tasks = _tasks.load(_config.ITEMS)
    r = aggregate_stats(_config.RUNS, tasks, since=args.since, epic=args.epic)
    print(f"{'tier':<8}{'runs':>5}{'done':>6}{'blocked':>8}{'block%':>8}{'cost':>10}{'mean':>9}{'timeouts':>9}")
    for tier in [*_tasks.TIERS, *(k for k in r["tiers"] if k not in _tasks.TIERS)]:
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
    tasks = _tasks.load(_config.ITEMS)
    mark = _config.STATE / ".report-last"
    since = args.since
    if since == "last":
        since = mark.read_text().strip() if mark.exists() else None
    cited = {}
    try:
        rows = _gitops.git("log", "--all", "--format=%h %s", *([f"--since={since}"] if since else []), raw=True)
    except _gitops.GitError:
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
    section("Human tasks ready", [f"  {_tasks.line(t, tasks).strip()}" for t in tasks.values()
                                  if t["status"] == "ready" and t.get("tier") == "human"])
    r = aggregate_stats(_config.RUNS, tasks, since=since[:10] if since else None)
    runs = sum(v["runs"] for v in r["tiers"].values())
    if runs:
        section("Spend", [f"  ${sum(v['cost'] for v in r['tiers'].values()):.2f} over {runs} run(s)"]
                + [f"    {k:<7} ${v['cost']:.2f}  {v['runs']} run(s)" for k, v in r["tiers"].items()])
    print("\n".join(out).rstrip() or "(nothing to report)")
    mark.write_text(datetime.datetime.now().isoformat(timespec="seconds") + "\n")
