"""Switches without an API (Ubiquiti standalone, Netgear smart/managed) over SNMP.

Inventory entries under `snmp:` give name, host and optionally `version: 3`.
v2c reads SNMP_COMMUNITY. v3 reads SNMP_USER, SNMP_AUTH_KEY, SNMP_PRIV_KEY (SHA + AES-128).
Read-only: Agent HQ never writes over SNMP.
"""

from __future__ import annotations

from typing import Any

from .common import LabError, device, env

SYS = {"descr": "1.3.6.1.2.1.1.1.0", "uptime": "1.3.6.1.2.1.1.3.0", "name": "1.3.6.1.2.1.1.5.0",
       "location": "1.3.6.1.2.1.1.6.0"}
IF_COLUMNS = {  # IF-MIB ifTable / ifXTable columns
    "descr": "1.3.6.1.2.1.2.2.1.2", "admin": "1.3.6.1.2.1.2.2.1.7", "oper": "1.3.6.1.2.1.2.2.1.8",
    "in_errors": "1.3.6.1.2.1.2.2.1.14", "out_errors": "1.3.6.1.2.1.2.2.1.20",
    "in_discards": "1.3.6.1.2.1.2.2.1.13", "out_discards": "1.3.6.1.2.1.2.2.1.19",
    "alias": "1.3.6.1.2.1.31.1.1.1.18", "speed_mbps": "1.3.6.1.2.1.31.1.1.1.15",
}
STATUS = {1: "up", 2: "down", 3: "testing", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}


async def _target(d: dict[str, Any]):
    from pysnmp.hlapi.v3arch.asyncio import (CommunityData, ContextData, SnmpEngine,
                                             UdpTransportTarget, UsmUserData,
                                             usmAesCfb128Protocol, usmHMACSHAAuthProtocol)

    if str(d.get("version", "2c")) == "3":
        user = env("SNMP_USER")
        if not user:
            raise LabError("SNMP_USER isn't set (needed for SNMPv3 devices)")
        auth = UsmUserData(user, env("SNMP_AUTH_KEY") or None, env("SNMP_PRIV_KEY") or None,
                           authProtocol=usmHMACSHAAuthProtocol, privProtocol=usmAesCfb128Protocol)
    else:
        community = env("SNMP_COMMUNITY")
        if not community:
            raise LabError("SNMP_COMMUNITY isn't set")
        auth = CommunityData(community, mpModel=1)
    target = await UdpTransportTarget.create((d["host"], int(d.get("port", 161))), timeout=3, retries=1)
    return SnmpEngine(), auth, target, ContextData()


async def get(d: dict[str, Any], oids: dict[str, str]) -> dict[str, Any]:
    from pysnmp.hlapi.v3arch.asyncio import ObjectIdentity, ObjectType, get_cmd

    engine, auth, target, ctx = await _target(d)
    err, status, _, binds = await get_cmd(engine, auth, target, ctx,
                                          *[ObjectType(ObjectIdentity(o)) for o in oids.values()])
    if err or status:
        raise LabError(f"SNMP get from {d['name']} failed: {err or status.prettyPrint()}")
    return {k: b[1] for k, b in zip(oids, binds)}


async def walk(d: dict[str, Any], oid: str) -> dict[int, Any]:
    """{ifIndex: value} for one table column."""
    from pysnmp.hlapi.v3arch.asyncio import ObjectIdentity, ObjectType, bulk_walk_cmd

    engine, auth, target, ctx = await _target(d)
    out: dict[int, Any] = {}
    async for err, status, _, binds in bulk_walk_cmd(engine, auth, target, ctx, 0, 25,
                                                      ObjectType(ObjectIdentity(oid)),
                                                      lexicographicMode=False):
        if err or status:
            raise LabError(f"SNMP walk on {d['name']} failed: {err or status.prettyPrint()}")
        for name, value in binds:
            out[int(str(name).rsplit(".", 1)[-1])] = value
    return out


def _num(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


async def interfaces(name: str) -> list[dict[str, Any]]:
    d = device("snmp", name)
    cols = {k: await walk(d, oid) for k, oid in IF_COLUMNS.items()}
    rows = []
    for idx, descr in sorted(cols["descr"].items()):
        rows.append({
            "index": idx, "name": str(descr), "alias": str(cols["alias"].get(idx, "")),
            "admin": STATUS.get(_num(cols["admin"].get(idx)), "?"),
            "oper": STATUS.get(_num(cols["oper"].get(idx)), "?"),
            "speed_mbps": _num(cols["speed_mbps"].get(idx)),
            "in_errors": _num(cols["in_errors"].get(idx)), "out_errors": _num(cols["out_errors"].get(idx)),
            "in_discards": _num(cols["in_discards"].get(idx)), "out_discards": _num(cols["out_discards"].get(idx)),
        })
    return rows


def uptime_text(ticks: Any) -> str:
    s = _num(ticks) // 100
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return f"{d}d {h}h {s // 60}m"


async def health(name: str) -> dict[str, Any]:
    d = device("snmp", name)
    sysv = await get(d, SYS)
    rows = await interfaces(name)
    expected = set(d.get("expected_up", []))
    return {
        "device": d["name"], "kind": d.get("kind", ""), "sys_name": str(sysv["name"]),
        "description": str(sysv["descr"])[:200], "uptime": uptime_text(sysv["uptime"]),
        "interfaces": {
            "total": len(rows), "up": sum(r["oper"] == "up" for r in rows),
            # admin up but oper down: a port that's on with nothing (working) connected
            "down": [r["name"] for r in rows if r["admin"] == "up" and r["oper"] != "up"
                     and (not expected or r["name"] in expected or r["alias"] in expected)],
            "with_errors": [r["name"] for r in rows if r["in_errors"] or r["out_errors"]],
        },
    }
