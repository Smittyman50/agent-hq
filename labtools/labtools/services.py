"""Technitium DNS, ISC Kea DHCP and simple network probes.

Technitium: HTTP API with TECHNITIUM_TOKEN (create one for a user with only the access
the instance needs: View for read access; flushing the cache needs Cache: Modify). Kea: the Control Agent's HTTP API (optional KEA_USER / KEA_PASSWORD for
its basic auth); listing leases needs the lease_cmds hook loaded in kea-dhcp4.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .common import LabError, client, env, need

# --------------------------------------------------------------------------- Technitium


async def technitium(path: str, **params: Any) -> dict[str, Any]:
    cfg = need("technitium")
    token = env("TECHNITIUM_TOKEN")
    if not token:
        raise LabError("TECHNITIUM_TOKEN isn't set for this tool server")
    async with client(cfg) as c:
        r = await c.get(f"{cfg['url'].rstrip('/')}/api/{path.strip('/')}", params={"token": token, **params})
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "ok":
        raise LabError(f"Technitium {path}: {data.get('errorMessage') or data.get('status')}")
    return data.get("response", {})


async def dns_stats() -> dict[str, Any]:
    resp = await technitium("dashboard/stats/get", type="LastHour", utc="true")
    s = resp.get("stats", {})
    total = s.get("totalQueries") or 0
    out = {k: s.get(k) for k in ("totalQueries", "totalNoError", "totalServerFailure", "totalNxDomain",
                                  "totalRefused", "totalBlocked", "totalCached", "totalClients",
                                  "zones", "cachedEntries")}
    out["server_failure_pct"] = round(100 * (s.get("totalServerFailure") or 0) / total, 2) if total else 0
    out["top_clients"] = [{"name": c.get("name"), "hits": c.get("hits")} for c in resp.get("topClients", [])[:5]]
    return {"period": "last hour", **out}


async def dns_flush_cache() -> dict[str, Any]:
    await technitium("cache/flush")
    return {"flushed": True}


async def dns_lookup(name: str, record_type: str = "A", server: str = "") -> dict[str, Any]:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    res = dns.asyncresolver.Resolver(configure=False)
    host = server or (need("technitium").get("dns_server") or "")
    if not host:
        raise LabError("give a server, or set technitium.dns_server in the inventory")
    res.nameservers = [host]
    res.lifetime = 5
    started = time.perf_counter()
    try:
        ans = await res.resolve(name, record_type, raise_on_no_answer=False)
        rcode = "NOERROR" if ans.rrset else "NOERROR (no answer)"
        answers = [r.to_text() for r in ans.rrset] if ans.rrset else []
    except dns.resolver.NXDOMAIN:
        rcode, answers = "NXDOMAIN", []
    except dns.resolver.NoNameservers as e:
        rcode, answers = f"SERVFAIL ({e})"[:200], []
    except dns.exception.Timeout:
        rcode, answers = "TIMEOUT", []
    return {"name": name, "type": record_type, "server": host, "rcode": rcode, "answers": answers[:20],
            "ms": round((time.perf_counter() - started) * 1000, 1)}


# --------------------------------------------------------------------------- Kea


async def kea(command: str, arguments: dict[str, Any] | None = None, service: str = "dhcp4") -> Any:
    cfg = need("kea")
    auth = (env("KEA_USER"), env("KEA_PASSWORD")) if env("KEA_USER") else None
    body: dict[str, Any] = {"command": command, "service": [service]}
    if arguments:
        body["arguments"] = arguments
    async with client(cfg, auth=auth) as c:
        r = await c.post(cfg["url"], json=body)
        r.raise_for_status()
        data = r.json()
    first = data[0] if isinstance(data, list) else data
    if first.get("result") not in (0, 3):  # 3 = empty
        raise LabError(f"Kea {command}: {first.get('text')}")
    return first.get("arguments") or {}


async def dhcp_pools() -> list[dict[str, Any]]:
    """Per subnet: addresses assigned out of the pool size, and declined."""
    conf = await kea("config-get")
    subnets = {}
    for sn in conf.get("Dhcp4", {}).get("subnet4", []):
        subnets[sn["id"]] = sn.get("subnet")
    for net in conf.get("Dhcp4", {}).get("shared-networks", []):
        for sn in net.get("subnet4", []):
            subnets[sn["id"]] = sn.get("subnet")
    stats = await kea("statistic-get-all")

    def val(name: str) -> int:
        v = stats.get(name)
        return int(v[0][0]) if v else 0

    rows = []
    for sid, cidr in sorted(subnets.items()):
        total = val(f"subnet[{sid}].total-addresses")
        used = val(f"subnet[{sid}].assigned-addresses")
        rows.append({"subnet_id": sid, "subnet": cidr, "assigned": used, "total": total,
                     "used_pct": round(100 * used / total, 1) if total else None,
                     "declined": val(f"subnet[{sid}].declined-addresses")})
    return rows


async def dhcp_leases(search: str = "") -> list[dict[str, Any]]:
    args = await kea("lease4-get-all")
    q = search.strip().lower()
    rows = []
    for x in args.get("leases", []):
        row = {"ip": x.get("ip-address"), "mac": x.get("hw-address"), "hostname": x.get("hostname", ""),
               "subnet_id": x.get("subnet-id"), "state": {0: "default", 1: "declined", 2: "expired-reclaimed"}.get(x.get("state"), x.get("state")),
               "expires": (x.get("cltt", 0) or 0) + (x.get("valid-lft", 0) or 0)}
        if not q or any(q in str(v).lower() for v in (row["ip"], row["mac"], row["hostname"])):
            rows.append(row)
    return rows


async def dhcp_lease_delete(ip: str) -> dict[str, Any]:
    await kea("lease4-del", {"ip-address": ip})
    return {"deleted": ip}


# --------------------------------------------------------------------------- probes

_HOST = re.compile(r"^[A-Za-z0-9.:\-_]{1,253}$")


def _host(host: str) -> str:
    host = host.strip()
    if not _HOST.match(host) or host.startswith("-"):
        raise LabError(f"'{host}' isn't a host name or address")
    return host


async def ping(host: str, count: int = 4) -> dict[str, Any]:
    host = _host(host)
    count = max(1, min(int(count), 10))
    proc = await asyncio.create_subprocess_exec(
        "ping", "-n", "-c", str(count), "-W", "2", host,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=count * 3 + 5)
    text = out.decode(errors="replace")
    loss = re.search(r"([\d.]+)% packet loss", text)
    rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", text)
    return {"host": host, "sent": count, "loss_pct": float(loss.group(1)) if loss else 100.0,
            "rtt_ms": {"min": float(rtt.group(1)), "avg": float(rtt.group(2)), "max": float(rtt.group(3))} if rtt else None,
            "reachable": bool(rtt)}


async def check_port(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    host = _host(host)
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=timeout)
        writer.close()
        return {"host": host, "port": port, "open": True, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except (OSError, asyncio.TimeoutError) as e:
        return {"host": host, "port": port, "open": False, "error": type(e).__name__}
