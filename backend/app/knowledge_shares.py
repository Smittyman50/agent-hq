"""Network shares for the knowledge library: a NAS or file server share, read in place.

Two ways in:

* **SMB** (TrueNAS, Windows, Samba): read over the network with smbprotocol, so the
  container needs no mount and no extra privileges. Give the share as
  ``smb://truenas.home.arpa/docs/Runbooks`` or ``\\\\truenas\\docs\\Runbooks``, with a user
  that has read access to it.
* **A mounted folder:** anything mounted into the container under ``AHQ_SHARES_DIR``
  (``/shares`` by default), such as an NFS export added as a Docker volume. Give the
  path, e.g. ``/shares/docs``. Paths outside that folder are refused, so a source can't
  read the app's own data or the container's files.

Nothing is ever written to the share. Changes are found by size and modified time, so
only new or changed files are read again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import unquote, urlparse

# Office files and PDFs are common on a NAS; images and media are not text.
SHARE_INCLUDE = ["**/*.md", "**/*.markdown", "**/*.txt", "**/*.pdf", "**/*.docx", "**/*.pptx",
                 "**/*.xlsx", "**/*.html", "**/*.htm"]
# ZFS snapshots (.zfs), recycle bins and OS clutter would only duplicate or add noise.
SHARE_EXCLUDE = [".zfs/**", "**/.zfs/**", "#recycle/**", ".recycle/**", "$RECYCLE.BIN/**",
                 "**/.Trash-*/**", ".obsidian/**", ".trash/**", ".git/**", "**/node_modules/**",
                 "**/.DS_Store", "**/Thumbs.db", "**/desktop.ini", "**/~$*", "**/._*"]
MAX_FILES = 20000  # past this, narrow the share path or the include patterns


def shares_dir() -> Path:
    return Path(os.environ.get("AHQ_SHARES_DIR", "/shares"))


@dataclass(frozen=True)
class Location:
    protocol: str  # smb | folder
    server: str = ""
    port: int = 445
    share: str = ""
    subpath: str = ""   # inside the share, with /
    path: str = ""      # folder: the absolute path

    @property
    def unc(self) -> str:
        base = f"\\\\{self.server}\\{self.share}"
        return base + ("\\" + self.subpath.replace("/", "\\") if self.subpath else "")

    @property
    def label(self) -> str:
        """A default name: the last folder of the path."""
        if self.protocol == "smb":
            return PurePosixPath(self.subpath).name or self.share
        return PurePosixPath(self.path).name or self.path


def parse(url: str) -> Location:
    """smb://host[:port]/share[/path], \\\\host\\share[\\path], //host/share[/path], or /a/path."""
    u = (url or "").strip()
    if u.startswith("\\\\") or (u.startswith("//") and not u.startswith("///")):
        u = "smb://" + u.replace("\\", "/").lstrip("/")
    if u.lower().startswith("smb://"):
        p = urlparse(u)
        parts = [unquote(x) for x in p.path.split("/") if x]
        if not p.hostname or not parts:
            raise ValueError("an SMB share looks like smb://truenas.home.arpa/share or \\\\truenas\\share")
        if p.username or p.password:
            raise ValueError("put the user and password in their own fields, not in the address")
        return Location("smb", server=p.hostname, port=p.port or 445, share=parts[0],
                        subpath="/".join(parts[1:]))
    if u.startswith("/"):
        return Location("folder", path=os.path.normpath(u))
    raise ValueError("give an SMB share (smb://host/share or \\\\host\\share) "
                     f"or a folder mounted under {shares_dir()}")


def check_folder(path: str) -> Path:
    """A mounted folder, only if it's inside AHQ_SHARES_DIR."""
    base = shares_dir().resolve()
    real = Path(path).resolve()
    if real != base and base not in real.parents:
        raise ValueError(f"{path} is outside {base}; mount the share under {base} "
                         "(AHQ_SHARES_DIR) and use that path")
    if not real.is_dir():
        raise ValueError(f"{path} isn't a folder in the container (is the share mounted?)")
    return real


@dataclass(frozen=True)
class Entry:
    path: str     # relative, with /
    size: int
    mtime: float


def _pruned(rel_dir: str, exclude: list[str]) -> bool:
    """Skip a whole folder an exclude pattern like 'Journal/**' covers."""
    p = PurePosixPath(rel_dir)
    return any(x.endswith("/**") and p.full_match(x[:-3]) for x in exclude)


class FolderShare:
    def __init__(self, loc: Location):
        self.base = check_folder(loc.path)

    def walk(self, exclude: list[str]) -> Iterator[Entry]:
        stack = [""]
        while stack:
            rel = stack.pop()
            with os.scandir(self.base / rel if rel else self.base) as it:
                for e in it:
                    child = f"{rel}/{e.name}" if rel else e.name
                    if e.is_symlink():  # could point anywhere; not followed
                        continue
                    if e.is_dir():
                        if not _pruned(child, exclude):
                            stack.append(child)
                    elif e.is_file():
                        st = e.stat()
                        yield Entry(child, st.st_size, st.st_mtime)

    def read(self, rel: str) -> bytes:
        f = (self.base / rel).resolve()
        if self.base not in f.parents:
            raise ValueError(f"{rel} is outside the share")
        return f.read_bytes()

    def close(self) -> None:
        pass


class SmbShare:
    def __init__(self, loc: Location, username: str, password: str | None):
        try:
            import smbclient
        except ImportError as e:  # pragma: no cover - installed with the app
            raise RuntimeError("smbprotocol is not installed") from e
        self._smb = smbclient
        self.loc = loc
        self._cache: dict = {}
        self._kw = {"port": loc.port, "connection_cache": self._cache}
        smbclient.register_session(loc.server, username=username or None, password=password or None,
                                   port=loc.port, connection_cache=self._cache, connection_timeout=20)

    def _path(self, rel: str) -> str:
        return self.loc.unc + ("\\" + rel.replace("/", "\\") if rel else "")

    def walk(self, exclude: list[str]) -> Iterator[Entry]:
        stack = [""]
        while stack:
            rel = stack.pop()
            with self._smb.scandir(self._path(rel), **self._kw) as it:
                for e in it:
                    child = f"{rel}/{e.name}" if rel else e.name
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        if not _pruned(child, exclude):
                            stack.append(child)
                    elif e.is_file():
                        st = e.stat()
                        yield Entry(child, st.st_size, st.st_mtime)

    def read(self, rel: str) -> bytes:
        with self._smb.open_file(self._path(rel), mode="rb", **self._kw) as f:
            return f.read()

    def close(self) -> None:
        try:
            self._smb.reset_connection_cache(fail_on_error=False, connection_cache=self._cache)
        except Exception:
            pass


def open_share(url: str, username: str = "", password: str | None = None) -> FolderShare | SmbShare:
    loc = parse(url)
    if loc.protocol == "smb":
        return SmbShare(loc, username, password)
    return FolderShare(loc)
