"""TrueNAS SCALE over its JSON-RPC 2.0 WebSocket API (wss://<nas>/api/current).

TRUENAS_API_KEY: for read access, a key for a user with the Read-only Administrator role;
for write access (service restarts), a user that may restart services. The REST API is
deprecated (removed in TrueNAS 26), so only the WebSocket API is used.
"""

from __future__ import annotations

import itertools
import json
import ssl
from typing import Any

from .common import LabError, env, gib, need, pct, verify_for

_ids = itertools.count(1)


async def call(method: str, *params: Any) -> Any:
    """One login + one call per connection: simple, and fine at monitoring rates."""
    import websockets

    cfg = need("truenas")
    key = env("TRUENAS_API_KEY")
    if not key:
        raise LabError("TRUENAS_API_KEY isn't set for this tool server")
    url = cfg["url"]
    v = verify_for(cfg)
    ctx = None
    if url.startswith("wss://"):
        ctx = v if isinstance(v, ssl.SSLContext) else ssl.create_default_context()
        if v is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(url, ssl=ctx, open_timeout=10, max_size=16 * 2 ** 20) as ws:
        async def rpc(m: str, p: list[Any]) -> Any:
            i = next(_ids)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == i:
                    if "error" in msg:
                        err = msg["error"]
                        raise LabError(f"TrueNAS {m}: {err.get('data', {}).get('reason') or err.get('message')}")
                    return msg.get("result")

        if not await rpc("auth.login_with_api_key", [key]):
            raise LabError("TrueNAS refused the API key")
        return await rpc(method, list(params))


def _parsed(v: Any) -> Any:
    return v.get("parsed") if isinstance(v, dict) and "parsed" in v else v


async def overview() -> dict[str, Any]:
    info = await call("system.info")
    pools = await call("pool.query")
    alerts = await call("alert.list")
    return {
        "hostname": info.get("hostname"), "version": info.get("version"),
        "uptime": info.get("uptime"),
        "pools": [{"name": p["name"], "status": p.get("status"), "healthy": p.get("healthy"),
                   "used_pct": pct(_parsed(p.get("allocated")), _parsed(p.get("size"))),
                   "scrub": ((p.get("scan") or {}).get("state") or "")} for p in pools],
        "alerts": [{"level": a.get("level"), "text": (a.get("formatted") or a.get("klass") or "")[:300],
                    "since": (a.get("datetime") or {}).get("$date") if isinstance(a.get("datetime"), dict) else a.get("datetime")}
                   for a in alerts if not a.get("dismissed")],
    }


async def datasets(pool: str = "") -> list[dict[str, Any]]:
    filters = [["pool", "=", pool]] if pool else []
    rows = await call("pool.dataset.query", filters, {"extra": {"flat": True, "retrieve_children": True,
                                                                  "properties": ["used", "available", "quota"]}})
    out = []
    for d in rows:
        used, avail = _parsed(d.get("used")), _parsed(d.get("available"))
        total = (used or 0) + (avail or 0)
        out.append({"name": d["name"], "used_gib": gib(used), "available_gib": gib(avail),
                    "used_pct": pct(used, total)})
    return sorted(out, key=lambda r: r["used_pct"] or 0, reverse=True)


async def jobs() -> dict[str, Any]:
    """Replication, periodic snapshot and cloud sync tasks whose last run didn't succeed."""
    def state_of(t: dict[str, Any]) -> str:
        st = t.get("state") or (t.get("job") or {})
        return str(st.get("state") if isinstance(st, dict) else st or "")

    out: dict[str, Any] = {}
    for label, method, name_key in (("replication", "replication.query", "name"),
                                    ("snapshot_tasks", "pool.snapshottask.query", "dataset"),
                                    ("cloud_sync", "cloudsync.query", "description")):
        try:
            rows = await call(method)
        except LabError as e:
            out[label] = {"error": str(e)}
            continue
        out[label] = [{"name": t.get(name_key), "state": state_of(t), "enabled": t.get("enabled", True)}
                      for t in rows if t.get("enabled", True) and state_of(t) not in ("SUCCESS", "FINISHED", "RUNNING", "PENDING", "")]
    return out


SERVICES = ("cifs", "nfs", "ssh", "iscsitarget", "ups", "snmp", "ftp")


async def service(name: str, action: str) -> dict[str, Any]:
    if action not in ("start", "stop", "restart"):
        raise LabError("action must be start, stop or restart")
    before = await call("service.query", [["service", "=", name]])
    if not before:
        raise LabError(f"no TrueNAS service '{name}'")
    job = await call(f"service.{action}", name)
    if isinstance(job, int):  # newer versions run it as a job
        try:
            await call("core.job_wait", job)
        except LabError:
            pass
    after = await call("service.query", [["service", "=", name]])
    return {"service": name, "action": action, "before": before[0].get("state"), "after": after[0].get("state")}
