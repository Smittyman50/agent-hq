"""Zabbix, Prometheus / Alertmanager and Graylog.

ZABBIX_TOKEN: an API token for a user in a group with read access to the host groups to
watch (a read-write instance's token also needs to acknowledge problems). PROMETHEUS_TOKEN /
ALERTMANAGER_TOKEN: optional bearer tokens. GRAYLOG_TOKEN: an access token for a user with
the Reader role (search) and event read permission.
"""

from __future__ import annotations

import time
from typing import Any

from .common import LabError, client, env, need, server_name

SEVERITY = {0: "not classified", 1: "information", 2: "warning", 3: "average", 4: "high", 5: "disaster"}

# --------------------------------------------------------------------------- Zabbix


async def zabbix(method: str, params: dict[str, Any] | list[Any]) -> Any:
    cfg = need("zabbix")
    token = env("ZABBIX_TOKEN")
    if not token:
        raise LabError("ZABBIX_TOKEN isn't set for this tool server")
    url = cfg["url"].rstrip("/")
    url = url if url.endswith("api_jsonrpc.php") else f"{url}/api_jsonrpc.php"
    async with client(cfg, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post(url, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1})
        r.raise_for_status()
        data = r.json()
    if "error" in data:
        raise LabError(f"Zabbix {method}: {data['error'].get('data') or data['error'].get('message')}")
    return data.get("result")


async def zabbix_problems(min_severity: int = 2, host: str = "", limit: int = 30) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged", "suppressed"],
                              "selectTags": "extend", "recent": False, "sortfield": ["eventid"], "sortorder": "DESC",
                              "severities": list(range(max(0, min(int(min_severity), 5)), 6)), "limit": 200}
    if host:
        hosts = await zabbix("host.get", {"output": ["hostid"], "search": {"host": host, "name": host},
                                          "searchByAny": True})
        if not hosts:
            raise LabError(f"no Zabbix host matching '{host}'")
        params["hostids"] = [h["hostid"] for h in hosts]
    problems = await zabbix("problem.get", params)
    trig_hosts: dict[str, str] = {}
    if problems:
        trigs = await zabbix("trigger.get", {"output": ["triggerid"], "triggerids": [p["objectid"] for p in problems],
                                             "selectHosts": ["name"]})
        trig_hosts = {t["triggerid"]: ", ".join(h["name"] for h in t.get("hosts", [])) for t in trigs}
    rows = []
    for p in problems:
        rows.append({"eventid": p["eventid"], "problem": p["name"], "host": trig_hosts.get(p["objectid"], "?"),
                     "severity": SEVERITY.get(int(p["severity"]), p["severity"]),
                     "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p["clock"]))),
                     "acknowledged": p.get("acknowledged") == "1", "suppressed": p.get("suppressed") == "1",
                     "tags": {t["tag"]: t.get("value", "") for t in p.get("tags", [])}})
    return rows[:max(1, min(int(limit), 200))]


async def zabbix_host(host: str) -> dict[str, Any]:
    hosts = await zabbix("host.get", {"output": ["hostid", "host", "name", "status", "maintenance_status"],
                                      "search": {"host": host, "name": host}, "searchByAny": True,
                                      "selectInterfaces": ["type", "ip", "dns", "available", "error"],
                                      "selectTags": "extend", "limit": 5})
    if not hosts:
        raise LabError(f"no Zabbix host matching '{host}'")
    avail = {"0": "unknown", "1": "available", "2": "unavailable"}
    kinds = {"1": "agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}
    return {"hosts": [{"name": h["name"], "monitored": h.get("status") == "0",
                       "in_maintenance": h.get("maintenance_status") == "1",
                       "interfaces": [{"type": kinds.get(i["type"], i["type"]), "address": i.get("ip") or i.get("dns"),
                                       "available": avail.get(str(i.get("available")), "?"),
                                       "error": i.get("error", "")} for i in h.get("interfaces", [])],
                       "tags": {t["tag"]: t.get("value", "") for t in h.get("tags", [])}} for h in hosts]}


async def zabbix_acknowledge(eventid: str, message: str, close: bool = False) -> dict[str, Any]:
    action = 2 | 4 | (1 if close else 0)  # acknowledge + add message (+ close)
    await zabbix("event.acknowledge", {"eventids": [str(eventid)], "action": action, "message": message[:2048]})
    return {"eventid": eventid, "acknowledged": True, "closed": close}


# --------------------------------------------------------------------------- Prometheus / Alertmanager


def _bearer(var: str) -> dict[str, str]:
    t = env(var)
    return {"Authorization": f"Bearer {t}"} if t else {}


async def prometheus(path: str, **params: Any) -> Any:
    cfg = need("prometheus")
    async with client(cfg, headers=_bearer("PROMETHEUS_TOKEN")) as c:
        r = await c.get(f"{cfg['url'].rstrip('/')}/api/v1/{path.strip('/')}", params=params or None)
        r.raise_for_status()
        data = r.json()
    if data.get("status") != "success":
        raise LabError(f"Prometheus {path}: {data.get('error')}")
    return data.get("data")


async def prom_query(promql: str, limit: int = 30) -> dict[str, Any]:
    data = await prometheus("query", query=promql)
    kind, result = data.get("resultType"), data.get("result")
    if kind in ("scalar", "string"):
        return {"type": kind, "value": result[1]}
    rows = [{"labels": x.get("metric", {}), "value": (x.get("value") or [None, None])[1]} for x in result]
    n = max(1, min(int(limit), 200))
    return {"type": kind, "series": rows[:n], "count": len(rows),
            **({"note": f"showing {n} of {len(rows)}; aggregate (sum/topk) to see less"} if len(rows) > n else {})}


async def prom_alerts() -> list[dict[str, Any]]:
    data = await prometheus("alerts")
    return [{"alert": a["labels"].get("alertname"), "state": a.get("state"), "severity": a["labels"].get("severity", ""),
             "labels": {k: v for k, v in a["labels"].items() if k not in ("alertname", "severity")},
             "summary": (a.get("annotations") or {}).get("summary") or (a.get("annotations") or {}).get("description", ""),
             "since": a.get("activeAt")} for a in data.get("alerts", [])]


async def prom_targets_down() -> list[dict[str, Any]]:
    data = await prometheus("targets", state="active")
    return [{"job": t["labels"].get("job"), "instance": t["labels"].get("instance"), "health": t.get("health"),
             "error": t.get("lastError", "")[:300], "last_scrape": t.get("lastScrape")}
            for t in data.get("activeTargets", []) if t.get("health") != "up"]


async def alertmanager(method: str, path: str, **kw: Any) -> Any:
    cfg = need("prometheus")
    url = cfg.get("alertmanager_url")
    if not url:
        raise LabError("prometheus.alertmanager_url isn't in the inventory")
    async with client(cfg, headers=_bearer("ALERTMANAGER_TOKEN")) as c:
        r = await c.request(method, f"{url.rstrip('/')}/api/v2/{path.strip('/')}", **kw)
        r.raise_for_status()
        return r.json() if r.content else {}


async def silences() -> list[dict[str, Any]]:
    rows = await alertmanager("GET", "silences")
    return [{"id": s["id"], "state": (s.get("status") or {}).get("state"), "matchers": s.get("matchers"),
             "ends": s.get("endsAt"), "comment": s.get("comment"), "by": s.get("createdBy")}
            for s in rows if (s.get("status") or {}).get("state") == "active"]


async def silence(matchers: dict[str, str], minutes: int, comment: str, by: str = "") -> dict[str, Any]:
    if not matchers:
        raise LabError("give at least one label to match, e.g. {'alertname': 'HostDown', 'instance': 'nas:9100'}")
    minutes = max(5, min(int(minutes), 24 * 60))
    now = time.time()
    body = {"matchers": [{"name": k, "value": v, "isRegex": False, "isEqual": True} for k, v in matchers.items()],
            "startsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "endsAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + minutes * 60)),
            "createdBy": by or f"Agent HQ ({server_name()})", "comment": comment[:500] or "silenced by Agent HQ"}
    out = await alertmanager("POST", "silences", json=body)
    return {"silence_id": out.get("silenceID"), "minutes": minutes, "matchers": matchers}


# --------------------------------------------------------------------------- Graylog


async def graylog(method: str, path: str, **kw: Any) -> Any:
    cfg = need("graylog")
    token = env("GRAYLOG_TOKEN")
    if not token:
        raise LabError("GRAYLOG_TOKEN isn't set for this tool server")
    async with client(cfg, auth=(token, "token"),
                      headers={"Accept": "application/json", "X-Requested-By": "agent-hq"}) as c:
        r = await c.request(method, f"{cfg['url'].rstrip('/')}/api/{path.strip('/')}", **kw)
        r.raise_for_status()
        return r.json()


async def graylog_search(query: str, minutes: int = 60, limit: int = 30) -> dict[str, Any]:
    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    limit = max(1, min(int(limit), 100))
    fields = ["timestamp", "source", "level", "message"]
    try:  # Graylog 5+/6: the simple message search API
        data = await graylog("POST", "search/messages", json={
            "query": query or "*", "timerange": {"type": "relative", "range": minutes * 60},
            "fields_in_order": fields, "size": limit, "sort": "timestamp", "sort_order": "desc"})
        schema = [c.get("field") for c in data.get("schema", [])]
        rows = [dict(zip(schema, r)) for r in data.get("datarows", [])]
    except Exception:  # older versions: the universal relative search
        data = await graylog("GET", "search/universal/relative", params={
            "query": query or "*", "range": minutes * 60, "limit": limit, "fields": ",".join(fields)})
        rows = [{k: m.get("message", {}).get(k) for k in fields} for m in data.get("messages", [])]
    for r in rows:
        if isinstance(r.get("message"), str):
            r["message"] = r["message"][:400]
    return {"query": query or "*", "minutes": minutes, "messages": rows}


async def graylog_events(minutes: int = 60, limit: int = 30) -> list[dict[str, Any]]:
    data = await graylog("POST", "events/search", json={
        "query": "", "page": 1, "per_page": max(1, min(int(limit), 100)),
        "timerange": {"type": "relative", "range": max(60, int(minutes) * 60)}})
    ctx = (data.get("context") or {}).get("event_definitions", {})
    out = []
    for e in data.get("events", []):
        ev = e.get("event", e)
        out.append({"time": ev.get("timestamp"), "definition": (ctx.get(ev.get("event_definition_id")) or {}).get("title", ""),
                    "message": (ev.get("message") or "")[:400], "priority": ev.get("priority"),
                    "source": ev.get("source", ""), "fields": ev.get("fields", {})})
    return out
