# Shared task state for teams (design)

Status: built (T-016 to T-021). Written as a proposal in T-015; where the build departed from it, the text says so. The README's "Shared task state for teams" is the user-facing summary.

## The problem

Task state is local on purpose. `<state>/items`, `<state>/epics` and `<state>/runs` are gitignored, and the commits that cite task IDs are the lasting record (SKILL.md, "Task state is local"). That works for one person and their agents on one machine. A team doesn't get one queue: each person has their own `T-001`, nobody sees what the others approved, and two dispatchers can run the same work.

This note designs an **opt-in** shared mode. A project that doesn't turn it on behaves exactly as today. It covers:

1. where shared state lives and how it moves between machines,
2. allocating IDs without collisions (`next_id` in `ergon/tasks.py`; it takes `max + 1` over local files only),
3. concurrent, conflicting edits to the same task,
4. how one person's approval stamp is trusted on another person's machine,
5. an optional two-person rule for opus or security-sensitive tasks.

### What has to stay true

- **Humans approve, agents execute.** Sharing must not give an agent a new way to make a task look approved. Ideally it makes that harder than it is today.
- **No code is pushed by ergon.** Epic and task merges stay local. Shared mode pushes one thing: the state ref.
- **Standard library and git only.** No server, no database, no Python dependencies.
- **Offline still works** for reading, proposing and working tasks. Approving and dispatching in shared mode need the network, because they need to know what everyone else has done.
- **Task files keep their format**, so every existing command keeps working on them.

## Where shared state lives: the options

| | A. Orphan `ergon-state` branch | B. Shared directory | C. Commit task files to the trunk | D. Forge issues (GitHub) as the store |
|---|---|---|---|---|
| Transport | git fetch/push of one ref | NFS, a synced folder, or one shared host | normal code pushes | `gh` API calls |
| History and audit | full git history of every change | none | full, but mixed into code history | the forge's |
| Concurrency | push is compare-and-swap on the ref; ergon merges | `flock` over network filesystems is unreliable; sync tools make "conflicted copy" files | merge conflicts in code PRs; epic and task branches carry stale copies of the queue | the API's, per issue |
| Offline | yes, sync later | no | yes | no |
| Trust of approvals | signed commits can carry them | Unix permissions at best | signed commits, but every status change is a trunk commit | forge identity, but agents run with the same `gh` token |
| Cost to build | moderate: sync, merge, verify | small, but `configure` assumes `state` sits under the checkout (`STATE.relative_to(HOME)`) | small, but it reverses the design and floods the trunk with status commits | large: a second state model next to the files |

**Verdict: A.** It is the only option that keeps the files, keeps working offline, has a real compare-and-swap (a rejected push), and gives approvals somewhere to carry a signature. B is fine for a team on one shared host, but it offers nothing for trust or history and isn't worth a second code path. C puts queue churn into code review and makes worktrees disagree about the queue. D replaces ergon's model instead of sharing it.

## Recommendation in brief

- `[shared]` in `.ergon.toml` turns it on. `<state>/` becomes a git worktree of an orphan branch, `ergon-state`, so every command still reads and writes the same files.
- Every mutating ergon command makes one local commit on that branch. `ergon sync` fetches, merges with ergon's own field-level rules, and pushes. Only humans and the dispatcher sync. Workers and agent shells never push.
- **IDs:** allocated optimistically over local and last-fetched remote state. A task that was never published is renumbered on sync if its ID was taken. A task must be published before it can be approved, and only approved tasks can be cited in commits, so an ID that any commit cites never changes.
- **Conflicts:** `status` is compare-and-set. A change applies only if the task is still in the status it moved from. Otherwise the first push wins and the loser is told. `## Notes` merges as a union of lines. Other fields and the brief merge three-way; on a real conflict the remote wins and the local version is saved and reported.
- **Dispatch claims** a task by pushing `ready → doing` before launching a worker, so two dispatchers never run the same task.
- **Approvals are signed commits.** In shared mode `approve` makes an SSH-signed commit with an `Ergon-Approve: T-NNN <digest>` trailer. The digest covers the brief, so editing an approved brief needs a new approval. A task counts as approved only if a commit signed by a key listed in `.ergon/approvers`, read from the remote trunk, approves its current digest. Dispatch, `next` and `check` verify that. The `approved:` field becomes a display copy.
- **Two-person rule:** `[[two_person]]` rules, matched like `[[accept]]`, require approvals from two different signers before a task leaves `proposed`.
- **No approval by inheritance.** In local mode a worker's non-feature FOUND follow-up (a bugfix, refactor or chore with declared, unprotected touches, within `follow_up_depth`) goes straight to `ready`, approved by inheritance from the task that found it (SKILL.md, "The one rule"). In shared mode it stays `proposed`. An approval here is a signed commit over one task's digest, and a parent's signature can't vouch for a task that didn't exist when it was signed. An `approved: auto …` stamp has no signed commit behind it, so dispatch, `next` and `check` treat it like any unsigned stamp.

## Design

### Layout and config

```toml
[shared]
remote = "origin"            # a remote name or URL. Use a private repo if the code repo is public
branch = "ergon-state"       # orphan branch holding items/ and epics/
# approvers = ".ergon/approvers"   # git allowed_signers file, read from <remote>/<trunk>

[[two_person]]               # optional, repeatable; matched like [[accept]]
tiers = ["opus"]
touches = ["server/auth/**"]
```

- `<state>/` (default `tasks/`) stays gitignored on the trunk and becomes a git worktree of `ergon-state`, so `ITEMS`, `EPICS` and `parse`/`dump` don't change. It sits inside the main checkout the way `.claude/worktrees/` already does.
- The state branch has its own `.gitignore` for `runs/`, `items/.lock` and `conflicts/`. Run logs hold transcripts and costs per machine, so they stay local. `stats` and `report` stay per machine.
- **Privacy.** Publishing the queue publishes every brief and every Note. If the code repo is public, an `ergon-state` branch on the same remote is public too. `remote` can be the URL of a separate private repo, and `sync --init` warns when the remote is the same one the trunk tracks or can be read without logging in, and asks before pushing.
- Turning it off: remove `[shared]`. The worktree stays as plain files, and ergon reads it as local state again.

### Sync, and what gets pushed

- Every command that changes state (`new`, `set`, `tidy`, `approve`, `reject`, `accept`, `close`, `import`, and dispatch's status moves) commits its change locally on `ergon-state`, under the existing `state_lock`. The message is short (`set T-015 status ready->doing`). Hand edits to task files are committed by the next ergon command or by `sync`. These local commits need no network.
- `ergon sync`: under the lock, commit any pending edits, `git fetch <remote> <branch>`, merge (next sections), push. If the push is rejected because someone pushed in between, it fetches and merges again, a few times at most. It prints what the merge changed and anything it had to refuse.
- **It merges, never rebases.** Approvals are signed commits, and a rebase would rewrite them and drop the signatures. The merge commit's tree is computed by ergon. It is not git's text merge.
- **Who pushes.** `sync` refuses inside agent shells (`CLAUDECODE` / `AI_AGENT`, like `approve`) and on a `task/` branch. An interactive agent's `new` or `set` is committed locally and published at the human's next `sync`. Nothing an agent does leaves the machine until a human syncs. `approve`, `accept`, `reject` and `close` sync before and after, since they already run only in a human's terminal. Dispatch syncs for its claims. That means dispatch in shared mode must be started by a human, which is how it is used today anyway. The guard gains two refusals: `ergon sync` from an agent, and git writes inside the state worktree.
- The README now says "No code is ever pushed; in shared mode ergon pushes the state branch".

### ID allocation

Today `next_id` is `max + 1` over local files. Two people offline both make `T-016`.

Options considered:

| | How | Problem |
|---|---|---|
| Reserve on create | `new` pushes a counter bump before writing the file | every `new` needs the network, including dispatch filing FOUND lines and blocked-task follow-ups under the lock |
| Per-person namespaces | `T-PS-016`, `T-016.ps` | breaks every ID regex (`TRACKER_ID`, `cited_tasks`, `CITE`, map links), branch names and habit |
| Leased blocks | each clone leases 20 IDs by push | offline creation fails once the lease runs out; gaps; more state |
| **Optimistic, renumber unpublished** | `max + 1` over local ∪ last-fetched remote; on sync, renumber local tasks whose ID was taken | an ID can change until it is published |

**Recommended: optimistic with renumbering**, made safe by one new rule. **In shared mode, `approve` and `start` refuse a task or epic that isn't published yet.** They sync first, so in practice they publish it. The commit-msg hook already refuses commits that cite an unapproved task. So an ID that might change can't be in any commit or on any branch name. Renumbering only ever touches local, proposed, unpublished files.

On sync, a local task is a collision if its ID isn't in the merge base, exists on the remote, and the remote file isn't the same task. Ergon gives it the next free ID and rewrites:

- the file name and `id:`,
- `depends:` in every local task that names it (`file_human` adds such a dependency to the blocked task),
- `source:` of tasks filed from it.

It doesn't edit tracked repo files. It searches the map cards and the `log` for the old ID and prints each hit, so a human can fix the `→ task` links. Sync's output lists every renumbering (`T-016 → T-019`). Epics are handled the same way.

### Concurrent edits to one task

The merge is per task, keyed by `id:` rather than path, so a task one side moved into an epic folder still pairs up. Ergon never deletes task files, so there are no delete/modify cases. For each task changed on both sides since the merge base:

| Part | Rule |
|---|---|
| `status` | Compare-and-set. Each local status change records the status it moved from (in the commit message, or as the merge-base value). It applies only if the remote still has that status. Otherwise the remote wins (it pushed first), the local move is dropped and reported, and a Note records it (`- 2026-11-09 sync: dropped dana's ready->blocked, remote had moved to doing`). |
| `approved` | Not merged as text. It is recomputed from verified approvals (below). |
| `accept` | Sticky toward `yes`: if either side has it, the result has it. Only a human takes it off, and that is a one-sided edit. |
| `title`, `tier`, `touches`, `depends`, `epic`, `tier_why`, `source` | Three-way per field. If both sides changed a field differently, the remote wins and the result is reported. A tier or touches change after approval changes the digest, so the task needs approving again either way. |
| `## Notes` | Union of lines in time order. Notes are append-only by convention, and dispatch, reject and sync all only append. |
| Rest of the body (What, Done when) | Three-way text merge (`git merge-file`). On a conflict the remote wins, the local version is saved to `<state>/conflicts/<ID>-<time>.md` (local only) and reported. |

Why "remote wins" and not something smarter: the remote side is what the team has already seen and maybe acted on. Every loss is printed, and none is silent. The costly race, two workers on one task, is prevented up front by claims, not repaired by the merge.

### Dispatch claims

- Before launching a worker, dispatch syncs and moves the task `ready → doing` with a local commit, then pushes. If the push is rejected, it fetches and merges. If the compare-and-set now fails because someone else claimed the task, it skips it.
- `--watch` syncs on every scan, which is also how it sees tasks a teammate approved.
- `done`, `blocked` and `review` are pushed when the task lands or stops. FOUND and blocked-task follow-ups are new proposed tasks, renumbered on sync if needed.
- **Held for review on someone else's machine.** `task/<ID>` branches stay local, so only the clone that ran dispatch can `accept`. `check`'s "in review but branch is gone" error should apply only in that clone. Dispatch records which clone holds the branch (for example `held: <user>@<host>` in the Note it already writes), and other clones show "held on dana@laptop". Pushing task branches so anyone can accept would be a separate decision. It would push code, so it is out of scope here.
- Epic branches are code branches and still shared through the team's normal git flow. `set`'s "status changes only from the epic's branch" check only sees local branches. A teammate without that branch isn't stopped. That stays a speed bump, and it is noted in the docs.

### Trusting approval stamps

Today `approved: <git user.name> <date>` is free text. On one machine the trust boundary is "my machine, my agents", and the CLI, guard, lint and commit hook are speed bumps inside it. Once shared, a stamp on my machine may have been written on yours. `user.name` is self-asserted, and push access doesn't tell a human from an agent, because agents run with the human's credentials.

Options considered:

- **Trust push access** (branch protection on `ergon-state`). This adds nothing over today: any agent with a shell can push.
- **Approve through the forge** (a PR against the state branch, reviewed). This is real identity, but it puts a PR round-trip behind every approval, and `gh` tokens are just as reachable by agents.
- **Signed approval commits, verified against a reviewed allowlist.** Recommended.

How it works:

1. **Approvers.** `.ergon/approvers` is a git `allowed_signers` file (`pavel@… namespaces="git" ssh-ed25519 AAAA…`), committed on the trunk. Ergon reads it from `<remote>/<trunk>`, not from the working tree, so changes to it go through the team's code review and branch protection. A local agent edit, or a dispatched task merged into the local trunk, doesn't count until it is merged upstream. Projects should also `[[protect]]` it with `min_tier = "human"`.
2. **Digest.** `approve` computes a digest of what is being approved. For a task that is `id`, `title`, `tier`, `touches`, `accept`, and the What and Done when sections, normalised. For an epic brief it is `id`, `title`, and the Goal, Out of scope and Constraints sections, normalised. `status`, `depends`, `epic` and Notes are left out of both. Dispatch adds dependencies to blocked tasks, an added dependency only holds work back, and Notes are scratch. The stamp becomes `approved: Pavel Sinelnikov 2026-11-08 sha256:3f2a9c1e`. There is no `#` in it, because `parse` treats ` #` as a comment.
3. **Signed commit.** In shared mode `approve` makes its state commit with `git commit -S` (SSH signing, git 2.34+) and a trailer per task or epic brief: `Ergon-Approve: T-015 sha256:3f2a9c1e…`, or `Ergon-Approve: E-003 sha256:…` for an epic. `accept`, `reject` and `close` sign theirs too (`Ergon-Accept:` and so on), so the record of human decisions is verifiable. Only approvals gate execution.
4. **Verification.** A task counts as approved only if some commit on `ergon-state` both
   - has a good signature (`%G?` = `G`) from a key in the approvers file, checked with `git -c gpg.ssh.allowedSignersFile=<that file> log --format=…`, and
   - carries an `Ergon-Approve` trailer for that task (or epic) whose digest matches its brief as it is now.

   Dispatch checks this before launching, `next` hides tasks that fail it, and `check` reports them (`T-015: brief changed after approval, or approval not signed by an approver`). The `approved:` field is a convenient copy that `sync` rewrites from the verified set. Editing it by hand does nothing.

   Epics use the same check. An epic's approval is signed and verified like a task's, with the epic's own digest. If an open epic's approval fails, `next` offers none of its tasks, dispatch neither starts them nor breaks the epic down, and `check` reports `E-NNN: open, but its approval doesn't verify`. Epic approvals are the human's `ergon approve E-NNN`, run in a terminal.
5. **What this stops.** A teammate's agent, or a compromised clone, that writes `status: ready` plus a plausible stamp and pushes it. Editing an approved brief to say something else, which today keeps its stamp. A "merge" that invents an approval: trailers live in signed commits, not in the merged files.
6. **What it doesn't stop.** An agent that can use the human's signing key. If the key sits unlocked in an `ssh-agent` with no confirmation, an agent in that shell can sign. The docs should say to use a key that needs a touch or a confirmation (a hardware key, `ssh-add -c`, a password manager's agent with a prompt). That turns the signature into proof a human was there, which is stronger than today's `/dev/tty` check. Denial is also possible: anyone with push access can drop or reset tasks. Branch protection against force pushes on `ergon-state` limits that, and the history keeps it visible.

**Old epics (the exemption).** An epic from before epics were approved has no `approved:` stamp. An open one with no stamp counts as approved, in local and shared mode alike, so existing projects keep working. `next`, dispatch and `check` don't flag it. Nothing new is grandfathered: `new epic` writes `proposed`, `set` won't move an epic out of `proposed`, and the guard refuses an agent's new or open epic brief. `restamp` (run by `sync`) leaves epics alone, so it never writes a stamp for an unstamped epic and never clears a stamp that fails. `check` reports a stamped open epic whose approval doesn't verify, but not an unstamped one. Removing this exemption is tracked in T-124, which is not built yet; until it lands, this is the behaviour.

In local mode, nothing changes. Recording the digest in local stamps too, to catch edits to approved briefs, would be cheap and useful. It is listed as an open question, not part of this design.

### Two-person rule (optional)

```toml
[[two_person]]
tiers = ["opus"]                 # and/or touches = [...]; both given means both must match
```

- Only available in shared mode, since it needs verified, distinct identities.
- A matching task needs `Ergon-Approve` trailers for its current digest from **two different principals** in the approvers file. One person with two keys is one principal.
- The first `approve` makes its signed commit and adds a Note (`approved by Pavel (1 of 2)`). The task stays `proposed` and `approved:` stays empty. The second approval moves it to `ready` and writes the stamp with both names. `ergon review` shows "1 of 2: Pavel" so the queue says who is still needed.
- The rule is matched against the task as it is at each approval. Retiering a task into or out of a rule changes its digest, so earlier approvals no longer count either way.
- The commit-msg hook must then treat a `proposed` task as unapproved even if it carries a stamp. Today it checks only that `approved:` isn't empty (`commit_msg_reason`). The two-person task fixes that.
- Possible extension, left open: for matching tasks that also need acceptance, `accept` must be signed by someone other than the person whose clone dispatched the task.

### Changes to existing commands

| Command | Shared-mode change |
|---|---|
| `init` | unchanged. New: `sync --init` creates the orphan branch from current local state and pushes it. On a fresh clone, `sync --join` makes the worktree. **Built differently:** it refuses if `<state>/` already holds task files (folding them in with renumbering isn't built); move them out, join, and refile |
| `new`, `import`, `set`, `tidy` | commit locally; IDs over local ∪ remote |
| `approve`, `review -i` | sync, refuse unpublished, sign, check the two-person rule, sync |
| `accept`, `reject`, `close` | sync, sign, sync |
| `next`, `board`, `show` | read local state; `next` hides tasks whose approval doesn't verify; `board` shows the time of the last sync |
| `check`, Stop hook | also: approvals verify, no unresolved renumbering, review-branch check only in the holding clone |
| `dispatch` | claims via push; `--watch` syncs per scan; refuses inside agent shells |
| commit-msg hook | unchanged rule, except a stamped `proposed` task is unapproved |
| guard | refuses `ergon sync` and git writes in the state worktree from agents |

### Rollout

1. One person runs `ergon sync --init`. It warns if the remote is public or is the trunk's own remote, and asks for confirmation.
2. They add `[shared]` and `.ergon/approvers` in a normal reviewed commit.
3. Teammates pull, run `ergon sync --join`, and set up SSH commit signing.
4. **Built differently:** `--join` refuses a `<state>/` that already has task files. Approved local tasks that collide with published IDs are reported, not renumbered. Their IDs may already be in commits, so a human resolves them with `close` or by refiling.

## Open questions

- Should local mode also record and check the brief digest? It would catch edits to approved briefs today, at the cost of stamps changing format.
- Should accepting a two-person task require someone other than the dispatching person?
- Should `sync` run automatically after every mutating human command, or stay explicit? This design keeps it explicit, plus the human-only commands and dispatch, which sync themselves.
- Task branches held for `review` stay local. Letting others accept would mean pushing code.

## Follow-up tasks

Filed as `proposed`, in dependency order:

1. T-016: shared state branch and `ergon sync` (layout, local commits, fetch/merge/push, who may push).
2. T-017: collision-free IDs: allocation over remote state, renumbering unpublished tasks, refusing to approve or start unpublished ones.
3. T-018: field-level merge rules for `ergon sync`.
4. T-019: dispatch claims and review-branch locality in shared mode.
5. T-020: brief digest and signed, verified approvals.
6. T-021: two-person rule.
7. T-022: docs: SKILL.md and README for shared mode.
