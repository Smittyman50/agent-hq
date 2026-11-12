"""Shared plumbing for lab tool servers: the inventory, HTTP clients, settings, and keeping
answers short enough for small local models.

The inventory (LAB_INVENTORY, default /etc/labtools/inventory.yaml) says what exists and
where. It holds no secrets: those come from environment variables, set per server instance,
so a read-only instance and a read-write one get different credentials under the same
names (see docs/labtools.md).
"""

from __future__ import annotations

import os
import ssl
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml

READ_ONLY = {"readOnlyHint": True}
# Write tools change things, so they suggest that Agent HQ asks you first (your approval
# rules can still say otherwise).
ASK = {"agent_hq/approval": "ask"}
CHANGES = {"readOnlyHint": False, "destructiveHint": True}

MAX_ITEMS = int(os.environ.get("LAB_MAX_ITEMS", "40"))
TIMEOUT = float(os.environ.get("LAB_TIMEOUT", "15"))


class LabError(Exception):
    """A problem worth telling the agent about in plain words."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def server_name() -> str:
    """This instance's name (LABTOOLS_NAME): its MCP name, and who it says made a change."""
    return env("LABTOOLS_NAME", "labtools")


@lru_cache(maxsize=1)
def inventory() -> dict[str, Any]:
    path = Path(env("LAB_INVENTORY", "/etc/labtools/inventory.yaml"))
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def section(name: str) -> Any:
    return inventory().get(name)


def need(name: str) -> dict[str, Any]:
    cfg = section(name)
    if not cfg:
        raise LabError(f"'{name}' isn't in the lab inventory ({env('LAB_INVENTORY', '/etc/labtools/inventory.yaml')})")
    return cfg


def device(kind: str, name: str) -> dict[str, Any]:
    """A named device from an inventory list such as `mikrotik` or `snmp`."""
    devices = section(kind) or []
    for d in devices:
        if d.get("name", "").lower() == name.strip().lower():
            return d
    known = ", ".join(d.get("name", "?") for d in devices) or "none"
    raise LabError(f"no {kind} device named '{name}'. Known: {known}")


def verify_for(cfg: dict[str, Any]) -> ssl.SSLContext | bool:
    """TLS verification: the inventory's verify_tls (true/false/CA file), else LAB_CA_FILE
    (your step-ca root), else the system store."""
    v = cfg.get("verify_tls", None)
    if v is False:
        return False
    ca = v if isinstance(v, str) else env("LAB_CA_FILE")
    if ca and Path(ca).exists():
        return ssl.create_default_context(cafile=ca)
    return True


def client(cfg: dict[str, Any], **kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=verify_for(cfg), timeout=TIMEOUT, follow_redirects=True, **kw)


def clip(items: list[Any], limit: int | None = None) -> dict[str, Any]:
    """A list cut to `limit` (default LAB_MAX_ITEMS), saying how many were left out."""
    n = max(1, min(int(limit or MAX_ITEMS), 500))
    out: dict[str, Any] = {"items": items[:n], "count": len(items)}
    if len(items) > n:
        out["note"] = f"showing {n} of {len(items)}; narrow the request to see others"
    return out


def pct(used: float | int | None, total: float | int | None) -> float | None:
    try:
        return round(100.0 * float(used) / float(total), 1) if total else None
    except (TypeError, ValueError):
        return None


def gib(n: float | int | None) -> float | None:
    try:
        return round(float(n) / 1024 ** 3, 1)
    except (TypeError, ValueError):
        return None


def failsafe(fn):
    """Turn exceptions into {"error": ...} so the agent reads what went wrong instead of a
    stack trace, and can say so in its report."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except LabError as e:
            return {"error": str(e)}
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            return {"error": f"{e.request.method} {e.request.url.path}: HTTP {e.response.status_code} {body}"}
        except httpx.HTTPError as e:
            return {"error": f"couldn't reach {getattr(e.request, 'url', '?')}: {type(e).__name__} {e}"}
        except Exception as e:  # noqa: BLE001 - anything else, said plainly
            return {"error": f"{type(e).__name__}: {e}"}

    return wrapper
