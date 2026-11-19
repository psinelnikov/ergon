"""`ergon obsidian`: unticked, tagged checkbox lines in an Obsidian vault become proposed tasks.

Read-only toward the vault (write-back is T-156). A line is untrusted text, as an issue body is for
`ergon import`: quoted into What, `tier: human`, never approved here."""
import fnmatch
import hashlib
import os
import re
from pathlib import Path

from . import config as _config
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
            if CHECKBOX.match(line) and pat.search(line):
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


def obsidian(args):
    cfg = settings()
    if cfg is None:
        print("ergon obsidian: not configured (no [obsidian] table in .ergon.toml); nothing to do")
        return
    if cfg["write_back"]:
        print("warning: write_back = true, but block ids come with T-156: importing with hash keys")
    hits = find(cfg["vault"], cfg["tag"], cfg["exclude"])
    filed = skipped = 0
    with _gitops.state_lock():
        seen = {t.get("source") for t in _tasks.load(_config.ITEMS).values()}
        for note, text in hits:
            k = key(note, text)
            src = f"obsidian:{note}#{k}"
            if src in seen:
                skipped += 1
                continue
            seen.add(src)
            title = title_of(text, cfg["tag"])
            if args.dry_run:
                print(f"would file: {title}  ({src})")
                filed += 1
                continue
            tid = _tasks.next_id(_config.ITEMS, "T")
            meta = {"id": tid, "title": title, "tier": "human", "category": "", "status": "proposed",
                    "epic": "", "depends": [], "touches": [], "source": source_of(note, k),
                    "created": _tasks.today(), "approved": "",
                    "tier_why": "imported from Obsidian, not sized: pick the tier before approving",
                    "_path": _config.ITEMS / f"{tid}-{_tasks.slug(title)}.md", "_body": body_of(note, text)}
            _tasks.dump(meta, _tasks.TASK_KEYS)
            _history.record("created", meta, tier="human", title=title, source=src, by="import")
            print(f"{meta['_path'].relative_to(_config.HOME)}  ({tid}, proposed, tier human)")
            filed += 1
    verb = "would file" if args.dry_run else "filed"
    print(f"found {len(hits)} tagged line(s): {verb} {filed}, skipped {skipped} already imported")
