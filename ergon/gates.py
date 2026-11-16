"""Approval gates, the Claude Code hooks and the git commit-msg hook."""
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
from . import tasks as _tasks


def setf(args):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    item = tasks.get(args.id) or epics.get(args.id)
    if not item:
        raise SystemExit(f"no such id: {args.id}")
    is_task = args.id in tasks
    epic = epics.get(item.get("epic")) if is_task else None
    new_status = dict(kv.partition("=")[::2] for kv in args.pairs).get("status")
    if new_status and _gitops.current_branch().startswith(_config.TASK_BRANCH):
        raise SystemExit("you're a dispatched worker: the dispatcher owns status. End your "
                         "reply with `RESULT: done` or `RESULT: blocked — <why>`")
    if not is_task and new_status == "done":
        raise SystemExit(f"close epics with `ergon finish {args.id}` (it merges the epic branch)")
    for kv in args.pairs:
        key, _, val = kv.partition("=")
        if key == "approved":
            raise SystemExit("approval is human-only: use `approve` from a real terminal")
        if is_task and key == "accept" and val.lower() not in _tasks.YES:
            raise SystemExit("set can only turn `accept:` on (accept=yes); a human clears it by editing the file")
        if is_task and key == "status":
            if val not in _tasks.AGENT_MOVES.get(item["status"], set()):
                raise SystemExit(f"{args.id}: {item['status']} -> {val} not allowed via set"
                                 + (" (needs human approve)" if item["status"] == "proposed" else "")
                                 + (" (a human accepts or rejects it)" if item["status"] == "review" else ""))
        if is_task and key == "tier" and val not in _tasks.TIERS:
            raise SystemExit(f"tier must be one of {_tasks.TIERS}")
        if not is_task and key == "status" and val not in _tasks.EPIC_STATUSES:
            raise SystemExit(f"epic status must be one of {_tasks.EPIC_STATUSES}")
        if key in ("depends", "touches"):
            val = [v for v in val.split(",") if v]
        item[key] = val
    if epic and new_status:
        try:
            _gitops.enter_epic(epic, starting=new_status == "doing")
        except _gitops.GitError as err:
            raise SystemExit(str(err))
    if is_task and any(kv.partition("=")[0] == "epic" for kv in args.pairs):
        if item["epic"] and item["epic"] not in epics:
            raise SystemExit(f"no such epic: {item['epic']}")
        old, item["_path"] = item["_path"], _tasks.task_home(item, epics) / item["_path"].name
        if old != item["_path"]:
            old.rename(item["_path"])
    _tasks.dump(item, _tasks.TASK_KEYS if is_task else _tasks.EPIC_KEYS)
    if (epic and new_status == "done" and item["tier"] != "human"
            and _gitops.branch_exists(_gitops.epic_branch(epic))
            and not any(_gitops.cites(c, args.id) for c in _gitops.branch_commits(_gitops.epic_branch(epic)))):
        print(f"warning: no commit on {_gitops.epic_branch(epic)} cites {args.id}; task files are local, "
              f"so put {args.id} in the commit message or the work leaves no record")
    if epic and new_status in ("done", "dropped") and _gitops.epic_complete(epic["id"], _tasks.load(_config.ITEMS)):
        try:
            _gitops.finish_epic(epic["id"])
        except _gitops.GitError as err:
            print(f"{args.id} is {new_status}; {epic['id']} is complete but not merged: {err}")


def ask(prompt):
    """Speed bump, not security: refuse inside agent shells, ask on the controlling tty."""
    if os.environ.get("CLAUDECODE") or os.environ.get("AI_AGENT"):
        raise SystemExit("refused: running inside an agent shell. A human approves from their own terminal.")
    try:
        with open("/dev/tty", "r") as tin, open("/dev/tty", "w") as tout:
            tout.write(prompt)
            tout.flush()
            return tin.readline().strip()
    except OSError:
        raise SystemExit("refused: no terminal to confirm on.")


def human_gate(prompt):
    return ask(prompt + " [y/N] ").lower() == "y"


def approver():
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
    return name or os.environ.get("USER", "human")


def targets(ids, tasks, epics, status="proposed"):
    """The tasks ids name; an epic ID names its tasks in `status`."""
    out = []
    for i in ids:
        if i in epics:
            out += [t for t in tasks.values() if t.get("epic") == i and t["status"] == status]
        elif i in tasks:
            out.append(tasks[i])
        else:
            raise SystemExit(f"no such id: {i}")
    return out


def approve(args):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    todo = [t for t in targets(args.ids, tasks, epics) if t["status"] == "proposed"]
    if not todo:
        raise SystemExit("nothing in `proposed` among those ids")
    for t in todo:
        print(_tasks.line(t, tasks))
    if not human_gate(f"Approve these {len(todo)} task(s) as {approver()}?"):
        raise SystemExit("aborted")
    stamp = f"{approver()} {_tasks.today()}"
    for t in todo:
        t["status"], t["approved"] = "ready", stamp
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("approved", t, by=approver(), tier=t["tier"])
    print(f"approved {len(todo)}")


def review_interactive(args):
    """HUMAN ONLY: walk the proposed queue one task at a time. Every answer lands immediately."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    queue = sorted((t for t in tasks.values() if t["status"] == "proposed"
                    and (not args.epic or t.get("epic") == args.epic)
                    and (not args.tier or t["tier"] == args.tier)),
                   key=lambda t: (t.get("epic") or "~", t["id"]))
    if not queue:
        raise SystemExit("nothing awaiting approval")
    stamp, tally = f"{approver()} {_tasks.today()}", {"approved": 0, "dropped": 0, "skipped": 0}
    for i, t in enumerate(queue, 1):
        while True:
            epic = epics.get(t.get("epic"))
            print(f"\n{'=' * 78}\n[{i}/{len(queue)}]  {epic['id'] + '  ' + epic['title'] if epic else '(no epic)'}\n")
            print(t["_path"].read_text().rstrip())
            waits = [d for d in t.get("depends") or [] if tasks.get(d, {}).get("status") != "done"]
            if waits:
                print(f"\n(waits on {', '.join(waits)})")
            choice = ask(f"\n{t['id']} [{t['tier']}]  [a]pprove  [r]eject  [t]ier  [e]dit  [s]kip  [q]uit > ").lower()
            if choice == "a":
                t["status"], t["approved"] = "ready", stamp
                _tasks.dump(t, _tasks.TASK_KEYS)
                _history.record("approved", t, by=approver(), tier=t["tier"])
                tally["approved"] += 1
                break
            if choice == "r":
                reason = ask("reason (optional): ")
                t["status"] = "dropped"
                if reason:
                    t["_body"] = t["_body"].rstrip() + f"\n\nRejected {_tasks.today()}: {reason}\n"
                _tasks.dump(t, _tasks.TASK_KEYS)
                _history.record("rejected", t, by=approver(), reason=reason or None, **{"from": "proposed", "to": "dropped"})
                tally["dropped"] += 1
                break
            if choice == "t":
                tier = ask(f"tier {_tasks.TIERS}: ").lower()
                if tier not in _tasks.TIERS:
                    print(f"not a tier: {tier!r}")
                    continue
                t["tier"] = tier
                t["tier_why"] = ask(f"tier_why (enter keeps {t.get('tier_why')!r}): ") or t.get("tier_why", "")
                _tasks.dump(t, _tasks.TASK_KEYS)
            elif choice == "e":
                subprocess.run([os.environ.get("EDITOR", "vi"), str(t["_path"])])
                t = _tasks.parse(t["_path"])
                if t["status"] != "proposed":
                    print(f"{t['id']} is now {t['status']}; moving on")
                    break
            elif choice == "s":
                tally["skipped"] += 1
                break
            elif choice == "q":
                print("  ".join(f"{k} {v}" for k, v in tally.items()))
                return
    print("\n" + "  ".join(f"{k} {v}" for k, v in tally.items()))


def reject(args):
    """HUMAN ONLY. A proposed task is dropped. A task in `review` goes back to `ready` with the
    reason in its Notes; its branch and worktree stay, so the next dispatch picks up from them
    and its worker is told why."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    named = targets(args.ids, tasks, epics)
    todo = [t for t in named if t["status"] == "proposed"]
    back = [t for t in named if t["status"] == "review"]
    if not todo and not back:
        raise SystemExit("nothing in `proposed` or `review` among those ids")
    if back and not args.reason:
        raise SystemExit("rejecting work in `review` needs --reason: it's what the next worker is told to fix")
    for t in todo + back:
        print(_tasks.line(t, tasks) + ("  (review -> ready)" if t in back else ""))
    what = " and ".join(x for x in (f"drop {len(todo)} proposed" if todo else "",
                                    f"send {len(back)} in review back to ready" if back else "") if x)
    if not human_gate(f"{what[0].upper() + what[1:]}?"):
        raise SystemExit("aborted")
    for t in todo:
        t["status"] = "dropped"
        if args.reason:
            t["_body"] = t["_body"].rstrip() + f"\n\nRejected {_tasks.today()}: {args.reason}\n"
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("rejected", t, by=approver(), reason=args.reason, **{"from": "proposed", "to": "dropped"})
    for t in back:
        t["status"] = "ready"
        _dispatch.add_note(t, f"rejected by {approver()}: {args.reason}", who="review")
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("rejected", t, by=approver(), reason=args.reason, **{"from": "review", "to": "ready"})
    print(", ".join(x for x in (f"dropped {len(todo)}" if todo else "",
                                f"sent {len(back)} back to ready" if back else "") if x))


def held_branch(t, epics):
    """(base, worktree, branch, companion worktrees) of a task dispatch held at `review`, as
    land() takes them. Raises GitError if its branch is gone."""
    branch = _config.TASK_BRANCH + t["id"]
    if not _gitops.branch_exists(branch):
        raise _gitops.GitError(f"{t['id']}: no branch {branch} to land; `ergon reject {t['id']} --reason R` "
                       f"sends it back to ready")
    base = _dispatch.base_branch(t, epics)
    wt = Path(_gitops.worktrees().get(branch) or _config.WORKTREES / t["id"])
    pubs = []
    for c in _config.COMPANIONS:
        if not _dispatch.companion_touch(t, c):
            continue
        if not _gitops.branch_exists(branch, c["path"]):
            raise _gitops.GitError(f"{t['id']}: no {c['name']} branch {branch}, though its touches: names {c['name']}")
        pubs.append((c, Path(_gitops.worktrees(c["path"]).get(branch) or wt / c["rel"])))
    return base, wt, branch, pubs


def accept(args):
    """HUMAN ONLY: land tasks that dispatch held at `review` the way dispatch would have (companions
    first, then the task branch into its epic branch or the trunk), mark them done, and close an
    epic whose last task this was. A landing that fails marks the task blocked, as in dispatch."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    todo = [t for t in targets(args.ids, tasks, epics, "review") if t["status"] == "review"]
    if not todo:
        raise SystemExit("nothing in `review` among those ids")
    for t in todo:
        print(_tasks.line(t, tasks) + f"  [{_config.TASK_BRANCH}{t['id']}]")
    if not human_gate(f"Accept and land these {len(todo)} task(s) as {approver()}?"):
        raise SystemExit("aborted")
    who, failed = f"{approver()} {_tasks.today()}", 0
    for tid in [t["id"] for t in todo]:
        with _gitops.state_lock():  # land() and finish_epic() expect it; also keeps dispatch off the trunk
            t, epics = _tasks.load(_config.ITEMS)[tid], _tasks.load(_config.EPICS)
            if t["status"] != "review":
                print(f"skip {tid}: now {t['status']}")
                continue
            try:
                base, wt, branch, pubs = held_branch(t, epics)
                stray = [f"{tree}: {p}" for tree in [wt] + [pub for _, pub in pubs]
                         if tree.exists() for p in _gitops.dirty_paths(tree)]
            except _gitops.GitError as err:
                print(f"{tid} not landed, still in review: {err}")
                failed += 1
                continue
            if stray:
                print(f"{tid} not landed, still in review: uncommitted changes would miss the merge; "
                      "commit or discard them, then accept again:\n  " + "\n  ".join(stray))
                failed += 1
                continue
            _history.record("accepted", t, by=approver())
            verdict, why = _dispatch.land(t, base, wt, branch, pubs, accepted=who)
            if verdict != "done":
                _dispatch.put_status(tid, "blocked", f"couldn't land it for {who}: {why}", who="accept")
                _history.record("blocked", t, reason=f"couldn't land it for {who}: {why}", log=None, session=None, asked=False)
                print(f"  blocked  {tid}  {why}")
                failed += 1
                continue
            _dispatch.put_status(tid, "done", f"accepted by {who}: {why}", who="accept")
            print(f"  done     {tid}  {why}")
            e = epics.get(t.get("epic"))
            if e and e["status"] == "open" and _gitops.epic_complete(e["id"], _tasks.load(_config.ITEMS)):
                try:
                    _gitops.finish_epic(e["id"])
                except _gitops.GitError as err:
                    print(f"  {e['id']} is complete but not merged: {err}")
    if failed:
        sys.exit(1)


def close(args):
    """HUMAN ONLY: close approved tasks (ready / blocked / doing) whose work already landed
    elsewhere (--as done) or that no longer apply (--as dropped). Skips the epic-branch check
    on purpose: the work isn't happening on the branch. Proposed tasks go through `reject`."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    todo = []
    for i in args.ids:
        t = tasks.get(i)
        if not t:
            raise SystemExit(f"no such task: {i}")
        if t["status"] == "proposed":
            raise SystemExit(f"{i} is proposed: use `ergon reject {i}` (nobody approved it)")
        if t["status"] in ("done", "dropped"):
            print(f"skip {i}: already {t['status']}")
            continue
        todo.append(t)
    if not todo:
        raise SystemExit("nothing to close")
    for t in todo:
        print(_tasks.line(t, tasks))
    if not human_gate(f"Close these {len(todo)} task(s) as {args.as_}?"):
        raise SystemExit("aborted")
    for t in todo:
        t["status"] = args.as_
        t["_body"] = t["_body"].rstrip() + f"\n\nClosed {_tasks.today()} as {args.as_}: {args.reason}\n"
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("closed", t, by=approver(), reason=args.reason, **{"as": args.as_})
    print(f"closed {len(todo)} as {args.as_}")
    for eid in sorted({t["epic"] for t in todo if t.get("epic") in epics}):
        if epics[eid]["status"] == "open" and _gitops.epic_complete(eid, _tasks.load(_config.ITEMS)):
            try:
                _gitops.finish_epic(eid)
            except _gitops.GitError as err:
                print(f"{eid} is complete but not merged: {err}")



# ---------- Claude Code hooks ----------
# The plugin wires these (hooks/hooks.json); dispatch also injects the guard into every worker.
# Like human_gate, a speed bump: it stops an agent drifting into approving its own work, not one
# set on getting around it. Any crash fails open so a bug here never wedges a session, and a
# project with no .ergon.toml is left alone entirely.

# Shell writes: in-place editors, redirects (not to /dev/null or another fd), tee, mv/cp/rm.
SHELL_WRITE = re.compile(r"\bsed\s+(-\w+\s+)*-i|\bperl\s+-\w*i|(?<![0-9&>])>(?!\s*/dev/null|&)|"
                         r"\btee\b|\b(mv|cp|rm|truncate|install)\s|python[0-9.]*\s+-c|<<")
HUMAN_CMD = re.compile(r"\bergon(?:\.py)?[\"']?\s+(approve|reject|close|accept)\b|\breview\s+(-i|--interactive)\b")
GUARD_TOOLS = "Edit|Write|MultiEdit|Bash"
GIT_WRITE = re.compile(r"\bgit\b[^;&|]*\s(add|commit|checkout|switch|reset|restore|merge|rebase|"
                       r"cherry-pick|revert|stash|apply|am|pull|push|fetch|branch|tag|worktree|"
                       r"clean|update-ref|commit-tree|notes|gc|prune)\b")


def frontmatter(text):
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    out = {}
    for line in (m.group(1).splitlines() if m else []):
        key, _, val = line.partition(":")
        out[key.strip()] = re.sub(r"\s+#.*$", "", val).strip()
    return out


def shared_companion_reason(tool, inp):
    """Why a dispatched worker's tool call must not run because it changes a companion's shared
    checkout, or None. Each worker whose task touches a companion has its own worktree of it."""
    for c in _config.COMPANIONS:
        why = (f"{c['path']} is the shared {c['name']} checkout; dispatched workers never change it. "
               f"If your task touches {c['name']}, its worktree is {c['rel']} inside your own "
               "worktree; if it has none, stop blocked")
        if tool == "Bash":
            cmd = inp.get("command", "")
            shared = re.compile(re.escape(str(c["path"])) + r"|(?:\.\./)+" + re.escape(c["rel"]) + r"\b")
            if shared.search(cmd) and (SHELL_WRITE.search(cmd) or GIT_WRITE.search(cmd)):
                return why
            continue
        path = inp.get("file_path", "")
        if not path:
            continue
        try:
            Path(path).resolve().relative_to(c["path"].resolve())
        except ValueError:
            continue
        return why
    return None


def guard_reason(tool, inp, worker=False):
    """Why this tool call must not run, or None."""
    if worker:
        why = shared_companion_reason(tool, inp)
        if why:
            return why
    if tool == "Bash":
        cmd = inp.get("command", "")
        if HUMAN_CMD.search(cmd):
            return ("approve / reject / close / accept / review -i are human-only; ask the user to run them "
                    "in their own terminal")
        if re.search(r"\bunset\b[^;&|]*\b(CLAUDECODE|AI_AGENT)\b|\benv\b[^;&|]*-u\s*(CLAUDECODE|AI_AGENT)|"
                     r"\b(CLAUDECODE|AI_AGENT)=", cmd):
            return "don't change CLAUDECODE / AI_AGENT: they're how ergon knows an agent is running"
        if _config.TASK_DIR_IN_CMD.search(cmd) and SHELL_WRITE.search(cmd):
            return ("don't write task files from the shell: use `ergon new` / `ergon set` for state, "
                    "Edit for a task's body")
        return None
    path = inp.get("file_path", "")
    if not _config.TASK_FILE.search(path):
        return None
    p = Path(path)
    before = p.read_text() if p.exists() else ""
    after = before
    if tool == "Write":
        after = inp.get("content", "")
    else:
        for e in inp.get("edits") or [inp]:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    fb, fa = frontmatter(before), frontmatter(after)
    if fb.get("approved", "") != fa.get("approved", ""):
        return "`approved:` is written by a human only (the one rule: `ergon guide`)"
    if fb.get("status", "") != fa.get("status", "") and not (not before and fa.get("status") in ("proposed", "open")):
        if fb.get("status") == "review":
            return "a task in review leaves it only by a human's `ergon accept` or `ergon reject`, run in their own terminal"
        return "change status with `ergon set ID status=…` (it enforces the allowed moves), not by editing the file"
    if fb.get("accept", "").lower() in _tasks.YES and fa.get("accept", "").lower() not in _tasks.YES:
        return "only a human takes `accept:` off a task (it holds the task's landing for their review)"
    return None


def hook(args):
    if args.event == "commit-msg":
        return commit_msg_hook(args)
    try:
        event = json.load(sys.stdin)
        try:
            _config.configure(args.project or os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd"))
        except _config.NotConfigured:
            sys.exit(0)  # not an ergon project
        if args.event == "guard":
            why = guard_reason(event.get("tool_name"), event.get("tool_input") or {}, args.worker)
            if why:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": f"ergon guard: {why}"}}))
        elif args.event == "stop" and not event.get("stop_hook_active"):
            errs, summary = _tasks.lint()
            if errs:
                print(json.dumps({"decision": "block", "reason": (
                    "`ergon check` fails:\n" + "\n".join(errs[:20]) + f"\n{summary}\n"
                    "Fix what your change caused (e.g. a card's `→ task` link to a task you closed). "
                    "If it isn't yours, say so to the user and stop.")}))
    except Exception as err:  # fail open
        print(f"ergon hook error (ignored): {err}", file=sys.stderr)
    sys.exit(0)


# ---------- git commit-msg hook ----------
# The Claude Code guard only reaches Claude Code. Agents that read AGENTS.md (Codex, Cursor, ...)
# meet only the CLI's checks, so `ergon init --hooks` adds the cheapest bump every agent hits:
# git itself refuses a commit whose message cites a task no human approved. Opt-in, per clone.
# Like the guard, a speed bump: `git commit --no-verify` skips it, and it fails open on a crash.

COMMIT_HOOK_MARK = "# ergon commit-msg hook"
COMMIT_HOOK = """#!/bin/sh
{mark}, written by `ergon init --hooks`. Delete this file to turn it off.
# Refuses a commit whose message cites a task (T-NNN) no human has approved. A speed bump, not
# a security boundary: it does nothing in a checkout without .ergon.toml.
py={python}
ergon_py={script}
[ -x "$py" ] || py=python3
if [ -e "$ergon_py" ]; then exec "$py" "$ergon_py" hook commit-msg "$1"; fi
if command -v ergon >/dev/null 2>&1; then exec ergon hook commit-msg "$1"; fi
echo "ergon commit-msg hook: ergon not found, so this commit's task IDs weren't checked" >&2
"""
SCISSORS = re.compile(r"^#\s*-+ >8 -+$")


def cited_tasks(msg):
    """Task IDs a commit message cites, ignoring the comment lines git strips (the editor
    template's "# On branch task/T-012", say) and everything below a `commit -v` scissors line."""
    kept = []
    for l in msg.splitlines():
        if SCISSORS.match(l):
            break
        if not l.startswith("#"):
            kept.append(l)
    return sorted(set(re.findall(r"\bT-\d{3,}\b", "\n".join(kept))))


def commit_msg_reason(msg, tasks):
    """Why a commit with this message must not land, or None. Each task it cites must exist and
    carry a human's `approved:` stamp: a proposed task, one rejected to dropped, one hand-edited
    to ready, or an ID nobody filed are all refused. Epic IDs aren't approved, so aren't checked."""
    bad = []
    for tid in cited_tasks(msg):
        t = tasks.get(tid)
        if not t:
            bad.append(f"{tid}: no such task in this tracker")
        elif not t.get("approved"):
            bad.append(f"{tid}: {t.get('status') or 'no status'}, and no human has approved it")
    if not bad:
        return None
    return ("ergon: commit refused. It cites tasks no human has approved:\n  " + "\n  ".join(bad) +
            "\nOnly a human approves a task (`ergon approve`, in their own terminal). Work only `ready` "
            "tasks (`ergon next`); file new work with `ergon new task` and ask the user to approve it.")


def commit_msg_hook(args):
    """git runs this from the checkout's top level with the message file's path. Exit 1 refuses."""
    try:
        try:
            _config.configure(args.project)
        except _config.NotConfigured:
            sys.exit(0)  # not an ergon project: behave as if there were no hook
        try:
            _gitops.git("rev-parse", "-q", "--verify", "MERGE_HEAD")
            sys.exit(0)  # a merge only brings in commits that were checked when they were made
        except _gitops.GitError:
            pass
        if not args.msgfile:
            raise ValueError("no message file given")
        why = commit_msg_reason(Path(args.msgfile).read_text(errors="replace"), _tasks.load(_config.ITEMS))
    except Exception as err:  # fail open, like the Claude Code hooks
        print(f"ergon commit-msg hook error (ignored): {err}", file=sys.stderr)
        sys.exit(0)
    if why:
        print(why, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def install_commit_hook():
    """Write the commit-msg hook into this repo's hooks folder (shared by every worktree). Never
    overwrites a hook that isn't ergon's, or one managed through core.hooksPath."""
    line = 'ergon hook commit-msg "$1" || exit 1'
    managed = subprocess.run(["git", "-C", str(_config.HOME), "config", "core.hooksPath"],
                             capture_output=True, text=True).stdout.strip()
    if managed:
        raise SystemExit(f"core.hooksPath is {managed}, so something else manages this repo's git hooks; "
                         f"ergon installed none. Add this line to that commit-msg hook:\n  {line}")
    dest = (_config.HOME / _gitops.git("rev-parse", "--git-path", "hooks", cwd=_config.HOME)).resolve() / "commit-msg"
    if dest.exists() and COMMIT_HOOK_MARK not in dest.read_text(errors="replace"):
        raise SystemExit(f"{dest} exists and isn't ergon's; left it alone. Add this line to it:\n  {line}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(COMMIT_HOOK.format(mark=COMMIT_HOOK_MARK, python=shlex.quote(sys.executable),
                                       script=shlex.quote(str(_config.PKG))))
    dest.chmod(0o755)
    print(f"installed {dest}: git now refuses commits citing a task no human has approved")
