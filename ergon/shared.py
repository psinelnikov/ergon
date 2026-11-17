"""Opt-in shared task state: <state>/ as a worktree of an orphan branch, and `ergon sync`.

The design is docs/shared-state.md ("Layout and config", "Sync, and what gets pushed"). Without
a [shared] table in .ergon.toml nothing here runs, and no command touches git for state. With it:

- `ergon sync --init` builds the orphan branch from local state, pushes it, and turns <state>/
  into a worktree of it. `ergon sync --join` does the same from the remote on another clone.
- Every command that writes state under state_lock commits the change, and any hand edits, on
  the state branch as the lock is released. Those commits never need the network.
- `ergon sync` commits pending edits, fetches, merges and pushes, and fetches and merges again
  when the push is rejected because someone pushed in between. It merges, never rebases.
- The merge pairs tasks and epics by ID and merges each one changed on both sides field by field
  ("Concurrent edits to one task"): status is compare-and-set, `approved:` is never merged as
  text, `accept:` sticks to yes, Notes are a union, the brief goes through `git merge-file`, and
  the remote wins a real conflict, which is reported. Ergon computes the merge commit's tree.
- Only a human syncs: never from an agent shell, never from a task/ branch. The guard refuses
  `ergon sync` and git writes in the state worktree from agents.
- Dispatch, started by a human, syncs on every scan and claims each task by pushing its
  ready->doing before the worker launches; a claim the merge drops lost to another dispatcher
  (dispatch.py, "claims").
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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


def commit_pending(label=None, body=None, trailers=None):
    """Commit what changed in items/ and epics/ (this command's writes and any hand edits) on the
    state branch. Returns what changed, [] if nothing did. The caller holds state_lock. `body`
    goes under the subject: dispatch's claims name the clone there, so two clones' claims are
    never the very same commit. With `trailers` (a human's decision: `Ergon-Approve: T-NNN
    sha256:…` and the like) the commit is made even if nothing changed, SSH-signed, with the
    trailers as its last paragraph; a failure to sign raises GitError and commits nothing."""
    paths = [p for p in ("items", "epics", ".gitignore") if (_config.STATE / p).exists()]
    git("add", "-A", "--", *paths)
    git("rm", "-q", "--cached", "--ignore-unmatch", "--", "items/.lock")  # in case .gitignore went missing
    parts = [what for what, _ in changes("HEAD").values()]
    if not parts and not trailers:
        return []
    label = label or COMMAND or "ergon"
    said = parts or [t.split(":", 1)[1].strip() for t in trailers]
    subject = f"{label}: {', '.join(said)}"
    msg = subject if len(subject) <= 72 else f"{label}: {len(said)} changes\n\n" + "\n".join(said)
    if body:
        msg += "\n\n" + body
    # --no-verify: the trunk's commit-msg hook (ergon's own) would refuse a message naming a
    # proposed task, and code hooks have no business with task files.
    if trailers:
        git("-c", "gpg.format=ssh", "commit", "-q", "--no-verify", "--allow-empty", "-S",
            "-m", msg + "\n\n" + "\n".join(trailers))
    else:
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


# ---------- merging both sides' edits ----------
# docs/shared-state.md, "Concurrent edits to one task". Tasks and epics pair by ID, not path, so a
# task moved into an epic folder on one side still meets its other side. Each one changed on both
# sides since the merge base is merged field by field. The remote wins a real conflict, because it
# is what the team has already seen, and every loss is reported. Nothing writes conflict markers.

NOTES = "## Notes"
# Fields written together with a status move: approve stamps a task ready, dispatch's asking:
# parks it blocked, `ergon answer` leaves answered: on a ready task. They come from whichever
# side's status wins, never mixed, so a merge can't pair one side's status with the other's stamp.
# After the merge, restamp() recomputes `approved:` of every open task from the verified signed
# approvals (gitops.approvals), so it is never merged as text.
STATUS_BOUND = ("approved", "asking", "answered")
DATED = re.compile(r"^- (\d{4}-\d{2}-\d{2})\b")


def tree(rev):
    """{path: (mode, blob)} of every file at rev."""
    out = {}
    for entry in git("ls-tree", "-r", "-z", rev, raw=True).split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            mode, _, sha = meta.split(" ")
            out[path] = (mode, sha)
    return out


def split_tree(t):
    """({ID: path} of the task and epic files, [the other paths]) of a tree()."""
    ids, rest = {}, []
    for path in t:
        if path.startswith(("items/", "epics/")) and ID_IN_PATH.search(path):
            ids[item_id(path)] = path
        else:
            rest.append(path)
    return ids, rest


def cat(sha):
    return git("cat-file", "blob", sha, raw=True)


def three_way(base, ours, theirs):
    """(result, conflicted): a change beats no change, and the remote wins a real conflict."""
    if ours == base or ours == theirs:
        return theirs, False
    if theirs == base:
        return ours, False
    return theirs, True


def merge_file(base, ours, theirs):
    """(text, clean): `git merge-file` of three texts. On a conflict (or a failure) the text is
    the remote's, so no conflict marker is ever kept."""
    if ours == base or ours == theirs:
        return theirs, True
    if theirs == base:
        return ours, True
    with tempfile.TemporaryDirectory(prefix="ergon-merge-") as d:
        names = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(d) / name).write_text(text)
            names.append(str(Path(d) / name))
        r = subprocess.run(["git", "merge-file", "-p", *names], capture_output=True, text=True)
    return (r.stdout, True) if r.returncode == 0 else (theirs, False)


def split_notes(body):
    """(the body with the lines under ## Notes taken out, those lines). The heading stays."""
    lines = body.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if l.rstrip() == NOTES:
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return "".join(lines[:i + 1] + lines[end:]), [x.rstrip("\n") for x in lines[i + 1:end]]
    return body, []


def note_entries(lines):
    """Notes as entries: a `- ` bullet and the lines under it, trailing blank lines dropped."""
    out = []
    for l in lines:
        if l.startswith("- ") or not out:
            out.append([l])
        else:
            out[-1].append(l)
    return [tuple(e[:max(i for i, x in enumerate(e) if x.strip()) + 1]) for e in out if any(x.strip() for x in e)]


def dated(entries):
    """[(date, entry)]: an entry without a date of its own takes the one before it."""
    out, last = [], ""
    for e in entries:
        m = DATED.match(e[0])
        last = m.group(1) if m else last
        out.append((last, e))
    return out


def union_notes(ours, theirs, extra=()):
    """Both sides' Notes lines, each entry once, in time order (the remote's first on a tie), then
    `extra`. One side's lines come back untouched when the union is just that side."""
    o, t = note_entries(ours), note_entries(theirs)
    have = set(t)
    both = dated(t) + [x for x in dated(o) if x[1] not in have]
    entries = [e for _, e in sorted(both, key=lambda x: x[0])] + [(x,) for x in extra]
    if entries == t:
        return theirs
    if entries == o:
        return ours
    return [l for e in entries for l in e]


def join_notes(brief, notes):
    """Put Notes lines back under the brief's ## Notes heading (adding the heading if it has none)."""
    if not notes:
        return brief
    text = "\n".join(notes) + "\n"
    lines = brief.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if l.rstrip() == NOTES:
            rest = "".join(lines[i + 1:])
            gap = "\n" if rest.strip() and notes[-1].strip() else ""
            return "".join(lines[:i]) + l.rstrip("\n") + "\n" + text + gap + rest
    return brief.rstrip("\n") + "\n\n" + NOTES + "\n" + text


def local_name():
    """Who this clone's commits are by (what sync's merge commit is authored as)."""
    r = run("var", "GIT_AUTHOR_IDENT")
    return r.stdout.split(" <")[0].strip() or os.environ.get("USER") or "this clone"


def fmt(v):
    if v is None or v == "":
        return "(empty)"
    return "[" + ", ".join(v) + "]" if isinstance(v, list) else str(v)


def merge_item(key, base, ours, theirs, epic=False):
    """Merge one task or epic file changed on both sides. Returns (text, [what to report],
    brief_lost, dropped): brief_lost means What / Done when conflicted and the remote's was kept,
    dropped that this clone's status move lost to the remote's."""
    keys = _tasks.EPIC_KEYS if epic else _tasks.TASK_KEYS
    b, o, t = (_tasks.parse_text(x, key) for x in (base, ours, theirs))
    said, out, extra_note = [], {}, []
    # status: compare-and-set against the status the local change moved from, the merge base's.
    # If the remote moved too, it pushed first: the local move is dropped, even to the same status
    # (two dispatchers claiming one task both move it ready->doing; the second claim lost).
    moved = o.get("status") != b.get("status")
    dropped = moved and t.get("status") != b.get("status")
    if dropped:
        side, move = t, f"{b.get('status')}->{o.get('status')}"
        said.append(f"{key}: dropped this clone's {move}: {remote()} had moved it to {t.get('status')} "
                    f"first (noted in its Notes)")
        extra_note.append(f"- {_tasks.today()} sync: dropped {local_name()}'s {move}, remote had "
                          f"moved to {t.get('status')}")
    else:
        side = o if moved else t
    other = t if side is o else o
    for k in ("status",) + STATUS_BOUND:
        out[k] = side.get(k)
        if k != "status" and other.get(k) != b.get(k) and other.get(k) != out[k]:
            said.append(f"{key}: kept {k}: {fmt(out[k])}, which goes with its status; "
                        f"{'this clone' if other is o else remote()}'s {fmt(other.get(k))} dropped")
    # accept: sticky toward yes. Only a one-sided edit, which needs no merge, takes it off.
    yes = [x.get("accept") for x in (t, o) if str(x.get("accept") or "").lower() in _tasks.YES]
    out["accept"] = yes[0] if yes else three_way(b.get("accept"), o.get("accept"), t.get("accept"))[0]
    names = list(keys)
    for m in (t, o, b):
        names += [k for k in m if not k.startswith("_") and k not in names]
    for k in names:
        if k in out or k == "id":
            continue
        out[k], clash = three_way(b.get(k), o.get(k), t.get(k))
        if clash:
            said.append(f"{key}: both sides changed {k}; kept {remote()}'s {fmt(t.get(k))}, "
                        f"this clone's {fmt(o.get(k))} dropped")
    # the body: Notes are a union of lines, the rest goes through git merge-file
    (bb, _), (ob, on), (tb, tn) = (split_notes(x["_body"]) for x in (b, o, t))
    brief, clean = merge_file(bb, ob, tb)
    meta = {"id": t.get("id") or key}
    meta.update((k, out[k]) for k in names if k != "id" and out.get(k) is not None)
    meta["_body"] = join_notes(brief, union_notes(on, tn, extra_note))
    same = lambda m: {k: v for k, v in m.items() if v != ""}
    for m, raw in ((t, theirs), (o, ours)):  # no reformatting when the result is one side's file
        if same(meta) == same(m):
            return raw, said, not clean, dropped
    return _tasks.render(meta, keys), said, not clean, dropped


def save_conflict(name, text):
    """Keep the local side of a conflict the remote won, in <state>/conflicts/ (never shared)."""
    d = _config.STATE / "conflicts"
    d.mkdir(exist_ok=True)
    stem, n = f"{name}-{time.strftime('%Y%m%dT%H%M%S')}", 1
    f = d / f"{stem}.md"
    while f.exists():
        n += 1
        f = d / f"{stem}-{n}.md"
    f.write_text(text)
    try:
        return f.relative_to(_config.HOME)
    except ValueError:
        return f


def merged_tree(base, ours, theirs):
    """The merge of three commits' state: ({path: (mode, blob) or (mode, None, text)}, [reports],
    {IDs whose status move here lost to the remote's}). Raises Conflict if each side created one
    ID for a different task (renumber() leaves those)."""
    tb, to, tt = tree(base), tree(ours), tree(theirs)
    (ib, rb), (io, ro), (it, rt) = split_tree(tb), split_tree(to), split_tree(tt)
    clash = sorted(k for k in (io.keys() & it.keys()) - ib.keys() if to[io[k]][1] != tt[it[k]][1])
    if clash:
        raise Conflict("\n".join(f"  {k}: each side created a different {k}" for k in clash))
    said, dropped = [], set()

    def pick(k):
        """(path, entry, merged fields or None) of ID k in the result, or None if it's gone."""
        pb, po, pt = ib.get(k), io.get(k), it.get(k)
        sb, so, st = (pb, tb.get(pb)), (po, to.get(po)), (pt, tt.get(pt))
        if so == sb:
            return (pt, tt[pt], None) if pt else None
        if st == sb:
            return (po, to[po], None) if po else None
        if po is None or pt is None:
            said.append(f"{k}: removed {'on ' + remote() if po else 'here'} but changed "
                        f"{'here' if po else 'on ' + remote()}; kept the changed file")
            return (po, to[po], None) if po else (pt, tt[pt], None)
        if pb is None:  # both sides created the same file
            return pt, tt[pt], None
        epic = pt.endswith("/" + _config.EPIC_FILE)
        ours_text = cat(to[po][1])
        text, notes, lost, moved_lost = merge_item(k, cat(tb[pb][1]), ours_text, cat(tt[pt][1]), epic)
        said.extend(notes)
        if moved_lost:
            dropped.add(k)
        if lost:
            said.append(f"{k}: both sides changed What / Done when and they conflict; kept {remote()}'s, "
                        f"and this clone's file is saved as {save_conflict(k, ours_text)}")
        # where it lives: the file name (an epic's folder name) merges three-way
        if epic:
            folder = three_way(*(os.path.dirname(x) for x in (pb, po, pt)))[0]
            return f"{folder}/{_config.EPIC_FILE}", (tt[pt][0], None, text), None
        name = three_way(*(os.path.basename(x) for x in (pb, po, pt)))[0]
        return f"{os.path.dirname(pt)}/{name}", (tt[pt][0], None, text), _tasks.parse_text(text, k)

    picked = {k: pick(k) for k in sorted(io.keys() | it.keys())}
    folders = {k: os.path.dirname(v[0]) for k, v in picked.items() if v and k.startswith("E-")}
    result = {}
    for k, v in picked.items():
        if not v:
            continue
        path, entry, meta = v
        if k.startswith("T-"):  # a task sits in its epic's folder, wherever that folder ended up
            home = os.path.dirname(path)
            if meta is not None:
                home = folders.get(meta.get("epic")) or "items"
            elif m := re.match(r"epics/(E-\d+)", home):
                home = folders.get(m.group(1), home)
            path = f"{home}/{os.path.basename(path)}"
        result[path] = entry
    # everything else (the state branch's .gitignore, notes kept in an epic's folder) by path
    for path in sorted(set(rb) | set(ro) | set(rt)):
        eb, eo, et = tb.get(path), to.get(path), tt.get(path)
        if eo == eb or eo == et:
            entry = et
        elif et == eb:
            entry = eo
        elif eo is None or et is None:
            entry = eo or et
            said.append(f"{path}: removed on one side and changed on the other; kept the changed file")
        else:
            text, clean = merge_file(cat(eb[1]) if eb else "", cat(eo[1]), cat(et[1]))
            entry = (et[0], None, text)
            if not clean:
                saved = save_conflict(path.replace("/", "_").removesuffix(".md"), cat(eo[1]))
                said.append(f"{path}: both sides changed it and they conflict; kept {remote()}'s, "
                            f"and this clone's is saved as {saved}")
        if entry:
            result[path] = entry
    return result, said, dropped


def write_tree(result):
    """A tree object holding merged_tree()'s result, built in a throwaway index."""
    lines = []
    for path, entry in sorted(result.items()):
        sha = entry[1]
        if sha is None:
            r = subprocess.run(["git", "-C", str(_config.STATE), "hash-object", "-w", "--stdin"],
                               input=entry[2], capture_output=True, text=True)
            if r.returncode:
                raise _gitops.GitError(f"git hash-object: {r.stderr.strip()}")
            sha = r.stdout.strip()
        lines.append(f"{entry[0]} {sha}\t{path}\0")
    tmp = Path(tempfile.mkdtemp(prefix="ergon-index-"))
    env = dict(os.environ, GIT_INDEX_FILE=str(tmp / "index"))
    try:
        r = subprocess.run(["git", "-C", str(_config.STATE), "update-index", "--add", "-z", "--index-info"],
                           input="".join(lines), capture_output=True, text=True, env=env)
        if r.returncode:
            raise _gitops.GitError(f"git update-index: {r.stderr.strip()}")
        w = run("write-tree", env=env)
        if w.returncode:
            raise _gitops.GitError(f"git write-tree: {w.stderr.strip()}")
        return w.stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def merge(theirs):
    """Merge the fetched remote state into the state branch. Returns (what came in, [what the
    merge had to drop or keep, to report], {IDs whose status move here lost to the remote's}).
    Raises Conflict, merging nothing, if each side created one ID for a different task."""
    ours = git("rev-parse", "HEAD")
    if theirs is None or is_ancestor(theirs, ours):
        return [], [], set()
    if is_ancestor(ours, theirs):
        came = changes(ours, theirs)
        git("merge", "-q", "--ff-only", theirs)
        return [what for what, _ in came.values()], [], set()
    r = run("merge-base", ours, theirs)
    if r.returncode:
        raise _gitops.GitError(f"{remote()} {branch()} shares no history with this clone's: was it made by "
                               f"another `ergon sync --init`? Nothing was merged")
    result, said, dropped = merged_tree(r.stdout.strip(), ours, theirs)
    msg = f"sync: merge {remote()} {branch()}" + ("\n\n" + "\n".join(said) if said else "")
    # A merge commit whose tree ergon computed, never a rebase: approvals become signed commits,
    # and a rebase would rewrite them. The worktree is clean (sync committed first), so this
    # fast-forward only writes the merged files out.
    commit = git("-c", "commit.gpgsign=false", "commit-tree", write_tree(result), "-p", ours, "-p", theirs, "-m", msg)
    came = changes(ours, commit)
    git("merge", "-q", "--ff-only", commit)
    return [what for what, _ in came.values()], said, dropped


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


def tree_ids(rev):
    """{ID: path} of every task and epic file at rev."""
    out = git("ls-tree", "-r", "--name-only", rev, "--", "items", "epics", raw=True)
    return {item_id(p): p for p in out.splitlines() if ID_IN_PATH.search(p)}


def remote_paths():
    """{ID: path} of every task and epic in the remote's state branch as last fetched ({} if never)."""
    if not _config.SHARED or not joined() or run("rev-parse", "-q", "--verify", remote_ref()).returncode:
        return {}
    return tree_ids(remote_ref())


def remote_ids(prefix):
    return [i for i in remote_paths() if i.startswith(prefix + "-")]


def collisions(theirs):
    """Local tasks and epics that are new here (not in the merge base) whose ID the remote has
    taken for a different task. Returns the IDs."""
    ours = git("rev-parse", "HEAD")
    r = run("merge-base", ours, theirs)
    if r.returncode:
        return []
    taken = tree_ids(theirs)
    out = []
    for key, (what, path) in sorted(changes(r.stdout.strip(), ours).items()):
        if what.split(" ", 1)[-1].startswith("new") and key in taken and path \
                and blob(ours, path) != blob(theirs, taken[key]):
            out.append(key)
    return out


def references(old):
    """Lines in the map cards and the log that name `old`: (path, line number, text)."""
    pat = re.compile(rf"\b{re.escape(old)}(?!\d)")
    files = []
    if _config.MAP and _config.MAP.is_dir():
        files += sorted(_config.MAP.rglob("*.md"))
    if _config.LOG and (_config.REPO / _config.LOG).is_file():
        files.append(_config.REPO / _config.LOG)
    return [(f.relative_to(_config.REPO), n, text.strip()) for f in files
            for n, text in enumerate(f.read_text(errors="replace").splitlines(), 1) if pat.search(text)]


def renumber(theirs):
    """Give each colliding unpublished task or epic the next free ID, rewriting its file name and
    `id:`, `depends:` and `source:` in local tasks that name it (and `epic:` for an epic). An
    approved task is reported and left alone. Returns {old: new}. The caller holds the state lock."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    moving = []
    for key in collisions(theirs):
        t = tasks.get(key)
        if t and (t.get("approved") or t.get("status") != "proposed"):
            print(f"collision: {key} is approved here, and a different {key} is on {remote()}; "
                  f"not renumbered, so resolve it by hand in {_config.STATE}")
        elif t or key in epics:
            moving.append(key)
    if not moving:
        return {}
    used = set(tasks) | set(epics) | set(tree_ids(theirs))
    mapping = {}
    for key in moving:
        n = max((int(i.split("-")[1]) for i in used if i[0] == key[0]), default=0) + 1
        mapping[key] = f"{key[0]}-{n:03d}"
        used.add(mapping[key])
    for old, new in mapping.items():
        if old in epics:  # the folder moves with the tasks filed in it
            src = epics[old]["_path"].parent
            src.rename(src.with_name(src.name.replace(old, new, 1)))
            e = _tasks.parse(src.with_name(src.name.replace(old, new, 1)) / _config.EPIC_FILE)
            e["id"] = new
            _tasks.dump(e, _tasks.EPIC_KEYS)
    for t in _tasks.load(_config.ITEMS).values():
        was, changed = t["_path"], False
        if t["id"] in mapping:
            t["id"], changed = mapping[t["id"]], True
            t["_path"] = was.with_name(re.sub(r"^T-\d+", t["id"], was.name))
        for field in ("depends", "source", "epic"):
            v = t.get(field)
            new = [mapping.get(d, d) for d in v] if isinstance(v, list) else mapping.get(v, v)
            if new != v:
                t[field], changed = new, True
        if changed:
            _tasks.dump(t, _tasks.TASK_KEYS)
            if t["_path"] != was:
                was.unlink()
    for old, new in mapping.items():
        print(f"renumbered: {old} → {new} ({old} is taken on {remote()}, and this one was never published)")
        for path, n, text in references(old):
            print(f"  {path}:{n} names {old}, which is now {new}: {text}")
    return mapping


def sync_core(say=print, quiet=False):
    """Commit, fetch, renumber collisions, merge and push. Returns {old ID: new ID} for what was
    renumbered. Raises SystemExit if it can't finish. `say` prints each line (dispatch indents
    them); `quiet` leaves out "up to date"."""
    if not joined():
        raise SystemExit(f"{_config.STATE} isn't a worktree of {branch()} yet: the first person runs "
                         f"`ergon sync --init`, everyone else `ergon sync --join`")
    with _gitops.state_lock("sync"):
        mine = commit_pending("sync")
        if mine:
            say("committed: " + ", ".join(mine))
        fetch_trunk(say)
        return push_rounds(say, quiet=quiet)


def fetch_trunk(say=print):
    """Fetch the trunk from the remote into refs/ergon/trunk, where the approvers file is read
    (gitops.approvals). If the remote has no trunk the ref goes, so nothing verifies. On any other
    failure the last fetch stands, and that is said."""
    r = run("fetch", "--quiet", "--no-tags", remote(), f"+refs/heads/{_config.MAIN}:{_gitops.TRUNK_REF}",
            cwd=_config.HOME)
    if r.returncode == 0:
        return True
    if "couldn't find remote ref" in (r.stderr + r.stdout).lower():
        run("update-ref", "-d", _gitops.TRUNK_REF, cwd=_config.HOME)
        say(f"{remote()} has no {_config.MAIN} branch, so there is no approvers file and no approval verifies")
    else:
        say(f"couldn't fetch {_config.MAIN} from {remote()} for the approvers file; using the last fetch: "
            f"{(r.stderr or r.stdout).strip()[:200]}")
    return False


def restamp(say=print):
    """Rewrite each open task's `approved:` from what verifies (gitops.approvals): the signer and
    date of its approval and the digest, or empty when its approval doesn't verify. Done and
    dropped tasks are history and keep theirs. Changes nothing when no approval can be verified at
    all (no approvers file yet, say), which is reported. The caller holds the state lock; commits."""
    apr = _gitops.approvals()
    if apr is None:
        return []
    if apr.error:
        say(f"approvals not checked: {apr.error}; approved: stamps left as they are")
        return []
    changed = []
    for t in _tasks.load(_config.ITEMS).values():
        if t.get("status") not in _gitops.GATED:
            continue
        want = apr.stamp(t) or ""
        if (t.get("approved") or "") != want:
            t["approved"] = want
            _tasks.dump(t, _tasks.TASK_KEYS)
            changed.append(f"{t['id']} approved: {want}" if want else
                           f"{t['id']} approved: cleared ({apr.problem(t)})")
    if changed:
        commit_pending("sync")
        for c in changed:
            say(f"restamped: {c}")
    return changed


def push_rounds(say=print, dropped=None, quiet=False):
    """sync_core after its commit: fetch, renumber collisions, merge and push, and again while the
    push is rejected. The caller holds the state lock and has committed. Returns {old ID: new ID}
    for what was renumbered; raises SystemExit if it can't finish. Adds to `dropped` (a set) each
    ID whose status move here lost to the remote's as it goes, so the caller knows that even when
    a later round raises."""
    out, renamed = "", {}
    for n in range(1, PUSH_TRIES + 1):
        try:
            theirs = fetch()
            if theirs:
                moved = renumber(theirs)
                if moved:
                    renamed.update(moved)
                    commit_pending("sync")
            came, said, lost = merge(theirs)
        except Conflict as c:
            raise SystemExit(
                f"sync stopped: these IDs name different tasks here and on {remote()}:\n{c}\n"
                f"Nothing was merged or pushed. Refile or close the local one in {_config.STATE}, "
                f"then run `ergon sync` again.")
        except _gitops.GitError as err:
            raise SystemExit(f"sync: {err}")
        if dropped is not None:
            dropped.update(lost)
        restamp(say)  # approved: is a copy of what verifies, never merged as text
        if came:
            say("pulled: " + ", ".join(came))
        for line in said:
            say(f"merged: {line}")
        if theirs and git("rev-parse", "HEAD") == theirs:
            if not quiet:
                say(f"{branch()} is up to date with {remote()}")
            return renamed
        try:
            ok, out = push()
        except _gitops.GitError as err:
            raise SystemExit(f"sync: {err}")
        if ok:
            git("update-ref", remote_ref(), "HEAD")  # a URL remote has no tracking ref: what we pushed is the remote now
            say(f"pushed {branch()} to {remote()}")
            return renamed
        say(f"push rejected: {remote()} {branch()} moved since the fetch; fetching and merging again "
            f"({n}/{PUSH_TRIES})")
    raise SystemExit(f"sync: the push was still rejected after {PUSH_TRIES} tries; your commits are kept "
                     f"locally, run `ergon sync` again.\n{out[:400]}")


def sync(args):
    refuse()
    if args.init:
        return init()
    if args.join:
        return join()
    sync_core()


def unpublished(ids):
    """The IDs among `ids` the remote doesn't have (as last fetched). [] in local mode."""
    if not _config.SHARED or not joined():
        return []
    have = remote_paths()
    return [i for i in sorted(set(ids)) if i not in have]


def ensure_published(ids):
    """For the commands that approve or start work, in shared mode: an ID that might still be
    renumbered can't be approved or started. If any of `ids` isn't on the remote yet, sync first.
    Refuses if the sync renumbered one of them (the caller's ID may now name a different task),
    failed, or left one unpublished."""
    todo = unpublished(ids)
    if not todo:
        return
    print(f"{', '.join(todo)} not published yet: syncing first")
    try:
        moved = sync_core()
    except SystemExit as err:
        raise SystemExit(f"refused: {', '.join(todo)} isn't published, and syncing failed: {err}")
    hit = [f"{o} is now {moved[o]}" for o in ids if o in moved]
    if hit:
        raise SystemExit(f"refused: the sync renumbered {'; '.join(hit)}. Check the new IDs, then run the command again.")
    left = unpublished(ids)
    if left:
        raise SystemExit(f"refused: {', '.join(left)} still isn't published to {remote()}; fix what "
                         f"`ergon sync` reports, then try again")


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
        fetch_trunk()
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
            fetch_trunk()
        except _gitops.GitError as err:
            raise SystemExit(f"sync --join: {err}")
    print(f"joined: {_config.STATE} is a worktree of {b} ({len(task_files())} task and epic files). "
          f"Run `ergon sync` to stay in step.")
