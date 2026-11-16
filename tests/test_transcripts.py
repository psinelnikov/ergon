"""Transcript reader: fixtures are small hand-written .jsonl files."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ergon import transcripts as T  # noqa: E402


def w(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e, separators=(",", ":")) + "\n")


def asst(mid, ts, usage, tool=None):
    content = [{"type": "tool_use", "id": "t" + mid, "name": tool, "input": {}}] if tool else []
    return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": "m", "usage": usage, "content": content}}


class TestTranscripts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = "/x/wt.1/T-1"
        self.dir = os.path.join(self.tmp, T.project_name(self.cwd))
        self.cache = os.path.join(self.tmp, "transcripts.json")

    def test_project_name(self):
        self.assertEqual(T.project_name("/a/b.c_d"), "-a-b-c-d")

    def test_cost_state(self):
        w(f"{self.dir}/s.jsonl", [
            {"type": "cost-state", "startTime": 1, "totalCostUSD": 1.5, "totalLinesAdded": 7, "totalLinesRemoved": 2,
             "totalAPIDuration": 100, "totalToolDuration": 50,
             "modelUsage": {"opus": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
                                     "cacheCreationInputTokens": 40, "thinkingTokens": 5, "costUSD": 1.5}}},
            asst("a", "t1", {}, "Bash"), asst("b", "t2", {}, "Bash"), asst("c", "t3", {}, "Read"),
            {"type": "user", "toolDenialKind": "hook", "message": {}},
            {"type": "user", "isCompactSummary": True, "message": {}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {}},
        ])
        s = T.session_summary("s", self.cwd, self.cache, self.tmp)
        self.assertEqual(s["cost"], 1.5)
        self.assertEqual(s["tokens"], {"input": 10, "output": 20, "cache_read": 30, "cache_write": 40, "thinking": 5})
        self.assertEqual(s["models"], {"opus": 1.5})
        self.assertEqual(s["tools"], {"Bash": 2, "Read": 1})
        self.assertEqual(s["denials"], {"hook": 1})
        self.assertEqual((s["compactions"], s["api_errors"], s["lines_added"], s["lines_removed"]), (1, 1, 7, 2))
        self.assertEqual((s["api_ms"], s["tool_ms"]), (100, 50))
        self.assertTrue(os.path.exists(self.cache))

    def test_usage_fallback_cache_rebuild_and_subagent(self):
        big = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 30000,
               "output_tokens_details": {"thinking_tokens": 3}}
        w(f"{self.dir}/s.jsonl", [asst("a", "t1", big), asst("b", "t2", big, "Edit")])
        w(f"{self.dir}/s/subagents/agent-1.jsonl", [asst("z", "t3", {"input_tokens": 5}, "Grep")])
        s = T.session_summary("s", self.cwd, self.cache, self.tmp)
        self.assertIsNone(s["cost"])
        self.assertEqual(s["tokens"]["cache_write"], 60000)
        self.assertEqual(s["tokens"]["input"], 7)
        self.assertEqual(s["tokens"]["thinking"], 6)
        self.assertEqual(s["tools"], {"Edit": 1, "Grep": 1})
        self.assertEqual(s["cache_rebuilds"], [["t2", 30000]])  # the first request never counts
        again = T.session_summary("s", self.cwd, self.cache, self.tmp)  # from the cache
        self.assertEqual(again, s)

    def test_cache_reuses_unchanged_file(self):
        w(f"{self.dir}/s.jsonl", [asst("a", "t1", {"input_tokens": 1})])
        T.session_summary("s", self.cwd, self.cache, self.tmp)
        p = f"{self.dir}/s.jsonl"
        c = json.load(open(self.cache))
        c[p]["summary"]["tools"] = {"Cached": 9}
        json.dump(c, open(self.cache, "w"))
        self.assertEqual(T.session_summary("s", self.cwd, self.cache, self.tmp)["tools"], {"Cached": 9})

    def test_last_request(self):
        side = dict(asst("c", "2026-11-16T10:05:00Z", {"input_tokens": 999999}), isSidechain=True)
        w(f"{self.dir}/s.jsonl", [
            asst("a", "2026-11-16T10:00:00.500Z", {"input_tokens": 10, "cache_read_input_tokens": 100}),
            asst("b", "2026-11-16T10:02:00.000Z", {"input_tokens": 5, "cache_read_input_tokens": 200,
                                                   "cache_creation_input_tokens": 30, "output_tokens": 7}),
            side,  # a subagent's request: its own context
            asst("d", "2026-11-16T10:06:00Z", {"input_tokens": 0}),  # a usage limit's synthetic message
        ])
        at, held = T.last_request("s", self.cwd, self.tmp)
        self.assertEqual(at, 1794823320.0)  # 2026-11-16T10:02:00Z
        self.assertEqual(held, 235)
        self.assertIsNone(T.last_request("nope", self.cwd, self.tmp))
        self.assertIsNone(T.last_request(None, self.cwd, self.tmp))

    def test_missing(self):
        self.assertIsNone(T.session_summary("nope", self.cwd, self.cache, self.tmp))
        self.assertIsNone(T.session_summary(None, self.cwd, self.cache, self.tmp))
        self.assertIsNone(T.read("/no/such/file.jsonl"))


if __name__ == "__main__":
    unittest.main()
