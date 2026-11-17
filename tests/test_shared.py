"""Shared task state (T-016): two clones of one project and a bare remote for the state branch.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from test_ergon import ERGON, clean_env, git_init, sh, stamp

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
        sh("git", "add", "-A", cwd=self.a)
        sh("git", "commit", "-qm", "ergon", cwd=self.a)
        self.b = base / "b"
        sh("git", "clone", "-q", str(self.a), str(self.b), cwd=base)

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

    def test_both_sides_changing_one_task_stops_with_a_report(self):
        self.start()
        self.ergon(self.a, "set", "T-001", "title=From A")
        self.ergon(self.a, "sync")
        tip = self.remote_tip()
        self.ergon(self.b, "set", "T-001", "title=From B")
        self.ergon(self.b, "new", "task", "Other", "--tier", "haiku")
        r = self.ergon(self.b, "sync", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sync stopped", r.stderr)
        self.assertIn("T-001: here edited", r.stderr)
        self.assertNotIn("T-002", r.stderr)
        self.assertEqual(self.remote_tip(), tip)  # nothing pushed
        self.assertIn("title: From B", self.task(self.b, "T-001").read_text())  # nothing merged
        self.assertEqual(sh("git", "status", "--porcelain", cwd=self.b / "tasks").stdout, "")


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
