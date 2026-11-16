"""The epic replay timeline behind `ergon serve`'s /e/E-NNN: one lane per task, time across.

`build` turns the history journal into phases per task; `svg` draws them as one inline SVG
with data attributes that epic.js reads for hover and replay. Pure functions, standard library
only, nothing here reads a file or the clock: serve.py passes the rows and `now`.

Phases (kind: what the task was doing):
  approval  created -> approved                    HUMAN: waiting for approval
  queued    approved (or answered / reset) -> dispatched
  worker    dispatched or resumed -> worker_end    round 2+ is a fix round
  verify    the verify command ran                 (ends at its row, `seconds` long)
  review    the reviewer ran                       (likewise; round N is fix round N-1)
  asking    question -> answered                   HUMAN: the worker asked, nobody answered yet
  blocked   blocked -> next dispatch               HUMAN, or machine when `--escalate` retried it
  hold      review_hold -> accepted / rejected     HUMAN: held for a human to accept
  landed    a zero-length marker at the merge
A phase still going when the journal ends runs to `now` and is marked open.
"""
import datetime
import html

HUMAN = ("approval", "asking", "blocked", "hold")
COST_EVENTS = ("worker_end", "review")
W, LEFT, RIGHT = 1000, 64, 14
COST_H, AXIS_H, LANE_H, BAR_H = 64, 18, 24, 14
MIN_W = 2.0


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def ts_of(at):
    try:
        return datetime.datetime.fromisoformat(str(at)).timestamp()
    except (TypeError, ValueError):
        return None


def num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def rnd(r):
    v = r.get("round")
    return v if isinstance(v, int) and v > 0 else 1


def phases(rows, now, tokens=None):
    """The phases of one task from its journal rows: dicts with kind, start, end (epoch
    seconds), label, outcome (ok / fail / ask / wait / retry), human, open, and, where the
    journal has them, cost, tokens, reason, tier, verdict."""
    rows = sorted(((ts_of(r.get("at")), i, r) for i, r in enumerate(rows)), key=lambda x: (x[0] is None, x[0], x[1]))
    out, cur = [], None

    def begin(kind, ts, label, **kw):
        return dict(kind=kind, start=ts, label=label, human=kind in HUMAN, **kw)

    def close(c, ts, **kw):
        if c:
            out.append(dict(c, end=max(ts, c["start"]), open=False, **kw))
        return None

    for ts, _, r in rows:
        if ts is None:
            continue
        ev = r.get("event")
        if ev == "created":
            cur = close(cur, ts)
            cur = begin("approval", ts, "waiting for approval")
        elif ev == "approved":
            cur = close(cur, ts, outcome="ok")
            cur = begin("queued", ts, "queued")
        elif ev in ("dispatched", "resumed"):
            if rnd(r) > 1:
                label = f"fix round {rnd(r)}"
            else:
                label = "worker" + (" (answered)" if r.get("after") == "answer" or r.get("answered") else "")
            cur = close(cur, ts, outcome="ok")
            cur = begin("worker", ts, label, tier=r.get("tier"))
        elif ev == "worker_end":
            verdict = r.get("verdict")
            if cur and cur["kind"] == "worker":
                c = cur
            else:  # no start row: the run's own length places it
                c = begin("worker", ts - num(r.get("seconds")), f"fix round {rnd(r)}" if rnd(r) > 1 else "worker")
                cur = close(cur, c["start"], outcome="ok")
            cur = close(c, ts, outcome={"done": "ok", "asking": "ask"}.get(verdict, "fail"), verdict=verdict,
                        cost=num(r.get("cost")), tokens=tokens(r) if tokens else None, reason=r.get("reason"),
                        tier=r.get("tier") or c.get("tier"))
            if verdict == "blocked":
                cur = begin("blocked", ts, "blocked", reason=r.get("reason"))
        elif ev == "blocked":
            if cur and cur["kind"] == "blocked":
                cur["reason"] = cur.get("reason") or r.get("reason")
            elif not cur or cur["kind"] != "asking":
                cur = close(cur, ts)
                cur = begin("blocked", ts, "blocked", reason=r.get("reason"))
        elif ev == "escalated":
            if cur and cur["kind"] == "blocked":
                cur["human"] = False  # the machine retried it: not a wait on a person
                cur["label"] = "blocked, escalated"
            cur = close(cur, ts, outcome="retry")
        elif ev == "question":
            cur = close(cur, ts)
            texts = [str(q.get("text")) for q in r.get("questions") or [] if isinstance(q, dict)]
            cur = begin("asking", ts, "asking", reason="; ".join(texts))
        elif ev == "answered":
            cur = close(cur, ts, outcome="ok")
            cur = begin("queued", ts, "queued (answered)")
        elif ev in ("verify", "review"):
            sec = num(r.get("seconds"))
            if ev == "verify":
                kw = dict(outcome="ok" if r.get("ok") else "fail", reason=None if r.get("ok") else f"exit {r.get('exit')}")
            else:
                kw = dict(outcome="ok" if r.get("verdict") == "pass" else "fail", reason=r.get("reason"),
                          cost=num(r.get("cost")), verdict=r.get("verdict"), tier=r.get("reviewer"))
            label = ev + (f" {rnd(r)}" if rnd(r) > 1 else "")
            out.append(dict(begin(ev, max(ts - sec, 0), label), end=ts, open=False, **kw))
        elif ev == "review_hold":
            cur = close(cur, ts)
            cur = begin("hold", ts, "held for accept")
        elif ev == "accepted":
            cur = close(cur, ts, outcome="ok")
        elif ev == "rejected":
            was_hold = cur and cur["kind"] == "hold"
            cur = close(cur, ts, outcome="fail")
            if was_hold:
                cur = begin("queued", ts, "queued")
        elif ev == "status" and r.get("to") == "ready" and cur and cur["kind"] == "blocked":
            cur = close(cur, ts, outcome="ok")
            cur = begin("queued", ts, "queued")
        elif ev in ("landed", "closed", "epic_finished"):
            cur = close(cur, ts, outcome="ok")
            out.append(dict(begin("landed", ts, ev), end=ts, open=False, outcome="ok" if ev == "landed" else "wait"))
    if cur:
        out.append(dict(cur, end=max(now, cur["start"]), open=True, outcome="wait"))
    out.sort(key=lambda p: (p["start"], p["end"]))
    return out


def build(rows, tasks, eid, now, tokens=None):
    """{'lanes': [{id, title, phases}], 't0', 't1', 'costs': [(ts, task, cost, cumulative)]}
    for epic eid: every task of the epic with at least one phase. `tokens(row)` -> int or None."""
    mine = {}
    for r in rows:
        tid = r.get("task")
        if tid and (r.get("epic") == eid or tasks.get(tid, {}).get("epic") == eid):
            mine.setdefault(tid, []).append(r)
    lanes = []
    for tid in sorted(mine):
        ph = phases(mine[tid], now, tokens)
        if ph:
            lanes.append({"id": tid, "title": (tasks.get(tid) or {}).get("title", ""), "phases": ph})
    if not lanes:
        return {"lanes": [], "t0": now, "t1": now, "costs": []}
    t0 = min(p["start"] for ln in lanes for p in ln["phases"])
    t1 = max(p["end"] for ln in lanes for p in ln["phases"])
    events = sorted((ts_of(r.get("at")), r["task"], num(r.get("cost"))) for rs in mine.values() for r in rs
                    if r.get("event") in COST_EVENTS and num(r.get("cost")) and ts_of(r.get("at")) is not None)
    costs, total = [], 0.0
    for ts, tid, c in events:
        total += c
        costs.append((ts, tid, c, total))
    return {"lanes": lanes, "t0": t0, "t1": max(t1, t0 + 1), "costs": costs}


def dur(s):
    s = int(max(s, 0))
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s" if m else f"{s}s"


def money(x):
    return f"${x:.2f}"


def tip(lane, p):
    bits = [f"{lane['id']} · {p['label']}"]
    if p["human"]:
        bits.append("waiting on a human")
    if p["kind"] != "landed":
        bits.append(dur(p["end"] - p["start"]) + (" and still going" if p.get("open") else ""))
    for key in ("verdict", "tier"):
        if p.get(key):
            bits.append(str(p[key]))
    if p.get("cost"):
        bits.append(money(p["cost"]))
    if p.get("tokens"):
        bits.append(f"{p['tokens']:,} tokens")
    if p.get("reason"):
        bits.append(str(p["reason"])[:300])
    return " · ".join(bits)


def svg(tl):
    """The SVG for build()'s result. Every phase is a <rect class="seg ..."> with data-s / data-e
    (seconds from the start), data-w (its full width) and data-tip; the cost line is one <path
    class=cost> per step with data-s and data-c, so epic.js can reveal them as the replay goes."""
    lanes, t0, t1 = tl["lanes"], tl["t0"], tl["t1"]
    span = t1 - t0
    plot = W - LEFT - RIGHT
    top = COST_H + AXIS_H
    height = top + LANE_H * len(lanes) + 6

    def x(ts):
        return LEFT + (ts - t0) / span * plot

    o = [f'<svg id=tl class=timeline viewBox="0 0 {W} {height}" width="{W}" height="{height}" role=img '
         f'aria-label="epic timeline" data-span="{span:.1f}">',
         '<defs><pattern id=hatch width=6 height=6 patternUnits=userSpaceOnUse patternTransform="rotate(45)">'
         '<rect class=hatchbg width=6 height=6 /><line class=hatchline x1=0 y1=0 x2=0 y2=6 /></pattern></defs>']
    peak = tl["costs"][-1][3] if tl["costs"] else 0
    base = COST_H - 6
    scale = (COST_H - 18) / peak if peak else 0
    o.append(f'<line class=axis x1={LEFT} x2={W - RIGHT} y1={base} y2={base} />')
    o.append(f'<text class=small x=2 y=12>cost</text><text class=small x=2 y=26 id=cost-now>{money(peak)}</text>')
    px, py = LEFT, base
    for ts, tid, c, cum in tl["costs"]:
        nx, ny = x(ts), base - cum * scale
        o.append(f'<path class=cost d="M{px:.1f} {py:.1f}H{nx:.1f}V{ny:.1f}" data-s="{ts - t0:.1f}" data-c="{cum:.4f}" '
                 f'data-tip="{esc(f"{tid} +{money(c)} · epic total {money(cum)}")}" />')
        px, py = nx, ny
    if tl["costs"]:
        o.append(f'<path class=cost d="M{px:.1f} {py:.1f}H{x(t1):.1f}" data-s="{tl["costs"][-1][0] - t0:.1f}" '
                 f'data-c="{peak:.4f}" />')
    for i in range(6):
        ts = t0 + span * i / 5
        label = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        anchor = "start" if i == 0 else "end" if i == 5 else "middle"
        o.append(f'<line class=tick x1={x(ts):.1f} x2={x(ts):.1f} y1={COST_H} y2={height} />'
                 f'<text class=small x={x(ts):.1f} y={COST_H + 12} text-anchor={anchor}>{esc(label)}</text>')
    for i, ln in enumerate(lanes):
        y = top + i * LANE_H
        o.append(f'<rect class="lane{" alt" if i % 2 else ""}" x=0 y={y} width={W} height={LANE_H} />')
        o.append(f'<a href="/t/{esc(ln["id"])}"><text class=lanelabel x=4 y={y + 16}>{esc(ln["id"])}</text></a>')
        for p in ln["phases"]:
            px0, px1 = x(p["start"]), x(p["end"])
            w = max(px1 - px0, MIN_W)
            cls = (f'seg k-{p["kind"]} o-{p.get("outcome", "wait")} {"human" if p["human"] else "machine"}'
                   + (" open" if p.get("open") else ""))
            o.append(f'<rect class="{cls}" x={px0:.1f} y={y + (LANE_H - BAR_H) / 2:.1f} width={w:.1f} height={BAR_H} '
                     f'rx=2 data-s="{p["start"] - t0:.1f}" data-e="{p["end"] - t0:.1f}" data-w="{w:.1f}" '
                     f'data-tip="{esc(tip(ln, p))}" />')
    o.append(f'<line id=head class=head x1={LEFT} x2={LEFT} y1=0 y2={height} visibility=hidden />')
    o.append("</svg>")
    return "".join(o)


def totals(tl):
    """(human seconds, machine seconds) summed over every phase but the zero-length markers."""
    h = m = 0.0
    for ln in tl["lanes"]:
        for p in ln["phases"]:
            d = p["end"] - p["start"]
            if p["human"]:
                h += d
            elif p["kind"] != "landed":
                m += d
    return h, m
