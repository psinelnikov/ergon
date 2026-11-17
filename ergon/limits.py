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
import tempfile

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
