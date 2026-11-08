"""End-to-end tests: each one builds a throwaway git repo and drives the real CLI.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ERGON = Path(__file__).resolve().parent.parent / "ergon.py"
sys.path.insert(0, str(ERGON.parent))
import ergon  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stands in for `claude -p`: commits a change citing the task, in the task worktree and in
any companion worktree inside it, then reports RESULT: done."""
import json, os, re, subprocess, sys
prompt = sys.argv[-1]
tid = re.search(r"dispatched on (T-\d+)", prompt).group(1)
def commit(where, msg):
    with open(os.path.join(where, f"{tid}.txt"), "w") as f:
        f.write("work\n")
    subprocess.run(["git", "-C", where, "add", "-A"], check=True)
    subprocess.run(["git", "-C", where, "commit", "-qm", msg], check=True)
commit(".", f"Do the thing ({tid})")
pub = os.path.join("pub", "src")
if os.path.isdir(pub):
    commit(pub, os.environ.get("FAKE_PUB_MSG", "Do the public thing").replace("{id}", tid))
print(json.dumps({"result": "did it\nRESULT: done", "session_id": "s-1", "total_cost_usd": 0.01}))
'''


def sh(*cmd, cwd, check=True, env=None, inp=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, input=inp)
    if check and r.returncode:
        raise AssertionError(f"{cmd} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT", "CLAUDE_PROJECT_DIR")}
    env.update(GIT_AUTHOR_NAME="Tester", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="Tester", GIT_COMMITTER_EMAIL="t@example.com")
    env.update(extra)
    return env


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    sh("git", "init", "-q", "-b", "main", cwd=path)
    sh("git", "config", "user.name", "Tester", cwd=path)
    sh("git", "config", "user.email", "t@example.com", cwd=path)
    (path / "README.md").write_text("hi\n")
    sh("git", "add", "-A", cwd=path)
    sh("git", "commit", "-qm", "init", cwd=path)


def stamp(path):
    """What a human does by hand: status ready + an approved: stamp."""
    text = path.read_text()
    text = re.sub(r"^status: proposed$", "status: ready", text, flags=re.M)
    text = re.sub(r"^approved:.*$", "approved: Tester 2026-01-01", text, flags=re.M)
    path.write_text(text)


class Project(unittest.TestCase):
    config = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "proj"
        git_init(self.root)
        self.env = clean_env()
        self.ergon("init")
        if self.config:
            (self.root / ".ergon.toml").write_text(textwrap.dedent(self.config))
        sh("git", "add", "-A", cwd=self.root)
        sh("git", "commit", "-qm", "ergon", cwd=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def ergon(self, *args, cwd=None, check=True, env=None, inp=None):
        return sh(sys.executable, str(ERGON), *args, cwd=cwd or self.root, check=check,
                  env=env or self.env, inp=inp)

    def task_file(self, tid):
        hits = list((self.root / "tasks").rglob(f"{tid}-*.md"))
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    def status(self, tid):
        return re.search(r"^status: (\w+)", self.task_file(tid).read_text(), re.M).group(1)

    def guard(self, tool, tool_input, *extra, env=None):
        event = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(self.root)})
        out = self.ergon("hook", "guard", *extra, inp=event, env=env).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else None


class TestConfig(unittest.TestCase):
    def test_mini_toml_matches_tomllib(self):
        text = ergon.CONFIG_TEMPLATE.replace("# [[companion]]", "[[companion]]")
        text = re.sub(r"^# (name|path|trunk|forbid_tracker_ids|policy|map|log) ", r"\1 ", text, flags=re.M)
        text += 'tags = ["a", \'b\',]\n'
        mini = ergon.mini_toml(text)
        self.assertEqual(mini["companion"][0]["path"], ".product/src")
        self.assertIs(mini["companion"][0]["forbid_tracker_ids"], True)
        self.assertEqual(mini["companion"][0]["tags"], ["a", "b"])
        self.assertEqual(mini["trunk"], "main")
        try:
            import tomllib
        except ModuleNotFoundError:
            return
        self.assertEqual(mini, tomllib.loads(text))


class TestBasics(Project):
    def test_init_writes_config_and_gitignore(self):
        self.assertTrue((self.root / ".ergon.toml").exists())
        gi = (self.root / ".gitignore").read_text()
        for line in ("tasks/items/", "tasks/epics/", "tasks/runs/", ".claude/worktrees/"):
            self.assertIn(line, gi)
        self.ergon("init")  # idempotent
        self.assertEqual((self.root / ".gitignore").read_text(), gi)

    def test_unconfigured_repo(self):
        other = Path(self.tmp.name).resolve() / "other"
        git_init(other)
        r = self.ergon("next", cwd=other, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ergon init", r.stderr)
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ergon approve T-001"}, "cwd": str(other)})
        self.assertEqual(self.ergon("hook", "guard", cwd=other, inp=event).stdout, "")

    def test_approval_is_human_only(self):
        self.ergon("new", "task", "Fix it", "--tier", "sonnet")
        self.assertEqual(self.status("T-001"), "proposed")
        r = self.ergon("set", "T-001", "status=ready", check=False)
        self.assertIn("not allowed", r.stderr)
        r = self.ergon("set", "T-001", "approved=me", check=False)
        self.assertIn("human-only", r.stderr)
        r = self.ergon("approve", "T-001", check=False, env=clean_env(CLAUDECODE="1"))
        self.assertIn("agent shell", r.stderr)
        self.assertEqual(self.status("T-001"), "proposed")
        stamp(self.task_file("T-001"))
        self.ergon("check")
        self.ergon("set", "T-001", "status=doing")
        self.assertEqual(self.status("T-001"), "doing")

    def test_check_fails_on_unstamped_ready(self):
        self.ergon("new", "task", "Fix it", "--tier", "haiku")
        f = self.task_file("T-001")
        f.write_text(f.read_text().replace("status: proposed", "status: ready"))
        r = self.ergon("check", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("without a human `approved:` stamp", r.stdout)

    def test_guard(self):
        self.ergon("new", "task", "Fix it", "--tier", "haiku")
        f = str(self.task_file("T-001"))
        self.assertIn("human only", self.guard("Edit", {"file_path": f, "old_string": "approved:",
                                                        "new_string": "approved: me"}))
        self.assertIn("ergon set", self.guard("Edit", {"file_path": f, "old_string": "status: proposed",
                                                      "new_string": "status: ready"}))
        self.assertIsNone(self.guard("Edit", {"file_path": f, "old_string": "## What",
                                             "new_string": "## What\nmore"}))
        self.assertIn("human-only", self.guard("Bash", {"command": "ergon approve T-001"}))
        self.assertIn("human-only", self.guard("Bash", {"command": "python3 ergon.py close T-001 --as done"}))
        self.assertIn("CLAUDECODE", self.guard("Bash", {"command": "unset CLAUDECODE; ergon next"}))
        self.assertIn("shell", self.guard("Bash", {"command": f"sed -i '' s/a/b/ {f}"}))
        self.assertIsNone(self.guard("Bash", {"command": "cat tasks/items/*.md"}))

    def test_stop_hook_blocks_on_lint_failure(self):
        self.ergon("new", "task", "Fix it", "--tier", "haiku")
        f = self.task_file("T-001")
        f.write_text(f.read_text().replace("tier: haiku", "tier: wizard"))
        event = json.dumps({"cwd": str(self.root)})
        out = json.loads(self.ergon("hook", "stop", inp=event).stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("wizard", out["reason"])


class TestConfigOnABranch(unittest.TestCase):
    def test_worktree_config_before_it_merges(self):
        """Adopting ergon on a branch: the config exists only in that worktree, state is shared."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "proj"
            git_init(root)
            wt = Path(d).resolve() / "wt"
            sh("git", "worktree", "add", "-q", "-b", "adopt", str(wt), cwd=root)
            (wt / ".ergon.toml").write_text('state = "work"\n')
            env = clean_env()
            r = sh(sys.executable, str(ERGON), "new", "task", "Hi", "--tier", "haiku", cwd=wt, env=env)
            self.assertIn("work/items/T-001", r.stdout)
            self.assertTrue((root / "work" / "items").is_dir())  # state lives in the main checkout
            r = sh(sys.executable, str(ERGON), "check", cwd=root, env=env, check=False)
            self.assertIn("ergon init", r.stderr)  # main checkout has no config yet


class TestMapAndAudit(Project):
    config = '''
        trunk = "main"
        state = "tasks"
        map = "map"
        log = "LOG.md"
        retired = "retired.txt"
    '''

    def test_map_links_and_audit(self):
        self.ergon("new", "task", "Fix the widget", "--tier", "haiku", "--source", "LOG.md:99")
        (self.root / "map").mkdir()
        (self.root / "map" / "widget.md").write_text("- widget is broken → task T-001\n- gone → task T-009\n")
        (self.root / "LOG.md").write_text("one\ntwo\n")
        (self.root / "retired.txt").write_text("widget | - | replaced by the gadget\n")
        r = self.ergon("check", check=False)
        self.assertIn("map/widget.md:2: links missing task T-009", r.stdout)
        self.assertNotIn("widget.md:1", r.stdout)
        out = self.ergon("audit").stdout
        self.assertIn("T-001 mentions widget", out)
        self.assertIn("LOG.md has", out)


class TestEpicBranch(Project):
    def test_epic_lifecycle(self):
        self.ergon("new", "epic", "Big thing")
        self.ergon("new", "task", "Part one", "--tier", "sonnet", "--epic", "E-001")
        stamp(self.task_file("T-001"))
        r = self.ergon("set", "T-001", "status=doing", check=False)
        self.assertNotEqual(r.returncode, 0)
        wt = self.root / ".claude" / "worktrees" / "E-001"
        self.assertTrue(wt.is_dir())
        self.ergon("set", "T-001", "status=doing", cwd=wt)
        (wt / "part1.txt").write_text("x\n")
        sh("git", "add", "-A", cwd=wt)
        sh("git", "commit", "-qm", "Add part one (T-001)", cwd=wt)
        self.ergon("set", "T-001", "status=done", cwd=wt)
        log = sh("git", "log", "-1", "--format=%B", "main", cwd=self.root).stdout
        self.assertIn("Merge E-001: Big thing", log)
        self.assertIn("Add part one (T-001)", log)
        self.assertIn("status: done", (self.root / "tasks/epics").glob("E-001-*/EPIC.md").__next__().read_text())
        branches = sh("git", "branch", "--list", "epic/*", cwd=self.root).stdout
        self.assertEqual(branches.strip(), "")


class TestDispatch(Project):
    config = '''
        trunk = "main"
        worker_rules = "WORKER.md"

        [[companion]]
        name = "acme/public"
        path = "pub/src"
        trunk = "main"
        forbid_tracker_ids = true
    '''

    def setUp(self):
        super().setUp()
        git_init(self.root / "pub" / "src")
        with open(self.root / ".gitignore", "a") as f:
            f.write("pub/\n")
        (self.root / "WORKER.md").write_text("Never touch prod.\n")
        sh("git", "add", "-A", cwd=self.root)
        sh("git", "commit", "-qm", "setup", cwd=self.root)
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        self.env = clean_env(PATH=f"{bindir}:{os.environ['PATH']}")

    def test_dispatch_lands_on_both_sides(self):
        self.ergon("new", "task", "Private only", "--tier", "haiku")
        self.ergon("new", "task", "Touch public", "--tier", "sonnet", "--touches", "acme/public")
        for t in ("T-001", "T-002"):
            stamp(self.task_file(t))
        dry = self.ergon("dispatch", "--dry-run").stdout
        self.assertIn("T-002", dry)
        self.assertIn("+ acme/public main", dry)
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "done", out)
        self.assertEqual(self.status("T-002"), "done", out)
        self.assertTrue((self.root / "T-001.txt").exists())  # merged into the checked-out main
        self.assertTrue((self.root / "pub" / "src" / "T-002.txt").exists())
        pub_log = sh("git", "log", "--format=%B", "main", cwd=self.root / "pub" / "src").stdout
        self.assertNotRegex(pub_log, r"\bT-\d{3}")
        self.assertEqual(sh("git", "worktree", "list", cwd=self.root / "pub" / "src").stdout.count("\n"), 1)
        runs = list((self.root / "tasks" / "runs").glob("*.json"))
        self.assertEqual(len(runs), 2)

    def test_dry_run_from_a_worktree(self):
        self.ergon("new", "task", "Private only", "--tier", "haiku")
        stamp(self.task_file("T-001"))
        wt = Path(self.tmp.name).resolve() / "side"
        sh("git", "worktree", "add", "-q", "-b", "side", str(wt), cwd=self.root)
        self.assertIn("would run  T-001", self.ergon("dispatch", "--dry-run", cwd=wt).stdout)
        r = self.ergon("dispatch", cwd=wt, check=False)
        self.assertIn("main checkout", r.stderr)
        self.assertEqual(self.status("T-001"), "ready")

    def test_tracker_id_in_public_commit_blocks(self):
        self.ergon("new", "task", "Touch public", "--tier", "sonnet", "--touches", "acme/public")
        stamp(self.task_file("T-001"))
        self.env["FAKE_PUB_MSG"] = "Leak {id}"
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "blocked", out)
        self.assertIn("cites T-001", self.task_file("T-001").read_text())
        self.assertFalse((self.root / "pub" / "src" / "T-001.txt").exists())

    def test_worker_guard_protects_shared_checkout(self):
        shared = self.root / "pub" / "src" / "README.md"
        reason = self.guard("Write", {"file_path": str(shared), "content": "x"}, "--worker",
                            "--project", str(self.root))
        self.assertIn("shared acme/public checkout", reason)
        self.assertIsNone(self.guard("Write", {"file_path": str(shared), "content": "x"}))
        reason = self.guard("Bash", {"command": f"git -C {self.root}/pub/src commit -am x"}, "--worker")
        self.assertIn("shared", reason)

    def test_worker_prompt_carries_rules(self):
        self.ergon("new", "task", "Private only", "--tier", "haiku")
        stamp(self.task_file("T-001"))
        bindir = Path(self.tmp.name) / "bin"
        (bindir / "claude").write_text(f"#!{sys.executable}\nimport sys, json\n"
                                       f"open({str(Path(self.tmp.name) / 'prompt.txt')!r}, 'w').write(sys.argv[-1])\n"
                                       "print(json.dumps({'result': 'RESULT: blocked — testing'}))\n")
        self.ergon("dispatch")
        prompt = (Path(self.tmp.name) / "prompt.txt").read_text()
        self.assertIn("Never touch prod.", prompt)
        self.assertIn("doesn't name acme/public", prompt)
        self.assertEqual(self.status("T-001"), "blocked")


if __name__ == "__main__":
    unittest.main()
