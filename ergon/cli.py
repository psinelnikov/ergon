"""ergon: a task tracker where humans approve and agents execute.

One markdown file per task / epic, frontmatter holds the state. Each task is sized by the
cheapest model tier that can do it; no agent starts one until a human approves it. The
workflow and the approval rule: `ergon guide`. Per-project settings: .ergon.toml in the
repo's main checkout (`ergon init` writes one). No dependencies beyond the standard library.

  ergon                        board: open work grouped by status, then tier
  ergon init [--hooks]         write .ergon.toml, the state folders and .gitignore lines here.
                               --hooks also installs a git commit-msg hook that refuses commits
                               citing a task no human approved (covers agents other than Claude Code)
  ergon guide                  print the workflow guide (also the plugin's `ergon` skill)
  ergon rules                  print this project's worker rules (worker_rules in .ergon.toml)
  ergon next [--tier T]        approved, unblocked tasks an agent of tier T may take
  ergon review [-i]            everything waiting on human approval (-i: HUMAN ONLY, walk it
                               task by task: approve / reject / retier / edit / skip)
  ergon show ID
  ergon new task "Title" --tier sonnet [--epic E-001] [--touches a,b] [--source LOG.md:42]
  ergon new epic "Title"
  ergon import gh#123 [--tier T] [--epic E] [--touches a,b]
                               a GitHub issue (or gh:OWNER/REPO#123) as a proposed task, via the
                               `gh` CLI: title, quoted body as What, source: gh#123. Tier is
                               human until you size it. When its epic merges, ergon prints (never
                               runs) the `gh issue comment/close` commands
  ergon set ID status=doing    agents: ready -> doing -> done | blocked (never approval)
  ergon start E-ID             branch epic/<folder> off the trunk and check it out in its own
                               worktree, <worktrees>/E-ID (also happens on an epic's first `doing`)
  ergon finish E-ID            close the epic and merge its branch into the trunk (also happens
                               when its last task goes done); no checkout needs switching
  ergon approve ID [ID ...]    HUMAN ONLY, real terminal. An epic ID approves its proposed tasks
  ergon reject ID [--reason R] HUMAN ONLY. proposed -> dropped; review -> ready (needs --reason,
                               which the next worker is shown; branch and worktree are kept)
  ergon accept ID [ID ...]     HUMAN ONLY, real terminal. Land a task dispatch held at `review`
                               (merge its branch like dispatch would, close the epic if last).
                               An epic ID accepts its tasks in review
  ergon answer ID ["text"]     HUMAN ONLY, real terminal. Answer the QUESTION: lines a worker
                               parked its task on (`RESULT: asking`); no text offers the options.
                               The task goes back to ready, and the next dispatch resumes the
                               worker's session with the answer (a fresh one if its cache is gone).
                               Never changes approved:, tier, touches: or accept:
  ergon close ID [ID ...] --as done|dropped --reason R
                               HUMAN ONLY. An approved task whose work landed elsewhere, or that
                               no longer applies; skips the epic-branch check, closes the epic
                               if it was the last open task
  ergon audit [--epic E]       read-only staleness leads: dead or unapproved dependencies,
                               commits already citing open tasks, citations that don't resolve,
                               companion-repo paths a task's touches: doesn't name, retired
                               nouns. Judgment: the task-audit skill
  ergon check                  lint; non-zero exit on any violation
  ergon tidy                   move every task and epic file to where it belongs: an epic's tasks
                               in epics/<epic>/, the rest in items/ (idempotent)
  ergon dispatch [--tier T] [--epic E] [--ids T-1,T-2] [--max N] [--jobs J]
                      [--budget-total USD [--budget-pct P]] [--watch [--interval SEC]]
                      [--dry-run]
                               run ready tasks headless, one worktree + task/<ID> branch each,
                               merge each finished branch into its epic branch (and, for a task
                               touching a companion repo, that repo's local trunk).
                               --watch (needs --budget-total) keeps rescanning for newly ready
                               tasks every --interval seconds (default 30) until the cap is hit
                               or Ctrl-C (stop launching, let workers finish; a second kills them)
                               A reviewer's fail (`review` in .ergon.toml) goes back to the worker
                               and is reviewed again, up to review_rounds times, then blocks
  ergon stats [--since DATE] [--epic E]
                               read-only: per tier, runs / done / blocked / block rate / cost /
                               timeouts from <state>/runs/*.json, and tasks that blocked twice+
  ergon serve [--port 4300] [--bind 127.0.0.1]
                               read-only web board, task pages (brief, runs, reviews, logs, resume
                               commands) and live dispatch state. GET/HEAD only; writes nothing.
                               Any --bind other than loopback warns: there is no login
  ergon hook guard|stop        Claude Code hooks (the plugin wires them); not for humans
  ergon hook commit-msg FILE   the git hook `init --hooks` installs; not for humans
"""
import argparse
import datetime
import fcntl
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from pathlib import Path

from . import config as _config
from . import dispatch as _dispatch
from . import gates as _gates
from . import gitops as _gitops
from . import report as _report
from . import serve as _serve
from . import tasks as _tasks


# ---------- setup ----------

GITIGNORE = ["{state}/items/", "{state}/epics/", "{state}/runs/", "{worktrees}/"]


def init(args):
    """Write .ergon.toml (if missing), the state folders, and the .gitignore lines."""
    if _config.REPO != _config.HOME:
        raise SystemExit(f"run init from the main checkout ({_config.HOME})")
    path = _config.HOME / _config.CONFIG_FILE
    if path.exists():
        print(f"{_config.CONFIG_FILE} already exists; leaving it")
    else:
        path.write_text(_config.CONFIG_TEMPLATE)
        print(f"wrote {_config.CONFIG_FILE}")
    _config.configure(_config.HOME)
    for d in (_config.ITEMS, _config.EPICS):
        d.mkdir(parents=True, exist_ok=True)
    gi = _config.HOME / ".gitignore"
    have = gi.read_text().splitlines() if gi.exists() else []
    want = [g.format(state=_config.STATE.relative_to(_config.HOME).as_posix(), worktrees=_config.WORKTREES.relative_to(_config.HOME).as_posix())
            for g in GITIGNORE]
    add = [g for g in want if g not in have]
    if add:
        with gi.open("a") as f:
            f.write(("\n" if have and have[-1].strip() else "")
                    + "# ergon: task state is local; commits that cite task IDs are the record\n"
                    + "\n".join(add) + "\n")
        print(f"added to .gitignore: {' '.join(add)}")
    if args.hooks:
        _gates.install_commit_hook()
    print("next: enable the ergon plugin for this project (README), then `ergon new epic \"...\"`")



def guide(args):
    text = _config.GUIDE.read_text()
    print(re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip())


def rules(args):
    text = _dispatch.worker_rules()
    print(text or f"(no worker_rules in {_config.CONFIG_FILE}: follow the project's CLAUDE.md / AGENTS.md)")


def main():
    ap = argparse.ArgumentParser(prog="ergon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    for name in ("guide", "rules"):
        sub.add_parser(name)
    sub.add_parser("init").add_argument(
        "--hooks", action="store_true",
        help="also install a git commit-msg hook refusing commits that cite an unapproved task")
    b = sub.add_parser("board")
    b.add_argument("--tier", choices=_tasks.TIERS)
    b.add_argument("--epic")
    b.add_argument("--all", action="store_true", help="include done and dropped")
    n = sub.add_parser("next")
    n.add_argument("--tier", choices=_tasks.TIERS)
    rv = sub.add_parser("review")
    rv.add_argument("-i", "--interactive", action="store_true", help="HUMAN ONLY: decide task by task")
    rv.add_argument("--epic")
    rv.add_argument("--tier", choices=_tasks.TIERS)
    s = sub.add_parser("show")
    s.add_argument("id")
    nw = sub.add_parser("new")
    nw.add_argument("kind", choices=["task", "epic"])
    nw.add_argument("title")
    nw.add_argument("--tier")
    nw.add_argument("--epic")
    nw.add_argument("--touches")
    nw.add_argument("--source")
    im = sub.add_parser("import", help="a GitHub issue as a proposed task: gh#123 or gh:OWNER/REPO#123")
    im.add_argument("ref", metavar="gh#N")
    im.add_argument("--tier", help="default: human, until someone sizes it")
    im.add_argument("--epic")
    im.add_argument("--touches")
    st = sub.add_parser("set")
    st.add_argument("id")
    st.add_argument("pairs", nargs="+", metavar="key=value")
    for name in ("start", "finish"):
        sub.add_parser(name).add_argument("id")
    a = sub.add_parser("approve")
    a.add_argument("ids", nargs="+")
    sub.add_parser("accept").add_argument("ids", nargs="+")
    an = sub.add_parser("answer", help="HUMAN ONLY: answer a worker's question and requeue its task")
    an.add_argument("id")
    an.add_argument("text", nargs="*", help="the answer; leave it out to pick from the options")
    r = sub.add_parser("reject")
    r.add_argument("ids", nargs="+")
    r.add_argument("--reason")
    c = sub.add_parser("close")
    c.add_argument("ids", nargs="+")
    c.add_argument("--as", dest="as_", required=True, choices=["done", "dropped"])
    c.add_argument("--reason", required=True, help="the evidence: a commit, a log line, or why it's moot")
    au = sub.add_parser("audit")
    au.add_argument("--epic")
    ss = sub.add_parser("stats")
    ss.add_argument("--since", metavar="DATE", help="YYYY-MM-DD: runs started that day or later")
    ss.add_argument("--epic")
    rp = sub.add_parser("report")
    rp.add_argument("--since", metavar="DATE|last", help="YYYY-MM-DD, or `last` for the previous report")
    sv = sub.add_parser("serve", help="read-only web board and task pages")
    sv.add_argument("--port", type=int, default=_serve.DEFAULT_PORT, help=f"default {_serve.DEFAULT_PORT}")
    sv.add_argument("--bind", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1); anything but loopback warns: no login")
    sub.add_parser("check")
    sub.add_parser("tidy")
    d = sub.add_parser("dispatch")
    d.add_argument("--tier", choices=_config.WORKER_TIERS)
    d.add_argument("--epic")
    d.add_argument("--ids", help="comma-separated task IDs (still only ready ones)")
    d.add_argument("--max", type=int, help="tasks to launch this run (default 6; no limit with --watch)")
    d.add_argument("--jobs", type=int, default=3, help="workers at once (default 3)")
    d.add_argument("--permission-mode", default="auto",
                   choices=["auto", "acceptEdits", "dontAsk", "bypassPermissions"])
    d.add_argument("--timeout", type=int, default=60, help="minutes per worker (default 60)")
    d.add_argument("--budget", type=float, help="max USD per worker (--max-budget-usd)")
    d.add_argument("--budget-total", type=float,
                   help="max USD for the whole run: stop launching once finished workers' cost reaches it")
    d.add_argument("--budget-pct", type=float, default=100,
                   help="use only this %% of --budget-total as the cap (default 100)")
    d.add_argument("--watch", action="store_true",
                   help="keep running: rescan for newly ready tasks until Ctrl-C or --budget-total is reached")
    d.add_argument("--interval", type=float, default=30, help="seconds between rescans with --watch (default 30)")
    d.add_argument("--escalate", action="store_true",
                   help="when a worker reports blocked, retry once one tier up (haiku→sonnet→opus)")
    d.add_argument("--dry-run", action="store_true")
    hk = sub.add_parser("hook")
    hk.add_argument("event", choices=["guard", "stop", "commit-msg"])
    hk.add_argument("msgfile", nargs="?", help="commit-msg: the file git passes")
    hk.add_argument("--worker", action="store_true", help="dispatch sets this: guard companion checkouts too")
    hk.add_argument("--project", help="the project to guard (default: $CLAUDE_PROJECT_DIR, then the event's cwd)")
    args = ap.parse_args()
    if args.cmd == "hook":
        return _gates.hook(args)  # configures itself, and stays silent outside an ergon project
    if args.cmd == "guide":
        return guide(args)
    try:
        _config.configure(need_config=args.cmd != "init")
    except _config.NotConfigured as err:
        raise SystemExit(f"ergon: {err}")
    if args.cmd == "init":
        return init(args)
    _config.ITEMS.mkdir(parents=True, exist_ok=True)
    _config.EPICS.mkdir(parents=True, exist_ok=True)
    if args.cmd in (None, "board"):
        if args.cmd is None:
            args.tier = args.epic = None
            args.all = False
        _tasks.board(args)
    elif args.cmd in ("new", "set", "start", "finish", "close", "tidy"):  # read, then write state: one at a time
        with _gitops.state_lock():
            {"new": _tasks.new, "set": _gates.setf, "start": _gitops.start, "finish": _gitops.finish, "close": _gates.close,
             "tidy": _tasks.tidy}[args.cmd](args)
    else:  # dispatch takes the lock itself, around each step (it runs for hours)
        {"next": _tasks.nxt, "review": _tasks.review, "show": _tasks.show, "approve": _gates.approve, "reject": _gates.reject, "accept": _gates.accept, "answer": _gates.answer,
         "audit": _tasks.audit, "stats": _report.stats, "report": _report.report, "check": _tasks.check, "dispatch": _dispatch.dispatch, "rules": rules,
         "import": _tasks.import_issue, "serve": _serve.serve}[args.cmd](args)


if __name__ == "__main__":
    main()
