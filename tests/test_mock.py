"""`ergon mock [T-NNN ...]` (T-311): a script-free mock of each proposed UI task's planned result."""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ergon import Project, clean_env, sh, stamp  # noqa: E402
from ergon import config as _config  # noqa: E402
from ergon import mock as M  # noqa: E402
from ergon import serve as _serve  # noqa: E402
from ergon import tasks as _tasks  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

MAP = """\
[[concern]]
id = "visibility"

[[concern]]
id = "model"

[[component]]
id = "ui.serve"
concern = "visibility"
files = ["ergon/serve.py", "ergon/serve.css"]
terms = ["board"]

[[component]]
id = "ui.stats"
concern = "visibility"
files = ["ergon/report.py"]
terms = ["stats"]

[[component]]
id = "model.tasks"
concern = "model"
files = ["ergon/tasks.py"]
terms = ["task"]
"""

FAKE_CLAUDE = r'''#!PYTHON
"""Stands in for `claude -p` under `ergon mock`: logs the task it was asked about to
$MOCK_TMP/asked.txt, saves the prompt and replies with $MOCK_REPLY as the result text."""
import json, os, re, sys
prompt = sys.stdin.read()
tid = re.search(r"<<<brief (T-\d+) ", prompt)[1]
with open(os.path.join(os.environ["MOCK_TMP"], "asked.txt"), "a") as f:
    f.write(tid + "\n")
open(os.path.join(os.environ["MOCK_TMP"], "prompt.txt"), "w").write(prompt)
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.02, "result": os.environ["MOCK_REPLY"]}))
'''

GOOD = {"where": "/review", "nodes": [
    {"type": "heading", "level": 2, "text": "Planned look"},
    {"type": "box", "title": "T-042 Show mocks", "children": [
        {"type": "badge", "text": "proposed", "tone": "warn"},
        {"type": "table", "columns": ["task", "cost"], "rows": [["T-042", "$0.12"]]},
        {"type": "list", "items": ["one", "two"]},
    ]},
    {"type": "text", "text": "$ ergon mock\nwrote tasks/runs/mock/T-042-....json", "mono": True},
]}


class MockProject(Project):
    config = 'concepts = "concepts.toml"\n'

    def setUp(self):
        super().setUp()
        (self.root / "concepts.toml").write_text(MAP)
        sh("git", "add", "-A", cwd=self.root)
        sh("git", "commit", "-qm", "map", cwd=self.root)
        bindir = Path(self.tmp.name) / "mockbin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        self.env = clean_env(PATH=f"{bindir}:{os.environ['PATH']}", MOCK_TMP=self.tmp.name)
        # T-001 serve page, T-002 the CLI, T-003 stats, T-004 not visible, T-005 visible but ready
        for title, touches in (("Mock section", "ergon/serve.py"), ("Mock command", "ergon/cli.py"),
                               ("Mock chart", "ergon/report.py/::stats"), ("Task model", "ergon/tasks.py"),
                               ("Board tweak", "ergon/serve.css")):
            self.ergon("new", "task", title, "--tier", "sonnet", "--touches", touches)
        stamp(self.task_file("T-005"))
        self.dir = self.root / "tasks" / "runs" / "mock"

    def mock(self, *ids, reply=None, check=True):
        return self.ergon("mock", *ids, check=check, env=dict(self.env, MOCK_REPLY=reply or json.dumps(GOOD)))

    def files(self):
        return sorted(self.dir.glob("*.json")) if self.dir.exists() else []

    def asked(self):
        p = Path(self.tmp.name) / "asked.txt"
        return p.read_text().split() if p.exists() else []

    def task(self, tid):
        _config.configure(self.root)
        return _tasks.load(_config.ITEMS)[tid]


class TestMock(MockProject):
    def test_writes_one_file_whose_hash_is_the_brief_hash(self):
        out = self.mock("T-001", reply="Here:\n```json\n" + json.dumps(GOOD) + "\n```").stdout
        [p] = self.files()
        self.assertRegex(p.name, r"^T-001-\d{8}-\d{6}\.json$")
        self.assertIn(f"wrote tasks/runs/mock/{p.name}", out)
        d = json.loads(p.read_text())
        self.assertEqual(set(d), {"task", "hash", "generated", "model", "cost", "format", "body"})
        self.assertEqual((d["task"], d["model"], d["cost"], d["format"], d["body"]),
                         ("T-001", "sonnet", 0.02, M.FORMAT, GOOD))
        t = self.task("T-001")
        self.assertEqual(d["hash"], _serve.brief_hash(t))
        self.assertFalse(M.mock_stale(t, d))
        self.assertEqual(M.latest_mock("T-001"), (p.name, d))
        prompt = (Path(self.tmp.name) / "prompt.txt").read_text()
        fence = re.search(r"<<<brief T-001 (\w+)>>>", prompt)[1]
        self.assertIn(f"<<<end {fence}>>>", prompt)
        self.assertIn("title: Mock section", prompt)
        f = self.task_file("T-001")  # an edited brief makes it stale
        f.write_text(f.read_text() + "\nmore detail\n")
        self.assertTrue(M.mock_stale(self.task("T-001"), d))

    def test_no_ids_asks_only_proposed_visible_tasks(self):
        self.mock()
        self.assertEqual(sorted(self.asked()), ["T-001", "T-002", "T-003"])
        self.assertEqual(sorted(p.name[:5] for p in self.files()), ["T-001", "T-002", "T-003"])
        _config.configure(self.root)
        tasks = _tasks.load(_config.ITEMS)
        self.assertEqual([i for i in sorted(tasks) if M.visible(tasks[i])], ["T-001", "T-002", "T-003", "T-005"])

    def test_without_a_map_only_the_cli_counts(self):
        cfg = self.root / ".ergon.toml"
        cfg.write_text(cfg.read_text().replace('concepts = "concepts.toml"\n', ""))
        self.mock()
        self.assertEqual(self.asked(), ["T-002"])

    def test_nothing_visible_says_so(self):
        for tid in ("T-001", "T-002", "T-003"):
            stamp(self.task_file(tid))
        out = self.mock().stdout
        self.assertIn("no proposed task touches a visible part", out)
        self.assertEqual(self.asked(), [])

    def test_a_reply_that_does_not_hold_writes_nothing(self):
        def node(**kw):
            return json.dumps({"where": "/review", "nodes": [kw]})
        bad = [
            "no JSON here",
            json.dumps({"where": "/review", "nodes": []}),
            json.dumps({"where": "/review", "nodes": [{"type": "html", "text": "x"}]}),
            node(type="text", text="hi", style="color:red"),
            node(type="heading", level=7, text="x"),
            node(type="table", columns=["a", "b"], rows=[["only one"]]),
            node(type="badge", text="x", tone="pink"),
            node(type="text", text="<script>alert(1)</script>"),
            node(type="text", text="a <SVG viewBox='0 0 1 1'></svg>"),
            node(type="list", items=['<img src=x onerror="alert(1)">']),
            node(type="box", title="<a href=x onclick=go()>", children=[]),
            node(type="text", text="javascript:alert(1)"),
            node(type="text", text="x" * (M.MAX_TEXT + 1)),
            json.dumps({"where": "/review", "nodes": [{"type": "list", "items": ["x"]}] * (M.MAX_NODES + 1)}),
        ]
        for reply in bad:
            with self.subTest(reply=reply[:60]):
                r = self.mock("T-001", reply=reply, check=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("nothing written", r.stderr)
                self.assertEqual(self.files(), [])
        self.assertNotEqual(self.ergon("mock", "T-404", check=False).returncode, 0)

    def test_one_failing_task_still_writes_the_others_and_exits_non_zero(self):
        r = self.mock("T-001", "T-002", reply=json.dumps({"where": "x", "nodes": [{"type": "nope"}]}), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.asked(), ["T-001", "T-002"])

    def test_plain_text_that_only_mentions_tags_holds(self):
        body = {"where": "ergon mock --help", "nodes": [
            {"type": "text", "text": "writes <state>/runs/mock/T-NNN-<time>.json; one=1 a<b", "mono": True}]}
        self.assertEqual(M.check_body(body), [])

    def test_boxes_nested_too_deep_fail(self):
        n = {"type": "text", "text": "x"}
        for _ in range(M.MAX_DEPTH + 1):
            n = {"type": "box", "children": [n]}
        self.assertTrue(any("nested" in e for e in M.check_body({"where": "x", "nodes": [n]})))


class TestLatestMock(MockProject):
    def test_newest_valid_file_wins_and_symlinks_and_bad_json_are_skipped(self):
        self.dir.mkdir(parents=True)
        good = {"task": "T-001", "hash": "h", "generated": "g", "model": "sonnet", "cost": None,
                "format": M.FORMAT, "body": GOOD}
        (self.dir / "T-001-20260101-000000.json").write_text(json.dumps(good))
        (self.dir / "T-001-20260102-000000.json").write_text("{not json")
        bad_body = dict(good, body={"where": "x", "nodes": [{"type": "text", "text": "<script>"}]})
        (self.dir / "T-001-20260103-000000.json").write_text(json.dumps(bad_body))
        (self.dir / "T-001-20260104-000000.json").write_text(json.dumps(dict(good, task="T-002")))
        (self.dir / "T-001-20260105-000000.json").write_text(json.dumps(dict(good, format="html")))
        elsewhere = Path(self.tmp.name) / "elsewhere.json"
        elsewhere.write_text(json.dumps(dict(good, hash="evil")))
        (self.dir / "T-001-20260106-000000.json").symlink_to(elsewhere)
        (self.dir / "T-0001-20260107-000000.json").write_text(json.dumps(good))  # another task's name
        _config.configure(self.root)
        self.assertEqual(M.latest_mock("T-001"), ("T-001-20260101-000000.json", good))
        self.assertEqual(M.latest_mock("T-002"), (None, None))
        (self.dir / "T-001-20260101-000000.json").unlink()
        self.assertEqual(M.latest_mock("T-001"), (None, None))
        (self.dir / "T-001-20260108-000000.json").write_text(json.dumps(dict(good, hash="newer")))
        self.assertEqual(M.latest_mock("T-001")[1]["hash"], "newer")


class TestDocs(unittest.TestCase):
    def test_help_readme_and_map(self):
        out = sh(sys.executable, "-m", "ergon", "--help", cwd=REPO, env=clean_env()).stdout
        self.assertIn("ergon mock [T-ID ...]", out)
        self.assertIn("`ergon mock [T-NNN ...]", (REPO / "README.md").read_text())
        self.assertIn('"ergon/mock.py"', (REPO / "concepts.toml").read_text())


if __name__ == "__main__":
    unittest.main()
