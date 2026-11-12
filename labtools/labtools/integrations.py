"""The integrations a lab tools server can expose. Each one adds read tools, and write tools
when the server runs with LABTOOLS_ACCESS=write.

Read tools carry readOnlyHint. Write tools are marked as changing things and suggest that
Agent HQ asks before each call (`agent_hq/approval: ask`); your approval rules decide in the
end. Which integrations a server exposes, and whether it may write, is configuration: one
image serves any mix (docs/labtools.md).

To add an integration: write a `register_<name>(mcp, write)` function below and list it in
INTEGRATIONS. Keep tools few, specific and well described, with short answers: small local
models choose better from a short list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from . import observe, proxmox, routeros, services, snmp, ssh, truenas
from .common import ASK, CHANGES, READ_ONLY, LabError, clip, failsafe, section
from .common import device as inventory_device


def reads(mcp: FastMCP) -> Callable:
    return lambda fn: mcp.tool(annotations=READ_ONLY)(failsafe(fn))


def changes(mcp: FastMCP) -> Callable:
    return lambda fn: mcp.tool(annotations=CHANGES, meta=ASK)(failsafe(fn))


# --------------------------------------------------------------------------- network devices


def register_network_devices(mcp: FastMCP, kinds: set[str]) -> None:
    """Device-level tools shared by the mikrotik and snmp integrations, answering for
    whichever of the two are enabled."""
    read = reads(mcp)

    def kind_of(name: str) -> str:
        for kind in ("mikrotik", "snmp"):
            if kind in kinds and any(d.get("name", "").lower() == name.strip().lower()
                                     for d in section(kind) or []):
                return kind
        raise LabError(f"no network device named '{name}'; see list_devices")

    @read
    async def list_devices() -> dict[str, Any]:
        """The network devices in the lab inventory: name, kind (mikrotik/snmp), role and host."""
        rows = []
        if "mikrotik" in kinds:
            rows += [{"name": d["name"], "kind": "mikrotik", "role": d.get("role", ""), "host": d["host"]}
                     for d in section("mikrotik") or []]
        if "snmp" in kinds:
            rows += [{"name": d["name"], "kind": f"snmp ({d.get('kind', 'switch')})", "role": d.get("role", ""),
                      "host": d["host"]} for d in section("snmp") or []]
        return {"devices": rows}

    @read
    async def device_health(device: str) -> dict[str, Any]:
        """One device's health: uptime, CPU/memory (MikroTik), temperatures, and which
        interfaces are down or show errors. Use this first when something seems wrong."""
        if kind_of(device) == "mikrotik":
            return await routeros.health(device)
        return await snmp.health(device)

    @read
    async def interfaces(device: str, only_problems: bool = True, limit: int = 40) -> dict[str, Any]:
        """A device's interfaces with link state and error/drop counters (since boot).
        only_problems=True lists just interfaces that are down where they should be up, or
        have errors; set False to see all."""
        if kind_of(device) == "mikrotik":
            d = inventory_device("mikrotik", device)
            rows = await routeros.interfaces(device)
            if only_problems:
                expected = set(d.get("expected_up", []))
                rows = [r for r in rows if routeros.is_down(r, expected) or routeros.has_errors(r)]
        else:
            rows = await snmp.interfaces(device)
            if only_problems:
                rows = [r for r in rows if (r["admin"] == "up" and r["oper"] != "up")
                        or r["in_errors"] or r["out_errors"]]
        return clip(rows, limit)


# --------------------------------------------------------------------------- MikroTik


def register_mikrotik(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def neighbors(device: str) -> dict[str, Any]:
        """What a MikroTik device sees on its ports (MNDP/CDP/LLDP): identity, address,
        interface, platform. Useful to find which port a device hangs off."""
        rows = await routeros.get(device, "ip/neighbor")
        return clip([{"interface": x.get("interface"), "identity": x.get("identity"),
                      "address": x.get("address", ""), "mac": x.get("mac-address"),
                      "platform": x.get("platform", ""), "board": x.get("board", "")} for x in rows])

    @read
    async def device_log(device: str, contains: str = "", limit: int = 30) -> dict[str, Any]:
        """Recent log lines from a MikroTik device, newest last. `contains` filters by text
        or topic (e.g. 'link', 'dhcp', 'error', 'interface')."""
        rows = await routeros.get(device, "log")
        q = contains.strip().lower()
        rows = [{"time": x.get("time"), "topics": x.get("topics"), "message": x.get("message")} for x in rows
                if not q or q in f"{x.get('topics', '')} {x.get('message', '')}".lower()]
        n = max(1, min(int(limit), 200))
        return {"items": rows[-n:], "count": len(rows)}

    @read
    async def wifi_clients(device: str) -> dict[str, Any]:
        """Wireless clients on a MikroTik access point: MAC, SSID, signal, uptime, rates."""
        return clip(await routeros.wifi_clients(device))

    if not write:
        return
    change = changes(mcp)

    @change
    async def mikrotik_interface(device: str, interface: str, action: str, reason: str) -> dict[str, Any]:
        """Enable, disable, or bounce (disable, wait 5 s, enable) one interface on a MikroTik
        device, reporting its state before and after. Refused for the device's protected
        interfaces (uplinks, management). `reason`: one line for the approval card."""
        action = action.strip().lower()
        if action not in ("enable", "disable", "bounce"):
            raise LabError("action must be enable, disable or bounce")
        d = inventory_device("mikrotik", device)
        if interface in d.get("protected_interfaces", []) and action != "enable":
            raise LabError(f"{interface} on {d['name']} is protected (uplink or management): "
                           "disabling it could cut the lab off. This has to be done by hand.")
        rows = await routeros.get(d["name"], "interface", params={"name": interface})
        if not rows:
            raise LabError(f"{d['name']} has no interface '{interface}'")
        rid = rows[0][".id"]
        before = routeros.interface_row(rows[0])
        if action in ("disable", "bounce"):
            await routeros.post(d["name"], "interface/disable", {".id": rid})
        if action == "bounce":
            await asyncio.sleep(5)
        if action in ("enable", "bounce"):
            await routeros.post(d["name"], "interface/enable", {".id": rid})
        await asyncio.sleep(3)
        after = routeros.interface_row((await routeros.get(d["name"], "interface", params={"name": interface}))[0])
        return {"device": d["name"], "interface": interface, "action": action,
                "before": {k: before[k] for k in ("running", "disabled")},
                "after": {k: after[k] for k in ("running", "disabled", "last_link_up")}}


def register_snmp(mcp: FastMCP, write: bool) -> None:
    """SNMP is read-only; its tools come from the shared network-device tools."""


# --------------------------------------------------------------------------- DNS, DHCP, probes


def register_dns(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def dns_status() -> dict[str, Any]:
        """Technitium DNS over the last hour: queries, server failures, NXDOMAIN, refused,
        blocked, cache size and top clients. A rising server_failure_pct means upstream or
        zone trouble."""
        return await services.dns_stats()

    @read
    async def dns_lookup(name: str, record_type: str = "A", server: str = "") -> dict[str, Any]:
        """Resolve a name against Technitium (or another `server`) and time it: rcode,
        answers, milliseconds. Use it to prove DNS works or doesn't."""
        return await services.dns_lookup(name, record_type, server)

    if write:
        @changes(mcp)
        async def dns_flush_cache(reason: str) -> dict[str, Any]:
            """Flush Technitium's DNS cache (e.g. after a stale or poisoned answer)."""
            return await services.dns_flush_cache()


def register_dhcp(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def dhcp_status() -> dict[str, Any]:
        """Kea DHCPv4 pool use per subnet: assigned of total addresses, % used, declined.
        A pool above ~90% is about to run out."""
        rows = await services.dhcp_pools()
        return {"subnets": rows, "nearly_full": [r["subnet"] for r in rows if (r["used_pct"] or 0) >= 90]}

    @read
    async def dhcp_leases(search: str = "", limit: int = 30) -> dict[str, Any]:
        """Kea DHCPv4 leases, filtered by IP, MAC or hostname text. Needs the lease_cmds hook."""
        return clip(await services.dhcp_leases(search), limit)

    if write:
        @changes(mcp)
        async def dhcp_lease_delete(ip: str, reason: str) -> dict[str, Any]:
            """Delete one Kea DHCPv4 lease (e.g. a stuck or conflicting address) so it can be
            handed out again."""
            return await services.dhcp_lease_delete(ip)


def register_probes(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def ping(host: str, count: int = 4) -> dict[str, Any]:
        """Ping a host from the lab tools container: loss % and round-trip times."""
        return await services.ping(host, count)

    @read
    async def check_port(host: str, port: int) -> dict[str, Any]:
        """Whether a TCP port answers (e.g. 53 on the DNS server, 8006 on Proxmox), and how fast."""
        return await services.check_port(host, port)


# --------------------------------------------------------------------------- Proxmox, TrueNAS


def register_proxmox(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def proxmox_overview() -> dict[str, Any]:
        """Proxmox cluster health: quorum, and each node's status, CPU %, memory % and uptime."""
        return await proxmox.overview()

    @read
    async def proxmox_guests(status: str = "", search: str = "", limit: int = 40) -> dict[str, Any]:
        """VMs and containers: VMID, name, type, node, status, CPU/memory %, uptime, HA state.
        Filter by status ('running', 'stopped') and/or name text or VMID."""
        rows = [g for g in await proxmox.guests() if not g["template"]]
        if status:
            rows = [g for g in rows if g["status"] == status]
        if search:
            q = search.lower()
            rows = [g for g in rows if q in (g["name"] or "").lower() or q == str(g["vmid"])]
        return clip(rows, limit)

    @read
    async def proxmox_storage() -> dict[str, Any]:
        """Every storage on every node: status, type, used % and size. Over ~85% needs attention."""
        rows = await proxmox.storage()
        return {"storage": rows,
                "nearly_full": [f"{r['storage']}@{r['node']}" for r in rows if (r["used_pct"] or 0) >= 85]}

    @read
    async def proxmox_tasks(hours: float = 24, failed_only: bool = True) -> dict[str, Any]:
        """Recent cluster tasks (backups, migrations, replication, starts/stops...). By default
        only ones that didn't end OK, newest first."""
        return clip(await proxmox.tasks(hours, failed_only))

    @read
    async def proxmox_backups(older_than_hours: float = 36) -> dict[str, Any]:
        """Guests whose newest backup is older than `older_than_hours`, or that have none,
        looking at every storage that holds backups (PBS included)."""
        return await proxmox.stale_backups(older_than_hours)

    if write:
        @changes(mcp)
        async def proxmox_guest_power(vmid: int, action: str, reason: str) -> dict[str, Any]:
            """Start, shut down (clean), reboot, stop (hard power-off) or resume a Proxmox VM
            or container, wait for the task, and report the status before and after. Prefer
            'shutdown' or 'reboot' over 'stop'. Never acts on nodes or deletes anything.
            `reason`: one line for the approval card."""
            return await proxmox.power(vmid, action.strip().lower())


def register_truenas(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def truenas_overview() -> dict[str, Any]:
        """TrueNAS health: version, pools (status, healthy, used %, scrub state) and active alerts."""
        return await truenas.overview()

    @read
    async def truenas_datasets(pool: str = "", limit: int = 25) -> dict[str, Any]:
        """Datasets by how full they are: used / available GiB and %. Optionally one pool."""
        return clip(await truenas.datasets(pool), limit)

    @read
    async def truenas_jobs() -> dict[str, Any]:
        """Replication, periodic snapshot and cloud sync tasks whose last run didn't succeed."""
        return await truenas.jobs()

    if write:
        @changes(mcp)
        async def truenas_service(service: str, action: str, reason: str) -> dict[str, Any]:
            """Start, stop or restart a TrueNAS service (cifs = SMB, nfs, ssh, iscsitarget, ups,
            snmp, ftp) and report its state before and after."""
            if service not in truenas.SERVICES:
                raise LabError(f"service must be one of {', '.join(truenas.SERVICES)}")
            return await truenas.service(service, action.strip().lower())


# --------------------------------------------------------------------------- monitoring


def register_zabbix(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def zabbix_problems(min_severity: int = 2, host: str = "", limit: int = 30) -> dict[str, Any]:
        """Open Zabbix problems, newest first: problem, host, severity, since, acknowledged,
        tags. min_severity: 0 not classified .. 2 warning .. 4 high .. 5 disaster."""
        return clip(await observe.zabbix_problems(min_severity, host, limit), limit)

    @read
    async def zabbix_host(host: str) -> dict[str, Any]:
        """A Zabbix host: monitored or not, maintenance, and each interface's availability
        (agent/SNMP) with its last error."""
        return await observe.zabbix_host(host)

    if write:
        @changes(mcp)
        async def zabbix_acknowledge(eventid: str, message: str, close: bool = False) -> dict[str, Any]:
            """Acknowledge a Zabbix problem with a message (what was found or done), and
            optionally close it (only if its trigger allows manual close)."""
            return await observe.zabbix_acknowledge(eventid, message, close)


def register_prometheus(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def prometheus_query(promql: str, limit: int = 30) -> dict[str, Any]:
        """Run an instant PromQL query, e.g. `up == 0`,
        `node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.1`,
        `rate(node_network_receive_errs_total[5m]) > 0`. Aggregate to keep answers short."""
        return await observe.prom_query(promql, limit)

    @read
    async def prometheus_alerts() -> dict[str, Any]:
        """Alerts Prometheus is evaluating as pending or firing, with labels and summary."""
        return clip(await observe.prom_alerts())

    @read
    async def prometheus_targets_down() -> dict[str, Any]:
        """Scrape targets that aren't up, with the scrape error: often the first sign a host
        or exporter is gone."""
        return clip(await observe.prom_targets_down())

    @read
    async def alert_silences() -> dict[str, Any]:
        """Active Alertmanager silences: what's muted, until when, why and by whom."""
        return clip(await observe.silences())

    if write:
        @changes(mcp)
        async def alert_silence(matchers: dict[str, str], minutes: int, comment: str) -> dict[str, Any]:
            """Silence Alertmanager alerts matching these labels for 5 minutes to 24 hours, e.g.
            while a fix settles or for a known, accepted condition. Say why in `comment`."""
            return await observe.silence(matchers, minutes, comment)


def register_graylog(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def log_search(query: str, minutes: int = 60, limit: int = 30) -> dict[str, Any]:
        """Search Graylog, newest first. Lucene syntax, e.g. `source:crs-core AND message:link`,
        `level:<=3`, `"authentication failure"`."""
        return await observe.graylog_search(query, minutes, limit)

    @read
    async def log_events(minutes: int = 60, limit: int = 30) -> dict[str, Any]:
        """Graylog events (alerts from event definitions) in the last `minutes`."""
        return clip(await observe.graylog_events(minutes, limit), limit)


# --------------------------------------------------------------------------- SSH


def register_ssh(mcp: FastMCP, write: bool) -> None:
    read = reads(mcp)

    @read
    async def service_status(host: str, name: str) -> dict[str, Any]:
        """Whether an allow-listed systemd service or Docker container is running on a lab
        host (see capabilities for the allow-list)."""
        return await ssh.status(host, name)

    if write:
        @changes(mcp)
        async def restart_service(host: str, name: str, reason: str) -> dict[str, Any]:
            """Restart an allow-listed systemd service or Docker container on a lab host over
            SSH, with its state before and after. Only names on that host's allow-list work."""
            return await ssh.restart(host, name)


# --------------------------------------------------------------------------- the registry


@dataclass(frozen=True)
class Integration:
    name: str
    summary: str
    inventory: str  # the inventory section it needs
    register: Callable[[FastMCP, bool], None]


INTEGRATIONS: dict[str, Integration] = {i.name: i for i in (
    Integration("mikrotik", "MikroTik RouterOS 7 devices over REST: health, interfaces, neighbours, logs, Wi-Fi clients; "
                            "write: enable/disable/bounce an unprotected interface", "mikrotik", register_mikrotik),
    Integration("snmp", "Switches over SNMP (read-only): health and interfaces", "snmp", register_snmp),
    Integration("dns", "Technitium DNS: stats, timed lookups; write: flush the cache", "technitium", register_dns),
    Integration("dhcp", "Kea DHCPv4: pool use, leases; write: delete a lease", "kea", register_dhcp),
    Integration("probes", "ping and TCP port checks from the container", "", register_probes),
    Integration("proxmox", "Proxmox VE: cluster, guests, storage, tasks, stale backups; write: guest power",
                "proxmox", register_proxmox),
    Integration("truenas", "TrueNAS SCALE: pools, alerts, datasets, replication/snapshot/cloud sync jobs; "
                           "write: start/stop/restart a service", "truenas", register_truenas),
    Integration("zabbix", "Zabbix: problems and host availability; write: acknowledge a problem", "zabbix",
                register_zabbix),
    Integration("prometheus", "Prometheus: queries, alerts, targets down; Alertmanager silences; write: add a silence",
                "prometheus", register_prometheus),
    Integration("graylog", "Graylog: log search and events (read-only)", "graylog", register_graylog),
    Integration("ssh", "allow-listed systemd services / Docker containers: status; write: restart",
                "ssh_hosts", register_ssh),
)}
