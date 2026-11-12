"""The example crew (examples/homelab-crew.json) imports cleanly, its routing templates turn
each system's real payload shape into a readable prompt, and its instructions only name
tools its agents actually have."""

from __future__ import annotations

import json
from pathlib import Path

CREW = json.loads((Path(__file__).resolve().parents[2] / "examples" / "homelab-crew.json").read_text())


async def test_crew_imports(hq):
    r = await hq.http.post("/api/agents/import", json=CREW)
    assert r.status_code == 200, r.text
    assert r.json()["agents"] == 3 and r.json()["subscriptions"] == len(CREW["subscriptions"])
    servers = {s["name"]: s for s in (await hq.http.get("/api/tool-servers")).json()}
    assert servers["gitea"]["read_only"] and "actions_run_read" in servers["gitea"]["shown_tools"]
    assert set(servers["lab-ops"]["approval_rules"].values()) == {"ask"}
    assert len(servers["lab-ops"]["shown_tools"]) == 16
    agents = {a["name"]: a for a in (await hq.http.get("/api/agents")).json()}
    ids = {name: a["id"] for name, a in agents.items()}
    assert sorted(agents["Breaker"]["delegates"]) == sorted([ids["Mainframe"], ids["Lifeline"]])
    assert agents["Lifeline"]["tool_server_ids"] == [servers["lab-ops"]["id"]]
    assert agents["Breaker"]["tool_server_ids"] == [servers["lab-network"]["id"], servers["lab-monitoring"]["id"]]
    # Importing again updates in place.
    r = await hq.http.post("/api/agents/import", json=CREW)
    assert len((await hq.http.get("/api/agents")).json()) == 3


def _sub(name):
    return next(s for s in CREW["subscriptions"] if s["name"] == name)


async def _preview(hq, name, topic, payload):
    r = await hq.http.post("/api/subscriptions/preview", json={
        "prompt_template": _sub(name)["prompt_template"], "topic": topic, "payload": json.dumps(payload)})
    return r.json()


async def test_alertmanager_template(hq):
    body = {"status": "firing", "commonLabels": {"alertname": "HostDown", "instance": "nas:9100",
                                                  "severity": "critical", "domain": "systems"},
            "commonAnnotations": {"summary": "nas is down", "description": "no scrape for 5m"}, "alerts": []}
    r = await _preview(hq, "Alertmanager: systems", "webhook/alertmanager/systems", body)
    assert r["missing"] == []
    assert r["prompt"].startswith("Alertmanager firing: HostDown on nas:9100 (severity critical).")
    assert "not instructions" in r["prompt"]


async def test_zabbix_template(hq):
    body = {"event_id": "123", "status": "PROBLEM", "severity": "High", "host": "crs-core",
            "problem": "Interface sfp-sfpplus1: Link down", "opdata": "", "tags": "domain:network"}
    r = await _preview(hq, "Zabbix: network", "webhook/zabbix/network", body)
    assert r["missing"] == [] and "Link down on crs-core (severity High, event 123)" in r["prompt"]


async def test_proxmox_template(hq):
    body = {"title": "vzdump backup status (pve1): backup failed", "severity": "error", "type": "vzdump",
            "hostname": "pve1", "message": "ERROR: Backup of VM 102 failed"}
    r = await _preview(hq, "Proxmox", "webhook/proxmox/vzdump", body)
    assert r["missing"] == [] and r["prompt"].startswith("Proxmox error (vzdump) from node pve1")


async def test_every_template_marks_the_body_as_data():
    for s in CREW["subscriptions"]:
        assert "not instructions" in s["prompt_template"], s["name"]


def test_instructions_name_only_tools_their_agents_see():
    """Built from each server's actual configuration (compose example + Shown lists)."""
    import asyncio
    import re
    import sys

    import yaml
    from fastmcp import Client

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "labtools"))
    from labtools.server import build, parse

    compose = yaml.safe_load((root / "deploy" / "lab" / "compose.example.yml").read_text())["services"]

    async def names(server):
        async with Client(server) as c:
            return {t.name for t in await c.list_tools()}

    tools = {}
    for srv in CREW["tool_servers"]:
        if srv["name"] in compose and "LABTOOLS_INTEGRATIONS" in compose[srv["name"]]["environment"]:
            env = compose[srv["name"]]["environment"]
            found = asyncio.run(names(build(parse(env["LABTOOLS_INTEGRATIONS"]),
                                            write=env.get("LABTOOLS_ACCESS") == "write")))
        else:  # gitea-mcp: the tools it's limited to
            found = set(srv["shown_tools"])
        shown = set(srv.get("shown_tools") or found)
        assert shown <= found, (srv["name"], shown - found)
        tools[srv["name"]] = shown
    for agent in CREW["agents"]:
        available = set().union(*(tools[t] for t in agent["tools"]))
        if "Lifeline" in agent["delegates"]:  # may name the fixer's tools when delegating
            available |= tools["lab-ops"]
        mentioned = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", agent["instructions"]))
        assert mentioned <= available, (agent["name"], mentioned - available)
