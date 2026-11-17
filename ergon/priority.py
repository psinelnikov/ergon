"""Priority score for ordering work: no I/O. tasks maps id -> task dict."""
from .tasks import TIERS

SIZE = {"haiku": 1, "sonnet": 3, "opus": 8, "human": 3}
NEUTRAL_COD = 50
UNBLOCK_WEIGHT = 25
CLOSED = {"done", "dropped"}


def unblocks(t, tasks):
    """Open tasks that depend on t, directly or transitively. Cycle-safe."""
    tid = t.get("id")
    dependents = {}
    for i, o in tasks.items():
        for d in o.get("depends") or []:
            dependents.setdefault(d, []).append(i)
    seen, stack = set(), [tid]
    while stack:
        for i in dependents.get(stack.pop(), []):
            if i not in seen and i != tid:
                seen.add(i)
                stack.append(i)
    return sum(1 for i in seen if tasks[i].get("status") not in CLOSED)


def cod(t):
    try:
        v = float(t.get("cod"))
    except (TypeError, ValueError):
        return float(NEUTRAL_COD)
    if v != v:  # NaN
        return float(NEUTRAL_COD)
    return min(100.0, max(0.0, v))


def score(t, tasks):
    return (cod(t) + UNBLOCK_WEIGHT * unblocks(t, tasks)) / SIZE[t.get("tier")]


def _bucket(t):
    return {"first": 0, "last": 2}.get(str(t.get("pin") or "").strip(), 1)


def sort_key(t, tasks):
    return (_bucket(t), -score(t, tasks), TIERS.index(t.get("tier")), t.get("id"))
