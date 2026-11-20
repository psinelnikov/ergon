"""Approval gates, the Claude Code hooks and the git commit-msg hook."""
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

from . import concepts as _concepts
from . import config as _config
from . import dispatch as _dispatch
from . import gitops as _gitops
from . import history as _history
from . import report as _report
from . import shared as _shared
from . import tasks as _tasks


def setf(args):
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    item = tasks.get(args.id) or epics.get(args.id)
    if not item:
        raise SystemExit(f"no such id: {args.id}")
    is_task = args.id in tasks
    epic = epics.get(item.get("epic")) if is_task else None
    if any("\n" in kv or "\r" in kv for kv in args.pairs):  # a line break would write a frontmatter line of its own
        raise SystemExit("set takes one line per key=value: no line breaks")
    args.pairs = [kv.partition("=")[0].strip() + "=" + kv.partition("=")[2] for kv in args.pairs]  # the file reads keys stripped
    bad = [kv.partition("=")[0] for kv in args.pairs if not re.fullmatch(r"[A-Za-z_][\w-]*", kv.partition("=")[0])]
    if bad:  # `verify:x=…` would be read back as `verify: x: …`
        raise SystemExit(f"not a field name: {bad[0]!r} (letters, digits, _ and -)")
    new_status = dict(kv.partition("=")[::2] for kv in args.pairs).get("status")
    old_status = item.get("status")
    if new_status and _gitops.current_branch().startswith(_config.TASK_BRANCH):
        raise SystemExit("you're a dispatched worker: the dispatcher owns status. End your "
                         "reply with `RESULT: done`, `RESULT: blocked — <why>`, or `QUESTION:` lines "
                         "and `RESULT: asking`")
    if not is_task and new_status == "done":
        raise SystemExit(f"close epics with `ergon finish {args.id}` (it merges the epic branch)")
    for kv in args.pairs:
        key, _, val = kv.partition("=")
        if key == "approved":
            raise SystemExit("approval is human-only: use `approve` from a real terminal")
        if key == "verify":  # a task's verify: replaces the runtime gate (`verify: true` passes anything)
            raise SystemExit("`verify:` overrides the runtime gate, so set won't write it: a human edits the "
                             "task file (it's in the brief digest, so an approved task needs approving again)")
        if key == "prove" and val.strip().lower() in _tasks.PROVE_NO:
            raise SystemExit("`prove: no` skips the red-on-base proof, so set won't write it: a human edits the "
                             "task file (it's in the brief digest, so an approved task needs approving again)")
        if key in ("harden", "harden_layer"):  # a harden task skips the red-on-base proof; the layer picks its gate
            raise SystemExit(f"`{key}:` decides what a harden task must prove (and a harden task skips the "
                             f"red-on-base proof), so set won't write it: a human edits the task file (it's in "
                             f"the brief digest, so an approved task needs approving again)")
        if key == "suppress_ok":  # the files a task may add a suppression marker to (T-160)
            raise SystemExit("`suppress_ok:` names the files the weakening check lets a task add a suppression "
                             "marker to, so set won't write it: a human edits the task file (it's in the brief "
                             "digest, so an approved task needs approving again)")
        if is_task and key == "pin":
            raise SystemExit("`pin:` is human-only: use `ergon pin ID first|last|clear` from a real terminal")
        if is_task and key in ("asking", "answered"):
            raise SystemExit(f"`{key}:` is written by dispatch and a human's `ergon answer` only")
        if is_task and key == "limited":
            raise SystemExit("`limited:` is written by dispatch only: it parks a task the usage limit stopped and "
                             "resumes it after the reset")
        if is_task and key == "status" and item.get("limited"):
            raise SystemExit(f"{args.id} is parked by the usage limit (`limited: {item['limited']}`): the next "
                             f"dispatch at or after the reset resumes it (a human's `close` gives it up)")
        if is_task and key == "status" and _tasks.is_asking(item):
            raise SystemExit(f"{args.id} waits on an answer to its worker's question: a human runs "
                             f"`ergon answer {args.id}` in their own terminal (or `ergon close` to give it up)")
        if is_task and key == "accept" and val.lower() not in _tasks.YES:
            raise SystemExit("set can only turn `accept:` on (accept=yes); a human clears it by editing the file")
        if is_task and key == "status":
            if val not in _tasks.AGENT_MOVES.get(item["status"], set()):
                raise SystemExit(f"{args.id}: {item['status']} -> {val} not allowed via set"
                                 + (" (needs human approve)" if item["status"] == "proposed" else "")
                                 + (" (a human accepts or rejects it)" if item["status"] == "review" else ""))
        if is_task and key in ("category", "touches") and _tasks.is_auto(item) and in_agent_shell():
            raise SystemExit(f"{args.id} was approved by inheritance (`approved: auto …`), so its `{key}:` is fixed: "
                             f"file a new task, or ask a human to change it")
        if (is_task and key in ("touches", "tier") and item.get("status") != "proposed"
                and (in_agent_shell() or _gitops.current_branch().startswith(_config.TASK_BRANCH))):
            raise SystemExit(f"{args.id}: " + replan_reason(key, item.get("status")).replace("<ID>", args.id))
        if is_task and key == "tier" and val not in _tasks.TIERS:
            raise SystemExit(f"tier must be one of {_tasks.TIERS}")
        if is_task and key == "category" and val and val not in _tasks.CATEGORIES:
            raise SystemExit(f"category must be one of {_tasks.CATEGORIES}")
        if not is_task and key == "status" and val not in _tasks.EPIC_STATUSES:
            raise SystemExit(f"epic status must be one of {_tasks.EPIC_STATUSES}")
        if not is_task and key == "status" and "proposed" in (val, item.get("status")) and val != item.get("status"):
            raise SystemExit(f"{args.id}: {item.get('status')} -> {val} not allowed via set: a proposed epic is "
                             f"opened by a human's `ergon approve {args.id}` (dropping one is a human's edit too)")
        if is_task and key == "status" and val == "doing":
            why = _tasks.epic_problem(epic, _gitops.approvals())
            if why:
                raise SystemExit(f"{args.id} can't start: {why}")
        if key in ("depends", "touches"):
            val = [v for v in val.split(",") if v]
        if is_task and key == "touches":  # function-level entries, `path/::fn` (T-145)
            bad = _concepts.touch_problems({"touches": val})
            if bad:
                raise SystemExit(f"{args.id}: touches: " + "; ".join(bad))
            for note in _concepts.fn_notes({"touches": val}, _concepts.fn_map()):
                print(f"{args.id}: touches: {note}", file=sys.stderr)
        item[key] = val
    if epic and new_status:
        try:
            _gitops.enter_epic(epic, starting=new_status == "doing")
        except _gitops.GitError as err:
            raise SystemExit(str(err))
    if is_task and any(kv.partition("=")[0] == "epic" for kv in args.pairs):
        if item["epic"] and item["epic"] not in epics:
            raise SystemExit(f"no such epic: {item['epic']}")
        old, item["_path"] = item["_path"], _tasks.task_home(item, epics) / item["_path"].name
        if old != item["_path"]:
            old.rename(item["_path"])
    _tasks.dump(item, _tasks.TASK_KEYS if is_task else _tasks.EPIC_KEYS)
    if new_status and new_status != old_status:  # work moved outside dispatch leaves a row too
        _history.record("status", item, by=approver(), agent=in_agent_shell(),
                        **{"from": old_status, "to": new_status})
    if (epic and new_status == "done" and item["tier"] != "human"
            and _gitops.branch_exists(_gitops.epic_branch(epic))
            and not any(_gitops.cites(c, args.id) for c in _gitops.branch_commits(_gitops.epic_branch(epic)))):
        print(f"warning: no commit on {_gitops.epic_branch(epic)} cites {args.id}; task files are local, "
              f"so put {args.id} in the commit message or the work leaves no record")
    if epic and new_status in ("done", "dropped") and _gitops.epic_complete(epic["id"], _tasks.load(_config.ITEMS)):
        try:
            _gitops.finish_epic(epic["id"])
        except _gitops.GitError as err:
            print(f"{args.id} is {new_status}; {epic['id']} is complete but not merged: {err}")


def in_agent_shell():
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("AI_AGENT"))


def ask(prompt):
    """Speed bump, not security: refuse inside agent shells, ask on the controlling tty."""
    if in_agent_shell():
        raise SystemExit("refused: running inside an agent shell. A human approves from their own terminal.")
    if os.environ.get("ERGON_ASK_STDIN"):  # scripted answers (tests); the agent-shell refusal above still holds
        sys.stdout.write(prompt)
        sys.stdout.flush()
        return sys.stdin.readline().strip()
    try:
        with open("/dev/tty", "r") as tin, open("/dev/tty", "w") as tout:
            tout.write(prompt)
            tout.flush()
            return tin.readline().strip()
    except OSError:
        raise SystemExit("refused: no terminal to confirm on.")


def human_gate(prompt):
    return ask(prompt + " [y/N] ").lower() == "y"


def approver():
    name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
    return name or os.environ.get("USER", "human")


def targets(ids, tasks, epics, status="proposed"):
    """The tasks ids name; an epic ID names its tasks in `status`."""
    out = []
    for i in ids:
        if i in epics:
            out += [t for t in tasks.values() if t.get("epic") == i and t["status"] == status]
        elif i in tasks:
            out.append(tasks[i])
        else:
            raise SystemExit(f"no such id: {i}")
    return out


def signing():
    """Shared mode, joined: human decisions are SSH-signed state commits. Refuses (SystemExit)
    if this clone can't sign, saying how to set it up. Returns whether to sign."""
    if not _shared.joined():
        return False
    why = _gitops.signing_problem()
    if why:
        raise SystemExit(f"refused: {why}")
    return True


def approve_ready():
    """Shared mode, or [signing] in local mode: refuse (SystemExit) unless an approval made here
    would verify: (shared) the state is a worktree, signing is set up, and the approvers file on
    the remote's trunk, or (local) on the committed trunk, lists this key."""
    if not _config.signed():
        return
    if not _config.SHARED:
        why = _gitops.approve_problem()
        if why:
            raise SystemExit(f"refused: {why}")
        return
    if _shared.joined():
        _shared.fetch_trunk()  # see the approvers file as the remote has it now
    why = _gitops.approve_problem()
    if why:
        raise SystemExit(f"refused: {why}")


def stamp_approved(todo):
    """Approve `todo`: proposed tasks move to ready, and each gets `approved: <name> <date>`. In
    shared mode the stamp also names its brief digest, and the stamps go in one SSH-signed state
    commit with an `Ergon-Approve: <ID> sha256:<digest>` trailer per task, which is what makes
    them count. If signing fails, nothing is approved (SystemExit). Takes the state lock. With
    [signing] in local mode the stamp names its digest too, and one detached signature over every
    `<ID> sha256:<digest>` goes to <state>/approvals/ before any stamp is written (T-210).

    A task under a [[two_person]] rule only gets its trailer in the signed commit. Then the
    approvals that verify are counted: with two different principals it moves to ready and its
    stamp names both; with fewer it gets a Note (`approved by Pavel (1 of 2)`), stays (or goes
    back to) proposed, and its approved: is emptied. What verifies decides, never who this clone
    thinks it is, so a second key of the same person still counts once. Returns the IDs of the
    two-person tasks still waiting for an approval."""
    no_choice = [why for t in todo if (why := _tasks.choice_problem(t))]
    if no_choice:  # E-006: a task with options is approved only once a human chose one
        raise SystemExit("refused: " + "; ".join(no_choice))
    shared = _config.SHARED is not None
    signed = _config.signed()
    two = {t["id"] for t in todo if _tasks.two_person(t)}
    with _gitops.state_lock():
        if shared:
            _shared.commit_pending()  # hand edits go in a commit of their own, not the signed one
        sig = {}
        if signed and not shared:  # local [signing]: sign first, so a failure leaves every file as it was
            sig = {"signature": _gitops.sign_approvals([(t["id"], _tasks.brief_digest(t)) for t in todo]).name}
        trailers, stamp = [], f"{approver()} {_tasks.today()}"
        for t in todo:
            digest = _tasks.brief_digest(t)
            trailers.append(f"{_gitops.APPROVE_TRAILER}: {t['id']} sha256:{digest}")
            if t["id"] in two:
                continue
            epic = _tasks.is_epic(t)  # an epic's brief: proposed -> open
            if t["status"] == "proposed":
                t["status"] = "open" if epic else "ready"
            t["approved"] = f"{stamp} sha256:{digest[:8]}" if signed else stamp
            _tasks.dump(t, _tasks.EPIC_KEYS if epic else _tasks.TASK_KEYS)
        if shared:
            _gitops.sign_state("approve", trailers, [t["_path"] for t in todo if t["id"] not in two])
        apr = _gitops.approvals() if signed else None
        for t in todo:
            if t["id"] not in two:
                _history.record("approved", t, by=approver(), tier=t.get("tier") or None, **approved_touches_field(t),
                                **sig)  # [signing]: the <state>/approvals/ file that signs it
                continue
            # the files change after the signed commit, from what it verifies as: committed
            # unsigned as the lock is released, like sync's restamp, since they grant nothing
            old = t["status"]
            who = [] if apr.error else apr.signers(t)
            if len(who) >= apr.needed(t):
                t["status"] = "ready" if old == "proposed" else old
                t["approved"] = apr.stamp(t)
                _history.record("approved", t, by=approver(), tier=t["tier"], approvers=[n for _, n, _ in who],
                                **approved_touches_field(t))
            else:
                if old == "ready":  # approved before, under another brief: it waits for its second approver
                    t["status"] = "proposed"
                t["approved"] = ""
                if who:
                    _dispatch.add_note(t, f"approved by {', '.join(n for _, n, _ in who)} "
                                          f"({len(who)} of {apr.needed(t)})", who="approve")
                _history.record("approval", t, by=approver(), tier=t["tier"], count=len(who), needed=apr.needed(t))
            _tasks.dump(t, _tasks.TASK_KEYS)
            if old == "ready" and t["status"] == "proposed":
                _history.record("status", t, by=approver(), **{"from": old, "to": t["status"]})
    waiting = []
    if signed:  # e.g. a key user.signingkey didn't name, so approve_ready couldn't check it
        for t in todo:
            why = apr.problem(t)
            if t["id"] in two and why:
                waiting.append(t["id"])
            if t["id"] in two and (not why or apr.signers(t)):
                print(f"{t['id']}: two-person rule, {apr.tally(t)}" + (f"; it stays {t['status']}" if why else ""))
            elif why:
                print(f"warning: {t['id']}'s approval doesn't verify: {why}")
    return waiting


def approved_touches_field(t):
    """The `touches`, `harden` and `suppress_ok` fields of t's `approved` history row
    (dispatch.approved_touches, approved_harden and approved_suppress_ok read them): a task's
    touches:, harden: and suppress_ok: as approved; nothing for an epic."""
    return {} if _tasks.is_epic(t) else _dispatch.approval_record(t)


def brief_due(e, apr):
    """Does approving epic e mean approving its brief? It's proposed, or (shared mode) open
    without a signed approval of its brief that verifies: a stamp that doesn't verify, or no stamp
    at all (an old epic, or one a clone pushed). In local mode an open epic is approved already."""
    if e.get("status") == "proposed":
        return True
    return bool(apr and e.get("status") == "open" and apr.problem(e))


def print_brief(e):
    """Print what approving epic e approves: its Goal, Out of scope and Constraints sections."""
    for name in _tasks.EPIC_DIGEST_SECTIONS:
        text = _tasks.normalised(_tasks.sections(e.get("_body") or "", name))
        print(f"      {name.capitalize()}: " + ("\n        ".join(text.split("\n")) if text else "(empty)"))


def approve(args):
    approve_ready()
    # With [[two_person]] rules, sync before and after: whether a task matches a rule, and whose
    # approvals it has, is only known from the latest state, and the others must see this one.
    count = bool(_config.SHARED and _config.TWO_PERSON)
    if count:
        sync_for_count(args.ids)
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    apr = _gitops.approvals()
    # An epic ID whose brief needs a human's approval (proposed, or in shared mode open without
    # an approval that verifies) approves the brief alone: its tasks are approved by naming the
    # epic again once it's open, so each approval is of something the human has read.
    briefs = [epics[i] for i in dict.fromkeys(args.ids) if i in epics and brief_due(epics[i], apr)]
    named = targets([i for i in args.ids if i not in {e["id"] for e in briefs}], tasks, epics)
    todo = [t for t in named if t["status"] == "proposed"]
    # shared mode: an approved task whose approval no longer verifies (its brief was edited, or it
    # was stamped by hand) is approved again by naming it; its status stays
    again = {t["id"]: why for t in named if apr and t["id"] in args.ids and t["status"] in ("ready", "blocked")
             and (why := apr.problem(t))}
    again.update((e["id"], apr.problem(e)) for e in briefs if e["status"] == "open")
    # local mode: no digest, so an approved task whose touches:, harden: or suppress_ok: gained an
    # entry since its approval (a hand edit) is approved again by naming it, or the weakening check
    # won't count the entry
    if not apr:
        rows = _history.read()
        for t in named:
            if t["id"] in args.ids and t["status"] in ("ready", "blocked"):
                why = [f"{k}: gained {', '.join(new)} since its approval"
                       for k, new in (("touches", _dispatch.unapproved_touches(t, rows)),
                                      ("harden", _dispatch.unapproved_harden(t, rows)),
                                      ("suppress_ok", _dispatch.unapproved_suppress_ok(t, rows))) if new]
                if why:
                    again[t["id"]] = "; ".join(why)
    todo += [t for t in named if t["id"] in again]
    no_choice = [why for t in todo if (why := _tasks.choice_problem(t))]
    todo = [t for t in todo if not _tasks.choice_problem(t)]
    for why in no_choice:
        print(f"left out: {why}")
    if no_choice and not todo and not briefs:
        raise SystemExit("refused: " + "; ".join(no_choice))
    todo = briefs + todo
    if not todo:
        raise SystemExit("nothing in `proposed` among those ids" + (
            ", nor an approved task or epic whose approval doesn't verify" if apr else
            ", nor an approved task whose touches:, harden: or suppress_ok: gained an entry since its approval"))
    # shared mode: an unpublished ID can still be renumbered, so sync first (or refuse)
    _shared.ensure_published([t["id"] for t in todo] + [i for i in args.ids if i in epics])
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    now = lambda t: (epics if _tasks.is_epic(t) else tasks).get(t["id"], {})
    gone = [t["id"] for t in todo if now(t).get("status") != t["status"]]  # a sync moved it on
    if gone:
        print(f"left out, their status changed in the sync: {', '.join(gone)}")
    todo = [now(t) for t in todo if t["id"] not in gone]
    if not todo:
        raise SystemExit("nothing left to approve")
    apr = _gitops.approvals() if count else apr
    for t in todo:
        if _tasks.is_epic(t):
            print(f"  {t['id']}  epic    {t['title']}  (its brief: Goal, Out of scope, Constraints)"
                  + (f"  (approve again: {again[t['id']]})" if t["id"] in again else ""))
            print_brief(t)
            continue
        print(_tasks.line(t, tasks) + (f"  (approve again: {again[t['id']]})" if t["id"] in again else "")
              + (f"  (two-person: {apr.tally(t)})" if _tasks.two_person(t) else ""))
        if str(t.get("verify") or "").strip():  # both are signed by the approval, so show them
            print(f"      verify: {str(t['verify']).strip()}  (replaces the runtime gate)")
        if _tasks.prove_off(t):
            print("      prove: no  (skips the red-on-base proof)")
        if _tasks.choice_text(t):  # the choice is part of what the approval approves
            print(f"      chosen {_tasks.choice_text(t)}")
    n_epics = sum(map(_tasks.is_epic, todo))
    what = " and ".join(x for x in (f"{n_epics} epic brief(s)" if n_epics else "",
                                    f"{len(todo) - n_epics} task(s)" if len(todo) > n_epics else "") if x)
    if not human_gate(f"Approve these {what} as {approver()}?"):
        raise SystemExit("aborted")
    waiting = stamp_approved(todo)
    print(f"approved {len(todo) - len(waiting)}"
          + (f"; {len(waiting)} wait for another approver: {', '.join(waiting)}" if waiting else ""))
    tasks = _tasks.load(_config.ITEMS)
    for e in (t for t in todo if _tasks.is_epic(t)):
        left = sum(t.get("epic") == e["id"] and t["status"] == "proposed" for t in tasks.values())
        if left:
            print(f"{e['id']} is {e['status']}; its {left} proposed task(s) aren't approved yet: "
                  f"`ergon approve {e['id']}` again approves them")
    if count:  # publish, so the other approvers see this approval
        sync_for_count([t["id"] for t in todo], after=True)


def sync_for_count(ids, after=False, what="approval"):
    """Shared mode, around approving two-person tasks: sync, so the count includes approvals other
    clones published, and so they see this one. A failure is reported, not fatal: counting fewer
    approvals than were made only ever leaves a task waiting, and an approval of a stale brief
    doesn't verify once the newer one merges in. Refuses if the sync renumbered one of `ids`, so
    a named ID never comes to mean another task. Refuses in an agent shell or on a task branch
    first, as `ergon sync` does: this runs before approve's prompt, which would refuse an agent
    only after the push."""
    _shared.refuse()
    try:
        moved = _shared.sync_core(quiet=True)
    except SystemExit as err:
        print(f"warning: couldn't sync ({err}); " + (
            f"run `ergon sync` to publish this {what}" if after else
            "approvals published from other clones since the last sync aren't counted" if what == "approval" else
            "acting on the state as last fetched"))
        return
    hit = [f"{o} is now {moved[o]}" for o in ids if o in moved]
    if hit:
        raise SystemExit(f"refused: the sync renumbered {'; '.join(hit)}. Check the new IDs, then run the command again.")


POLICY_HEAD = "# Policies\n\nSettled review findings, written by a human (`ergon review -i`, p). Workers and reviewers read them.\n\n## Stances\n"


def vetoed(t):
    """A task whose findings a human may settle as a policy: held in `review`, or blocked on a review veto."""
    return t["status"] == "review" or (
        t["status"] == "blocked" and re.search(r"\breview \(\w+\) failed:", _report.last_note(t)))


def write_policy(t):
    """Prompt for a policy from t's last review finding and append it to the policies file."""
    finding = _report.last_note(t)
    print(f"\nfinding: {finding or '(the task is held in review)'}")
    title = ask("policy title: ")
    why = ask("why: ") if title else ""
    applies = ask("applies to: ") if title else ""
    if not title:
        print("no title; nothing written")
        return False
    path = _config.POLICIES
    text = path.read_text() if path.exists() else POLICY_HEAD
    n = max([int(m) for m in re.findall(r"^### P-(\d+)\b", text, re.M)] or [0]) + 1
    entry = (f"\n### P-{n} {title}\n- **Why:** {why}\n- **Applies to:** {applies}\n"
             f"- **Examples:** {t['id']}: {finding}\n- **Decided:** {approver()} {_tasks.today()}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n" + entry)
    if ask(f"P-{n} written to {path.relative_to(_config.HOME)}. open $EDITOR to refine it? [y/N] ").lower() == "y":
        subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])
    return True


def review_brief(e, at, tasks, apr, tally):
    """One epic brief in the interactive review, while its approval is due (`brief_due`): show
    its Goal, Out of scope and Constraints and offer to approve it, which approves the brief
    alone as `ergon approve E-NNN` does (signed in shared mode; its tasks stay proposed), reject
    it (a proposed brief only: drops it as `ergon reject E-NNN` does), edit it, or skip it.
    Returns False on quit."""
    while brief_due(e, apr):
        why = apr.problem(e) if e["status"] == "open" else ""
        print(f"\n{'=' * 78}\n{at}  {e['id']}  {e['title']}  (epic brief, {e['status']})"
              + (f"\n(approve again: {why})" if why else "") + "\n")
        print_brief(e)
        kids = sum(t.get("epic") == e["id"] and t["status"] == "proposed" for t in tasks.values())
        if kids:
            print(f"\n(approving it approves the brief only: its {kids} proposed task(s) stay proposed)")
        can_reject = e["status"] == "proposed"  # an open epic is already approved: not a drop
        choice = ask(f"\n{e['id']} [epic]  [a]pprove  " + ("[r]eject  " if can_reject else "")
                     + "[e]dit  [s]kip  [q]uit > ").lower()
        if choice == "r" and can_reject:
            try:
                sign = signing()
            except SystemExit as err:
                print(err)
                continue
            reason = ask("reason (optional): ")
            e["status"] = "dropped"
            if reason:
                e["_body"] = e["_body"].rstrip() + f"\n\nRejected {_tasks.today()}: {reason}\n"
            with _gitops.state_lock():
                if sign:
                    _shared.commit_pending()  # hand edits go in a commit of their own
                _tasks.dump(e, _tasks.EPIC_KEYS)
                if sign:
                    _gitops.sign_state("reject", [f"{_gitops.REJECT_TRAILER}: {e['id']}"], [e["_path"]])
            _history.record("rejected", e, by=approver(), reason=reason or None, **{"from": "proposed", "to": "dropped"})
            tally["dropped"] += 1
            return True
        if choice == "a":
            try:  # shared mode: refuses, approving nothing, unless the approval would verify
                approve_ready()
                stamp_approved([e])
            except SystemExit as err:
                print(err)
                continue
            tally["approved"] += 1
            if _config.SHARED and _config.TWO_PERSON:  # publish it, as `approve` does
                sync_for_count([e["id"]], after=True)
            return True
        if choice == "e":
            subprocess.run([os.environ.get("EDITOR", "vi"), str(e["_path"])])
            with _gitops.state_lock():  # shared mode: commits the edit
                e = _tasks.parse(e["_path"])
            if not brief_due(e, apr):
                print(f"{e['id']} is now {e['status']}; moving on")
        elif choice == "s":
            tally["skipped"] += 1
            return True
        elif choice == "q":
            return False
    return True


def review_interactive(args):
    """HUMAN ONLY: walk the proposed queue one item at a time (an epic whose brief is due, by
    `brief_due`, comes before its tasks, offering `a`: approve the brief alone, as approving the
    epic's ID does), then tasks waiting on an answer to their worker's question (offering `a`:
    answer it, as `ergon answer` does), then tasks whose review vetoed them (offering `p`: save
    the finding as a policy). A proposed task with options offers `c`: choose one, as `ergon choose`
    does; approving it is refused until it has a choice. Every answer lands immediately."""
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    held = _shared.unpublished([t["id"] for t in tasks.values() if t["status"] == "proposed"]
                               + [t["epic"] for t in tasks.values() if t["status"] == "proposed" and t.get("epic")]
                               + [e["id"] for e in epics.values() if e.get("status") in ("proposed", "open")])
    if held:  # sync first, then look at the queue again (IDs may have changed)
        print(f"{', '.join(held)} not published yet: syncing first")
        try:
            _shared.sync_core()
        except SystemExit as err:
            print(f"sync failed ({err}); unpublished tasks are left out of this review")
        tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
        held = set(_shared.unpublished(list(tasks) + list(epics)))
        if held:
            print(f"refused, not published to {_shared.remote()}: {', '.join(sorted(held))}")
    else:
        held = set()
        if _config.SHARED and _config.TWO_PERSON:  # see the latest briefs and approvals, as approve does
            sync_for_count([t["id"] for t in tasks.values() if t["status"] == "proposed"]
                           + [e["id"] for e in epics.values() if e.get("status") == "proposed"])
            tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    apr = _gitops.approvals()  # None in local mode; in shared mode, which approvals verify
    pick = lambda t: (t["id"] not in held and (not args.epic or t.get("epic") == args.epic) and (not args.tier or t["tier"] == args.tier))
    # an epic's brief has no tier, and comes before its own tasks: approving it is what opens them
    order = lambda t: (t["id"], "") if _tasks.is_epic(t) else (t.get("epic") or "~", t["id"])
    briefs = [e for e in epics.values() if brief_due(e, apr) and e["id"] not in held
              and (not args.epic or e["id"] == args.epic) and not args.tier]
    queue = sorted(briefs + [t for t in tasks.values() if t["status"] == "proposed" and pick(t)], key=order)
    queue += sorted((t for t in tasks.values() if _tasks.is_asking(t) and pick(t)), key=order)
    queue += sorted((t for t in tasks.values() if vetoed(t) and not _tasks.is_asking(t) and pick(t)), key=order)
    if not queue:
        raise SystemExit("nothing awaiting approval or an answer")
    tally = {"approved": 0, "dropped": 0, "answered": 0, "skipped": 0, "policies": 0}
    for i, t in enumerate(queue, 1):
        if _tasks.is_epic(t):
            if not review_brief(t, f"[{i}/{len(queue)}]", tasks, apr, tally):
                print("  ".join(f"{k} {v}" for k, v in tally.items()))
                return
            continue
        if _tasks.is_asking(t):
            epic = epics.get(t.get("epic"))
            print(f"\n{'=' * 78}\n[{i}/{len(queue)}]  {t['id']}  {t['title']}  (asking)"
                  f"\n{epic['id'] + '  ' + epic['title'] if epic else '(no epic)'}")
            show_questions(t)
            while True:
                choice = ask(f"\n{t['id']} [{t['tier']}]  [a]nswer  [s]kip  [q]uit > ").lower()
                if choice == "a":
                    try:
                        answer_task(t)
                        tally["answered"] += 1
                    except SystemExit as err:  # no answer, not confirmed, or moved on meanwhile: nothing written
                        if str(err).startswith("refused"):  # ask()'s agent-shell / no-tty refusal ends the review
                            raise
                        print(err)
                        tally["skipped"] += 1
                    break
                if choice == "s":
                    tally["skipped"] += 1
                    break
                if choice == "q":
                    print("  ".join(f"{k} {v}" for k, v in tally.items()))
                    return
            continue
        while t["status"] != "proposed":
            print(f"\n{'=' * 78}\n[{i}/{len(queue)}]  {t['id']}  {t['title']}  ({t['status']})\n")
            print(_report.last_note(t) or "(held in review)")
            choice = ask(f"\n{t['id']}  [p]olicy from this finding  [s]kip  [q]uit > ").lower()
            if choice == "p":
                tally["policies"] += write_policy(t)
                break
            if choice == "s":
                tally["skipped"] += 1
                break
            if choice == "q":
                print("  ".join(f"{k} {v}" for k, v in tally.items()))
                return
        while t["status"] == "proposed":
            epic = epics.get(t.get("epic"))
            print(f"\n{'=' * 78}\n[{i}/{len(queue)}]  {epic['id'] + '  ' + epic['title'] if epic else '(no epic)'}\n")
            print(t["_path"].read_text().rstrip())
            waits = [d for d in t.get("depends") or [] if tasks.get(d, {}).get("status") != "done"]
            if waits:
                print(f"\n(waits on {', '.join(waits)})")
            if apr and _tasks.two_person(t):
                print(f"\n(two-person: {apr.tally(t)})")
            has_opts = _tasks.has_options(t)
            if has_opts:
                print("\n" + (f"(chosen {_tasks.choice_text(t)})" if _tasks.choice_text(t) else
                              "(options and no choice yet: [c]hoose one before approving)"))
            choice = ask(f"\n{t['id']} [{t['tier']}]  [a]pprove  " + ("[c]hoose  " if has_opts else "")
                         + "[r]eject  [t]ier  [e]dit  [s]kip  [q]uit > ").lower()
            if choice == "c" and has_opts:
                try:
                    choose_task(t)
                    tally["chosen"] = tally.get("chosen", 0) + 1
                except SystemExit as err:  # nothing picked, not confirmed, or moved on: nothing written
                    if str(err).startswith("refused"):  # ask()'s agent-shell / no-tty refusal ends the review
                        raise
                    print(err)
                t = _tasks.parse(t["_path"])
                continue
            if choice == "a":
                why = _tasks.choice_problem(t)
                if why:
                    print(f"refused: {why}")
                    continue
                try:  # shared mode: refuses, approving nothing, unless the approval would verify
                    approve_ready()
                    waiting = stamp_approved([t])
                except SystemExit as err:
                    print(err)
                    continue
                if waiting:  # a two-person task still waiting for its second approver
                    tally["1 of 2"] = tally.get("1 of 2", 0) + 1
                else:
                    tally["approved"] += 1
                if _config.TWO_PERSON:  # publish it, so the other approvers see it
                    sync_for_count([t["id"]], after=True)
                break
            if choice == "r":
                try:
                    sign = signing()
                except SystemExit as err:
                    print(err)
                    continue
                reason = ask("reason (optional): ")
                t["status"] = "dropped"
                if reason:
                    t["_body"] = t["_body"].rstrip() + f"\n\nRejected {_tasks.today()}: {reason}\n"
                with _gitops.state_lock():
                    if sign:
                        _shared.commit_pending()  # hand edits go in a commit of their own
                    _tasks.dump(t, _tasks.TASK_KEYS)
                    if sign:
                        _gitops.sign_state("reject", [f"{_gitops.REJECT_TRAILER}: {t['id']}"], [t["_path"]])
                _history.record("rejected", t, by=approver(), reason=reason or None, **{"from": "proposed", "to": "dropped"})
                tally["dropped"] += 1
                break
            if choice == "t":
                tier = ask(f"tier {_tasks.TIERS}: ").lower()
                if tier not in _tasks.TIERS:
                    print(f"not a tier: {tier!r}")
                    continue
                t["tier"] = tier
                t["tier_why"] = ask(f"tier_why (enter keeps {t.get('tier_why')!r}): ") or t.get("tier_why", "")
                with _gitops.state_lock():
                    _tasks.dump(t, _tasks.TASK_KEYS)
            elif choice == "e":
                subprocess.run([os.environ.get("EDITOR", "vi"), str(t["_path"])])
                with _gitops.state_lock():  # shared mode: commits the edit
                    t = _tasks.parse(t["_path"])
                if t["status"] != "proposed":
                    print(f"{t['id']} is now {t['status']}; moving on")
                    break
            elif choice == "s":
                tally["skipped"] += 1
                break
            elif choice == "q":
                print("  ".join(f"{k} {v}" for k, v in tally.items()))
                return
    print("\n" + "  ".join(f"{k} {v}" for k, v in tally.items()))


def reject(args):
    """HUMAN ONLY. A proposed task is dropped. A task in `review` goes back to `ready` with the
    reason in its Notes; its branch and worktree stay, so the next dispatch picks up from them
    and its worker is told why. An epic ID whose brief is proposed drops the brief alone, as
    approving it approves the brief alone: its proposed tasks stay proposed, and aren't offered
    or started while the epic is dropped (`_tasks.epic_problem`); naming the epic again drops
    them. On any other epic, its ID names its proposed tasks."""
    if _config.SHARED:  # act on the latest state, and publish the result
        sync_for_count(args.ids, what="rejection")
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    briefs = [epics[i] for i in dict.fromkeys(args.ids) if i in epics and epics[i].get("status") == "proposed"]
    named = targets([i for i in args.ids if i not in {e["id"] for e in briefs}], tasks, epics)
    todo = [t for t in named if t["status"] == "proposed"]
    back = [t for t in named if t["status"] == "review"]
    if not briefs and not todo and not back:
        raise SystemExit("nothing in `proposed` or `review` among those ids")
    # shared mode: the branch is only in the clone dispatch held it on, so only that clone can reject it
    away = {t["id"]: who for t in back
            if (who := _tasks.held_elsewhere(t)) and not _gitops.branch_exists(_config.TASK_BRANCH + t["id"])}
    if away:
        back = [t for t in back if t["id"] not in away]
        said = "\n".join(f"refused: {tid} is held on {who}: reject it there" for tid, who in away.items())
        if not briefs and not todo and not back:
            raise SystemExit(said)
        print(said)
    if back and not args.reason:
        raise SystemExit("rejecting work in `review` needs --reason: it's what the next worker is told to fix")
    sign = signing()
    named_ids = {t["id"] for t in todo}
    for e in briefs:
        print(f"  {e['id']}  epic    {e['title']}  (its brief: proposed -> dropped)")
        print_brief(e)
        kids = sum(t.get("epic") == e["id"] and t["status"] == "proposed" and t["id"] not in named_ids for t in tasks.values())
        if kids:
            print(f"      (the brief only: its {kids} proposed task(s) stay proposed, and aren't offered or "
                  f"started while it's dropped; naming {e['id']} again drops them)")
    for t in todo + back:
        print(_tasks.line(t, tasks) + ("  (review -> ready)" if t in back else ""))
    what = " and ".join(x for x in (f"drop {len(briefs)} epic brief(s)" if briefs else "",
                                    f"drop {len(todo)} proposed" if todo else "",
                                    f"send {len(back)} in review back to ready" if back else "") if x)
    if not human_gate(f"{what[0].upper() + what[1:]}?"):
        raise SystemExit("aborted")
    for t in back:  # keep the rejected diff before anything moves; only git diff, offline
        try:
            ibase, _wt, ibranch, ipubs = held_branch(t, epics)
            _dispatch.save_incident(t, "reject", args.reason, ibase, ibranch, ipubs)
        except _gitops.GitError as err:
            print(f"ergon: no incident saved for {t['id']}: {err}", file=sys.stderr)
    with _gitops.state_lock():  # in shared mode, commits the change on the state branch, signed
        if sign:
            _shared.commit_pending()  # hand edits go in a commit of their own, not the signed one
        for t in briefs + todo:
            t["status"] = "dropped"
            if args.reason:
                t["_body"] = t["_body"].rstrip() + f"\n\nRejected {_tasks.today()}: {args.reason}\n"
            _tasks.dump(t, _tasks.EPIC_KEYS if _tasks.is_epic(t) else _tasks.TASK_KEYS)
        for t in back:
            t["status"] = "ready"
            _dispatch.add_note(t, f"rejected by {approver()}: {args.reason}", who="review")
            _tasks.dump(t, _tasks.TASK_KEYS)
        if sign:
            _gitops.sign_state("reject", [f"{_gitops.REJECT_TRAILER}: {t['id']}" for t in briefs + todo + back],
                               [t["_path"] for t in briefs + todo + back])
        for t in briefs + todo:
            _history.record("rejected", t, by=approver(), reason=args.reason, **{"from": "proposed", "to": "dropped"})
        for t in back:
            _history.record("rejected", t, by=approver(), reason=args.reason, **{"from": "review", "to": "ready"})
    print(", ".join(x for x in (f"dropped {len(briefs)} epic brief(s)" if briefs else "",
                                f"dropped {len(todo)}" if todo else "",
                                f"sent {len(back)} back to ready" if back else "") if x))
    if _config.SHARED:
        sync_for_count(args.ids, after=True, what="rejection")


def held_branch(t, epics):
    """(base, worktree, branch, companion worktrees) of a task dispatch held at `review`, as
    land() takes them. Raises GitError if its branch is gone."""
    branch = _config.TASK_BRANCH + t["id"]
    if not _gitops.branch_exists(branch):
        raise _gitops.GitError(f"{t['id']}: no branch {branch} to land; `ergon reject {t['id']} --reason R` "
                       f"sends it back to ready")
    base = _dispatch.base_branch(t, epics)
    wt = Path(_gitops.worktrees().get(branch) or _config.WORKTREES / t["id"])
    pubs = []
    for c in _config.COMPANIONS:
        if not _dispatch.companion_touch(t, c):
            continue
        if not _gitops.branch_exists(branch, c["path"]):
            raise _gitops.GitError(f"{t['id']}: no {c['name']} branch {branch}, though its touches: names {c['name']}")
        pubs.append((c, Path(_gitops.worktrees(c["path"]).get(branch) or wt / c["rel"])))
    return base, wt, branch, pubs


def accept(args):
    """HUMAN ONLY: land tasks that dispatch held at `review` the way dispatch would have (companions
    first, then the task branch into its epic branch or the trunk), mark them done, and close an
    epic whose last task this was. A landing that fails marks the task blocked, as in dispatch."""
    if _config.SHARED:  # act on the latest state, and publish the result
        sync_for_count(args.ids, what="acceptance")
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    todo = [t for t in targets(args.ids, tasks, epics, "review") if t["status"] == "review"]
    if not todo:
        raise SystemExit("nothing in `review` among those ids")
    # shared mode: task branches are never pushed, so only the clone dispatch held it on can land it
    away = {t["id"]: who for t in todo
            if (who := _tasks.held_elsewhere(t)) and not _gitops.branch_exists(_config.TASK_BRANCH + t["id"])}
    if away:
        todo = [t for t in todo if t["id"] not in away]
        said = "\n".join(f"refused: {tid} is held on {who}: its branch {_config.TASK_BRANCH}{tid} is in that "
                         f"clone, so accept it there" for tid, who in away.items())
        if not todo:
            raise SystemExit(said)
        print(said)
    sign = signing()
    for t in todo:
        print(_tasks.line(t, tasks) + f"  [{_config.TASK_BRANCH}{t['id']}]")
    if not human_gate(f"Accept and land these {len(todo)} task(s) as {approver()}?"):
        raise SystemExit("aborted")
    who, failed = f"{approver()} {_tasks.today()}", 0
    for tid in [t["id"] for t in todo]:
        passes, recorded = 0, False
        while True:  # a base that moved since the task was held: a pass on its merge, outside the lock
            moved = None
            with _gitops.state_lock():  # land() and finish_epic() expect it; also keeps dispatch off the trunk
                t, epics = _tasks.load(_config.ITEMS)[tid], _tasks.load(_config.EPICS)
                if t["status"] != "review":
                    print(f"skip {tid}: now {t['status']}")
                    break
                try:
                    base, wt, branch, pubs = held_branch(t, epics)
                    stray = [f"{tree}: {p}" for tree in [wt] + [pub for _, pub in pubs]
                             if tree.exists() for p in _gitops.dirty_paths(tree)]
                except _gitops.GitError as err:
                    print(f"{tid} not landed, still in review: {err}")
                    failed += 1
                    break
                if stray:
                    print(f"{tid} not landed, still in review: uncommitted changes would miss the merge; "
                          "commit or discard them, then accept again:\n  " + "\n  ".join(stray))
                    failed += 1
                    break
                if not recorded:
                    _history.record("accepted", t, by=approver())
                    recorded = True
                if _config.CONFLICT_ROUNDS:
                    try:
                        found = _dispatch.find_conflicts(base, wt, branch, pubs)
                    except _gitops.GitError:
                        found = None  # land() reports it, and blocks as before
                    if found:  # the base moved while it waited: a conflict round, then a new accept
                        for s in found:
                            _history.record("conflict", t, files=s["files"], base=s["base"], after="accept",
                                            companion=s["c"]["name"] if s["c"] else None)
                        where = "; ".join(f"{s['base']} in {', '.join(s['files'])}" if s["files"] else s["base"] for s in found)
                        _dispatch.put_status(tid, "ready", f"accepted by {who}, but it conflicts with {where}; "
                                             f"{_dispatch.ACCEPT_CONFLICT_MARK}", who="accept")
                        print(f"  ready    {tid}  conflicts with {where}: the next dispatch resolves it")
                        break
                try:
                    moved = _dispatch.moved_bases(base, wt, branch, pubs)
                except _gitops.GitError:
                    moved = None  # land() reports it
                if moved and all(s["clean"] for s in moved):
                    if passes >= _config.REMERGE_ROUNDS:  # it kept moving: the next dispatch takes it on
                        note = _dispatch.moving_note(moved, passes)
                        _dispatch.put_status(tid, "ready", f"accepted by {who}, but {note}; "
                                             f"{_dispatch.ACCEPT_REMERGE_MARK}", who="accept")
                        print(f"  ready    {tid}  {note}: the next dispatch runs the gates on its merge again")
                        break
                else:  # nothing moved since it was held (or a conflict, which land() blocks on)
                    moved = None
                    verdict, why = _dispatch.land(t, base, wt, branch, pubs, accepted=who)
                    if verdict != "done":
                        _dispatch.put_status(tid, "blocked", f"couldn't land it for {who}: {why}", who="accept")
                        _history.record("blocked", t, reason=f"couldn't land it for {who}: {why}", log=None, session=None, asked=False)
                        print(f"  blocked  {tid}  {why}")
                        failed += 1
                        break
                    _dispatch.put_status(tid, "done", f"accepted by {who}: {why}", who="accept")
                    if sign:  # landed already, so a failure to sign only warns
                        _gitops.sign_state("accept", [f"{_gitops.ACCEPT_TRAILER}: {tid}"], undo=False)
                    print(f"  done     {tid}  {why}")
                    e = epics.get(t.get("epic"))
                    if e and e["status"] == "open" and _gitops.epic_complete(e["id"], _tasks.load(_config.ITEMS)):
                        try:
                            _gitops.finish_epic(e["id"])
                        except _gitops.GitError as err:
                            print(f"  {e['id']} is complete but not merged: {err}")
                    break
            # outside the lock: merge what moved into the branch and run the gates on the merge (T-196)
            passes += 1
            where = _dispatch.bases_text(moved)
            print(f"  merging  {tid}  {where} moved since it was held: merging it into {branch} and running "
                  f"the gates on the merge (pass {passes}/{_config.REMERGE_ROUNDS})", flush=True)
            try:
                for s in moved:
                    _dispatch.merge_base_in(t, s, branch, passes, "accept")
                red, _fixable = _dispatch.merged_gates(t, base, wt, branch, pubs)
            except _gitops.GitError as err:
                print(f"{tid} not landed, still in review: {err}")
                failed += 1
                break
            if red:  # back to ready: the next dispatch runs the fix round and holds it at review again
                with _gitops.state_lock():
                    if _tasks.load(_config.ITEMS)[tid]["status"] == "review":
                        _dispatch.put_status(tid, "ready", f"accepted by {who}, but merged with {where}, which "
                                             f"moved while it waited, its gates went red: {red}; "
                                             f"{_dispatch.ACCEPT_REMERGE_MARK}", who="accept")
                print(f"  ready    {tid}  red on its merge with {where}: the next dispatch runs a fix round, "
                      "then it waits for your accept again")
                break
            print(f"  merging  {tid}  green on the merge", flush=True)
    if _config.SHARED:
        sync_for_count(args.ids, after=True, what="acceptance")
    if failed or away:
        sys.exit(1)


RE_PLAN = ("An answer steers the work only. It never changes approved:, tier, touches: or accept:. If the "
           "question needs one of those, it's a re-plan: don't answer. Edit the task by hand, or "
           "`ergon close {id} --as dropped --reason R` and file a new one.")


def show_questions(t):
    """Print the question(s) t's worker parked it on, each with its options, and the re-plan rule."""
    for n, (q, opts) in enumerate(_dispatch.asked(t)[0], 1):
        print(f"\nQ{n}  {q}")
        for i, o in enumerate(opts, 1):
            print(f"    {i}. {o}" + ("  (recommended)" if i == 1 else ""))
    print("\n" + RE_PLAN.format(id=t["id"]))


def answer(args):
    """HUMAN ONLY: answer the question(s) a dispatched worker parked its task on."""
    t = _tasks.load(_config.ITEMS).get(args.id)
    if not t:
        raise SystemExit(f"no such task: {args.id}")
    if not _tasks.is_asking(t):
        raise SystemExit(f"{args.id} isn't waiting on an answer (status {t['status']}"
                         + (", answered already: the next dispatch resumes it" if t.get("answered") else "") + ")")
    print(f"{args.id}  {t['title']}")
    show_questions(t)
    answer_task(t, " ".join(args.text).strip() if args.text else "")


def answer_task(t, text=""):
    """HUMAN ONLY, the one path for `ergon answer` and the interactive review: take an answer for
    asking task t (with no text, offer each question's options to pick from) and confirm it; then
    write it to Notes, swap `asking:` for `answered:` and set the task ready, so the next dispatch
    resumes the worker's session with it (or a fresh worker, if the cache is gone). Never touches
    approved:, tier, touches: or accept:. Raises SystemExit, writing nothing, if no answer is
    given or confirmed."""
    tid = t["id"]
    if not text:
        qs, parts = _dispatch.asked(t)[0], []
        for n, (q, opts) in enumerate(qs or [("(no question recorded)", [])], 1):
            got = ask(f"\nQ{n} answer" + (f" (1-{len(opts)}, or your own words)" if opts else "") + ": ")
            if got.isdigit() and opts and 1 <= int(got) <= len(opts):
                got = opts[int(got) - 1]
            if not got:
                raise SystemExit("no answer given; nothing written")
            parts.append(got)
        text = parts[0] if len(parts) == 1 else " ".join(f"Q{n}: {a}." for n, a in enumerate(parts, 1))
    text = _dispatch.note_line(text)
    print(f"\nanswer: {text}")
    if not human_gate(f"Send this answer to {tid}'s worker as {approver()}?"):
        raise SystemExit("aborted")
    with _gitops.state_lock():
        t = _tasks.load(_config.ITEMS)[tid]
        if not _tasks.is_asking(t):
            raise SystemExit(f"{tid} is now {t['status']}; nothing written")
        session = t.pop("asking")
        t["status"], t["answered"] = "ready", session
        _dispatch.add_note(t, text, who=f"answer by {approver()}")
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("answered", t, by=approver(), answer=text,
                        session=None if session == _dispatch.NO_SESSION else session)
    print(f"{tid} is ready: the next `ergon dispatch` resumes its worker with your answer")


CHOICE_STEERS = ("A choice steers only: it never changes tier, touches:, accept: or approved:. If the chosen "
                 "option needs a different tier or touches:, edit those before approving.")


def choose(args):
    """HUMAN ONLY: pick one of a proposed task's options (`ergon choose ID N`), or say how it is
    built in your own words (`--own "<text>"`). With neither, offers the options to pick from."""
    if in_agent_shell():
        raise SystemExit("refused: running inside an agent shell. A human chooses from their own terminal.")
    t = _tasks.load(_config.ITEMS).get(args.id)
    if not t:
        raise SystemExit(f"no such task: {args.id}")
    if args.n is not None and args.own is not None:
        raise SystemExit("give an option's number or --own \"<text>\", not both")
    choose_task(t, args.n, args.own)


def choose_task(t, n=None, own=None):
    """HUMAN ONLY, the one path for `ergon choose` and `c` in the interactive review: check that t
    is proposed and has a well-formed `## Options` section, take the choice (a number, or the
    human's own text; with neither, ask), print it and confirm; then write or replace the
    section's `Chosen:` line and record a `chosen` row. Never touches tier, touches:, accept: or
    approved:. Raises SystemExit, writing nothing, if any of that fails or isn't confirmed."""
    tid = t["id"]
    if t["status"] != "proposed":
        raise SystemExit(f"{tid} is {t['status']}: a choice is made on a proposed task only, before it is "
                         f"approved (after that it's a re-plan: edit the task by hand and approve it again)")
    if not _tasks.has_options(t):
        raise SystemExit(f"{tid} has no ## Options section to choose from")
    opts, chosen = _tasks.options(t)
    if len(opts) < 2:
        raise SystemExit(f"{tid}: {_tasks.options_problems(t)[0]}; fix the section first")
    print(f"{tid}  {t['title']}\n\nOPTIONS")
    _tasks.print_options(t)
    if chosen:
        print(f"\n(replaces the current choice: {chosen['line']})")
    if n is None and own is None:
        got = ask(f"\nchoice (1-{len(opts)}, or your own words): ").strip()
        if not got:
            raise SystemExit("no choice given; nothing written")
        if got.isdigit():
            n = got
        else:
            own = got
    if own is not None:
        text = _dispatch.note_line(_dispatch.md_trim(own))
        if not text:
            raise SystemExit("--own needs the text of your choice")
        option, what = "own", f"your own: {text}"
    else:
        if not str(n).isdigit() or not 1 <= int(n) <= len(opts):
            raise SystemExit(f"{tid}: pick an option from 1 to {len(opts)}, or --own \"<text>\"")
        option, text = int(n), opts[int(n) - 1]
        what = f"{option}. {text}"
    print(f"\nchoice: {what}\n\n{CHOICE_STEERS}")
    if not human_gate(f"Choose this for {tid} as {approver()}?"):
        raise SystemExit("aborted")
    with _gitops.state_lock():  # shared mode: the write is a state commit, like a `set`
        t = _tasks.load(_config.ITEMS).get(tid)
        if not t or t["status"] != "proposed" or not _tasks.has_options(t):
            raise SystemExit(f"{tid} is now {t['status'] if t else 'gone'}; nothing written")
        t["_body"] = _tasks.set_chosen(t["_body"], _tasks.chosen_line(option, approver(), _tasks.today(),
                                                                      text))
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("chosen", t, by=approver(), option=option, text=text)
    print(f"{tid}: chose {what}. Approve it from your own terminal: ergon approve {tid}")


def pin(args):
    """HUMAN ONLY: `ergon pin ID first|last|clear` writes or removes `pin:`, which overrides the
    priority score in the order. Reorders only, so it asks for no confirmation."""
    if in_agent_shell():
        raise SystemExit("refused: running inside an agent shell. A human pins from their own terminal.")
    with _gitops.state_lock():
        t = _tasks.load(_config.ITEMS).get(args.id)
        if not t:
            raise SystemExit(f"no such task: {args.id}")
        if args.where == "clear":
            t.pop("pin", None)
        else:
            t["pin"] = args.where
        _tasks.dump(t, _tasks.TASK_KEYS)
        _history.record("pinned", t, by=approver(), pin=None if args.where == "clear" else args.where)
    print(f"{args.id}: " + ("pin cleared" if args.where == "clear" else f"pinned {args.where}"))


def close(args):
    """HUMAN ONLY: close approved tasks (ready / blocked / doing) whose work already landed
    elsewhere (--as done) or that no longer apply (--as dropped). Skips the epic-branch check
    on purpose: the work isn't happening on the branch. Proposed tasks go through `reject`.
    Takes the state lock itself (the CLI doesn't), so the shared-mode syncs run outside it."""
    if _config.SHARED:  # act on the latest state, and publish the result
        sync_for_count(args.ids, what="close")
    tasks, epics = _tasks.load(_config.ITEMS), _tasks.load(_config.EPICS)
    todo = []
    for i in args.ids:
        t = tasks.get(i)
        if not t:
            raise SystemExit(f"no such task: {i}")
        if t["status"] == "proposed":
            raise SystemExit(f"{i} is proposed: use `ergon reject {i}` (nobody approved it)")
        if t["status"] in ("done", "dropped"):
            print(f"skip {i}: already {t['status']}")
            continue
        todo.append(t)
    if not todo:
        raise SystemExit("nothing to close")
    sign = signing()
    for t in todo:
        print(_tasks.line(t, tasks))
    if not human_gate(f"Close these {len(todo)} task(s) as {args.as_}?"):
        raise SystemExit("aborted")
    with _gitops.state_lock():
        if sign:
            _shared.commit_pending()  # hand edits go in a commit of their own, not the signed one
        for t in todo:
            t["status"] = args.as_
            t.pop("asking", None)
            t.pop("answered", None)
            t.pop("limited", None)
            t["_body"] = t["_body"].rstrip() + f"\n\nClosed {_tasks.today()} as {args.as_}: {args.reason}\n"
            _tasks.dump(t, _tasks.TASK_KEYS)
        if sign:
            _gitops.sign_state("close", [f"{_gitops.CLOSE_TRAILER}: {t['id']} {args.as_}" for t in todo],
                               [t["_path"] for t in todo])
        for t in todo:
            _history.record("closed", t, by=approver(), reason=args.reason, **{"as": args.as_})
    print(f"closed {len(todo)} as {args.as_}")
    for eid in sorted({t["epic"] for t in todo if t.get("epic") in epics}):
        if epics[eid]["status"] == "open" and _gitops.epic_complete(eid, _tasks.load(_config.ITEMS)):
            try:
                _gitops.finish_epic(eid)
            except _gitops.GitError as err:
                print(f"{eid} is complete but not merged: {err}")
    if _config.SHARED:
        sync_for_count(args.ids, after=True, what="close")



# ---------- Claude Code hooks ----------
# The plugin wires these (hooks/hooks.json); dispatch also injects the guard into every worker.
# Like human_gate, a speed bump: it stops an agent drifting into approving its own work, not one
# set on getting around it. Any crash fails open so a bug here never wedges a session, and a
# project with no .ergon.toml is left alone entirely.

# Shell writes: in-place editors, redirects (not to /dev/null or another fd), tee, mv/cp/rm.
SHELL_WRITE = re.compile(r"\bsed\s+(-\w+\s+)*-i|\bperl\s+-\w*i|(?<![0-9&>])>(?!\s*/dev/null|&)|"
                         r"\btee\b|\b(mv|cp|rm|truncate|install)\s|python[0-9.]*\s+-c|<<")
TAR_WRITE = re.compile(r"\btar\s+[xcru]|\btar\s[^;&|]*(\s-\w*[xcru]|--(extract|create|append|update|get))")
# The human-only commands as plain text. human_cmd falls back to it for text it can't split into
# words and for strings handed to a program that runs them as code (bash -c, python -c, ssh, ...).
HUMAN_CMD = re.compile(r"\bergon(?:\.py)?[\"']?\s+(approve|reject|close|accept|answer|sync|pin|choose)\b|\breview\s+(-i|--interactive)\b")
HUMAN_VERBS = {"approve", "reject", "close", "accept", "answer", "sync", "pin", "choose"}
# Programs that run a string argument or their stdin as code: what's quoted for them is a command.
CODE_RUNNER = re.compile(r"(?:ba|da|z|k|mk|c|tc|fi|a)?sh|busybox|eval|env|ssh|su|watch|script|tmux|screen|"
                         r"parallel|flock|expect|osascript|python[0-9.]*|pypy[0-9.]*|perl|ruby|node|nodejs|"
                         r"deno|bun|php|lua|tclsh|[gmn]?awk")
GUARD_TOOLS = "Edit|Write|MultiEdit|Bash"
GIT_WRITE = re.compile(r"\bgit\b[^;&|]*\s(add|commit|checkout|switch|reset|restore|merge|rebase|"
                       r"cherry-pick|revert|stash|apply|am|pull|push|fetch|branch|tag|worktree|"
                       r"clean|update-ref|commit-tree|notes|gc|prune)\b")


def shell_commands(s, i=0, close=None):
    """Split shell text into simple commands, each (its words after quote removal, its heredoc
    bodies as (text, expands)). A command piped from one with heredocs shares them (`cat <<EOF | sh`
    runs the body). The commands inside $(...), `...` and (...) come out as commands of
    their own; a word holding a substitution keeps its source text. Returns (commands, the index
    after `close`). Raises ValueError on text it can't split (an open quote, an unclosed paren)."""
    cmds, words, docs, word, pending, n = [], [], [], [], [], len(s)
    inword = False

    def end_word():
        nonlocal inword
        if inword:
            words.append("".join(word))
        word.clear()
        inword = False

    def end_cmd():  # a command with no words keeps its heredocs for the next (`cat <<EOF |` + newline)
        nonlocal words, docs
        end_word()
        if words:
            cmds.append((words, docs))
            words, docs = [], []

    def sub(i):  # s[i:] starts with "$(": the commands up to its ")" are commands too
        found, j = shell_commands(s, i + 2, ")")
        cmds.extend(found)
        word.append(s[i:j])
        return j

    def tick(i):  # s[i] opens a `...` substitution
        j = i + 1
        while j < n and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        if j >= n:
            raise ValueError("open backtick")
        cmds.extend(shell_commands(re.sub(r"\\([`$\\\"])", r"\1", s[i + 1:j]))[0])
        word.append(s[i:j + 1])
        return j + 1

    def dquote(i):  # s[i] is the char after an opening "
        while i < n and s[i] != '"':
            if s[i] == "\\" and i + 1 < n:
                if s[i + 1] in '$`"\\':
                    word.append(s[i + 1])
                elif s[i + 1] != "\n":
                    word.append(s[i:i + 2])
                i += 2
            elif s.startswith("$(", i):
                i = sub(i)
            elif s[i] == "`":
                i = tick(i)
            else:
                word.append(s[i])
                i += 1
        if i >= n:
            raise ValueError("open double quote")
        return i + 1

    def heredoc(i):  # s[i] is the char after "<<"
        strip = s.startswith("-", i)
        i += strip
        while i < n and s[i] in " \t":
            i += 1
        j = i
        while j < n and s[j] not in " \t\n;&|<>()":
            j += 1
        delim = re.sub(r"[\"'\\]", "", s[i:j])
        if not delim:
            raise ValueError("heredoc without a delimiter")
        pending.append((delim, delim == s[i:j], strip, docs))
        return j

    def bodies(i):  # s[i] starts the line after one that opened heredocs
        while pending:
            delim, expands, strip, into = pending.pop(0)
            lines = []
            while True:
                if i >= n:
                    raise ValueError("open heredoc")
                j = s.find("\n", i)
                j = n if j < 0 else j
                line, i = s[i:j], j + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    break
                lines.append(line)
            into.append(("\n".join(lines), expands))
        return i

    while i < n:
        c = s[i]
        if c == "\\":
            if not s.startswith("\n", i + 1):
                word.append(s[i + 1:i + 2])
                inword = True
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise ValueError("open single quote")
            word.append(s[i + 1:j])
            inword, i = True, j + 1
        elif s.startswith("$'", i):
            j = i + 2
            while j < n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                raise ValueError("open $' quote")
            word.append(s[i + 2:j])
            inword, i = True, j + 1
        elif c == '"':
            inword, i = True, dquote(i + 1)
        elif s.startswith("$(", i):
            inword, i = True, sub(i)
        elif c == "`":
            inword, i = True, tick(i)
        elif c == "#" and not inword:
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_cmd()
            i = bodies(i + 1)
        elif s.startswith("||", i):
            end_cmd()
            i += 2
        elif c == "|":  # a pipe: the heredocs read so far feed the next command's stdin too
            piped = docs
            end_cmd()
            docs = piped
            i += 2 if s.startswith("|&", i) else 1
        elif c in ";&":
            end_cmd()
            i += 1
        elif c == "(":
            end_cmd()
            found, i = shell_commands(s, i + 1, ")")
            cmds.extend(found)
        elif c == ")":
            if close != ")" or pending:
                raise ValueError("unmatched )")
            end_cmd()
            return cmds, i + 1
        elif c in "<>":
            end_word()
            if s.startswith("<<", i) and not s.startswith("<<<", i):
                i = heredoc(i + 2)
            else:
                i += 1
                while i < n and s[i] in "<>&|":
                    i += 1
        else:
            word.append(c)
            inword, i = True, i + 1
    if close or pending:
        raise ValueError("unclosed " + (close or "heredoc"))
    end_cmd()
    return cmds, i


def _program(word):
    return word.rstrip("/").rsplit("/", 1)[-1]


# git's global options that take the next word as their value (git -C dir, git -c key=value).
GIT_OPT_ARG = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env",
               "--attr-source"}
# The config keys whose value git runs as a command (from git-config(1) and the commands' own pages),
# lowercased: section and variable names are case-insensitive. git_config_command reads a value
# as the command line it becomes; T-060 refuses a definition of one of these whose value it can't see.
# Paths to a tool's binary (difftool.<tool>.path, ...) name a program, not a command line, so aren't here.
GIT_COMMAND_KEYS = re.compile(
    r"alias\..+"                                       # a `!` alias is shell text, any other a git call
    r"|core\.(editor|pager|sshcommand|fsmonitor|askpass|gitproxy|alternaterefscommand)"
    r"|sequence\.editor|pager\..+|interactive\.difffilter"
    r"|diff\.external|diff\..+\.(textconv|command)"
    r"|(diff|merge)tool\..+\.cmd|merge\..+\.driver"
    r"|filter\..+\.(clean|smudge|process)"
    r"|credential\.(.+\.)?helper"                      # run as `git credential-<value>` unless `!` or a path
    r"|gpg\.program|gpg\..+\.program|gpg\.ssh\.defaultkeycommand"
    r"|remote\..+\.(uploadpack|receivepack)|uploadpack\.packobjectshook"
    r"|submodule\..+\.update"                          # only a `!` value is a command
    r"|tar\..+\.command|trailer\..+\.(command|cmd)|hook\..+\.command"
    r"|sendemail\.(.+\.)?(smtpserver|sendmailcmd|tocmd|cccmd|headercmd)|imap\.tunnel"
    r"|(browser|man)\..+\.cmd|guitool\..+\.cmd|instaweb\.httpd")
# The environment variables whose value git runs as a command (git(1), git-var(1)): its editor and
# pager, falling back to VISUAL/EDITOR and PAGER; ssh, askpass and proxy programs; an external diff.
# The askpass and ssh programs are run as a path, not shell text, but are read as commands anyway.
# less, git's default pager, runs LESSOPEN / LESSCLOSE (a leading `|`, `||` or `-` is a mode flag).
GIT_COMMAND_ENV = {"GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "VISUAL", "EDITOR", "GIT_PAGER", "PAGER",
                   "GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND",
                   "GIT_EXTERNAL_DIFF", "LESSOPEN", "LESSCLOSE"}
# Programs whose NAME=value arguments set a variable a later command inherits.
SHELL_DECLARE = {"export", "declare", "typeset", "local", "readonly"}
# Per git subcommand, the options whose value git runs as a command ("cmd"; "cmd=" when the value
# must be attached, as for an optional one) or sets as config ("config", key=value): long options as
# "<shortest abbreviation>|<rest>", then short letters, then the other short letters that take a value
# (the rest of a cluster is theirs).
GIT_CMD_OPTS = {
    "rebase": ({"--ex|ec": "cmd"}, {"x": "cmd"}, "sXCS"),
    "difftool": ({"--e|xtcmd": "cmd"}, {"x": "cmd"}, "t"),
    "filter-branch": ({f"--{o}|": "cmd" for o in ("setup", "env-filter", "tree-filter", "index-filter",
                                                  "parent-filter", "msg-filter", "commit-filter",
                                                  "tag-name-filter")}, {}, ""),
    "clone": ({"--u|pload-pack": "cmd", "--c|onfig": "config"}, {"u": "cmd", "c": "config"}, "obj"),
    "fetch": ({"--u|pload-pack": "cmd"}, {}, "jo"),
    "pull": ({"--u|pload-pack": "cmd"}, {}, "josX"),
    "ls-remote": ({"--u|pload-pack": "cmd", "--e|xec": "cmd"}, {"u": "cmd"}, "o"),
    "fetch-pack": ({"--u|pload-pack": "cmd", "--e|xec": "cmd"}, {}, ""),
    "push": ({"--r|eceive-pack": "cmd", "--e|xec": "cmd"}, {}, "o"),
    "send-pack": ({"--r|eceive-pack": "cmd", "--e|xec": "cmd"}, {}, ""),
    "archive": ({"--e|xec": "cmd"}, {}, "o"),
    "grep": ({"--o|pen-files-in-pager": "cmd="}, {"O": "cmd="}, "efABCm"),
    "send-email": ({"--to-c|md": "cmd", "--cc-c|md": "cmd", "--header-c|md": "cmd", "--sendmail-c|md": "cmd",
                    "--smtp-server|": "cmd"}, {}, ""),
    "instaweb": ({"--h|ttpd": "cmd"}, {"d": "cmd"}, "bpm"),  # run as `$httpd conf`, word-split
}
# Config keys whose value is a remote's URL, and the url.<base>.insteadOf keys whose <base> is one:
# an ext:: URL there is a command git runs (ext_command) on the next fetch or push.
GIT_URL_KEYS = re.compile(r"remote\..+\.(url|pushurl)|submodule\..+\.url")
GIT_INSTEADOF_KEY = re.compile(r"url\.(.+)\.(push)?insteadof", re.I | re.S)


def ext_command(url):
    """The command line git runs for an ext:: URL (git-remote-ext(1)), or None if url isn't one.
    Its argv is the text after `ext::` split at each unescaped space, with `% ` a literal space,
    `%%` a percent, `%s`/`%S` the service name, and an argument starting `%G` or `%V` dropped.
    The transport name is matched case-blind; an escape git would reject is read leniently."""
    m = re.match(r"(?is)ext::(.*)", url)
    if not m:
        return None
    args, cur, keep, text, i = [], "", True, m.group(1), 0
    while i <= len(text):
        c = text[i] if i < len(text) else " "
        if i == len(text) or c == " ":
            if keep:
                args.append(cur)
            cur, keep = "", True
        elif c == "%" and i + 1 < len(text):
            i, e = i + 1, text[i + 1]
            if e in "GV" and cur == "":
                keep = False
            cur += {"s": "upload-pack", "S": "git-upload-pack"}.get(e, e)
        else:
            cur += c
        i += 1
    return shlex.join(args)


def git_config_command(key, val):
    """The command line git runs from config key = val, or None if it runs none: a `!` alias as shell
    text and any other as the git call it expands to, a credential helper as `git credential-<val>`
    unless it's `!` text or an absolute path, a submodule's update only if it's `!` text, an ext::
    URL as a remote's or submodule's URL or as the <base> an insteadOf rewrites to (ext_command),
    and the value itself for the other GIT_COMMAND_KEYS."""
    k = key.lower()
    if GIT_URL_KEYS.fullmatch(k):
        return ext_command(val)
    if m := GIT_INSTEADOF_KEY.fullmatch(key):
        return ext_command(m.group(1))
    if not GIT_COMMAND_KEYS.fullmatch(k):
        return None
    if k.startswith("alias."):
        return val[1:] if val.startswith("!") else "git " + val
    if k.startswith("credential."):
        return val[1:] if val.startswith("!") else val if val.startswith("/") else "git credential-" + val
    if k.startswith("submodule."):
        return val[1:] if val.startswith("!") else None
    return val


def _option_values(rest, longs, shorts, valued):
    """(kind, value) for each option of GIT_CMD_OPTS' kinds in rest, up to `--`: `--opt value`,
    `--opt=value`, or an abbreviation of it; `-x value`, `-xvalue`, or x late in a cluster."""
    j = 0
    while j < len(rest):
        a, j = rest[j], j + 1
        if a == "--":
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            kind = next((kd for o, kd in longs.items()
                         if name.startswith(o.split("|")[0]) and o.replace("|", "").startswith(name)), None)
            if kind and eq:
                yield kind.rstrip("="), val
            elif kind and not kind.endswith("=") and j < len(rest):
                yield kind, rest[j]
                j += 1
        elif re.match(r"-[^-]", a):  # a cluster of short options: -x cmd, -xcmd, -ix cmd
            for p, c in enumerate(a[1:], 2):
                if c in shorts:
                    kind = shorts[c]
                    if a[p:]:
                        yield kind.rstrip("="), a[p:]
                    elif not kind.endswith("=") and j < len(rest):
                        yield kind, rest[j]
                        j += 1
                    break
                if c in valued:  # takes the rest of the word as its value
                    break


def _git_calls(words):
    """Each git call among words, as (the settings its -c and --config-env options make, its
    subcommand, the words after it). A setting is (key, value); --config-env's value is None, as
    git reads it from the environment variable it names."""
    for k, w in enumerate(words):
        prog, i, sets = _program(w), k + 1, []
        if prog == "git":
            while i < len(words) and words[i].startswith("-"):
                a = words[i]
                if a.startswith("--config-env="):
                    sets.append((a[len("--config-env="):].partition("=")[0], None))
                elif a in GIT_OPT_ARG and i + 1 < len(words):
                    i += 1
                    key, eq, val = words[i].partition("=")
                    if a == "-c" and eq:
                        sets.append((key, val))
                    elif a == "--config-env":
                        sets.append((key, None))
                i += 1
            sub, rest = (words[i], words[i + 1:]) if i < len(words) else ("", [])
        elif prog.startswith("git-"):  # a dashed form: $(git --exec-path)/git-filter-branch
            sub, rest = prog[4:], words[i:]
        else:
            continue
        yield sets, sub, rest


def git_config_sets(words):
    """(key, value) for each config entry a git call among words sets: `git -c key=value`,
    `git --config-env key=VAR` (value None: it's in the environment), `git config key value` (any
    adjacent pair, so every form of it) and `git clone -c/--config key=value`."""
    out = []
    for sets, sub, rest in _git_calls(words):
        out += sets
        if sub == "config":
            out += [(a, rest[j + 1]) for j, a in enumerate(rest[:-1]) if "=" not in a]
        elif sub == "clone":
            out += [tuple(v.split("=", 1)) for kind, v in _option_values(rest, *GIT_CMD_OPTS[sub])
                    if kind == "config" and "=" in v]
    return out


def git_commands(words):
    """The command lines a git call among words runs from its own arguments, which the lexer saw
    as data: the value of each GIT_CMD_OPTS option (rebase --exec, filter-branch's filters, difftool
    --extcmd, instaweb --httpd, ...), `submodule foreach`'s command, the command of each ext:: URL
    among its arguments (clone, fetch, push, ls-remote, remote add, archive --remote=...; any
    subcommand, as a word or an --option= value), and each GIT_COMMAND_KEYS or URL value it sets
    (git_config_sets) as git_config_command reads it. A later `git x` or `git fetch x` names no
    verb, so an alias or a remote is checked where it is defined; one whose value the text doesn't
    hold is git_config_reason's."""
    out = [c for key, val in git_config_sets(words)
           if val is not None and (c := git_config_command(key, val)) is not None]
    for _, sub, rest in _git_calls(words):
        for a in rest:  # an ext:: URL is a command wherever git takes a URL
            if (c := ext_command(re.sub(r"^--[^=]+=", "", a))) is not None:
                out.append(c)
        if sub in ("submodule", "submodule--helper"):
            j = next((m for m, a in enumerate(rest) if not a.startswith("-") or a == "--"), len(rest))
            if rest[j:j + 1] == ["foreach"]:  # one word is shell text, more are a command and its args
                args = rest[j + 1:]
                while args and args[0].startswith("-"):
                    args, dashdash = args[1:], args[0] == "--"
                    if dashdash:
                        break
                if args:
                    out.append(args[0] if len(args) == 1 else shlex.join(args))
        elif sub in GIT_CMD_OPTS:
            out += [val for kind, val in _option_values(rest, *GIT_CMD_OPTS[sub]) if kind == "cmd"]
    return out


def _env_config_commands(vals, name):
    """The command lines git runs from the config the GIT_CONFIG_* variable name sets, given the
    values vals holds so far: each GIT_CONFIG_PARAMETERS entry ('key'='value' ...), or the
    GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> pair it is half of, as git_config_command reads them
    (so a URL key's ext:: value too). A half the text doesn't hold, or a value that can't be split,
    gives none: git_config_reason refuses those."""
    if name == "GIT_CONFIG_PARAMETERS":
        try:
            pairs = [e.partition("=")[::2] for e in shlex.split(vals[name])]
        except ValueError:
            return []
    else:
        n = name.rsplit("_", 1)[1]
        pairs = [(vals.get(f"GIT_CONFIG_KEY_{n}"), vals.get(f"GIT_CONFIG_VALUE_{n}"))]
    return [c for key, val in pairs
            if key is not None and val is not None and (c := git_config_command(key, val)) is not None]


def _env_assignments(words):
    """(name, "+" or "", value) of each NAME=value or NAME+=value among a simple command's words that
    sets the environment of what it runs or of later commands: ahead of its program (or alone),
    as an argument of export / declare, or ahead of a git call behind a wrapper (sudo, env, nice).
    Anywhere else (after git, as echo's argument) it's data."""
    git_at = next((k for k, w in enumerate(words) if _program(w) == "git" or _program(w).startswith("git-")), -1)
    head = next((k for k, w in enumerate(words) if not ASSIGNMENT.match(w)), len(words))
    declares = head < len(words) and _program(words[head]) in SHELL_DECLARE
    for k, w in enumerate(words):
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)", w, re.S)
        if m and (k < head or declares or k < git_at):
            yield m.groups()


def env_commands(cmds):
    """The command lines git runs from the environment a list of shell commands sets, in order: the
    GIT_COMMAND_ENV variables, and the command-valued config of GIT_CONFIG_PARAMETERS and of each
    GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> pair (_env_config_commands). Set as `GIT_PAGER=x git
    log`, `sudo GIT_EDITOR=x git commit` (an assignment ahead of a git call), or a lone `PAGER=x` or
    `export PAGER=x` a later git call inherits. `NAME+=x` appends to the value set earlier in the
    text. An assignment-shaped word after git, or as another program's argument (echo, a commit
    message), is data."""
    vals, out = {}, []
    for words, _ in cmds:
        for name, append, val in _env_assignments(words):
            if not (name in GIT_COMMAND_ENV or GIT_CONFIG_ENV.fullmatch(name)):
                continue
            vals[name] = (vals.get(name, "") if append else "") + val
            if name.startswith("GIT_CONFIG_"):
                out += _env_config_commands(vals, name)
            else:
                out.append(vals[name].lstrip("|-") if name.startswith("LESS") else vals[name])
    return out


def human_cmd(cmd):
    """Whether a shell command runs a human-only ergon command: approve, reject, close, accept,
    answer, sync, pin, choose, or the interactive review. Only a real call counts: `ergon` (ergon.py, the package's
    folder or __main__.py) as a word of a simple command followed by the verb, wherever that
    command sits (after &&, inside $(...), behind sudo or xargs, in a heredoc fed to a shell). The
    verbs inside a quoted grep pattern or commit message are data and pass. Strings given to a
    program that runs them as code are checked as text, as is anything that can't be split. The
    commands git runs from its own arguments (git_commands: rebase --exec, submodule foreach, an
    alias or a command-valued config key it sets) are checked as commands, as are the ones it runs
    from the environment a command line sets for it (env_commands: GIT_PAGER=..., GIT_EDITOR=...,
    GIT_CONFIG_PARAMETERS=..., GIT_CONFIG_KEY_<n>=... GIT_CONFIG_VALUE_<n>=...)."""
    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        return bool(HUMAN_CMD.search(cmd))
    if any(human_cmd(t) for t in env_commands(cmds)):
        return True
    for words, docs in cmds:
        if any(CODE_RUNNER.fullmatch(_program(w)) for w in words):
            if any(HUMAN_CMD.search(t) or (t != cmd and human_cmd(t)) for t in words + [d for d, _ in docs]):
                return True
        if any(expands and re.search(r"\$\(|`", d) and HUMAN_CMD.search(d) for d, expands in docs):
            return True
        if any(human_cmd(t) for t in git_commands(words)):
            return True
        for k, w in enumerate(words):
            if _program(w) not in ("ergon", "ergon.py") and not w.endswith("ergon/__main__.py"):
                continue
            rest = words[k + 1:]
            at = next((m for m, a in enumerate(rest) if not a.startswith("-")), None)
            if at is None:
                continue
            if rest[at] in HUMAN_VERBS:
                return True
            if rest[at] == "review" and any(a == "-i" or (a.startswith("--i") and "--interactive".startswith(a))
                                            for a in rest[at + 1:]):
                return True
    return False


HUMAN_MSG = ("approve / reject / close / accept / answer / sync / pin / choose / review -i are human-only; ask the "
             "user to run them in their own terminal")
# The sections GIT_COMMAND_KEYS names keys in: `git config --rename-section user alias` turns keys the
# guard never read as commands (user.name) into ones git runs.
GIT_COMMAND_SECTIONS = {"alias", "core", "sequence", "pager", "interactive", "diff", "difftool", "mergetool", "merge",
                        "filter", "credential", "gpg", "remote", "uploadpack", "submodule", "tar", "trailer", "hook",
                        "sendemail", "imap", "browser", "man", "guitool", "instaweb"}
# git's config from the environment: GIT_CONFIG_PARAMETERS ('key'='value' ...) and the GIT_CONFIG_COUNT
# entries GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n>. Group 1 the name, 2 the <n>, 3 `=value` (or `+=...`).
GIT_CONFIG_ENV = re.compile(r"(GIT_CONFIG_PARAMETERS|GIT_CONFIG_(?:KEY|VALUE)_([0-9]+))(\+?=.*)?", re.S)
# One of those names anywhere in a word, and what before it makes a mention a read: `$NAME`, `${NAME}`,
# `${!NAME}`, `${#NAME}`, `env -uNAME`, `env --unset=NAME`. A `${NAME:=value}` sets it.
GIT_CONFIG_ENV_IN = re.compile(r"GIT_CONFIG_(?:PARAMETERS|KEY_|VALUE_)")
GIT_CONFIG_ENV_READ = re.compile(r"(?:\$|\$\{[!#]?|-u|--unset=)$")
# Programs whose arguments name an environment variable without setting it.
ENV_READERS = {"printenv", "echo", "grep", "egrep", "fgrep", "rg", "unset"}
DYNAMIC_ENV_MSG = ("this command sets a GIT_CONFIG_* variable whose name the guard can't read (an expansion in the "
                   "name, a nameref, `${NAME:=value}`): git can read an alias or command-valued key from it, so it's "
                   "refused. Write `git -c <key>=<value> ...` out in full, or ask the user")


def _unseen(val):
    """Whether a config value isn't all in the text: a `$` or backtick expands it, in the shell now
    or, for a value git runs as shell text, when git runs it (`!$V` reads V then, `!$1` the alias's
    arguments)."""
    return "$" in val or "`" in val


def unseen_config_msg(key, why):
    return (f"git runs the value of `{key}` as a command, and the guard checks that command where it is "
            f"defined, but here it can't see it: {why}. So the definition is refused. Write the value out "
            f"in full (`git -c {key}=<command> ...` or `git config {key} <command>`), or ask the user")


def git_config_reason(cmd):
    """Why a shell command defines a git alias or GIT_COMMAND_KEYS key in a way human_cmd can't
    check, or None: with `git --config-env` (the value is in an environment variable), with a `$` or
    backtick in the value, by renaming a section into or out of one that holds such keys, or through
    GIT_CONFIG_PARAMETERS / GIT_CONFIG_KEY_<n> + GIT_CONFIG_VALUE_<n>. A value the text holds there
    is read like `git -c`'s, a human-only verb in it refused; a key or value set by an earlier command
    can't be seen, so its other half is refused. Strings run as code are checked too."""
    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        if re.search(r"--config-env|rename-section|GIT_CONFIG_(PARAMETERS|KEY_|VALUE_)", cmd):
            return ("this command sets git config through --config-env, a section rename or GIT_CONFIG_*, and "
                    "the guard can't split it to see what it defines, so it's refused")
        return None
    env = []  # (name, n, value or None) of each GIT_CONFIG_* any of its commands sets
    for words, docs in cmds:
        # bash -c '...', a heredoc fed to sh. env's own words are read below; so is a lone NAME=value.
        if any(CODE_RUNNER.fullmatch(_program(w)) and _program(w) != "env" for w in words):
            for t in words + [d for d, _ in docs]:
                if t == cmd or (GIT_CONFIG_ENV.fullmatch(t) and not re.search(r"\s", t)):
                    continue
                if why := git_config_reason(t):
                    return why
        for key, val in git_config_sets(words):
            if git_config_command(key, "!") is None:
                continue
            if val is None:
                return unseen_config_msg(key, "`--config-env` has git read it from an environment variable")
            if _unseen(val):
                return unseen_config_msg(key, f"`{val}` holds a `$` or backtick expansion")
        for _, sub, rest in _git_calls(words):
            at = next((j for j, a in enumerate(rest) if a in ("--rename-section", "rename-section")), None)
            if sub != "config" or at is None:
                continue
            names = [a for j, a in enumerate(rest[at + 1:]) if not a.startswith("-")
                     and (j == 0 or rest[at + j] not in ("-f", "--file", "--blob"))]
            if any(_unseen(a) or a.partition(".")[0].lower() in GIT_COMMAND_SECTIONS for a in names):
                return ("`git config --rename-section` into or out of a section that holds git aliases or "
                        "command-valued keys turns values the guard never read as commands into ones git "
                        "runs, so it's refused; ask the user")
        if _program(words[0]) in ENV_READERS:
            continue
        words = _without_messages(words)
        for k, w in enumerate(words):
            m = GIT_CONFIG_ENV.fullmatch(w)
            if m:
                if (m.group(3) or k > 0) and (k == 0 or words[k - 1] not in ("-u", "--unset")):
                    # NAME=value; or `read NAME`, `printf -v NAME`, `export NAME`, NAME+=...: a value the text doesn't hold
                    val = m.group(3)[1:] if m.group(3) and m.group(3).startswith("=") else None
                    env.append((m.group(1), m.group(2), val))
                continue
            for h in GIT_CONFIG_ENV_IN.finditer(w):
                # `GIT_CONFIG_KEY_$i=...`, `declare -n r=GIT_CONFIG_KEY_0`, `${GIT_CONFIG_KEY_0:=...}`
                read = GIT_CONFIG_ENV_READ.search(w[:h.start()])
                if not read or (read.group(0).startswith("${") and re.match(r"\w*:?=", w[h.end():])):
                    return DYNAMIC_ENV_MSG
    keys = {n: v for name, n, v in env if name.startswith("GIT_CONFIG_KEY_")}
    for name, n, val in env:
        if name == "GIT_CONFIG_PARAMETERS":
            if val is None or _unseen(val):
                return unseen_config_msg(name, "its value comes from the environment or an expansion")
            try:
                entries = [e.partition("=")[::2] for e in shlex.split(val)]
            except ValueError:
                return unseen_config_msg(name, "the guard can't split its value")
            for key, v in entries:
                if (c := git_config_command(key, v)) is not None and human_cmd(c):
                    return HUMAN_MSG
        elif name.startswith("GIT_CONFIG_KEY_"):
            if val is None or _unseen(val):
                return unseen_config_msg(name, "the key comes from the environment or an expansion, so git may "
                                         f"run GIT_CONFIG_VALUE_{n}")
            if git_config_command(val, "!") is not None and not any(
                    nm.startswith("GIT_CONFIG_VALUE_") and m == n for nm, m, _ in env):
                return unseen_config_msg(val, f"its value, GIT_CONFIG_VALUE_{n}, is set by another command")
        else:  # GIT_CONFIG_VALUE_<n>: read as its key, or as any command when the key isn't in the text
            key = keys.get(n)
            if key is not None and git_config_command(key, "!") is None:
                continue
            if val is None or _unseen(val):
                return unseen_config_msg(key or name, f"{name} comes from the environment or an expansion")
            runs = [git_config_command(key, val)] if key else [val, val[1:], "git " + val]
            if any(c and human_cmd(c) for c in runs):
                return HUMAN_MSG
    return None


# The GIT_COMMAND_ENV variables only git reads. A value the text doesn't show is refused for these
# (git_env_reason) but not for EDITOR, VISUAL, PAGER, SSH_ASKPASS or LESSOPEN, which other programs read
# too and which a profile or script often sets from another variable.
GIT_ONLY_ENV = {n for n in GIT_COMMAND_ENV if n.startswith("GIT_")}
GIT_ONLY_ENV_IN = re.compile(r"\b(?:" + "|".join(sorted(GIT_ONLY_ENV, key=len, reverse=True)) + r")\b")
# Programs that set the variables they name to a value from their input.
ENV_FROM_INPUT = {"read", "mapfile", "readarray"}


def unseen_env_msg(name, why):
    return (f"git runs the value of {name} as a command, and the guard checks that command where {name} is set, "
            f"but here it can't see it: {why}. So it's refused. Write the value out in full (`{name}=<command> "
            f"git ...`; `~` for your home directory), or ask the user")


def git_env_reason(cmd):
    """Why a shell command sets one of git's own command variables (GIT_ONLY_ENV: GIT_EDITOR, GIT_PAGER,
    GIT_SSH_COMMAND, ...) to a value human_cmd can't check, or None. Where env_commands reads an
    assignment (ahead of a program or a git call, alone, exported): a value with a `$` or backtick
    expansion, now or when git runs it through the shell, or a `+=` onto a value the text didn't set.
    Anywhere: exporting it with no value the text set earlier, `read` / `mapfile` / `printf -v` into
    it, a `for` / `select` loop over it, a nameref to it, or `${NAME:=value}` with an unseen value (a
    value the text shows is checked by human_cmd). Strings run as code are checked too."""
    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        if m := GIT_ONLY_ENV_IN.search(cmd):
            return unseen_env_msg(m.group(0), "the guard can't split this command")
        return None
    seen = set()  # the GIT_ONLY_ENV names the text has set to a value it shows
    for words, docs in cmds:
        # bash -c '...', a heredoc fed to sh. env's own words are read below.
        if any(CODE_RUNNER.fullmatch(_program(w)) and _program(w) != "env" for w in words):
            for t in words + [d for d, _ in docs]:
                if t != cmd and (why := git_env_reason(t)):
                    return why
        for name, append, val in _env_assignments(words):
            if name not in GIT_ONLY_ENV:
                continue
            if _unseen(val):
                return unseen_env_msg(name, f"`{val}` holds a `$` or backtick expansion")
            if append and name not in seen:
                return unseen_env_msg(name, "`+=` adds to a value set outside this command")
            seen.add(name)
        for w in _without_messages(words):
            for m in re.finditer(r"\$\{(\w+):?=([^}]*)(\}?)", w):
                if m.group(1) not in GIT_ONLY_ENV:
                    continue
                if _unseen(m.group(2)) or "{" in m.group(2) or not m.group(3):
                    return unseen_env_msg(m.group(1), f"`{m.group(0)}` sets it to a value it can't read")
                if human_cmd(m.group(2)):
                    return HUMAN_MSG
                seen.add(m.group(1))
        head = next((k for k, w in enumerate(words) if not ASSIGNMENT.match(w)), len(words))
        if head == len(words):
            continue
        prog, args = _program(words[head]), words[head + 1:]
        opts = "".join(a[1:] for a in args if re.fullmatch(r"-[A-Za-z]+", a))
        for j, a in enumerate(args):
            if prog in ("declare", "typeset", "local") and "n" in opts and a.partition("=")[2] in GIT_ONLY_ENV:
                return unseen_env_msg(a.partition("=")[2], f"`{a}` makes a nameref that sets it under another name")
            if prog == "printf" and a.startswith("-v") and (a[2:] or (args[j + 1] if j + 1 < len(args) else "")) \
                    in GIT_ONLY_ENV:
                return unseen_env_msg(a[2:] or args[j + 1], "`printf -v` sets it")
            if a not in GIT_ONLY_ENV:
                continue
            if prog in ENV_FROM_INPUT:
                return unseen_env_msg(a, f"`{prog}` sets it from its input")
            if prog in ("for", "select") and j == 0:
                return unseen_env_msg(a, f"a `{prog}` loop sets it to each of its words")
            if a not in seen and ((prog == "export" and "n" not in opts)
                                  or (prog in ("declare", "typeset", "local") and "x" in opts)):
                return unseen_env_msg(a, f"`{prog}` exports a value set outside this command")
    return None


# A section header as git reads one: [section], [section.sub], or [section "sub"] (whitespace on one
# line before the quote).
GIT_CONFIG_HEADER = re.compile(r"\[([A-Za-z0-9.-]*)(?:\]|[ \t\v\f\r]+\"((?:[^\"\\\n]|\\[^\n])*)\"\])")


def git_config_entries(text):
    """The entries of text read as a git config file, the way git's own parser (config.c) reads it:
    (key, value) with the key lowercased and qualified by its section, value None for a key with no
    `=`. None if git would refuse the file, so that nothing in it is config."""
    s = text.replace("\r\n", "\n")
    n, i, out, base = len(s), 1 if s.startswith("﻿") else 0, [], ""
    while i < n:
        c = s[i]
        if c in " \t\n\v\f\r":
            i += 1
        elif c in "#;":
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "[":  # [section], [section.sub] or [section "sub"]; a key may follow on the same line
            m = GIT_CONFIG_HEADER.match(s, i)
            if not m:
                return None
            sub = m.group(2)
            base = m.group(1).lower() + ("" if sub is None else "." + re.sub(r"\\(.)", r"\1", sub))
            i = m.end()
        elif c.isascii() and c.isalpha():
            j = i
            while j < n and (s[j].isascii() and s[j].isalnum() or s[j] == "-"):
                j += 1
            key, i = base + "." + s[i:j].lower(), j
            while i < n and s[i] in " \t":
                i += 1
            if i >= n or s[i] == "\n":
                out.append((key, None))
                continue
            if s[i] != "=":
                return None
            val, quote, comment, space, i = [], False, False, 0, i + 1
            while True:
                c, i = (s[i] if i < n else "\n"), i + 1
                if c == "\n":
                    if quote:
                        return None
                    break
                if comment:
                    continue
                if c in " \t\v\f\r" and not quote:
                    space += any(val)
                    continue
                if c in ";#" and not quote:
                    comment = True
                    continue
                val.append(" " * space)
                space = 0
                if c == "\\":
                    c, i = (s[i] if i < n else "\n"), i + 1
                    if c == "\n":  # a continued line
                        continue
                    if c not in 'tbn\\"':
                        return None
                    val.append({"t": "\t", "b": "\b", "n": "\n"}.get(c, c))
                elif c == '"':
                    quote = not quote
                else:
                    val.append(c)
            out.append((key, "".join(val)))
        else:
            return None
    return out


def _command_entries(text):
    """The git aliases and command-valued entries (git_config_command) of text read as git config."""
    return [(k, v) for k, v in git_config_entries(text) or [] if git_config_command(k, v or "") is not None]


def edit_result(path, tool, inp):
    """(the file's text before, its text after) a Write, Edit or MultiEdit."""
    p = Path(path)
    try:
        before = p.read_text(errors="replace") if p.is_file() else ""
    except OSError:
        before = ""
    if tool == "Write":
        return before, inp.get("content", "")
    after = before
    for e in inp.get("edits") or [inp]:
        old, new = e.get("old_string", ""), e.get("new_string", "")
        after = after.replace(old, new) if e.get("replace_all") else after.replace(old, new, 1)
    return before, after


def config_edit_reason(path, tool, inp):
    """Why an Edit or Write must not run: the file reads as git config afterwards and the edit adds
    an alias or command-valued entry. Any file counts, not only .git/config or ~/.gitconfig: one
    include.path names is config too, and the guard can't tell which files git will include."""
    if not path:
        return None
    before, after = edit_result(path, tool, inp)
    added = _command_entries(after)
    for e in _command_entries(before):
        if e in added:
            added.remove(e)
    if not added:
        return None
    key = added[0][0]
    return (f"after this edit {Path(path).name} reads as a git config file and defines `{key}`, which git runs as "
            f"a command (a git alias or a command-valued key): the guard can't tell which files git reads config "
            f"from, so an edit doesn't define one. Use `git config {key} <value>`, which it checks, or ask the user")


# Files git reads config from, named in shell text: .git/config (and a worktree's or submodule's under
# .git/), config.worktree, ~/.gitconfig and kin, /etc/gitconfig, $XDG_CONFIG_HOME/git/config, and
# `$(git rev-parse --git-dir)/config` or `$GIT_DIR/config`; as a word ends, or inside code.
GIT_CONFIG_NAME = re.compile(r"(?:^|[/\s'\"=(:])(?:\.?gitconfig[\w.-]*|config\.worktree|\.git/(?:[^\s'\"]*/)?config)"
                             r"(?![\w./-])|(?:/git|\)|\$\{?\w+\}?)/config(?:\.worktree)?(?![\w./-])")
# A bare `config` counts where it names a file in a git directory: `cd .git && echo ... >> config`.
GIT_DIR_CONFIG = re.compile(r"/\.git/(?:[^/]+/)*config(?:\.worktree)?$")
CONFIG_WRITE = re.compile(r"\b(?:ln|dd|rsync)\s")


def git_config_files(cwd):
    """The files git reads config from in cwd (`git config --list --show-origin --includes`): the
    system, global and repository files and every file they include, each as git names it and resolved."""
    try:
        r = subprocess.run(["git", "config", "--list", "--show-origin", "--includes", "-z"], cwd=cwd,
                           capture_output=True, text=True, timeout=10, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    out = set()
    for origin in r.stdout.split("\0")[::2]:
        if origin.startswith("file:"):
            f = os.path.join(cwd, os.path.expanduser(origin[5:]))
            out |= {os.path.normpath(f), os.path.realpath(f)}
    return out


def config_write_reason(cmd, cwd):
    """Why a shell command must not run because it may write a file git reads config from: it holds a
    shell write and names one (GIT_CONFIG_NAME, a `config` inside a .git directory, or a file git
    reads or includes from cwd). What it writes can define an alias or command-valued key the guard
    never sees, so `git config` (checked by human_cmd) or Edit (config_edit_reason) is the way."""
    hit = SHELL_WRITE.search(cmd) or TAR_WRITE.search(cmd) or CONFIG_WRITE.search(cmd)
    if not hit:
        return None
    named = names_outside_data(cmd, GIT_CONFIG_NAME) or names_outside_data(cmd, GIT_DIR_CONFIG, cwd)
    if not named:
        files = git_config_files(cwd)
        named = bool(files) and names_outside_data(
            cmd, re.compile("|".join(rf"^{re.escape(f)}$" for f in sorted(files))), cwd)
    if not named:
        return None
    return (f"this command contains `{' '.join(hit.group(0).split())}` and names a file git reads config from "
            f"(.git/config, config.worktree, ~/.gitconfig, git/config or one git includes): a shell write there "
            f"can define a git alias or command-valued key the guard can't see, so it's refused. Use "
            f"`git config <key> <value>`, which the guard checks, or Edit; or ask the user")


def frontmatter(text):
    """The fields of a task file's frontmatter, read by tasks.parse_fields: quotes and list
    style don't change a value."""
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    out, bad = _tasks.parse_fields(m.group(1))
    for _, line in bad:  # a line ergon can't read still counts for the guard: ` key: v` is key `key`
        key, _, val = line.partition(":")
        out.setdefault(key.strip(), _tasks._scalar(val))
    return out


def shared_companion_reason(tool, inp):
    """Why a dispatched worker's tool call must not run because it changes a companion's shared
    checkout, or None. Each worker whose task touches a companion has its own worktree of it."""
    for c in _config.COMPANIONS:
        why = (f"{c['path']} is the shared {c['name']} checkout; dispatched workers never change it. "
               f"If your task touches {c['name']}, its worktree is {c['rel']} inside your own "
               "worktree; if it has none, stop blocked")
        if tool == "Bash":
            cmd = inp.get("command", "")
            shared = re.compile(re.escape(str(c["path"])) + r"|(?:\.\./)+" + re.escape(c["rel"]) + r"\b")
            if shared.search(cmd) and (SHELL_WRITE.search(cmd) or GIT_WRITE.search(cmd)):
                return why
            continue
        path = inp.get("file_path", "")
        if not path:
            continue
        try:
            Path(path).resolve().relative_to(c["path"].resolve())
        except ValueError:
            continue
        return why
    return None


POLICY_MSG = "the policies file is written by a human only (the p answer of the interactive review); propose one with a `POLICY: <title> — <why>` line"


def is_policies(path):
    """Whether path is the policies file, or its copy in a task worktree."""
    rel = _config.POLICIES.relative_to(_config.HOME).as_posix()
    return bool(path) and (str(Path(path).resolve()) == str(_config.POLICIES.resolve())
                           or Path(path).as_posix().endswith("/" + rel))


RUNS_WRITER = "run logs and history.jsonl under the state's runs/ are written by dispatch and the ergon CLI only"
RUNS_MSG = RUNS_WRITER + ("; read them, don't write them: they're dispatch's record of every round and verdict")


def is_runs(path):
    """Whether path is in the state's runs/ (history.jsonl, worker, verify and review logs),
    or in its copy in a task worktree."""
    if not path:
        return False
    p = Path(path)
    try:
        p.resolve().relative_to(_config.RUNS.resolve())
        return True
    except ValueError:
        return bool(_config.RUNS_PATH.search(p.as_posix()))


def names_outside_data(cmd, pattern, cwd=None):
    """Whether shell text names pattern anywhere but in a heredoc's data. A heredoc's body is data
    (`cat > tests/t.py <<'EOF'` writing a test that mentions the path) unless it reaches a program
    that runs it or turns it into arguments (a shell or interpreter, xargs, also through a pipe),
    or it expands a $(...) or `...`. Text that can't be split is matched whole.
    With cwd (where the command runs), a word also counts as the path it names from there, after
    any `cd` or `pushd` before it: `cd tasks && echo x > items/T-001.md` names tasks/items. A `cd`
    can fail or sit in a subshell, so a word is tried from every directory the shell may be in."""
    return _names_outside_data(cmd, pattern, {os.path.normpath(cwd)} if cwd else set())


MAX_CWDS = 256  # directories _names_outside_data follows before it gives up and counts the text as a match


def _chdir_targets(words):
    """Directories that `env -C/--chdir`, `make -C`, `tar -C` and `git -C` in a command's words
    start their command in."""
    out = []
    for k, w in enumerate(words):
        prog = _program(w)
        if prog not in ("env", "make", "gmake", "tar", "git"):
            continue
        rest = words[k + 1:]
        for i, a in enumerate(rest):
            if a.startswith("--chdir=") and prog == "env":
                out.append(a[8:])
            elif a.startswith("--directory=") and prog in ("make", "gmake", "tar"):
                out.append(a[12:])
            elif a in ("-C", "--chdir", "--directory") and i + 1 < len(rest):
                out.append(rest[i + 1])
            elif a.startswith("-C") and len(a) > 2 and prog in ("make", "gmake", "tar"):
                out.append(a[2:])
    return out


_MSG_SHORT = re.compile(r"-[A-Za-z]*m$")


def _without_messages(words):
    """The words of a command minus the values of `git commit/tag/notes -m` and `--message[=]`, which are
    text, not paths. A value with $(...) or backticks may run a command, so it stays."""
    k = next((i for i, w in enumerate(words) if _program(w) == "git"), None)
    if k is None or not any(w in ("commit", "tag", "notes") for w in words[k + 1:]):
        return words
    live = re.compile(r"\$\(|`")
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
            if not live.search(w):
                continue
        elif w == "--message" or _MSG_SHORT.match(w):
            skip = True
        elif w.startswith("--message=") and not live.search(w):
            continue
        out.append(w)
    return out


def _names_outside_data(cmd, pattern, dirs):
    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        return bool(pattern.search(cmd))
    dirs = set(dirs)

    def names_in(t, ds):
        return any(pattern.search(os.path.normpath(os.path.join(d, os.path.expanduser(t)))) for d in ds)

    def names(t):
        return bool(pattern.search(t)) or names_in(t, dirs)

    for words, docs in cmds:
        words = _without_messages(words)
        runs = any(CODE_RUNNER.fullmatch(_program(w)) for w in words)
        code = runs or any(_program(w) == "xargs" for w in words)
        live = [d for d, expands in docs if code or (expands and re.search(r"\$\(|`", d))]
        if any(names(t) for t in words + live):
            return True
        # env -C tasks sh -c '...', make -C tasks, tar -C tasks -x...: the command and the text it runs
        # start in another directory. tar and make write into the directory itself, so probe its children.
        moved = _chdir_targets(words)
        if moved and dirs:
            inner = {os.path.normpath(os.path.join(d, t)) for d in dirs for t in moved}
            if len(dirs | inner) > MAX_CWDS:
                return True
            if any(pattern.search(os.path.join(d, sub)) for d in inner for sub in ("items", "epics", "runs")
                   if any(_program(w) == "tar" for w in words)):
                return True
            if any(names_in(t, inner) for t in words + live) or any(
                    t != cmd and _names_outside_data(t, pattern, inner)
                    for t in (words if runs else []) + live):
                return True
        # bash -c 'cd tasks && ...', a heredoc fed to sh, or one whose $(...) runs a command
        if dirs and any(t != cmd and _names_outside_data(t, pattern, dirs) for t in (words if runs else []) + live):
            return True
        k = next((i for i, w in enumerate(words) if _program(w) in ("cd", "pushd")), None)
        if k is None or not dirs:
            continue
        target = next((w for w in words[k + 1:] if not w.startswith("-")), None)
        target = os.path.expanduser(target or "~")
        dirs |= {os.path.normpath(os.path.join(d, target)) for d in dirs}
        if len(dirs) > MAX_CWDS:
            return True
    return False


# git subcommands that change a repository, its refs or a worktree (state_git_reason).
STATE_GIT_WRITES = {"add", "commit", "checkout", "switch", "reset", "restore", "merge", "rebase", "cherry-pick",
                    "revert", "stash", "apply", "am", "pull", "push", "fetch", "branch", "tag", "worktree", "clean",
                    "update-ref", "commit-tree", "notes", "gc", "prune", "rm", "mv", "symbolic-ref", "replace",
                    "filter-branch", "filter-repo", "read-tree", "update-index", "write-tree", "mktree"}


def state_git_reason(cmd, cwd=None):
    """In shared mode, why a shell command must not run because it is a git write in the state
    worktree (run there, pointed at it with -C, --git-dir or --work-tree, or after a `cd` into
    it), names the state branch, or adds, moves or removes the state worktree. Only ergon commits
    on the state branch, and only a human publishes it. None without [shared]."""
    if not _config.SHARED:
        return None
    state, branch = _config.STATE.resolve(), _config.SHARED["branch"]
    why = (f"{_config.STATE.relative_to(_config.HOME)}/ is the shared state worktree (branch {branch}): only "
           "ergon commits there, and only a human publishes it. Use `ergon new` / `ergon set` (Edit for a "
           "task's body), and ask the user to sync")
    names_branch = re.compile(rf"(?:^|[:/+^]){re.escape(branch)}(?:$|[:~^@])")

    def inside(p):
        try:
            p.resolve().relative_to(state)
            return True
        except (ValueError, OSError):
            return False

    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        rel = re.escape(_config.STATE.relative_to(_config.HOME).as_posix())
        hit = re.search(rf"(?:^|[\s/\"'=]){rel}(?:/|\b)", cmd) or names_branch.search(cmd)
        return why if hit and GIT_WRITE.search(cmd) else None
    here = Path(cwd or _config.HOME)
    for words, docs in cmds:
        if any(CODE_RUNNER.fullmatch(_program(w)) for w in words):  # bash -c "...", a heredoc fed to sh
            if any(t != cmd and state_git_reason(t, str(here)) for t in words + [d for d, _ in docs]):
                return why
        k = next((i for i, w in enumerate(words) if _program(w) in ("git", "cd", "pushd")), None)
        if k is None:
            continue
        if _program(words[k]) in ("cd", "pushd"):
            target = next((w for w in words[k + 1:] if not w.startswith("-")), None)
            here = here / os.path.expanduser(target) if target else _config.HOME
            continue
        aimed = any(inside(here / os.path.expanduser(w.partition("=")[2])) for w in words[:k]
                    if w.startswith(("GIT_DIR=", "GIT_WORK_TREE=")))
        d, i = here, k + 1
        while i < len(words) and words[i].startswith("-"):
            w = words[i]
            if w == "-C" and i + 1 < len(words):
                d, i = d / os.path.expanduser(words[i + 1]), i + 2
                continue
            name, eq, val = w.partition("=")
            if name in ("--git-dir", "--work-tree"):
                if not eq and i + 1 < len(words):
                    val, i = words[i + 1], i + 1
                aimed = aimed or inside(d / os.path.expanduser(val))
            elif w in ("-c", "--namespace", "--exec-path", "--super-prefix", "--config-env") and i + 1 < len(words):
                i += 1
            i += 1
        if i >= len(words) or words[i] not in STATE_GIT_WRITES:
            continue
        verb, rest = words[i], words[i + 1:]
        if inside(d) or aimed or any(names_branch.search(w) for w in rest):
            return why
        if any("refs/ergon/" in w for w in rest):  # e.g. the trunk the approvers file is read from
            return ("refs/ergon/ holds what ergon fetched from the shared remote (the trunk the approvers "
                    "file is read from, the state branch as last fetched); only ergon writes it")
        if verb == "worktree" and any(inside(d / os.path.expanduser(w)) for w in rest if not w.startswith("-")):
            return why
    return None


def is_flat_epic(path):
    """Is path an epic file written flat in the state's epics/ (`ergon tidy` would move it into a
    folder as EPIC.md), so the guard's status and approved: rules cover it too?"""
    try:
        p = Path(path).resolve()
        return p.suffix == ".md" and p.parent == _config.EPICS.resolve()
    except (OSError, ValueError):
        return False


# ---------- read-only guard (`hook guard --read-only`, the breakdown agent's) ----------
# The breakdown agent is read-only by its dontAsk allowlist and by dispatch's `git status` check
# after the run. This is a third layer, so it is an allowlist too, not a list of known writes:
# the tools that only read, and shell commands made only of programs that only read (git and
# ergon only with their read verbs), with no redirect to a file, no heredoc, no variable
# assignment and nothing it can't split. Anything else is refused, including text it can't parse.

READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "TodoWrite"}
# Programs that only read files and print. Not sed/awk/xargs/env/tee/tree/uniq-with-output and the
# like: each can write a file or run another program. sort and find are checked for the options that do.
READ_ONLY_PROGRAMS = {"ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "pwd", "echo",
                      "printf", "true", "false", "test", "[", "cd", "basename", "dirname", "realpath", "cut",
                      "tr", "nl", "sort", "uniq", "diff", "cmp", "comm", "stat", "du", "which", "git", "ergon"}
READ_ONLY_GIT = {"log", "show", "diff", "grep", "ls-files", "ls-tree", "blame", "status", "rev-parse", "rev-list",
                 "cat-file", "shortlog", "describe", "merge-base", "name-rev", "show-ref", "for-each-ref",
                 "diff-tree", "diff-index", "diff-files", "whatchanged", "count-objects", "branch", "worktree"}
GIT_LIST_BRANCH = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--show-current", "-l", "--list",
                   "--no-color", "--color"}
READ_ONLY_ERGON = {"board", "show", "next", "review", "rules", "guide", "check", "audit", "stats", "related"}
FIND_WRITES = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# Redirects that write nowhere: to /dev/null, or from one descriptor to another (2>&1, >&2, >&-).
HARMLESS_REDIRECT = re.compile(r"(?:[0-9]*|&)>>?\|?[ \t]*/dev/null(?![^\s;&|()<>])|[0-9]*>&(?:[0-9]+|-)(?![^\s;&|()<>])")
READ_ONLY_MSG = ("the breakdown agent is read-only: {what}. Read files with Read, Grep and Glob, and run only "
                 "read-only commands (git log/show/diff/grep/ls-files/blame/status, ergon show/next/board/check, "
                 "ls, cat, head, tail, wc, grep), one program at a time, with no redirect to a file")


def _code_text(cmd):
    """cmd with its quoted literals blanked out, so what's left is what the shell reads as operators:
    '...' and $'...' always, "..." unless it holds a $(...) or `...` (that's code, so it stays), and
    a comment (a # starting a word, as shell_commands reads it) up to its newline: a quote in a
    comment opens nothing, so it can't hide a redirect after it. None if a quote doesn't close."""
    out, i, n = [], 0, len(cmd)
    inword = False  # as shell_commands tracks it: an escaped space or a quote is inside a word
    while i < n:
        c = cmd[i]
        if c == "#" and not inword:
            j = cmd.find("\n", i)
            out.append(" ")
            i = n if j < 0 else j
            continue
        inword = c not in " \t\n;&|()<>"
        if c == "\\":
            out.append("  ")
            i += 2
        elif c == "'" or cmd.startswith("$'", i):
            j = i + (2 if c == "$" else 1)
            while j < n and cmd[j] != "'":
                j += 2 if c == "$" and cmd[j] == "\\" else 1
            if j >= n:
                return None
            out.append(" ")
            i = j + 1
        elif c == '"':
            j = i + 1
            while j < n and cmd[j] != '"':
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                return None
            span = cmd[i:j + 1]
            out.append(span if re.search(r"\$\(|`", span) else " ")
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _long_opt(word, *names):
    """Whether word is one of the long options names, or an abbreviation of one getopt or git would take."""
    name = word.partition("=")[0]
    return len(name) > 3 and name.startswith("--") and any(n.startswith(name) for n in names)


def _read_only_git(words):
    """Why a git call (words[0] is git) isn't a read, or None."""
    i = 1
    while i < len(words) and words[i].startswith("-"):
        w = words[i]
        if w.startswith(("-c", "--config-env", "--exec-path")):
            return f"`git {w}` sets config or the programs git runs"
        if w in ("-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(words):
            i += 1
        i += 1
    if i >= len(words):
        return None  # bare `git` prints its usage
    sub, rest = words[i], words[i + 1:]
    if sub not in READ_ONLY_GIT:
        return f"`git {sub}` isn't one of the read-only git commands"
    if any(_long_opt(a, "--output", "--open-files-in-pager") for a in rest):
        return f"`git {sub} --output` writes a file and `--open-files-in-pager` runs a program"
    if sub == "grep" and any(re.match(r"-[^-]*O", a) for a in rest):
        return "`git grep -O` runs a program"
    if sub == "branch":
        listing = any(a in ("-l", "--list") for a in rest)
        if any(a not in GIT_LIST_BRANCH for a in rest if a.startswith("-")) or (
                not listing and any(not a.startswith("-") for a in rest)):
            return "`git branch` only lists here (-a, -r, -v, --list, --show-current)"
    if sub == "worktree" and rest[:1] != ["list"]:
        return "`git worktree` only lists here (`git worktree list`)"
    return None


def _read_only_words(words):
    """Why one simple command (its words) isn't a read, or None."""
    prog = words[0]
    if ASSIGNMENT.match(prog):
        return f"no variable assignments (`{prog}`): one can point git or a program at code to run"
    if prog not in READ_ONLY_PROGRAMS:
        return f"`{prog}` isn't one of the read-only programs"
    rest = words[1:]
    if prog == "git":
        return _read_only_git(words)
    if prog == "ergon":
        verb = next((a for a in rest if not a.startswith("-")), "board")
        if verb not in READ_ONLY_ERGON:
            return f"`ergon {verb}` isn't one of ergon's read commands"
        if verb == "review" and any(re.match(r"-[^-]*i", a) or _long_opt(a, "--interactive") for a in rest):
            return "`ergon review -i` is a human's"
    if prog == "find" and any(a in FIND_WRITES for a in rest):
        return "`find -exec/-ok/-delete/-fprint/-fls` runs a program or writes a file"
    if prog == "rg" and any(a == "--pre" or a.startswith("--pre=") for a in rest):
        return "`rg --pre` runs a program"
    if prog == "sort" and any(re.match(r"-[^-]*o", a) or (a.startswith("--") and a.startswith(("--o", "--co")))
                              for a in rest):
        return "`sort -o` writes a file and `--compress-program` runs one"
    if prog == "uniq" and len([a for a in rest if not a.startswith("-")]) > 1:
        return "`uniq IN OUT` writes OUT"
    return None


def read_only_reason(tool, inp):
    """Why a call by the read-only (breakdown) agent must not run, or None. Refuses every tool but
    READ_ONLY_TOOLS and Bash, and Bash unless each command it runs passes _read_only_words."""
    if tool != "Bash":
        return None if tool in READ_ONLY_TOOLS else READ_ONLY_MSG.format(what=f"{tool} isn't a read-only tool")
    cmd = inp.get("command", "")
    code = _code_text(cmd)
    if code is None:
        return READ_ONLY_MSG.format(what="a quote doesn't close")
    if ">" in HARMLESS_REDIRECT.sub(" ", code):
        return READ_ONLY_MSG.format(what="no redirect to a file (only to /dev/null or another descriptor)")
    if re.search(r"(?<!<)<<(?!<)", code):
        return READ_ONLY_MSG.format(what="no heredocs")
    try:
        cmds = shell_commands(cmd)[0]
    except (ValueError, RecursionError):
        return READ_ONLY_MSG.format(what="a command it can't split into words")
    if not cmds:
        return None
    for words, _ in cmds:
        why = _read_only_words(words)
        if why:
            return READ_ONLY_MSG.format(what=why)
    return None


def guard_reason(tool, inp, worker=False, cwd=None, read_only=False):
    """Why this tool call must not run, or None. cwd: where a Bash command runs (the event's).
    read_only: the breakdown agent's guard, which refuses anything that may write (read_only_reason)
    on top of every rule below."""
    if read_only:
        why = read_only_reason(tool, inp)
        if why or tool != "Bash":  # its hook sees every tool; the rules below are for Bash, Edit and Write
            return why
    if worker:
        why = shared_companion_reason(tool, inp)
        if why:
            return why
    if tool == "Bash":
        cmd = inp.get("command", "")
        why = state_git_reason(cmd, cwd)
        if why:
            return why
        if human_cmd(cmd):
            return HUMAN_MSG
        why = git_config_reason(cmd)
        if why:
            return why
        why = git_env_reason(cmd)
        if why:
            return why
        if re.search(r"\bunset\b[^;&|]*\b(CLAUDECODE|AI_AGENT)\b|\benv\b[^;&|]*-u\s*(CLAUDECODE|AI_AGENT)|"
                     r"\b(CLAUDECODE|AI_AGENT)=", cmd):
            return "don't change CLAUDECODE / AI_AGENT: they're how ergon knows an agent is running"
        if SHELL_WRITE.search(cmd) and names_outside_data(cmd, re.compile(re.escape(_config.POLICIES.name))):
            return POLICY_MSG
        hit = SHELL_WRITE.search(cmd) or TAR_WRITE.search(cmd)
        if hit and names_outside_data(cmd, _config.RUNS_IN_CMD, cwd or str(_config.HOME)):
            return (f"{RUNS_WRITER}; this command names runs/ and contains `{' '.join(hit.group(0).split())}`, "
                    "which counts as a possible write, so it's refused. To read them, use cat, jq, grep, "
                    "head/tail or the Read tool instead, e.g. `jq . <file under runs/>`")
        if hit and names_outside_data(cmd, _config.TASK_DIR_IN_CMD, cwd or str(_config.HOME)):
            return (f"don't write task files from the shell: this command contains `{' '.join(hit.group(0).split())}` "
                    "and names items/ or epics/; use `ergon new` / `ergon set` for state, Edit for a task's body")
        return config_write_reason(cmd, cwd or str(_config.HOME))
    path = inp.get("file_path", "")
    if is_policies(path):
        return POLICY_MSG
    if is_runs(path):
        return RUNS_MSG
    why = config_edit_reason(path, tool, inp)
    if why:
        return why
    if not (_config.TASK_FILE.search(path) or is_flat_epic(path)):
        return None
    p = Path(path)
    before, after = edit_result(path, tool, inp)
    fb, fa = frontmatter(before), frontmatter(after)
    if fb.get("approved", "") != fa.get("approved", ""):
        return "`approved:` is written by a human only (the one rule: `ergon guide`)"
    if (p.name == _config.EPIC_FILE or is_flat_epic(path)) and fb.get("breakdown", "") != fa.get("breakdown", ""):
        return ("an epic's `breakdown:` is dispatch's claim that it is being broken down: an agent's fake claim "
                "with a far-future deadline would keep the epic from ever being broken down, so only dispatch writes it")
    if _tasks.is_auto(fb):  # approved by inheritance for what it was filed as: no relabelling, no widening
        for key in ("category", "touches"):
            if fb.get(key, "") != fa.get(key, ""):
                return (f"`{key}:` of a task approved by inheritance (`approved: auto …`) is fixed: its approval "
                        f"holds only for what it was filed as. File a new task, or ask a human to change it")
    # touches: (it names the gate files the weakening check lets the task change) and tier: are
    # part of what a human approved: past proposed, an agent changing either is a re-plan (T-117).
    # Shared mode's digest un-approves such an edit; local mode has only this and dispatch's record
    # of the approved touches: (dispatch.approved_touches).
    if not (p.name == _config.EPIC_FILE or is_flat_epic(path)) and before and fb.get("status", "") != "proposed":
        for key in ("touches", "tier"):
            if fm_value(fb, key) != fm_value(fa, key):
                return replan_reason(key, fb.get("status") or "approved")
    # The two fields that weaken a gate, as `set` refuses them: the digest catches them in shared
    # mode only, so in local mode this is what stops an edit to an approved task's gate (T-150).
    # A new file counts too: a brief that arrives with its own gate is a human's to write.
    if fb.get("verify", "") != fa.get("verify", ""):
        return ("`verify:` overrides the runtime gate (`verify: true` passes anything), so a human writes, "
                "changes or removes it by editing the task file")
    if _tasks.prove_off(fa) and not _tasks.prove_off(fb):
        return "`prove: no` skips the red-on-base proof, so a human writes it by editing the task file"
    # A harden task skips the red-on-base proof and its layer picks the gate its own proof runs, so
    # `set` refuses both whatever the value (T-149); the same edit by hand is refused here (T-153).
    for key in ("harden", "harden_layer"):
        if fb.get(key, "") != fa.get(key, ""):
            return (f"`{key}:` decides what a harden task must prove (and a harden task skips the red-on-base "
                    "proof), so a human writes, changes or removes it by editing the task file")
    # suppress_ok: names the files a task may add a suppression marker to (T-160): refused by `set`
    # whatever the value, and the same edit by hand here, a new file arriving with one included.
    if fm_value(fb, "suppress_ok") != fm_value(fa, "suppress_ok"):
        return ("`suppress_ok:` names the files the weakening check lets a task add a suppression marker to, "
                "so a human writes, changes or removes it by editing the task file")
    # A new task or epic file starts proposed: in local mode an open epic with no stamp counts as
    # approved (nothing verifies one), so an agent's new EPIC.md can't be open.
    if fb.get("status", "") != fa.get("status", "") and not (not before and fa.get("status") == "proposed"):
        if fb.get("status") == "review":
            return "a task in review leaves it only by a human's `ergon accept` or `ergon reject`, run in their own terminal"
        if (p.name == _config.EPIC_FILE or is_flat_epic(path)) and (not before or "proposed" in (fb.get("status"), fa.get("status"))):
            return ("an epic leaves `proposed` only by a human's `ergon approve E-NNN` (it approves the brief); "
                    "agents file epics with `ergon new epic`, which writes proposed")
        return "change status with `ergon set ID status=…` (it enforces the allowed moves), not by editing the file"
    if fb.get("pin", "") != fa.get("pin", ""):
        return "`pin:` is written by a human's `ergon pin` only"
    # E-006: the human's pick among a task's options. Adding, changing or removing it (the whole
    # section going counts) is a human's `ergon choose`, so an agent can't choose for them.
    if _tasks.chosen_lines(before) != _tasks.chosen_lines(after):
        return ("a `Chosen:` line under `## Options` is the human's choice: it is written by a human's "
                "`ergon choose` only (ask the user to run it in their own terminal)")
    for key in ("asking", "answered"):
        if fb.get(key, "") != fa.get(key, ""):
            return f"`{key}:` is written by dispatch and a human's `ergon answer` only"
    if fb.get("limited", "") != fa.get("limited", ""):
        return ("`limited:` is written by dispatch only: it parks a task the usage limit stopped and resumes it "
                "after the reset")
    if fb.get("accept", "").lower() in _tasks.YES and fa.get("accept", "").lower() not in _tasks.YES:
        return "only a human takes `accept:` off a task (it holds the task's landing for their review)"
    return None


def fm_value(fm, key):
    """A frontmatter value as the guard compares it: touches: and suppress_ok: as their set of
    entries (dispatch.touch_key), so reordering or re-spacing the list changes nothing; anything
    else stripped."""
    val = fm.get(key, "")
    if key not in ("touches", "suppress_ok"):
        return ", ".join(val) if isinstance(val, list) else val.strip()
    if not isinstance(val, list):
        val = val.strip().strip("[]").split(",")
    return frozenset(x for x in (_dispatch.touch_key(v) for v in val) if x)


def replan_reason(key, status):
    """Why an agent may not change `key` (touches or tier) of a task that is `status`, past proposed."""
    return (f"`{key}:` of a {status} task is part of what a human approved (touches: names the gate files the "
            f"weakening check lets it change; tier: picks the model), so an agent doesn't change it: that's a "
            f"re-plan. A dispatched worker ends `RESULT: blocked — re-plan: <why>`; otherwise ask the user to "
            f"edit it and run `ergon approve <ID>` again")


def deny(why):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"ergon guard: {why}"}}))


def record_refusal(event, why, worker):
    """Append a `guard` history row for a refused tool call. Never raises: a failed write only warns."""
    try:
        item = None
        try:
            br = _gitops.git("branch", "--show-current", cwd=event.get("cwd") or None)
        except Exception:  # noqa: BLE001 - not a git folder: no task
            br = ""
        if br.startswith(_config.TASK_BRANCH):
            tid = br[len(_config.TASK_BRANCH):]
            item = _tasks.load(_config.ITEMS).get(tid) or {"id": tid}
        _history.record("guard", item, tool=event.get("tool_name"), reason=str(why)[:200], worker=bool(worker))
    except Exception as err:  # noqa: BLE001 - history never turns a deny into an error
        print(f"ergon: couldn't record guard refusal: {err}", file=sys.stderr)


def hook(args):
    if args.event == "commit-msg":
        return commit_msg_hook(args)
    # The read-only guard (the breakdown agent's) fails closed: with no project, or on a crash, it refuses.
    read_only = args.event == "guard" and getattr(args, "read_only", False)
    try:
        event = json.load(sys.stdin)
        try:
            _config.configure(args.project or os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd"))
        except _config.NotConfigured:
            if read_only:
                deny("the read-only guard found no ergon project, so it refuses everything")
            sys.exit(0)  # not an ergon project
        if args.event == "guard":
            why = guard_reason(event.get("tool_name"), event.get("tool_input") or {}, args.worker,
                               event.get("cwd"), read_only)
            if why:
                record_refusal(event, why, args.worker)
                deny(why)
        elif args.event == "stop" and not event.get("stop_hook_active"):
            errs, summary = _tasks.lint()
            if errs:
                print(json.dumps({"decision": "block", "reason": (
                    "`ergon check` fails:\n" + "\n".join(errs[:20]) + f"\n{summary}\n"
                    "Fix what your change caused (e.g. a card's `→ task` link to a task you closed). "
                    "If it isn't yours, say so to the user and stop.")}))
    except Exception as err:  # fail open, but for the read-only guard
        if read_only:
            deny(f"the read-only guard failed ({err}), so it refuses")
        else:
            print(f"ergon hook error (ignored): {err}", file=sys.stderr)
    sys.exit(0)


# ---------- git commit-msg hook ----------
# The Claude Code guard only reaches Claude Code. Agents that read AGENTS.md (Codex, Cursor, ...)
# meet only the CLI's checks, so `ergon init --hooks` adds the cheapest bump every agent hits:
# git itself refuses a commit whose message cites a task no human approved. Opt-in, per clone.
# Like the guard, a speed bump: `git commit --no-verify` skips it, and it fails open on a crash.

COMMIT_HOOK_MARK = "# ergon commit-msg hook"
COMMIT_HOOK = """#!/bin/sh
{mark}, written by `ergon init --hooks`. Delete this file to turn it off.
# Refuses a commit whose message cites a task (T-NNN) no human has approved. A speed bump, not
# a security boundary: it does nothing in a checkout without .ergon.toml.
py={python}
ergon_py={script}
[ -x "$py" ] || py=python3
if [ -e "$ergon_py" ]; then exec "$py" "$ergon_py" hook commit-msg "$1"; fi
if command -v ergon >/dev/null 2>&1; then exec ergon hook commit-msg "$1"; fi
echo "ergon commit-msg hook: ergon not found, so this commit's task IDs weren't checked" >&2
"""
SCISSORS = re.compile(r"^#\s*-+ >8 -+$")


def cited_tasks(msg):
    """Task IDs a commit message cites, ignoring the comment lines git strips (the editor
    template's "# On branch task/T-012", say) and everything below a `commit -v` scissors line."""
    kept = []
    for l in msg.splitlines():
        if SCISSORS.match(l):
            break
        if not l.startswith("#"):
            kept.append(l)
    return sorted(set(re.findall(r"\bT-\d{3,}\b", "\n".join(kept))))


def commit_msg_reason(msg, tasks, epics=None):
    """Why a commit with this message must not land, or None. Each task it cites must exist, be
    out of `proposed` and carry a human's `approved:` stamp: a proposed task (stamped or not), one
    rejected to dropped, one hand-edited to ready, or an ID nobody filed are all refused. So is an
    approved task whose epic's brief isn't approved (`_tasks.epic_problem`: proposed, dropped, or
    in shared mode open without a signed approval that verifies, stamped or not); a task with no
    epic passes, and so does one of an open epic without a stamp in local mode. An epic ID cited
    on its own isn't checked. In shared mode the stamp is only a display copy, so the signed
    approval must verify too (`Approvals.problem`); approvals load once, if a task is cited."""
    bad = []
    apr = None
    for tid in cited_tasks(msg):
        if apr is None and _config.signed():
            apr = _gitops.approvals()
        t = tasks.get(tid)
        if not t:
            bad.append(f"{tid}: no such task in this tracker")
        elif not t.get("approved") or t.get("status") == "proposed":
            # a proposed task is unapproved whatever its stamp says: a two-person task waiting for
            # its second approver, or a stamp written by hand
            bad.append(f"{tid}: {t.get('status') or 'no status'}, and no human has approved it")
        elif apr is not None and apr.problem(t):
            bad.append(f"{tid}: approval doesn't verify: {apr.problem(t)}")
        elif _tasks.is_auto(t) and _tasks.approval_of(t, tasks)[2]:  # approved by inheritance, no longer holds
            bad.append(f"{tid}: its auto approval doesn't hold: {_tasks.approval_of(t, tasks)[2]}")
        elif why := _tasks.epic_problem(_tasks.epic_of(t, epics or {}), apr):
            bad.append(f"{tid}: {why}")
    if not bad:
        return None
    return ("ergon: commit refused. It cites tasks no human has approved:\n  " + "\n  ".join(bad) +
            "\nOnly a human approves a task (`ergon approve`, in their own terminal). Work only `ready` "
            "tasks (`ergon next`); file new work with `ergon new task` (a feature needs 2 to 4 `--option`s, the recommended one first) and ask the user to approve it.")


def commit_msg_hook(args):
    """git runs this from the checkout's top level with the message file's path. Exit 1 refuses."""
    try:
        try:
            _config.configure(args.project)
        except _config.NotConfigured:
            sys.exit(0)  # not an ergon project: behave as if there were no hook
        try:
            _gitops.git("rev-parse", "-q", "--verify", "MERGE_HEAD")
            sys.exit(0)  # a merge only brings in commits that were checked when they were made
        except _gitops.GitError:
            pass
        if not args.msgfile:
            raise ValueError("no message file given")
        # git points GIT_DIR and friends at the repo being committed in; approvals are read from the
        # state worktree, a different repo, so those must not leak into `git -C <state>`.
        for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_COMMON_DIR",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_QUARANTINE_PATH"):
            os.environ.pop(var, None)
        why = commit_msg_reason(Path(args.msgfile).read_text(errors="replace"), _tasks.load(_config.ITEMS),
                               _tasks.load(_config.EPICS))
    except Exception as err:  # fail open, like the Claude Code hooks
        print(f"ergon commit-msg hook error (ignored): {err}", file=sys.stderr)
        sys.exit(0)
    if why:
        print(why, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def install_commit_hook():
    """Write the commit-msg hook into this repo's hooks folder (shared by every worktree). Never
    overwrites a hook that isn't ergon's, or one managed through core.hooksPath."""
    line = 'ergon hook commit-msg "$1" || exit 1'
    managed = subprocess.run(["git", "-C", str(_config.HOME), "config", "core.hooksPath"],
                             capture_output=True, text=True).stdout.strip()
    if managed:
        raise SystemExit(f"core.hooksPath is {managed}, so something else manages this repo's git hooks; "
                         f"ergon installed none. Add this line to that commit-msg hook:\n  {line}")
    dest = (_config.HOME / _gitops.git("rev-parse", "--git-path", "hooks", cwd=_config.HOME)).resolve() / "commit-msg"
    if dest.exists() and COMMIT_HOOK_MARK not in dest.read_text(errors="replace"):
        raise SystemExit(f"{dest} exists and isn't ergon's; left it alone. Add this line to it:\n  {line}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(COMMIT_HOOK.format(mark=COMMIT_HOOK_MARK, python=shlex.quote(sys.executable),
                                       script=shlex.quote(str(_config.PKG))))
    dest.chmod(0o755)
    print(f"installed {dest}: git now refuses commits citing a task no human has approved")
