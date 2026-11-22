"""`ergon serve`: the read-only board and task pages, driven over HTTP against a fixture state dir.

Run: python3 -m unittest discover -s tests
"""
import contextlib
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ergon import ERGON, Project, stamp  # noqa: E402
from ergon import serve as S  # noqa: E402

HOSTILE = '<script>alert("x")</script> <img src=x onerror=alert(1)> [click](javascript:alert(1))'


def row(at, task, event, **kw):
    return dict({"at": at, "task": task, "epic": "E-001", "event": event}, **kw)


class ServeFixture(Project):
    """E-001 with: T-001 done (two review rounds, a run log, a transcript), T-002 doing, T-003 in
    review, T-004 a hostile proposed task, T-005 a human task a blocked worker filed."""

    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"  # ~/.claude/projects lives here for the server
        self.env = dict(self.env, HOME=str(self.home))
        self.ergon("new", "epic", "Ship it")
        for title, tier in (("Done thing", "sonnet"), ("Running thing", "opus"), ("Held thing", "opus"),
                            ("Evil " + HOSTILE, "haiku"), ("Answer the worker", "human")):
            self.ergon("new", "task", title, "--tier", tier, "--epic", "E-001")
        for tid, status in (("T-001", "done"), ("T-002", "doing"), ("T-003", "review")):
            f = self.task_file(tid)
            stamp(f)
            f.write_text(re.sub(r"^status: \w+$", f"status: {status}", f.read_text(), flags=re.M))
        f = self.task_file("T-004")
        f.write_text(f.read_text().replace("## What\n", f"## What\n{HOSTILE}\n\n- **bold** `<b>code</b>` see T-001\n", 1))
        f = self.task_file("T-005")
        f.write_text(re.sub(r"^source:.*$", "source: T-002", f.read_text(), flags=re.M))

        self.runs = self.root / "tasks" / "runs"
        self.runs.mkdir(parents=True, exist_ok=True)
        wt = ".claude/worktrees/T-001"
        log = "tasks/runs/T-001-20261116-120000.json"
        rows = [
            row("2026-11-16T12:00:00+00:00", "T-001", "dispatched", tier="sonnet", worktree=wt, branch="task/T-001"),
            row("2026-11-16T12:05:00+00:00", "T-001", "worker_end", tier="sonnet", verdict="done", reason="did it",
                session="s-1", cost=0.5, seconds=300, log=log),
            row("2026-11-16T12:06:00+00:00", "T-001", "review", verdict="fail", reviewer="opus", tier="sonnet",
                reason="missing <tests>", cost=0.25, seconds=60, log="tasks/runs/T-001-20261116-120600-review.json"),
            row("2026-11-16T12:07:00+00:00", "T-001", "resumed", session="s-1", round=2, tier="sonnet"),
            row("2026-11-16T12:09:00+00:00", "T-001", "worker_end", tier="sonnet", verdict="done", reason="fixed",
                session="s-1", cost=0.125, seconds=120, log="tasks/runs/T-001-20261116-120700-fix2.json", round=2),
            row("2026-11-16T12:10:00+00:00", "T-001", "review", verdict="pass", reviewer="opus", tier="sonnet",
                reason=None, cost=0.125, seconds=50, log="tasks/runs/T-001-20261116-121000-review2.json", round=2),
            row("2026-11-16T12:11:00+00:00", "T-001", "landed", base="epic/E-001", branch="task/T-001", commit="abc"),
            row("2026-11-16T13:00:00+00:00", "T-002", "dispatched", tier="opus", worktree=".claude/worktrees/T-002"),
            row("2026-11-16T13:10:00+00:00", "T-002", "worker_end", tier="opus", verdict="done", cost=1.0, seconds=600),
            row("2026-11-16T13:11:00+00:00", "T-002", "review", verdict="fail", reviewer="opus", cost=0.5, seconds=60),
            row("2026-11-16T13:12:00+00:00", "T-002", "dispatched", tier="opus", round=2, fresh_because="cold"),
        ]
        (self.runs / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.runs / "T-001-20261116-120000.json").write_text(json.dumps(
            {"result": "did it\nRESULT: done", "session_id": "s-1", "total_cost_usd": 0.5}))
        (self.runs / "T-001-20261116-120600-review.json").write_text("<script>not html</script>\n")
        (self.root / "tasks" / "secret.txt").write_text("outside runs\n")
        os.symlink(self.root / ".ergon.toml", self.runs / "T-001-link.json")

        tdir = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(self.root / wt))
        tdir.mkdir(parents=True)
        entry = {"type": "assistant", "timestamp": "2026-11-16T12:01:00.000Z",
                 "message": {"id": "m1", "model": "m", "usage": {"input_tokens": 1234, "output_tokens": 567},
                             "content": [{"type": "tool_use", "id": "u1", "name": "Bash", "input": {}}]}}
        (tdir / "s-1.jsonl").write_text(json.dumps(entry, separators=(",", ":")) + "\n")

        self.before = self.snapshot()
        self.proc = subprocess.Popen([sys.executable, str(ERGON), "serve", "--port", "0"], cwd=self.root,
                                     env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
        if not m:
            self.proc.kill()
            self.fail(f"no address in {line!r}: {self.proc.stderr.read()}")
        self.port = int(m[1])

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        super().tearDown()

    def snapshot(self):
        """Every file under the project (but .git) and the fake ~/.claude, with size and mtime.
        (Not all of the fake home: macOS's system Python caches bytecode under ~/Library.)"""
        out = {}
        for base in (self.root, self.home / ".claude"):
            for p in base.rglob("*"):
                if ".git" in p.relative_to(base).parts or not p.is_file():
                    continue
                st = p.lstat()
                out[str(p)] = (st.st_size, st.st_mtime_ns)
        return out

    def req(self, method, path, host=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", host or f"127.0.0.1:{self.port}")
            if body is not None:
                c.putheader("Content-Length", str(len(body)))
            c.endheaders(body.encode() if body is not None else None)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read().decode("utf-8", "replace")
        finally:
            c.close()


class TestServe(ServeFixture):
    def test_board(self):
        code, headers, page = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        for section in ("PROPOSED (2)", "DOING (1)", "REVIEW (1)"):
            self.assertIn(section, page)
        self.assertNotIn("Awaiting answer", page)  # T-005 is a human task, not a worker's question
        self.assertNotIn("DONE (", page)  # open work only, as `ergon` prints it
        self.assertIn('<b id="count-approval">2</b>', page)
        self.assertIn('<b id="count-acceptance">1</b>', page)
        self.assertIn('<b id="count-answer">0</b>', page)
        self.assertIn("1/5", page)  # E-001's progress
        self.assertIn('href="/t/T-002"', page)
        # PROPOSED lists haiku before human, as `ergon` sorts by tier
        self.assertLess(page.index('href="/t/T-004"'), page.index('href="/t/T-005"'))
        # the only script is ours, and it runs under the CSP nonce
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", headers["Content-Security-Policy"])[1]
        self.assertEqual(re.findall(r"<script[^>]*>", page), [f'<script nonce="{nonce}">'])
        self.assertIn("&lt;script&gt;", page)  # T-004's title, escaped

    def test_limited_and_asking_marks(self):
        f = self.task_file("T-002")
        f.write_text(re.sub(r"^status: \w+$", "status: blocked\nlimited: 2030-01-01T09:30:00Z worker",
                            f.read_text(), flags=re.M))
        f = self.task_file("T-003")
        f.write_text(re.sub(r"^status: \w+$", "status: blocked\nasking: sess-1", f.read_text(), flags=re.M))
        want = S._limits.clock(S._tasks.limited_mark("2030-01-01T09:30:00Z worker")[0])
        self.assertIn(f"limited until {want}", self.req("GET", "/")[2])
        self.assertIn(f"limited until {want}", self.req("GET", "/t/T-002")[2])
        self.assertIn("<span class=park>asking</span>", self.req("GET", "/")[2])
        self.assertIn("<span class=park>asking</span>", self.req("GET", "/t/T-003")[2])
        self.assertNotIn("class=park", self.req("GET", "/t/T-001")[2])

    def test_awaiting_answer_lists_asking_tasks_only(self):
        f = self.task_file("T-003")
        f.write_text(re.sub(r"^status: \w+$", "status: blocked\nasking: sess-1", f.read_text(), flags=re.M).rstrip()
                     + "\n- 2026-11-08 dispatch: asking: 1 question(s) below\n"
                     "- 2026-11-08 question: Which <b>store</b>? [options: sqlite | json]\n")
        page = self.req("GET", "/")[2]
        self.assertIn("Awaiting answer (1)", page)
        self.assertIn('<b id="count-answer">1</b>', page)
        sec = page[page.index("Awaiting answer"):]
        self.assertIn('href="/t/T-003"', sec)
        self.assertNotIn("T-005", sec)  # the ready human task stays under READY only
        self.assertIn("Which &lt;b&gt;store&lt;/b&gt;?", sec)
        self.assertEqual(json.loads(self.req("GET", "/live.json")[2])["counts"]["answer"], 1)

    def test_live_json(self):
        code, headers, body = self.req("GET", "/live.json")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        d = json.loads(body)
        self.assertEqual(d["counts"], {"approval": 2, "choice": 0, "acceptance": 1, "answer": 0})
        [doing] = d["doing"]
        self.assertEqual(doing["id"], "T-002")
        self.assertEqual(doing["round"], 2)
        self.assertEqual(doing["spent"], 1.5)  # its worker and reviewer so far
        self.assertEqual(doing["last"], "dispatched")
        self.assertAlmostEqual(doing["since_ts"], S.parse_at("2026-11-16T13:12:00+00:00"))

    def test_questions_in_lens_order_on_review_and_task_page(self):
        # T-258: the open questions list lens options in lens order, none recommended, on /review and /t/T-NNN.
        f = self.task_file("T-003")
        f.write_text(re.sub(r"^status: \w+$", "status: blocked\nasking: sess-1", f.read_text(), flags=re.M).rstrip()
                     + "\n- 2026-11-08 dispatch: asking: 1 question(s) below\n"
                     "- 2026-11-08 question: Which store? [options: risk: a journal \u2014 gives up speed | "
                     "minimal: a flat file \u2014 gives up queries]\n")
        for path in ("/", "/t/T-003"):
            page = self.req("GET", path)[2]
            self.assertLess(page.index("minimal: a flat file"), page.index("risk: a journal"), path)
            self.assertNotIn("recommended", page, path)
        self.assertIn("Open questions", self.req("GET", "/t/T-003")[2])
        self.assertNotIn("Open questions", self.req("GET", "/t/T-001")[2])

    def test_task_page(self):
        code, _, page = self.req("GET", "/t/T-001")
        self.assertEqual(code, 200)
        self.assertIn("Done thing", page)
        self.assertIn("<th>approved</th><td class=why>Tester 2026-01-01</td>", page)  # frontmatter
        self.assertIn("2 worker run(s), 2 review(s), $1.00 in the journal", page)
        self.assertIn('last review <span class="v-pass">pass</span> (round 2)', page)
        self.assertIn("missing &lt;tests&gt;", page)  # the round 1 veto, escaped
        self.assertIn('<a href="/runs/T-001-20261116-120000.json">T-001-20261116-120000.json</a>', page)
        resume = f"cd {self.root / '.claude/worktrees/T-001'} &amp;&amp; claude --resume s-1"
        self.assertIn(resume, page)
        self.assertIn("tokens in 1,234 out 567", page)  # from the transcript (T-025)
        self.assertIn("tools Bash 1", page)
        self.assertIn("<td>landed</td>", page)  # the journal
        self.assertEqual(self.req("GET", "/t/T-999")[0], 404)
        self.assertEqual(self.req("GET", "/t/E-001")[0], 404)

    def test_task_page_evidence(self):
        d = self.runs / "T-001-evidence"
        d.mkdir()
        (d / "shot.png").write_bytes(b"\x89PNG\x00fake")
        (d / "out.txt").write_text("3 passed <b>\n")
        files = [{"name": "shot.png", "caption": HOSTILE}, {"name": "out.txt", "caption": "tests"}]
        journal = self.runs / ("history" + ".jsonl")
        with journal.open("a") as f:
            f.write(json.dumps(row("2026-11-16T12:08:00+00:00", "T-001", "evidence", files=files,
                                   refused=["../x: outside the worktree"])) + "\n")
        code, _, page = self.req("GET", "/t/T-001")
        self.assertEqual(code, 200)
        section = page.split("<h2>Evidence</h2>")[1].split("<h2>")[0]
        self.assertIn('<img src="/runs/T-001-evidence/shot.png"', section)
        self.assertIn('<a href="/runs/T-001-evidence/out.txt">out.txt</a>', section)
        self.assertNotIn("<script>", section)
        self.assertNotIn("<img src=x", section)
        self.assertIn("&lt;script&gt;alert", section)
        self.assertIn("refused: ../x: outside the worktree", section)
        code, _, body = self.req("GET", "/runs/T-001-evidence/out.txt")
        self.assertEqual(code, 200)
        self.assertIn("3 passed", body)
        self.assertEqual(self.req("GET", "/runs/T-001-evidence/..%2fsecret.txt")[0], 404)
        self.assertEqual(self.req("GET", "/runs/T-001-evidence/missing.png")[0], 404)

    def test_epic_page_breakdown_runs(self):
        d = self.runs / "breakdown"
        d.mkdir()
        (d / "E-001-20261116-100000.json").write_text("bd log <b>\n")
        journal = self.runs / ("history" + ".jsonl")
        with journal.open("a") as f:
            f.write(json.dumps(row("2026-11-16T10:00:00+00:00", None, "breakdown", verdict="blocked", cost=1.25,
                                   seconds=30, log="tasks/runs/breakdown/E-001-20261116-100000.json")) + "\n")
        code, _, page = self.req("GET", "/e/E-001")
        self.assertEqual(code, 200)
        sec = page.split("<h2>Breakdown runs</h2>")[1].split("<h2")[0]
        self.assertIn('<span class="v-blocked">blocked</span>', sec)
        self.assertIn("$1.25", sec)
        self.assertIn('<a href="/runs/breakdown/E-001-20261116-100000.json">', sec)
        code, _, body = self.req("GET", "/runs/breakdown/E-001-20261116-100000.json")
        self.assertEqual(code, 200)
        self.assertIn("bd log", body)
        for bad in ("/runs/breakdown/..%2fT-001-20261116-120000.json", "/runs/breakdown/missing.json",
                    "/runs/breakdown/.hidden", "/runs/breakdown/%2e%2e/x"):
            self.assertEqual(self.req("GET", bad)[0], 404, bad)

    def test_follow_ups_open_on_board_and_task_page(self):
        f = self.task_file("T-004")
        f.write_text(re.sub(r"(?m)^source:.*$", "source: T-001", f.read_text()))
        f = self.task_file("T-005")
        f.write_text(re.sub(r"(?m)^source:.*$", "source: T-004", f.read_text()))  # a follow-up of a follow-up
        _, _, board = self.req("GET", "/")
        sec = board.split("<h2 id=followups>")[1].split("<h2")[0]
        self.assertIn("done, 2 follow-ups open", sec)
        self.assertIn('href="/t/T-001"', sec)
        _, _, page = self.req("GET", "/t/T-001")
        fu = page.split("<h2>Follow-ups</h2>")[1].split("<h2")[0]
        self.assertIn("done, 2 follow-ups open", fu)
        self.assertIn('href="/t/T-004"', fu)
        self.assertIn("proposed", fu)
        self.assertIn("href=\"/t/T-005\"", fu)
        f = self.task_file("T-004")
        f.write_text(re.sub(r"(?m)^status: \w+$", "status: dropped", f.read_text()))
        f = self.task_file("T-005")
        f.write_text(re.sub(r"(?m)^status: \w+$", "status: done", f.read_text()))
        _, _, board = self.req("GET", "/")
        self.assertNotIn("<h2 id=followups>", board)
        _, _, page = self.req("GET", "/t/T-001")
        self.assertIn("dropped", page.split("<h2>Follow-ups</h2>")[1])  # listed, but not open

    def test_category_shown(self):
        f = self.task_file("T-004")
        f.write_text(re.sub(r"(?m)^category:.*$", "category: bugfix", f.read_text()))
        _, _, board = self.req("GET", "/")
        self.assertIn("bugfix", re.search(r'<li><a class=id href="/t/T-004">.*?</li>', board, re.S)[0])
        self.assertIn("feature", re.search(r'<li><a class=id href="/t/T-005">.*?</li>', board, re.S)[0])  # unset
        _, _, page = self.req("GET", "/t/T-004")
        self.assertIn("<span class=tier>bugfix</span>", page)
        _, _, review = self.req("GET", "/review")
        card = re.search(r'<section class=card id="T-004">(.*?)</section>', review, re.S)[1]
        self.assertIn(">bugfix<", card)

    def test_review_lists_proposed_epic(self):
        self.ergon("new", "epic", "Evil epic " + HOSTILE)
        f = next(p for p in (self.root / "tasks" / "epics").rglob("*.md") if "E-002" in str(p))
        f.write_text(f.read_text().replace("## Goal\n", f"## Goal\n{HOSTILE}\n", 1))
        _, _, review = self.req("GET", "/review")
        sec = review.split("<h2 id=epics>")[1].split("<h2")[0]
        self.assertIn("E-002", sec)
        self.assertIn("&lt;script&gt;", sec)
        self.assertNotIn("<script>alert", sec)
        self.assertNotIn("<img src=x", sec)
        self.assertIn("ergon approve E-001 E-002", sec)
        self.assertNotIn("Nothing is waiting", review)

    def test_priority_on_board_and_task_page(self):
        f = self.task_file("T-004")
        t = f.read_text()
        t = t.replace("\ntier:", '\ncod: 80\ncod_why: "<b>x</b> see T-001"\ngraded: Tester 2026-11-17\npin: first\ntier:', 1)
        f.write_text(t)
        _, _, board = self.req("GET", "/")
        li = re.search(r'<li><a class=id href="/t/T-004">.*?</li>', board, re.S)[0]
        self.assertIn('class="score"', li)
        self.assertIn("pin: first", li)
        _, _, page = self.req("GET", "/t/T-004")
        self.assertIn("<h2>Priority</h2>", page)
        self.assertIn("cod 80, unblocks 0, size 1", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; see", page)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn('<a href="/t/T-001">T-001</a>', page.split("cod_why")[1])
        self.assertIn("Tester 2026-11-17", page)
        _, _, page = self.req("GET", "/t/T-005")
        self.assertIn("not graded (neutral 50)", page)

    def test_board_priority_and_reviews(self):
        f = self.task_file("T-004")
        f.write_text(f.read_text().replace("\ntier:", '\ncod: 80\ncod_why: "<b>x</b> see T-001"\ntier:', 1))
        grade = self.runs / "grade"
        grade.mkdir()
        (grade / "grade-20261117-120000.json").write_text(json.dumps(
            {"generated": "2026-11-17T12:00:00+00:00", "model": "sonnet", "cost": 0.08, "written": ["T-004"]}))
        _, _, board = self.req("GET", "/")
        prio = board.split("<h2 id=priority>")[1].split("<h2")[0]
        self.assertIn("top 2 of 2 not started", prio)
        self.assertLess(prio.index("/t/T-004"), prio.index("/t/T-005"))  # 80/1 beats an ungraded human task
        self.assertIn("&lt;b&gt;x&lt;/b&gt; see", prio)
        self.assertNotIn("<b>x</b>", prio)
        self.assertIn("not graded (neutral 50)", prio)  # T-005
        self.assertIn("last graded 2026-11-17T12:00:00+00:00 by sonnet · 1 task(s) · $0.08", prio)
        li = re.search(r'<li><a class=id href="/t/T-004">.*?</li>', board, re.S)[0]
        self.assertIn("p 80", li)
        self.assertIn("cod 80", li)
        revs = board.split("<h2 id=reviews>")[1].split("<h2")[0]
        self.assertEqual(re.findall(r'href="/t/(T-\d+)"', revs), ["T-002", "T-001", "T-001"])  # newest first
        self.assertIn('<td class="v-pass"><b>pass</b></td>', revs)
        self.assertIn("missing &lt;tests&gt;", revs)
        self.assertIn('href="/runs/T-001-20261116-120600-review.json"', revs)

    def test_board_without_reviews_or_grades(self):
        (self.runs / "history.jsonl").write_text("")
        _, _, board = self.req("GET", "/")
        self.assertIn("no review has run yet", board.split("<h2 id=reviews>")[1])
        self.assertIn("nothing graded yet", board.split("<h2 id=priority>")[1].split("<h2")[0])

    def test_task_body_is_escaped(self):
        code, _, page = self.req("GET", "/t/T-004")
        self.assertEqual(code, 200)
        self.assertNotIn("<script", page)  # a task page carries no script at all
        self.assertNotIn("<img", page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", page)
        self.assertIn("<strong>bold</strong>", page)  # the markdown subset still works
        self.assertIn("<code>&lt;b&gt;code&lt;/b&gt;</code>", page)
        self.assertIn('<a href="/t/T-001">T-001</a>', page)

    def add_options(self, tid, *opts):
        f = self.task_file(tid)
        f.write_text(f.read_text().rstrip("\n") + "\n\n## Options\n"
                     + "".join(f"{n}. {o}" + (" (recommended)" if n == 1 else "") + "\n" for n, o in enumerate(opts, 1)))

    def add_lens_options(self, tid, crux, *opts):
        f = self.task_file(tid)
        f.write_text(f.read_text().rstrip("\n") + f"\n\n## Options\nCrux: {crux}\n" + "".join(
            f"- {lens}: {approach}\n  Optimizes for: {o}\n  Gives up: {g}\n  Regret if: {r}\n"
            for lens, approach, o, g, r in opts))

    def test_lens_options_shown_with_no_recommended_mark(self):
        """T-262: a lens-format ## Options shows its crux and each option's fields in lens order on
        /review and /t/T-NNN, nothing marked recommended, and the choose commands count in that order."""
        self.add_lens_options("T-004", "whether the page stays one <ol>",
                              ("risk", "a second list for fields", "a flat section",
                               "a nested list", "fields read as options too"),
                              ("minimal", "fields inside each <li>", "one list, few rules",
                               "a deeper <li>", "an option field hides"))
        for path in ("/review", "/t/T-004"):
            _, _, page = self.req("GET", path)
            self.assertIn("<p class=crux>Crux: whether the page stays one &lt;ol&gt;</p>", page)
            sec = page.split('<ol class=options>')[1].split("</ol>")[0]
            self.assertLess(sec.index("minimal: fields inside each &lt;li&gt;"),
                            sec.index("risk: a second list for fields"))  # LENSES order, not written order
            self.assertIn("Optimizes for: one list, few rules", sec)
            self.assertIn("Gives up: a deeper &lt;li&gt;", sec)
            self.assertIn("Regret if: an option field hides", sec)
            self.assertNotIn("recommended", sec)
            self.assertIn("ergon choose T-004 1</code>", page)
            self.assertIn("ergon choose T-004 2</code>", page)
        self.ergon("choose", "T-004", "1", inp="y\n", env=dict(self.env, ERGON_ASK_STDIN="1"))
        _, _, page = self.req("GET", "/t/T-004")
        li = page.split('<li class="option chosen">')[1].split("</li>")[0]
        self.assertIn("minimal: fields inside each &lt;li&gt;", li)  # 1 counts in lens order
        self.assertIn("chosen by", li)

    def test_options_shown_with_choose_commands(self):
        """T-194: /review and /t/T-NNN list a task's options, the recommended one marked, one `ergon choose`
        command each plus --own; after a choice, the chosen mark shows and the commands are gone."""
        self.add_options("T-004", "plain <script>alert(1)</script> approach", "second approach")
        for path in ("/review", "/t/T-004"):
            _, _, page = self.req("GET", path)
            sec = page.split('<ol class=options>')[1].split("</ol>")[0]
            self.assertIn("plain &lt;script&gt;alert(1)&lt;/script&gt; approach", sec)
            self.assertIn("second approach", sec)
            self.assertNotIn("<script>alert(1)", page)
            self.assertEqual(sec.count("recommended"), 1)
            self.assertIn("ergon choose T-004 1</code>", page)
            self.assertIn("ergon choose T-004 2</code>", page)
            self.assertIn("ergon choose T-004 --own", page)
        _, _, review = self.req("GET", "/review")
        appr = review.split("<h2>Approve</h2>")[1]
        self.assertNotIn("ergon approve T-004", appr)
        self.assertRegex(appr, r"Choose first.*T-004")
        _, _, board = self.req("GET", "/")
        self.assertIn("Awaiting choice (1)", board)
        self.assertRegex(board, r'id="count-choice">1<')
        self.ergon("choose", "T-004", "2", inp="y\n", env=dict(self.env, ERGON_ASK_STDIN="1"))
        for path in ("/review", "/t/T-004"):
            _, _, page = self.req("GET", path)
            self.assertIn("chosen by", page)
            self.assertNotIn("ergon choose T-004", page)
        _, _, review = self.req("GET", "/review")
        self.assertIn("ergon approve T-004", review.split("<h2>Approve</h2>")[1])
        _, _, board = self.req("GET", "/")
        self.assertNotIn("Awaiting choice", board)

    def test_findings_flag_a_feature_without_options(self):
        f = self.task_file("T-004")
        f.write_text(re.sub(r"^category: \w+\n", "", f.read_text(), flags=re.M).replace("\ntier:", "\ncategory: feature\ntier:", 1))
        _, _, review = self.req("GET", "/review")
        self.assertIn("a proposed feature with no ## Options", review)
        self.add_options("T-004", "a", "b")
        _, _, review = self.req("GET", "/review")
        self.assertIn("has ## Options and no choice yet", review)

    def test_only_get_and_head(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND"):
            for path in ("/", "/t/T-001", "/runs/T-001-20261116-120000.json"):
                code, headers, _ = self.req(method, path, body="status=done")
                self.assertEqual(code, 405, (method, path))
                self.assertEqual(headers.get("Allow"), "GET, HEAD")
        code, headers, body = self.req("HEAD", "/t/T-001")
        self.assertEqual((code, body), (200, ""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(self.snapshot(), self.before)  # nothing written, not even a transcript cache

    def test_logs(self):
        code, headers, body = self.req("GET", "/runs/T-001-20261116-120600-review.json")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        self.assertEqual(body, "<script>not html</script>\n")
        self.assertEqual(self.req("GET", "/runs/history.jsonl")[0], 200)
        for path in ("/runs/../../.ergon.toml", "/runs/..%2f..%2f.ergon.toml", "/runs/%2e%2e/secret.txt",
                     "/runs/..%2Fsecret.txt", "/runs/..", "/runs/.", "/runs/", "/runs/T-001-link.json",
                     "/runs/nope.json", "/runs//etc/passwd", "/runs/%00", "/../.ergon.toml", "/tasks/secret.txt",
                     "/.ergon.toml", "/t/../.ergon.toml", "http://evil/runs/history.jsonl"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_host_header(self):
        self.assertEqual(self.req("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/live.json", host=f"rebind.example:{self.port}")[0], 403)
        self.assertEqual(self.req("GET", "/", host=f"localhost:{self.port}")[0], 200)
        self.assertEqual(self.req("GET", "/", host="100.64.1.2:4300")[0], 200)  # a tailnet IP
        self.assertEqual(self.req("GET", "/", host="[::1]:4300")[0], 200)


FAKE_QUIZ_CLAUDE = r"""#!PYTHON
import json, os, sys
tmp = os.environ["QUIZ_TMP"]
open(os.path.join(tmp, "quiz-prompt.txt"), "w").write(sys.stdin.read())
open(os.path.join(tmp, "quiz-argv.json"), "w").write(json.dumps(sys.argv[1:]))
qs = [
    {"task": "T-004", "question": "What does T-004 <b>change</b>?", "options": ["The board", "Nothing", "<i>prod</i>"],
     "answer": 0, "explanation": "Its What says the board."},
    {"task": "T-005", "question": "Who answers T-005?", "options": ["A worker", "A human"], "answer": 1,
     "explanation": "It is a human task."},
    {"task": "T-001", "question": "Not asked about", "options": ["a", "b"], "answer": 0, "explanation": ""},
    {"task": "T-004", "question": "Broken", "options": ["only one"], "answer": 3},
]
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.02,
                  "result": "Here you go:\n```json\n" + json.dumps(qs) + "\n```"}))
"""
APPROVE = "ergon " + "approve"  # spelled apart: the guard hook reads this file's edits as commands


class TestReview(ServeFixture):
    """/review over the fixture's two proposed tasks: T-004 (hostile title, a filled-in brief that
    cites a missing file) and T-005 (the bare template: empty Done when, tier_why and touches)."""

    def setUp(self):
        super().setUp()
        f = self.task_file("T-004")
        text = f.read_text()
        text = re.sub(r"^depends:.*$", "depends: [T-001]", text, flags=re.M)
        text = re.sub(r"^touches:.*$", "touches: [README.md, ergon/new_thing.py]", text, flags=re.M)
        text = re.sub(r"^tier_why:.*$", "tier_why: one file", text, flags=re.M)
        text = text.replace("## What\n", "## What\nChange the board, see ergon/nowhere.py:12 and README.md:1 "
                            "and README.md:40 (https://example.com/a/b.py:3).\n", 1)
        text = text.replace("## Done when\n", "## Done when\n- the board shows it\n", 1)
        f.write_text(text)
        f = self.task_file("T-005")
        f.write_text(re.sub(r"^depends:.*$", "depends: [T-004, T-099]", f.read_text(), flags=re.M))
        self.before = self.snapshot()

    def fake_claude(self):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "claude").write_text(FAKE_QUIZ_CLAUDE.replace("#!PYTHON", f"#!{sys.executable}"))
        (bindir / "claude").chmod(0o755)
        return dict(self.env, PATH=f"{bindir}{os.pathsep}{self.env['PATH']}", QUIZ_TMP=self.tmp.name)

    def card(self, page, tid):
        m = re.search(rf'<section class=card id="{tid}">(.*?)</section>', page, re.S)
        return m[1] if m else None

    def test_independent_ordered_by_score_and_ungraded_finding(self):
        # T-004 and T-005 are linked; add two independent tasks, the cheaper-per-size one graded higher
        self.ergon("new", "task", "Big", "--tier", "opus", "--epic", "E-001")
        self.ergon("new", "task", "Small", "--tier", "haiku", "--epic", "E-001")
        for tid, cod in (("T-006", 100), ("T-007", 20)):
            f = self.task_file(tid)
            f.write_text(f.read_text().replace("\ntier:", f"\ncod: {cod}\ntier:", 1))
        _, _, page = self.req("GET", "/review")
        cards = re.findall(r'<section class=card id="(T-\d+)">', page)
        self.assertLess(cards.index("T-007"), cards.index("T-006"))  # 20/1 beats 100/8
        prio = page.split("<h2 id=priority>")[1].split("<h2")[0]
        self.assertEqual(re.findall(r'<a href="#(T-\d+)">', prio), ["T-004", "T-007", "T-005", "T-006"])
        self.assertIn("20.0 <span class=muted>(cod 20, unblocks 0, size 1)</span>", self.card(page, "T-007"))
        self.assertIn("not graded (neutral 50)", self.card(page, "T-004"))
        self.assertEqual(cards[:2], ["T-004", "T-005"])  # the chain stays first, in dependency order
        find = re.search(r'<div class=finding id="find-T-005">.*?</div>', page, re.S)[0]
        self.assertIn("not graded", find)
        self.assertNotIn("not graded", re.search(r'<div class=finding id="find-T-006">.*?</div>', page, re.S)[0]
                         if 'id="find-T-006"' in page else "")

    def test_lists_proposed_only(self):
        code, headers, page = self.req("GET", "/review")
        self.assertEqual(code, 200)
        self.assertEqual(re.findall(r'<section class=card id="(T-\d+)">', page), ["T-004", "T-005"])
        self.assertIn("<h1>Proposed tasks (2)</h1>", page)
        self.assertIn("<b>1</b>haiku", page)
        self.assertIn("<b>1</b>human", page)
        self.assertIn("1 of 2 have every dependency done", page)  # T-004's T-001 is done; T-005 waits
        self.assertIn('<a class=more href="/review">', self.req("GET", "/")[2])  # the board links it
        # the map: each proposed box links to its card, a non-proposed dependency to its page,
        # a missing one nowhere; T-005 has findings and is marked
        svg = re.search(r"<svg class=depmap.*?</svg>", page, re.S)[0]
        for tid in ("T-004", "T-005"):
            self.assertRegex(svg, rf'<a href="#{tid}"><g class="box st-proposed[^"]*">')
        self.assertIn('<a href="/t/T-001"><g class="box st-done">', svg)
        self.assertIn('<g class="box st-missing">', svg)
        self.assertNotIn('href="#T-099"', svg)
        self.assertNotIn('href="#T-001"', svg)
        self.assertIn('<a href="#T-005"><g class="box st-proposed flag">', svg)
        self.assertEqual(svg.count("class=edge"), 3)
        # the cards
        c4 = self.card(page, "T-004")
        self.assertIn("one file", c4)
        self.assertIn("<code>README.md</code>", c4)
        self.assertIn("<h3>Done when</h3><ul><li>the board shows it</li></ul>", c4)
        self.assertIn('<a href="/t/T-001">T-001</a> <span class=muted>(done)</span>', c4)
        self.assertIsNone(self.card(page, "T-001"))  # only a box on the map
        # it never approves: a command to copy, nothing to submit
        self.assertIn(f"<code class=cmd>{APPROVE} T-004 T-005</code>", page)
        self.assertNotIn("<form", page)

    def test_dep_map_per_epic(self):
        # one map per epic, then the tasks with no epic; a dependency in another epic is drawn in both
        self.ergon("new", "epic", "Second " + HOSTILE)
        self.ergon("new", "task", "Other epic", "--tier", "haiku", "--epic", "E-002")
        self.ergon("new", "task", "Loose", "--tier", "haiku")
        f = self.task_file("T-006")
        f.write_text(re.sub(r"^depends:.*$", "depends: [T-005]", f.read_text(), flags=re.M))
        page = self.req("GET", "/review")[2]
        sec = page.split("<h2>Dependency map</h2>")[1].split("<h2")[0]
        self.assertEqual(re.findall(r'<h3 class=mhead id="?(map-[\w-]+)', sec), ["map-E-001", "map-E-002", "map-none"])
        self.assertNotIn(HOSTILE, sec)
        svgs = re.findall(r"<svg class=depmap.*?</svg>", sec, re.S)
        self.assertEqual(len(svgs), 3)
        boxes = [re.findall(r'<text class=bid [^>]*>(T-\d+)</text>', s) for s in svgs]
        self.assertEqual(sorted(boxes[0]), ["T-001", "T-004", "T-005", "T-099"])
        self.assertEqual(sorted(boxes[1]), ["T-005", "T-006"])  # T-005 is E-001's, drawn here too
        self.assertEqual(boxes[2], ["T-007"])
        self.assertIn('<a href="#T-005"><g class="box st-proposed flag">', svgs[1])
        self.assertEqual(page.count("<h3 class=ghead"), page.split("<h2>Tasks</h2>")[1].count("<h3 class=ghead"))

    def test_findings(self):
        page = self.req("GET", "/review")[2]
        f4 = re.search(r'<div class=finding id="find-T-004">.*?</div>', page, re.S)[0]
        self.assertIn("cites ergon/nowhere.py:12: no such file in the main checkout", f4)
        self.assertIn("cites README.md:40: the file has 2 lines", f4)
        self.assertNotIn("README.md:1:", f4)  # that line exists
        self.assertNotIn("b.py", f4)  # a URL isn't a citation
        self.assertIn("touches ergon/new_thing.py: not in the main checkout", f4)
        self.assertNotIn("touches README.md", f4)
        self.assertNotIn("Done when is empty", f4)
        f5 = re.search(r'<div class=finding id="find-T-005">.*?</div>', page, re.S)[0]
        for want in ("Done when is empty", "tier_why is empty", "touches is empty",
                     "depends on T-099: no such task"):
            self.assertIn(want, f5)
        self.assertNotIn("depends on T-004", f5)  # proposed, not dead

    def test_escaped(self):
        code, headers, page = self.req("GET", "/review")
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", headers["Content-Security-Policy"])[1]
        self.assertEqual(re.findall(r"<script[^>]*>", page), [f'<script nonce="{nonce}">'])
        self.assertNotIn("<img", page)
        self.assertNotIn('href="javascript:', page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", page)

    def test_read_only(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            code, headers, _ = self.req(method, "/review", body="ids=T-004")
            self.assertEqual(code, 405, method)
            self.assertEqual(headers.get("Allow"), "GET, HEAD")
        code, headers, body = self.req("HEAD", "/review")
        self.assertEqual((code, body), (200, ""))
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(self.req("GET", "/review")[0], 200)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(self.status("T-004"), "proposed")

    def test_quiz(self):
        page = self.req("GET", "/review")[2]
        self.assertIn("No quiz yet. Run <code>ergon quiz</code>", page)
        self.assertNotIn("<div id=quiz", page)

        env = self.fake_claude()
        out = self.ergon("quiz", env=env).stdout
        self.assertIn("2 new question(s)", out)
        self.assertIn("2 unusable dropped", out)  # the one on T-001 and the broken one
        argv = json.loads((Path(self.tmp.name) / "quiz-argv.json").read_text())
        self.assertEqual(argv[:5], ["-p", "--model", "sonnet", "--tools", ""])  # no tools at all
        prompt = (Path(self.tmp.name) / "quiz-prompt.txt").read_text()
        self.assertIn("=== T-004 ===", prompt)
        self.assertIn("=== T-005 ===", prompt)
        self.assertNotIn("Done thing", prompt)  # T-001 isn't proposed
        [f] = list((self.runs / "quiz").glob("quiz-*.json"))
        data = json.loads(f.read_text())
        self.assertEqual([q["task"] for q in data["questions"]], ["T-004", "T-005"])
        self.assertEqual(data["questions"][0]["hash"], S.brief_hash(S._tasks.parse(self.task_file("T-004"))))

        page = self.req("GET", "/review")[2]
        self.assertIn('<div id=quiz class=quiz data-total="2">', page)
        self.assertIn("What does T-004 &lt;b&gt;change&lt;/b&gt;?", page)
        self.assertIn('<button type=button class=opt data-i="2">&lt;i&gt;prod&lt;/i&gt;</button>', page)
        self.assertIn('<div class=q data-answer="1">', page)
        self.assertIn("Who answers T-005?", page)

        # T-005's brief changes after the quiz was written: its question goes, T-004's stays
        f5 = self.task_file("T-005")
        f5.write_text(f5.read_text().replace("## What\n", "## What\nNow something else.\n", 1))
        page = self.req("GET", "/review")[2]
        self.assertNotIn("Who answers T-005?", page)
        self.assertIn("What does T-004 &lt;b&gt;change&lt;/b&gt;?", page)
        self.assertIn("1 older question(s) hidden", page)
        self.assertIn('data-total="1"', page)

        # a second run asks only about T-005 and keeps T-004's question
        (Path(self.tmp.name) / "quiz-prompt.txt").unlink()
        out = self.ergon("quiz", env=env).stdout
        self.assertIn("1 new question(s), 1 kept", out)
        prompt = (Path(self.tmp.name) / "quiz-prompt.txt").read_text()
        self.assertNotIn("=== T-004 ===", prompt)
        self.assertIn("Now something else.", prompt)
        page = self.req("GET", "/review")[2]
        self.assertIn("Who answers T-005?", page)
        self.assertIn('data-total="2"', page)

        # once T-004 leaves proposed, its question goes too, and it has no card
        stamp(self.task_file("T-004"))
        page = self.req("GET", "/review")[2]
        self.assertNotIn("What does T-004", page)
        self.assertIsNone(self.card(page, "T-004"))
        self.assertIn("already has current questions", self.ergon("quiz", env=env).stdout)

    def test_quiz_failure_writes_nothing(self):
        env = self.fake_claude()
        bindir = Path(self.tmp.name) / "bin"
        (bindir / "claude").write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'result': 'sorry, no'}}))\n")
        r = self.ergon("quiz", env=env, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing written", r.stderr)
        self.assertFalse((self.runs / "quiz").exists())

    def test_quiz_claude_error_names_quiz(self):
        # the claude -p call is grade.call, shared with `ergon grade`; its messages name the caller
        env = self.fake_claude()
        bindir = Path(self.tmp.name) / "bin"
        (bindir / "claude").write_text(f"#!{sys.executable}\nimport json\n"
                                       f"print(json.dumps({{'is_error': True, 'result': 'over budget'}}))\n")
        r = self.ergon("quiz", env=env, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ergon quiz: claude failed (over budget); nothing written", r.stderr)
        self.assertFalse((self.runs / "quiz").exists())


class TestServeUnits(unittest.TestCase):
    def test_quiz_items(self):
        from ergon import cli
        q = [{"task": "T-001"}]
        for text in (json.dumps(q), "```json\n" + json.dumps(q) + "\n```", "Sure:\n" + json.dumps(q) + "\nDone."):
            self.assertEqual(cli.quiz_items(text), q, text)
        self.assertIsNone(cli.quiz_items("no json here"))
        self.assertIsNone(cli.quiz_items('{"task": "T-001"}'))

    def test_loopback(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost"):
            self.assertTrue(S.is_loopback(host), host)
        for host in ("0.0.0.0", "::", "100.64.1.2", "192.168.1.5", "example.com", ""):
            self.assertFalse(S.is_loopback(host), host)

    def test_host_ok(self):
        for h in ("127.0.0.1", "127.0.0.1:4300", "localhost:4300", "[::1]:4300", "100.101.102.103:4300"):
            self.assertTrue(S.host_ok(h), h)
        for h in ("", "evil.example", "evil.example:4300", "127.0.0.1.nip.io", "[::1]x", "127.0.0.1:x"):
            self.assertFalse(S.host_ok(h), h)

    def test_non_loopback_bind_warns(self):
        class Args:
            bind, port = "100.64.1.2", 4300
        err = io.StringIO()
        real = S.make_server

        def refuse(bind, port):
            raise OSError("not here")
        S.make_server = refuse
        try:
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                S.serve(Args)
        finally:
            S.make_server = real
        self.assertIn("WARNING: listening on 100.64.1.2, not loopback", err.getvalue())
        Args.bind = "127.0.0.1"
        err = io.StringIO()
        S.make_server = refuse
        try:
            with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                S.serve(Args)
        finally:
            S.make_server = real
        self.assertNotIn("WARNING", err.getvalue())

    def test_markdown_passes_no_html(self):
        out = S.markdown("# <h1>x</h1>\n\n```\n<script>\n```\n- <a href=x>y</a>\n1. **<i>z</i>**", {})
        self.assertNotRegex(out, r"<(?!/?(h2|pre|code|ul|ol|li|p|strong|br)\b)")
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>&lt;i&gt;z&lt;/i&gt;</strong>", out)


class TestEpicTimeline(ServeFixture):
    """/e/E-001 from a fixture journal: T-001 reviewed with a fix round then landed, T-002 blocked
    then retried then landed, T-003 asked, was answered, and waited to be accepted."""

    def at(self, hm):
        return f"2026-11-16T{hm}:00+00:00"

    def journal(self):
        a = self.at
        rows = [
            row(a("10:00"), "T-001", "created"), row(a("10:00"), "T-002", "created"), row(a("10:00"), "T-003", "created"),
            row(a("11:00"), "T-001", "approved"), row(a("10:30"), "T-002", "approved"), row(a("10:30"), "T-003", "approved"),
            row(a("11:30"), "T-001", "dispatched", tier="sonnet", worktree=".claude/worktrees/T-001"),
            row(a("11:40"), "T-001", "worker_end", tier="sonnet", verdict="done", cost=0.5, seconds=600, session="s-1"),
            row(a("11:41"), "T-001", "verify", ok=True, seconds=30),
            row(a("11:42"), "T-001", "review", verdict="fail", reviewer="opus", reason="no <tests>", cost=0.25, seconds=60),
            row(a("11:45"), "T-001", "resumed", session="s-1", round=2, tier="sonnet"),
            row(a("11:50"), "T-001", "worker_end", tier="sonnet", verdict="done", cost=0.125, seconds=300, round=2),
            row(a("11:52"), "T-001", "review", verdict="pass", reviewer="opus", cost=0.125, seconds=60, round=2),
            row(a("11:53"), "T-001", "landed", base="epic/E-001"),
            row(a("11:00"), "T-002", "dispatched", tier="haiku"),
            row(a("11:10"), "T-002", "worker_end", tier="haiku", verdict="blocked", reason="needs the schema", cost=0.25, seconds=600),
            row(a("11:10"), "T-002", "blocked", reason="needs the schema"),
            row(a("12:00"), "T-002", "status", to="ready"),
            row(a("12:05"), "T-002", "dispatched", tier="haiku"),
            row(a("12:15"), "T-002", "worker_end", tier="haiku", verdict="done", cost=0.5, seconds=600),
            row(a("12:16"), "T-002", "landed", base="epic/E-001"),
            row(a("11:00"), "T-003", "dispatched", tier="opus"),
            row(a("11:05"), "T-003", "worker_end", tier="opus", verdict="asking", cost=1.0, seconds=300),
            row(a("11:05"), "T-003", "question", questions=[{"text": "which db?", "options": []}]),
            row(a("11:30"), "T-003", "answered", by="p"),
            row(a("11:31"), "T-003", "resumed", session="s", tier="opus", after="answer"),
            row(a("11:40"), "T-003", "worker_end", tier="opus", verdict="done", cost=1.0, seconds=540),
            row(a("11:41"), "T-003", "review_hold"),
            row(a("12:30"), "T-003", "accepted", by="p"),
            row(a("12:31"), "T-003", "landed", base="epic/E-001"),
        ]
        (self.runs / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return rows

    def test_phases_and_boundaries(self):
        from ergon import timeline as TL
        rows = self.journal()
        tl = TL.build(rows, {}, "E-001", TL.ts_of(self.at("13:00")))
        self.assertEqual([ln["id"] for ln in tl["lanes"]], ["T-001", "T-002", "T-003"])
        got = {ln["id"]: [(p["kind"], p["label"], p["outcome"], p["human"],
                           (p["start"] - tl["t0"]) / 60, (p["end"] - tl["t0"]) / 60) for p in ln["phases"]]
               for ln in tl["lanes"]}
        # minutes from 10:00
        self.assertEqual(got["T-001"], [
            ("approval", "waiting for approval", "ok", True, 0, 60),
            ("queued", "queued", "ok", False, 60, 90),
            ("worker", "worker", "ok", False, 90, 100),
            ("verify", "verify", "ok", False, 100.5, 101),
            ("review", "review", "fail", False, 101, 102),
            ("worker", "fix round 2", "ok", False, 105, 110),
            ("review", "review 2", "ok", False, 111, 112),
            ("landed", "landed", "ok", False, 113, 113)])
        # blocked then retried: the wait for a human ends when the task was set ready
        self.assertEqual([x[:5] for x in got["T-002"] if x[0] in ("worker", "blocked")], [
            ("worker", "worker", "fail", False, 60), ("blocked", "blocked", "ok", True, 70),
            ("worker", "worker", "ok", False, 125)])
        self.assertEqual([x[5] for x in got["T-002"] if x[0] == "blocked"], [120])
        kinds3 = [(x[0], x[3], x[4], x[5]) for x in got["T-003"]]
        self.assertIn(("asking", True, 65, 90), kinds3)
        self.assertIn(("hold", True, 101, 150), kinds3)
        self.assertEqual(kinds3[-1][0], "landed")
        self.assertEqual([c[3] for c in tl["costs"]], sorted(c[3] for c in tl["costs"]))
        self.assertAlmostEqual(tl["costs"][-1][3], 3.75)

    def test_page(self):
        self.journal()
        code, headers, page = self.req("GET", "/e/E-001")
        self.assertEqual(code, 200)
        self.assertEqual(page.count('class="lane'), 3)  # one lane per task
        self.assertEqual(len(re.findall(r'<a href="/t/T-00\d"><text', page)), 3)
        self.assertEqual(len(re.findall(r'<rect class="seg k-worker', page)), 6)
        self.assertEqual(len(re.findall(r'<rect class="seg [^"]* human', page)), 6)  # approvals x3, asking, hold, blocked
        self.assertIn("waiting on a human", page)
        self.assertIn("no &lt;tests&gt;", page)
        self.assertIn("which db?", page)
        self.assertIn('id=play', page)
        self.assertIn('<option value=16>', page)
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", headers["Content-Security-Policy"])[1]
        self.assertEqual(re.findall(r"<script[^>]*>", page), [f'<script nonce="{nonce}">'])
        self.assertEqual(self.req("GET", "/e/E-009")[0], 404)
        self.assertIn('href="/e/E-001"', self.req("GET", "/")[2])
        self.assertIn('(<a href="/e/E-001">E-001</a>', self.req("GET", "/")[2])
        self.assertEqual(self.snapshot().keys() - self.before.keys(), set())

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_replay_reaches_the_final_state(self):
        self.journal()
        page = self.req("GET", "/e/E-001")[2]
        span = float(re.search(r'data-span="([\d.]+)"', page)[1])
        segs = [(float(a), float(b), float(c)) for a, b, c in
                re.findall(r'class="seg[^>]*data-s="([\d.]+)" data-e="([\d.]+)" data-w="([\d.]+)"', page)]
        self.assertGreater(len(segs), 10)
        js = (Path(S.__file__).parent / "epic.js").read_text()
        prog = ("const {reveal}=require(process.argv[1]);const segs=JSON.parse(process.argv[2]);const span=+process.argv[3];"
                "let ok=true;for(const [s,e,w] of segs){ if(reveal(s,e,w,span)!==w) ok=false;"
                " if(reveal(s,e,w,-1)!==null) ok=false;"
                " if(e>s){const m=reveal(s,e,w,(s+e)/2); if(!(m>0&&m<w)) ok=false;}}"
                "console.log(ok?'final':'broken')")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "epic.js").write_text(js)
            out = subprocess.run(["node", "-e", prog, str(Path(d) / "epic.js"), json.dumps(segs), str(span)],
                                 capture_output=True, text=True, cwd=d)
        self.assertEqual(out.stdout.strip(), "final", out.stderr)


if __name__ == "__main__":
    unittest.main()


class TestMetrics(ServeFixture):
    def test_metrics_matches_stats(self):
        code, headers, page = self.req("GET", "/metrics")
        self.assertEqual(code, 200)
        self.assertIn("<svg", page)
        self.assertIn("Landed per week", page)
        self.assertIn("2026-W47", page)
        stats = self.ergon("stats").stdout
        row = next(l for l in stats.splitlines() if l.startswith("sonnet")).split()
        self.assertIn(f"<td>sonnet</td><td class=num>{row[1]}</td><td class=num>{row[2]}</td>"
                      f"<td class=num>{row[3]}</td><td class=num>{row[4]}</td><td class=num>{row[5]}</td>", page)
        self.assertIn("<td>sonnet</td><td class=num>1</td><td class=num>1</td>", page)
        # the same range, filtered: nothing before the fixture's day
        _, _, empty = self.req("GET", "/metrics?until=2020-01-01")
        self.assertIn("No runs yet", empty)
        self.assertEqual(self.snapshot(), self.before)

    def test_metrics_trust_pyramid(self):
        _, _, page = self.req("GET", "/metrics")
        self.assertIn("Trust pyramid", page)
        self.assertIn("caught by review + human", page)
        self.assertIn("<title>review: 2</title>", page)  # the fixture's two failed reviews

    def test_metrics_veto_and_filter(self):
        _, _, page = self.req("GET", "/metrics?epic=E-001&since=2026-11-16")
        self.assertIn("veto rate", page)
        self.assertIn("rounds that fixed it", page)
        _, _, hostile = self.req("GET", "/metrics?epic=%3Cscript%3E&since=%22%3E")
        self.assertNotIn("<script>alert", hostile)


class TestMetricsEmpty(Project):
    def test_empty_state(self):
        from ergon import metrics
        m = metrics.compute({}, [], self.root / "nothing")
        self.assertTrue(m["empty"])
        body = metrics.render(m, S.esc, S.money, {"epic": None, "since": None, "until": None, "epics": []})
        self.assertIn("No runs yet", body)


class TestReviewGroups(ServeFixture):
    """Grouping of proposed tasks by depends: T-004 and T-005 start out unlinked."""

    def setUp(self):
        super().setUp()
        for n in range(4):
            self.ergon("new", "task", f"Extra {n}", "--tier", "sonnet")  # T-006..T-009

    def link(self, tid, deps, status=None):
        f = self.task_file(tid)
        text = re.sub(r"^depends:.*$", f"depends: [{', '.join(deps)}]", f.read_text(), flags=re.M)
        if status:
            text = re.sub(r"^status: \w+$", f"status: {status}", text, flags=re.M)
        f.write_text(text)

    def order(self, page):
        return re.findall(r'<section class=card id="(T-\d+)">', page)

    def region(self, page, low):
        m = re.search(rf'<h3 class=ghead id="g-{low}">.*?(?=<h3 class=ghead|<h2)', page, re.S)
        return m[0] if m else None

    def test_chain_independent_and_done_base(self):
        # T-006 <- T-007 <- T-008 chain; T-004 independent; T-009 depends on done T-001
        self.link("T-007", ["T-006"])
        self.link("T-008", ["T-007"])
        self.link("T-009", ["T-001"])
        page = self.req("GET", "/review")[2]
        order = self.order(page)
        self.assertEqual(order[:3], ["T-006", "T-007", "T-008"])
        self.assertEqual(sorted(order[3:]), ["T-004", "T-005", "T-009"])
        g = self.region(page, "T-006")
        self.assertIn("T-006</a> → <a", g)
        self.assertIn("3 linked tasks, no epic", g)
        self.assertIn(f"<code class=cmd>{APPROVE} T-006 T-007 T-008</code>", g)
        self.assertIn("Independent", page)
        self.assertEqual(page.count("<h3 class=ghead"), 2)
        self.assertNotIn('class=card id="T-001"', page)

    def test_shared_ready_joins_shared_done_does_not(self):
        self.link("T-006", ["T-002"], "proposed")  # T-002 is doing
        self.link("T-007", ["T-002"])
        page = self.req("GET", "/review")[2]
        g = self.region(page, "T-006")
        self.assertIn("waits on", g)
        self.assertIn("T-002</a> (doing)", g)
        self.assertIn(f"<code class=cmd>{APPROVE} T-006 T-007</code>", g)
        self.link("T-006", ["T-001"])
        self.link("T-007", ["T-001"])
        page = self.req("GET", "/review")[2]
        self.assertNotIn("<h3 class=ghead", page)
        self.assertEqual(len(self.order(page)), 6)

    def test_finding_cycle_and_hostile(self):
        self.link("T-006", ["T-007"])
        self.link("T-007", ["T-006"])
        code, _, page = self.req("GET", "/review")
        self.assertEqual(code, 200)
        self.assertEqual(self.order(page).count("T-006"), 1)
        self.assertEqual(self.order(page).count("T-007"), 1)
        self.assertRegex(page, r'depends on proposed <a href="/t/T-007">T-007</a>: approving this alone leaves it waiting\.')
        self.link("T-004", ["T-006"])  # the hostile title sits in a group
        page = self.req("GET", "/review")[2]
        self.assertIn(f"<code class=cmd>{APPROVE} T-004 T-006 T-007</code>", self.region(page, "T-004") or self.region(page, "T-006"))
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)", page)
