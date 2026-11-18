"""Shared task state (T-016): two clones of one project and a bare remote for the state branch.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from test_ergon import ERGON, FAKE_CLAUDE, clean_env, git_init, sh, stamp

SHARED_CONFIG = """\
trunk = "main"
state = "tasks"
worktrees = ".claude/worktrees"

[shared]
remote = "{remote}"
branch = "ergon-state"
"""


class Shared(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.remote = base / "state.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.remote), cwd=base)
        self.env = clean_env(ERGON_ASK_STDIN="1")
        self.a = base / "a"
        git_init(self.a)
        self.ergon(self.a, "init", "--hooks")  # the commit-msg hook must not refuse state commits
        (self.a / ".ergon.toml").write_text(SHARED_CONFIG.format(remote=self.remote))
        # Approvals are SSH-signed (T-020): a throwaway key per clone, both listed in the approvers
        # file on the remote's trunk, and one more key nobody listed.
        self.keys = base / "keys"
        self.keys.mkdir()
        for name in ("a", "b", "x"):
            sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{name}@example.com",
               "-f", str(self.keys / name), cwd=base)
        (self.a / ".ergon").mkdir()
        (self.a / ".ergon" / "approvers").write_text("".join(
            f'{n}@example.com namespaces="git" {(self.keys / (n + ".pub")).read_text().strip()}\n' for n in ("a", "b")))
        sh("git", "add", "-A", cwd=self.a)
        sh("git", "commit", "-qm", "ergon", cwd=self.a)
        sh("git", "push", "-q", str(self.remote), "main", cwd=self.a)
        self.b = base / "b"
        sh("git", "clone", "-q", str(self.a), str(self.b), cwd=base)
        for where, key in ((self.a, "a"), (self.b, "b")):
            self.signing(where, key)

    def signing(self, where, key):
        sh("git", "config", "gpg.format", "ssh", cwd=where)
        sh("git", "config", "user.signingkey", str(self.keys / key), cwd=where)

    def tearDown(self):
        self.tmp.cleanup()

    def ergon(self, where, *args, check=True, env=None, inp=None):
        return sh(sys.executable, str(ERGON), *args, cwd=where, check=check, env=env or self.env, inp=inp)

    def task(self, where, tid):
        hits = list((where / "tasks").rglob(f"{tid}-*.md"))
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    def remote_tip(self):
        return sh("git", "rev-parse", "ergon-state", cwd=self.remote).stdout.strip()

    def last_subject(self, where):
        return sh("git", "log", "-1", "--format=%s", cwd=where / "tasks").stdout.strip()

    def start(self):
        """A files T-001 and publishes it; B joins."""
        self.ergon(self.a, "new", "task", "One", "--tier", "haiku")
        r = self.ergon(self.a, "sync", "--init", inp="y\n")
        self.assertIn("pushed ergon-state", r.stdout)
        self.ergon(self.b, "sync", "--join")
        self.assertIn("title: One", self.task(self.b, "T-001").read_text())


class TestSync(Shared):
    def test_task_created_in_one_clone_appears_in_the_other(self):
        self.start()
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.a).stdout, "")  # the worktree is hidden
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.b).stdout, "")
        self.assertEqual(sh("git", "-C", "tasks", "branch", "--show-current", cwd=self.b).stdout.strip(), "ergon-state")
        # every mutating command commits locally, no network
        tip = self.remote_tip()
        self.ergon(self.b, "new", "task", "Two", "--tier", "sonnet")
        self.assertEqual(self.last_subject(self.b), "new: T-002 new proposed")
        self.assertEqual(self.remote_tip(), tip)
        self.assertIn("pushed", self.ergon(self.b, "sync").stdout)
        self.assertNotEqual(self.remote_tip(), tip)
        r = self.ergon(self.a, "sync")
        self.assertIn("pulled: T-002 new proposed", r.stdout)
        self.assertIn("title: Two", self.task(self.a, "T-002").read_text())
        # approvals and hand edits travel too
        self.ergon(self.a, "approve", "T-002", inp="y\n")
        self.assertEqual(self.last_subject(self.a), "approve: T-002 proposed->ready")
        f = self.task(self.a, "T-001")
        f.write_text(f.read_text().replace("## What\n", "## What\nA hand edit.\n"))
        r = self.ergon(self.a, "sync")
        self.assertIn("committed: T-001 edited", r.stdout)
        self.ergon(self.b, "sync")
        self.assertIn("status: ready", self.task(self.b, "T-002").read_text())
        self.assertIn("A hand edit.", self.task(self.b, "T-001").read_text())
        self.assertIn("up to date", self.ergon(self.b, "sync").stdout)
        # state commits name tasks but aren't work on them
        self.assertNotIn("new: T-00", self.ergon(self.a, "audit").stdout)

    def test_rejected_push_is_fetched_merged_and_retried(self):
        self.start()
        self.ergon(self.b, "set", "T-001", "title=Renamed")  # B's local commit, not pushed yet
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        # B pushes in between A's fetch and A's push, once
        mark = Path(self.tmp.name) / "pushed-once"
        hook = self.a / ".git" / "hooks" / "pre-push"
        hook.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            [ -e "{mark}" ] && exit 0
            touch "{mark}"
            unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
            git -C "{self.b / 'tasks'}" push -q "{self.remote}" ergon-state:ergon-state
            """))
        hook.chmod(0o755)
        r = self.ergon(self.a, "sync")
        self.assertTrue(mark.exists())
        self.assertIn("push rejected", r.stdout)
        self.assertIn("pulled: T-001 edited", r.stdout)
        self.assertIn("pushed ergon-state", r.stdout)
        self.assertIn("title: Renamed", self.task(self.a, "T-001").read_text())
        self.ergon(self.b, "sync")
        self.assertIn("title: Two", self.task(self.b, "T-002").read_text())
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.b / "tasks").stdout.strip(), self.remote_tip())


class TestMerge(Shared):
    """Both clones change T-001 since they last met; A pushes first, B's sync merges (T-018)."""

    def edit(self, where, tid, old, new):
        f = self.task(where, tid)
        text = f.read_text()
        self.assertIn(old, text)
        f.write_text(text.replace(old, new, 1))

    def settle(self):
        """Sync both clones until they agree; then they hold the same files, none has a conflict
        marker, and `ergon check` passes in each."""
        for where in (self.a, self.b, self.a):
            self.ergon(where, "sync")
        files = {}
        for where in (self.a, self.b):
            files[where] = {p.relative_to(where / "tasks").as_posix(): p.read_text()
                            for d in ("items", "epics") for p in sorted((where / "tasks" / d).rglob("*.md"))}
            for name, text in files[where].items():
                for marker in ("<<<<<<<", ">>>>>>>", "\n=======\n", "|||||||"):
                    self.assertNotIn(marker, text, f"{where.name}: {name}")
            r = self.ergon(where, "check", check=False)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(files[self.a], files[self.b])
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.b / "tasks").stdout.strip(), self.remote_tip())
        return files[self.a]

    def approved_start(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.assertIn("status: ready", self.task(self.b, "T-001").read_text())

    def test_fields_merge_three_way_and_the_remote_wins_a_real_conflict(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "title=From A")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "title=From B")
        self.ergon(self.b, "set", "T-001", "tier=sonnet")
        self.ergon(self.b, "new", "task", "Other", "--tier", "haiku")
        r = self.ergon(self.b, "sync")
        self.assertIn("merged: T-001: both sides changed title; kept", r.stdout)
        self.assertIn("this clone's From B dropped", r.stdout)
        self.assertIn("pushed ergon-state", r.stdout)
        text = self.task(self.b, "T-001").read_text()
        self.assertIn("title: From A", text)
        self.assertIn("tier: sonnet", text)
        # a merge commit with both sides as parents, not a rebase
        parents = sh("git", "log", "-1", "--format=%P", cwd=self.b / "tasks").stdout.split()
        self.assertEqual(len(parents), 2)
        self.assertIn("both sides changed title", sh("git", "log", "-1", "--format=%B", cwd=self.b / "tasks").stdout)
        files = self.settle()
        self.assertIn("tier: sonnet", next(v for k, v in files.items() if "/T-001-" in k))
        self.assertTrue(any("/T-002-" in k for k in files))

    def test_status_race_drops_the_second_claim_and_notes_it(self):
        self.approved_start()
        self.ergon(self.a, "set", "T-001", "status=doing")
        self.ergon(self.a, "sync")
        tip = self.remote_tip()
        # the second ready->doing, by someone else (the same person in the same second would make
        # A's very commit, byte for byte, and there would be nothing to merge)
        dana = clean_env(ERGON_ASK_STDIN="1", GIT_AUTHOR_NAME="Dana", GIT_COMMITTER_NAME="Dana")
        self.ergon(self.b, "set", "T-001", "status=doing", env=dana)
        r = self.ergon(self.b, "sync", env=dana)
        self.assertIn("merged: T-001: dropped this clone's ready->doing", r.stdout)
        self.assertIn("had moved it to doing first", r.stdout)
        self.assertNotEqual(self.remote_tip(), tip)  # the Note is published
        files = self.settle()
        text = next(v for k, v in files.items() if "/T-001-" in k)
        self.assertIn("status: doing", text)
        self.assertRegex(text, r"\n- \d{4}-\d\d-\d\d sync: dropped Dana's ready->doing, remote had moved to doing\n")
        self.assertIn("approved: ", text)

    def test_a_status_move_after_the_remote_moved_loses_to_it(self):
        self.approved_start()
        self.ergon(self.a, "set", "T-001", "status=doing")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "status=blocked")
        self.ergon(self.b, "set", "T-001", "tier_why=Edited on B")
        r = self.ergon(self.b, "sync")
        self.assertIn("dropped this clone's ready->blocked", r.stdout)
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("status: doing", text)
        self.assertIn("tier_why: Edited on B", text)  # the rest of B's edit still merges (outside the brief, so the approval holds)
        self.assertIn("sync: dropped Tester's ready->blocked, remote had moved to doing", text)

    def test_a_status_move_the_remote_didnt_race_applies(self):
        self.approved_start()
        self.ergon(self.a, "set", "T-001", "tier_why=Edited on A")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "status=doing")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("dropped", r.stdout)
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("status: doing", text)
        self.assertIn("tier_why: Edited on A", text)

    def test_notes_appended_on_both_sides_are_kept(self):
        self.start()
        self.edit(self.a, "T-001", "## Notes\n", "## Notes\n- 2026-11-02 A: second\n- 2026-11-04 A: fourth\n")
        self.ergon(self.a, "sync")
        self.edit(self.b, "T-001", "## Notes\n", "## Notes\n- 2026-11-01 B: first\n- 2026-11-03 B: third\n"
                                                 "  wrapped under third\n")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("merged:", r.stdout)  # nothing was lost
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        notes = text.split("## Notes\n", 1)[1]
        self.assertEqual(notes, "- 2026-11-01 B: first\n- 2026-11-02 A: second\n- 2026-11-03 B: third\n"
                                "  wrapped under third\n- 2026-11-04 A: fourth\n")
        # appended again on both sides: still each line once
        self.edit(self.a, "T-001", "A: fourth\n", "A: fourth\n- 2026-11-05 A: fifth\n")
        self.edit(self.b, "T-001", "A: fourth\n", "A: fourth\n- 2026-11-05 B: fifth\n")
        self.ergon(self.a, "sync")
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertEqual(text.count("A: fourth"), 1)
        self.assertTrue(text.endswith("- 2026-11-04 A: fourth\n- 2026-11-05 A: fifth\n- 2026-11-05 B: fifth\n"), text)

    def test_brief_edits_merge_and_a_conflict_keeps_the_remote_and_saves_the_local_copy(self):
        self.start()
        self.edit(self.a, "T-001", "## What\n", "## What\nBuild the thing.\n")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        # different sections: both kept
        self.edit(self.a, "T-001", "Build the thing.\n", "Build the thing well.\n")
        self.ergon(self.a, "sync")
        self.edit(self.b, "T-001", "## Done when\n", "## Done when\n- tests pass\n")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("merged:", r.stdout)
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("Build the thing well.\n", text)
        self.assertIn("- tests pass\n", text)
        # the same line: the remote's wins, B's whole file is kept locally
        self.edit(self.a, "T-001", "Build the thing well.", "Build it in Rust.")
        self.ergon(self.a, "sync")
        self.edit(self.b, "T-001", "Build the thing well.", "Build it in Go.")
        self.edit(self.b, "T-001", "## Notes\n", "## Notes\n- 2026-11-10 B: picked Go\n")
        r = self.ergon(self.b, "sync")
        self.assertIn("merged: T-001: both sides changed What / Done when and they conflict", r.stdout)
        saved = list((self.b / "tasks" / "conflicts").glob("T-001-*.md"))
        self.assertEqual(len(saved), 1)
        self.assertIn(f"saved as tasks/conflicts/{saved[0].name}", r.stdout)
        self.assertIn("Build it in Go.", saved[0].read_text())
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("Build it in Rust.", text)
        self.assertNotIn("Build it in Go.", text)
        self.assertIn("- 2026-11-10 B: picked Go\n", text)  # Notes still merge
        self.assertEqual(sh("git", "ls-files", "conflicts", cwd=self.b / "tasks").stdout, "")  # local only
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.b / "tasks").stdout, "")

    def test_accept_yes_survives(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "accept=yes")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "title=Edited on B")
        self.ergon(self.b, "sync")
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("accept: yes", text)
        self.assertIn("title: Edited on B", text)
        # taken off on one side while the other edits the task: yes sticks
        self.edit(self.a, "T-001", "accept: yes\n", "")
        self.ergon(self.a, "set", "T-001", "title=Edited on A")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "tier=sonnet")
        self.ergon(self.b, "sync")
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("accept: yes", text)
        self.assertIn("tier: sonnet", text)
        self.assertIn("title: Edited on A", text)

    def test_a_task_moved_into_an_epic_on_one_side_and_edited_on_the_other_is_merged(self):
        self.start()
        self.ergon(self.a, "new", "epic", "Big")
        self.ergon(self.a, "set", "T-001", "epic=E-001")
        self.assertTrue((self.a / "tasks" / "epics" / "E-001-big" / "T-001-one.md").exists())
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "tier=sonnet")
        self.edit(self.b, "T-001", "## What\n", "## What\nEdited on B.\n")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("merged:", r.stdout)
        files = self.settle()
        self.assertEqual([k for k in files if "T-001" in k], ["epics/E-001-big/T-001-one.md"])
        text = files["epics/E-001-big/T-001-one.md"]
        self.assertIn("epic: E-001", text)
        self.assertIn("tier: sonnet", text)
        self.assertIn("Edited on B.", text)


class TestWhoSyncs(Shared):
    def test_refused_in_agent_shells_and_on_task_branches(self):
        self.start()
        for var in ("CLAUDECODE", "AI_AGENT"):
            r = self.ergon(self.b, "sync", check=False, env=clean_env(**{var: "1"}))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("agent shell", r.stderr)
        wt = self.a / ".claude" / "worktrees" / "T-001"
        sh("git", "worktree", "add", "-q", "-b", "task/T-001", str(wt), cwd=self.a)
        r = self.ergon(wt, "sync", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("task/ branch", r.stderr)

    def test_init_warns_about_the_trunks_own_remote_and_asks(self):
        origin = Path(self.tmp.name) / "origin.git"
        sh("git", "init", "-q", "--bare", str(origin), cwd=self.a)
        sh("git", "remote", "add", "origin", str(origin), cwd=self.a)
        sh("git", "push", "-q", "-u", "origin", "main", cwd=self.a)
        (self.a / ".ergon.toml").write_text(SHARED_CONFIG.format(remote="origin"))
        self.ergon(self.a, "new", "task", "One", "--tier", "haiku")
        r = self.ergon(self.a, "sync", "--init", check=False, inp="n\n")
        self.assertIn("warning: origin is the remote the trunk (main) tracks", r.stdout)
        self.assertIn("aborted", r.stderr)
        self.assertFalse((self.a / "tasks" / ".git").exists())
        self.assertEqual(sh("git", "ls-remote", "--heads", str(origin), "ergon-state", cwd=self.a).stdout, "")

    def test_init_and_join_hint_about_policies_unless_on_the_trunk(self):
        hint = "policies stay on this machine"
        self.ergon(self.a, "new", "task", "One", "--tier", "haiku")
        r = self.ergon(self.a, "sync", "--init", inp="y\n")
        self.assertIn(hint, r.stdout)
        r = self.ergon(self.b, "sync", "--join")
        self.assertIn(hint, r.stdout)
        # configured to a trunk path: no hint
        c = Path(self.tmp.name) / "c"
        sh("git", "clone", "-q", str(self.a), str(c), cwd=self.a)
        (c / ".ergon.toml").write_text(
            SHARED_CONFIG.replace("[shared]", 'policies = ".ergon/policies.md"\n\n[shared]').format(remote=self.remote))
        r = self.ergon(c, "sync", "--join")
        self.assertNotIn(hint, r.stdout)

    def test_join_refuses_local_tasks_and_init_refuses_twice(self):
        self.start()
        r = self.ergon(self.a, "sync", "--init", check=False, inp="y\n")
        self.assertIn("run `ergon sync`", r.stderr)
        c = Path(self.tmp.name) / "c"
        sh("git", "clone", "-q", str(self.a), str(c), cwd=self.a)
        self.ergon(c, "new", "task", "Mine", "--tier", "haiku")
        r = self.ergon(c, "sync", "--join", check=False)
        self.assertIn("already holds 1 task", r.stderr)
        self.assertFalse((c / "tasks" / ".git").exists())


class TestGuard(Shared):
    def guard(self, cmd, cwd=None, where=None):
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd or self.a)})
        out = self.ergon(where or self.a, "hook", "guard", inp=event).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else None

    def test_agents_neither_sync_nor_write_git_in_the_state_worktree(self):
        self.start()
        for cmd in ["ergon sync", "python3 -m ergon sync --init", "cd x && ergon sync --join"]:
            self.assertIn("human-only", self.guard(cmd), cmd)
        for cmd in ["git -C tasks commit -qam x", "cd tasks && git add -A && git commit -m x",
                    "cd tasks/items; git reset --hard HEAD~1", f"git -C {self.a / 'tasks'} push origin HEAD",
                    "git --git-dir=tasks/.git --work-tree=tasks commit -m x",
                    "git push origin ergon-state", "git push origin HEAD:refs/heads/ergon-state",
                    "git branch -f ergon-state main", "git update-ref refs/heads/ergon-state HEAD",
                    "git worktree remove --force tasks", "bash -c 'cd tasks && git commit -am x'",
                    "GIT_DIR=tasks/.git git commit -m x", "sh <<EOF\ngit -C tasks push\nEOF"]:
            self.assertIn("shared state worktree", self.guard(cmd) or "", cmd)
        self.assertIn("shared state worktree", self.guard("git commit -m x", cwd=self.a / "tasks" / "items"))
        for cmd in ["git -C tasks log --oneline", "git -C tasks status", "git -C tasks diff HEAD~1",
                    "git commit -m 'update tasks and ergon-state docs'", "git push origin main", "git status",
                    "git log ergon-state", "cat tasks/items/*.md"]:
            self.assertIsNone(self.guard(cmd), cmd)
        self.assertIsNone(self.guard("git log --oneline", cwd=self.a / "tasks"))


class TestLocalOnly(unittest.TestCase):
    """Without [shared] nothing changes: no command runs git on the state folder."""

    def test_no_git_for_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        root, bin_, log = base / "proj", base / "bin", base / "git.log"
        git_init(root)
        bin_.mkdir()
        real = shutil.which("git")
        (bin_ / "git").write_text(f'#!/bin/sh\nprintf "%s %s\\n" "$PWD" "$*" >> "{log}"\nexec "{real}" "$@"\n')
        (bin_ / "git").chmod(0o755)
        env = clean_env(ERGON_ASK_STDIN="1", PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}")
        run = lambda *a, **kw: sh(sys.executable, str(ERGON), *a, cwd=root, env=env, **kw)
        run("init")
        run("new", "epic", "Epic")
        run("new", "task", "One", "--tier", "haiku")
        run("new", "task", "Two", "--tier", "haiku", "--epic", "E-001")
        run("set", "T-001", "title=Renamed")
        run("approve", "T-001", inp="y\n")
        run("reject", "T-002", inp="y\n")
        run("tidy")
        run("set", "T-001", "status=doing")
        run("check")
        run("audit")
        r = run("sync", check=False)
        self.assertIn("no [shared] table", r.stderr)
        self.assertFalse((root / "tasks" / ".git").exists())
        calls = log.read_text()
        self.assertIn("rev-parse", calls)  # the wrapper saw ergon's git calls
        state = str(root / "tasks")
        self.assertEqual([l for l in calls.splitlines() if state in l], [])
        self.assertNotIn("--exclude", calls)
        # and the guard's state-worktree refusal stays off
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git -C tasks commit -m x"}, "cwd": str(root)})
        self.assertEqual(run("hook", "guard", inp=event).stdout, "")


if __name__ == "__main__":
    unittest.main()


class TestIdAllocation(Shared):
    def test_offline_collision_renumbers_the_second_clone_and_its_dependants(self):
        self.start()
        self.ergon(self.a, "new", "task", "From A", "--tier", "haiku")  # T-002 in both
        self.ergon(self.b, "new", "task", "From B", "--tier", "haiku")
        self.ergon(self.b, "new", "task", "Needs B", "--tier", "haiku")  # T-003
        self.ergon(self.b, "set", "T-003", "depends=T-002")
        self.ergon(self.b, "new", "task", "Filed from B", "--tier", "haiku", "--source", "T-002")  # T-004
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "sync")
        self.assertIn("renumbered: T-002 → T-005", r.stdout)
        self.assertIn("title: From A", self.task(self.b, "T-002").read_text())
        self.assertIn("title: From B", self.task(self.b, "T-005").read_text())
        self.assertIn("depends: [T-005]", self.task(self.b, "T-003").read_text())
        self.assertIn("source: T-005", self.task(self.b, "T-004").read_text())
        self.assertIn("pushed ergon-state", r.stdout)
        self.ergon(self.a, "sync")
        self.assertIn("title: From B", self.task(self.a, "T-005").read_text())
        self.assertEqual(self.ergon(self.b, "sync").stdout.count("renumbered"), 0)

    def test_map_cards_and_log_hits_are_printed(self):
        self.start()
        (self.b / "map").mkdir()
        (self.b / ".ergon.toml").write_text((self.b / ".ergon.toml").read_text() + 'map = "map"\nlog = "TODO.md"\n')
        # options sit above [shared]: rewrite so they stay top-level
        (self.b / ".ergon.toml").write_text(SHARED_CONFIG.format(remote=self.remote).replace(
            "[shared]", 'map = "map"\nlog = "TODO.md"\n\n[shared]'))
        (self.b / "map" / "x.md").write_text("gap → task T-002\n")
        (self.b / "TODO.md").write_text("see T-002 here\nT-0021 is other\n")
        self.ergon(self.a, "new", "task", "From A", "--tier", "haiku")
        self.ergon(self.b, "new", "task", "From B", "--tier", "haiku")
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "sync")
        self.assertIn("map/x.md:1 names T-002", r.stdout)
        self.assertIn("TODO.md:1 names T-002", r.stdout)
        self.assertNotIn("TODO.md:2", r.stdout)

    def test_approving_an_unpublished_task_syncs_first(self):
        self.start()
        self.ergon(self.b, "new", "task", "Two", "--tier", "haiku")
        tip = self.remote_tip()
        r = self.ergon(self.b, "approve", "T-002", inp="y\n")
        self.assertIn("syncing first", r.stdout)
        self.assertNotEqual(self.remote_tip(), tip)
        self.assertIn("status: ready", self.task(self.b, "T-002").read_text())

    def test_approving_refuses_when_the_sync_renumbered_it(self):
        self.start()
        self.ergon(self.a, "new", "task", "From A", "--tier", "haiku")
        self.ergon(self.b, "new", "task", "From B", "--tier", "haiku")
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "approve", "T-002", check=False, inp="y\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("renumbered", r.stderr + r.stdout)
        self.assertIn("status: proposed", self.task(self.b, "T-002").read_text())
        self.assertIn("status: proposed", self.task(self.b, "T-003").read_text())

    def test_approving_refuses_when_the_sync_fails(self):
        self.start()
        self.ergon(self.b, "new", "task", "Two", "--tier", "haiku")
        sh("git", "-C", "tasks", "remote", "add", "x", "/nonexistent", cwd=self.b)
        shutil.move(str(self.remote), str(self.remote) + ".gone")
        r = self.ergon(self.b, "approve", "T-002", check=False, inp="y\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stderr + r.stdout)
        self.assertIn("status: proposed", self.task(self.b, "T-002").read_text())

    def test_a_colliding_approved_task_is_reported_and_left_alone(self):
        self.start()
        self.ergon(self.a, "new", "task", "From A", "--tier", "haiku")
        self.ergon(self.b, "new", "task", "From B", "--tier", "haiku")
        t = self.task(self.b, "T-002")
        t.write_text(t.read_text().replace("status: proposed", "status: ready").replace("approved: ", "approved: Dana 2026-11-17", 1))
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "sync", check=False)
        self.assertIn("collision: T-002 is approved here", r.stdout)
        self.assertNotIn("renumbered:", r.stdout)
        self.assertIn("title: From B", self.task(self.b, "T-002").read_text())

    def test_an_epic_collision_renumbers_its_folder_and_tasks(self):
        self.start()
        self.ergon(self.a, "new", "epic", "Epic A")
        self.ergon(self.b, "new", "epic", "Epic B")
        self.ergon(self.b, "new", "task", "In B", "--tier", "haiku", "--epic", "E-001")
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "sync")
        self.assertIn("renumbered: E-001 → E-002", r.stdout)
        self.assertTrue(list((self.b / "tasks" / "epics").glob("E-002-epic-b/EPIC.md")))
        self.assertIn("epic: E-002", self.task(self.b, "T-002").read_text())


class TestDispatchClaims(Shared):
    """Dispatch claims a task by pushing ready -> doing before its worker launches (T-019). Two
    clones, the fake `claude` on PATH, and each clone a different person (`held:` names them)."""

    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        bindir = base / "bin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        self.seen = base / "seen.jsonl"
        common = dict(ERGON_ASK_STDIN="1", PATH=f"{bindir}:{os.environ['PATH']}", FAKE_SEEN=str(self.seen))
        self.env_a = clean_env(USER="ann", LOGNAME="ann", **common)
        self.env_b = clean_env(USER="dana", LOGNAME="dana", **common)
        self.host = socket.gethostname().split(".")[0]

    def workers(self):
        """The task each worker launched (from either clone) was dispatched on."""
        if not self.seen.exists():
            return []
        rows = [json.loads(l) for l in self.seen.read_text().splitlines() if l.strip()]
        return [re.search(r"dispatched on (T-\d+)", r["prompt"]).group(1) for r in rows if r["agent"] != "REVIEW"]

    def status(self, where, tid):
        return re.search(r"^status: (\S+)", self.task(where, tid).read_text(), re.M).group(1)

    def ready_in_both(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.assertEqual(self.status(self.b, "T-001"), "ready")

    def test_two_dispatchers_racing_for_one_task_launch_one_worker(self):
        self.ready_in_both()
        # A's whole dispatch runs between B's fetch and B's push of its claim
        mark, a_out = Path(self.tmp.name) / "raced", Path(self.tmp.name) / "a-dispatch.txt"
        hook = self.b / ".git" / "hooks" / "pre-push"
        hook.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            [ -e "{mark}" ] && exit 0
            touch "{mark}"
            unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
            cd "{self.a}" && USER=ann LOGNAME=ann "{sys.executable}" "{ERGON}" dispatch > "{a_out}" 2>&1
            exit 0
            """))
        hook.chmod(0o755)
        r = self.ergon(self.b, "dispatch", env=self.env_b)
        self.assertTrue(mark.exists())
        a = a_out.read_text()
        self.assertIn("doing    T-001", a)
        self.assertIn("done     T-001", a)
        self.assertEqual(self.workers(), ["T-001"])  # one worker: A's
        self.assertIn("push rejected", r.stdout)
        self.assertIn("claimed  T-001  another dispatcher claimed it first (", r.stdout)
        self.assertIn("has it done); not launched", r.stdout)
        self.assertNotIn("doing    T-001", r.stdout)
        self.assertFalse((self.b / ".claude" / "worktrees" / "T-001").exists())  # B made nothing for it
        self.assertEqual(self.status(self.b, "T-001"), "done")
        # A pushed done as it landed; B's lost claim is noted, not applied
        path = self.task(self.b, "T-001").relative_to(self.b / "tasks").as_posix()
        text = sh("git", "show", f"ergon-state:{path}", cwd=self.remote).stdout
        self.assertIn("status: done", text)
        self.assertRegex(text, r"sync: dropped \S+'s ready->doing, remote had moved to done")
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.b / "tasks").stdout.strip(), self.remote_tip())
        # each claim commit names the clone that made it
        log = sh("git", "log", "--format=%B", "ergon-state", cwd=self.remote).stdout
        self.assertIn(f"Claimed by ann@{self.host} in {self.a}", log)
        self.assertIn(f"Claimed by dana@{self.host} in {self.b}", log)

    def test_two_dispatchers_started_together_launch_one_worker(self):
        self.ready_in_both()
        procs = [subprocess.Popen([sys.executable, str(ERGON), "dispatch"], cwd=where, env=dict(env, FAKE_SLEEP="1"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for where, env in ((self.a, self.env_a), (self.b, self.env_b))]
        outs = [p.communicate(timeout=120) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, out + err)
        self.assertEqual(self.workers(), ["T-001"], outs)
        self.assertEqual(sum("doing    T-001" in out for out, _ in outs), 1, outs)
        for where in (self.a, self.b):
            self.ergon(where, "sync")
        self.assertEqual(self.status(self.a, "T-001"), "done")
        self.assertEqual(self.status(self.b, "T-001"), "done")

    def test_watch_picks_up_a_teammates_approval_after_a_sync(self):
        self.start()
        p = subprocess.Popen([sys.executable, str(ERGON), "dispatch", "--watch", "--budget-total", "5",
                              "--interval", "0.5", "--max", "1"], cwd=self.a, env=self.env_a,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(2)  # a few scans with nothing ready
            self.assertIsNone(p.poll())
            self.assertEqual(self.workers(), [])
            self.ergon(self.b, "approve", "T-001", inp="y\n", env=self.env_b)
            self.ergon(self.b, "sync", env=self.env_b)
            out, err = p.communicate(timeout=60)  # --max 1: it stops once T-001 is through
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("pulled: T-001 proposed->ready", out)
        self.assertIn("doing    T-001", out)
        self.assertEqual(self.workers(), ["T-001"])
        self.assertEqual(self.status(self.a, "T-001"), "done")
        self.ergon(self.b, "sync", env=self.env_b)
        self.assertEqual(self.status(self.b, "T-001"), "done")  # pushed as it landed

    def test_a_review_hold_names_its_clone_for_check_and_accept(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "accept=yes")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        r = self.ergon(self.a, "dispatch", env=self.env_a)
        self.assertIn("review   T-001", r.stdout)
        self.assertIn(f"(held: ann@{self.host})", self.task(self.a, "T-001").read_text())
        self.ergon(self.b, "sync", env=self.env_b)  # A pushed the hold as it stopped
        self.assertEqual(self.status(self.b, "T-001"), "review")
        # B has no task/T-001 branch, and isn't meant to: its check passes
        r = self.ergon(self.b, "check", check=False, env=self.env_b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # B's accept refuses and says where the branch is
        r = self.ergon(self.b, "accept", "T-001", check=False, inp="y\n", env=self.env_b)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"T-001 is held on ann@{self.host}: its branch task/T-001 is in that clone", r.stderr)
        self.assertEqual(self.status(self.b, "T-001"), "review")
        # the holding clone still checks its own branch
        self.assertEqual(self.ergon(self.a, "check", check=False, env=self.env_a).returncode, 0)
        sh("git", "worktree", "remove", "--force", str(self.a / ".claude" / "worktrees" / "T-001"), cwd=self.a)
        sh("git", "branch", "-D", "task/T-001", cwd=self.a)
        r = self.ergon(self.a, "check", check=False, env=self.env_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("in review but branch task/T-001 is gone", r.stdout)
        self.assertEqual(self.ergon(self.b, "check", check=False, env=self.env_b).returncode, 0)

    def test_a_bugfix_follow_up_stays_proposed_in_shared_mode(self):
        # T-136: a parent's signature can't vouch for a task that didn't exist when it was signed
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        env = dict(self.env_a, FAKE_FOUND="Fix X — y breaks\nTOUCHES: a.py\nCATEGORY: bugfix")
        r = self.ergon(self.a, "dispatch", env=env)
        self.assertEqual(self.status(self.a, "T-001"), "done", r.stdout)
        text = self.task(self.a, "T-002").read_text()
        self.assertEqual(self.status(self.a, "T-002"), "proposed", text)
        self.assertIn("category: bugfix\n", text)
        self.assertRegex(text, r"(?m)^approved:\s*$")
        self.assertIn("proposed T-002  (found by T-001)", r.stdout)

    def test_accept_lands_on_the_holding_clone(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "accept=yes")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "dispatch", env=self.env_a)
        r = self.ergon(self.a, "accept", "T-001", inp="y\n", env=self.env_a)
        self.assertIn("done     T-001", r.stdout)
        self.assertEqual(self.status(self.a, "T-001"), "done")

    def test_refused_in_an_agent_shell(self):
        self.ready_in_both()
        agent = dict(self.env_a, CLAUDECODE="1")
        r = self.ergon(self.a, "dispatch", check=False, env=agent)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("agent shell", r.stderr)
        self.assertEqual(self.workers(), [])
        self.assertEqual(self.status(self.a, "T-001"), "ready")
        self.assertIn("would run  T-001", self.ergon(self.a, "dispatch", "--dry-run", env=agent).stdout)

    # --- accept / reject / close sync before and after (T-064)
    def remote_status(self, tid):
        names = sh("git", "ls-tree", "-r", "--name-only", "ergon-state", cwd=self.remote).stdout.split()
        f = next(n for n in names if f"/{tid}-" in "/" + n or n.startswith(f"{tid}-"))
        text = sh("git", "show", f"ergon-state:{f}", cwd=self.remote).stdout
        return re.search(r"^status: (\S+)", text, re.M).group(1)

    def held_in_review(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "accept=yes")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "dispatch", env=self.env_a)
        self.assertEqual(self.status(self.a, "T-001"), "review")

    def test_accept_publishes_straight_away(self):
        self.held_in_review()
        self.ergon(self.a, "accept", "T-001", inp="y\n", env=self.env_a)
        self.assertEqual(self.remote_status("T-001"), "done")

    def test_reject_and_close_publish_straight_away(self):
        self.start()
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        self.ergon(self.a, "sync")
        self.ergon(self.a, "reject", "T-002", inp="y\n", env=self.env_a)
        self.assertEqual(self.remote_status("T-002"), "dropped")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "close", "T-001", "--as", "done", "--reason", "x", inp="y\n", env=self.env_a)
        self.assertEqual(self.remote_status("T-001"), "done")

    def test_reject_skips_a_task_another_clone_already_closed(self):
        self.start()
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        # B drops it before A (not yet synced) tries to
        self.ergon(self.b, "reject", "T-002", inp="y\n", env=self.env_b)
        self.assertEqual(self.remote_status("T-002"), "dropped")
        r = self.ergon(self.a, "reject", "T-002", check=False, inp="y\n", env=self.env_a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing in `proposed` or `review`", r.stderr + r.stdout)
        self.assertEqual(self.status(self.a, "T-002"), "dropped")

    def test_close_reports_a_task_another_clone_already_closed(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.ergon(self.b, "close", "T-001", "--as", "done", "--reason", "x", inp="y\n", env=self.env_b)
        r = self.ergon(self.a, "close", "T-001", "--as", "dropped", "--reason", "y", check=False, inp="y\n", env=self.env_a)
        self.assertIn("skip T-001: already done", r.stdout)
        self.assertEqual(self.status(self.a, "T-001"), "done")

    def test_each_still_commits_and_warns_with_the_remote_unreachable(self):
        self.held_in_review()
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        self.ergon(self.a, "new", "task", "Three", "--tier", "haiku")
        self.ergon(self.a, "approve", "T-003", inp="y\n")
        shutil.move(str(self.remote), str(self.remote) + ".gone")
        for args in (("accept", "T-001"), ("reject", "T-002"),
                     ("close", "T-003", "--as", "dropped", "--reason", "x")):
            r = self.ergon(self.a, *args, inp="y\n", env=self.env_a)
            self.assertIn("run `ergon sync` to publish", r.stdout, args)
            self.assertIn("couldn't sync", r.stdout, args)
        self.assertEqual(self.status(self.a, "T-001"), "done")
        self.assertEqual(self.status(self.a, "T-002"), "dropped")
        self.assertEqual(self.status(self.a, "T-003"), "dropped")


BREAKDOWN_ONE = """TASK: Add the schema
tier: haiku
tier_why: the exact change is written below
touches: db/schema.sql
depends:
## What
Add a `widgets` table to db/schema.sql:1.
## Done when
- `widgets` is in db/schema.sql
END TASK
RESULT: done
"""


class TestBreakdownClaims(Shared):
    """Dispatch claims an epic's breakdown by pushing a `breakdown:` field on the epic before the
    agent launches, and takes it off when the breakdown ends (T-130). Two clones, each a person."""

    def setUp(self):
        super().setUp()
        base = Path(self.tmp.name)
        bindir = base / "bin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        self.calls = base / "breakdowns.jsonl"
        common = dict(ERGON_ASK_STDIN="1", PATH=f"{bindir}:{os.environ['PATH']}",
                      FAKE_BREAKDOWN=BREAKDOWN_ONE, FAKE_BREAKDOWN_SEEN=str(self.calls))
        self.env_a = clean_env(USER="ann", LOGNAME="ann", **common)
        self.env_b = clean_env(USER="dana", LOGNAME="dana", **common)
        self.host = socket.gethostname().split(".")[0]

    def breakdowns(self):
        """The checkout each breakdown agent ran in, from either clone."""
        if not self.calls.exists():
            return []
        return [json.loads(l)["cwd"] for l in self.calls.read_text().splitlines() if l.strip()]

    def epic(self, where):
        return next((where / "tasks" / "epics").glob("E-001-*/EPIC.md")).read_text()

    def remote_epic(self):
        names = sh("git", "ls-tree", "-r", "--name-only", "ergon-state", cwd=self.remote).stdout.split()
        return sh("git", "show", f"ergon-state:{next(n for n in names if n.endswith('/EPIC.md'))}",
                  cwd=self.remote).stdout

    def remote_tasks(self):
        names = sh("git", "ls-tree", "-r", "--name-only", "ergon-state", cwd=self.remote).stdout.split()
        return sorted(re.search(r"(T-\d+)", n).group(1) for n in names if re.search(r"/T-\d+-", n))

    def empty_epic_in_both(self):
        """A files epic E-001, publishes it and approves it; B joins. It has no tasks."""
        self.ergon(self.a, "new", "epic", "Widgets", env=self.env_a)
        f = next((self.a / "tasks" / "epics").glob("E-001-*/EPIC.md"))
        f.write_text(f.read_text().replace("## Goal\n", "## Goal\nShip widgets end to end.\n"))
        self.ergon(self.a, "sync", "--init", inp="y\n", env=self.env_a)
        self.assertIn("approved 1", self.ergon(self.a, "approve", "E-001", inp="y\n", env=self.env_a).stdout)
        self.ergon(self.a, "sync", env=self.env_a)
        self.ergon(self.b, "sync", "--join", env=self.env_b)
        self.assertIn("status: open", self.epic(self.b))

    def test_two_dispatchers_racing_for_one_empty_epic_run_one_breakdown(self):
        self.empty_epic_in_both()
        # A's whole dispatch (claim, breakdown, filing, release) runs between B's fetch and B's push
        # of its claim. Without a claim B would push nothing before its own breakdown, so A's
        # dispatch would run in the middle of B's filing and break the epic down a second time.
        mark, a_out = Path(self.tmp.name) / "raced", Path(self.tmp.name) / "a-dispatch.txt"
        hook = self.b / ".git" / "hooks" / "pre-push"
        hook.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            [ -e "{mark}" ] && exit 0
            touch "{mark}"
            unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR GIT_OBJECT_DIRECTORY
            cd "{self.a}" && USER=ann LOGNAME=ann "{sys.executable}" "{ERGON}" dispatch > "{a_out}" 2>&1
            exit 0
            """))
        hook.chmod(0o755)
        r = self.ergon(self.b, "dispatch", env=self.env_b)
        self.assertTrue(mark.exists())
        a = a_out.read_text()
        self.assertIn("breakdown E-001  read-only in", a)
        self.assertIn("proposed T-001  (breakdown E-001)  Add the schema", a)
        self.assertEqual(self.breakdowns(), [str(self.a)])  # one breakdown: A's
        self.assertIn("push rejected", r.stdout)
        self.assertIn("claimed  breakdown E-001  E-001 got tasks from another clone; claim released, not started",
                      r.stdout)
        self.assertNotIn("read-only in", r.stdout)
        self.assertEqual(self.remote_tasks(), ["T-001"])
        # both claims went out and came off again: the remote holds none
        self.assertNotIn("\nbreakdown:", self.remote_epic())
        self.assertNotIn("\nbreakdown:", self.epic(self.b))
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.b / "tasks").stdout.strip(), self.remote_tip())
        log = sh("git", "log", "--format=%B", "ergon-state", cwd=self.remote).stdout
        self.assertIn(f"Breakdown of E-001 claimed by ann@{self.host} in {self.a}", log)
        self.assertIn(f"Breakdown of E-001 claimed by dana@{self.host} in {self.b}", log)

    def test_a_second_clone_that_sees_the_claim_leaves_the_epic_alone(self):
        self.empty_epic_in_both()
        p = subprocess.Popen([sys.executable, str(ERGON), "dispatch"], cwd=self.a,
                             env=dict(self.env_a, FAKE_SLEEP="4"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        try:
            for _ in range(200):  # until A's claim is on the remote (its breakdown is running then)
                if "\nbreakdown: ann@" in self.remote_epic():
                    break
                time.sleep(0.05)
            claim = re.search(r"^breakdown: (.*)$", self.remote_epic(), re.M).group(1)
            self.assertRegex(claim, rf"^ann@{re.escape(self.host)} until \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
                                    r"\([0-9a-f]{8}\)$")
            r = self.ergon(self.b, "dispatch", env=self.env_b)
            self.assertIn(f"claimed  breakdown E-001  another dispatcher holds it (ann@{self.host} until ", r.stdout)
            self.assertNotIn("read-only in", r.stdout)
            dry = self.ergon(self.b, "dispatch", "--dry-run", env=self.env_b).stdout
            self.assertIn("claimed  breakdown E-001  another dispatcher holds it", dry)
            self.assertNotIn("would run  breakdown", dry)
            out, err = p.communicate(timeout=120)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(p.returncode, 0, out + err)
        self.assertIn("proposed T-001  (breakdown E-001)", out)
        self.assertEqual(self.breakdowns(), [str(self.a)])
        self.assertNotIn("\nbreakdown:", self.remote_epic())  # released as it ended, and pushed
        self.assertEqual(self.remote_tasks(), ["T-001"])

    def test_a_claim_that_cant_be_pushed_launches_nothing(self):
        self.empty_epic_in_both()
        hook = self.b / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'no pushing today' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = self.remote_tip()
        r = self.ergon(self.b, "dispatch", env=self.env_b)
        self.assertIn("claimed  breakdown E-001  couldn't push the claim, so it's dropped and the breakdown "
                      "not started", r.stdout)
        self.assertEqual(self.breakdowns(), [])  # no agent, so no cost
        self.assertNotIn("breakdown(s), $", r.stdout)
        self.assertNotIn("\nbreakdown:", self.epic(self.b))
        self.assertEqual(self.remote_tip(), before)
        hook.unlink()  # it never ran here, so the next dispatch breaks it down
        r = self.ergon(self.b, "dispatch", env=self.env_b)
        self.assertIn("proposed T-001  (breakdown E-001)", r.stdout)
        self.assertEqual(self.breakdowns(), [str(self.b)])
        self.assertNotIn("\nbreakdown:", self.remote_epic())

    def test_a_failed_breakdown_releases_its_claim_and_a_lapsed_claim_holds_nothing(self):
        self.empty_epic_in_both()
        blocked = dict(self.env_a, FAKE_BREAKDOWN="nothing\nRESULT: blocked — say where widgets live")
        r = self.ergon(self.a, "dispatch", env=blocked)
        self.assertIn("breakdown E-001 blocked: say where widgets live", r.stdout)
        self.assertNotIn("\nbreakdown:", self.remote_epic())  # a failed run doesn't hold the epic
        self.ergon(self.a, "dispatch", env=self.env_a)  # A's own history row: A doesn't run it again
        self.assertEqual(self.breakdowns(), [str(self.a)])
        # a dispatcher that died mid-run left a claim; past its deadline it holds nothing
        f = next((self.a / "tasks" / "epics").glob("E-001-*/EPIC.md"))
        f.write_text(f.read_text().replace("\n---\n", "\nbreakdown: ann@gone until 2020-01-01T00:00:00Z (0badc0de)\n---\n", 1))
        self.ergon(self.a, "sync", env=self.env_a)
        self.assertIn("breakdown: ann@gone", self.remote_epic())
        r = self.ergon(self.b, "dispatch", env=self.env_b)
        self.assertIn("proposed T-001  (breakdown E-001)", r.stdout)
        self.assertEqual(self.breakdowns(), [str(self.a), str(self.b)])
        self.assertNotIn("\nbreakdown:", self.remote_epic())


class TestSignedApprovals(Shared):
    """Approvals are SSH-signed state commits, verified against the approvers file on the remote's
    trunk (T-020). Each failing case is left out by `next`, reported by `check` and skipped by
    dispatch."""

    def digest(self, where, tid):
        from ergon import tasks as T
        return T.brief_digest(T.parse_text(self.task(where, tid).read_text()))

    def edit(self, where, tid, old, new):
        f = self.task(where, tid)
        self.assertIn(old, f.read_text())
        f.write_text(f.read_text().replace(old, new, 1))

    def counts(self, where, tid):
        """next lists tid, check passes and dispatch would run it."""
        self.assertIn(f"  {tid}  ", self.ergon(where, "next").stdout)
        r = self.ergon(where, "check", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"would run  {tid}", self.ergon(where, "dispatch", "--dry-run").stdout)

    def doesnt_count(self, where, tid, why):
        """next leaves tid out, check reports it with `why`, dispatch skips it."""
        r = self.ergon(where, "next")
        self.assertNotIn(f"  {tid}  ", r.stdout)
        self.assertIn(f"left out because their approval doesn't verify: {tid}", r.stdout)
        r = self.ergon(where, "check", check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"{tid}: ready, but its approval doesn't verify: {why}", r.stdout)
        r = self.ergon(where, "dispatch", "--dry-run")
        self.assertNotIn(f"would run  {tid}", r.stdout)
        self.assertIn(f"skipped  {tid}  approval doesn't verify: {why}", r.stdout)

    def signed_by_hand(self, where, tid, key):
        """A stamp plus a commit signed with `key` whose trailer approves tid's current brief."""
        stamp(self.task(where, tid))
        sh("git", "-C", "tasks", "-c", "gpg.format=ssh", "-c", f"user.signingkey={self.keys / key}",
           "commit", "-qa", "-S", "--no-verify", "-m",
           f"approve by hand\n\nErgon-Approve: {tid} sha256:{self.digest(where, tid)}", cwd=where)

    def head(self, where, fmt="%B"):
        return sh("git", "-C", "tasks", "log", "-1", f"--format={fmt}", cwd=where).stdout

    def signed(self, where):
        return "-----BEGIN SSH SIGNATURE-----" in sh("git", "-C", "tasks", "cat-file", "commit", "HEAD", cwd=where).stdout

    def test_a_signed_approval_verifies_here_and_in_the_other_clone(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        d = self.digest(self.a, "T-001")
        self.assertRegex(self.task(self.a, "T-001").read_text(), rf"\napproved: Tester \d{{4}}-\d\d-\d\d sha256:{d[:8]}\n")
        self.assertIn(f"\n\nErgon-Approve: T-001 sha256:{d}\n", self.head(self.a))
        self.assertTrue(self.signed(self.a))
        self.counts(self.a, "T-001")
        self.ergon(self.a, "sync")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("restamped", r.stdout)  # the stamp approve wrote is what verifies
        self.counts(self.b, "T-001")

    def test_a_stamp_written_by_hand_doesnt_count(self):
        self.start()
        stamp(self.task(self.a, "T-001"))
        self.doesnt_count(self.a, "T-001", "no signed approval")
        # approved: is a copy of what verifies: sync clears it, and the other clone doesn't run it
        r = self.ergon(self.a, "sync")
        self.assertIn("restamped: T-001 approved: cleared (no signed approval", r.stdout)
        self.assertIn("\napproved:\n", self.task(self.a, "T-001").read_text())
        self.ergon(self.b, "sync")
        self.doesnt_count(self.b, "T-001", "no signed approval")

    def test_an_approval_signed_by_an_unlisted_key_doesnt_count(self):
        self.start()
        self.signing(self.a, "x")
        r = self.ergon(self.a, "approve", "T-001", check=False, inp="y\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't in .ergon/approvers", r.stderr)
        self.assertIn("status: proposed", self.task(self.a, "T-001").read_text())
        self.signed_by_hand(self.a, "T-001", "x")  # what approve refused to make
        self.doesnt_count(self.a, "T-001", "approval not signed by an approver")

    def test_an_approval_of_a_brief_edited_afterwards_doesnt_count(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.edit(self.a, "T-001", "## What\n", "## What\nAlso drop the production database.\n")
        self.doesnt_count(self.a, "T-001", "brief changed after approval")
        r = self.ergon(self.a, "approve", "T-001", inp="y\n")  # approving it again makes it count; it stays ready
        self.assertIn("(approve again: brief changed after approval", r.stdout)
        self.counts(self.a, "T-001")
        # an edit made on the other clone, merged in by sync, is caught the same way
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.ergon(self.b, "set", "T-001", "tier=opus")
        r = self.ergon(self.b, "sync")
        self.assertIn("restamped: T-001 approved: cleared (brief changed after approval", r.stdout)
        self.assertIn("tier: opus", self.task(self.b, "T-001").read_text())
        self.ergon(self.a, "sync")
        self.doesnt_count(self.a, "T-001", "brief changed after approval")

    def test_an_epic_brief_approval_is_signed_and_editing_its_goal_unapproves_it(self):
        self.start()
        agent = clean_env(CLAUDECODE="1")
        self.ergon(self.a, "new", "epic", "Big", env=agent)
        self.ergon(self.a, "new", "task", "Part", "--tier", "haiku", "--epic", "E-001", env=agent)
        epic = next((self.a / "tasks" / "epics").glob("E-001-*/EPIC.md"))
        epic.write_text(epic.read_text().replace("## Goal\n", "## Goal\nShip the big thing.\n"))
        self.ergon(self.a, "approve", "T-002", inp="y\n")  # the task, but not its epic's brief
        r = self.ergon(self.a, "next")
        self.assertNotIn("  T-002  ", r.stdout)
        self.assertIn("left out because epic E-001 is proposed", r.stdout)
        self.assertIn("skipped  T-002  epic E-001 is proposed", self.ergon(self.a, "dispatch", "--dry-run").stdout)
        r = self.ergon(self.a, "approve", "E-001", inp="y\n")
        self.assertIn("approved 1", r.stdout)
        from ergon import tasks as T
        d = T.brief_digest(T.parse(epic))
        self.assertIn(f"\n\nErgon-Approve: E-001 sha256:{d}\n", self.head(self.a))
        self.assertTrue(self.signed(self.a))
        self.assertRegex(epic.read_text(), rf"\nstatus: open\n(.*\n)*approved: Tester \d{{4}}-\d\d-\d\d sha256:{d[:8]}\n")
        self.counts(self.a, "T-002")
        # Notes and status aren't the brief; the Goal is
        epic.write_text(epic.read_text() + "\n## Notes\n- a note\n")
        self.counts(self.a, "T-002")
        epic.write_text(epic.read_text().replace("Ship the big thing.", "Also drop the production database."))
        r = self.ergon(self.a, "next")
        self.assertNotIn("  T-002  ", r.stdout)
        self.assertIn("left out because epic E-001's approval doesn't verify: brief changed after approval", r.stdout)
        r = self.ergon(self.a, "check", check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("E-001: open, but its approval doesn't verify: brief changed after approval", r.stdout)
        r = self.ergon(self.a, "dispatch", "--dry-run")
        self.assertIn("skipped  T-002  epic E-001's approval doesn't verify: brief changed", r.stdout)
        # sync leaves an epic's stamp alone: what verifies decides, stamp or no stamp
        self.assertNotIn("restamped: E-001", self.ergon(self.a, "sync").stdout)
        self.assertIn("sha256:", epic.read_text())
        r = self.ergon(self.a, "approve", "E-001", inp="y\n")  # approving it again makes it count
        self.assertIn("(approve again: brief changed after approval", r.stdout)
        self.counts(self.a, "T-002")
        # and it verifies in the other clone too
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.counts(self.b, "T-002")

    def test_an_epic_stamp_written_by_hand_doesnt_count(self):
        self.start()
        self.ergon(self.a, "new", "epic", "Big")
        self.ergon(self.a, "new", "task", "Part", "--tier", "haiku", "--epic", "E-001")
        self.ergon(self.a, "approve", "T-002", inp="y\n")
        epic = next((self.a / "tasks" / "epics").glob("E-001-*/EPIC.md"))
        epic.write_text(epic.read_text().replace("status: proposed", "status: open")
                        .replace("approved:", "approved: Tester 2026-01-01"))
        r = self.ergon(self.a, "check", check=False)
        self.assertIn("E-001: open, but its approval doesn't verify: no signed approval", r.stdout)
        self.assertNotIn("  T-002  ", self.ergon(self.a, "next").stdout)

    def test_an_open_epic_with_no_stamp_doesnt_count_until_a_human_approves_it(self):
        # an epic from before epics were approved, or one a clone without the guard pushed (T-124)
        self.start()
        home = self.b / "tasks" / "epics" / "E-001-old"
        home.mkdir(parents=True)
        epic = home / "EPIC.md"
        epic.write_text("---\nid: E-001\ntitle: Old\nstatus: open\ncreated: 2026-01-01\n---\n\n## Goal\nG\n")
        self.ergon(self.b, "set", "T-001", "epic=E-001")
        self.ergon(self.b, "approve", "T-001", inp="y\n")  # the task's own approval verifies
        self.ergon(self.b, "sync")
        self.ergon(self.a, "sync")
        for where in (self.a, self.b):
            why = "epic E-001's approval doesn't verify: no signed approval"
            r = self.ergon(where, "next")
            self.assertNotIn("  T-001  ", r.stdout)
            self.assertIn(f"left out because {why}", r.stdout)
            r = self.ergon(where, "check", check=False)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("E-001: open, but its approval doesn't verify: no signed approval", r.stdout)
            r = self.ergon(where, "dispatch", "--dry-run")
            self.assertNotIn("would run  T-001", r.stdout)
            self.assertIn(f"skipped  T-001  {why}", r.stdout)
        # `approve E-001` approves its brief again, with a signed stamp that verifies
        from ergon import tasks as T
        self.assertIn("(approve again: no signed approval", self.ergon(self.a, "approve", "E-001", inp="y\n").stdout)
        epic = next((self.a / "tasks" / "epics").glob("E-001-*/EPIC.md"))
        d = T.brief_digest(T.parse(epic))
        self.assertIn(f"\n\nErgon-Approve: E-001 sha256:{d}\n", self.head(self.a))
        self.assertTrue(self.signed(self.a))
        self.assertRegex(epic.read_text(), rf"\nstatus: open\n(.*\n)*approved: Tester \d{{4}}-\d\d-\d\d sha256:{d[:8]}\n")
        self.counts(self.a, "T-001")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "sync")
        self.counts(self.b, "T-001")

    def test_brief_due_for_an_open_epic_with_no_stamp_only_in_shared_mode(self):
        from ergon import gates as G

        class Apr:  # approvals as gitops.approvals() gives them: nothing for E-001 verifies
            def problem(self, e):
                return "no signed approval"

        old = {"id": "E-001", "status": "open", "approved": ""}
        self.assertTrue(G.brief_due(old, Apr()))
        self.assertFalse(G.brief_due(old, None))  # local mode: the exemption stays
        self.assertFalse(G.brief_due(dict(old, status="done"), Apr()))
        from ergon import tasks as T
        self.assertIn("no signed approval", T.epic_problem(old, Apr()))
        self.assertIsNone(T.epic_problem(old, None))
        self.assertIsNone(T.epic_problem(dict(old, status="done"), Apr()))

    def test_an_approvers_file_only_in_the_working_tree_doesnt_count(self):
        self.start()
        # the remote's trunk loses the file; A still has it checked out, and committed on its own main
        sh("git", "rm", "-q", ".ergon/approvers", cwd=self.b)
        sh("git", "commit", "-qm", "no approvers", cwd=self.b)
        sh("git", "push", "-q", str(self.remote), "main", cwd=self.b)
        self.assertTrue((self.a / ".ergon" / "approvers").exists())
        r = self.ergon(self.a, "approve", "T-001", check=False, inp="y\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("there is no approvers file .ergon/approvers on", r.stderr)
        self.assertIn("status: proposed", self.task(self.a, "T-001").read_text())
        self.signed_by_hand(self.a, "T-001", "a")  # a listed key, but listed only here
        self.doesnt_count(self.a, "T-001", "no approvers file .ergon/approvers on")
        # sync changes no stamp while nothing can verify, and says so
        r = self.ergon(self.a, "sync")
        self.assertIn("approvals not checked: no approvers file", r.stdout)
        self.assertIn("approved: Tester 2026-01-01", self.task(self.a, "T-001").read_text())

    def test_notes_status_and_depends_dont_invalidate_an_approval(self):
        self.start()
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        before = self.digest(self.a, "T-001")
        self.edit(self.a, "T-001", "## Notes\n", "## Notes\n- 2026-11-17 dispatch: a note\n")
        self.ergon(self.a, "set", "T-001", "status=blocked")
        self.ergon(self.a, "set", "T-001", "depends=T-002")
        r = self.ergon(self.a, "check", check=False)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.ergon(self.a, "set", "T-001", "status=ready")
        self.ergon(self.a, "set", "T-001", "depends=")
        self.assertEqual(self.digest(self.a, "T-001"), before)
        self.counts(self.a, "T-001")
        self.assertNotIn("restamped", self.ergon(self.a, "sync").stdout)

    def test_approve_refuses_without_signing_and_approves_nothing_when_signing_fails(self):
        self.start()
        bare = clean_env(ERGON_ASK_STDIN="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        sh("git", "config", "--unset", "gpg.format", cwd=self.a)
        r = self.ergon(self.a, "approve", "T-001", check=False, env=bare, inp="y\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SSH commit signing isn't set up", r.stderr)
        self.assertIn("git config gpg.format ssh", r.stderr)
        self.assertIn("status: proposed", self.task(self.a, "T-001").read_text())
        # set up, but with a key git can't sign with
        self.signing(self.a, "a")
        sh("git", "config", "user.signingkey", str(self.keys / "gone"), cwd=self.a)
        tip = self.head(self.a, "%H")
        r = self.ergon(self.a, "approve", "T-001", check=False, env=bare, inp="y\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("signing the state commit failed, so nothing was changed", r.stderr)
        self.assertIn("status: proposed", self.task(self.a, "T-001").read_text())
        self.assertEqual(self.head(self.a, "%H"), tip)
        self.assertEqual(sh("git", "-C", "tasks", "status", "--porcelain", cwd=self.a).stdout, "")

    def test_sync_rewrites_an_approved_stamp_edited_by_hand(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        good = re.search(r"\napproved: (.*)\n", self.task(self.a, "T-001").read_text()).group(1)
        self.edit(self.a, "T-001", f"approved: {good}", "approved: Mallory 2026-01-01 sha256:00000000")
        self.counts(self.a, "T-001")  # the stamp never counted for anything
        r = self.ergon(self.a, "sync")
        self.assertIn(f"restamped: T-001 approved: {good}", r.stdout)
        self.assertIn(f"\napproved: {good}\n", self.task(self.a, "T-001").read_text())

    def test_reject_and_close_are_signed_with_their_trailers(self):
        self.start()
        self.ergon(self.a, "new", "task", "Two", "--tier", "haiku")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.ergon(self.a, "close", "T-001", "--as", "dropped", "--reason", "moot", inp="y\n")
        self.assertIn("\n\nErgon-Close: T-001 dropped\n", self.head(self.a))
        self.assertTrue(self.signed(self.a))
        self.ergon(self.a, "reject", "T-002", inp="y\n")
        self.assertIn("\n\nErgon-Reject: T-002\n", self.head(self.a))
        self.assertTrue(self.signed(self.a))

    def test_agents_cant_move_the_ref_the_approvers_file_is_read_from(self):
        self.start()
        for cmd in ["git update-ref refs/ergon/trunk HEAD", "git fetch . main:refs/ergon/trunk"]:
            event = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.a)})
            out = self.ergon(self.a, "hook", "guard", inp=event).stdout
            self.assertIn("only ergon writes it", out, cmd)

    def commit_citing(self, where, tid):
        return sh("git", "commit", "--allow-empty", "-qm", f"Work ({tid})", cwd=where, check=False, env=self.env)

    def test_commit_msg_hook_checks_signed_approval(self):
        self.start()
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 0, r.stderr)
        # the brief changes after approval
        self.edit(self.a, "T-001", "## What\n", "## What\nAlso drop the production database.\n")
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("brief changed after approval", r.stderr)

    def test_commit_msg_hook_checks_the_epic(self):
        self.start()
        self.ergon(self.a, "new", "epic", "Big")
        self.ergon(self.a, "set", "T-001", "epic=E-001")
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("epic E-001 is proposed", r.stderr)
        self.assertIn("T-001", r.stderr)
        self.ergon(self.a, "approve", "E-001", inp="y\n")
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 0, r.stderr)
        # the epic's brief changes after approval: its stamp no longer verifies
        f = next((self.a / "tasks").rglob("EPIC.md"))
        f.write_text(f.read_text() + "\nAlso drop the production database.\n")
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("epic E-001's approval doesn't verify", r.stderr)

    def test_commit_msg_hook_refuses_a_hand_stamped_task(self):
        self.start()
        stamp(self.task(self.a, "T-001"))
        r = self.commit_citing(self.a, "T-001")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no signed approval", r.stderr)


class TestTwoPerson(Shared):
    """[[two_person]] (T-021): a matching task needs verified approvals of its current brief from
    two different principals in the approvers file before it leaves `proposed`."""

    RULES = '\n[[two_person]]\ntiers = ["opus"]\n\n[[two_person]]\ntouches = ["server/auth/**"]\n'

    counts, doesnt_count, digest = TestSignedApprovals.counts, TestSignedApprovals.doesnt_count, TestSignedApprovals.digest

    def setUp(self):
        super().setUp()
        # A's second key, listed under the same principal: one person, two keys
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "a2@example.com", "-f", str(self.keys / "a2"), cwd=self.a)
        with open(self.a / ".ergon" / "approvers", "a") as f:
            f.write(f'a@example.com namespaces="git" {(self.keys / "a2.pub").read_text().strip()}\n')
        sh("git", "commit", "-qam", "a's second key", cwd=self.a)
        sh("git", "push", "-q", str(self.remote), "main", cwd=self.a)
        for where in (self.a, self.b):
            with open(where / ".ergon.toml", "a") as f:
                f.write(self.RULES)
        sh("git", "config", "user.name", "Dana", cwd=self.b)
        self.env_b = clean_env(ERGON_ASK_STDIN="1", GIT_AUTHOR_NAME="Dana", GIT_COMMITTER_NAME="Dana")

    def as_b(self, *args, **kw):
        return self.ergon(self.b, *args, env=self.env_b, **kw)

    def text(self, where, tid="T-001"):
        return self.task(where, tid).read_text()

    def waits(self, where, tid, who):
        """tid is still proposed, unstamped, with a `1 of 2` Note, and nothing runs it."""
        text = self.text(where, tid)
        self.assertIn("status: proposed", text)
        self.assertIn("\napproved:\n", text)
        self.assertRegex(text, rf"\n- \d{{4}}-\d\d-\d\d approve: approved by {who} \(1 of 2\)\n")
        self.assertNotIn(f"  {tid}  ", self.ergon(where, "next").stdout)
        self.assertNotIn(f"would run  {tid}", self.ergon(where, "dispatch", "--dry-run").stdout)

    def test_one_principal_leaves_it_proposed_and_a_second_makes_it_ready(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "tier=opus")
        r = self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.assertIn("T-001: two-person rule, 1 of 2: Tester; needs another approver (not Tester); it stays proposed", r.stdout)
        self.assertIn("approved 0; 1 wait for another approver: T-001", r.stdout)
        self.assertIn("pushed ergon-state", r.stdout)  # published, so the other approvers see it
        self.waits(self.a, "T-001", "Tester")
        self.assertIn("two-person: 1 of 2: Tester; needs another approver (not Tester)", self.ergon(self.a, "review").stdout)
        # the same person again, then with their second key: still one principal
        self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.signing(self.a, "a2")
        r = self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.assertIn("1 of 2: Tester;", r.stdout)
        self.waits(self.a, "T-001", "Tester")
        self.assertEqual(len(re.findall(r"approved by Tester \(1 of 2\)", self.text(self.a, "T-001"))), 3)
        # a stamp written by hand on the proposed task doesn't let a commit cite it
        f = self.task(self.a, "T-001")
        f.write_text(f.read_text().replace("\napproved:\n", "\napproved: Tester 2026-01-01\n"))
        r = sh("git", "commit", "--allow-empty", "-m", "Work (T-001)", cwd=self.a, check=False, env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001: proposed, and no human has approved it", r.stderr)
        f.write_text(f.read_text().replace("\napproved: Tester 2026-01-01\n", "\napproved:\n"))
        # a second principal: approve syncs first, so B counts A's approvals
        r = self.as_b("approve", "T-001", inp="y\n")
        self.assertIn("(two-person: 1 of 2: Tester; needs another approver (not Tester))", r.stdout)
        self.assertIn("T-001: two-person rule, 2 of 2: Tester, Dana", r.stdout)
        self.assertIn("approved 1", r.stdout)
        d = self.digest(self.b, "T-001")
        self.assertRegex(self.text(self.b), rf"\nstatus: ready\n")
        self.assertRegex(self.text(self.b), rf"\napproved: Tester and Dana \d{{4}}-\d\d-\d\d sha256:{d[:8]}\n")
        self.counts(self.b, "T-001")
        self.assertEqual(sh("git", "commit", "--allow-empty", "-m", "Work (T-001)", cwd=self.b,
                            env=self.env_b, check=False).returncode, 0)
        r = self.ergon(self.a, "sync")
        self.assertNotIn("restamped", r.stdout)  # the stamp approve wrote is what verifies
        self.counts(self.a, "T-001")

    def test_retiering_into_a_rule_after_one_approval_needs_two_fresh_ones(self):
        self.start()
        # a task no rule matches needs one approval
        r = self.as_b("approve", "T-001", inp="y\n")
        self.assertIn("approved 1", r.stdout)
        self.assertIn("status: ready", self.text(self.b))
        self.counts(self.b, "T-001")
        self.ergon(self.a, "sync")
        self.ergon(self.a, "set", "T-001", "tier=opus")
        self.doesnt_count(self.a, "T-001", "brief changed after approval")
        # B's approval of the haiku brief doesn't count as one of the two
        r = self.ergon(self.a, "approve", "T-001", inp="y\n")
        self.assertIn("(approve again: brief changed after approval", r.stdout)
        self.assertIn("1 of 2: Tester;", r.stdout)
        self.waits(self.a, "T-001", "Tester")  # back to proposed, where the other approvers look
        self.assertEqual(self.ergon(self.a, "check", check=False).returncode, 0)
        r = self.as_b("approve", "T-001", inp="y\n")
        self.assertIn("2 of 2: Tester, Dana", r.stdout)
        self.assertIn("tier: opus", self.text(self.b))
        self.assertIn("status: ready", self.text(self.b))
        self.counts(self.b, "T-001")

    def test_a_touches_rule_matches_and_a_ready_task_with_one_approval_doesnt_run(self):
        self.start()
        self.ergon(self.a, "new", "task", "Keys", "--tier", "haiku", "--touches", "server/auth/keys.py")
        self.ergon(self.a, "approve", "T-001", "T-002", inp="y\n")
        self.assertIn("status: ready", self.text(self.a, "T-001"))
        self.waits(self.a, "T-002", "Tester")
        # approve syncs before its prompt here, so an agent shell is refused before anything is pushed
        tip = self.remote_tip()
        self.ergon(self.a, "new", "task", "Three", "--tier", "opus")
        r = self.ergon(self.a, "approve", "T-003", check=False, inp="y\n", env=clean_env(ERGON_ASK_STDIN="1", CLAUDECODE="1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused: running inside an agent shell", r.stderr)
        self.assertEqual(self.remote_tip(), tip)
        # moved to ready by hand with its one approval: nothing runs it, and check says why
        f = self.task(self.a, "T-002")
        f.write_text(f.read_text().replace("status: proposed", "status: ready"))
        self.doesnt_count(self.a, "T-002", "two-person rule: 1 of 2 approvals of this brief (Tester)")


class TestTwoPersonConfig(unittest.TestCase):
    def test_two_person_without_shared_is_a_config_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve() / "proj"
        git_init(root)
        env = clean_env()
        sh(sys.executable, str(ERGON), "init", cwd=root, env=env)
        with open(root / ".ergon.toml", "a") as f:
            f.write('\n[[two_person]]\ntiers = ["opus"]\n')
        r = sh(sys.executable, str(ERGON), "board", cwd=root, env=env, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("[[two_person]] needs [shared]", r.stderr)
        (root / ".ergon.toml").write_text((root / ".ergon.toml").read_text().replace('tiers = ["opus"]', 'tiers = ["gpt"]'))
        r = sh(sys.executable, str(ERGON), "board", cwd=root, env=env, check=False)
        self.assertIn("[[two_person]] #1 needs tiers", r.stderr)
