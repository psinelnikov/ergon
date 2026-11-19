"""`ergon obsidian`: unticked, tagged checkbox lines in an Obsidian vault become proposed tasks.

A line is untrusted text, as an issue body is for `ergon import`: quoted into What, `tier: human`,
never approved here. With `write_back = true` (and never in an agent shell) it also writes to the vault:
a ` ^ergon-<hash>` block id on each imported line, and a tick on the line of a task that is done or
dropped. Each write changes one line, keeps every other byte, goes through a temp file and a rename, and
is skipped when the note changed since it was read."""
import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import config as _config
from . import gates as _gates
from . import gitops as _gitops
from . import history as _history
from . import tasks as _tasks

CHECKBOX = re.compile(r"^\s*[-*]\s+\[ \]\s+(\S.*)$")
BLOCK_ID = re.compile(r"\s\^[\w-]+\s*$")
DATE = r"\d{4}-\d{2}-\d{2}"
EMOJI = "📅⏳🛫➕✅🔁⏫🔼🔽"
FIELD = re.compile(rf"(?:[📅⏳🛫➕✅]️?\s*{DATE}|🔁️?[^{EMOJI}]*|[⏫🔼🔽]️?)")
DUE = re.compile(rf"📅️?\s*({DATE})")
SCHEDULED = re.compile(rf"⏳️?\s*({DATE})")
TITLE_MAX = 70


def settings():
    """The [obsidian] table as {vault, tag, exclude, write_back}, or None when it isn't configured."""
    tbl = _config.CFG.get("obsidian")
    if tbl is None:
        return None
    where = f"{_config.CONFIG_FILE}: [obsidian]"
    if not isinstance(tbl, dict) or not isinstance(tbl.get("vault"), str) or not tbl["vault"].strip():
        raise SystemExit(f"ergon: {where} needs vault, a path")
    tag = tbl.get("tag", "agent")
    exclude = tbl.get("exclude", [])
    exclude = [exclude] if isinstance(exclude, str) else exclude
    wb = tbl.get("write_back", False)
    if not isinstance(tag, str) or not tag.strip().lstrip("#"):
        raise SystemExit(f"ergon: {where} tag is a tag name such as \"agent\"")
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        raise SystemExit(f"ergon: {where} exclude is a list of folder patterns")
    if not isinstance(wb, bool):
        raise SystemExit(f"ergon: {where} write_back is true or false")
    vault = Path(os.path.expanduser(tbl["vault"].strip()))
    if not vault.is_absolute():
        vault = _config.HOME / vault
    if not vault.is_dir():
        raise SystemExit(f"ergon: {where} vault {str(vault)!r} doesn't exist or isn't a folder")
    return {"vault": vault, "tag": tag.strip().lstrip("#"), "exclude": [x.strip("/") for x in exclude],
            "write_back": wb}


def notes(vault, exclude):
    """Relative posix paths of the vault's .md files, skipping dot-folders and `exclude` folders."""
    out = []
    for dirpath, dirs, names in os.walk(vault):
        rel = Path(dirpath).relative_to(vault).as_posix()
        rel = "" if rel == "." else rel
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and not any(fnmatch.fnmatchcase(f"{rel}/{d}".lstrip("/"), p) or fnmatch.fnmatchcase(d, p)
                                     for p in exclude))
        out += [f"{rel}/{n}".lstrip("/") for n in sorted(names) if n.endswith(".md") and not n.startswith(".")]
    return sorted(out)


def tag_re(tag):
    return re.compile(rf"(?<![\w/#-])#{re.escape(tag)}(?![\w/-])", re.I)


def find(vault, tag, exclude):
    """[(note, line text)] of the unticked checkbox lines carrying the tag, in file order."""
    pat, hits = tag_re(tag), []
    for note in notes(vault, exclude):
        try:
            text = (vault / note).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if CHECKBOX.match(line) and pat.search(line) and not MARK.search(line):  # a marked line is dealt with
                hits.append((note, line.strip()))
    return hits


def key(note, text):
    return hashlib.sha256((note + text).encode("utf-8")).hexdigest()[:10]


def title_of(text, tag):
    body = CHECKBOX.match(text).group(1)
    body = BLOCK_ID.sub("", body)
    body = tag_re(tag).sub("", body)
    body = FIELD.sub("", body).replace("️", "")
    title = re.sub(r"\s+", " ", BLOCK_ID.sub("", body)).strip().strip("\"'")
    title = re.sub(r"(?<=\s)#|^#+\s*", "", title)  # ` #` starts a comment in frontmatter
    if title.startswith("[") and title.endswith("]"):
        title = title[1:-1].strip()
    title = title or "Obsidian task"
    return title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 1].rstrip() + "…"


def body_of(note, text):
    quoted = "\n".join(f"> {l}".rstrip() for l in text.splitlines())
    notes_ = []
    for label, rx in (("due", DUE), ("scheduled", SCHEDULED)):
        if m := rx.search(text):
            notes_.append(f"{label}: {m.group(1)}\n")
    return (f"\n## What\nImported from the Obsidian note `{note}`. Its text was written outside this repo: "
            f"rewrite it as a brief, size it and fill in Done when before approving.\n\n{quoted}\n"
            f"\n## Done when\n\n## Notes\n" + "".join(notes_))


def source_of(note, k):
    src = f"obsidian:{note}#{k}"
    return f'"{src}"' if re.search(r"\s#", src) else src


BOX = re.compile(r"^(\s*[-*]\s+)\[([ xX])\]")
MARK = re.compile(r"\(ergon T-\d+[^)]*\)")
STRIP_MARK = re.compile(rf"\s*(?:✅\s*{DATE}\s*)?\(ergon T-\d+(?: dropped)?\)")


def split_ref(src):
    """(note, ref) of an `obsidian:<note>#<ref>` source, or None. ref is `^block-id` or a hash key."""
    src = src.strip().strip("\"'")
    if not src.startswith("obsidian:") or "#" not in src:
        return None
    note, ref = src[len("obsidian:"):].rsplit("#", 1)
    return note, ref


def rewrite(path, old, new):
    """Replace `path`'s bytes with `new` via temp file + rename, unless it no longer holds `old`."""
    path = path.resolve()
    try:
        if path.read_bytes() != old:
            return False
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ergon-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(new)
            shutil.copymode(path, tmp)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError:
        return False
    return True


def read_note(vault, note):
    """(raw bytes, list of lines split on \\n) or (None, None) when the note can't be read."""
    try:
        raw = (vault / note).read_bytes()
        return raw, raw.decode("utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return None, None


def put_lines(raw, lines, changed, vault, note):
    """Write `lines` back if any changed. True when written or nothing to do, False when skipped."""
    if not changed:
        return True
    return rewrite(vault / note, raw, "\n".join(lines).encode("utf-8"))


def with_block_id(line, bid):
    return line.rstrip() + f" ^{bid}"


def add_ids(vault, note, wanted):
    """Append ` ^<id>` to the first unticked line whose text is `text`, for each (text, id) in `wanted`.
    Returns the set of texts done, empty when the note changed or can't be read."""
    raw, lines = read_note(vault, note)
    if raw is None:
        return set()
    done = set()
    for text, bid in wanted:
        for n, part in enumerate(lines):
            body = part.rstrip("\r")
            if CHECKBOX.match(body) and body.strip() == text:
                lines[n] = with_block_id(body, bid) + part[len(body):]
                done.add(text)
                break
    return done if put_lines(raw, lines, bool(done), vault, note) else set()


def normal(line):
    """A line as it was imported: the ergon mark off, the box unticked."""
    return BOX.sub(r"\1[ ]", STRIP_MARK.sub("", line.strip()), count=1)


def locate(lines, note, ref):
    for n, part in enumerate(lines):
        body = part.rstrip("\r")
        if ref.startswith("^"):
            if re.search(rf"\s{re.escape(ref)}\s*$", body):
                return n
        elif BOX.match(body) and key(note, normal(body)) == ref:
            return n
    return None


def ticked(line, tid, status):
    """The line after marking it for a done/dropped task, or the same line when nothing is due."""
    if MARK.search(line):
        return line
    m = BOX.match(line)
    if status == "done":
        if not m or m.group(2) != " ":
            return line
        line = BOX.sub(r"\1[x]", line, count=1)
        mark = f" ✅ {_tasks.today()} (ergon {tid})"
    else:
        mark = f" (ergon {tid} dropped)"
    b = BLOCK_ID.search(line)
    if b:  # a block id has to stay the last thing on its line
        return line[:b.start()].rstrip() + mark + " " + b.group().strip()
    return line.rstrip() + mark


def tick(cfg, write):
    """Mark the line of each done/dropped imported task. Returns the number of lines changed (or to change)."""
    vault, by_note = cfg["vault"], {}
    for tid, t in sorted(_tasks.load(_config.ITEMS).items()):
        sr = split_ref(str(t.get("source", "")))
        if sr and t.get("status") in ("done", "dropped"):
            by_note.setdefault(sr[0], []).append((tid, t["status"], sr[1]))
    count = 0
    for note, todo in sorted(by_note.items()):
        raw, lines = read_note(vault, note)
        changes = []
        for tid, status, ref in todo:
            n = None if lines is None else locate(lines, note, ref)
            if n is None:
                print(f"{tid}: line gone from {note}")
                continue
            body = lines[n].rstrip("\r")
            new = ticked(body, tid, status)
            if new != body:
                changes.append((tid, n, body, new))
        for tid, n, body, new in changes:
            lines[n] = new + lines[n][len(body):]
        if write and changes and not put_lines(raw, lines, True, vault, note):
            for tid, *_ in changes:
                print(f"{tid}: {note} changed since it was read; skipped, run again")
            continue
        for tid, n, body, new in changes:
            print(f"{tid}: {'ticked' if write else 'would change'} in {note}\n  - {body.strip()}\n  + {new.strip()}")
            count += 1
    return count


def obsidian(args):
    cfg = settings()
    if cfg is None:
        print("ergon obsidian: not configured (no [obsidian] table in .ergon.toml); nothing to do")
        return
    shell = _gates.in_agent_shell()
    wb = cfg["write_back"]
    write = wb and not shell and not args.dry_run
    hits = find(cfg["vault"], cfg["tag"], cfg["exclude"])
    filed = skipped = 0
    with _gitops.state_lock():
        seen = {t.get("source") for t in _tasks.load(_config.ITEMS).values()}
        fresh = []  # (note, text, hash src, block id or None, id is new)
        for note, text in hits:
            k = key(note, text)
            src = f"obsidian:{note}#{k}"
            b = BLOCK_ID.search(text)
            bid = b.group().strip()[1:] if b and wb else None
            if src in seen or (bid and f"obsidian:{note}#^{bid}" in seen):
                skipped += 1
                continue
            seen.add(src)
            if bid:
                seen.add(f"obsidian:{note}#^{bid}")
            fresh.append([note, text, k, bid, False])
        if wb and not shell:
            for f in fresh:
                if not f[3]:
                    f[3], f[4] = f"ergon-{f[2]}", True
            if write:
                for note in sorted({f[0] for f in fresh if f[4]}):
                    done = add_ids(cfg["vault"], note, [(f[1], f[3]) for f in fresh if f[0] == note and f[4]])
                    for f in fresh:
                        if f[0] == note and f[4] and f[1] not in done:
                            print(f"{note}: changed since it was read, or unreadable; no block id added, "
                                  f"filing {f[1]!r} with a hash key")
                            f[3] = None
        for note, text, k, bid, new_id in fresh:
            ref = f"^{bid}" if bid else k
            src = f"obsidian:{note}#{ref}"
            title = title_of(text, cfg["tag"])
            if args.dry_run:
                print(f"would file: {title}  ({src})")
                if new_id:
                    print(f"would add ^{bid} to {note}\n  - {text}\n  + {with_block_id(text, bid)}")
                filed += 1
                continue
            tid = _tasks.next_id(_config.ITEMS, "T")
            meta = {"id": tid, "title": title, "tier": "human", "category": "", "status": "proposed",
                    "epic": "", "depends": [], "touches": [], "source": source_of(note, ref),
                    "created": _tasks.today(), "approved": "",
                    "tier_why": "imported from Obsidian, not sized: pick the tier before approving",
                    "_path": _config.ITEMS / f"{tid}-{_tasks.slug(title)}.md", "_body": body_of(note, text)}
            _tasks.dump(meta, _tasks.TASK_KEYS)
            _history.record("created", meta, tier="human", title=title, source=src, by="import")
            print(f"{meta['_path'].relative_to(_config.HOME)}  ({tid}, proposed, tier human)")
            filed += 1
    verb = "would file" if args.dry_run else "filed"
    print(f"found {len(hits)} tagged line(s): {verb} {filed}, skipped {skipped} already imported")
    if wb:
        if shell and not args.dry_run:
            print("ergon obsidian: running in an agent shell; writing the vault is a human's, so nothing was written")
        n = tick(cfg, write)
        if n:
            print(f"{n} line(s) {'ticked' if write else 'to tick (not written)'}")
