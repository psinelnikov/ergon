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
