from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
from fastmcp import Client

from labtools import observe, proxmox, routeros, services, ssh, truenas
from labtools.common import LabError, inventory
from labtools.integrations import INTEGRATIONS
from labtools.server import build, parse

WRITE_TOOLS = {"mikrotik_interface", "dns_flush_cache", "dhcp_lease_delete", "proxmox_guest_power",
               "truenas_service", "zabbix_acknowledge", "alert_silence", "restart_service"}


def data(x):
    return httpx.Response(200, json={"data": x})


# --------------------------------------------------------------------------- MCP surface


async def _tools(server):
    async with Client(server) as c:
        return {t.name: t for t in await c.list_tools()}


@pytest.mark.parametrize("name", list(INTEGRATIONS))
async def test_read_access_only_reads(name):
    tools = await _tools(build([name]))
    assert "capabilities" in tools and len(tools) <= 9  # small enough for local models
    assert all(t.annotations and t.annotations.readOnlyHint for t in tools.values())
    assert not WRITE_TOOLS & set(tools)


async def test_write_access_adds_write_tools_that_ask():
    tools = await _tools(build(parse("all"), write=True))
    assert WRITE_TOOLS <= set(tools)
    for name, t in tools.items():
        if name in WRITE_TOOLS:
            assert t.annotations.readOnlyHint is False and (t.meta or {}).get("agent_hq/approval") == "ask", name
        else:
            assert t.annotations.readOnlyHint is True, name


async def test_any_mix_of_integrations():
    tools = await _tools(build(["proxmox", "zabbix"], write=True, name="my-ops"))
    assert set(tools) == {"capabilities", "proxmox_overview", "proxmox_guests", "proxmox_storage", "proxmox_tasks",
                          "proxmox_backups", "proxmox_guest_power", "zabbix_problems", "zabbix_host",
                          "zabbix_acknowledge"}
    # Network device tools appear with either device integration, for that kind only.
    assert {"list_devices", "device_health", "interfaces"} <= set(await _tools(build(["snmp"])))


def test_integration_names_are_checked():
    assert parse(" Proxmox, dns,proxmox ") == ["proxmox", "dns"]
    assert parse("all") == list(INTEGRATIONS)
    with pytest.raises(SystemExit, match="unknown integration"):
        parse("proxmox,toaster")
    with pytest.raises(SystemExit, match="LABTOOLS_INTEGRATIONS"):
        parse("")


async def test_capabilities_says_what_it_may_touch():
    async with Client(build(["mikrotik", "ssh", "graylog"], write=True, name="fixes")) as c:
        r = (await c.call_tool("capabilities", {})).data
    assert r["server"] == "fixes" and r["access"] == "write"
    assert r["integrations"]["graylog"]["configured"] is True
    assert r["write_targets"]["mikrotik_protected_interfaces"]["crs-core"] == ["sfp-sfpplus1", "ether1"]
    assert r["write_targets"]["ssh_allow_list"]["dhcp1"]["containers"] == ["graylog"]
    async with Client(build(["proxmox"])) as c:
        r = (await c.call_tool("capabilities", {})).data
    assert r["access"] == "read" and "write_targets" not in r


async def test_errors_come_back_as_text_not_exceptions(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_INVENTORY", str(tmp_path / "missing.yaml"))
    inventory.cache_clear()
    async with Client(build(["proxmox"])) as c:
        r = await c.call_tool("proxmox_overview", {})
    assert "isn't in the lab inventory" in r.data["error"]


# --------------------------------------------------------------------------- MikroTik


IFACES = [
    {".id": "*1", "name": "sfp-sfpplus1", "type": "ether", "running": "false", "disabled": "false",
     "rx-error": "0", "tx-error": "0", "rx-drop": "0", "tx-drop": "0", "link-downs": "3"},
    {".id": "*2", "name": "ether2", "type": "ether", "running": "true", "disabled": "false",
     "rx-error": "17", "tx-error": "0", "last-link-up-time": "2026-11-10 09:00:00"},
    {".id": "*3", "name": "ether3", "type": "ether", "running": "false", "disabled": "false"},  # never used
    {".id": "*4", "name": "ether4", "type": "ether", "running": "false", "disabled": "false",
     "last-link-up-time": "2026-11-11 22:00:00"},
]


@respx.mock
async def test_mikrotik_health_finds_down_and_erroring_ports():
    respx.get("https://crs.lab/rest/system/resource").respond(json={
        "board-name": "CRS326", "version": "7.16", "uptime": "3d", "cpu-load": "12",
        "free-memory": "300", "total-memory": "1000", "free-hdd-space": "5", "total-hdd-space": "10"})
    respx.get("https://crs.lab/rest/system/health").respond(json=[{"name": "temperature", "value": "41", "type": "C"}])
    respx.get("https://crs.lab/rest/interface").respond(json=IFACES)
    h = await routeros.health("crs-core")
    assert h["memory_used_pct"] == 70.0 and h["sensors"] == {"temperature": "41C"}
    assert h["interfaces"]["down"] == ["sfp-sfpplus1", "ether4"]  # expected up / was up; ether3 never used
    assert h["interfaces"]["with_errors"] == ["ether2"]
    assert respx.calls[0].request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_interfaces_tool_lists_only_problems():
    respx.get("https://crs.lab/rest/interface").respond(json=IFACES)
    async with Client(build(["mikrotik", "snmp", "dhcp"])) as c:
        r = await c.call_tool("interfaces", {"device": "crs-core"})
    assert [x["name"] for x in r.data["items"]] == ["sfp-sfpplus1", "ether2", "ether4"]
    async with Client(build(["mikrotik", "snmp", "dhcp"])) as c:
        r = await c.call_tool("interfaces", {"device": "nope"})
    assert "no network device named 'nope'" in r.data["error"]


@respx.mock
async def test_wifi_falls_back_to_the_wireless_package():
    respx.get("https://ap.lab/rest/interface/wifi/registration-table").respond(400)
    respx.get("https://ap.lab/rest/interface/wireless/registration-table").respond(json=[
        {"mac-address": "AA", "interface": "wlan1", "signal-strength": "-61dBm", "uptime": "1h"}])
    [c] = await routeros.wifi_clients("ap-office")
    assert c["signal"] == "-61dBm" and c["interface"] == "wlan1"


@respx.mock
async def test_protected_interfaces_cannot_be_disabled():
    async with Client(build(["mikrotik"], write=True)) as c:
        r = await c.call_tool("mikrotik_interface", {"device": "crs-core", "interface": "ether1",
                                                     "action": "bounce", "reason": "flapping"})
    assert "protected" in r.data["error"] and not respx.calls


@respx.mock
async def test_bounce_disables_then_enables(monkeypatch):
    import asyncio

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    respx.get("https://crs.lab/rest/interface", params={"name": "ether4"}).respond(json=[IFACES[3]])
    dis = respx.post("https://crs.lab/rest/interface/disable").respond(json=[])
    en = respx.post("https://crs.lab/rest/interface/enable").respond(json=[])
    async with Client(build(["mikrotik"], write=True)) as c:
        r = await c.call_tool("mikrotik_interface", {"device": "crs-core", "interface": "ether4",
                                                     "action": "bounce", "reason": "port stuck"})
    assert r.data["action"] == "bounce" and dis.called and en.called
    assert json.loads(dis.calls[0].request.content) == {".id": "*4"}


# --------------------------------------------------------------------------- DNS / DHCP


@respx.mock
async def test_dns_stats_and_errors():
    respx.get("http://dns.lab:5380/api/dashboard/stats/get").respond(json={"status": "ok", "response": {
        "stats": {"totalQueries": 1000, "totalServerFailure": 25, "totalNxDomain": 40, "zones": 4},
        "topClients": [{"name": "10.0.0.5", "hits": 400}]}})
    s = await services.dns_stats()
    assert s["server_failure_pct"] == 2.5 and s["top_clients"][0]["hits"] == 400
    respx.get("http://dns.lab:5380/api/cache/flush").respond(json={"status": "error", "errorMessage": "denied"})
    with pytest.raises(LabError, match="denied"):
        await services.dns_flush_cache()


@respx.mock
async def test_kea_pools_and_leases():
    def kea(request):
        cmd = json.loads(request.content)["command"]
        if cmd == "config-get":
            return httpx.Response(200, json=[{"result": 0, "arguments": {"Dhcp4": {
                "subnet4": [{"id": 1, "subnet": "10.0.10.0/24"}],
                "shared-networks": [{"subnet4": [{"id": 2, "subnet": "10.0.20.0/24"}]}]}}}])
        if cmd == "statistic-get-all":
            return httpx.Response(200, json=[{"result": 0, "arguments": {
                "subnet[1].total-addresses": [[100, "t"]], "subnet[1].assigned-addresses": [[95, "t"]],
                "subnet[2].total-addresses": [[50, "t"]], "subnet[2].assigned-addresses": [[10, "t"]],
                "subnet[2].declined-addresses": [[1, "t"]]}}])
        if cmd == "lease4-get-all":
            return httpx.Response(200, json=[{"result": 0, "arguments": {"leases": [
                {"ip-address": "10.0.10.7", "hw-address": "aa:bb", "hostname": "printer", "subnet-id": 1, "state": 0},
                {"ip-address": "10.0.10.8", "hw-address": "cc:dd", "hostname": "nas", "subnet-id": 1, "state": 1}]}}])
        return httpx.Response(200, json=[{"result": 1, "text": f"unknown {cmd}"}])

    respx.post("http://dhcp.lab:8000").mock(side_effect=kea)
    async with Client(build(["mikrotik", "snmp", "dhcp"])) as c:
        r = await c.call_tool("dhcp_status", {})
        leases = await c.call_tool("dhcp_leases", {"search": "print"})
    assert r.data["nearly_full"] == ["10.0.10.0/24"]
    assert r.data["subnets"][1] == {"subnet_id": 2, "subnet": "10.0.20.0/24", "assigned": 10, "total": 50,
                                    "used_pct": 20.0, "declined": 1}
    assert [x["hostname"] for x in leases.data["items"]] == ["printer"]


async def test_probe_hosts_are_checked():
    with pytest.raises(LabError):
        await services.ping("-f 10.0.0.1")
    with pytest.raises(LabError):
        await services.ping("x; reboot")


# --------------------------------------------------------------------------- Proxmox


GUESTS = [{"vmid": 101, "name": "kea", "type": "lxc", "node": "pve1", "status": "running", "cpu": 0.02,
           "mem": 50, "maxmem": 100, "uptime": 90000},
          {"vmid": 102, "name": "graylog", "type": "qemu", "node": "pve2", "status": "stopped"},
          {"vmid": 9000, "name": "tmpl", "type": "qemu", "node": "pve1", "status": "stopped", "template": 1}]
API = "https://pve.lab:8006/api2/json"


@respx.mock
async def test_proxmox_overview_and_guests():
    respx.get(f"{API}/cluster/status").mock(return_value=data([{"type": "cluster", "name": "lab", "quorate": 1}]))
    respx.get(f"{API}/nodes").mock(return_value=data([{"node": "pve1", "status": "online", "cpu": 0.25,
                                                         "mem": 8, "maxmem": 32, "uptime": 200000}]))
    respx.get(f"{API}/cluster/resources", params={"type": "vm"}).mock(return_value=data(GUESTS))
    o = await proxmox.overview()
    assert o["quorate"] and o["nodes"][0] == {"node": "pve1", "status": "online", "cpu_pct": 25.0,
                                              "memory_pct": 25.0, "uptime": "2d 7h"}
    async with Client(build(["proxmox"])) as c:
        r = await c.call_tool("proxmox_guests", {"status": "stopped"})
    assert [g["name"] for g in r.data["items"]] == ["graylog"]  # the template isn't listed
    assert respx.calls[0].request.headers["authorization"] == "PVEAPIToken=watch@pve!mon=abc"


@respx.mock
async def test_stale_backups():
    now = time.time()
    respx.get(f"{API}/nodes").mock(return_value=data([{"node": "pve1", "status": "online"},
                                                        {"node": "pve2", "status": "online"}]))
    respx.get(f"{API}/storage").mock(return_value=data([{"storage": "pbs", "content": "backup", "shared": 1},
                                                          {"storage": "local-lvm", "content": "images"}]))
    content = respx.get(f"{API}/nodes/pve1/storage/pbs/content").mock(return_value=data([
        {"vmid": 101, "ctime": now - 3600}, {"vmid": 102, "ctime": now - 5 * 86400}]))
    respx.get(f"{API}/cluster/resources", params={"type": "vm"}).mock(return_value=data(GUESTS))
    r = await proxmox.stale_backups(36)
    assert [s["vmid"] for s in r["stale"]] == [102] and r["guests"] == 2
    assert content.call_count == 1  # shared storage is read once


@respx.mock
async def test_power_waits_for_the_task(monkeypatch):
    import asyncio

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    import itertools

    states = itertools.chain(["stopped"], itertools.repeat("running"))
    respx.get(f"{API}/cluster/resources", params={"type": "vm"}).mock(
        side_effect=lambda r: data([{**GUESTS[1], "status": next(states)}, GUESTS[2]]))
    start = respx.post(f"{API}/nodes/pve2/qemu/102/status/start").mock(return_value=data("UPID:pve2:1"))
    respx.get(f"{API}/nodes/pve2/tasks/UPID:pve2:1/status").mock(
        side_effect=[data({"status": "running"}), data({"status": "stopped", "exitstatus": "OK"})])
    r = await proxmox.power(102, "start")
    assert start.called and r["before"] == "stopped" and r["after"] == "running" and r["task_result"] == "OK"
    with pytest.raises(LabError, match="template"):
        await proxmox.power(9000, "start")
    with pytest.raises(LabError, match="action must be"):
        await proxmox.power(102, "destroy")


# --------------------------------------------------------------------------- TrueNAS


async def test_truenas_overview_and_jobs(monkeypatch):
    replies = {
        "system.info": {"hostname": "nas", "version": "25.04.2", "uptime": "10 days"},
        "pool.query": [{"name": "tank", "status": "DEGRADED", "healthy": False,
                        "allocated": 600, "size": 1000, "scan": {"state": "FINISHED"}}],
        "alert.list": [{"level": "CRITICAL", "formatted": "Pool tank state is DEGRADED", "dismissed": False},
                       {"level": "INFO", "formatted": "old", "dismissed": True}],
        "replication.query": [{"name": "to-offsite", "enabled": True, "state": {"state": "ERROR"}},
                              {"name": "ok", "enabled": True, "state": {"state": "FINISHED"}}],
        "pool.snapshottask.query": [{"dataset": "tank/vm", "enabled": True, "state": {"state": "FINISHED"}}],
        "cloudsync.query": [{"description": "b2", "enabled": True, "job": {"state": "FAILED"}}],
    }

    async def fake_call(method, *params):
        return replies[method]

    monkeypatch.setattr(truenas, "call", fake_call)
    o = await truenas.overview()
    assert o["pools"][0]["used_pct"] == 60.0 and [a["level"] for a in o["alerts"]] == ["CRITICAL"]
    j = await truenas.jobs()
    assert j["replication"] == [{"name": "to-offsite", "state": "ERROR", "enabled": True}]
    assert j["snapshot_tasks"] == [] and j["cloud_sync"][0]["state"] == "FAILED"


# --------------------------------------------------------------------------- observability


@respx.mock
async def test_zabbix_problems_with_hosts():
    def zbx(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer z"
        result = {"problem.get": [{"eventid": "9", "objectid": "77", "name": "Link down on port 3", "severity": "4",
                                   "clock": "1762900000", "acknowledged": "0", "suppressed": "0",
                                   "tags": [{"tag": "domain", "value": "network"}]}],
                  "trigger.get": [{"triggerid": "77", "hosts": [{"name": "crs-core"}]}]}[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

    respx.post("http://zbx.lab/api_jsonrpc.php").mock(side_effect=zbx)
    [p] = await observe.zabbix_problems(min_severity=3)
    assert p["host"] == "crs-core" and p["severity"] == "high" and p["tags"] == {"domain": "network"}
    assert json.loads(respx.calls[0].request.content)["params"]["severities"] == [3, 4, 5]


@respx.mock
async def test_prometheus_query_is_trimmed():
    respx.get("http://prom.lab:9090/api/v1/query").respond(json={"status": "success", "data": {
        "resultType": "vector", "result": [{"metric": {"instance": f"h{i}"}, "value": [0, "0"]} for i in range(50)]}})
    r = await observe.prom_query("up == 0", limit=5)
    assert len(r["series"]) == 5 and r["count"] == 50 and "note" in r


@respx.mock
async def test_silences_are_bounded():
    route = respx.post("http://am.lab:9093/api/v2/silences").respond(json={"silenceID": "s1"})
    r = await observe.silence({"alertname": "HostDown"}, minutes=100000, comment="maintenance")
    assert r["minutes"] == 24 * 60 and route.called
    with pytest.raises(LabError):
        await observe.silence({}, 10, "x")


@respx.mock
async def test_graylog_search_falls_back_to_the_old_api():
    respx.post("http://graylog.lab:9000/api/search/messages").respond(404)
    respx.get("http://graylog.lab:9000/api/search/universal/relative").respond(json={"messages": [
        {"message": {"timestamp": "t", "source": "crs-core", "level": 3, "message": "link down"}}]})
    r = await observe.graylog_search("source:crs-core", minutes=30)
    assert r["messages"] == [{"timestamp": "t", "source": "crs-core", "level": 3, "message": "link down"}]
    assert respx.calls[-1].request.headers["x-requested-by"] == "agent-hq"


# --------------------------------------------------------------------------- SSH


def test_ssh_allow_list_and_commands():
    h, kind = ssh.allowed("dhcp1", "isc-kea-dhcp4-server")
    assert kind == "service"
    assert ssh.commands(kind, "isc-kea-dhcp4-server") == (
        "sudo -n systemctl restart isc-kea-dhcp4-server", "systemctl is-active isc-kea-dhcp4-server")
    assert ssh.allowed("dhcp1", "graylog")[1] == "container"
    with pytest.raises(LabError, match="allow-list"):
        ssh.allowed("dhcp1", "sshd")
    with pytest.raises(LabError, match="no SSH host"):
        ssh.allowed("other", "x")
