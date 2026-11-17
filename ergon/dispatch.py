"""Headless workers: prepare, run, verify, review, land."""
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
from . import gates as _gates
from . import gitops as _gitops
from . import history as _history
from . import limits as _limits
from . import report as _report
from . import tasks as _tasks


# ---------- dispatch ----------
# Each ready task gets a worktree at <worktrees>/<ID> on branch task/<ID>, cut from its epic
# branch (created as a ref, never checked out here) or from the trunk for a task with no epic.
# A headless `claude -p --agent task-<tier>` works it and commits there. The dispatcher alone
# moves status: doing when it launches, then done once the branch merges into its base, or
# blocked (worktree and branch kept for you) on anything else, a merge conflict included.
# A task whose touches: names a companion repo also gets its own worktree of that repo's
# checkout, on a task/<ID> branch there too, which lands on the companion's local trunk first.
# Nothing is ever pushed.

WORKER_PROMPT = """You are dispatched on {id}.

Working tree: {wt} (a git worktree of this repo), on branch {branch}, cut from {base}.
{retry}{nested}{companions}{rules}
Work you find but don't do: end your final message with one line per item, before the RESULT line,
`FOUND: <title> — <why>`. The dispatcher files each as a proposed task.

The task file, read-only to you (it lives in the main checkout):

{text}
"""

WORKER_POLICIES = """
Policies a human settled on past review findings. Follow them. To propose one, end a line
`POLICY: <title> — <why>` before the RESULT line (it becomes a note on the task); never write the
policies file yourself:

{policies}
"""

REVIEW_POLICIES = """
Policies a human settled on past findings. Do not raise a finding one of them settles:

{policies}
"""

COMPANION_OWN = """
The companion repo {name} is yours alone at {pub}: a git worktree of the shared checkout
{shared}, on branch {branch}, cut from its local {ctrunk}. Make and commit every change to it
there, never in {shared}. {ids}Push neither repo; the dispatcher merges {branch} into {name}'s
{ctrunk} and into {base}.
"""

COMPANION_NO_IDS = """Its commit messages never cite tracker IDs{policy}: your commit in {wt}, which
cites {id}, names the {name} commit. """

COMPANION_NONE = """
This task's touches: doesn't name {name}, so it has no checkout of it. You may read {shared},
but never change it: if the task needs a change there, stop blocked.
"""


def gitlinks():
    return [l.split("\t", 1)[1] for l in _gitops.git("ls-files", "-s", raw=True).splitlines()
            if l.startswith("160000")]


def nested_touch(t, links):
    for p in (x.strip("/") for x in t.get("touches") or []):
        if any(p == g or p.startswith(g + "/") or g.startswith(p + "/") for g in links):
            return p
    return None


def companion_touch(t, c):
    """Does t's touches: name companion c (so its worker needs a checkout of it)?"""
    return any(p == k or p.startswith(k + "/")
               for p in (x.strip("/") for x in t.get("touches") or []) for k in c["touches"])


def has_checkout(c):
    return (c["path"] / ".git").exists()


def add_note(t, note, who="dispatch"):
    body = t["_body"].rstrip()
    t["_body"] = (body if "## Notes" in body else body + "\n\n## Notes") + f"\n- {_tasks.today()} {who}: {note}\n"


def put_status(tid, status, note=None, who="dispatch", drop=()):
    t = _tasks.load(_config.ITEMS)[tid]
    t["status"] = status
    for k in drop:
        t.pop(k, None)
    if note:
        add_note(t, note, who)
    _tasks.dump(t, _tasks.TASK_KEYS)


# ---------- questions ----------
# A worker that needs a decision ends with `QUESTION: <text>` lines (each optionally followed by
# `OPTIONS: a | b | c`, recommended first) and `RESULT: asking`. Dispatch parks the task: status
# blocked, frontmatter `asking: <session>`, the questions as `question:` bullets in Notes, the
# worktree and branch kept. Only a human's `ergon answer` moves it on: it writes the answer to
# Notes, swaps `asking:` for `answered: <session>` and sets the task ready. The next dispatch
# clears `answered:` at launch and resumes that session with the answer (a fresh worker with the
# questions and answer in its prompt when resume_choice() says the cache is gone).

ASKING_MARK = "dispatch: asking:"
NO_SESSION = "-"


def parse_questions(text):
    """[(question, [options])] from a worker's QUESTION: / OPTIONS: lines. An OPTIONS line belongs
    to the QUESTION line before it."""
    out = []
    for l in text.splitlines():
        m = re.match(r"^\W*(QUESTION|OPTIONS):\s*(.*?)\s*$", l)
        if not m:
            continue
        val = m[2].strip(" *`")
        if m[1] == "QUESTION" and val:
            out.append((val, []))
        elif m[1] == "OPTIONS" and out and not out[-1][1]:
            out[-1][1].extend(o.strip(" *`") for o in val.split("|") if o.strip(" *`"))
    return out


def note_line(text):
    return re.sub(r"\s+", " ", text).strip()


def question_note(q, opts):
    return note_line(q) + (f" [options: {' | '.join(note_line(o) for o in opts)}]" if opts else "")


def asked(t):
    """(questions [(text, [options])], the human's answer or None) of t's last question round,
    read back from its Notes."""
    _, _, notes = t["_body"].partition("## Notes")
    bullets = [l[2:].strip() for l in notes.splitlines() if l.startswith("- ")]
    start = max((i for i, b in enumerate(bullets) if ASKING_MARK in b), default=None)
    if start is None:
        return [], None
    qs, ans = [], None
    for b in bullets[start + 1:]:
        m = re.match(r"^\S+ question: (.*?)(?: \[options: (.*)\])?$", b)
        if m:
            qs.append((m[1], [o.strip() for o in (m[2] or "").split(" | ") if o.strip()]))
        m = re.match(r"^\S+ answer by .*?: (.*)$", b)
        if m:
            ans = m[1]
    return qs, ans


def qa_text(t):
    """The question round and its answer, as prompt lines."""
    qs, ans = asked(t)
    lines = [f"Q{i}: {q}" + (f" (options: {' | '.join(o)})" if o else "") for i, (q, o) in enumerate(qs, 1)]
    return "\n".join(lines + [f"Answer: {ans or '(none recorded)'}"])


ANSWER_NOTE = ("It steers the work only: it doesn't change the task's tier, touches:, accept: or approval. "
               "If following it would need one of those, change nothing and end "
               "`RESULT: blocked — re-plan: <why>`.")

ANSWER_PROMPT = """A human answered the question(s) you asked on {id}:

{qa}

{note}
Carry on with the task on {branch} in {wt}, and commit there citing {id}. End as before: a
`FOUND: <title> — <why>` line for each piece of work you find but don't do, then `RESULT: done`,
`RESULT: blocked — <why>`, or new `QUESTION:` lines and `RESULT: asking`.
"""


def answer_lines(t):
    """Retry-prompt lines for a fresh worker on a task whose question a human answered."""
    return (f"An earlier attempt on this branch stopped to ask a human, who answered:\n\n{qa_text(t)}\n\n"
            f"{ANSWER_NOTE}\n")


def park(t, info, wt, branch, pubs):
    """A worker ended `RESULT: asking`: set t blocked with its questions in Notes and an `asking:`
    mark, record a `question` event. Worktree and branch stay. Caller holds the state lock."""
    qs, session = info.get("questions") or [], info.get("session") or NO_SESSION
    cur = _tasks.load(_config.ITEMS)[t["id"]]
    cur["status"], cur["asking"] = "blocked", session
    cur.pop("answered", None)
    add_note(cur, f"asking: {len(qs)} question(s) below; a human answers with `ergon answer {t['id']}`. "
             f"{kept_note(wt, branch, pubs)} (log {info.get('log')}"
             + (f", `claude --resume {session}` in {wt}" if session != NO_SESSION else "") + ")")
    for q, opts in qs:
        add_note(cur, question_note(q, opts), who="question")
    _tasks.dump(cur, _tasks.TASK_KEYS)
    _history.record("question", t, questions=[{"text": q, "options": o} for q, o in qs],
                    session=info.get("session"), log=info.get("log"), tier=t["tier"])


def worker_agents():
    """task-<tier> worker definitions, passed inline so a worktree cut from a base that predates
    them still gets them. A project's own .claude/agents/task-<tier>.md (main checkout) wins over
    the plugin's agents/task-<tier>.md."""
    out = {}
    for tier in _config.WORKER_TIERS:
        path = next(p for p in (_config.AGENTS / f"task-{tier}.md", _config.PLUGIN_AGENTS / f"task-{tier}.md") if p.exists())
        m = _tasks.parse(path)
        out[f"task-{tier}"] = {"description": m["description"], "model": m.get("model") or tier,
                               "prompt": m["_body"].strip()}
    return out


def worker_settings():
    cmd = f'python3 "{_config.PKG}" hook guard --worker --project "{_config.HOME}"'
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": _gates.GUARD_TOOLS, "hooks": [{"type": "command", "command": cmd}]}]}})


def worker_rules():
    return _config.WORKER_RULES.read_text().strip() if _config.WORKER_RULES and _config.WORKER_RULES.exists() else ""


def policies_text():
    """The policies file's text, or ''. Written by a human only: the guard refuses agent writes."""
    return _config.POLICIES.read_text().strip() if _config.POLICIES.exists() else ""


def base_branch(t, epics):
    e = epics.get(t.get("epic"))
    if not e:
        return _config.MAIN
    b = _gitops.epic_branch(e)
    if not _gitops.branch_exists(b):
        _gitops.git("branch", b, _config.MAIN)
        print(f"started {e['id']} on branch {b} (cut from {_config.MAIN}, not checked out)")
    return b


def prepare(t, epics):
    """Worktree + branch for t, reusing both if an earlier run left them, and the same in each
    companion t touches. Returns (base, wt, branch, pubs); pubs is [(companion, its worktree)]."""
    base, branch, wt = base_branch(t, epics), _config.TASK_BRANCH + t["id"], _config.WORKTREES / t["id"]
    if _gitops.worktrees().get(branch):
        wt = Path(_gitops.worktrees()[branch])
    elif _gitops.branch_exists(branch):
        _gitops.git("worktree", "add", str(wt), branch)
    else:
        _gitops.git("worktree", "add", "-b", branch, str(wt), base)
    pubs = []
    for c in _config.COMPANIONS:
        if not companion_touch(t, c):
            continue
        # Its own worktree of the shared checkout: never the checkout itself, never another
        # task's. Same object store and refs, so the dispatcher merges it without a fetch.
        pub = wt / c["rel"]
        held = _gitops.worktrees(c["path"]).get(branch)
        if held and Path(held).resolve() != pub.resolve():
            raise _gitops.GitError(f"{c['name']} branch {branch} is checked out at {held}; free it (`git -C "
                           f"{c['path']} worktree remove {held}`, or switch that checkout off it) and dispatch again")
        if not held:
            pub.parent.mkdir(parents=True, exist_ok=True)
            if _gitops.branch_exists(branch, c["path"]):
                _gitops.git("worktree", "add", str(pub), branch, cwd=c["path"])
            else:
                _gitops.git("worktree", "add", "-b", branch, str(pub), c["trunk"], cwd=c["path"])
        pubs.append((c, pub))
    return base, wt, branch, pubs


KILLED = "ergon: killed"
PROCS, PROCS_LOCK, KILL_ALL = set(), threading.Lock(), threading.Event()


def kill_workers():
    """Second Ctrl-C: kill every running worker (and its process group) and refuse new ones."""
    KILL_ALL.set()
    with PROCS_LOCK:
        for p in list(PROCS):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass


def run_proc(cmd, cwd, timeout):
    """Run a worker in its own session (a terminal Ctrl-C doesn't reach it) so the dispatcher
    decides what an interrupt means. Returns (stdout, stderr); stderr starts with KILLED if
    kill_workers() ended it."""
    with PROCS_LOCK:
        if KILL_ALL.is_set():
            return "", KILLED
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
        PROCS.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = p.communicate()
        err = f"timed out after {timeout // 60} min\n{err or ''}"
    finally:
        with PROCS_LOCK:
            PROCS.discard(p)
    if KILL_ALL.is_set() and p.returncode == -signal.SIGKILL and not err.startswith("timed out"):
        return out or "", KILLED
    return out or "", err or ""


def run_worker(t, base, wt, branch, pubs, args, agents, settings, rnd=None, prompt=None, resume=None, veto=None, lines=None):
    """Run one headless worker and record its worker_end. Returns (verdict, reason, run info);
    never raises. A fix round (rnd, from 2) either resumes the worker's session with `prompt`, or
    starts a fresh worker whose prompt quotes the reviewer's `veto`."""
    start = time.monotonic()
    if prompt is None:
        prompt = worker_prompt(t, base, wt, branch, pubs, veto, lines)
    verdict, why, info = _run_worker(t, wt, args, agents, settings, prompt, resume, rnd)
    info["seconds"] = round(time.monotonic() - start, 1)
    _history.record("worker_end", t, tier=t["tier"], verdict=verdict, reason=why,
                    session=info.get("session"), cost=info.get("cost", 0), seconds=info["seconds"],
                    log=info.get("log"), **_round(rnd))
    return verdict, why, info


def _round(rnd):
    """The history's `round` field: absent on round 1."""
    return {"round": rnd} if rnd and rnd > 1 else {}


def worker_prompt(t, base, wt, branch, pubs, veto=None, lines=None):
    retry = ""
    try:
        if _gitops.git("rev-list", "--count", f"{base}..{branch}") != "0":
            retry = (f"An earlier attempt left commits on this branch (`git log {base}..HEAD`) and "
                     "notes in the task file. Pick up from there.\n")
        for c, pub in pubs:
            if _gitops.git("rev-list", "--count", f"{c['trunk']}..{branch}", cwd=c["path"]) != "0":
                retry += (f"An earlier attempt left {c['name']} commits too (`git -C {pub} log "
                          f"{c['trunk']}..HEAD`).\n")
    except _gitops.GitError:
        pass
    retry += lines or (veto_lines(veto) if veto else review_retry(t))
    links = gitlinks()
    nested = (f"Nested repos ({', '.join(links)}) are empty in this worktree: if the task needs one, "
              "stop blocked.\n") if links else ""
    own = {c["name"]: pub for c, pub in pubs}
    companions = ""
    for c in _config.COMPANIONS:
        if c["name"] in own:
            ids = COMPANION_NO_IDS.format(policy=f" ({c['policy']})" if c["policy"] else "", wt=wt,
                                          id=t["id"], name=c["name"]) if c["forbid_ids"] else ""
            companions += COMPANION_OWN.format(name=c["name"], pub=own[c["name"]], shared=c["path"],
                                               branch=branch, ctrunk=c["trunk"], ids=ids, base=base)
        else:
            companions += COMPANION_NONE.format(name=c["name"], shared=c["path"])
    rules = worker_rules()
    pol = policies_text()
    return WORKER_PROMPT.format(id=t["id"], wt=wt, branch=branch, base=base, retry=retry,
                                nested=nested, companions=companions,
                                rules=(f"\nThis project's worker rules:\n\n{rules}\n" if rules else "")
                                + (WORKER_POLICIES.format(policies=pol) if pol else ""),
                                text=t["_path"].read_text())


def _run_worker(t, wt, args, agents, settings, prompt, resume=None, rnd=None):
    # A resumed session gets the same agent, permission mode and guard hook as a fresh one.
    cmd = ["claude", "-p", "--agents", json.dumps(agents), "--agent", f"task-{t['tier']}",
           "--permission-mode", args.permission_mode, "--settings", settings,
           "--output-format", "json"]
    if resume:
        cmd += ["--resume", resume]
    if args.budget:
        cmd += ["--max-budget-usd", str(args.budget)]
    # A fix round's log is <ID>-<time>-fix<N>.json: `stats` counts its cost, not another run.
    suffix = f"-fix{rnd}" if rnd and rnd > 1 else ("-" + t["tier"] if t.get("escalated") else "")
    stem = f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}{suffix}"
    log = _config.RUNS / f"{stem}.json"
    out, err = run_proc(cmd + [prompt], wt, args.timeout * 60)
    log.write_text(out)
    if err.strip():
        (_config.RUNS / f"{stem}.err").write_text(err)
    info = {"log": log.relative_to(_config.HOME)}
    if err.startswith(KILLED):
        return "blocked", "killed by a second Ctrl-C", info
    try:
        d = json.loads(out)
    except ValueError:
        return "blocked", "worker output wasn't JSON" + (f" ({err.strip().splitlines()[0]})" if err.strip() else ""), info
    info.update(session=d.get("session_id"), cost=d.get("total_cost_usd") or 0)
    text = d.get("result") or ""
    if d.get("is_error"):
        return "blocked", f"worker errored ({d.get('subtype') or d.get('terminal_reason')}): {text[:200]}", info
    info["found"] = [(m[0].strip(" *`"), m[1].strip(" *`")) for m in
                     re.findall(r"^\W*FOUND:\s*(.*?)(?:\s+[—–]\s+(.*))?$", text, re.M) if m[0].strip(" *`")]
    info["policy"] = [(m[0].strip(" *`"), m[1].strip(" *`")) for m in
                      re.findall(r"^\W*POLICY:\s*(.*?)(?:\s+[—–]\s+(.*))?$", text, re.M) if m[0].strip(" *`")]
    found = re.findall(r"^\W*RESULT:\s*(done|blocked|asking)\b[\s—:–-]*(.*)$", text, re.M)
    if not found:
        return "blocked", "worker ended without a RESULT line", info
    verdict, why = found[-1]
    if verdict == "asking":
        info["questions"] = parse_questions(text)
        if not info["questions"]:
            return "blocked", "worker ended `RESULT: asking` without a `QUESTION:` line", info
        return "asking", f"{len(info['questions'])} question(s)", info
    info["asked"] = verdict == "blocked"  # the worker itself says what a human must do
    return verdict, why.strip(" *`") or "(no reason given)", info


def file_found(t, found, epics):
    """Create each FOUND line a worker emitted as a `proposed` task: same tier, epic and a
    source: of the task that found it. Never ready. Caller holds the state lock."""
    made = []
    for title, why in found:
        tid = _tasks.next_id(_config.ITEMS, "T")
        e = epics.get(t.get("epic"))
        body = _tasks.TASK_BODY.replace("## What\n", f"## What\n{why or title}\n", 1)
        meta = {"id": tid, "title": title, "tier": t["tier"], "status": "proposed",
                "epic": t.get("epic") or "", "depends": [], "touches": [],
                "source": t["id"], "created": _tasks.today(), "approved": "", "tier_why": "",
                "_path": _tasks.task_home(t, epics) / f"{tid}-{_tasks.slug(title)}.md", "_body": body}
        _tasks.dump(meta, _tasks.TASK_KEYS)
        _history.record("created", meta, tier=meta["tier"], title=title, source=t["id"], by="found")
        made.append((tid, title))
    return made


def file_human(t, why, epics):
    """A worker reported `RESULT: blocked`: file a proposed human-tier task for what it asked
    for and make the blocked task depend on it. Caller holds the state lock."""
    tid = _tasks.next_id(_config.ITEMS, "T")
    title = why if len(why) <= 100 else why[:97].rstrip() + "..."
    body = _tasks.TASK_BODY.replace("## What\n", f"## What\n{why}\n", 1)
    meta = {"id": tid, "title": title, "tier": "human", "status": "proposed",
            "epic": t.get("epic") or "", "depends": [], "touches": [],
            "source": t["id"], "created": _tasks.today(), "approved": "", "tier_why": "",
            "_path": _tasks.task_home(t, epics) / f"{tid}-{_tasks.slug(title)}.md", "_body": body}
    _tasks.dump(meta, _tasks.TASK_KEYS)
    _history.record("created", meta, tier="human", title=title, source=t["id"], by="blocked")
    cur = _tasks.load(_config.ITEMS)[t["id"]]
    cur["depends"] = list(cur.get("depends") or []) + [tid]
    _tasks.dump(cur, _tasks.TASK_KEYS)
    return tid, title


def drop_worktree(wt, branch, repo=None):
    """Remove a landed task's worktree, then its branch. Returns what's left for you, or ''."""
    try:
        _gitops.git("worktree", "remove", str(wt), cwd=repo)
        _gitops.git("branch", "-D", branch, cwd=repo)  # merged: merge_into landed it
        return ""
    except _gitops.GitError as err:
        return f"; left {wt} on {branch} for you to remove ({str(err)[:200]})"


VERIFY_TIMEOUT = 15 * 60  # seconds


def run_verify(t, wt, rnd=None):
    """Run the verify command (task `verify:` over config `verify`) in the task worktree.
    Returns None if it passes or none is set, else a reason; full output goes to <state>/runs/."""
    cmd = t.get("verify") or _config.CFG.get("verify")
    if not cmd:
        return None
    start = time.monotonic()
    try:
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        text_of = lambda b: (b.decode(errors="replace") if isinstance(b, bytes) else b) or ""
        out, code = f"{text_of(e.stdout)}{text_of(e.stderr)}\ntimed out after {VERIFY_TIMEOUT // 60} min\n", None
    seconds = round(time.monotonic() - start, 1)
    if code == 0:
        _history.record("verify", t, ok=True, cmd=cmd, seconds=seconds, log=None, **_round(rnd))
        return None
    _config.RUNS.mkdir(parents=True, exist_ok=True)
    log = _config.RUNS / f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}-verify.log"
    log.write_text(out)
    _history.record("verify", t, ok=False, cmd=cmd, seconds=seconds, log=log, exit=code, **_round(rnd))
    tail = " | ".join(l.strip() for l in out.strip().splitlines()[-5:])[-400:]
    how = "timed out" if code is None else f"failed (exit {code})"
    return f"verify `{cmd}` {how}: {tail} (full output {log.relative_to(_config.HOME)})"


REVIEW_PROMPT = """You are reviewing {id} before it lands.

Working tree: {wt}, on branch {branch}, cut from {base}. A {tier}-tier worker says the task is done.
{companions}{rules}
The task file:

{text}

The diff, `git diff {base}...{branch}`{size}:

{diff}
"""
REVIEW_DIFF_MAX = 100_000  # characters inlined per diff; past that the reviewer runs git diff itself
REVIEW_LINE = re.compile(r"^\W*REVIEW:\s*(pass|fail)\b[\s—:–-]*(.*)$")


def reviewer_tier(tier):
    """Who reviews a tier's work: one tier above, capped at opus (opus reviews opus)."""
    return _config.WORKER_TIERS[min(_config.WORKER_TIERS.index(tier) + 1, len(_config.WORKER_TIERS) - 1)]


def review_agent():
    """The task-review definition (a project's .claude/agents/task-review.md wins over the
    plugin's), minus a model: run_review sets that from reviewer_tier()."""
    path = next(p for p in (_config.AGENTS / "task-review.md", _config.PLUGIN_AGENTS / "task-review.md") if p.exists())
    m = _tasks.parse(path)
    agent = {"description": m["description"], "prompt": m["_body"].strip()}
    tools = m.get("tools")
    if tools:
        agent["tools"] = tools if isinstance(tools, list) else [x.strip() for x in tools.split(",") if x.strip()]
    return agent


def review_diff(base, branch, repo=None):
    """(diff text for the prompt, size note). Past REVIEW_DIFF_MAX the prompt gets the stat only."""
    diff = _gitops.git("diff", f"{base}...{branch}", raw=True, cwd=repo)
    if len(diff) <= REVIEW_DIFF_MAX:
        return diff or "(empty)", ""
    stat = _gitops.git("diff", "--stat", f"{base}...{branch}", raw=True, cwd=repo)
    return stat, f" (too long to inline at {len(diff)} characters: its --stat follows; run it yourself)"


def tree_state(trees, branch, repos):
    """What a read-only reviewer must leave as it found it: each tree's HEAD and tracked changes,
    and where `branch` points in each repo."""
    return ([(_gitops.git("rev-parse", "HEAD", cwd=x), _gitops.dirty_paths(x)) for x in trees]
            + [_gitops.git("rev-parse", branch, cwd=r) for r in repos])


def run_review(t, tier, base, wt, branch, pubs, args, agent, settings, rnd=None):
    """Run the reviewer on a finished task branch and record its review event. Returns (failure
    reason or None, cost, reviewer tier, fixable). Only a last line of `REVIEW: pass` passes: no
    such line, a fail, output that isn't JSON, an error, a kill, a timeout, or a reviewer that
    changed the branch or worktree all block the landing. Only a `REVIEW: fail` is `fixable`:
    the rest leave the worker no findings to fix."""
    start = time.monotonic()
    log = _config.RUNS / (f"{t['id']}-{datetime.datetime.now():%Y%m%d-%H%M%S}-review"
                          + (str(rnd) if rnd and rnd > 1 else "") + ".json")
    failed, cost, who, fixable = _run_review(t, tier, base, wt, branch, pubs, args, agent, settings, log)
    _history.record("review", t, verdict="fail" if failed else "pass", reviewer=who, tier=tier,
                    reason=failed, cost=cost, seconds=round(time.monotonic() - start, 1), log=log, **_round(rnd))
    return failed, cost, who, fixable


def _run_review(t, tier, base, wt, branch, pubs, args, agent, settings, log):
    who = reviewer_tier(tier)
    trees, repos = [wt] + [pub for _, pub in pubs], [None] + [c["path"] for c, _ in pubs]
    before = tree_state(trees, branch, repos)
    diff, size = review_diff(base, branch)
    companions = ""
    for c, pub in pubs:
        cdiff, csize = review_diff(c["trunk"], branch, c["path"])
        companions += (f"\nCompanion repo {c['name']}: worktree {pub}, branch {branch} cut from its "
                       f"{c['trunk']}. Its diff, `git -C {pub} diff {c['trunk']}...{branch}`{csize}:\n\n{cdiff}\n")
    rules = worker_rules()
    pol = policies_text()
    prompt = REVIEW_PROMPT.format(id=t["id"], wt=wt, branch=branch, base=base, tier=tier, companions=companions,
                                  rules=(f"\nThe worker followed these project rules:\n\n{rules}\n" if rules else "")
                                  + (REVIEW_POLICIES.format(policies=pol) if pol else ""),
                                  text=t["_path"].read_text(), diff=diff, size=size)
    cmd = ["claude", "-p", "--agents", json.dumps({"task-review": dict(agent, model=who)}),
           "--agent", "task-review", "--permission-mode", args.permission_mode, "--settings", settings,
           "--output-format", "json"]
    if args.budget:
        cmd += ["--max-budget-usd", str(args.budget)]
    out, err = run_proc(cmd + [prompt], wt, args.timeout * 60)
    log.write_text(out)
    if err.strip():
        log.with_suffix(".err").write_text(err)
    fail = lambda why, cost=0, fixable=False: (f"review ({who}) failed: {why} (review log {log.relative_to(_config.HOME)})",
                                               cost, who, fixable)
    if err.startswith(KILLED):
        return fail("killed by a second Ctrl-C")
    try:
        d = json.loads(out)
        if not isinstance(d, dict):
            raise ValueError
    except ValueError:
        return fail("reviewer output wasn't JSON" + (f" ({err.strip().splitlines()[0]})" if err.strip() else ""))
    cost = d.get("total_cost_usd") or 0
    try:
        changed = tree_state(trees, branch, repos) != before
    except _gitops.GitError:
        changed = True
    if changed:
        return fail("the reviewer changed the branch or worktree; it must stay read-only", cost)
    text = d.get("result") or ""
    if d.get("is_error"):
        return fail(f"reviewer errored ({d.get('subtype') or d.get('terminal_reason')}): {text[:200]}", cost)
    last = next((l for l in reversed(text.strip().splitlines()) if l.strip()), "")
    m = REVIEW_LINE.match(last.strip(" *`"))
    if not m:
        return fail("reviewer didn't end with a `REVIEW: pass` or `REVIEW: fail` line", cost)
    if m[1] == "pass":
        return None, cost, who, False
    return fail(m[2].strip(" *`") or "(no reason given)", cost, fixable=True)


def review_retry(t):
    """Retry-prompt lines for a task whose last attempt the reviewer vetoed or a human rejected
    in review, or ''."""
    note = _report.last_note(t)
    human = re.search(r"\breview: rejected by (.*)", note)
    if human:
        return (f"A human reviewed the last attempt's branch and sent it back, not merged: rejected by {human[1]}\n"
                "Fix what they raised on this branch; it waits for their acceptance again before it lands.\n")
    m = re.search(r"\bdispatch: (review \(\w+\) failed: .*)", note)
    return veto_lines(m[1]) if m else ""


def veto_lines(failed):
    """Prompt lines quoting a reviewer's veto (run_review's failure reason) and its log. The
    first review log named is the latest: a block after fix rounds names the earlier ones after."""
    log = re.search(r"\(review log (\S+?\.json)\)", failed)
    return (f"The reviewer vetoed the last attempt: {failed}\n"
            + (f"Its full review is in {_config.HOME / log[1]}.\n" if log else "")
            + "Fix what it raised on this branch; it reviews again before this lands.\n")


def gate(t, tier, base, wt, branch, pubs, args, reviewer, settings, rnd=None):
    """After a worker says done, before the merge lock: its done claim holds, verify passes,
    then the reviewer passes if `review` names the task's tier or `tier`, the one that finished
    (after --escalate the lower tier's commits are on the branch too). The reviewer is one tier
    above `tier`. Touches only the task's own worktrees and branch, so a long verify or review
    doesn't hold the state lock. Returns (failure reason or None, review cost, fixable): a
    reviewer's `REVIEW: fail` is fixable (True), and so is a red verify ("verify"), each by a fix
    round (fix_loop). The caller adds kept_note."""
    for tree in [wt] + [pub for _, pub in pubs]:
        if _gitops.dirty_paths(tree):
            return f"worker said done but left uncommitted changes in {tree}", 0, False
    msgs = _gitops.git("log", "--format=%B%x1e", f"{base}..{branch}", raw=True)
    if not re.search(rf"\b{t['id']}\b", msgs):
        return f"worker said done but no commit on {branch} cites {t['id']}", 0, False
    failed = run_verify(t, wt, rnd)
    if failed:
        return failed, 0, "verify"
    if t["tier"] not in _config.REVIEW and tier not in _config.REVIEW:
        return None, 0, False
    failed, cost, who, fixable = run_review(t, tier, base, wt, branch, pubs, args, reviewer, settings, rnd)
    print(f"  review   {t['id']}  {who}: " + ("pass" if not failed else "fail")
          + (f" (round {rnd})" if rnd and rnd > 1 else ""), flush=True)
    return failed, cost, fixable


FIX_PROMPT = """The reviewer vetoed your work on {id} (fix round {n} of {rounds}): {veto}
{log}
Fix what it raised on {branch} in {wt}, and commit there citing {id}. The done checks, verify and
the review run again when you finish. End as before: a `FOUND: <title> — <why>` line for each
piece of work you find but don't do, then `RESULT: done`, `RESULT: blocked — <why>`, or
`QUESTION:` lines and `RESULT: asking`.
"""


VERIFY_FIX_PROMPT = """Verify failed on your work on {id} (verify fix round {n} of {rounds}): {failed}

The last lines of its output:

{tail}

The full output is in {log}.
Fix it on {branch} in {wt}, and commit there citing {id}. The done checks and verify run again
when you finish. If the failure isn't caused by this branch (a flaky test, a broken trunk, a
missing tool), change nothing and end `RESULT: blocked — outside: <why>`: verify runs once more
as it is, and this doesn't use up a round. Otherwise end as before: a `FOUND: <title> — <why>`
line for each piece of work you find but don't do, then `RESULT: done` or
`RESULT: blocked — <why>`.
"""


def verify_detail(failed):
    """(last ~200 lines of the verify output, its full log path) from run_verify's reason."""
    m = re.search(r"\(full output (\S+?)\)\s*$", failed)
    log = _config.HOME / m[1] if m else None
    try:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-200:])
    except (OSError, AttributeError):
        tail = "(output unavailable)"
    return tail, log or "(unknown)"


def verify_lines(failed):
    """Prompt lines for a fresh worker told that verify failed."""
    tail, log = verify_detail(failed)
    return (f"Verify failed on the last attempt: {failed}\nThe last lines of its output:\n\n{tail}\n\n"
            f"The full output is in {log}.\nFix it on this branch; verify runs again before this lands. "
            "If the failure isn't caused by this branch (a flaky test, a broken trunk, a missing tool), "
            "change nothing and end `RESULT: blocked — outside: <why>`.\n")


def resume_choice(session, wt):
    """Resume the worker's own session for a fix round, or start a fresh one? Resumed only while
    its prompt cache is likely alive (its last request younger than review_resume_secs) and its
    context is light (under review_resume_tokens): resumed after that, the whole context is sent
    again uncached, and a fresh worker costs less. Anything unreadable goes fresh. Returns
    (True, None) or (False, why). From SClaude's review_keep (lib/review/review_round.sh)."""
    from . import transcripts
    if not session:
        return False, "the worker reported no session"
    for cwd in dict.fromkeys([str(wt), os.path.realpath(wt)]):
        got = transcripts.last_request(session, cwd)
        if got:
            break
    else:
        return False, "its transcript can't be read"
    at, held = got
    idle = max(0, time.time() - at)
    if idle >= _config.RESUME_SECS:
        return False, (f"its last request was {idle / 60:.0f} min ago, so its prompt cache has likely "
                       f"expired (review_resume_secs = {_config.RESUME_SECS})")
    if held >= _config.RESUME_TOKENS:
        return False, f"its context holds {held:,} tokens (review_resume_tokens = {_config.RESUME_TOKENS:,})"
    return True, None


def fix_round(t, tier, base, wt, branch, pubs, args, agents, settings, session, veto, rnd, verify=None, n=None):
    """Hand a reviewer's veto back to a worker on the same branch: its own session resumed if
    resume_choice() allows, else a fresh worker with the retry prompt. Returns run_worker's
    (verdict, reason, info), with info["resumed"] and, for a fresh one, info["fresh_because"].
    `verify` = (n, rounds) makes it a round for a red verify (`veto` is run_verify's reason).
    `rnd` numbers every worker run (logs, history); a review fix round's own count is `n`."""
    n, rounds = verify or (n, _config.REVIEW_ROUNDS)
    what = "verify fix" if verify else "fix"
    lines = verify_lines(veto) if verify else None
    resume, fresh_because = resume_choice(session, wt)
    tt = dict(t, tier=tier)
    if resume:
        _history.record("resumed", t, session=session, round=rnd, tier=tier)
        print(f"  {what:<8} {t['id']}  round {n}/{rounds}: resumed {session}", flush=True)
        if verify:
            tail, vlog = verify_detail(veto)
            prompt = VERIFY_FIX_PROMPT.format(id=t["id"], n=n, rounds=rounds, failed=veto, tail=tail,
                                              log=vlog, branch=branch, wt=wt)
        else:
            log = re.search(r"\(review log (\S+?\.json)\)", veto)
            prompt = FIX_PROMPT.format(id=t["id"], n=n, rounds=rounds, veto=veto, branch=branch, wt=wt,
                                       log=f"Its full review is in {_config.HOME / log[1]}.\n" if log else "")
        verdict, why, info = run_worker(tt, base, wt, branch, pubs, args, agents, settings, rnd=rnd,
                                        prompt=prompt, resume=session)
    else:
        _history.record("dispatched", t, tier=tier, model=agents[f"task-{tier}"]["model"], base=base,
                        branch=branch, worktree=wt, companions=[c["name"] for c, _ in pubs],
                        round=rnd, fresh_because=fresh_because)
        print(f"  {what:<8} {t['id']}  round {n}/{rounds}: fresh session ({fresh_because})", flush=True)
        verdict, why, info = run_worker(tt, base, wt, branch, pubs, args, agents, settings, rnd=rnd,
                                        veto=veto, lines=lines)
    info["resumed"], info["fresh_because"] = resume, fresh_because
    return verdict, why, info


def rounds_note(last, vetoes, stop=""):
    """A block reason after fix rounds: the last failure first (review_retry quotes it and its
    log to the next worker), why no fix round followed it, then each earlier round's veto."""
    if not vetoes:
        return last + stop
    return (f"{last} (round {len(vetoes) + 1}, after {len(vetoes)} fix round(s)){stop}. Earlier rounds: "
            + "; ".join(f"round {i}: {v}" for i, v in enumerate(vetoes, 1)))


def kept_note(wt, branch, pubs):
    return f"Branch {branch} and worktree {wt} kept" + "".join(f" ({c['name']} worktree {pub})" for c, pub in pubs)


def land(t, base, wt, branch, pubs=(), accepted=None):
    """After gate() passes: merge (companions first), clean up. Returns (verdict, reason).
    Nothing merges unless every merge would go through cleanly. Caller holds the state lock.
    `accepted` ("<name> <date>", from `accept`) forces a merge commit into base that records
    who accepted it: task files are local, so history is the only lasting record."""
    kept = kept_note(wt, branch, pubs)
    for c, pub in pubs:
        if c["forbid_ids"]:
            leak = _config.TRACKER_ID.search(_gitops.git("log", "--format=%B", f"{c['trunk']}..{branch}", raw=True, cwd=c["path"]))
            if leak:
                return "blocked", (f"a {c['name']} commit on {branch} cites {leak.group()}; tracker IDs stay out "
                                   f"of {c['name']}, so reword it in {pub}. {kept}")
        try:
            _gitops.merge_check(c["trunk"], branch, c["path"])
        except _gitops.GitError as err:
            return "blocked", f"{c['name']}: {err}. {kept}: merge by hand"
    if pubs:
        try:
            _gitops.merge_check(base, branch)
        except _gitops.GitError as err:
            return "blocked", f"{err}. {kept}: merge by hand"
    landed, tips = [], []
    for c, pub in pubs:
        # A fast-forward writes no message at all; a merge commit cites no tracker ID if forbidden.
        msg = f"Merge {_config.TRACKER_ID.sub('', t['title']).strip()}" if c["forbid_ids"] else f"Merge {t['id']}: {t['title']}"
        try:
            _gitops.merge_into(c["trunk"], branch, msg, repo=c["path"])
        except _gitops.GitError as err:
            return "blocked", (f"{c['name']}: {err}. {kept}: merge by hand"
                               + (f". Already landed: {', '.join(landed)}" if landed else ""))
        landed.append(f"{c['name']} {c['trunk']}")
        tips.append({"name": c["name"], "trunk": c["trunk"], "commit": _gitops.tip(c["trunk"], c["path"])})
    try:
        _gitops.merge_into(base, branch, f"Merge {t['id']}: {t['title']}"
                   + (f"\n\nAccepted by {accepted}" if accepted else ""), no_ff=bool(accepted))
    except _gitops.GitError as err:
        return "blocked", (f"{err}. {kept}: merge by hand"
                           + (f". Already landed: {', '.join(landed)}" if landed else ""))
    _history.record("landed", t, base=base, branch=branch, commit=_gitops.tip(base),
                    accepted_by=accepted, companions=tips)
    # A companion worktree sits inside the task's own, and removing that would take it along
    # and leave the companion a stale entry: remove those first.
    left = ""
    for c, pub in pubs:
        left += drop_worktree(pub, branch, c["path"])
    left += drop_worktree(wt, branch) if not left else f"; left {wt} too"
    return "done", f"merged into {base}" + "".join(f" and {c['name']} {c['trunk']}" for c, _ in pubs) + left


def print_limit(results, window):
    """Each task's cost as a share of the 5-hour limit, at the pooled rate (limits.rate()), after
    this run's own reading of the window. Nothing before any measurement (an API key, probes off)."""
    per, runs = _limits.rate()
    if window:
        moved = round(100 * (window["after"] - window["before"]))
        print(f"5-hour limit: {100 * window['before']:.0f}% → {100 * window['after']:.0f}% over this run"
              + (f" (+{moved} points, with anything else on your plan meanwhile)" if window["same"]
                 else " (the window reset during the run, so this run gives no rate)"))
    if per is None:
        return
    print(f"each task's share at {per:.1f}% per $ (measured over {runs} run{'s' if runs != 1 else ''}):")
    for t, verdict, _, info in sorted(results, key=lambda r: -r[3].get("cost", 0)):
        cost = info.get("cost", 0)
        print(f"  {t['id']}  {verdict:<8}{'$%.2f' % cost:>8}  ≈ {_limits.pct(cost, per):>6} of the 5-hour limit  {t['title']}")


def dispatch(args):
    if args.watch and not args.budget_total:
        raise SystemExit("--watch needs --budget-total (a cap on spend, so an unattended watch can't run away)")
    if args.watch and args.dry_run:
        raise SystemExit("--watch and --dry-run don't combine")
    if args.max is None:
        args.max = float("inf") if args.watch else 6
    if _config.REPO != _config.HOME and not args.dry_run:  # a dry run only reads, so any checkout will do
        raise SystemExit(f"run dispatch from the main checkout ({_config.HOME})")
    if not shutil.which("claude") and not args.dry_run:
        raise SystemExit("no `claude` on PATH")
    if not args.dry_run:
        _config.RUNS.mkdir(exist_ok=True)
        _config.WORKTREES.mkdir(parents=True, exist_ok=True)
        _gitops.git("worktree", "prune")
        for c in _config.COMPANIONS:  # forget companion worktrees whose directory is gone (a removed task worktree)
            if has_checkout(c):
                _gitops.git("worktree", "prune", cwd=c["path"])
    links, epics = gitlinks(), _tasks.load(_config.EPICS)
    ids = set(args.ids.split(",")) if args.ids else None
    agents, settings = worker_agents(), worker_settings()
    reviewer = review_agent() if _config.REVIEW else None
    tried, skipped, results = set(), {}, []
    cap = args.budget_total * args.budget_pct / 100 if args.budget_total else None
    capped = stopped = False
    probing = not args.dry_run and _limits.enabled()  # the 5-hour window, read before and after
    window = None

    def spent():
        return sum(r[3].get("cost", 0) for r in results)
    every_touch = {k for c in _config.COMPANIONS for k in c["touches"]}

    def eligible(tasks):
        epics.update(_tasks.load(_config.EPICS))  # another run or session may have closed one
        for t in sorted(tasks.values(), key=lambda t: (_tasks.TIERS.index(t["tier"]), t["id"])):
            if (t["id"] in tried or t["status"] != "ready" or not _tasks.deps_done(t, tasks)
                    or (ids and t["id"] not in ids) or (args.tier and t["tier"] != args.tier)
                    or (args.epic and t.get("epic") != args.epic)):
                continue
            if t["tier"] == "human":
                continue
            nested = nested_touch(t, links)
            if nested:
                skipped[t["id"]] = f"touches nested repo {nested}: work it interactively"
                continue
            missing = [c for c in _config.COMPANIONS if companion_touch(t, c) and not has_checkout(c)]
            if missing:
                skipped[t["id"]] = (f"touches {missing[0]['name']} but there's no checkout at "
                                    f"{missing[0]['path']}: clone the repo there")
                continue
            if t.get("epic") and epics.get(t["epic"], {}).get("status") != "open":
                skipped[t["id"]] = f"epic {t['epic']} isn't open"
                continue
            yield t

    live, live_lock = {}, threading.Lock()  # running task id -> what its calls have cost so far

    def round_stop(info, round_cost, rnd):
        """Why no fix round may start now, or ''. Not after a Ctrl-C, nor when what's spent (the
        finished tasks, and every running task's calls so far) plus about one more round (the
        last one's cost; each call is capped at --budget) would cross the --budget-total cap."""
        if stops:
            return "; no fix round: dispatch was interrupted"
        if cap is None:
            return ""
        with live_lock:
            now = spent() + sum(live.values())
        est = min(round_cost, 2 * args.budget) if args.budget else round_cost
        if now + est > cap:
            return (f"; no fix round {rnd - 1}: ${now:.2f} spent and a round costs about ${est:.2f}, "
                    f"past the ${cap:.2f} cap")
        return ""

    def fix_loop(t, tier, base, wt, branch, pubs, info):
        """gate() the finished branch; on a red verify or a reviewer's `REVIEW: fail`, hand it back
        to a worker (fix_round) and gate again: up to verify_rounds and review_rounds times. A
        worker that ends `blocked — outside: <why>` on a verify round costs no round: verify reruns
        once as it is, and a second red blocks the task and files a human task with that reason.
        Returns (verdict, reason). Folds every call's cost and FOUND lines, and the last worker's
        log and session, into info; info["rounds"] gets the earlier vetoes when a fix worker blocks."""
        kept, vetoes, rnd = kept_note(wt, branch, pubs), [], 1
        vfixes, outside = 0, None
        repos = [None] + [c["path"] for c, _ in pubs]
        round_cost = info.get("cost", 0)
        info.setdefault("review_cost", 0)

        def fix(failed, verify=None, n=None):
            """One fix worker on the branch. Returns (verdict, reason); folds its cost, FOUND lines,
            log and session into info. A review round whose worker commits nothing blocks."""
            nonlocal rnd, round_cost
            rnd += 1
            try:
                tips = [_gitops.git("rev-parse", branch, cwd=r) for r in repos]
            except _gitops.GitError:
                tips = None
            v, w, finfo = fix_round(t, tier, base, wt, branch, pubs, args, agents, settings,
                                    info.get("session"), failed, rnd, verify, n)
            round_cost = finfo.get("cost", 0)
            info["cost"] = info.get("cost", 0) + round_cost
            info["found"] = (info.get("found") or []) + (finfo.get("found") or [])
            info["policy"] = (info.get("policy") or []) + (finfo.get("policy") or [])
            info.update(log=finfo["log"], session=finfo.get("session") or info.get("session"),
                        asked=finfo.get("asked"), questions=finfo.get("questions"))
            with live_lock:
                live[t["id"]] = info["cost"]
            if v == "done" and tips is not None and not verify:
                try:
                    if [_gitops.git("rev-parse", branch, cwd=r) for r in repos] == tips:
                        v, w = "blocked", f"worker said done but committed nothing on {branch} since the review"
                except _gitops.GitError:
                    pass
            return v, w

        while True:
            try:
                failed, rcost, fixable = gate(t, tier, base, wt, branch, pubs, args, reviewer, settings, rnd)
            except _gitops.GitError as err:
                failed, rcost, fixable = str(err), 0, False
            info["cost"] = info.get("cost", 0) + rcost
            info["review_cost"] += rcost
            round_cost += rcost
            with live_lock:
                live[t["id"]] = info["cost"]
            if not failed:
                if vetoes or vfixes or outside:
                    info["fixed"] = "; ".join(x for x in (
                        f"verify passed after {vfixes} fix round(s)" if vfixes else "",
                        f"verify passed on rerun after the worker found the failure outside the branch: {outside}"
                        if outside else "",
                        f"review passed in round {len(vetoes) + 1}, after {len(vetoes)} fix round(s): "
                        + "; ".join(f"round {i}: {x}" for i, x in enumerate(vetoes, 1)) if vetoes else "") if x)
                return "done", ""
            if fixable == "verify":
                if outside:  # the rerun as it was is red too: the worker's reason goes to a human
                    info.update(asked=True, rounds=f" (verify still red on rerun: {failed})")
                    return "blocked", f"{outside}. {kept}"
                stop = ""
                if vfixes >= _config.VERIFY_ROUNDS:
                    stop = f"; no verify fix rounds left (verify_rounds = {_config.VERIFY_ROUNDS})" if vfixes else ""
                else:
                    stop = round_stop(info, round_cost, rnd + 1)
                if stop or vfixes >= _config.VERIFY_ROUNDS:
                    return "blocked", f"{failed}{stop}. {kept}"
                v, w = fix(failed, (vfixes + 1, _config.VERIFY_ROUNDS))
                if v == "blocked" and w.lower().startswith("outside:"):
                    outside, info["asked"] = w, False
                    print(f"  verify   {t['id']}  worker says outside the branch ({w}): rerunning as is", flush=True)
                    continue
                vfixes += 1
                if v != "done":
                    info["rounds"] = f" (verify fix round {vfixes}, after: {failed})"
                    return ("asking" if v == "asking" else "blocked"), w
                continue
            stop = ""
            if fixable and len(vetoes) < _config.REVIEW_ROUNDS:
                stop = round_stop(info, round_cost, rnd + 1)
            elif fixable and _config.REVIEW_ROUNDS:
                stop = f"; no fix rounds left (review_rounds = {_config.REVIEW_ROUNDS})"
            if not fixable or stop or len(vetoes) >= _config.REVIEW_ROUNDS:
                return "blocked", f"{rounds_note(failed, vetoes, stop)}. {kept}"
            vetoes.append(failed)
            v, w = fix(failed, n=len(vetoes))
            if v != "done":
                info["rounds"] = (f" (fix round {len(vetoes)}, after "
                                  + "; ".join(f"round {i}: {x}" for i, x in enumerate(vetoes, 1)) + ")")
                return ("asking" if v == "asking" else "blocked"), w

    def one(t, base, wt, branch, pubs, answered=None):
        if answered:  # a human answered its question: resume that session, or a fresh worker
            resume, fresh_because = resume_choice(None if answered == NO_SESSION else answered, wt)
            if resume:
                _history.record("resumed", t, session=answered, tier=t["tier"], after="answer")
                print(f"  answer   {t['id']}  resumed {answered}", flush=True)
                prompt = ANSWER_PROMPT.format(id=t["id"], qa=qa_text(t), note=ANSWER_NOTE, branch=branch, wt=wt)
                verdict, why, info = run_worker(t, base, wt, branch, pubs, args, agents, settings,
                                                prompt=prompt, resume=answered)
            else:
                print(f"  answer   {t['id']}  fresh session ({fresh_because})", flush=True)
                verdict, why, info = run_worker(t, base, wt, branch, pubs, args, agents, settings,
                                                lines=answer_lines(t))
            info["resumed"], info["fresh_because"] = resume, fresh_because
        else:
            verdict, why, info = run_worker(t, base, wt, branch, pubs, args, agents, settings)
        up = None
        if (args.escalate and verdict == "blocked" and info.get("asked")
                and t["tier"] in _config.WORKER_TIERS[:-1]):
            # the worker itself reported blocked: retry once a tier up on the same branch
            up = _config.WORKER_TIERS[_config.WORKER_TIERS.index(t["tier"]) + 1]
            print(f"  escalate {t['id']}  {t['tier']}→{up}: {why}")
            _history.record("escalated", t, **{"from": t["tier"], "to": up, "model": agents[f"task-{up}"]["model"],
                                               "reason": why})
            first, why1 = t["tier"], why
            v2, why, info2 = run_worker(dict(t, tier=up, escalated=True), base, wt, branch, pubs, args, agents, settings)
            info2["cost"] = info.get("cost", 0) + info2.get("cost", 0)
            info2["found"] = (info.get("found") or []) + (info2.get("found") or [])
            info2["policy"] = (info.get("policy") or []) + (info2.get("policy") or [])
            verdict, info = v2, info2
        with live_lock:
            live[t["id"]] = info.get("cost", 0)
        if verdict == "done":
            verdict, why = fix_loop(t, up or t["tier"], base, wt, branch, pubs, info)
        with _gitops.state_lock():  # also serializes every merge into a companion's trunk
            # `t` is the task as it was at launch, so nothing the worker did to its file since
            # (dropping `accept:`, retiering) lets the branch skip a human's acceptance.
            if verdict == "done" and _tasks.needs_accept(t, *([up] if up else [])):
                verdict, why = "review", (f"waits for a human: `ergon accept {t['id']}` lands it, `ergon reject "
                                          f"{t['id']} --reason R` sends it back. {kept_note(wt, branch, pubs)}")
                _history.record("review_hold", t, branch=branch, base=base)
            elif verdict == "done":
                verdict, why = land(t, base, wt, branch, pubs)
            if verdict == "asking":
                park(t, info, wt, branch, pubs)
                print(f"  asking   {t['id']}  {why}: `ergon answer {t['id']}`")
            elif verdict in ("done", "review"):
                put_status(t["id"], verdict, "; ".join(x for x in (
                    f"escalated {first}→{up}: {why1}" if up else "", info.get("fixed", ""),
                    why if verdict == "review" else "") if x) or None)
                if up:
                    t2 = _tasks.load(_config.ITEMS)[t["id"]]
                    t2["tier"] = up
                    _tasks.dump(t2, _tasks.TASK_KEYS)
                print(f"  {verdict:<8} {t['id']}  {why}")
                e = epics.get(t.get("epic"))
                if verdict == "done" and e and _gitops.epic_complete(e["id"], _tasks.load(_config.ITEMS)):
                    try:
                        _gitops.finish_epic(e["id"])
                    except _gitops.GitError as err:
                        print(f"  {e['id']} is complete but not merged: {err}")
            else:
                put_status(t["id"], "blocked", f"{why}{info.get('rounds', '')} (log {info['log']}"
                           + (f", `claude --resume {info['session']}` in {wt}" if info.get("session") else "") + ")")
                _history.record("blocked", t, reason=why, log=info.get("log"), session=info.get("session"),
                                asked=bool(info.get("asked")))
                print(f"  blocked  {t['id']}  {why}")
            if info.get("policy"):
                cur = _tasks.load(_config.ITEMS)[t["id"]]
                for title, why in info["policy"]:
                    add_note(cur, f"policy proposed: {title}" + (f" — {why}" if why else ""), who="worker")
                _tasks.dump(cur, _tasks.TASK_KEYS)
            for fid, title in file_found(t, info.get("found") or [], epics):
                print(f"  proposed {fid}  (found by {t['id']})  {title}")
                info.setdefault("filed", []).append(fid)
            if verdict == "blocked" and info.get("asked"):
                hid, title = file_human(t, why, epics)
                print(f"  proposed {hid}  (human, {t['id']} now depends on it)  {title}")
        return t, verdict, why, info

    def shared(a, b):
        """Paths both tasks name. A companion itself isn't shared: each task gets its own
        worktree of it, so only paths inside it that both name count."""
        return sorted((set(a.get("touches") or []) & set(b.get("touches") or [])) - every_touch)

    def also(t):
        return "".join(f" + {c['name']} {c['trunk']}" for c in _config.COMPANIONS if companion_touch(t, c))

    if args.dry_run:
        tasks = _tasks.load(_config.ITEMS)
        plan = list(eligible(tasks))
        rr = _config.REVIEW_ROUNDS
        rounds = f"up to {rr} fix round{'s' if rr != 1 else ''}" if rr else "no fix rounds"
        for t in plan:
            e = epics.get(t.get("epic"))
            print(f"  would run  {t['id']}  {t['tier']:<6}  on {_gitops.epic_branch(e) if e else _config.MAIN}"
                  + also(t) + (f", reviewed by {reviewer_tier(t['tier'])} ({rounds})"
                               if t["tier"] in _config.REVIEW else "")
                  + (f", up to {_config.VERIFY_ROUNDS} verify fix round{'s' if _config.VERIFY_ROUNDS != 1 else ''}"
                     if _config.VERIFY_ROUNDS and (t.get("verify") or _config.CFG.get("verify")) else "")
                  + (f", escalates to {_config.WORKER_TIERS[_config.WORKER_TIERS.index(t['tier']) + 1]} if blocked"
                     if args.escalate and t["tier"] in _config.WORKER_TIERS[:-1] else "")
                  + (", held for your accept" if _tasks.needs_accept(t) else "")
                  + (", resumes with your answer" if t.get("answered") else "") + f"  {t['title']}")
        if not plan:
            print("nothing to dispatch")
        if cap is not None:
            n = min(len(plan), args.max)
            vr = _config.VERIFY_ROUNDS
            counted = set()

            def max_calls(t):
                """Calls `t` may make, each capped at --budget: its worker; a second worker a tier up
                if --escalate retries it; then fix_loop's calls: per verify fix round a fix worker
                (when a verify is set), and for a reviewed tier the review plus every fix round
                (a fix worker and a review again)."""
                verified = bool(vr and (t.get("verify") or _config.CFG.get("verify")))
                if verified:
                    counted.add("verify")

                def after(reviewed):
                    if reviewed:
                        counted.add("review")
                    return (1 + 2 * rr if reviewed else 0) + (vr if verified else 0)
                own = t["tier"] in _config.REVIEW
                calls = 1 + after(own)
                if args.escalate and t["tier"] in _config.WORKER_TIERS[:-1]:
                    counted.add("escalate")  # after it the tier that finished is reviewed too
                    up = _config.WORKER_TIERS[_config.WORKER_TIERS.index(t["tier"]) + 1]
                    calls = max(calls, 2 + after(own or up in _config.REVIEW))
                return calls
            # The n costliest count, so the estimate stays an upper bound whichever tasks launch.
            calls = sorted((max_calls(t) for t in plan), reverse=True)[:n]
            what = [x for k, x in (
                ("escalate", "a second worker if --escalate retries it a tier up"),
                ("review", f"the review and {rounds}" + (" (a fix and a review each)" if rr else "")
                           + " for a reviewed tier"),
                ("verify", f"up to {vr} verify fix round{'s' if vr != 1 else ''} (a fix each) "
                           "if a verify is set")) if k in counted]
            print(f"  cap        ${cap:.2f} total ({args.budget_pct:g}% of ${args.budget_total:.2f}): "
                  "no new task launches once finished workers' cost reaches it"
                  + (f"; estimated at most ${sum(calls) * args.budget:.2f} for {n} task(s) at --budget "
                     f"${args.budget:g} per call" + (", counting a task as its worker plus " + ", plus ".join(what)
                                                     if what else "")
                     if args.budget else "; no --budget, so no estimate"))
        # Shared touches: whichever lands second (task into epic, or epic into trunk) may conflict,
        # so a task sharing a path with an earlier one is held until that one lands.
        for i, a in enumerate(plan):
            for b in plan[:i]:
                if shared(a, b):
                    print(f"  held       {a['id']} behind {b['id']}: both touch {', '.join(shared(a, b))}")
                    break
        for a in plan:
            for b in (t for t in tasks.values() if t["status"] in ("doing", "review")):
                if shared(a, b):
                    print(f"  overlap    {a['id']} and {b['id']} ({b['status']}) both touch {', '.join(shared(a, b))}")
    else:
        running, launched, active = {}, 0, {}  # active: running task id -> task
        held_on = {}  # task id -> the running task it was last recorded held behind
        stops, tick = [], 0.25
        stop_evt = threading.Event()

        def on_sigint(signum, frame):
            stops.append(1)
            if len(stops) == 1:
                print("\n  interrupt: no new launches; waiting for running workers "
                      "(Ctrl-C again kills them and marks them blocked)", flush=True)
            else:
                print("\n  interrupt: killing running workers", flush=True)
                kill_workers()
            stop_evt.set()
        before = _limits.probe() if probing else None
        old_handler = signal.signal(signal.SIGINT, on_sigint)
        next_scan, scan = 0.0, True
        try:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                while True:
                    stopping = bool(stops)
                    if scan and not stopping:
                        # the lock is held only for the scan, so `approve` works between scans
                        with _gitops.state_lock():
                            if cap is not None and spent() >= cap:
                                capped = True
                            queue = [] if launched >= args.max or capped else list(eligible(_tasks.load(_config.ITEMS)))
                            while queue and len(running) < args.jobs and launched < args.max:
                                t = queue.pop(0)
                                behind = next((o for o in active.values() if shared(t, o)), None)
                                if behind:  # held: stays eligible, retried when a running task lands
                                    if held_on.get(t["id"]) != behind["id"]:  # once per blocker, not per scan
                                        held_on[t["id"]] = behind["id"]
                                        _history.record("held", t, behind=behind["id"], touches=shared(t, behind))
                                    continue
                                tried.add(t["id"])
                                try:
                                    base, wt, branch, pubs = prepare(t, epics)
                                except _gitops.GitError as err:
                                    skipped[t["id"]] = f"couldn't make a worktree: {err}"
                                    continue
                                # An answered question rides along to the worker; the mark goes with
                                # the launch, so a retry after this run starts over.
                                answered = t.get("answered") or None
                                put_status(t["id"], "doing", drop=("answered",))
                                _history.record("dispatched", t, tier=t["tier"], model=agents[f"task-{t['tier']}"]["model"],
                                                base=base, branch=branch, worktree=wt,
                                                companions=[c["name"] for c, _ in pubs],
                                                **({"answered": True} if answered else {}))
                                print(f"  doing    {t['id']}  {t['tier']:<6}  {branch} from {base}"
                                      + also(t) + f"  {t['title']}")
                                running[pool.submit(one, t, base, wt, branch, pubs, answered)] = t["id"]
                                active[t["id"]] = t
                                launched += 1
                        next_scan = time.monotonic() + args.interval
                    scan = False
                    over = stopping or capped or launched >= args.max
                    if not running and (not args.watch or over):
                        break
                    if running:
                        finished, _ = wait(running, timeout=tick, return_when=FIRST_COMPLETED)
                    else:
                        stop_evt.wait(tick)
                        finished = ()
                    for f in finished:
                        tid = running.pop(f)
                        active.pop(tid, None)
                        with live_lock:  # the cost moves from live to results in one step
                            results.append(f.result())
                            live.pop(tid, None)
                        scan = True
                    if args.watch and time.monotonic() >= next_scan:
                        scan = True
        finally:
            signal.signal(signal.SIGINT, old_handler)
        stopped = bool(stops)
        if before and results and not KILL_ALL.is_set():
            window = _limits.measure(before, _limits.probe(), spent())
    for tid, why in sorted(skipped.items()):
        print(f"  skipped  {tid}  {why}")
    if results:
        cost = sum(r[3].get("cost", 0) for r in results)
        done = sum(r[1] == "done" for r in results)
        rcost = sum(r[3].get("review_cost", 0) for r in results)
        held = sum(r[1] == "review" for r in results)
        asking = sum(r[1] == "asking" for r in results)
        print(f"\n{done}/{len(results)} done" + (f", {held} awaiting your accept" if held else "")
              + (f", {asking} asking you a question" if asking else "")
              + f", ${cost:.2f}" + (f" (reviews ${rcost:.2f} of it)" if rcost else "")
              + f". Logs in {_config.RUNS.relative_to(_config.HOME)}/")
        print_limit(results, window)
        filed = [f for r in results for f in r[3].get("filed", [])]
        if filed:
            print(f"{len(filed)} proposed from workers' FOUND lines: {', '.join(filed)}")
    if stopped:
        print("\ninterrupted: stopped launching" + ("; killed running workers" if KILL_ALL.is_set() else ""))
    if capped:
        left = [t["id"] for t in eligible(_tasks.load(_config.ITEMS))]
        mean = spent() / len(results) if results else 0
        print(f"\nbudget cap ${cap:.2f} reached (spent ${spent():.2f}): stopped launching. "
              f"Not launched: {', '.join(left) or 'none'}"
              + (f". Estimated ~${mean * len(left):.2f} more to run them (mean ${mean:.2f}/task)" if left else ""))
    tasks = _tasks.load(_config.ITEMS)
    yours = [t for t in tasks.values() if t["tier"] == "human" and t["status"] == "ready" and _tasks.deps_done(t, tasks)]
    if yours:
        print(f"\nREADY FOR YOU ({len(yours)} human-tier)")
        for t in sorted(yours, key=lambda t: t["id"]):
            print(_tasks.line(t, tasks))
    waiting = [t for t in tasks.values() if _tasks.is_asking(t)]
    if waiting:
        print(f"\nAWAITING YOUR ANSWER ({len(waiting)}): `ergon answer <ID>` (human only) answers and requeues one")
        for t in sorted(waiting, key=lambda t: t["id"]):
            print(_tasks.line(t, tasks))
    held = [t for t in tasks.values() if t["status"] == "review"]
    if held:
        print(f"\nAWAITING YOUR ACCEPT ({len(held)}): `ergon accept <ID>` lands one, "
              "`ergon reject <ID> --reason R` sends it back")
        for t in sorted(held, key=lambda t: t["id"]):
            print(_tasks.line(t, tasks))
