"""MikroTik RouterOS 7 over its REST API (/rest, HTTPS, the `www-ssl` service).

Read access: a RouterOS user in a group with `read,api,rest-api` policies; write access
also needs `write`. Credentials: MIKROTIK_USER / MIKROTIK_PASSWORD (the same
account on every device, or per device with `user_env`/`password_env` in the inventory).
"""

from __future__ import annotations

from typing import Any

from .common import LabError, client, device, env


def _auth(d: dict[str, Any]) -> tuple[str, str]:
    user = env(d.get("user_env", "MIKROTIK_USER"))
    password = env(d.get("password_env", "MIKROTIK_PASSWORD"))
    if not user:
        raise LabError("MIKROTIK_USER isn't set for this tool server")
    return user, password


def _base(d: dict[str, Any]) -> str:
    scheme = d.get("scheme", "https")
    return f"{scheme}://{d['host']}/rest"


async def get(name: str, path: str, params: dict[str, Any] | None = None) -> Any:
    d = device("mikrotik", name)
    async with client(d, auth=_auth(d)) as c:
        r = await c.get(f"{_base(d)}/{path.strip('/')}", params=params)
        r.raise_for_status()
        return r.json()


async def post(name: str, path: str, body: dict[str, Any]) -> Any:
    d = device("mikrotik", name)
    async with client(d, auth=_auth(d)) as c:
        r = await c.post(f"{_base(d)}/{path.strip('/')}", json=body)
        r.raise_for_status()
        return r.json() if r.content else {}


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def interface_row(x: dict[str, Any]) -> dict[str, Any]:
    return {"name": x.get("name"), "type": x.get("type"), "comment": x.get("comment", ""),
            "running": x.get("running") == "true", "disabled": x.get("disabled") == "true",
            "rx_errors": _int(x.get("rx-error")), "tx_errors": _int(x.get("tx-error")),
            "rx_drops": _int(x.get("rx-drop")), "tx_drops": _int(x.get("tx-drop")),
            "link_downs": _int(x.get("link-downs")),
            "last_link_down": x.get("last-link-down-time", ""),
            "last_link_up": x.get("last-link-up-time", "")}


def is_down(row: dict[str, Any], expected_up: set[str]) -> bool:
    """Enabled but not running, where it should be up: listed in the inventory's
    expected_up, or it has been up since boot (an unused port never has)."""
    return (not row["disabled"] and not row["running"]
            and (row["name"] in expected_up or bool(row["last_link_up"])))


def has_errors(row: dict[str, Any]) -> bool:
    """Error or drop counters above zero (they count since boot: compare two readings to
    see whether they're still rising)."""
    return any(row[k] for k in ("rx_errors", "tx_errors", "rx_drops", "tx_drops"))


async def interfaces(name: str) -> list[dict[str, Any]]:
    return [interface_row(x) for x in await get(name, "interface")]


async def health(name: str) -> dict[str, Any]:
    d = device("mikrotik", name)
    res = await get(name, "system/resource")
    out: dict[str, Any] = {
        "device": d["name"], "board": res.get("board-name"), "version": res.get("version"),
        "uptime": res.get("uptime"), "cpu_load_pct": _int(res.get("cpu-load")),
        "memory_used_pct": round(100 - 100 * _int(res.get("free-memory")) / max(_int(res.get("total-memory")), 1), 1),
        "disk_used_pct": round(100 - 100 * _int(res.get("free-hdd-space")) / max(_int(res.get("total-hdd-space")), 1), 1),
    }
    try:
        sensors = await get(name, "system/health")
        out["sensors"] = {s.get("name"): f"{s.get('value')}{s.get('type', '')}" for s in sensors
                          if isinstance(s, dict) and s.get("name")}
    except Exception:  # not every board has sensors
        out["sensors"] = {}
    rows = await interfaces(name)
    expected = set(d.get("expected_up", []))
    out["interfaces"] = {"total": len(rows), "running": sum(r["running"] for r in rows),
                         "down": [r["name"] for r in rows if is_down(r, expected)],
                         "with_errors": [r["name"] for r in rows if has_errors(r)]}
    return out


async def wifi_clients(name: str) -> list[dict[str, Any]]:
    """Wireless clients: the wifi package (RouterOS 7.13+) first, then the older wireless one."""
    for path in ("interface/wifi/registration-table", "interface/wireless/registration-table"):
        try:
            rows = await get(name, path)
        except Exception:
            continue
        return [{"mac": x.get("mac-address"), "interface": x.get("interface"), "ssid": x.get("ssid", ""),
                 "signal": x.get("signal") or x.get("signal-strength"), "uptime": x.get("uptime"),
                 "rx_rate": x.get("rx-rate"), "tx_rate": x.get("tx-rate")} for x in rows]
    raise LabError(f"{name} has no wireless registration table (not an AP, or no wifi package)")
