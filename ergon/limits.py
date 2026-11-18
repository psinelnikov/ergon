"""What a dispatch run costs as a share of the plan's 5-hour limit.

Claude Code reports a subscription's 5-hour window on the stream-json `rate_limit_event`
message: its utilization (0..1, in steps of 0.01) and when it resets. It comes once per
session, from response headers, so it can't be read per task: workers run side by side and
share the window. Instead dispatch reads it before launching and after the last worker ends,
with one tiny haiku call each (probe()), and records the pair with the run's cost as a `window`
history row. Every measurement in the same window gives a rate, percentage points per dollar
of the cost `claude -p` reports; rate() pools the recent ones (sum of points over sum of dollars),
so whole-percent steps and small runs average out. A task's share is then its cost times the
rate. The window also counts anything else using the plan meanwhile (an interactive session),
so the rate is an upper bound, never an exact split.

On an API key there is no window: probe() returns None and nothing is shown. `limit_probe =
false` in .ergon.toml turns the probes off.
"""
import json
import re
import tempfile
import time

from . import config as _config
from . import history as _history

PROBE_TIMEOUT = 90  # seconds
POOL = 10  # the last this many same-window measurements make the rate


def enabled():
    v = _config.CFG.get("limit_probe", True)
    if not isinstance(v, bool):
        raise _config.NotConfigured(f"{_config.CONFIG_FILE}: limit_probe is true or false")
    return v


def parse(out):
    """The 5-hour window in `claude -p --output-format stream-json` output, as
    {"utilization": 0..1, "resets_at": epoch seconds, "cost": the call's USD}, or None."""
    win, cost = None, 0.0
    for line in (out or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "result":
            cost = float(d.get("total_cost_usd") or 0)
        if d.get("type") != "rate_limit_event" or win:
            continue
        info = d.get("rate_limit_info") or {}
        w = (info.get("unifiedWindows") or {}).get("five_hour")
        if not w and info.get("rateLimitType") == "five_hour" and "utilization" in info:
            w = {"utilization": info["utilization"], "resetsAt": info.get("resetsAt")}
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            win = {"utilization": float(w["utilization"]), "resets_at": w.get("resetsAt")}
    return dict(win, cost=cost) if win else None


def probe():
    """Read the 5-hour window with one minimal haiku call (about $0.003), or None: no
    subscription window, `claude` failed, a second Ctrl-C, or probes are off. Never raises."""
    from . import dispatch as _dispatch  # dispatch imports this module
    try:
        if not enabled():
            return None
        with tempfile.TemporaryDirectory() as cwd:  # no project hooks or CLAUDE.md to load
            out, _ = _dispatch.run_proc(["claude", "-p", "--model", "haiku", "--max-turns", "1",
                                         "--output-format", "stream-json", "--verbose", "Reply: ok"],
                                        cwd, PROBE_TIMEOUT)
        return parse(out)
    except _config.NotConfigured:
        raise
    except Exception:  # noqa: BLE001 - a probe is a nicety, never a reason to stop
        return None


def measure(before, after, cost):
    """Record the run's `window` row and return it (None without both readings). `cost`: what
    the run's workers and reviewers cost; the probes' own cost is added. `same` is false when
    the window reset in between, and such a row gives no rate."""
    if not (before and after):
        return None
    same = before.get("resets_at") == after.get("resets_at") and after["utilization"] >= before["utilization"]
    row = {"before": before["utilization"], "after": after["utilization"], "resets_at": after.get("resets_at"),
           "cost": round(cost + before.get("cost", 0) + after.get("cost", 0), 6), "same": same}
    _history.record("window", **row)
    return row


def rate(rows=None):
    """(percentage points of the 5-hour window per USD, how many runs measured it), pooled over
    the last POOL same-window `window` rows; (None, 0) before any."""
    rows = _history.read() if rows is None else rows
    got = [r for r in rows if r.get("event") == "window" and r.get("same")
           and isinstance(r.get("cost"), (int, float)) and r["cost"] > 0][-POOL:]
    dollars = sum(r["cost"] for r in got)
    if not got or dollars <= 0:
        return None, 0
    points = sum(100 * (r["after"] - r["before"]) for r in got)
    return points / dollars, len(got)


def pct(cost, per_dollar):
    """'6.3%' for a cost at a rate, '<0.1%' for a sliver."""
    p = cost * per_dollar
    return "<0.1%" if 0 < p < 0.05 else f"{p:.1f}%"


# ---------- limit stops ----------
# When the plan's limit runs out mid-run, `claude -p --output-format json` still prints a result,
# in one of these shapes (the first one is what dispatch's own logs show today):
#   {"is_error": true, "subtype": "success", "terminal_reason": "api_error", "api_error_status": 429,
#    "result": "You've hit your 5-hour limit · resets 10pm (Europe/Lisbon)"}
#   the same with "You've hit your limit · resets 3pm (America/New_York)", "5-hour limit reached ∙
#   resets 6:30pm", or a weekly one, "You've hit your weekly limit · resets Nov 21, 3pm (…)"
#   {"is_error": false, "subtype": "success", "result": "Claude AI usage limit reached|1794935400"}
#   (older Claude Code: the reset as epoch seconds, and no error flag)
# The text alone decides, and only when it is the whole reply (one short line): a worker's long
# report that talks about limits is never one. stop() is the one place these patterns live.

RETRY_MINUTES = 30  # no reset in the message and none from probe(): try again this much later
_EPOCH = re.compile(r"^Claude AI usage limit reached\|(\d{9,11})\s*$")
_LIMIT = re.compile(r"(?i)^(?:you(?:'|’)ve hit|you have hit|you(?:'|’)ve reached|you have reached)\s+your\s+"
                    r"(?:[\w-]+\s+){0,3}limit\b|^(?:[\w-]+\s+){0,3}limit reached\b|^claude ai usage limit reached\b")
_RESETS = re.compile(r"(?i)\bresets\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2,8})\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
                     r"(?P<h>\d{1,2})(?::(?P<m>\d\d))?\s*(?P<ap>am|pm)?\b(?:\s*\((?P<tz>[\w/+-]+)\))?")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def stop(d, now=None):
    """Is a `claude -p --output-format json` result `d` (the parsed dict) a stop by the plan's
    usage limit? {"text": the message, "resets_at": epoch seconds, or None when the message gives
    no reset that can be read} if so, else None. An ordinary error, a 429 without a limit message
    and a reply that merely mentions a limit are not."""
    if not isinstance(d, dict):
        return None
    text = str(d.get("result") or "").strip()
    if not text or "\n" in text or len(text) > 200:
        return None
    m = _EPOCH.match(text)
    if m:
        return {"text": text, "resets_at": int(m[1])}
    if not _LIMIT.search(text):
        return None
    return {"text": text, "resets_at": reset_time(text, now)}


def reset_time(text, now=None):
    """The epoch seconds a limit message's `resets <when>` names, or None. A time alone is the
    next one at or after now (an hour's slack: the message may be read a little after it was
    written); a date without a year is this year's, or next year's if that one is long past. The
    zone in brackets counts when this Python knows it, else local time."""
    import datetime
    m = _RESETS.search(text or "")
    if not m:
        return None
    h, mi, ap = int(m["h"]), int(m["m"] or 0), (m["ap"] or "").lower()
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    tz = None
    if m["tz"]:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m["tz"])
        except Exception:  # noqa: BLE001 - an unknown zone, or no tz database: local time
            tz = None
    now_dt = datetime.datetime.fromtimestamp(now if now is not None else time.time(), tz).astimezone(tz)
    if m["mon"]:
        mon = m["mon"][:3].lower()
        if mon not in _MONTHS:
            return None
        try:
            at = now_dt.replace(month=_MONTHS.index(mon) + 1, day=int(m["day"]), hour=h, minute=mi,
                                second=0, microsecond=0)
        except ValueError:
            return None
        if at < now_dt - datetime.timedelta(days=30):
            at = at.replace(year=at.year + 1)
        return int(at.timestamp())
    at = now_dt.replace(hour=h, minute=mi, second=0, microsecond=0)
    if at < now_dt - datetime.timedelta(hours=1):
        at += datetime.timedelta(days=1)
    return int(at.timestamp())


def resets_at(lim, now=None):
    """When to try again after limit stop `lim` (stop()'s): the reset its message names, else the
    one probe() reads, else RETRY_MINUTES from now; a reset that isn't ahead counts as none (the
    task would go straight back into the limit). Epoch seconds."""
    now = time.time() if now is None else now
    if isinstance(lim.get("resets_at"), (int, float)) and lim["resets_at"] > now:
        return int(lim["resets_at"])
    win = probe()
    at = (win or {}).get("resets_at")
    if isinstance(at, (int, float)) and at > now:
        return int(at)
    return int(now + RETRY_MINUTES * 60)


ISO = "%Y-%m-%dT%H:%M:%SZ"


def iso(epoch):
    """Epoch seconds as the UTC ISO time a `limited:` mark holds."""
    import datetime
    return datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc).strftime(ISO)


def clock(epoch, now=None):
    """Epoch seconds as local `HH:MM`, with the date in front when it isn't today."""
    import datetime
    at = datetime.datetime.fromtimestamp(int(epoch))
    today = datetime.datetime.fromtimestamp(now if now is not None else time.time()).date()
    return at.strftime("%H:%M") if at.date() == today else at.strftime("%Y-%m-%d %H:%M")
