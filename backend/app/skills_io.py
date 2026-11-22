"""Agent Skills (SKILL.md) import and export.

Agent HQ reads and writes skills in the Agent Skills format (https://agentskills.io), the
one Hermes skills and the Skills Hub use: a folder with a SKILL.md (YAML frontmatter, then
markdown instructions) and, optionally, other files (scripts/, references/, assets/).

How the fields map:

| SKILL.md                  | Agent HQ                                                      |
|---------------------------|---------------------------------------------------------------|
| `name` (hyphens)          | name (underscores): `check-pve-backups` ↔ `check_pve_backups` |
| `description`             | When to use                                                   |
| first `# Heading` of body | Title (the rest of the body is the procedure)                 |
| `allowed-tools`           | Tools it relies on (space-separated)                          |
| `metadata.agent-hq-script`| Workbench script                                              |
| anything else             | kept as is (license, compatibility, other metadata)           |
| other files in the folder | the skill's files, which agents read with `read_skill_file`   |

So a skill exported from Agent HQ and imported back is unchanged.

Imports never change a skill straight away: each skill found becomes a proposal (Requests →
Skill proposals) that runs through the evals like any other skill change and waits for you.
Sources: a SKILL.md (uploaded or pasted), a folder, a zip (one skill or many), or a Git
repository (every SKILL.md in it, or under a path).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse

import yaml
from sqlmodel import select

from . import db, skills
from .models import Skill, SkillProposal

log = logging.getLogger("agent_hq.skills_io")

MAX_SKILLS = 50                  # per import
MAX_FILES = 200                  # per skill, besides SKILL.md
MAX_SKILL_BYTES = 5_000_000      # a skill's files together
MAX_UNPACKED = 50_000_000        # a zip, unpacked
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".DS_Store", "__MACOSX"}
SCRIPT_KEY = "agent-hq-script"


@dataclass
class Parsed:
    """One skill read from a SKILL.md (and its folder)."""

    name: str
    title: str
    when_to_use: str
    body: str
    tools: list[str] = field(default_factory=list)
    script: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, str]] = field(default_factory=list)
    where: str = ""  # path of the SKILL.md within what was imported
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- names


def to_spec_name(name: str) -> str:
    return name.replace("_", "-")


def from_spec_name(name: str) -> str:
    """An Agent Skills name as an Agent HQ one: lowercase_with_underscores, 3-61 characters,
    starting with a letter."""
    n = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    if n and not n[0].isalpha():
        n = "skill_" + n
    if 0 < len(n) < 3:
        n = n + "_skill"
    return n[:61].rstrip("_")


def _title_from_name(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().capitalize()


# --------------------------------------------------------------------------- SKILL.md

_FRONT = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.S)


def parse(text: str, folder: str = "") -> Parsed:
    """Read a SKILL.md. `folder` is its directory's name, used when there's no `name`."""
    m = _FRONT.match(text or "")
    if not m:
        raise ValueError("a SKILL.md starts with YAML frontmatter between --- lines")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"the frontmatter isn't valid YAML: {e}") from None
    if not isinstance(meta, dict):
        raise ValueError("the frontmatter must be a mapping (name: ..., description: ...)")
    meta = dict(meta)
    raw_name = str(meta.pop("name", "") or folder or "").strip()
    name = from_spec_name(raw_name)
    if not name:
        raise ValueError("the frontmatter has no name")
    description = str(meta.pop("description", "") or "").strip()
    if not description:
        raise ValueError(f"{raw_name}: the frontmatter has no description")
    body = m.group(2).replace("\r\n", "\n").strip()
    title = ""
    head = re.match(r"\A#[ \t]+(.+?)[ \t]*(?:\n|\Z)", body)
    if head:
        title, body = head.group(1).strip(), body[head.end():].strip()
    tools_raw = meta.pop("allowed-tools", None)
    if isinstance(tools_raw, list):
        tools = [str(t).strip() for t in tools_raw if str(t).strip()]
    else:
        tools = str(tools_raw or "").split()
    script = None
    md = meta.get("metadata")
    if isinstance(md, dict):
        md = dict(md)
        script = str(md.pop(SCRIPT_KEY, "") or "").strip() or None
        if md:
            meta["metadata"] = md
        else:
            meta.pop("metadata")
    p = Parsed(name=name, title=title or _title_from_name(raw_name), when_to_use=description, body=body,
               tools=tools, script=script, frontmatter=_jsonable(meta))
    if raw_name and to_spec_name(name) != raw_name:
        p.notes.append(f"name {raw_name!r} is {name} here")
    if not body:
        p.notes.append("the SKILL.md has no instructions after the frontmatter")
    return p


def _jsonable(v: Any) -> Any:
    """Frontmatter as plain JSON (YAML can hold dates and the like)."""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)


def render(k: Skill | Any) -> str:
    """A skill as SKILL.md: frontmatter, then `# Title` and the procedure."""
    meta: dict[str, Any] = {"name": to_spec_name(k.name), "description": k.when_to_use}
    extra = dict(k.frontmatter or {})
    md = extra.pop("metadata", None)
    for key, v in extra.items():
        if key not in ("name", "description", "allowed-tools"):
            meta[key] = v
    if k.tools:
        meta["allowed-tools"] = " ".join(k.tools)
    if k.script:
        md = {**(md if isinstance(md, dict) else {}), SCRIPT_KEY: k.script}
    if md:
        meta["metadata"] = md
    front = yaml.dump(meta, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1_000_000)
    return f"---\n{front}---\n\n# {k.title}\n\n{k.body.strip()}\n"


def folder_name(k: Skill | Any) -> str:
    return to_spec_name(k.name)


def write_zip(items: list[Skill]) -> bytes:
    """Skills as a zip: one folder per skill, each with its SKILL.md and files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k in items:
            top = folder_name(k)
            z.writestr(f"{top}/SKILL.md", render(k))
            for f in k.files or []:
                data = (base64.b64decode(f.get("content", "")) if f.get("encoding") == "base64"
                        else f.get("content", "").encode())
                z.writestr(f"{top}/{f['path']}", data)
    return buf.getvalue()


# --------------------------------------------------------------------------- reading what was given


def _clean_path(p: str) -> str | None:
    parts = [x for x in PurePosixPath(p.replace("\\", "/")).parts if x not in ("", ".", "/")]
    if not parts or any(x == ".." for x in parts) or any(x in SKIP_PARTS for x in parts):
        return None
    return "/".join(parts)


def _file_entry(path: str, data: bytes) -> dict[str, str]:
    try:
        text = data.decode("utf-8")
        if "\x00" not in text:
            return {"path": path, "content": text, "encoding": "utf-8"}
    except UnicodeDecodeError:
        pass
    return {"path": path, "content": base64.b64encode(data).decode(), "encoding": "base64"}


def from_files(files: list[tuple[str, bytes]]) -> tuple[list[Parsed], list[str]]:
    """Skills in a set of files: a lone SKILL.md (any file name), a folder's files with
    their relative paths, zips (unpacked) or a mix. Returns (skills, problems)."""
    tree: dict[str, bytes] = {}
    problems: list[str] = []
    for name, data in files:
        if name.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
            try:
                tree.update(_unzip(data, prefix=""))
            except ValueError as e:
                problems.append(f"{name}: {e}")
            continue
        p = _clean_path(name)
        if p:
            tree[p] = data
    if not tree:
        return [], problems or ["nothing to import"]
    if not any(PurePosixPath(p).name.lower() == "skill.md" for p in tree):
        mds = [p for p in tree if p.lower().endswith(".md")]
        if len(tree) == 1 and mds:  # a single markdown file under another name
            only = mds[0]
            tree = {"SKILL.md": tree[only]}
        else:
            return [], problems + ["no SKILL.md found"]
    found, more = from_tree(tree)
    return found, problems + more


def _unzip(data: bytes, prefix: str) -> dict[str, bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a valid zip file") from None
    total, out = 0, {}
    for info in z.infolist():
        if info.is_dir():
            continue
        total += info.file_size
        if total > MAX_UNPACKED:
            raise ValueError(f"it unpacks to more than {MAX_UNPACKED // 1_000_000} MB")
        p = _clean_path(prefix + info.filename)
        if p:
            out[p] = z.read(info)
    return out


def from_tree(tree: dict[str, bytes]) -> tuple[list[Parsed], list[str]]:
    """Every skill in a tree of {relative path: bytes}: each SKILL.md and the files in its
    folder (not those in a nested skill's folder)."""
    roots = sorted({str(PurePosixPath(p).parent) for p in tree if PurePosixPath(p).name.lower() == "skill.md"},
                   key=lambda r: (r.count("/"), r))
    roots = ["" if r == "." else r for r in roots]
    problems: list[str] = []
    if len(roots) > MAX_SKILLS:
        problems.append(f"found {len(roots)} skills; importing the first {MAX_SKILLS}")
        roots = roots[:MAX_SKILLS]
    out: list[Parsed] = []

    def owner(path: str) -> str | None:
        best = None
        for r in roots:
            if r == "" or path == r or path.startswith(r + "/"):
                if best is None or len(r) > len(best):
                    best = r
        return best

    by_root: dict[str, list[tuple[str, bytes]]] = {r: [] for r in roots}
    md_of: dict[str, str] = {}
    for p, data in sorted(tree.items()):
        r = owner(p)
        if r is None:
            continue
        rel = p[len(r) + 1:] if r else p
        if rel.lower() == "skill.md":
            md_of[r] = p
        else:
            by_root[r].append((rel, data))
    for r in roots:
        where = md_of[r]
        try:
            text = tree[where].decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{where}: not UTF-8 text")
            continue
        try:
            sk = parse(text, folder=PurePosixPath(r).name if r else "")
        except ValueError as e:
            problems.append(f"{where}: {e}")
            continue
        sk.where = where
        rest = by_root[r]
        if len(rest) > MAX_FILES:
            problems.append(f"{where}: {len(rest)} files; keeping the first {MAX_FILES}")
            rest = rest[:MAX_FILES]
        if sum(len(d) for _, d in rest) > MAX_SKILL_BYTES:
            problems.append(f"{where}: its files are over {MAX_SKILL_BYTES // 1_000_000} MB, so they were left out")
            rest = []
        sk.files = [_file_entry(rel, d) for rel, d in rest]
        if r and PurePosixPath(r).name != to_spec_name(sk.name) and PurePosixPath(r).name != sk.name:
            sk.notes.append(f"its folder is {PurePosixPath(r).name!r}")
        out.append(sk)
    return out, problems


# --------------------------------------------------------------------------- Git


def parse_git_url(url: str, branch: str = "", path: str = "") -> tuple[str, str, str]:
    """A repo URL, or a link to a folder in one (GitHub /tree/<branch>/<path>, Gitea
    /src/branch/<branch>/<path>, GitLab /-/tree/<branch>/<path>), as (clone URL, branch, path)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("give the repository's URL")
    if url.startswith(("git@", "ssh://", "file://")):
        return url, branch, path.strip("/")
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("use an https:// (or git@ / ssh://) repository URL")
    parts = [unquote(x) for x in u.path.strip("/").split("/") if x]
    for marker in (["-", "tree"], ["-", "blob"], ["tree"], ["blob"], ["src", "branch"]):
        n = len(marker)
        for i in range(2, len(parts) - n + 1):
            if parts[i:i + n] == marker and len(parts) > i + n:
                repo = "/".join(parts[:i])
                branch = branch or parts[i + n]
                sub = "/".join(parts[i + n + 1:])
                if sub.lower().endswith("skill.md"):
                    sub = sub.rsplit("/", 1)[0] if "/" in sub else ""
                return f"{u.scheme}://{u.netloc}/{repo}", branch, (path or sub).strip("/")
    return url, branch, path.strip("/")


def _read_checkout(repo: Path, sub: str) -> dict[str, bytes]:
    base = (repo / sub).resolve() if sub else repo.resolve()
    if not str(base).startswith(str(repo.resolve())) or not base.exists():
        raise ValueError(f"the repository has no {sub!r}")
    tree, total = {}, 0
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(base).as_posix()
        p = _clean_path(rel)
        if not p:
            continue
        total += f.stat().st_size
        if total > MAX_UNPACKED:
            raise ValueError(f"more than {MAX_UNPACKED // 1_000_000} MB under that path; point at a smaller folder")
        tree[p] = f.read_bytes()
    return tree


async def from_git(url: str, *, branch: str = "", path: str = "", username: str = "",
                   token: str = "") -> tuple[list[Parsed], list[str], str]:
    """Clone a repository (shallow, into a temporary folder) and read its skills.
    Returns (skills, problems, origin)."""
    from .knowledge import _git

    clone_url, branch, sub = parse_git_url(url, branch, path)
    src = SimpleNamespace(token=token or None, username=username or None)
    tmp = Path(tempfile.mkdtemp(prefix="ahq-skills-"))
    loop = asyncio.get_running_loop()
    try:
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags"] + (["--branch", branch] if branch else [])
        await loop.run_in_executor(None, lambda: _git(src, *args, clone_url, str(tmp / "repo"), timeout=300))
        tree = await loop.run_in_executor(None, lambda: _read_checkout(tmp / "repo", sub))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    found, problems = from_tree(tree)
    if not found and not problems:
        problems.append("no SKILL.md found" + (f" under {sub}" if sub else ""))
    origin = clone_url + (f"@{branch}" if branch else "") + (f":{sub}" if sub else "")
    for sk in found:
        sk.where = f"{sub}/{sk.where}" if sub else sk.where
    return found, problems, origin


# --------------------------------------------------------------------------- proposals


def _same(k: Skill, sk: Parsed) -> bool:
    return (k.title == sk.title.strip() and k.when_to_use == sk.when_to_use.strip()
            and k.body == sk.body.strip() and list(k.tools or []) == sk.tools
            and (k.script or None) == (sk.script or None)
            and dict(k.frontmatter or {}) == sk.frontmatter
            and sorted(map(_key, k.files or [])) == sorted(map(_key, sk.files)))


def _key(f: dict) -> tuple:
    return f.get("path", ""), f.get("encoding", "utf-8"), f.get("content", "")


async def propose(items: list[Parsed], *, origin: str, scope: str = "team",
                  agent_id: str | None = None, by: str = "you") -> list[dict[str, Any]]:
    """Each skill becomes a proposal (never applied straight away); its evals run as for any
    skill change. A skill that's already here and identical is skipped."""
    from . import evals, subagents

    results = []
    for sk in items:
        res: dict[str, Any] = {"name": sk.name, "title": sk.title, "where": sk.where, "notes": sk.notes}
        why = skills.check_fields(sk.name, sk.title, sk.when_to_use, sk.body)
        if why:
            results.append({**res, "status": "refused", "summary": why})
            continue
        base = skills.find(sk.name)
        if base is not None and _same(base, sk):
            results.append({**res, "status": "unchanged", "skill_id": base.id,
                            "summary": f"{sk.name} is already here, unchanged"
                                       + (" (archived)" if base.status in skills.ARCHIVED else "")})
            continue
        with db.session() as s:
            waiting = s.exec(select(SkillProposal).where(
                SkillProposal.name == sk.name, SkillProposal.source == "import",
                SkillProposal.status.in_(("pending", "testing")))).first()
        if waiting is not None:
            results.append({**res, "status": "refused", "proposal_id": waiting.id,
                            "summary": f"an import of {sk.name} is already waiting for you"})
            continue
        own = scope == "agent" and agent_id
        p = SkillProposal(skill_id=base.id if base else None, name=sk.name, title=sk.title,
                          when_to_use=sk.when_to_use, body=sk.body, tools=sk.tools, script=sk.script,
                          files=sk.files, frontmatter=sk.frontmatter,
                          scope=(base.scope if base else ("agent" if own else "team")),
                          reason=f"Imported from {origin}" + (f" ({sk.where})" if sk.where else "")
                                 + (f" by {by}" if by else "") + "."
                                 + (" " + "; ".join(sk.notes) + "." if sk.notes else ""),
                          source="import", origin=origin,
                          agent_id=(base.agent_id or "") if base else (agent_id if own else ""),
                          agent_name="Import", base_version=base.version if base else None)
        with db.session() as s:
            s.add(p)
            s.commit()
            s.refresh(p)
        cases = evals.cases_for_change(p) if evals.get_settings()["on_proposals"] else []
        if cases:
            evals.queue_proposal_run(p, cases)
        await subagents.notify({"type": "skill_proposal", "proposal_id": p.id, "agent": "Import",
                                "skill": p.name, "title": p.title, "revision": bool(base),
                                "reason": p.reason,
                                "link": subagents.link(f"/requests/skills?skill={p.id}")})
        results.append({**res, "status": "proposed", "proposal_id": p.id, "revision": bool(base),
                        "tests": len(cases),
                        "summary": ("an update to " if base else "a new skill: ") + sk.name})
    return results
