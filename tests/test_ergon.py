"""End-to-end tests: each one builds a throwaway git repo and drives the real CLI.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path

ERGON = Path(__file__).resolve().parent.parent / "ergon"
sys.path.insert(0, str(ERGON.parent))
import ergon  # noqa: E402

FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Stands in for `claude -p`: commits a change citing the task, in the task worktree and in
any companion worktree inside it, then reports RESULT: done. As the task-review agent it
reports $FAKE_REVIEW (default REVIEW: pass) and appends its model and prompt to $FAKE_SEEN."""
import json, os, re, subprocess, sys, time
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
prompt = sys.argv[-1]
tier = sys.argv[sys.argv.index("--agent") + 1].replace("task-", "").upper()
if os.environ.get("FAKE_SEEN"):
    agents = json.loads(sys.argv[sys.argv.index("--agents") + 1])
    with open(os.environ["FAKE_SEEN"], "a") as f:
        f.write(json.dumps({"agent": tier, "model": agents["task-" + tier.lower()]["model"], "prompt": prompt}) + "\n")
if tier == "REVIEW":
    if os.environ.get("FAKE_REVIEW_COMMIT"):
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "sneaky"], check=True)
    print(json.dumps({"result": "looked\n" + os.environ.get("FAKE_REVIEW", "REVIEW: pass"),
                      "session_id": "r-1", "total_cost_usd": 0.02}))
    sys.exit()
tid = re.search(r"dispatched on (T-\d+)", prompt).group(1)
def commit(where, msg):
    with open(os.path.join(where, f"{tid}.txt"), "a") as f:
        f.write("work\n")
    subprocess.run(["git", "-C", where, "add", "-A"], check=True)
    subprocess.run(["git", "-C", where, "commit", "-qm", msg], check=True)
commit(".", f"Do the thing ({tid})")
pub = os.path.join("pub", "src")
if os.path.isdir(pub):
    commit(pub, os.environ.get("FAKE_PUB_MSG", "Do the public thing").replace("{id}", tid))
if os.environ.get("FAKE_DIRTY"):
    open(f"{tid}.txt", "a").write("more\n")
found = "".join(f"FOUND: {x}\n" for x in os.environ.get("FAKE_FOUND", "").split("|") if x)
print(json.dumps({"result": "did it\n" + found + os.environ.get("FAKE_RESULT_" + tier, os.environ.get("FAKE_RESULT", "RESULT: done")), "session_id": "s-1", "total_cost_usd": 0.01}))
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


class TestProtect(Project):
    config = """
        [[protect]]
        touches = ["server/auth/**", "prod-db"]
        min_tier = "opus"
        note = "keys"
    """

    def test_protect(self):
        self.ergon("new", "task", "A", "--tier", "sonnet", "--touches", "server/auth/login.py")
        self.ergon("new", "task", "B", "--tier", "opus", "--touches", "server/auth")
        self.ergon("new", "task", "C", "--tier", "haiku", "--touches", "prod-db")
        self.ergon("new", "task", "D", "--tier", "sonnet", "--touches", "web/app.py")
        r = self.ergon("check", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001: touches server/auth/login.py (protect server/auth/**) needs tier opus+", r.stdout)
        self.assertIn("T-003", r.stdout)
        self.assertNotIn("T-002", r.stdout)
        self.assertNotIn("T-004", r.stdout)
        for tid in ("T-001", "T-003"):
            f = self.task_file(tid)
            f.write_text(f.read_text().replace("status: proposed", "status: dropped"))
        self.ergon("check")


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



FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1:3] != ["issue", "view"]:
    sys.exit("fake gh: only issue view")
num = int(sys.argv[3])
if num == 404:
    sys.exit("GraphQL: Could not resolve to an issue")
print(json.dumps({"number": num, "title": f"Widget #{num} crashes on save", "state": "OPEN",
                  "url": f"https://github.com/acme/app/issues/{num}", "labels": [{"name": "bug"}],
                  "body": "Steps:\n## Repro\nclick save"}))
"""


class TestImport(Project):
    def setUp(self):
        super().setUp()
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        (bindir / "gh").write_text(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        (bindir / "gh").chmod(0o755)
        self.gh_log = Path(self.tmp.name) / "gh.log"
        self.env = clean_env(PATH=f"{bindir}:{os.environ['PATH']}", FAKE_GH_LOG=str(self.gh_log))

    def gh_calls(self):
        return [json.loads(l) for l in self.gh_log.read_text().splitlines()]

    def test_import_then_epic_finish_prints_never_runs_close(self):
        self.ergon("new", "epic", "Fix widgets")
        out = self.ergon("import", "gh#123", "--epic", "E-001").stdout
        self.assertIn("T-001, proposed, tier human", out)
        text = self.task_file("T-001").read_text()
        self.assertIn("title: Widget 123 crashes on save", text)  # ` #` would start a comment
        self.assertIn("source: gh#123", text)
        self.assertIn("status: proposed", text)
        self.assertRegex(text, r"(?m)^approved:$")
        self.assertIn("> ## Repro", text)  # quoted: not a section of the task file
        self.assertIn("Issue labels: bug", text)
        self.ergon("check")
        self.assertIn("already T-001", self.ergon("import", "gh#123", check=False).stderr)
        self.ergon("import", "gh:acme/other#7", "--tier", "haiku", "--epic", "E-001")
        self.assertIn("could not resolve", self.ergon("import", "gh#404", check=False).stderr.lower())
        self.assertIn("Linear", self.ergon("import", "linear:ABC-12", check=False).stderr)
        self.assertEqual(self.gh_calls()[0][:5], ["issue", "view", "123", "--json", "number,title,body,url,state,labels"])
        self.assertEqual(self.gh_calls()[1][:5], ["issue", "view", "7", "-R", "acme/other"])

        self.ergon("set", "T-001", "tier=sonnet")
        for tid in ("T-001", "T-002"):
            stamp(self.task_file(tid))
        self.ergon("set", "T-001", "status=doing", check=False)  # opens the epic worktree
        wt = self.root / ".claude" / "worktrees" / "E-001"
        for tid in ("T-001", "T-002"):
            self.ergon("set", tid, "status=doing", cwd=wt)
            (wt / f"{tid}.txt").write_text("x\n")
            sh("git", "add", "-A", cwd=wt)
            sh("git", "commit", "-qm", f"Fix it ({tid})", cwd=wt)
        self.assertNotIn("gh issue", self.ergon("set", "T-001", "status=done", cwd=wt).stdout)
        out = self.ergon("set", "T-002", "status=done", cwd=wt).stdout
        self.assertIn("E-001 done: merged", out)
        merge = sh("git", "rev-parse", "--short", "main", cwd=self.root).stdout.strip()
        self.assertIn(f"gh issue comment 123 --body 'Done in E-001 (merge {merge}): T-001 Widget 123 crashes on save'", out)
        self.assertIn("gh issue close 123\n", out)
        self.assertIn("gh issue close 7 -R acme/other", out)
        self.assertTrue(all(c[:2] == ["issue", "view"] for c in self.gh_calls()))  # printed, never run

    def test_import_without_gh(self):
        env = clean_env(PATH="/usr/bin:/bin")
        r = self.ergon("import", "gh#1", check=False, env=env)
        self.assertIn("needs the GitHub CLI", r.stderr)

class DispatchProject(Project):
    """A project with a companion checkout, worker rules and the fake `claude` on PATH."""
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


class TestDispatch(DispatchProject):
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

    def test_budget_total_stops_launches(self):
        for n in "ABC":
            self.ergon("new", "task", f"Job {n}", "--tier", "sonnet")
        for t in ("T-001", "T-002", "T-003"):
            stamp(self.task_file(t))
        dry = self.ergon("dispatch", "--dry-run", "--budget-total", "0.025", "--budget-pct", "80", "--budget", "1").stdout
        self.assertIn("cap        $0.02", dry)
        out = self.ergon("dispatch", "--jobs", "1", "--budget-total", "0.025", "--budget-pct", "80").stdout
        self.assertEqual(self.status("T-001"), "done", out)
        self.assertEqual(self.status("T-002"), "done", out)
        self.assertEqual(self.status("T-003"), "ready", out)
        self.assertIn("budget cap $0.02 reached", out)
        self.assertIn("Not launched: T-003", out)

    def watch(self, *extra):
        return subprocess.Popen([sys.executable, str(ERGON), "dispatch", "--watch", "--interval", "0.2", *extra],
                                cwd=self.root, env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)

    def wait_status(self, tid, want, proc, secs=30):
        import time
        end = time.time() + secs
        while time.time() < end:
            if self.status(tid) == want:
                return
            time.sleep(0.1)
        proc.kill()
        self.fail(f"{tid} never became {want}: {proc.communicate()[0]}")

    def test_watch_needs_budget_total(self):
        r = self.ergon("dispatch", "--watch", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--budget-total", r.stdout + r.stderr)

    def test_watch_picks_up_tasks_approved_mid_run_and_ends_at_cap(self):
        for n in "ABC":
            self.ergon("new", "task", f"Job {n}", "--tier", "sonnet")
        stamp(self.task_file("T-001"))
        p = self.watch("--budget-total", "0.025")
        self.wait_status("T-001", "done", p)
        stamp(self.task_file("T-002"))  # approved while the watch runs
        self.wait_status("T-002", "done", p)
        stamp(self.task_file("T-003"))
        out = p.communicate(timeout=30)[0]  # 3 x $0.01 reaches the cap: the watch ends by itself
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.status("T-003"), "done", out)
        self.assertIn("budget cap", out)

    def test_watch_ctrl_c_lets_workers_finish_and_second_kills(self):
        import signal, time
        for n in "AB":
            self.ergon("new", "task", f"Job {n}", "--tier", "sonnet")
            stamp(self.task_file("T-001" if n == "A" else "T-002"))
        self.env["FAKE_SLEEP"] = "3"
        p = self.watch("--budget-total", "5", "--jobs", "1")
        self.wait_status("T-001", "doing", p)
        p.send_signal(signal.SIGINT)
        out = p.communicate(timeout=30)[0]
        self.assertEqual(self.status("T-001"), "done", out)  # finished despite the interrupt
        self.assertEqual(self.status("T-002"), "ready", out)  # never launched
        self.setUp()
        self.ergon("new", "task", "Job A", "--tier", "sonnet")
        stamp(self.task_file("T-001"))
        self.env["FAKE_SLEEP"] = "20"
        p = self.watch("--budget-total", "5")
        self.wait_status("T-001", "doing", p)
        time.sleep(0.5)
        p.send_signal(signal.SIGINT)
        time.sleep(0.5)
        p.send_signal(signal.SIGINT)
        out = p.communicate(timeout=30)[0]
        self.assertEqual(self.status("T-001"), "blocked", out)

    def test_overlapping_touches_run_in_sequence(self):
        for touches in ("shared.py", "a.py"):
            with self.subTest(touches):
                self.setUp()
                self.ergon("new", "task", "One", "--tier", "sonnet", "--touches", "shared.py")
                self.ergon("new", "task", "Two", "--tier", "sonnet", "--touches", touches + ",x" if touches == "a.py" else touches)
                for t in ("T-001", "T-002"):
                    stamp(self.task_file(t))
                dry = self.ergon("dispatch", "--dry-run").stdout
                held = "held       T-002 behind T-001" in dry
                self.assertEqual(held, touches == "shared.py", dry)
                out = self.ergon("dispatch", "--jobs", "2").stdout
                self.assertEqual(self.status("T-002"), "done", out)
                order = [l.split()[0] + l.split()[1] for l in out.splitlines() if l.split()[:1] in (["doing"], ["done"])]
                if touches == "shared.py":
                    self.assertEqual(order, ["doingT-001", "doneT-001", "doingT-002", "doneT-002"], out)
                else:
                    self.assertEqual(order[:2], ["doingT-001", "doingT-002"], out)

    def test_found_lines_become_proposed_tasks(self):
        for res, want in (("RESULT: done", "done"), ("RESULT: blocked — nope", "blocked")):
            with self.subTest(res):
                self.setUp()
                self.ergon("new", "task", "Source", "--tier", "sonnet")
                stamp(self.task_file("T-001"))
                self.env.update(FAKE_FOUND="Fix alpha — it breaks|Fix beta — also", FAKE_RESULT=res)
                out = self.ergon("dispatch").stdout
                self.assertEqual(self.status("T-001"), want, out)
                for tid, title in (("T-002", "Fix alpha"), ("T-003", "Fix beta")):
                    text = self.task_file(tid).read_text()
                    self.assertEqual(self.status(tid), "proposed")
                    self.assertIn("source: T-001", text)
                    self.assertIn("tier: sonnet", text)
                    self.assertIn(f"title: {title}", text)
                self.assertIn("2 proposed", out)

    def test_escalate_retries_one_tier_up(self):
        blocked = "RESULT: blocked \u2014 too hard"
        cases = [("haiku", "--escalate", {"FAKE_RESULT_HAIKU": blocked}, "done", "sonnet", 2),
                 ("haiku", None, {"FAKE_RESULT_HAIKU": blocked}, "blocked", "haiku", 1),
                 ("opus", "--escalate", {"FAKE_RESULT_OPUS": blocked}, "blocked", "opus", 1),
                 ("haiku", "--escalate", {"FAKE_DIRTY": "1"}, "blocked", "haiku", 1)]
        for tier, flag, env, want, want_tier, runs in cases:
            with self.subTest(tier=tier, flag=flag, env=env):
                self.setUp()
                self.ergon("new", "task", "Hard", "--tier", tier)
                stamp(self.task_file("T-001"))
                self.env.update(env)
                out = self.ergon("dispatch", *([flag] if flag else [])).stdout
                self.assertEqual(self.status("T-001"), want, out)
                text = self.task_file("T-001").read_text()
                self.assertIn(f"tier: {want_tier}", text)
                self.assertEqual(len(list((self.root / "tasks" / "runs").glob("*.json"))), runs, out)
                self.assertEqual("escalated haiku\u2192sonnet: too hard" in text, want_tier == "sonnet")

    def test_worker_block_files_human_task(self):
        self.ergon("new", "task", "Source", "--tier", "sonnet")
        stamp(self.task_file("T-001"))
        self.env.update(FAKE_RESULT="RESULT: blocked \u2014 rotate the API key")
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "blocked", out)
        text = self.task_file("T-002").read_text()
        self.assertIn("tier: human", text)
        self.assertIn("status: proposed", text)
        self.assertIn("source: T-001", text)
        self.assertIn("title: rotate the API key", text)
        self.assertIn("depends: [T-002]", self.task_file("T-001").read_text())
        self.assertFalse(list(self.root.glob("tasks/**/T-003*")))
        stamp(self.task_file("T-002"))
        self.assertIn("T-002", self.ergon("next", "--tier", "human").stdout)

    def test_land_block_files_no_human_task(self):
        self.ergon("new", "task", "Source", "--tier", "sonnet")
        stamp(self.task_file("T-001"))
        self.env.update(FAKE_DIRTY="1")
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "blocked", out)
        self.assertIn("uncommitted", self.task_file("T-001").read_text())
        self.assertFalse(list(self.root.glob("tasks/**/T-002*")))

    def test_verify_gates_landing(self):
        for cmd, want in (("true", "done"), ("echo boom-line; exit 3", "blocked")):
            with self.subTest(cmd):
                self.setUp()
                self.ergon("new", "task", "Gated", "--tier", "sonnet")
                f = self.task_file("T-001")
                stamp(f)
                f.write_text(f.read_text().replace("\ntier:", f"\nverify: {cmd}\ntier:", 1))
                out = self.ergon("dispatch").stdout
                self.assertEqual(self.status("T-001"), want, out)
                wt = self.root / ".claude" / "worktrees" / "T-001"
                branches = sh("git", "branch", "--list", "task/T-001", cwd=self.root).stdout
                if want == "done":
                    self.assertTrue((self.root / "T-001.txt").exists())
                else:
                    self.assertIn("boom-line", self.task_file("T-001").read_text())
                    self.assertTrue(wt.exists())
                    self.assertIn("task/T-001", branches)
                    self.assertFalse((self.root / "T-001.txt").exists())  # nothing merged
                    logs = list((self.root / "tasks" / "runs").glob("T-001-*-verify.log"))
                    self.assertEqual(len(logs), 1)
                    self.assertIn("boom-line", logs[0].read_text())

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


class TestReview(DispatchProject):
    config = DispatchProject.config.replace('worker_rules = "WORKER.md"', 'worker_rules = "WORKER.md"\n        review = ["haiku", "opus"]')

    def setUp(self):
        super().setUp()
        self.seen = Path(self.tmp.name) / "seen.jsonl"
        self.env["FAKE_SEEN"] = str(self.seen)

    def calls(self):
        return [json.loads(l) for l in self.seen.read_text().splitlines()] if self.seen.exists() else []

    def test_review_gates_landing(self):
        cases = [("pass", "REVIEW: pass", None, "done", None),
                 ("fail", "REVIEW: fail \u2014 skipped the Done when tests", None, "blocked",
                  "review (sonnet) failed: skipped the Done when tests"),
                 ("no line", "Looks fine to me.", None, "blocked", "didn't end with a `REVIEW: pass`"),
                 ("pass not last", "REVIEW: pass\nbut wait, one more thing", None, "blocked", "didn't end with"),
                 ("misspelt", "REVIEW: passed", None, "blocked", "didn't end with"),
                 ("not json", None, None, "blocked", "wasn't JSON"),
                 ("changed branch", "REVIEW: pass", "1", "blocked", "changed the branch or worktree")]
        for name, review, sneak, want, why in cases:
            with self.subTest(name):
                self.setUp()
                self.ergon("new", "task", "Reviewed", "--tier", "haiku")
                stamp(self.task_file("T-001"))
                if review is None:  # a reviewer that prints garbage instead of JSON
                    bindir = Path(self.tmp.name) / "bin"
                    fake = (bindir / "claude").read_text()
                    (bindir / "claude").write_text(fake.replace('if tier == "REVIEW":', 'if tier == "REVIEW":\n    print("oops"); sys.exit()', 1))
                else:
                    self.env["FAKE_REVIEW"] = review
                if sneak:
                    self.env["FAKE_REVIEW_COMMIT"] = sneak
                out = self.ergon("dispatch").stdout
                self.assertEqual(self.status("T-001"), want, out)
                calls = self.calls()
                self.assertEqual([c["agent"] for c in calls], ["HAIKU", "REVIEW"])
                self.assertEqual(calls[1]["model"], "sonnet")  # one tier above the worker
                prompt = calls[1]["prompt"]
                self.assertIn("You are reviewing T-001", prompt)
                self.assertIn("title: Reviewed", prompt)
                self.assertIn("git diff main...task/T-001", prompt)
                self.assertIn("+work", prompt)
                self.assertIn("Never touch prod.", prompt)
                reviews = list((self.root / "tasks" / "runs").glob("T-001-*-review.json"))
                self.assertEqual(len(reviews), 1)
                cost = "$0.03 (reviews $0.02 of it)" if review is not None else "$0.01"
                self.assertIn(f"1 done, " if want == "done" else "0/1 done, ", out)
                self.assertIn(cost, out)
                if want == "done":
                    self.assertTrue((self.root / "T-001.txt").exists())
                    self.assertIn("review   T-001  sonnet: pass", out)
                else:
                    text = self.task_file("T-001").read_text()
                    self.assertIn(why, text)
                    self.assertIn("review log tasks/runs/T-001-", text)
                    self.assertIn("Branch task/T-001 and worktree", text)
                    self.assertFalse((self.root / "T-001.txt").exists())  # nothing merged
                    self.assertIn("task/T-001", sh("git", "branch", "--list", "task/T-001", cwd=self.root).stdout)
                    self.assertFalse(list(self.root.glob("tasks/**/T-002*")))  # no human task filed

    def test_review_tiers_and_retry_prompt(self):
        self.ergon("new", "task", "Unreviewed", "--tier", "sonnet")
        self.ergon("new", "task", "Top", "--tier", "opus")
        for t in ("T-001", "T-002"):
            stamp(self.task_file(t))
        dry = self.ergon("dispatch", "--dry-run").stdout
        self.assertRegex(dry, r"T-002  opus .*, reviewed by opus")
        self.assertNotIn("reviewed by", [l for l in dry.splitlines() if "T-001" in l][0])
        self.env["FAKE_REVIEW"] = "REVIEW: fail \u2014 no test for the edge case"
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "done", out)  # sonnet isn't in review: no reviewer
        self.assertEqual(self.status("T-002"), "blocked", out)
        reviews = [c for c in self.calls() if c["agent"] == "REVIEW"]
        self.assertEqual([c["model"] for c in reviews], ["opus"])  # capped: opus reviews opus
        # A human moves it back to ready; the retry is told what the reviewer said.
        f = self.task_file("T-002")
        f.write_text(re.sub(r"^status: blocked$", "status: ready", f.read_text(), flags=re.M))
        self.env["FAKE_REVIEW"] = "REVIEW: pass"
        self.seen.unlink()
        time.sleep(1.1)  # run logs are named to the second
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-002"), "done", out)
        worker = [c for c in self.calls() if c["agent"] == "OPUS"][0]["prompt"]
        self.assertIn("The reviewer vetoed the last attempt: review (opus) failed: no test for the edge case", worker)
        self.assertRegex(worker, r"Its full review is in /\S+/tasks/runs/T-002-\d+-\d+-review\.json\.")
        self.assertIn("An earlier attempt left commits", worker)
        stats = self.ergon("stats").stdout
        opus = [l.split() for l in stats.splitlines() if l.startswith("opus")][0]
        self.assertEqual(opus[:4], ["opus", "2", "2", "0"])  # reviews aren't runs
        self.assertEqual(opus[5], "$0.06")  # two worker runs + two reviews

    def test_review_sees_companion_diff(self):
        self.ergon("new", "task", "Touch public", "--tier", "haiku", "--touches", "acme/public")
        stamp(self.task_file("T-001"))
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "done", out)
        self.assertTrue((self.root / "pub" / "src" / "T-001.txt").exists())
        prompt = [c for c in self.calls() if c["agent"] == "REVIEW"][0]["prompt"]
        self.assertIn("Companion repo acme/public", prompt)
        self.assertIn("diff main...task/T-001`:", prompt)
        self.assertEqual(prompt.count("+work"), 2)  # the task's diff and the companion's

    def test_escalated_task_is_reviewed_from_the_tier_that_finished(self):
        self.ergon("new", "task", "Hard", "--tier", "haiku")
        stamp(self.task_file("T-001"))
        self.env["FAKE_RESULT_HAIKU"] = "RESULT: blocked \u2014 too hard"
        out = self.ergon("dispatch", "--escalate").stdout
        self.assertEqual(self.status("T-001"), "done", out)  # sonnet isn't listed, haiku is
        self.assertEqual([(c["agent"], c["model"]) for c in self.calls()],
                         [("HAIKU", "haiku"), ("SONNET", "sonnet"), ("REVIEW", "opus")])
        self.assertIn("A sonnet-tier worker says", self.calls()[2]["prompt"])

    def test_bad_review_config(self):
        (self.root / ".ergon.toml").write_text('review = ["human"]\n')
        r = self.ergon("board", check=False)
        self.assertIn("review lists worker tiers", r.stderr)


class TestAccept(DispatchProject):
    """Human acceptance of output: dispatch holds a marked task at `review` instead of landing it."""
    config = DispatchProject.config + '''
        [[accept]]
        tiers = ["opus"]

        [[accept]]
        touches = ["server/**"]
    '''

    def human(self, *args, answer="y"):
        """Run ergon the way a human does: on a terminal of its own, answering the [y/N] prompt."""
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.root)
                env = {k: v for k, v in self.env.items() if k not in ("CLAUDECODE", "AI_AGENT")}
                os.execve(sys.executable, [sys.executable, str(ERGON), *args], env)
            finally:
                os._exit(127)
        os.write(fd, (answer + "\n").encode())
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    def has_branch(self, name):
        return bool(sh("git", "branch", "--list", name, cwd=self.root).stdout.strip())

    def on(self, branch, path):
        return sh("git", "cat-file", "-e", f"{branch}:{path}", cwd=self.root, check=False).returncode == 0

    def test_review_holds_then_accept_lands_and_closes_epic(self):
        self.ergon("new", "epic", "Big thing")
        self.ergon("new", "task", "Risky", "--tier", "opus", "--epic", "E-001")
        self.ergon("new", "task", "Routine", "--tier", "sonnet", "--epic", "E-001")
        for t in ("T-001", "T-002"):
            stamp(self.task_file(t))
        dry = self.ergon("dispatch", "--dry-run").stdout
        self.assertIn("held for your accept", [l for l in dry.splitlines() if "T-001" in l][0])
        self.assertNotIn("held for your accept", [l for l in dry.splitlines() if "T-002" in l][0])
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-002"), "done", out)
        self.assertEqual(self.status("T-001"), "review", out)
        self.assertIn("1 awaiting your accept", out)
        self.assertIn("AWAITING YOUR ACCEPT (1)", out)
        epic = next((self.root / "tasks/epics").glob("E-001-*"))
        self.assertIn("status: open", (epic / "EPIC.md").read_text())  # not complete: T-001 isn't done
        branch = f"epic/{epic.name}"
        self.assertTrue(self.on(branch, "T-002.txt"))
        self.assertFalse(self.on(branch, "T-001.txt"))  # nothing of T-001 merged
        self.assertTrue(self.has_branch("task/T-001"))
        self.assertTrue((self.root / ".claude" / "worktrees" / "T-001").is_dir())
        self.assertIn("ergon accept T-001", self.task_file("T-001").read_text())
        self.ergon("check")
        self.assertIn("AWAITING ACCEPTANCE (1)", self.ergon("review").stdout)

        # Agents can't get it out of review: not by set, not by editing the file, not by accept.
        r = self.ergon("set", "T-001", "status=done", check=False)
        self.assertIn("review -> done not allowed", r.stderr)
        f = str(self.task_file("T-001"))
        self.assertIn("ergon accept", self.guard("Edit", {"file_path": f, "old_string": "status: review",
                                                         "new_string": "status: done"}))
        self.assertIn("human-only", self.guard("Bash", {"command": "ergon accept T-001"}))
        self.assertIn("human-only", self.guard("Bash", {"command": "python3 ergon.py accept E-001"}))
        r = self.ergon("accept", "T-001", check=False, env=clean_env(CLAUDECODE="1"))
        self.assertIn("agent shell", r.stderr)
        self.assertEqual(self.status("T-001"), "review")

        code, out = self.human("accept", "T-001", answer="n")
        self.assertIn("aborted", out)
        self.assertEqual(self.status("T-001"), "review")
        code, out = self.human("accept", "E-001")  # an epic ID accepts its tasks in review
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status("T-001"), "done", out)
        self.assertIn("accepted by Tester", self.task_file("T-001").read_text())
        self.assertIn("status: done", (epic / "EPIC.md").read_text())  # its last task: the epic closed
        self.assertTrue((self.root / "T-001.txt").exists())  # in the checked-out trunk
        self.assertTrue((self.root / "T-002.txt").exists())
        self.assertIn("Merge E-001: Big thing", sh("git", "log", "-1", "--format=%B", "main", cwd=self.root).stdout)
        self.assertRegex(sh("git", "log", "--format=%B", "main", cwd=self.root).stdout,
                         r"Merge T-001: Risky\n\nAccepted by Tester \d{4}-")  # history records who accepted
        self.assertFalse(self.has_branch("task/T-001"))
        self.assertFalse(self.has_branch(branch))
        self.assertFalse((self.root / ".claude" / "worktrees" / "T-001").exists())

    def test_reject_sends_review_back_to_ready(self):
        self.ergon("new", "task", "Api", "--tier", "haiku", "--touches", "server/api.py")
        self.ergon("new", "task", "Opted in", "--tier", "sonnet")
        self.ergon("new", "task", "Plain", "--tier", "sonnet")
        for t in ("T-001", "T-002", "T-003"):
            stamp(self.task_file(t))
        f2 = self.task_file("T-002")
        f2.write_text(f2.read_text().replace("\ntier:", "\naccept: yes\ntier:", 1))
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "review", out)  # an [[accept]] touches rule
        self.assertEqual(self.status("T-002"), "review", out)  # its own accept: yes
        self.assertEqual(self.status("T-003"), "done", out)

        # An agent can't take the mark off.
        self.assertIn("only a human", self.guard("Edit", {"file_path": str(f2), "old_string": "accept: yes\n",
                                                         "new_string": ""}))
        r = self.ergon("set", "T-003", "accept=no", check=False)
        self.assertIn("only turn `accept:` on", r.stderr)

        code, out = self.human("reject", "T-001")
        self.assertNotEqual(code, 0)
        self.assertIn("needs --reason", out)
        code, out = self.human("reject", "T-001", "--reason", "no test for the 404 path")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status("T-001"), "ready")
        self.assertIn("review: rejected by Tester: no test for the 404 path", self.task_file("T-001").read_text())
        self.assertTrue(self.has_branch("task/T-001"))  # kept for the next attempt
        self.assertFalse((self.root / "T-001.txt").exists())

        seen = Path(self.tmp.name) / "seen.jsonl"
        self.env["FAKE_SEEN"] = str(seen)
        time.sleep(1.1)  # run logs are named to the second
        out = self.ergon("dispatch").stdout
        self.assertEqual(self.status("T-001"), "review", out)  # held again
        prompt = json.loads(seen.read_text().splitlines()[0])["prompt"]
        self.assertIn("rejected by Tester: no test for the 404 path", prompt)
        self.assertIn("An earlier attempt left commits", prompt)

        # A review task whose branch is gone has nothing to accept: check says so.
        self.ergon("check")
        sh("git", "worktree", "remove", "--force", str(self.root / ".claude" / "worktrees" / "T-002"), cwd=self.root)
        sh("git", "branch", "-D", "task/T-002", cwd=self.root)
        r = self.ergon("check", check=False)
        self.assertIn("T-002: in review but branch task/T-002 is gone", r.stdout)
        code, out = self.human("accept", "T-002")
        self.assertNotEqual(code, 0)
        self.assertIn("no branch task/T-002", out)
        self.assertEqual(self.status("T-002"), "review")

    def test_bad_accept_config(self):
        (self.root / ".ergon.toml").write_text('[[accept]]\ntiers = ["human"]\n')
        r = self.ergon("board", check=False)
        self.assertIn("[[accept]] #1 needs tiers", r.stderr)


class TestStats(Project):
    def test_stats_per_tier(self):
        self.ergon("new", "task", "A", "--tier", "sonnet")
        self.ergon("new", "task", "B", "--tier", "haiku")
        runs = self.root / "tasks" / "runs"
        runs.mkdir(parents=True, exist_ok=True)

        def log(name, result, cost, **kw):
            (runs / name).write_text(json.dumps({"result": result, "total_cost_usd": cost, **kw}))
        log("T-001-20261101-100000.json", "RESULT: done", 1.0)
        log("T-001-20261102-100000.json", "RESULT: blocked — x", 0.5)
        log("T-001-20261103-100000.json", "RESULT: blocked — y", 0.5)
        log("T-002-20261103-100000.json", "RESULT: done", 0.25)
        log("T-002-20261001-100000.json", "RESULT: done", 9)  # before --since
        (runs / "T-002-20261104-100000.json").write_text("")  # timed out
        (runs / "T-002-20261104-100000.err").write_text("timed out after 60 min\n")
        (runs / "T-001-20261105-100000.json").write_text("not json")
        (runs / "junk.json").write_text("{}")
        out = self.ergon("stats", "--since", "2026-11-01").stdout
        rows = {l.split()[0]: l.split() for l in out.splitlines() if l.split()[:1] in (["sonnet"], ["haiku"])}
        self.assertEqual(rows["sonnet"], ["sonnet", "3", "1", "2", "67%", "$2.00", "$0.67", "0"])
        self.assertEqual(rows["haiku"], ["haiku", "2", "1", "1", "50%", "$0.25", "$0.12", "1"])
        self.assertIn("T-001 x2", out)
        self.assertIn("skipped 2", out)


class TestReport(Project):
    def test_report_sections(self):
        for n in "ABCD":
            self.ergon("new", "task", f"Task {n}", "--tier", "sonnet")
        for tid, st in (("T-001", "done"), ("T-002", "blocked")):
            f = self.task_file(tid)
            f.write_text(re.sub(r"^status: \w+$", f"status: {st}", f.read_text(), flags=re.M))
        f = self.task_file("T-002")
        f.write_text(f.read_text().rstrip() + "\n- 2026-11-08 dispatch: needs a decision\n")
        (self.root / "x.txt").write_text("x")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "Land it (T-001)"], check=True)
        time.sleep(1.1)  # git --since has one-second resolution
        out = self.ergon("report").stdout
        self.assertRegex(out, r"Landed\n  T-001  Task A\n      \w+ Land it \(T-001\)")
        self.assertRegex(out, r"Blocked\n  T-002  Task B\n      .*needs a decision")
        self.assertRegex(out, r"Awaiting approval\n  2 proposed\n    -\s+sonnet\s+2")
        self.assertNotIn("Human tasks", out)
        self.assertNotIn("Spend", out)
        self.assertTrue((self.root / "tasks" / ".report-last").exists())
        self.assertNotIn("Landed", self.ergon("report", "--since", "last").stdout)



class TestCommitHook(DispatchProject):
    """`ergon init --hooks`: git refuses commits citing a task no human approved, whatever agent
    (or human) makes them."""

    def setUp(self):
        super().setUp()
        self.hook = self.root / ".git" / "hooks" / "commit-msg"
        self.ergon("new", "task", "Proposed", "--tier", "sonnet")
        self.ergon("new", "task", "Approved", "--tier", "haiku")
        stamp(self.task_file("T-002"))

    def commit(self, msg, cwd=None):
        return sh("git", "commit", "--allow-empty", "-m", msg, cwd=cwd or self.root, check=False, env=self.env)

    def test_off_by_default(self):
        self.assertFalse(self.hook.exists())
        self.assertEqual(self.commit("Work (T-001)").returncode, 0)

    def test_refuses_unapproved_passes_approved(self):
        out = self.ergon("init", "--hooks").stdout
        self.assertIn("installed", out)
        self.assertTrue(os.access(self.hook, os.X_OK))
        r = self.commit("Do the proposed thing (T-001)")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001: proposed, and no human has approved it", r.stderr)
        r = self.commit("Invent a task (T-099)")
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-099: no such task", r.stderr)
        self.assertEqual(self.commit("Do the approved thing (T-002)").returncode, 0)
        self.assertEqual(self.commit("Cites nothing, or an epic E-001").returncode, 0)
        r = self.commit("Mixed (T-002, T-001)")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("T-002", r.stderr)
        # Comment lines git strips don't count ("# On branch task/T-001" in the editor template).
        (Path(self.tmp.name) / "msg").write_text("Approved work (T-002)\n\n# On branch task/T-001\n")
        self.assertEqual(sh("git", "commit", "--allow-empty", "--cleanup=strip", "-F",
                            str(Path(self.tmp.name) / "msg"), cwd=self.root, check=False).returncode, 0)
        # Hand-editing a task to ready without a stamp isn't approval.
        f = self.task_file("T-001")
        f.write_text(f.read_text().replace("status: proposed", "status: ready"))
        self.assertEqual(self.commit("Sneak (T-001)").returncode, 1)

    def test_worktree_and_merge(self):
        self.ergon("init", "--hooks")
        self.ergon("init", "--hooks")  # idempotent: rewrites its own hook
        wt = Path(self.tmp.name) / "wt"
        sh("git", "worktree", "add", "-q", "-b", "side", str(wt), cwd=self.root)
        self.assertEqual(self.commit("From a worktree (T-001)", cwd=wt).returncode, 1)
        self.assertEqual(self.commit("From a worktree (T-002)", cwd=wt).returncode, 0)
        # A merge commit only brings in checked commits; epic merges list dropped tasks, say.
        r = sh("git", "merge", "--no-ff", "-m", "Merge side: T-001 (dropped), T-002", "side",
               cwd=self.root, check=False, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_dispatch_lands_with_hook(self):
        self.ergon("init", "--hooks")
        out = self.ergon("dispatch", "--ids", "T-002").stdout
        self.assertEqual(self.status("T-002"), "done", out)

    def test_leaves_foreign_hooks_and_non_ergon_repos(self):
        self.hook.write_text("#!/bin/sh\nexit 0\n")
        r = self.ergon("init", "--hooks", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("isn't ergon's", r.stderr)
        self.assertEqual(self.hook.read_text(), "#!/bin/sh\nexit 0\n")
        self.hook.unlink()
        sh("git", "config", "core.hooksPath", ".husky", cwd=self.root)
        r = self.ergon("init", "--hooks", check=False)
        self.assertIn("core.hooksPath", r.stderr)
        self.assertFalse(self.hook.exists())
        sh("git", "config", "--unset", "core.hooksPath", cwd=self.root)
        self.ergon("init", "--hooks")
        (self.root / ".ergon.toml").unlink()  # no config: the hook does nothing
        self.assertEqual(self.commit("Work (T-001)").returncode, 0)

if __name__ == "__main__":
    unittest.main()
