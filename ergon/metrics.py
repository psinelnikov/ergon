"""The numbers and inline SVG charts behind `ergon serve`'s /metrics page.

compute() reads the run logs (aggregate_stats, so the per-tier figures are the ones `ergon stats`
prints for the same range), the history journal and the transcripts; render() turns that into
HTML. Charts are plain SVG with classes (no style attributes: the CSP forbids them); colours
live in serve.css, so light and dark both work.
"""
import datetime
import statistics

from . import history as _history
from . import report as _report
from . import tasks as _tasks

REBUILD_USD_PER_MTOK = {"opus": 18.75, "sonnet": 3.75, "haiku": 1.25}  # 5-minute cache write, an estimate


def _day(at):
    return str(at or "")[:10]


def _ts(at):
    try:
        return datetime.datetime.fromisoformat(str(at)).timestamp()
    except (TypeError, ValueError):
        return None


def pct(v, p):
    v = sorted(v)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def spread(vals):
    return {"n": len(vals), "median": statistics.median(vals) if vals else None, "p90": pct(vals, 0.9)}


def done_by_category(tasks, epic=None):
    """{category: done tasks}, every category listed; a task with none counts as a feature."""
    out = dict.fromkeys(_tasks.CATEGORIES, 0)
    for t in tasks.values():
        if t.get("status") == "done" and (not epic or t.get("epic") == epic):
            out[_tasks.category(t)] += 1
    return out


def compute(tasks, rows, runs_dir, since=None, until=None, epic=None, summary=None, worktree_of=None):
    """summary(session, worktree) -> transcript summary or None; worktree_of(rows, tid) -> path."""
    stats = _report.aggregate_stats(runs_dir, tasks, since=since, epic=epic, until=until)
    keep = []
    for r in rows:
        d = _day(r.get("at"))
        if (since and d < since) or (until and d > until):
            continue
        if epic and r.get("epic") != epic:
            continue
        keep.append(r)
    by = {}
    for r in keep:
        by.setdefault(r.get("task"), []).append(r)
    tier_of = lambda tid: (tasks.get(tid) or {}).get("tier") or "?"

    esc = {}
    for r in keep:
        if r["event"] == "escalated":
            k = f"{r.get('from')} → {r.get('to')}"
            esc[k] = esc.get(k, 0) + 1

    veto = {}
    fixed = [0, 0]  # fail reviews followed by a pass, fail reviews
    for tid, trail in by.items():
        reviews = [r for r in trail if r["event"] == "review" and r.get("verdict") in ("pass", "fail")]
        for i, r in enumerate(reviews):
            v = veto.setdefault(r.get("tier") or tier_of(tid), [0, 0])
            v[1] += 1
            v[0] += r["verdict"] == "fail"
            if r["verdict"] == "fail" and i + 1 < len(reviews):
                fixed[1] += 1
                fixed[0] += reviews[i + 1]["verdict"] == "pass"
            elif r["verdict"] == "fail":
                fixed[1] += 1
    landed = {tid: next(r for r in trail if r["event"] == "landed") for tid, trail in by.items()
              if any(r["event"] == "landed" for r in trail)}
    rounds = {tid: sum(r["event"] == "review" and r.get("verdict") == "fail" for r in by[tid]) for tid in landed}

    def gap(a, b):
        out = []
        for tid, trail in by.items():
            t0 = next((_ts(r["at"]) for r in trail if r["event"] in a), None)
            if t0 is None:
                continue
            t1 = next((_ts(r["at"]) for r in trail if r["event"] in b and (_ts(r["at"]) or 0) >= t0), None)
            if t1 is not None:
                out.append(t1 - t0)
        return out

    created = {r["task"]: _ts(r["at"]) for r in rows if r["event"] == "created" and r.get("task")}
    lead = [_ts(r["at"]) - created[tid] for tid, r in landed.items()
            if tid in created and _ts(r["at"]) is not None and created[tid] is not None]
    weeks = {}
    for r in landed.values():
        try:
            y, w, _ = datetime.date.fromisoformat(_day(r["at"])).isocalendar()
        except ValueError:
            continue
        weeks[f"{y}-W{w:02d}"] = weeks.get(f"{y}-W{w:02d}", 0) + 1

    tokens, denials, rebuilds = {}, {}, {"n": 0, "tokens": 0, "usd": 0.0}
    seen = set()
    for tid, trail in by.items():
        for r in trail:
            sid = r.get("session")
            if r["event"] != "worker_end" or not sid or (tid, sid) in seen or not summary:
                continue
            seen.add((tid, sid))
            s = summary(sid, worktree_of(rows, tid))
            if not s:
                continue
            for k, n in s["tokens"].items():
                tokens[k] = tokens.get(k, 0) + n
            for k, n in s["denials"].items():
                denials[k] = denials.get(k, 0) + n
            for _at, wrote in s["cache_rebuilds"]:
                rebuilds["n"] += 1
                rebuilds["tokens"] += wrote
                rebuilds["usd"] += wrote / 1e6 * REBUILD_USD_PER_MTOK.get(tier_of(tid), 0)
    return {
        "stats": stats, "categories": done_by_category(tasks, epic), "escalations": esc, "veto": veto, "fixed": fixed,
        "landed": len(landed), "rounds": rounds,
        "latency": {"proposed → approved": spread(gap(("created",), ("approved",))),
                    "asking → answered": spread(gap(("question",), ("answered",))),
                    "review → accepted": spread(gap(("review_hold",), ("accepted",)))},
        "lead": spread(lead), "weeks": dict(sorted(weeks.items())),
        "pyramid": _history.catches(keep),
        "tokens": tokens, "denials": denials, "rebuilds": rebuilds,
        "empty": not stats["tiers"] and not keep,
    }


# ---------- charts ----------

def bars(items, esc, fmt=lambda v: f"{v:g}", cls="b1", label="chart"):
    """Horizontal bars: items [(label, value)]."""
    items = [(k, v) for k, v in items]
    if not items:
        return ""
    top = max(v for _, v in items) or 1
    h = 22 * len(items) + 4
    out = [f'<svg class=chart viewBox="0 0 560 {h}" role=img aria-label="{esc(label)}">',
           '<line class=axis x1=130 y1=0 x2=130 y2=%d />' % h]
    for i, (k, v) in enumerate(items):
        y = 22 * i + 4
        w = round(330 * v / top)
        out.append(f'<text x=124 y={y + 12} text-anchor=end>{esc(k)}</text>'
                   f'<rect class={cls} x=130 y={y} width={max(w, 1) if v else 0} height=16 rx=2 />'
                   f'<text class=m x={136 + w} y={y + 12}>{esc(fmt(v))}</text>')
    out.append("</svg>")
    return "".join(out)


def columns(items, esc, label="chart"):
    """Vertical columns: items [(label, count)]."""
    if not items:
        return ""
    top = max(v for _, v in items) or 1
    n = len(items)
    step = min(60, 520 // n)
    out = [f'<svg class=chart viewBox="0 0 560 150" role=img aria-label="{esc(label)}">',
           '<line class=axis x1=20 y1=120 x2=550 y2=120 />']
    for i, (k, v) in enumerate(items):
        x = 24 + i * step
        hh = round(90 * v / top)
        out.append(f'<rect class=b3 x={x} y={120 - hh} width={max(step - 8, 4)} height={hh} rx=2 />'
                   f'<text class=m x={x + max(step - 8, 4) // 2} y={114 - hh} text-anchor=middle>{v}</text>'
                   f'<text class=m x={x + max(step - 8, 4) // 2} y=136 text-anchor=middle>{esc(k[5:])}</text>')
    out.append("</svg>")
    return "".join(out)


def stacked(weeks, layers, esc, label="chart"):
    """Stacked columns per week: weeks {week: {layer: n}}, layers in stack order (bottom first)."""
    if not weeks:
        return ""
    top = max(sum(w.values()) for w in weeks.values()) or 1
    step = min(60, 520 // len(weeks))
    bw = max(step - 8, 4)
    out = [f'<svg class=chart viewBox="0 0 560 150" role=img aria-label="{esc(label)}">',
           '<line class=axis x1=20 y1=120 x2=550 y2=120 />']
    for i, (wk, w) in enumerate(weeks.items()):
        x = 24 + i * step
        y = 120
        for j, k in enumerate(layers):
            n = w.get(k, 0)
            if not n:
                continue
            hh = max(round(90 * n / top), 1)
            y -= hh
            out.append(f'<rect class=b{j % 7 + 1} x={x} y={y} width={bw} height={hh}><title>{esc(k)}: {n}</title></rect>')
        out.append(f'<text class=m x={x + bw // 2} y=136 text-anchor=middle>{esc(wk[5:])}</text>')
    out.append("</svg>")
    return "".join(out)


def dur(s):
    if s is None:
        return "—"
    s = int(s)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def table(head, rows, esc):
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td{' class=num' if i else ''}>{esc(c)}</td>" for i, c in enumerate(r)) + "</tr>"
                 for r in rows)
    return f'<div class=table><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>'


def section(title, chart, tbl):
    return f"<h2>{title}</h2><div class=pair><div>{chart}</div><div>{tbl}</div></div>"


def render(m, esc, money, filters):
    """The page body. filters: {epic, since, until, epics: [ids]}."""
    opts = "".join(f'<option value="{esc(e)}"{" selected" if e == filters["epic"] else ""}>{esc(e)}</option>'
                   for e in ["", *filters["epics"]])
    b = ['<div class=top><h1>Metrics</h1><span class=crumb><a href="/">board</a></span></div>',
         '<form class=filter method=get action="/metrics">'
         f'<label>epic<select name=epic>{opts}</select></label>'
         f'<label>from<input type=date name=since value="{esc(filters["since"] or "")}"></label>'
         f'<label>to<input type=date name=until value="{esc(filters["until"] or "")}"></label>'
         '<button>Show</button></form>']
    if m["empty"]:
        b.append("<p>No runs yet in this range, so there is nothing to chart.</p>")
        return "".join(b)
    tiers = [(k, v) for k in [*_tasks.TIERS, *m["stats"]["tiers"]] if (v := m["stats"]["tiers"].get(k))]
    tiers = list(dict(tiers).items())
    rows = [(k, v["runs"], v["done"], v["blocked"], f"{100 * v['blocked'] / (v['runs'] or 1):.0f}%",
             money(v["cost"]), money(v["cost"] / (v["runs"] or 1))) for k, v in tiers]
    b.append(section("Runs and cost per tier",
                     bars([(k, v["cost"]) for k, v in tiers], esc, money, "b1", "cost per tier")
                     + bars([(f"{k} block%", 100 * v["blocked"] / (v["runs"] or 1)) for k, v in tiers], esc,
                            lambda v: f"{v:.0f}%", "b2", "block rate per tier"),
                     table(["tier", "runs", "done", "blocked", "block%", "cost", "mean"], rows, esc)
                     + (table(["escalation", "n"], sorted(m["escalations"].items()), esc) if m["escalations"] else "")))
    b.append(section("Done tasks per category", bars(list(m["categories"].items()), esc, str, "b1", "done per category"),
                     table(["category", "done"], list(m["categories"].items()), esc)))
    v = m["veto"]
    fx = m["fixed"]
    r = m["rounds"]
    mean = f"{sum(r.values()) / len(r):.2f}" if r else "—"
    b.append(section("Review",
                     bars([(k, 100 * a / n) for k, (a, n) in sorted(v.items())], esc, lambda x: f"{x:.0f}%", "b4",
                          "reviewer veto rate per worker tier"),
                     table(["worker tier", "reviews", "vetoes", "veto rate"],
                           [(k, n, a, f"{100 * a / n:.0f}%") for k, (a, n) in sorted(v.items())], esc)
                     + table(["measure", "value"],
                             [("fix rounds per landed task", f"{mean} over {len(r)}"),
                              ("rounds that fixed it", f"{fx[0]} of {fx[1]}")], esc)))
    lat = m["latency"]
    lead = m["lead"]
    b.append(section("Human latency and lead time",
                     bars([(k, x["median"] or 0) for k, x in lat.items()] + [("created → landed", lead["median"] or 0)],
                          esc, dur, "b1", "median latency"),
                     table(["interval", "n", "median", "p90"],
                           [(k, x["n"], dur(x["median"]), dur(x["p90"])) for k, x in [*lat.items(), ("created → landed", lead)]],
                           esc)))
    w = m["weeks"]
    b.append(section("Landed per week", columns(list(w.items()), esc, "tasks landed per week"),
                     table(["week", "landed"], list(w.items()), esc) if w else "<p>Nothing has landed in this range.</p>"))
    py = m["pyramid"]
    if py["totals"]:
        cols = _report.layer_order(py["totals"])
        late = f"{100 * py['late']:.0f}%" if py["late"] is not None else "—"
        legend = "<p>" + " · ".join(f"{esc(k)}" for k in cols) + " (bottom to top)</p>"
        b.append(section("Trust pyramid: catches per layer",
                         stacked(py["weeks"], cols, esc, "catches per layer per week") + legend,
                         table(["week", *cols], [(wk, *(w.get(k, 0) for k in cols)) for wk, w in py["weeks"].items()], esc)
                         + table(["measure", "value"],
                                 [("caught by review + human", late),
                                  ("answers to questions (not catches)", py["answers"])], esc)))
    tk = m["tokens"]
    rb = m["rebuilds"]
    if tk:
        b.append(section("Tokens", bars(list(tk.items()), esc, lambda x: f"{x:,}", "b1", "tokens by kind"),
                         table(["kind", "tokens"], [(k, f"{x:,}") for k, x in tk.items()], esc)
                         + table(["cache rebuilds", "tokens rewritten", "est. cost"],
                                 [(rb["n"], f"{rb['tokens']:,}", money(rb["usd"]))], esc)))
    else:
        b.append("<h2>Tokens</h2><p>No transcripts found for these runs.</p>")
    d = m["denials"]
    b.append("<h2>Denials by tool</h2>" + (section("", bars(sorted(d.items()), esc, lambda x: f"{x}", "b2", "denials"),
                                                   table(["tool", "denials"], sorted(d.items()), esc))
                                           if d else "<p>None.</p>"))
    return "".join(b)
