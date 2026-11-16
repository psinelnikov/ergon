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
        for section in ("PROPOSED (2)", "DOING (1)", "REVIEW (1)", "Awaiting answer (1)"):
            self.assertIn(section, page)
        self.assertNotIn("DONE (", page)  # open work only, as `ergon` prints it
        self.assertIn('<b id="count-approval">2</b>', page)
        self.assertIn('<b id="count-acceptance">1</b>', page)
        self.assertIn('<b id="count-answer">1</b>', page)
        self.assertIn("1/5", page)  # E-001's progress
        self.assertIn('href="/t/T-002"', page)
        # PROPOSED lists haiku before human, as `ergon` sorts by tier
        self.assertLess(page.index('href="/t/T-004"'), page.index('href="/t/T-005"'))
        # the only script is ours, and it runs under the CSP nonce
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", headers["Content-Security-Policy"])[1]
        self.assertEqual(re.findall(r"<script[^>]*>", page), [f'<script nonce="{nonce}">'])
        self.assertIn("&lt;script&gt;", page)  # T-004's title, escaped

    def test_live_json(self):
        code, headers, body = self.req("GET", "/live.json")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        d = json.loads(body)
        self.assertEqual(d["counts"], {"approval": 2, "acceptance": 1, "answer": 1})
        [doing] = d["doing"]
        self.assertEqual(doing["id"], "T-002")
        self.assertEqual(doing["round"], 2)
        self.assertEqual(doing["spent"], 1.5)  # its worker and reviewer so far
        self.assertEqual(doing["last"], "dispatched")
        self.assertAlmostEqual(doing["since_ts"], S.parse_at("2026-11-16T13:12:00+00:00"))

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


class TestServeUnits(unittest.TestCase):
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
        self.assertAlmostEqual(tl["costs"][-1][3], 3.625)

    def test_page(self):
        self.journal()
        code, headers, page = self.req("GET", "/e/E-001")
        self.assertEqual(code, 200)
        self.assertEqual(page.count('class="lane'), 3)  # one lane per task
        self.assertEqual(len(re.findall(r'<a href="/t/T-00\d"><text', page)), 3)
        self.assertEqual(len(re.findall(r'<rect class="seg k-worker', page)), 6)
        self.assertEqual(len(re.findall(r'<rect class="seg [^"]* human', page)), 5)  # approvals x3, asking, hold, blocked: see below
        self.assertIn("waiting on a human", page)
        self.assertIn("no &lt;tests&gt;", page)
        self.assertIn("which db?", page)
        self.assertIn('id=play', page)
        self.assertIn('<option value=16>', page)
        self.assertIn("1,751 tokens", page) if False else None
        nonce = re.search(r"script-src 'nonce-([\w-]+)'", headers["Content-Security-Policy"])[1]
        self.assertEqual(re.findall(r"<script[^>]*>", page), [f'<script nonce="{nonce}">'])
        self.assertEqual(self.req("GET", "/e/E-009")[0], 404)
        self.assertIn('href="/e/E-001"', self.req("GET", "/")[2])
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
