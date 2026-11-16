"""What a worker session spent, read from Claude Code's own transcripts.

Ported from SClaude's sclaude/lib/metrics/claude_usage.py (read, project_name), MIT:

    Copyright (c) SClaude contributors

    Permission is hereby granted, free of charge, to any person obtaining a copy of this
    software and associated documentation files (the "Software"), to deal in the Software
    without restriction, including without limitation the rights to use, copy, modify, merge,
    publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
    to whom the Software is furnished to do so, subject to the following conditions: The above
    copyright notice and this permission notice shall be included in all copies or substantial
    portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

A session leaves ~/.claude/projects/<cwd, non-alphanumerics as "-">/<session>.jsonl, and its
subagents under <session>/subagents/*.jsonl. Every reader here returns None, never raises, when
a transcript is missing or moved.
"""
import json
import os
import re

ROOT = os.path.expanduser("~/.claude/projects")
VERSION = 1  # bumped when a summary gains a field: every cached transcript is read again
CACHE_REBUILT_MIN = 20000  # below this, a request writing more than it reads is a short context growing


def project_name(path):
    """The directory Claude Code keeps a working directory's sessions in."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _load(line):
    try:
        e = json.loads(line)
    except ValueError:
        return {}
    return e if isinstance(e, dict) else {}


def read(path):
    """One transcript's summary dict, or None when it cannot be read."""
    usage, tools, seen_tools, requested, runs, denials = {}, {}, set(), {}, {}, {}
    compactions = errors = 0
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    with fh:
        for line in fh:
            if b'"type":"assistant"' in line:
                entry = _load(line)
                msg = entry.get("message") or {}
                if entry.get("isApiErrorMessage"):
                    errors += 1
                if isinstance(msg.get("usage"), dict) and msg.get("id"):
                    usage[msg["id"]] = (msg.get("model"), msg["usage"])
                    if not entry.get("isSidechain"):
                        requested.setdefault(msg["id"], entry.get("timestamp"))
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use" \
                            and block.get("id") not in seen_tools:
                        seen_tools.add(block.get("id"))
                        name = block.get("name") or "?"
                        tools[name] = tools.get(name, 0) + 1
                continue
            if b'"cost-state"' in line:
                entry = _load(line)
                if entry.get("type") == "cost-state":
                    runs[str(entry.get("startTime"))] = entry
                continue
            if b'"toolDenialKind"' in line:
                kind = _load(line).get("toolDenialKind")
                if kind:
                    denials[kind] = denials.get(kind, 0) + 1
            if b'"isCompactSummary":true' in line:
                compactions += 1

    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "thinking": 0}
    cost, models, lines_added, lines_removed, api_ms, tool_ms = None, {}, 0, 0, 0, 0
    if runs:  # Claude Code's own cumulative account; used as is
        cost = 0.0
        for run in runs.values():
            cost += float(run.get("totalCostUSD") or 0)
            lines_added += int(run.get("totalLinesAdded") or 0)
            lines_removed += int(run.get("totalLinesRemoved") or 0)
            api_ms += int(run.get("totalAPIDuration") or 0)
            tool_ms += int(run.get("totalToolDuration") or 0)
            for model, u in (run.get("modelUsage") or {}).items():
                tokens["input"] += int(u.get("inputTokens") or 0)
                tokens["output"] += int(u.get("outputTokens") or 0)
                tokens["cache_read"] += int(u.get("cacheReadInputTokens") or 0)
                tokens["cache_write"] += int(u.get("cacheCreationInputTokens") or 0)
                tokens["thinking"] += int(u.get("thinkingTokens") or 0)
                models[model] = models.get(model, 0) + float(u.get("costUSD") or 0)
    else:  # an older Claude Code, or a subagent: the messages' usage, summed
        for model, u in usage.values():
            tokens["input"] += int(u.get("input_tokens") or 0)
            tokens["output"] += int(u.get("output_tokens") or 0)
            tokens["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            tokens["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
            tokens["thinking"] += int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    # A request, past the first, that wrote more to the cache than it read and more than
    # CACHE_REBUILT_MIN: what an expired cache looks like. [[when, tokens written]]
    rebuilds = []
    for i, (mid, at) in enumerate(requested.items()):
        u = usage[mid][1]
        wrote = int(u.get("cache_creation_input_tokens") or 0)
        if i and wrote > int(u.get("cache_read_input_tokens") or 0) and wrote > CACHE_REBUILT_MIN:
            rebuilds.append([at, wrote])
    return {"cost": cost, "models": models, "tokens": tokens, "tools": tools, "denials": denials,
            "compactions": compactions, "api_errors": errors, "cache_rebuilds": rebuilds,
            "lines_added": lines_added, "lines_removed": lines_removed,
            "api_ms": api_ms, "tool_ms": tool_ms}


def _merge(a, b):
    """b folded into a (both summaries); a's cost stays: a subagent's is in its parent's account."""
    for k in ("tokens", "tools", "denials", "models"):
        for name, n in b[k].items():
            a[k][name] = a[k].get(name, 0) + n
    for k in ("compactions", "api_errors", "lines_added", "lines_removed", "api_ms", "tool_ms"):
        a[k] += b[k]
    a["cache_rebuilds"] += b["cache_rebuilds"]
    return a


def _cached_read(path, cache, fresh):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = [VERSION, st.st_size, int(st.st_mtime)]
    kept = cache.get(path)
    if isinstance(kept, dict) and kept.get("key") == key and isinstance(kept.get("summary"), dict):
        summary = kept["summary"]
    else:
        summary = read(path)
        if summary is None:
            return None
    fresh[path] = {"key": key, "summary": summary}
    return summary


def session_summary(session, cwd, cache_path=None, root=None):
    """The summary of one session run in cwd, its subagents folded in, or None when the
    transcript is missing. Reads are cached by (size, mtime) in cache_path (a JSON file)."""
    if not session or not cwd:
        return None
    d = os.path.join(root or ROOT, project_name(cwd))
    main = os.path.join(d, f"{session}.jsonl")
    cache = {}
    if cache_path:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
            if not isinstance(cache, dict):
                cache = {}
        except (OSError, ValueError):
            cache = {}
    fresh = {}
    out = _cached_read(main, cache, fresh)
    if out is not None:
        out = json.loads(json.dumps(out))  # a copy: merging must not touch the cached one
        subs = os.path.join(d, session, "subagents")
        try:
            names = sorted(n for n in os.listdir(subs) if n.endswith(".jsonl"))
        except OSError:
            names = []
        for n in names:
            sub = _cached_read(os.path.join(subs, n), cache, fresh)
            if sub is not None:
                _merge(out, json.loads(json.dumps(sub)))
    if cache_path and fresh != {k: cache[k] for k in fresh if k in cache}:
        try:
            # entries for other sessions stay; this one's replace theirs
            cache.update(fresh)
            tmp = f"{cache_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return out


def describe(s, top=3):
    """Short text lines for a summary: tokens, top tools, denials."""
    t = s["tokens"]
    lines = [f"tokens in {t['input']:,} out {t['output']:,} cache-read {t['cache_read']:,} "
             f"cache-write {t['cache_write']:,} thinking {t['thinking']:,}"]
    if s["tools"]:
        best = sorted(s["tools"].items(), key=lambda kv: (-kv[1], kv[0]))[:top]
        lines.append("tools " + ", ".join(f"{k} {v}" for k, v in best))
    if s["denials"]:
        lines.append("denials " + ", ".join(f"{k} {v}" for k, v in sorted(s["denials"].items())))
    extra = [f"{n} {label}" for n, label in ((s["compactions"], "compaction(s)"), (s["api_errors"], "API error(s)"),
                                             (len(s["cache_rebuilds"]), "cache rebuild(s)")) if n]
    if extra:
        lines.append(", ".join(extra))
    return lines


def _unix(ts):
    """A transcript timestamp ("2026-11-16T17:39:33.123Z") as a Unix time, or None."""
    import datetime
    if not isinstance(ts, str) or not ts:
        return None
    try:
        d = datetime.datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.timezone.utc)
    return d.timestamp()


def last_request(session, cwd, root=None):
    """(when, tokens) of the last request a session made itself, or None when its transcript is
    missing or holds none: `when` a Unix time (the prompt cache lives from then) and `tokens`
    what that request sent, cached or not (the context the session holds). Subagents' requests
    and zero-usage ones (the synthetic message a usage limit ends a turn with) don't count.
    Ported from SClaude's session_end.py (context, requested), MIT as above."""
    if not session or not cwd:
        return None
    path = os.path.join(root or ROOT, project_name(cwd), f"{session}.jsonl")
    got = None
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    with fh:
        for line in fh:
            if b'"type":"assistant"' not in line:
                continue
            entry = _load(line)
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            usage = (entry.get("message") or {}).get("usage") or {}
            try:
                held = sum(int(usage.get(k) or 0) for k in
                           ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (TypeError, ValueError, AttributeError):
                continue
            at = _unix(entry.get("timestamp"))
            if held and at is not None:
                got = (at, held)
    return got
