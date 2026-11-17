"""Epic branches, worktrees, start and finish."""
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
from . import history as _history
from . import shared as _shared
from . import tasks as _tasks


# ---------- epic branches ----------
# An epic's work lives on epic/<epic folder name>, cut from main when the epic starts and checked
# out in its own worktree (.claude/worktrees/E-NNN), so several epics can be worked at once.
# It merges back (--no-ff, local only, never pushed) when its last task is done or dropped,
# without switching any checkout but the epic's own.

class GitError(Exception):
    pass


@contextmanager
def state_lock(label=None):
    """Exclusive across processes and threads (each call opens its own descriptor). Not reentrant.
    In shared mode, what the holder wrote (and any hand edits) is committed on the state branch
    before the lock is released; `label` (default: the command) heads the commit message. A
    holder that raises leaves its writes for the next command to commit."""
    with open(_config.LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
        if _config.SHARED:
            _shared.commit_on_release(label)


def git(*args, raw=False, cwd=None):
    r = subprocess.run(["git", "-C", str(cwd or _config.REPO), *args], capture_output=True, text=True)
    if r.returncode:
        raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout if raw else r.stdout.strip()


def tip(ref, repo=None):
    """The commit `ref` points at, or None. For the history, so it never raises."""
    try:
        return git("rev-parse", ref, cwd=repo)
    except GitError:
        return None


def epic_branch(e):
    return f"epic/{e['_path'].parent.name}"


def branch_exists(name, repo=None):
    try:
        git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=repo)
        return True
    except GitError:
        return False


def current_branch():
    return git("branch", "--show-current")


def dirty_paths(cwd=None):
    """Tracked files with uncommitted changes (untracked files don't block a merge)."""
    out = git("status", "--porcelain", "--untracked-files=no", raw=True, cwd=cwd)
    return [l[3:] for l in out.splitlines() if l.strip()]


def worktrees(repo=None):
    """{branch: path} for every checked-out branch in any worktree of this repo (or `repo`)."""
    out, path = {}, None
    for l in git("worktree", "list", "--porcelain", cwd=repo).splitlines():
        if l.startswith("worktree "):
            path = l[len("worktree "):]
        elif l.startswith("branch refs/heads/"):
            out[l[len("branch refs/heads/"):]] = path
    return out


def merge_check(base, branch, repo=None):
    """The tree merging branch into base would give, or GitError naming the conflicts."""
    tree = subprocess.run(["git", "-C", str(repo or _config.REPO), "merge-tree", "--write-tree", base, branch],
                          capture_output=True, text=True)
    if tree.returncode:
        files = sorted(set(re.findall(r"CONFLICT \([^)]*\): .*? in (\S+)", tree.stdout)))
        raise GitError(f"conflicts with {base}" + (f" in {', '.join(files)}" if files else ""))
    return tree.stdout.split()[0]


def merge_into(base, branch, msg, no_ff=False, repo=None):
    """Land branch on base (in this repo, or `repo`) without touching any checkout that isn't
    on base. Raises GitError."""
    tree = merge_check(base, branch, repo)
    where = worktrees(repo).get(base)
    if where:  # base is checked out (e.g. your main checkout): merge there so its tree follows
        r = subprocess.run(["git", "-C", where, "merge", *(["--no-ff"] if no_ff else []),
                            "--no-edit", "-m", msg, branch], capture_output=True, text=True)
        if r.returncode:
            subprocess.run(["git", "-C", where, "merge", "--abort"], capture_output=True)
            raise GitError(f"merge in {where} refused: {(r.stderr or r.stdout).strip()[:300]}")
        return
    old, tip = git("rev-parse", base, cwd=repo), git("rev-parse", branch, cwd=repo)
    fork = git("merge-base", base, branch, cwd=repo)
    if fork == tip:  # already in base
        return
    new = tip if fork == old and not no_ff else \
        git("commit-tree", tree, "-p", old, "-p", tip, "-m", msg, cwd=repo)
    git("update-ref", f"refs/heads/{base}", new, old, cwd=repo)


def epic_complete(eid, tasks):
    kids = [t for t in tasks.values() if t.get("epic") == eid]
    return bool(kids) and all(t["status"] in ("done", "dropped") for t in kids)


def open_epic(e):
    """Cut the epic's branch from main if it has none, and check it out in its own worktree
    unless some checkout already has it. Returns that checkout's path."""
    branch = epic_branch(e)
    if not branch_exists(branch):
        git("branch", branch, _config.MAIN)
        print(f"started {e['id']} on branch {branch} (cut from {_config.MAIN})")
    where = worktrees().get(branch)
    if not where:
        where = _config.WORKTREES / e["id"]
        git("worktree", "add", str(where), branch)
        print(f"checked out {branch} at {where}")
    return Path(where)


def enter_epic(e, starting):
    """Refuse unless this checkout is on the epic's branch. If `starting`, open the epic first,
    so the refusal says where it's checked out."""
    branch = epic_branch(e)
    if current_branch() == branch or not (starting or branch_exists(branch)):
        return
    where = open_epic(e) if starting else worktrees().get(branch)
    raise GitError(f"{e['id']} is worked on {branch}, " + (
        f"checked out at {where}: cd there and run this again" if where
        else f"which isn't checked out: `ergon start {e['id']}` opens a worktree for it"))


def release(branch, where):
    """Move the checkout at `where` off `branch` so the branch can go. Returns what happened."""
    wt = Path(where)
    own = wt.parent == _config.WORKTREES  # the epic's own worktree, not the main checkout
    if own and wt.resolve() != _config.REPO.resolve():  # never remove the checkout we're running from
        try:
            git("worktree", "remove", str(wt))
            return f"removed its worktree {wt}"
        except GitError:
            pass  # untracked files in it: detach it below and leave it for you
    if not own and _config.MAIN not in worktrees():
        git("switch", _config.MAIN, cwd=wt)
        return f"{wt} is now on {_config.MAIN}"
    git("switch", "--detach", _config.MAIN, cwd=wt)
    return f"{wt} is detached at {_config.MAIN}" + (f"; `git worktree remove {wt}` when you're done in it" if own else "")


def start(args):
    epics = _tasks.load(_config.EPICS)
    e = epics.get(args.id)
    if not e:
        raise SystemExit(f"no such epic: {args.id}")
    if e["status"] != "open":
        raise SystemExit(f"{args.id} is {e['status']}")
    _shared.ensure_published([args.id])  # shared mode: an unpublished epic ID can still be renumbered
    try:
        where = open_epic(e)
    except GitError as err:
        raise SystemExit(str(err))
    print(f"{args.id} is checked out at {where}: work and run `ergon set` there")


def branch_commits(branch):
    """(short hash, subject, full message) for each commit on `branch` not yet on main."""
    # --no-merges: dispatch lands each task/<ID> branch with a "Merge T-NNN" commit.
    out = git("log", "--no-merges", "--format=%h%x1f%s%x1f%B%x1e", f"{_config.MAIN}..{branch}", raw=True)
    return [tuple(c.strip("\n").split("\x1f")) for c in out.split("\x1e") if c.strip()]


def cites(commit, tid):
    return re.search(rf"\b{tid}\b", commit[2]) is not None


def merge_message(e, tasks, commits):
    """The epic's permanent record. Task files are local, so this is what history keeps."""
    lines = [f"Merge {e['id']}: {e['title']}", ""]
    goal = re.search(r"## Goal\n(.*?)(?=\n## |\Z)", e["_body"], re.S)
    if goal and goal.group(1).strip():
        lines += [goal.group(1).strip(), ""]
    for t in sorted((t for t in tasks.values() if t.get("epic") == e["id"]), key=lambda t: t["id"]):
        lines.append(f"{t['id']} {t['title']}" + (" (dropped)" if t["status"] == "dropped" else ""))
        lines += [f"  {c[0]} {c[1]}" for c in commits if cites(c, t["id"])]
    return "\n".join(lines)


def finish_epic(eid):
    """Merge a complete epic's branch into main, then close it. Raises GitError if it can't."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    e = epics[eid]
    if not epic_complete(eid, tasks):
        raise GitError(f"{eid} still has open tasks")
    branch = epic_branch(e)
    if not branch_exists(branch):
        e["status"] = "done"
        _tasks.dump(e, _tasks.EPIC_KEYS)
        _history.record("epic_finished", e, branch=None, commit=None)
        print(f"{eid} done (never started on a branch, nothing to merge)")
        _tasks.print_issue_commands(eid, tasks)
        return
    where = worktrees().get(branch)
    stray = dirty_paths(where) if where else []
    if stray:
        raise GitError(f"uncommitted changes in {where} would miss the merge; commit them, then run "
                       f"`ergon finish {eid}`:\n  " + "\n  ".join(stray))
    try:
        merge_into(_config.MAIN, branch, merge_message(e, tasks, branch_commits(branch)), no_ff=True)
    except GitError as err:
        raise GitError(f"merging {branch} into {_config.MAIN} failed and changed nothing. Resolve it by hand "
                       f"(merge {_config.MAIN} into {branch}, say), then run `ergon finish {eid}`.\n{err}")
    note = f"; {release(branch, where)}" if where else ""
    git("merge-base", "--is-ancestor", branch, _config.MAIN)  # raises if the merge somehow didn't land
    git("branch", "-D", branch)  # -d would check against this checkout's HEAD, not main
    e["status"] = "done"
    _tasks.dump(e, _tasks.EPIC_KEYS)
    _history.record("epic_finished", e, branch=branch, commit=tip(_config.MAIN))
    print(f"{eid} done: merged {branch} into {_config.MAIN} (not pushed) and deleted the branch{note}")
    _tasks.print_issue_commands(eid, tasks, git("rev-parse", "--short", _config.MAIN))


def finish(args):
    if args.id not in _tasks.load(_config.EPICS):
        raise SystemExit(f"no such epic: {args.id}")
    try:
        finish_epic(args.id)
    except GitError as err:
        raise SystemExit(str(err))
