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
import tempfile
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


# ---------- signed approvals (shared mode) ----------
# docs/shared-state.md, "Trusting approval stamps". In shared mode a task counts as approved only
# if a commit on the state branch with a good SSH signature from a key in the approvers file
# carries an `Ergon-Approve: <ID> sha256:<digest>` trailer for the task's current brief digest
# (tasks.brief_digest). The approvers file is a git allowed_signers file read from the trunk as
# fetched from the [shared] remote (refs/ergon/trunk, which `ergon sync` and `approve` refresh),
# never from the working tree, so a change to it counts only once it is merged upstream. The
# `approved:` field is a display copy: sync rewrites it from what verifies, and editing it does
# nothing. Every failure here fails closed: no approvers file, no state worktree, no ssh-keygen,
# a git error all mean nothing verifies. Nothing turns verification off but removing [shared].

APPROVE_TRAILER = "Ergon-Approve"
ACCEPT_TRAILER, REJECT_TRAILER, CLOSE_TRAILER = "Ergon-Accept", "Ergon-Reject", "Ergon-Close"
TRUNK_REF = "refs/ergon/trunk"  # the trunk as last fetched from the [shared] remote
GATED = ("ready", "doing", "review", "blocked")  # open statuses whose approval must verify
APPROVE_VALUE = re.compile(r"^(T-\d{3,})\s+sha256:([0-9a-f]{64})$")


def approvers_path():
    """Where the approvers file sits in the trunk ([shared] approvers, default .ergon/approvers)."""
    p = str(((_config.CFG.get("shared") or {}).get("approvers")) or ".ergon/approvers").strip().strip("/")
    if not p or p.startswith(".git/") or ".." in p.split("/"):
        raise SystemExit(f"{_config.CONFIG_FILE}: [shared] approvers must be a path inside the repo, like .ergon/approvers")
    return p


def where():
    """The remote and branch the approvers file is read from, for messages."""
    return f"{_config.SHARED['remote']} {_config.MAIN}"


def approvers_text():
    """The approvers file as last fetched from the trunk on the [shared] remote, or None."""
    r = subprocess.run(["git", "-C", str(_config.HOME), "show", f"{TRUNK_REF}:{approvers_path()}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def ssh_keygen():
    return shutil.which("ssh-keygen")


def signing_howto():
    return (f"To sign in shared mode, set up SSH commit signing (git 2.34+) in this clone:\n"
            f"  git config gpg.format ssh\n"
            f"  git config user.signingkey ~/.ssh/id_ed25519.pub   # your public key, or key::ssh-ed25519 AAAA...\n"
            f"Then add your key to {approvers_path()} on {_config.MAIN} as an allowed_signers line\n"
            f"  you@example.com namespaces=\"git\" ssh-ed25519 AAAA...\n"
            f"and have it reviewed, merged and pushed to {where()}. Ergon reads it from there, never "
            f"from this checkout. Use a key that needs a touch or a confirmation (a hardware key, "
            f"`ssh-add -c`), so an agent in your shell can't sign for you.")


def config_value(key):
    r = subprocess.run(["git", "-C", str(_config.HOME), "config", "--get", key], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def signing_problem():
    """Why this clone can't make an SSH-signed state commit, with how to set it up; None if it can."""
    if not ssh_keygen():
        return "no ssh-keygen on PATH, so nothing can be signed or verified.\n" + signing_howto()
    if config_value("gpg.format") != "ssh":
        return "SSH commit signing isn't set up here (gpg.format isn't ssh).\n" + signing_howto()
    if not (config_value("user.signingkey") or config_value("gpg.ssh.defaultKeyCommand")):
        return "no signing key: user.signingkey isn't set.\n" + signing_howto()
    return None


def own_key():
    """The public key blob (base64) this clone signs with, if user.signingkey says without asking
    for a passphrase; else None."""
    key = config_value("user.signingkey")
    if key.startswith("key::"):
        key = key[5:]
    elif not re.match(r"^(ssh-|sk-|ecdsa-)", key):
        path = Path(os.path.expanduser(key)) if key else None
        if not path:
            return None
        pub = path if path.suffix == ".pub" else path.with_name(path.name + ".pub")
        try:
            key = pub.read_text()
        except OSError:
            return None
    parts = key.split()
    return parts[1] if len(parts) >= 2 else None


def listed(blob, text):
    """Is the key blob on a line of an allowed_signers text?"""
    return any(blob in l.split() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#"))


def approve_problem():
    """Why approving from this clone can't make an approval that verifies, or None. Shared mode."""
    if not _shared.joined():
        return (f"{_config.STATE} isn't a worktree of {_shared.branch()}, so an approval can't be signed: "
                f"run `ergon sync --join` (or `ergon sync --init`) first")
    why = signing_problem()
    if why:
        return why
    text = approvers_text()
    if text is None:
        return (f"there is no approvers file {approvers_path()} on {where()} (as of the last fetch), so no "
                f"approval would count.\n" + signing_howto())
    blob = own_key()
    if blob and not listed(blob, text):
        return (f"your signing key ({config_value('user.signingkey')}) isn't in {approvers_path()} on "
                f"{where()}, so your approval wouldn't count.\n" + signing_howto())
    return None


class Approvals:
    """What the state branch's signed commits approve, verified against the approvers file."""

    def __init__(self, error=None, good=None, seen=None):
        self.error = error  # why nothing verifies, or None
        self.good = good or {}  # {task ID: {digest: (signer name, date)}}, newest approval first
        self.seen = seen or set()  # task IDs some Ergon-Approve trailer names, signed or not

    def problem(self, t):
        """Why t's approval doesn't verify, or None if it does."""
        if self.error:
            return self.error
        tid, d = t["id"], _tasks.brief_digest(t)
        mine = self.good.get(tid) or {}
        if d in mine:
            return None
        if mine:
            old = next(iter(mine))
            return (f"brief changed after approval (approved sha256:{old[:8]}, now sha256:{d[:8]}): "
                    f"a human re-approves it with `ergon approve {tid}`")
        if tid in self.seen:
            return (f"approval not signed by an approver: no good signature from a key in "
                    f"{approvers_path()} on {where()}")
        return ("no signed approval: its approved: stamp was written by hand, or before approvals were "
                f"signed; a human approves it with `ergon approve {tid}`")

    def stamp(self, t):
        """The approved: stamp t's verified approval makes, or None if it doesn't verify."""
        if self.problem(t):
            return None
        name, date = self.good[t["id"]][_tasks.brief_digest(t)]
        return f"{name} {date} sha256:{_tasks.brief_digest(t)[:8]}"


def approvals():
    """Approvals verified now; None outside shared mode. Never raises: a failure is its error."""
    if not _config.SHARED:
        return None
    if not _shared.joined():
        return Approvals(error=f"{_config.STATE} isn't a worktree of {_shared.branch()}, so no approval "
                               f"can be verified: run `ergon sync --join`")
    keygen = ssh_keygen()
    if not keygen:
        return Approvals(error="no ssh-keygen on PATH, so no approval signature can be verified")
    try:
        text = approvers_text()
    except SystemExit as err:
        return Approvals(error=str(err))
    if text is None:
        return Approvals(error=f"no approvers file {approvers_path()} on {where()} as of the last "
                               f"`ergon sync`, so no approval can be verified")
    with tempfile.TemporaryDirectory(prefix="ergon-approvers-") as d:
        allowed = Path(d) / "allowed_signers"
        allowed.write_text(text)
        # Every setting that decides a verdict is given here, so no config (a repo's, say) can
        # swap the verifier or the signers list.
        r = subprocess.run(
            ["git", "-C", str(_config.STATE), "-c", f"gpg.ssh.allowedSignersFile={allowed}",
             "-c", f"gpg.ssh.program={keygen}", "-c", "log.showSignature=false",
             "log", f"--grep=^{APPROVE_TRAILER}:", f"refs/heads/{_shared.branch()}",
             f"--format=%G?%x1f%an%x1f%as%x1f%(trailers:key={APPROVE_TRAILER},valueonly,separator=%x1e)%x1d"],
            capture_output=True, text=True)
    if r.returncode:
        return Approvals(error=f"reading approvals failed: {(r.stderr or r.stdout).strip()[:300]}")
    good, seen = {}, set()
    for rec in r.stdout.split("\x1d"):
        fields = rec.strip("\n").split("\x1f")
        if len(fields) != 4:
            continue
        status, name, date, values = fields
        for v in values.split("\x1e"):
            m = APPROVE_VALUE.match(v.strip())
            if not m:
                continue
            seen.add(m.group(1))
            if status == "G":
                good.setdefault(m.group(1), {}).setdefault(m.group(2), (name, date))
    return Approvals(good=good, seen=seen)


def sign_state(label, trailers, paths=(), undo=True):
    """Shared mode, under the state lock: commit what changed (normally just this command's writes)
    on the state branch, SSH-signed, with `trailers`. If signing fails and `undo`, put `paths` back
    as the branch has them and raise SystemExit; otherwise warn and leave the change for the next
    commit, unsigned."""
    if not _shared.joined():
        return
    try:
        _shared.commit_pending(label, trailers=trailers)
    except GitError as err:
        if not undo:
            print(f"warning: couldn't sign the state commit ({err}); the change is committed unsigned "
                  f"as the lock is released", file=sys.stderr)
            return
        rel = [str(Path(p).resolve().relative_to(_config.STATE.resolve())) for p in paths]
        if rel:
            subprocess.run(["git", "-C", str(_config.STATE), "checkout", "HEAD", "--", *rel], capture_output=True)
        raise SystemExit(f"refused: signing the state commit failed, so nothing was changed: {err}\n"
                         + signing_howto())
