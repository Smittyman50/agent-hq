"""Agent HQ lab tools: MCP servers for watching and fixing a homelab, built from
integrations (MikroTik, SNMP, Technitium DNS, Kea DHCP, probes, Proxmox, TrueNAS, Zabbix,
Prometheus/Alertmanager, Graylog, SSH).

One image, any number of instances: each picks its integrations (LABTOOLS_INTEGRATIONS),
read or write access (LABTOOLS_ACCESS) and its own credentials. Run with
`python -m labtools`. See labtools/server.py and docs/labtools.md.
"""
