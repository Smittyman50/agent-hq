# Lab tools

`labtools/` is one image of MCP servers for the systems in a homelab. You run as many **instances** as you like. Each one exposes the **integrations** you choose, with **read** or **write** access, using **its own credentials**. Then you give each agent the instances it should have.

The framework fixes no agents and no roles. You could build:
- one agent per system,
- a network agent and a systems agent sharing a monitoring instance,
- a single "ops" agent with everything,
- watchers that only read, plus a fixer that may write.

[examples/homelab-crew.md](examples/homelab-crew.md) is one worked example.

## Integrations

| Integration | Read tools | Write tools (`LABTOOLS_ACCESS=write`) | Inventory section |
|---|---|---|---|
| `mikrotik` | `neighbors`, `device_log`, `wifi_clients` (+ the device tools below) | `mikrotik_interface`: enable / disable / bounce; refused for protected interfaces | `mikrotik` |
| `snmp` | the device tools below (read-only) | | `snmp` |
| *either of the two* | `list_devices`, `device_health`, `interfaces` | | |
| `dns` | `dns_status`, `dns_lookup` (Technitium) | `dns_flush_cache` | `technitium` |
| `dhcp` | `dhcp_status`, `dhcp_leases` (Kea) | `dhcp_lease_delete` | `kea` |
| `probes` | `ping`, `check_port` | | |
| `proxmox` | `proxmox_overview`, `proxmox_guests`, `proxmox_storage`, `proxmox_tasks`, `proxmox_backups` | `proxmox_guest_power`: start / shutdown / reboot / stop / resume a guest | `proxmox` |
| `truenas` | `truenas_overview`, `truenas_datasets`, `truenas_jobs` | `truenas_service`: start / stop / restart a service | `truenas` |
| `zabbix` | `zabbix_problems`, `zabbix_host` | `zabbix_acknowledge` | `zabbix` |
| `prometheus` | `prometheus_query`, `prometheus_alerts`, `prometheus_targets_down`, `alert_silences` | `alert_silence` (5 min to 24 h) | `prometheus` |
| `graylog` | `log_search`, `log_events` | | `graylog` |
| `ssh` | `service_status` | `restart_service`, only for allow-listed services / containers | `ssh_hosts` |

Every instance also has `capabilities`, which reports its integrations, its access and what it may act on.

**How tools are marked:**
- **Read tools** carry `readOnlyHint`. They don't trigger webhook caution, and they start ticked as *Safe in tests*.
- **Write tools** are marked as changing things and suggest **Ask**. Agent HQ waits for your approval unless your rules say otherwise, and work started by a webhook asks regardless.

Each tool does one specific read or change and returns a short answer, so small local models handle them well. An instance with a few integrations has 5 to 20 tools. Use **Shown** on the Tools page to trim further.

## Configuring an instance

| Variable | Meaning |
|---|---|
| `LABTOOLS_INTEGRATIONS` | comma-separated integrations, or `all` |
| `LABTOOLS_ACCESS` | `read` (default) or `write` (read tools plus write tools) |
| `LABTOOLS_NAME` | the instance's name: its MCP name, and the author of changes it makes (e.g. Alertmanager silences) |
| `LABTOOLS_PORT` | default 8820: `http://<host>:8820/mcp` |
| `LAB_INVENTORY` | default `/etc/labtools/inventory.yaml` |
| `LAB_CA_FILE` | CA certificate for HTTPS (e.g. your step-ca root); per system `verify_tls` in the inventory overrides it |
| `LABTOOLS_SSH_KEY`, `LABTOOLS_SSH_KNOWN_HOSTS`, `LABTOOLS_SSH_USER` | the `ssh` integration's key, known_hosts file and default user (`agent-hq`) |
| `LAB_MAX_ITEMS`, `LAB_TIMEOUT` | longest list returned (40) and per-request timeout in seconds (15) |

`deploy/lab/compose.example.yml` runs four instances. Rename, merge or split them however you like:

| Instance | Integrations | Access |
|---|---|---|
| `lab-network` | mikrotik, snmp, dns, dhcp, probes | read |
| `lab-systems` | proxmox, truenas | read |
| `lab-monitoring` | zabbix, prometheus, graylog | read |
| `lab-ops` | all | write |

It also runs Gitea's official MCP server in read-only mode. Use other existing MCP servers the same way: register them, tick **Only reads** if they can't change anything, and untick tools you don't need under **Shown**.

```bash
cp deploy/lab/inventory.example.yaml deploy/lab/inventory.yaml        # describe your lab
cp deploy/lab/instance.env.example deploy/lab/lab-network.env         # one per instance
docker compose -f docker-compose.yml -f deploy/lab/compose.example.yml up -d --build
```

Register each instance under **Settings → Tools** (HTTP, `http://lab-network:8820/mcp`, untick *Prefix tool names*) and tick it on the agents that should have it. **Test connection** lists its tools.

## The inventory

`deploy/lab/inventory.yaml` is shared by all instances. It lists what exists and holds no secrets:
- devices (`mikrotik`, `snmp`) and service addresses (`technitium`, `kea`, `proxmox`, `truenas`, `prometheus`, `zabbix`, `graylog`)
- `ssh_hosts`: the services and containers that may be restarted on each host

It also sets guards that write instances respect:
- `protected_interfaces` per MikroTik device (uplinks, management) can never be disabled or bounced.
- `expected_up` marks interfaces that should always be up.

Name devices and hosts the way you talk about them; agents use those names.

## Credentials

Each instance reads credentials under the same variable names from its own `deploy/lab/<instance>.env` (see `instance.env.example`; the files are git-ignored). Give read instances read-only credentials, and a write instance separate, narrow write credentials. An agent with only read instances then can't change anything, even if a webhook payload talks it into trying.

| Integration | Read access | Write access |
|---|---|---|
| `proxmox` | `PROXMOX_TOKEN` for a user with **PVEAuditor** on `/` | a token with **VM.PowerMgmt** on the guests it may restart (a pool is easiest) |
| `truenas` | `TRUENAS_API_KEY`, **Read-only Administrator** role | a user allowed to restart services |
| `mikrotik` | `MIKROTIK_USER`/`MIKROTIK_PASSWORD`, group with `read,api,rest-api` (enable `www-ssl`) | group with `read,write,api,rest-api` |
| `snmp` | `SNMP_COMMUNITY` (v2c, read-only) or `SNMP_USER`/`SNMP_AUTH_KEY`/`SNMP_PRIV_KEY` (v3, SHA/AES) | |
| `dns` | `TECHNITIUM_TOKEN`, user with **View** on Dashboard | user with **Modify** on Cache |
| `dhcp` | `KEA_USER`/`KEA_PASSWORD` if the Control Agent uses basic auth; `lease4-get-all` needs the `lease_cmds` hook | same; deleting leases also needs `lease_cmds` |
| `zabbix` | `ZABBIX_TOKEN`, user group with read access to your host groups | a user that may acknowledge problems |
| `prometheus` | optional `PROMETHEUS_TOKEN` / `ALERTMANAGER_TOKEN` | Alertmanager: creates silences |
| `graylog` | `GRAYLOG_TOKEN`, **Reader** role plus event read permission | |
| `ssh` | | a key in `deploy/lab/ssh/` (see its README), `known_hosts`, and on each host a sudoers line for exactly the allowed restarts |

Sudoers example: `agent-hq ALL=(root) NOPASSWD: /usr/bin/systemctl restart isc-kea-dhcp4-server`.

## Guards

On top of your approval of each write call:
- **MikroTik:** protected interfaces can't be disabled or bounced.
- **SSH:** only allow-listed names run, through fixed commands (`systemctl restart`, `docker restart`), and unknown host keys are refused.
- **Alertmanager:** silences last 5 minutes to 24 hours.
- **Proxmox:** power actions on guests only, never on nodes, and nothing is ever deleted.
- **Probes:** host names are checked, so they can't carry command options.

## Adding an integration

In `labtools/labtools/integrations.py`:
1. Write `register_<name>(mcp, write)`. Decorate read tools with `reads(mcp)` and write tools, registered only when `write` is true, with `changes(mcp)`.
2. Add it to `INTEGRATIONS` with a one-line summary and its inventory section.

Put the API client in its own module, as with `proxmox.py`, and test it against recorded responses (see `labtools/tests`). Keep tools few and answers short.

## Alerts

To have your systems wake agents when something happens, see [lab-alerts.md](lab-alerts.md).
