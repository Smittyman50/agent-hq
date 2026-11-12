"""Proxmox VE over its REST API with an API token.

PROXMOX_TOKEN is the whole token, `user@realm!tokenid=secret`. Read access: a token whose
user has the PVEAuditor role on /. Write access (guest power): a separate token with
VM.PowerMgmt, and nothing more, on the guests it may start, stop and reboot.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .common import LabError, client, env, gib, need, pct


def _headers() -> dict[str, str]:
    token = env("PROXMOX_TOKEN")
    if not token or "=" not in token:
        raise LabError("PROXMOX_TOKEN isn't set (format: user@realm!tokenid=secret)")
    return {"Authorization": f"PVEAPIToken={token}"}


async def call(method: str, path: str, **kw: Any) -> Any:
    cfg = need("proxmox")
    async with client(cfg, headers=_headers()) as c:
        r = await c.request(method, f"{cfg['url'].rstrip('/')}/api2/json/{path.strip('/')}", **kw)
        r.raise_for_status()
        return r.json().get("data")


async def get(path: str, **params: Any) -> Any:
    return await call("GET", path, params=params or None)


def _uptime(s: Any) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return ""
    d, s = divmod(s, 86400)
    return f"{d}d {s // 3600}h"


async def overview() -> dict[str, Any]:
    status = await get("cluster/status")
    cluster = next((x for x in status if x.get("type") == "cluster"), {})
    nodes = await get("nodes")
    return {
        "cluster": cluster.get("name", "(single node)"),
        "quorate": bool(cluster.get("quorate", 1)),
        "nodes": [{"node": n["node"], "status": n.get("status"),
                   "cpu_pct": round(100 * float(n.get("cpu") or 0), 1),
                   "memory_pct": pct(n.get("mem"), n.get("maxmem")),
                   "uptime": _uptime(n.get("uptime"))} for n in nodes],
    }


async def guests() -> list[dict[str, Any]]:
    rows = await get("cluster/resources", type="vm")
    return [{"vmid": x["vmid"], "name": x.get("name"), "type": x.get("type"), "node": x.get("node"),
             "status": x.get("status"), "template": bool(x.get("template")),
             "cpu_pct": round(100 * float(x.get("cpu") or 0), 1),
             "memory_pct": pct(x.get("mem"), x.get("maxmem")), "uptime": _uptime(x.get("uptime")),
             "ha": x.get("hastate", "")} for x in rows]


async def guest(vmid: int) -> dict[str, Any]:
    for g in await guests():
        if int(g["vmid"]) == int(vmid):
            return g
    raise LabError(f"no guest with VMID {vmid}")


async def storage() -> list[dict[str, Any]]:
    rows = await get("cluster/resources", type="storage")
    return [{"storage": x.get("storage"), "node": x.get("node"), "status": x.get("status"),
             "type": x.get("plugintype"), "used_pct": pct(x.get("disk"), x.get("maxdisk")),
             "used_gib": gib(x.get("disk")), "size_gib": gib(x.get("maxdisk")),
             "shared": bool(x.get("shared"))} for x in rows]


async def tasks(hours: float = 24, failed_only: bool = True) -> list[dict[str, Any]]:
    since = time.time() - hours * 3600
    rows = [x for x in await get("cluster/tasks") if (x.get("starttime") or 0) >= since]
    out = []
    for x in sorted(rows, key=lambda r: r.get("starttime") or 0, reverse=True):
        status = x.get("status") or ("running" if not x.get("endtime") else "")
        if failed_only and status in ("OK", "running"):
            continue
        out.append({"type": x.get("type"), "id": x.get("id"), "node": x.get("node"), "user": x.get("user"),
                    "status": status,
                    "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(x.get("starttime") or 0))})
    return out


async def stale_backups(hours: float = 36) -> dict[str, Any]:
    """Guests whose newest backup (on any storage holding backups) is older than `hours`,
    or that have none. Templates are skipped."""
    nodes = [n["node"] for n in await get("nodes") if n.get("status") == "online"]
    stores = [s for s in await get("storage") if "backup" in (s.get("content") or "")]
    latest: dict[int, float] = {}
    seen_shared = set()
    for st in stores:
        for node in nodes:
            if st.get("shared") and st["storage"] in seen_shared:
                break
            try:
                items = await get(f"nodes/{node}/storage/{st['storage']}/content", content="backup")
            except Exception:
                continue
            seen_shared.add(st["storage"])
            for it in items or []:
                vmid = it.get("vmid")
                if vmid is not None:
                    latest[int(vmid)] = max(latest.get(int(vmid), 0), float(it.get("ctime") or 0))
    cutoff = time.time() - hours * 3600
    stale = []
    all_guests = [g for g in await guests() if not g["template"]]
    for g in all_guests:
        t = latest.get(int(g["vmid"]))
        if not t or t < cutoff:
            stale.append({"vmid": g["vmid"], "name": g["name"],
                          "newest_backup": time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "none"})
    return {"older_than_hours": hours, "guests": len(all_guests), "stale": stale,
            "backup_storages": [s["storage"] for s in stores]}


# --------------------------------------------------------------------------- write

POWER_ACTIONS = ("start", "shutdown", "reboot", "stop", "resume")


async def power(vmid: int, action: str, wait_seconds: int = 90) -> dict[str, Any]:
    if action not in POWER_ACTIONS:
        raise LabError(f"action must be one of {', '.join(POWER_ACTIONS)}")
    g = await guest(vmid)
    if g["template"]:
        raise LabError(f"{vmid} is a template")
    kind = "qemu" if g["type"] == "qemu" else "lxc"
    upid = await call("POST", f"nodes/{g['node']}/{kind}/{vmid}/status/{action}")
    result = {"vmid": vmid, "name": g["name"], "action": action, "before": g["status"], "task": upid}
    deadline = time.monotonic() + max(5, min(wait_seconds, 600))
    while time.monotonic() < deadline:
        st = await get(f"nodes/{g['node']}/tasks/{upid}/status")
        if st.get("status") == "stopped":
            result["task_result"] = st.get("exitstatus")
            break
        await asyncio.sleep(2)
    else:
        result["task_result"] = "still running"
    result["after"] = (await guest(vmid))["status"]
    return result
