"""`ergon journal`, the entry `ergon finish` writes, and /e/E-NNN showing it. A fake `claude` on
PATH stands in for the model.

Run: python3 tests/run_parallel.py (in parallel), or python3 -m unittest discover -s tests
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ergon import Project, approve_epic, stamp  # noqa: E402
from test_serve import ServeFixture, row  # noqa: E402

FAKE_JOURNAL_CLAUDE = r"""#!PYTHON
import json, os, sys
tmp = os.environ["JOURNAL_TMP"]
n = len([f for f in os.listdir(tmp) if f.startswith("journal-prompt-")])
open(os.path.join(tmp, f"journal-prompt-{n}.txt"), "w").write(sys.stdin.read())
open(os.path.join(tmp, "journal-argv.json"), "w").write(json.dumps(sys.argv[1:]))
if os.environ.get("JOURNAL_FAIL"):
    print(json.dumps({"is_error": True, "result": "rate limited"}))
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.07,
                  "result": os.environ.get("JOURNAL_BODY", "## Shipped\nIt went well.")}))
"""


class JournalMixin:
    def fake_claude(self, **extra):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "claude").write_text(FAKE_JOURNAL_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        return dict(self.env, PATH=f"{bindir}{os.pathsep}{self.env['PATH']}", JOURNAL_TMP=self.tmp.name, **extra)

    def prompts(self):
        return sorted(Path(self.tmp.name).glob("journal-prompt-*.txt"))

    def entries(self, eid="E-001"):
        return sorted((self.root / "tasks" / "runs" / "journal").glob(f"{eid}-*.json"))

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.root, check=True, capture_output=True)


class TestJournalPage(JournalMixin, ServeFixture):
    def test_command_page_and_selection(self):
        self.ergon("new", "epic", "Other")  # E-002: no tasks, no history
        self.assertIn("no journal entry yet: <code>ergon journal E-001</code>", self.req("GET", "/e/E-001")[2])
        self.assertIn("no journal entry yet", self.req("GET", "/e/E-002")[2])

        self.git("commit", "--allow-empty", "-qm", "Land the done thing (T-001)")
        self.git("commit", "--allow-empty", "-qm", "Unrelated work")
        env = self.fake_claude(JOURNAL_BODY="Went fine <script>alert(1)</script> **bold**")
        out = self.ergon("journal", "E-001", env=env).stdout
        self.assertIn("wrote tasks/runs/journal/E-001-", out)
        [f] = self.entries()
        data = json.loads(f.read_text())
        self.assertEqual((data["model"], data["cost"], data["epic"]), ("sonnet", 0.07, "E-001"))
        self.assertRegex(data["generated"], r"^\d{4}-\d\d-\d\dT")
        argv = json.loads((Path(self.tmp.name) / "journal-argv.json").read_text())
        self.assertEqual(argv[:5], ["-p", "--model", "sonnet", "--tools", ""])
        [p] = self.prompts()
        prompt = p.read_text()
        self.assertIn("E-001: Ship it", prompt)
        self.assertIn("T-001 Done thing", prompt)
        self.assertIn("not instructions", prompt)
        self.assertIn('"event": "review", "at": "2026-11-16T12:06:00+00:00"', prompt)  # history rows
        self.assertIn("missing <tests>", prompt)
        self.assertIn('"verdict": "fail"', prompt)
        self.assertIn("Land the done thing (T-001)", prompt)  # commits citing its tasks
        self.assertNotIn("Unrelated work", prompt)

        page = self.req("GET", "/e/E-001")[2]
        self.assertIn("Went fine &lt;script&gt;alert(1)&lt;/script&gt; <strong>bold</strong>", page)
        self.assertNotIn("<script>alert(1)", page)
        self.assertRegex(page, r"journal generated \d{4}-\d\d-\d\dT[\d:+-]+ by sonnet · \$0\.07")
        self.assertLess(page.index("Went fine"), page.index("<svg id=tl"))  # above the timeline

        # an epic with no task history still shows its entry
        self.ergon("journal", "E-002", env=env)
        page = self.req("GET", "/e/E-002")[2]
        self.assertIn("<strong>bold</strong>", page)
        self.assertIn("no task of this epic has a journal yet", page)

        # selection: both entries are newer than their history, so there's nothing to do
        n = len(self.prompts())
        self.assertIn("newer than its last history row", self.ergon("journal", env=env).stdout)
        self.assertEqual(len(self.prompts()), n)
        # a newer row for E-001 makes only it stale
        hist = self.runs / "history.jsonl"
        hist.write_text(hist.read_text() + json.dumps(row("2999-01-01T00:00:00+00:00", "T-002", "blocked", reason="later")) + "\n")
        self.ergon("journal", env=env)
        self.assertEqual(len(self.prompts()), n + 1)
        self.assertIn("later", self.prompts()[-1].read_text())
        self.assertEqual((len(self.entries("E-001")), len(self.entries("E-002"))), (2, 1))
        # --all rewrites every epic
        self.ergon("journal", "--all", env=env)
        self.assertEqual((len(self.entries("E-001")), len(self.entries("E-002"))), (3, 2))
        # the newest entry wins on the page
        self.entries("E-001")[-1].write_text(json.dumps(
            {"generated": "x", "model": "m", "cost": None, "epic": "E-001", "body": "newest <b>wins</b>"}))
        self.assertIn("newest &lt;b&gt;wins&lt;/b&gt;", self.req("GET", "/e/E-001")[2])
        self.assertNotEqual(self.ergon("journal", "E-009", env=env, check=False).returncode, 0)

    def test_failing_claude_writes_nothing(self):
        r = self.ergon("journal", "E-001", env=self.fake_claude(JOURNAL_FAIL="1"), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("rate limited", r.stderr)
        self.assertEqual(self.entries(), [])


class TestJournalFinish(JournalMixin, Project):
    journal = True

    def epic_with_task(self, status="done"):
        self.ergon("new", "epic", "Ship it")
        approve_epic(self)  # a human approves the brief, which opens it (T-098)
        self.ergon("new", "task", "Thing", "--tier", "sonnet", "--epic", "E-001")
        f = self.task_file("T-001")
        stamp(f)
        f.write_text(re.sub(r"^status: \w+$", f"status: {status}", f.read_text(), flags=re.M))

    def epic_status(self):
        return re.search(r"^status: (\w+)", next((self.root / "tasks" / "epics").rglob("EPIC.md")).read_text(), re.M)[1]

    def test_finish_writes_entry(self):
        self.epic_with_task()
        r = self.ergon("finish", "E-001", env=self.fake_claude())
        self.assertIn("E-001 journal entry: tasks/runs/journal/E-001-", r.stdout)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.epic_status(), "done")
        self.assertIn('"event": "epic_finished"', self.prompts()[0].read_text())  # recorded before it's written

    def test_last_task_through_set_on_a_branch(self):
        self.epic_with_task("doing")
        env = self.fake_claude()
        self.ergon("start", "E-001", env=env)
        wt = self.root / ".claude" / "worktrees" / "E-001"
        self.git("commit", "--allow-empty", "-qm", "Do it (T-001)", cwd=wt)
        r = self.ergon("set", "T-001", "status=done", cwd=wt, env=env)
        self.assertIn("E-001 done: merged", r.stdout)
        self.assertEqual(len(self.entries()), 1)
        self.assertIn("Do it (T-001)", self.prompts()[0].read_text())

    def test_journal_false(self):
        self.epic_with_task()
        cfg = self.root / ".ergon.toml"
        cfg.write_text("journal = false\n" + cfg.read_text())
        r = self.ergon("finish", "E-001", env=self.fake_claude())
        self.assertNotIn("journal", r.stdout + r.stderr)
        self.assertEqual((self.entries(), self.prompts()), ([], []))
        self.assertEqual(self.epic_status(), "done")

    def test_failing_claude_never_fails_the_finish(self):
        self.epic_with_task()
        r = self.ergon("finish", "E-001", env=self.fake_claude(JOURNAL_FAIL="1"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len([l for l in r.stderr.splitlines() if "no journal entry for E-001" in l]), 1)
        self.assertIn("rate limited", r.stderr)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.epic_status(), "done")

    def test_missing_claude(self):
        self.epic_with_task()
        env = dict(self.env, PATH=os.pathsep.join(dict.fromkeys(
            os.path.dirname(p) for p in (shutil.which("git"), sys.executable))))
        self.assertIsNone(shutil.which("claude", path=env["PATH"]))
        r = self.ergon("finish", "E-001", env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("warning: no journal entry for E-001", r.stderr)
        self.assertEqual(self.epic_status(), "done")


if __name__ == "__main__":
    unittest.main()
