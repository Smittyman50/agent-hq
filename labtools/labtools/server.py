"""A lab tools MCP server, assembled from configuration.

    LABTOOLS_INTEGRATIONS  which integrations to expose, comma-separated, or "all"
                           (mikrotik, snmp, dns, dhcp, probes, proxmox, truenas, zabbix,
                           prometheus, graylog, ssh; see integrations.py)
    LABTOOLS_ACCESS        read (default): read tools only
                           write: read tools plus write tools, each asking first in Agent HQ
    LABTOOLS_NAME          this instance's name (default labtools)
    LABTOOLS_PORT          port for http://0.0.0.0:<port>/mcp (default 8820)
    LAB_INVENTORY          the inventory file (default /etc/labtools/inventory.yaml)

Run several instances from the one image, each with its own integrations, access and
credentials, and give each agent the instances it should have (docs/labtools.md).
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from . import truenas
from .common import READ_ONLY, env, failsafe, section
from .integrations import INTEGRATIONS, register_network_devices


def parse(spec: str) -> list[str]:
    names = [n.strip().lower() for n in spec.replace(";", ",").split(",") if n.strip()]
    if names == ["all"]:
        return list(INTEGRATIONS)
    unknown = [n for n in names if n not in INTEGRATIONS]
    if unknown:
        raise SystemExit(f"unknown integration(s): {', '.join(unknown)}. Known: {', '.join(INTEGRATIONS)}, or 'all'")
    if not names:
        raise SystemExit(f"set LABTOOLS_INTEGRATIONS (comma-separated: {', '.join(INTEGRATIONS)}, or 'all')")
    return list(dict.fromkeys(names))


def build(integrations: list[str], write: bool = False, name: str = "labtools") -> FastMCP:
    summaries = "; ".join(f"{n}: {INTEGRATIONS[n].summary}" for n in integrations)
    mcp = FastMCP(name, instructions=(
        f"Lab tools ({'read and write' if write else 'read-only'}). {summaries}. "
        "Call capabilities to see what is configured. "
        + ("Write tools change things and ask the user first: check state before and after, "
           "one change at a time." if write else "Nothing here changes anything.")))

    @mcp.tool(annotations=READ_ONLY)
    @failsafe
    async def capabilities() -> dict[str, Any]:
        """What this server is set up for: its integrations and whether each has an inventory
        entry, read or write access and, for write access, what it may act on (protected
        interfaces, SSH allow-lists, TrueNAS services)."""
        out: dict[str, Any] = {"server": name, "access": "write" if write else "read", "integrations": {
            n: {"configured": not INTEGRATIONS[n].inventory or bool(section(INTEGRATIONS[n].inventory)),
                "summary": INTEGRATIONS[n].summary} for n in integrations}}
        if write:
            targets: dict[str, Any] = {}
            if "mikrotik" in integrations:
                targets["mikrotik_protected_interfaces"] = {
                    d["name"]: d.get("protected_interfaces", []) for d in section("mikrotik") or []}
            if "ssh" in integrations:
                targets["ssh_allow_list"] = {h["name"]: {"services": h.get("services", []),
                                                         "containers": h.get("containers", [])}
                                             for h in section("ssh_hosts") or []}
            if "truenas" in integrations:
                targets["truenas_services"] = list(truenas.SERVICES)
            out["write_targets"] = targets
        return out

    kinds = {k for k in ("mikrotik", "snmp") if k in integrations}
    if kinds:
        register_network_devices(mcp, kinds)
    for n in integrations:
        INTEGRATIONS[n].register(mcp, write)
    return mcp


def from_env() -> FastMCP:
    access = env("LABTOOLS_ACCESS", "read").lower()
    if access not in ("read", "write"):
        raise SystemExit("LABTOOLS_ACCESS must be read or write")
    return build(parse(env("LABTOOLS_INTEGRATIONS")), write=access == "write", name=env("LABTOOLS_NAME", "labtools"))


def main() -> None:
    mcp = from_env()
    mcp.run(transport="http", host="0.0.0.0", port=int(env("LABTOOLS_PORT", "8820")))


if __name__ == "__main__":
    main()
