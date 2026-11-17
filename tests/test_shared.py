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
        self.ergon(self.b, "set", "T-001", "title=Edited on B")
        r = self.ergon(self.b, "sync")
        self.assertIn("dropped this clone's ready->blocked", r.stdout)
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("status: doing", text)
        self.assertIn("title: Edited on B", text)  # the rest of B's edit still merges
        self.assertIn("sync: dropped Tester's ready->blocked, remote had moved to doing", text)

    def test_a_status_move_the_remote_didnt_race_applies(self):
        self.approved_start()
        self.ergon(self.a, "set", "T-001", "title=Edited on A")
        self.ergon(self.a, "sync")
        self.ergon(self.b, "set", "T-001", "status=doing")
        r = self.ergon(self.b, "sync")
        self.assertNotIn("dropped", r.stdout)
        text = next(v for k, v in self.settle().items() if "/T-001-" in k)
        self.assertIn("status: doing", text)
        self.assertIn("title: Edited on A", text)

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
