"""The knowledge library: your documents, notes and runbooks, searchable by agents.

Collections hold documents from sources:

* **upload**: files you add in the UI (PDF, Word, PowerPoint, Excel, HTML, Markdown, text);
* **url**: a web page, re-read on a schedule if you like;
* **git**: a repository, cloned read-only and pulled on a schedule; only changed files
  are re-indexed. With "Obsidian vault" on, notes are read the Obsidian way: frontmatter
  and #tags become tags, [[wikilinks]] are kept as links, %%comments%% are dropped, and
  citations open the note in Obsidian (obsidian://).

Documents are turned into Markdown, cut into sections at their headings and embedded with
the embedding model from Settings → Memory. Search is hybrid (meaning + keywords), as for
memories. Agents get matching excerpts automatically (collections with auto-recall on)
and can search and read documents with tools; answers cite the document they came from.

Access is per collection: pick the agents that may read it, or none for everyone. Sub-agents
read what their parent may read.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

from sqlalchemy import text as sql
from sqlmodel import delete, func, select, update

from . import db, memory
from .config import settings
from .models import (
    AgentDef,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeDoc,
    KnowledgeSource,
    SystemSetting,
)

log = logging.getLogger("agent_hq.knowledge")

DEFAULTS: dict[str, Any] = {
    "recall": True,          # matching excerpts come up by themselves
    "recall_count": 3,
    "min_score": 0.45,       # recall threshold (search shows everything)
    "chunk_chars": 2400,     # target section size
    "max_file_mb": 20,
}

TEXT_EXT = {".md", ".markdown", ".txt", ".text", ".rst", ".org", ".csv", ".json", ".yaml", ".yml", ".log"}
CONVERT_EXT = {".pdf", ".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm", ".epub"}
SUPPORTED_EXT = TEXT_EXT | CONVERT_EXT

DEFAULT_INCLUDE = ["**/*.md", "**/*.txt", "**/*.pdf"]
DEFAULT_EXCLUDE = [".obsidian/**", ".trash/**", ".git/**", "**/node_modules/**"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "knowledge")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    merged["recall_count"] = max(0, min(10, int(merged["recall_count"])))
    merged["min_score"] = max(0.0, min(1.0, float(merged["min_score"])))
    merged["chunk_chars"] = max(500, min(8000, int(merged["chunk_chars"])))
    merged["max_file_mb"] = max(1, min(200, int(merged["max_file_mb"])))
    with db.session() as s:
        row = s.get(SystemSetting, "knowledge") or SystemSetting(key="knowledge")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


def root() -> Path:
    p = settings.data_dir / "knowledge"
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- storage

_pgvector: bool | None = None


def init_storage() -> None:
    """On Postgres: a vector column and a full-text index for the chunks."""
    global _pgvector
    if not db.engine().url.get_backend_name().startswith("postgresql") or not memory.pgvector_enabled():
        _pgvector = False
        return
    try:
        with db.engine().begin() as c:
            c.execute(sql("ALTER TABLE knowledgechunk ADD COLUMN IF NOT EXISTS embedding_vec vector"))
            c.execute(sql("CREATE INDEX IF NOT EXISTS knowledgechunk_fts ON knowledgechunk "
                          "USING gin (to_tsvector('english', heading || ' ' || text))"))
        _pgvector = True
    except Exception as e:
        log.warning("knowledge: pgvector not available (%s); search falls back to Python", e)
        _pgvector = False


def pgvector_enabled() -> bool:
    if _pgvector is None:
        init_storage()
    return bool(_pgvector)


# --------------------------------------------------------------------------- text


def to_markdown(name: str, data: bytes) -> str:
    """A file as Markdown text (PDF, Office and HTML via markitdown)."""
    ext = PurePosixPath(name.lower()).suffix
    if ext in TEXT_EXT or not ext:
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
    if ext in CONVERT_EXT:
        try:
            from markitdown import MarkItDown
        except ImportError as e:  # pragma: no cover - installed with the app
            raise RuntimeError("markitdown is not installed") from e
        result = MarkItDown(enable_plugins=False).convert_stream(io.BytesIO(data), file_extension=ext)
        return (result.text_content or "").strip()
    raise ValueError(f"unsupported file type: {ext or name}")


_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_COMMENT = re.compile(r"%%.*?%%", re.S)
_EMBED = re.compile(r"!\[\[([^\]]+)\]\]")
_WIKILINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|([^\]]*))?\]\]")
_TAG = re.compile(r"(?<![\w/#&])#([A-Za-z][\w/-]*)")
_FENCE = re.compile(r"^(```|~~~)")


@dataclass
class Note:
    title: str
    text: str
    tags: list[str]
    links: list[str]


def parse_note(path: str, raw: str, *, obsidian: bool) -> Note:
    """Title, clean text, tags and links of a Markdown note."""
    title = PurePosixPath(path).stem if path else ""
    tags: list[str] = []
    links: list[str] = []
    body = raw.replace("\r\n", "\n")
    if obsidian or body.startswith("---"):
        m = _FRONTMATTER.match(body)
        if m:
            try:
                import yaml

                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:
                meta = {}
            if isinstance(meta, dict):
                title = str(meta.get("title") or title)
                fm_tags = meta.get("tags") or meta.get("tag") or []
                if isinstance(fm_tags, str):
                    fm_tags = re.split(r"[,\s]+", fm_tags)
                tags += [str(t).lstrip("#") for t in fm_tags if str(t).strip()]
                aliases = meta.get("aliases") or []
                if isinstance(aliases, list) and aliases:
                    body = body[m.end():]
                    body = f"(Also known as: {', '.join(map(str, aliases))})\n\n" + body
                else:
                    body = body[m.end():]
            else:
                body = body[m.end():]
    if obsidian:
        body = _COMMENT.sub("", body)
        body = _EMBED.sub(lambda m: f"[embedded: {m.group(1)}]", body)

        def link(m: re.Match) -> str:
            target = m.group(1).strip()
            links.append(target)
            return (m.group(2) or target).strip()

        body = _WIKILINK.sub(link, body)
        in_code = False
        for line in body.splitlines():
            if _FENCE.match(line.strip()):
                in_code = not in_code
            elif not in_code and not line.lstrip().startswith("#"):
                tags += _TAG.findall(line)
            elif not in_code:  # a heading line can still carry #tags after the text
                tags += _TAG.findall(line.lstrip("# "))
    if not obsidian or not title:
        h1 = re.search(r"^# (.+)$", body, re.M)
        if h1 and not obsidian:
            title = h1.group(1).strip() or title
    return Note(title=title or path, text=body.strip(),
                tags=sorted(set(t for t in tags if t)), links=sorted(set(links)))


def chunk_markdown(text: str, max_chars: int = 2400) -> list[tuple[str, str]]:
    """Sections at headings, split further by paragraph when long, small ones merged.
    Returns (heading path, text) pairs."""
    sections: list[tuple[list[str], list[str]]] = []
    stack: list[tuple[int, str]] = []
    cur: list[str] = []
    in_code = False

    def flush():
        if any(line.strip() for line in cur):
            sections.append(([h for _, h in stack], cur.copy()))
        cur.clear()

    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            in_code = not in_code
        m = None if in_code else re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level, head = len(m.group(1)), m.group(2).strip().rstrip("#").strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, head))
            continue
        cur.append(line)
    flush()

    out: list[tuple[str, str]] = []
    for heads, lines in sections:
        heading = " › ".join(heads)
        body = "\n".join(lines).strip()
        if len(body) <= max_chars:
            out.append((heading, body))
            continue
        piece = ""
        for para in re.split(r"\n\s*\n", body):
            para = para.strip()
            while len(para) > max_chars:  # one enormous paragraph (a PDF page, a table)
                cut = para.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                if piece:
                    out.append((heading, piece))
                    piece = ""
                out.append((heading, para[:cut].strip()))
                para = para[cut:].strip()
            if piece and len(piece) + len(para) + 2 > max_chars:
                out.append((heading, piece))
                piece = ""
            piece = f"{piece}\n\n{para}" if piece else para
        if piece:
            out.append((heading, piece))
    # Merge tiny neighbours: a short block before the first heading goes into the next
    # section; a short subsection joins its parent section.
    merged: list[tuple[str, str]] = []
    preamble = ""
    for heading, body in out:
        if not heading and len(body) < 300 and not merged:
            preamble = f"{preamble}\n\n{body}".strip()
            continue
        if preamble:
            body, preamble = f"{preamble}\n\n{body}", ""
        if merged and merged[-1][0] and len(body) < 300 and len(merged[-1][1]) + len(body) < max_chars \
                and heading.startswith(merged[-1][0]):
            prev_h, prev_b = merged[-1]
            sub = heading[len(prev_h):].lstrip(" ›")
            merged[-1] = (prev_h, prev_b + (f"\n\n**{sub}**\n" if sub else "\n\n") + body)
        else:
            merged.append((heading, body))
    if preamble:
        merged.append(("", preamble))
    return [(h, b) for h, b in merged if b.strip()]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:32]


# --------------------------------------------------------------------------- documents


def upsert_doc(src: KnowledgeSource, path: str, *, title: str, text: str, link: str = "",
               tags: list[str] | None = None, links: list[str] | None = None,
               size: int = 0, stored_file: str | None = None) -> tuple[KnowledgeDoc, bool]:
    """Create or update a document; returns (doc, changed)."""
    h = _hash(f"{title}\n{text}")
    with db.session() as s:
        doc = s.exec(select(KnowledgeDoc).where(KnowledgeDoc.source_id == src.id,
                                                KnowledgeDoc.path == path)).first()
        changed = doc is None or doc.hash != h
        doc = doc or KnowledgeDoc(source_id=src.id, collection_id=src.collection_id, path=path)
        doc.title, doc.text, doc.link = title[:300], text, link
        doc.tags, doc.links = tags or [], links or []
        doc.size, doc.hash = size or len(text.encode()), h
        if stored_file:
            doc.stored_file = stored_file
        if changed:
            doc.updated_at = _now()
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return doc, changed


def remove_docs(doc_ids: list[str]) -> None:
    if not doc_ids:
        return
    with db.session() as s:
        for d in s.exec(select(KnowledgeDoc).where(KnowledgeDoc.id.in_(doc_ids))).all():
            if d.stored_file:
                try:
                    (root() / d.stored_file).unlink(missing_ok=True)
                except OSError:
                    pass
        s.exec(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id.in_(doc_ids)))
        s.exec(delete(KnowledgeDoc).where(KnowledgeDoc.id.in_(doc_ids)))
        s.commit()


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


async def index_doc(doc_id: str, *, force: bool = False) -> int:
    """(Re)build a document's chunks and their embeddings. Returns the chunk count."""
    with db.session() as s:
        doc = s.get(KnowledgeDoc, doc_id)
    if doc is None:
        return 0
    if doc.indexed_hash == doc.hash and not force:
        return doc.chunks
    cfg = get_settings()
    parts = chunk_markdown(doc.text, int(cfg["chunk_chars"]))
    vectors, model = None, None
    if parts:
        try:
            inputs = [f"{doc.title}" + (f" › {h}" if h else "") + f"\n{b}" for h, b in parts]
            vectors = []
            for i in range(0, len(inputs), 32):
                got = await memory.embed_texts(inputs[i:i + 32])
                if not got:
                    vectors = None
                    break
                vectors += got[0]
                model = got[1]
        except Exception as e:
            log.warning("knowledge: embedding %s failed (%s); keyword search only", doc.path, e)
            vectors = None
    rows = [KnowledgeChunk(doc_id=doc.id, source_id=doc.source_id, collection_id=doc.collection_id,
                           ord=i, heading=h[:500], text=b, embedding_model=model if vectors else None,
                           embedding=(vectors[i] if vectors and not pgvector_enabled() else None))
            for i, (h, b) in enumerate(parts)]
    with db.session() as s:
        s.exec(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.id))
        for r in rows:
            s.add(r)
        d = s.get(KnowledgeDoc, doc.id)
        d.chunks, d.indexed_hash = len(rows), doc.hash
        s.add(d)
        s.commit()
    if vectors and pgvector_enabled():
        with db.engine().begin() as c:
            for r, v in zip(rows, vectors):
                c.execute(sql("UPDATE knowledgechunk SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
                          {"v": _vec_literal(v), "id": r.id})
    return len(rows)


# --------------------------------------------------------------------------- sources


def matches(path: str, include: list[str], exclude: list[str]) -> bool:
    p = PurePosixPath(path)
    if any(p.full_match(x) for x in exclude):
        return False
    return any(p.full_match(x) for x in (include or DEFAULT_INCLUDE))


def ca_bundle() -> str | None:
    """System CAs plus your own root (AHQ_EXTRA_CA_FILE, e.g. your step-ca root), so your
    Gitea and internal sites over HTTPS are trusted. None when there's nothing extra."""
    extra = os.environ.get("AHQ_EXTRA_CA_FILE", "")
    if not extra or not Path(extra).is_file():
        return None
    out = root() / "ca-bundle.pem"
    system = next((Path(p) for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
                   if Path(p).is_file()), None)
    try:
        import certifi

        system = system or Path(certifi.where())
    except ImportError:
        pass
    data = (system.read_bytes() if system else b"") + b"\n" + Path(extra).read_bytes()
    if not out.exists() or out.read_bytes() != data:
        out.write_bytes(data)
    return str(out)


def _redact(text: str, src: KnowledgeSource) -> str:
    if src.token:
        text = text.replace(src.token, "•••")
    return text


def _git(src: KnowledgeSource, *args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    cmd = ["git"]
    if src.token:
        cred = base64.b64encode(f"{src.username or 'git'}:{src.token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {cred}"]
    ca = ca_bundle()
    if ca:
        cmd += ["-c", f"http.sslCAInfo={ca}"]
    cmd += list(args)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode != 0:
        raise RuntimeError(_redact((r.stderr or r.stdout).strip()[-1500:], src) or f"git {args[0]} failed")
    return r.stdout


def _repo_web(url: str, branch: str, path: str) -> str:
    base = url[:-4] if url.endswith(".git") else url
    if base.startswith("http"):
        return f"{base}/src/branch/{quote(branch)}/{quote(path)}"
    return ""


def _repo_name(url: str) -> str:
    name = PurePosixPath(urlparse(url).path).name
    return name[:-4] if name.endswith(".git") else name


def _obsidian_link(vault: str, path: str) -> str:
    stem = path[:-3] if path.lower().endswith(".md") else path
    return f"obsidian://open?vault={quote(vault)}&file={quote(stem)}"


async def _collect_git(src: KnowledgeSource, full: bool) -> list[str]:
    """Clone or pull; read changed files into documents. Returns the doc ids that changed."""
    repo = root() / "git" / src.id
    loop = asyncio.get_running_loop()
    fresh = not (repo / ".git").exists()
    if fresh:
        if repo.exists():
            shutil.rmtree(repo)
        repo.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--single-branch", "--no-tags"] + (["--branch", src.branch] if src.branch else [])
        await loop.run_in_executor(None, lambda: _git(src, *args, src.url, str(repo)))
    else:
        branch = src.branch or (await loop.run_in_executor(
            None, lambda: _git(src, "rev-parse", "--abbrev-ref", "HEAD", cwd=repo))).strip()
        await loop.run_in_executor(None, lambda: _git(src, "fetch", "--no-tags", "origin", branch, cwd=repo))
        await loop.run_in_executor(None, lambda: _git(src, "reset", "--hard", "FETCH_HEAD", cwd=repo))
    head = (await loop.run_in_executor(None, lambda: _git(src, "rev-parse", "HEAD", cwd=repo))).strip()
    branch = src.branch or (await loop.run_in_executor(
        None, lambda: _git(src, "rev-parse", "--abbrev-ref", "HEAD", cwd=repo))).strip()

    all_files = [p for p in (await loop.run_in_executor(
        None, lambda: _git(src, "ls-files", "-z", cwd=repo))).split("\0") if p]
    wanted = {p for p in all_files if matches(p, src.include, src.exclude or DEFAULT_EXCLUDE)}
    with db.session() as s:
        known = {d.path: d.id for d in s.exec(select(KnowledgeDoc).where(KnowledgeDoc.source_id == src.id)).all()}

    if full or fresh or not src.last_commit or src.last_commit == head and not known:
        todo = wanted
    elif src.last_commit == head:
        todo = wanted - set(known)
    else:
        try:
            diff = await loop.run_in_executor(None, lambda: _git(
                src, "diff", "--name-only", "-z", src.last_commit, head, cwd=repo))
            todo = {p for p in diff.split("\0") if p} & wanted | (wanted - set(known))
        except RuntimeError:  # the old commit is gone (force push): read everything
            todo = wanted
    remove_docs([i for p, i in known.items() if p not in wanted])

    vault = src.vault_name or _repo_name(src.url or "") or "vault"
    max_bytes = int(get_settings()["max_file_mb"]) * 1024 * 1024
    changed = []
    for path in sorted(todo):
        f = repo / path
        try:
            if not f.is_file() or f.stat().st_size > max_bytes:
                continue
            data = f.read_bytes()
            raw = await loop.run_in_executor(None, lambda: to_markdown(path, data))
        except Exception as e:
            log.info("knowledge: skipped %s (%s)", path, e)
            continue
        if path.lower().endswith((".md", ".markdown")):
            note = parse_note(path, raw, obsidian=src.obsidian)
        else:
            note = Note(title=PurePosixPath(path).stem, text=raw, tags=[], links=[])
        link = _obsidian_link(vault, path) if src.obsidian else _repo_web(src.url or "", branch, path)
        doc, did = upsert_doc(src, path, title=note.title, text=note.text, link=link,
                              tags=note.tags, links=note.links, size=len(data))
        if did:
            changed.append(doc.id)
    with db.session() as s:
        row = s.get(KnowledgeSource, src.id)
        row.last_commit = head
        if not row.branch:
            row.branch = branch
        s.add(row)
        s.commit()
    return changed


async def _collect_url(src: KnowledgeSource) -> list[str]:
    import httpx

    ca = ca_bundle()
    async with httpx.AsyncClient(follow_redirects=True, timeout=30, verify=ca or True,
                                 headers={"User-Agent": "AgentHQ-knowledge/1.0"}) as c:
        r = await c.get(src.url)
        r.raise_for_status()
        data = r.content[: int(get_settings()["max_file_mb"]) * 1024 * 1024]
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(ctype, ".html")
    name = PurePosixPath(urlparse(str(r.url)).path).name or "page"
    if not PurePosixPath(name).suffix:
        name += ext
    text = await asyncio.get_running_loop().run_in_executor(None, lambda: to_markdown(name, data))
    title = name
    if ext == ".html":
        m = re.search(rb"<title[^>]*>(.*?)</title>", data, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1).decode("utf-8", "ignore")).strip()[:200] or name
    if not src.name or src.name == src.url:
        _set(src.id, name=title)
    doc, did = upsert_doc(src, src.url, title=title, text=text, link=src.url, size=len(data))
    return [doc.id] if did else []


def _title_of(name: str, data: bytes, text: str) -> str:
    """An HTML page's <title>, else the first heading, else the file name."""
    if name.lower().endswith((".html", ".htm")):
        m = re.search(rb"<title[^>]*>(.*?)</title>", data[:200000], re.I | re.S)
        if m:
            t = re.sub(r"\s+", " ", m.group(1).decode("utf-8", "ignore")).strip()
            if t:
                return t[:200]
    h1 = re.search(r"^# (.+)$", text[:5000], re.M)
    return h1.group(1).strip()[:200] if h1 else PurePosixPath(name).stem


def add_upload(src: KnowledgeSource, filename: str, data: bytes) -> KnowledgeDoc:
    """Store an uploaded file and its text (indexing happens in the background)."""
    name = PurePosixPath(filename.replace("\\", "/")).name or "file"
    if PurePosixPath(name.lower()).suffix not in SUPPORTED_EXT:
        raise ValueError(f"{name}: unsupported type (try PDF, Word, PowerPoint, Excel, HTML, Markdown or text)")
    if len(data) > int(get_settings()["max_file_mb"]) * 1024 * 1024:
        raise ValueError(f"{name}: larger than the {get_settings()['max_file_mb']} MB limit")
    text = to_markdown(name, data)
    if not text.strip():
        raise ValueError(f"{name}: no text found (a scanned PDF needs OCR first)")
    folder = root() / "uploads" / src.id
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"uploads/{src.id}/{hashlib.sha1(name.encode()).hexdigest()[:8]}-{name}"
    (root() / stored).write_bytes(data)
    if name.lower().endswith((".md", ".markdown")):
        note = parse_note(name, text, obsidian=False)
    else:
        note = Note(title=_title_of(name, data, text), text=text, tags=[], links=[])
    doc, _ = upsert_doc(src, name, title=note.title, text=note.text, tags=note.tags,
                        size=len(data), stored_file=stored)
    return doc


def _set(src_id: str, **fields: Any) -> None:
    with db.session() as s:
        row = s.get(KnowledgeSource, src_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        s.add(row)
        s.commit()


def _counts(src_id: str) -> tuple[int, int]:
    with db.session() as s:
        docs = s.exec(select(func.count()).select_from(KnowledgeDoc).where(KnowledgeDoc.source_id == src_id)).one()
        chunks = s.exec(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.source_id == src_id)).one()
    return docs, chunks


def claim(src_id: str) -> bool:
    with db.session() as s:
        res = s.exec(update(KnowledgeSource).where(KnowledgeSource.id == src_id,
                                                   KnowledgeSource.status != "indexing")
                     .values(status="indexing", error=None))
        s.commit()
        return (res.rowcount or 0) == 1


async def index_source(src_id: str, *, full: bool = False) -> KnowledgeSource:
    """Collect a source's documents and index what changed. Call after claim()."""
    with db.session() as s:
        src = s.get(KnowledgeSource, src_id)
    if src is None:
        raise LookupError(src_id)
    try:
        if src.kind == "git":
            await _collect_git(src, full)
        elif src.kind == "url":
            await _collect_url(src)
        with db.session() as s:
            ids = [d.id for d in s.exec(select(KnowledgeDoc).where(KnowledgeDoc.source_id == src_id)).all()
                   if full or d.indexed_hash != d.hash]
        for i in ids:
            await index_doc(i, force=full)
        docs, chunks = _counts(src_id)
        _set(src_id, status="ready", error=None, last_indexed_at=_now(), docs=docs, chunks=chunks)
    except Exception as e:
        log.warning("knowledge: indexing %s failed: %s", src.name or src.url, e)
        docs, chunks = _counts(src_id)
        _set(src_id, status="error", error=_redact(f"{type(e).__name__}: {e}", src)[:2000],
             last_indexed_at=_now(), docs=docs, chunks=chunks)
    with db.session() as s:
        return s.get(KnowledgeSource, src_id)


def delete_source(src_id: str) -> None:
    with db.session() as s:
        ids = [d.id for d in s.exec(select(KnowledgeDoc).where(KnowledgeDoc.source_id == src_id)).all()]
    remove_docs(ids)
    for folder in (root() / "git" / src_id, root() / "uploads" / src_id):
        shutil.rmtree(folder, ignore_errors=True)
    with db.session() as s:
        row = s.get(KnowledgeSource, src_id)
        if row:
            s.delete(row)
            s.commit()


def delete_collection(collection_id: str) -> None:
    with db.session() as s:
        ids = [x.id for x in s.exec(select(KnowledgeSource).where(KnowledgeSource.collection_id == collection_id)).all()]
    for i in ids:
        delete_source(i)
    with db.session() as s:
        row = s.get(KnowledgeCollection, collection_id)
        if row:
            s.delete(row)
            s.commit()


def due_sources() -> list[KnowledgeSource]:
    now = _now()
    with db.session() as s:
        rows = list(s.exec(select(KnowledgeSource).where(KnowledgeSource.status != "indexing")).all())
    out = []
    for r in rows:
        if r.status == "queued":
            out.append(r)
        elif r.kind in ("git", "url") and r.refresh_minutes > 0 and (
                r.last_indexed_at is None
                or _aware(r.last_indexed_at) + timedelta(minutes=r.refresh_minutes) <= now):
            out.append(r)
    return out


async def run_due() -> list[KnowledgeSource]:
    done = []
    for src in due_sources():
        if claim(src.id):
            done.append(await index_source(src.id))
    return done


def recover_stuck(minutes: int = 60) -> None:
    """Sources left 'indexing' by a restart are queued again."""
    with db.session() as s:
        rows = s.exec(select(KnowledgeSource).where(KnowledgeSource.status == "indexing")).all()
        cutoff = _now() - timedelta(minutes=minutes)
        for r in rows:
            if r.last_indexed_at is None or _aware(r.last_indexed_at) < cutoff:
                r.status = "queued"
                s.add(r)
        s.commit()


async def loop(broker, every: float = 60) -> None:
    """Background: index queued sources and refresh git and web sources on schedule."""
    recover_stuck(0)
    while True:
        try:
            if await broker.r.set("ahq:knowledge:indexer", "1", nx=True, ex=3600):
                try:
                    await run_due()
                finally:
                    await broker.r.delete("ahq:knowledge:indexer")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("knowledge indexer failed")
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- access & search


def readable_collections(agent: AgentDef | None, *, recall_only: bool = False) -> list[KnowledgeCollection]:
    """Collections an agent may read (None = all, for the UI)."""
    with db.session() as s:
        rows = list(s.exec(select(KnowledgeCollection).where(KnowledgeCollection.enabled == True)).all())  # noqa: E712
    if agent is None:
        return [c for c in rows if c.auto_recall or not recall_only]
    who = {agent.id} | ({agent.parent_agent_id} if agent.is_subagent and agent.parent_agent_id else set())
    return [c for c in rows if (not c.agent_ids or who & set(c.agent_ids))
            and (c.auto_recall or not recall_only)]


def has_knowledge(agent: AgentDef) -> bool:
    ids = [c.id for c in readable_collections(agent)]
    if not ids:
        return False
    with db.session() as s:
        return s.exec(select(func.count()).select_from(KnowledgeChunk)
                      .where(KnowledgeChunk.collection_id.in_(ids))).one() > 0


@dataclass
class Hit:
    chunk: KnowledgeChunk
    doc: KnowledgeDoc
    collection: str
    score: float
    sim: float | None
    keyword: float

    def ref(self) -> str:
        return f"{self.doc.title}" + (f" › {self.chunk.heading}" if self.chunk.heading else "")

    def out(self, clip: int = 1500) -> dict[str, Any]:
        t = self.chunk.text
        return {"doc_id": self.doc.id, "title": self.doc.title, "section": self.chunk.heading,
                "collection": self.collection, "path": self.doc.path, "link": self.doc.link,
                "tags": self.doc.tags, "score": round(self.score, 3),
                "text": t if len(t) <= clip else t[:clip] + " …"}


async def search(query: str, *, collection_ids: list[str], k: int = 6, min_score: float = 0.0,
                 tags: list[str] | None = None) -> list[Hit]:
    """Hybrid search over the chunks of the given collections."""
    if not collection_ids or not query.strip():
        return []
    qvec, model = None, None
    try:
        got = await memory.embed_texts([query], is_query=True)
        if got:
            qvec, model = got[0][0], got[1]
    except Exception as e:
        log.warning("knowledge: query embedding failed (%s); keyword search only", e)
    q_tokens = memory.tokens(query)
    cands: dict[str, float | None] = {}
    if pgvector_enabled():
        params: dict[str, Any] = {f"c{i}": c for i, c in enumerate(collection_ids)}
        inc = "collection_id IN (" + ", ".join(f":c{i}" for i in range(len(collection_ids))) + ")"
        with db.engine().connect() as c:
            if qvec is not None:
                rows = c.execute(sql(
                    f"SELECT id, 1 - (embedding_vec <=> CAST(:v AS vector)) FROM knowledgechunk "
                    f"WHERE embedding_vec IS NOT NULL AND embedding_model = :m AND {inc} "
                    f"ORDER BY embedding_vec <=> CAST(:v AS vector) LIMIT 80"),
                    {**params, "v": _vec_literal(qvec), "m": model}).fetchall()
                cands.update({r[0]: float(r[1]) for r in rows})
            words = " OR ".join(sorted(q_tokens)) or query
            for r in c.execute(sql(
                    f"SELECT id FROM knowledgechunk WHERE to_tsvector('english', heading || ' ' || text) @@ "
                    f"websearch_to_tsquery('english', :q) AND {inc} LIMIT 80"), {**params, "q": words}).fetchall():
                cands.setdefault(r[0], None)
            if q_tokens:
                like = " OR ".join(f"text ILIKE :t{i}" for i in range(len(q_tokens)))
                for r in c.execute(sql(f"SELECT id FROM knowledgechunk WHERE ({like}) AND {inc} LIMIT 80"),
                                   {**params, **{f"t{i}": f"%{t}%" for i, t in enumerate(sorted(q_tokens))}}).fetchall():
                    cands.setdefault(r[0], None)
        with db.session() as s:
            chunks = list(s.exec(select(KnowledgeChunk).where(KnowledgeChunk.id.in_(list(cands))))) if cands else []
    else:
        with db.session() as s:
            chunks = list(s.exec(select(KnowledgeChunk).where(KnowledgeChunk.collection_id.in_(collection_ids))
                                 .limit(20000)).all())
        for ch in chunks:
            cands[ch.id] = (memory.cosine(qvec, ch.embedding)
                            if qvec is not None and ch.embedding and ch.embedding_model == model else None)
    if not chunks:
        return []
    with db.session() as s:
        docs = {d.id: d for d in s.exec(select(KnowledgeDoc).where(
            KnowledgeDoc.id.in_(list({c.doc_id for c in chunks})))).all()}
        names = {c.id: c.name for c in s.exec(select(KnowledgeCollection)).all()}
    want_tags = {t.lstrip("#").lower() for t in (tags or []) if t.strip()}
    hits = []
    for ch in chunks:
        doc = docs.get(ch.doc_id)
        if doc is None:
            continue
        if want_tags and not want_tags & {t.lower() for t in doc.tags}:
            continue
        sim = cands.get(ch.id)
        kw = memory.keyword_score(q_tokens, f"{doc.title} {ch.heading} {ch.text}")
        if qvec is not None:
            score = 0.7 * max(sim or 0.0, 0.0) + 0.3 * kw
        else:
            score = 0.85 * kw
        if score >= min_score and (score > 0 or not query.strip()):
            hits.append(Hit(ch, doc, names.get(ch.collection_id, ""), score, sim, kw))
    hits.sort(key=lambda h: h.score, reverse=True)
    # At most two sections from one document, so one long note doesn't crowd out the rest.
    per_doc: dict[str, int] = {}
    out = []
    for h in hits:
        if per_doc.get(h.doc.id, 0) >= 2:
            continue
        per_doc[h.doc.id] = per_doc.get(h.doc.id, 0) + 1
        out.append(h)
        if len(out) >= k:
            break
    return out


def format_recall(hits: list[Hit]) -> str:
    if not hits:
        return ""
    parts = []
    for i, h in enumerate(hits, 1):
        text = h.chunk.text if len(h.chunk.text) <= 900 else h.chunk.text[:900] + " …"
        parts.append(f"[{i}] {h.ref()} ({h.collection}; doc {h.doc.id})"
                     + (f" {h.doc.link}" if h.doc.link else "") + f"\n{text}")
    return ("Excerpts from the knowledge library that may help (the user's own documents and "
            "notes). When you use one, say which document it came from. Use search_knowledge "
            "for more, read_document for a whole document:\n\n" + "\n\n".join(parts))


async def recall_for(agent: AgentDef, query: str) -> str:
    cfg = get_settings()
    if not cfg["recall"] or not cfg["recall_count"] or not query.strip():
        return ""
    ids = [c.id for c in readable_collections(agent, recall_only=True)]
    hits = await search(query[:2000], collection_ids=ids, k=int(cfg["recall_count"]),
                        min_score=float(cfg["min_score"]))
    return format_recall(hits)


def find_doc(ref: str, agent: AgentDef) -> KnowledgeDoc | None:
    """A document by id, path or title, among those the agent may read."""
    ids = [c.id for c in readable_collections(agent)]
    if not ids:
        return None
    ref = ref.strip()
    with db.session() as s:
        d = s.get(KnowledgeDoc, ref)
        if d and d.collection_id in ids:
            return d
        q = select(KnowledgeDoc).where(KnowledgeDoc.collection_id.in_(ids))
        for cond in (KnowledgeDoc.path == ref, KnowledgeDoc.title == ref,
                     KnowledgeDoc.path == ref + ".md", func.lower(KnowledgeDoc.title) == ref.lower()):
            d = s.exec(q.where(cond)).first()
            if d:
                return d
    return None


KNOWLEDGE_RULES = """\
Knowledge library: the user's own documents and notes (runbooks, references, their vault).
Search it with search_knowledge before guessing about their setup, and open a whole
document with read_document when an excerpt isn't enough. Say which document you used
(title and section); never invent what a document says."""
