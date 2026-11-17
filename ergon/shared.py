"""Opt-in shared task state: <state>/ as a worktree of an orphan branch, and `ergon sync`.

The design is docs/shared-state.md ("Layout and config", "Sync, and what gets pushed"). Without
a [shared] table in .ergon.toml nothing here runs, and no command touches git for state. With it:

- `ergon sync --init` builds the orphan branch from local state, pushes it, and turns <state>/
  into a worktree of it. `ergon sync --join` does the same from the remote on another clone.
- Every command that writes state under state_lock commits the change, and any hand edits, on
  the state branch as the lock is released. Those commits never need the network.
- `ergon sync` commits pending edits, fetches, merges and pushes, and fetches and merges again
  when the push is rejected because someone pushed in between. It merges, never rebases.
- The merge is conservative for now: if both sides changed the same task since they last met,
  sync stops, reports it, and merges and pushes nothing. Field-level merging replaces that.
- Only a human syncs: never from an agent shell, never from a task/ branch. The guard refuses
  `ergon sync` and git writes in the state worktree from agents.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config as _config
from . import gates as _gates
from . import gitops as _gitops
from . import tasks as _tasks

COMMAND = None  # the CLI command running, named in state commit messages
PUSH_TRIES = 4  # fetch-merge-push rounds before sync gives up on a remote that keeps moving

# The state branch's own .gitignore: only items/ and epics/ are shared. Run logs, the lock,
# conflict copies, policies and anything a trunk keeps in this folder stay on this machine.
STATE_GITIGNORE = """\
# ergon shared task state: only items/ and epics/ are shared; everything else here stays local
/*
!/.gitignore
!/items/
!/epics/
items/.lock
"""
ID_IN_PATH = re.compile(r"([TE]-\d{3,})[^/]*(?:/EPIC)?\.md$")


class Conflict(Exception):
    pass


def branch():
    return _config.SHARED["branch"]


def remote():
    return _config.SHARED["remote"]


def remote_ref():
    """Where sync keeps the remote's state branch as last fetched (a remote URL has no
    remote-tracking refs, so this works the same for a name or a URL)."""
    return f"refs/ergon/remote/{branch()}"


def git(*args, raw=False, cwd=None):
    return _gitops.git(*args, raw=raw, cwd=cwd or _config.STATE)


def run(*args, cwd=None, env=None):
    return subprocess.run(["git", "-C", str(cwd or _config.STATE), *args], capture_output=True, text=True, env=env)


def joined():
    """Whether <state>/ is a worktree checked out on the state branch."""
    if not _config.SHARED or not (_config.STATE / ".git").exists():
        return False
    r = run("symbolic-ref", "-q", "HEAD")
    top = run("rev-parse", "--show-toplevel")
    return (r.returncode == 0 and r.stdout.strip() == f"refs/heads/{branch()}"
            and top.returncode == 0 and Path(top.stdout.strip()).resolve() == _config.STATE.resolve())


def log_all():
    """What to give `git log` for "every branch" (audit and report look for commits citing
    tasks): --all, less the state branch, whose commit messages name tasks without being work
    on them. Without [shared], plain --all."""
    if not _config.SHARED:
        return ["--all"]
    b = branch()
    out = [f"--exclude=refs/heads/{b}", "--exclude=refs/ergon/*", f"--exclude=refs/remotes/*/{b}", "--all"]
    if _gitops.branch_exists(b, _config.HOME):
        out.append(f"^refs/heads/{b}")  # --all also takes each worktree's HEAD, which --exclude misses
    return out


# ---------- local commits ----------

def item_id(path):
    m = ID_IN_PATH.search(path)
    return m.group(1) if m else path


def blob(rev, path):
    """The file's text at rev ("" is the index), or None if it has none there."""
    r = run("show", f"{rev}:{path}")
    return r.stdout if r.returncode == 0 else None


def status_of(text):
    m = re.match(r"---\n(.*?)\n---", text or "", re.S)
    s = re.search(r"^status:\s*([\w-]+)", m.group(1), re.M) if m else None
    return s.group(1) if s else None


def changes(a, b=None):
    """{task or epic ID (a path for other files): (what changed, its path at b or None)} between
    commit a and commit b, or the index if b is None."""
    args = ["diff", "--name-status", "-M"] + ([a, b] if b else ["--cached", a]) + ["--", "items", "epics", ".gitignore"]
    out = {}
    for line in git(*args, raw=True).splitlines():
        parts = line.split("\t")
        code, old, new = parts[0][:1], parts[1], parts[-1]
        before = None if code == "A" else blob(a, old)
        after = None if code == "D" else blob(b or "", new)
        key = item_id(new)
        if before is None:
            what = f"{key} new" + (f" {status_of(after)}" if status_of(after) else "")
        elif after is None:
            what = f"{key} removed"
        elif status_of(before) != status_of(after):
            what = f"{key} {status_of(before)}->{status_of(after)}"
        else:
            what = f"{key} " + ("moved" if code == "R" and before == after else "edited")
        out[key] = (what, None if code == "D" else new)
    return out


def commit_pending(label=None):
    """Commit what changed in items/ and epics/ (this command's writes and any hand edits) on the
    state branch. Returns what changed, [] if nothing did. The caller holds state_lock."""
    paths = [p for p in ("items", "epics", ".gitignore") if (_config.STATE / p).exists()]
    git("add", "-A", "--", *paths)
    git("rm", "-q", "--cached", "--ignore-unmatch", "--", "items/.lock")  # in case .gitignore went missing
    parts = [what for what, _ in changes("HEAD").values()]
    if not parts:
        return []
    label = label or COMMAND or "ergon"
    subject = f"{label}: {', '.join(parts)}"
    msg = subject if len(subject) <= 72 else f"{label}: {len(parts)} changes\n\n" + "\n".join(parts)
    # --no-verify: the trunk's commit-msg hook (ergon's own) would refuse a message naming a
    # proposed task, and code hooks have no business with task files. Never signed here.
    git("-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", msg)
    return parts


def commit_on_release(label=None):
    """state_lock calls this as it releases the lock. A failed commit never fails the command:
    the change stays on disk and the next command or `ergon sync` commits it."""
    if not joined():
        return
    try:
        commit_pending(label)
    except _gitops.GitError as err:
        print(f"ergon: couldn't commit the state change on {branch()} ({err}); the next ergon "
              f"command or `ergon sync` commits it", file=sys.stderr)


# ---------- fetch, merge, push ----------

def fetch(cwd=None):
    """Fetch the remote's state branch into remote_ref(). Returns its commit, or None if the
    remote has no such branch."""
    r = run("fetch", "--quiet", "--no-tags", remote(), f"+refs/heads/{branch()}:{remote_ref()}", cwd=cwd)
    if r.returncode:
        if "couldn't find remote ref" in (r.stderr + r.stdout).lower():
            return None
        raise _gitops.GitError(f"fetching {branch()} from {remote()} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return git("rev-parse", remote_ref(), cwd=cwd)


def push(cwd=None):
    """Push the state branch, never forced. Returns (pushed, git's output): not pushed means
    the remote moved since the fetch (or refused the update), so fetch and merge again."""
    r = run("push", "--porcelain", remote(), f"refs/heads/{branch()}:refs/heads/{branch()}", cwd=cwd)
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0:
        return True, out
    if re.search(r"\[(remote )?rejected\]|fetch first|non-fast-forward|stale info|cannot lock ref|failed to update ref", out):
        return False, out
    raise _gitops.GitError(f"pushing {branch()} to {remote()} failed: {out[:400]}")


def is_ancestor(a, b):
    return run("merge-base", "--is-ancestor", a, b).returncode == 0


def merge(theirs):
    """Merge the fetched remote state into the state branch. Returns what came in. Raises
    Conflict, merging nothing, if both sides changed one task since they last met."""
    ours = git("rev-parse", "HEAD")
    if theirs is None or is_ancestor(theirs, ours):
        return []
    if is_ancestor(ours, theirs):
        came = changes(ours, theirs)
        git("merge", "-q", "--ff-only", theirs)
        return [what for what, _ in came.values()]
    r = run("merge-base", ours, theirs)
    if r.returncode:
        raise _gitops.GitError(f"{remote()} {branch()} shares no history with this clone's: was it made by "
                               f"another `ergon sync --init`? Nothing was merged")
    base = r.stdout.strip()
    mine, came = changes(base, ours), changes(base, theirs)
    both = sorted(k for k in mine.keys() & came.keys()
                  if blob(ours, mine[k][1]) != blob(theirs, came[k][1]))
    if both:
        raise Conflict("\n".join(f"  {k}: here {mine[k][0].split(' ', 1)[1]}, on {remote()} "
                                 f"{came[k][0].split(' ', 1)[1]}" for k in both))
    m = run("-c", "commit.gpgsign=false", "merge", "-q", "--no-ff", "--no-verify", "--no-edit",
            "-m", f"sync: merge {remote()} {branch()}", theirs)
    if m.returncode:
        run("merge", "--abort")
        raise _gitops.GitError(f"git couldn't merge {remote()} {branch()}, so nothing was merged: "
                               f"{(m.stderr or m.stdout).strip()[:400]}")
    return [what for what, _ in came.values()]


def refuse():
    """Only a human syncs: what sync pushes is the whole team's queue."""
    if _gates.in_agent_shell():
        raise SystemExit("refused: running inside an agent shell. `ergon sync` publishes the team's queue, "
                         "so a human runs it from their own terminal.")
    if _gitops.current_branch().startswith(_config.TASK_BRANCH):
        raise SystemExit(f"refused: this checkout is on a {_config.TASK_BRANCH} branch, and dispatched work "
                         f"never syncs. Run `ergon sync` from the main checkout.")
    if not _config.SHARED:
        raise SystemExit(f"no [shared] table in {_config.CONFIG_FILE}, so task state is local and there is "
                         f"nothing to sync (docs/shared-state.md)")


def sync(args):
    refuse()
    if args.init:
        return init()
    if args.join:
        return join()
    if not joined():
        raise SystemExit(f"{_config.STATE} isn't a worktree of {branch()} yet: the first person runs "
                         f"`ergon sync --init`, everyone else `ergon sync --join`")
    out = ""
    with _gitops.state_lock("sync"):
        mine = commit_pending("sync")
        if mine:
            print("committed: " + ", ".join(mine))
        for n in range(1, PUSH_TRIES + 1):
            try:
                theirs = fetch()
                came = merge(theirs)
            except Conflict as c:
                raise SystemExit(
                    f"sync stopped: these changed both here and on {remote()} since the last sync:\n{c}\n"
                    f"Nothing was merged or pushed. Merging both sides' edits to one task isn't built yet: "
                    f"merge by hand in {_config.STATE} (git merge {remote_ref()}, fix the files, commit), "
                    f"then run `ergon sync` again.")
            except _gitops.GitError as err:
                raise SystemExit(f"sync: {err}")
            if came:
                print("pulled: " + ", ".join(came))
            if theirs and git("rev-parse", "HEAD") == theirs:
                print(f"{branch()} is up to date with {remote()}")
                return
            try:
                ok, out = push()
            except _gitops.GitError as err:
                raise SystemExit(f"sync: {err}")
            if ok:
                print(f"pushed {branch()} to {remote()}")
                return
            print(f"push rejected: {remote()} {branch()} moved since the fetch; fetching and merging again "
                  f"({n}/{PUSH_TRIES})")
    raise SystemExit(f"sync: the push was still rejected after {PUSH_TRIES} tries; your commits are kept "
                     f"locally, run `ergon sync` again.\n{out[:400]}")


# ---------- --init and --join ----------

def task_files():
    return _tasks.files(_config.ITEMS) + _tasks.files(_config.EPICS)


def url_of(name):
    r = run("remote", "get-url", name, cwd=_config.HOME)
    return r.stdout.strip() if r.returncode == 0 else name


def same_url(a, b):
    norm = lambda u: re.sub(r"(\.git)?/*$", "", u.strip())
    return norm(a) == norm(b)


def trunk_remote():
    """The remote the trunk tracks (falling back to origin), or None."""
    r = run("config", f"branch.{_config.MAIN}.remote", cwd=_config.HOME)
    if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != ".":
        return r.stdout.strip()
    return "origin" if "origin" in run("remote", cwd=_config.HOME).stdout.split() else None


def https_url(url):
    """The https address a forge serves `url` at, or None for a local path or file:// URL."""
    if url.startswith(("https://", "http://")):
        return url
    m = (re.match(r"^ssh://(?:[^@/]+@)?([\w.-]+\.[a-z]{2,})(?::\d+)?/(.+)$", url)
         or re.match(r"^(?:[^@/:]+@)?([\w.-]+\.[a-z]{2,}):(?!/)(.+)$", url))
    return f"https://{m.group(1)}/{m.group(2)}" if m else None


def publicly_readable(url):
    """Whether anyone can read `url`: an anonymous ls-remote over https succeeds, with every
    credential helper and prompt turned off."""
    https = https_url(url)
    if not https:
        return False
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", SSH_ASKPASS="true")
    try:
        r = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "--heads", https],
                           capture_output=True, text=True, env=env, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def init_warnings():
    out, url = [], url_of(remote())
    tr = trunk_remote()
    if tr and (tr == remote() or same_url(url_of(tr), url)):
        out.append(f"{remote()} is the remote the trunk ({_config.MAIN}) tracks: whoever can read the code can "
                   f"read every brief and Note on {branch()}. A separate private repo keeps them apart "
                   f"(set remote to its URL in [shared]).")
    if publicly_readable(url):
        out.append(f"{url} can be read without logging in: {branch()} would publish every brief and Note to "
                   f"anyone. Use a private repo.")
    return out


def exclude_state():
    """Keep <state>/ out of the trunk's `git status` and `git add -A`: a nested worktree shows up
    there as one untracked folder. In .git/info/exclude, so it is this clone's alone."""
    common = Path(git("rev-parse", "--git-common-dir", cwd=_config.HOME))
    f = (common if common.is_absolute() else _config.HOME / common) / "info" / "exclude"
    line = "/" + _config.STATE.relative_to(_config.HOME).as_posix() + "/"
    have = f.read_text().splitlines() if f.exists() else []
    if line not in have:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as fh:
            fh.write(("\n" if have and have[-1].strip() else "")
                     + "# ergon: the shared state worktree (ergon sync)\n" + line + "\n")


def attach(checkout):
    """Make <state>/ a worktree of the state branch where it stands. `git worktree add` refuses a
    folder that has files in it (the ones the branch was built from, run logs), so add one with
    no checkout in a spare folder, move its .git link into <state>/, repair the link, and load the
    index from the branch. `checkout` also writes the branch's files out."""
    _config.STATE.mkdir(parents=True, exist_ok=True)
    spare = Path(tempfile.mkdtemp(prefix=".ergon-attach-", dir=_config.HOME))
    try:
        tmp = spare / _config.STATE.name
        git("worktree", "add", "-q", "--no-checkout", str(tmp), branch(), cwd=_config.HOME)
        os.replace(tmp / ".git", _config.STATE / ".git")
    finally:
        shutil.rmtree(spare, ignore_errors=True)
    git("worktree", "repair", str(_config.STATE), cwd=_config.HOME)
    git("reset", "-q")
    if checkout:
        git("checkout", "-q", "--", ".")


def root_commit():
    """A parentless commit of <state>'s .gitignore, items/ and epics/ as they are on disk."""
    gitdir = git("rev-parse", "--absolute-git-dir", cwd=_config.HOME)
    tmp = Path(tempfile.mkdtemp(prefix="ergon-index-"))
    env = dict(os.environ, GIT_INDEX_FILE=str(tmp / "index"))
    try:
        base = ["git", f"--git-dir={gitdir}", f"--work-tree={_config.STATE}"]
        r = subprocess.run(base + ["add", "-A", "--", ".gitignore", "items", "epics"],
                           cwd=_config.STATE, capture_output=True, text=True, env=env)
        if r.returncode:
            raise _gitops.GitError(f"git add: {r.stderr.strip()}")
        tree = subprocess.run(base + ["write-tree"], cwd=_config.STATE, capture_output=True, text=True,
                              env=env, check=True).stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n = len(task_files())
    return git("-c", "commit.gpgsign=false", "commit-tree", tree, "-m",
               f"sync --init: shared task state ({n} task and epic files)", cwd=_config.HOME)


def init():
    """The first person: build the state branch from local state, push it, and make <state>/ a
    worktree of it. Warns when the remote is the trunk's own or public, and asks first."""
    b, gi = branch(), _config.STATE / ".gitignore"
    if (_config.STATE / ".git").exists():
        raise SystemExit(f"{_config.STATE} is a git checkout already" + (f" of {b}: run `ergon sync`" if joined() else ""))
    if _gitops.branch_exists(b, _config.HOME):
        raise SystemExit(f"a local branch {b} exists already: `ergon sync --join` checks it out, or delete it")
    ls = run("ls-remote", "--heads", remote(), f"refs/heads/{b}", cwd=_config.HOME)
    if ls.returncode:
        raise SystemExit(f"can't reach {remote()}: {(ls.stderr or ls.stdout).strip()[:400]}")
    if ls.stdout.strip():
        raise SystemExit(f"{remote()} has a {b} branch already: run `ergon sync --join` to use it")
    if gi.exists() and gi.read_text() != STATE_GITIGNORE:
        raise SystemExit(f"{gi} exists and isn't ergon's; move it out of the way first")
    files = task_files()
    for w in init_warnings():
        print(f"warning: {w}")
    print(f"This publishes {len(files)} task and epic files from {_config.STATE} (every brief and Note) "
          f"to {url_of(remote())}, branch {b}.")
    if not _gates.human_gate(f"Create {b} and push it?"):
        raise SystemExit("aborted")
    with _gitops.state_lock("sync"):
        gi.write_text(STATE_GITIGNORE)
        try:
            commit = root_commit()
            git("update-ref", f"refs/heads/{b}", commit, "", cwd=_config.HOME)
            ok, out = push(cwd=_config.HOME)
            if not ok:
                raise _gitops.GitError(f"{remote()} refused {b}: {out[:400]}")
        except _gitops.GitError as err:
            run("update-ref", "-d", f"refs/heads/{b}", cwd=_config.HOME)
            gi.unlink()
            raise SystemExit(f"sync --init: {err}\nNothing changed.")
        fetch(cwd=_config.HOME)
        attach(checkout=False)
        exclude_state()
        stray = git("status", "--porcelain", "--", "items", "epics", raw=True)
        if stray.strip():
            print(f"warning: {_config.STATE} differs from what was pushed:\n{stray}")
    print(f"pushed {b} to {remote()}; {_config.STATE} is now a worktree of it. Commit [shared] in "
          f"{_config.CONFIG_FILE}; teammates then run `ergon sync --join`.")


def join():
    """Everyone else: make <state>/ a worktree of the remote's state branch."""
    b = branch()
    if (_config.STATE / ".git").exists():
        raise SystemExit(f"{_config.STATE} is a git checkout already" + (f" of {b}: run `ergon sync`" if joined() else ""))
    local = task_files()
    if local:
        raise SystemExit(f"{_config.STATE} already holds {len(local)} task and epic files, and folding local tasks "
                         f"into the shared queue (renumbering their IDs) isn't built yet. Move them out of "
                         f"{_config.STATE}, join, then file them again with `ergon new`.")
    with _gitops.state_lock("sync"):
        try:
            theirs = fetch(cwd=_config.HOME)
            if not theirs:
                raise SystemExit(f"{remote()} has no {b} branch: the first person runs `ergon sync --init`")
            if _gitops.branch_exists(b, _config.HOME):
                if run("merge-base", "--is-ancestor", b, theirs, cwd=_config.HOME).returncode:
                    raise SystemExit(f"the local branch {b} has commits {remote()} lacks; push or delete it first")
                git("update-ref", f"refs/heads/{b}", theirs, cwd=_config.HOME)
            else:
                git("branch", b, theirs, cwd=_config.HOME)
            attach(checkout=True)
            exclude_state()
        except _gitops.GitError as err:
            raise SystemExit(f"sync --join: {err}")
    print(f"joined: {_config.STATE} is a worktree of {b} ({len(task_files())} task and epic files). "
          f"Run `ergon sync` to stay in step.")
