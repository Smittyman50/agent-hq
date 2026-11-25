# Tools

Adding tools from MCP servers and the catalog, the bundled tool servers, the homelab tool servers and tool approvals. Back to the [overview](../README.md).

## Adding tools

Register MCP servers on the **Settings → Tools** page:
- **HTTP:** a URL like `http://perceptor:8811/mcp`
- **stdio:** a command plus arguments, which is launched **on whichever worker runs the agent**, so it must be installed there

Click **Test connection** to list the server's tools (this connects from the API host), then tick the server on any agent, or tick **All agents** on the server to give it to every agent (sub-agents included). Desktops and workbenches aren't registered here: create them under **Settings → Desktops & workbenches** (see [Desktops and workbenches](desktops-and-workbenches.md#desktops-and-workbenches)). Tool names are prefixed with the server name by default (`proxmox_list_vms`) so they never collide.

The worker image is a slim Python image without `npx` or `uvx`, so a stdio server you'd normally start with those commands won't run there. Install it from the catalog as a [package server](#package-servers) instead (its own container, served over HTTP), or run it as a container yourself and register it over HTTP, the way the web reader below does.

**Headers.** An HTTP server can get headers, one `Name: value` per line, for example `Authorization: Bearer …` for a hosted server that wants an API key. With `AHQ_SECRET_KEY` set they are encrypted in the database (existing ones at the next start), and the form shows a saved value as `(saved, encrypted)`: leave that line as it is to keep it.

### The MCP server catalog

Instead of writing a tool server yourself, look for one someone has already published. **Settings → Tools → Catalog** searches the [official MCP Registry](https://registry.modelcontextprotocol.io), the open, community-run list of MCP servers. Each entry shows its publisher (a GitHub account, or a domain they proved they own), version, repository and how it runs.

- **Remote servers** (hosted at a URL) have an **Add** button. It opens the usual form, filled in: name, description, URL and any headers the server asks for (an API key, say), with what each header is for. Fill in the values and save.
- **Package servers** (an npm or PyPI package) have an **Install** button. See [Package servers](#package-servers) below. Servers published only as a Docker image are listed but can't be installed yet.

**Nothing is trusted by default.** A listing is the publisher's own description, not a review. A server added from the catalog starts with every tool set to **Ask me** and isn't given to any agent, so it does nothing until you've looked at it: save it, click **Test connection** to see its tools, relax the approvals for the ones you trust, then tick it on the agents that need it. Check the repository first, and remember that whatever the server is sent (your agents' tool arguments) goes to whoever hosts it. The Tools list shows which servers came from the catalog, and the catalog shows which entries you've already added.

The search runs on the API host, which needs to reach the registry. `AHQ_MCP_REGISTRY_URL` points it at another registry that speaks the same API (a sub-registry, or one you host yourself); set it empty to turn the catalog off.

### Package servers

Most published MCP servers are npm or PyPI packages that talk over stdio. Agent HQ runs each one you install in a **container of its own**, from the `agent-hq/mcp-runner` image (`mcp-runner/`), and agents reach it over HTTP like any other tool server. Build the image once (and again after pulling changes to `mcp-runner/`):

```bash
docker compose --profile images build
```

**Installing.** In **Settings → Tools → Catalog**, click **Install npm package** (or **pypi**) on an entry. The form shows what it will run (`npx -y <package>@<version>` or `uvx <package>==<version>`) and asks for what the package needs: its settings (environment variables; required and secret ones are marked), its arguments (anything in `<angle brackets>` must be replaced, e.g. a directory or a URL), and the hosts it may reach. Install creates:

- a **resource** of kind *package server* under **Settings → Desktops & workbenches**, where you start, stop, restart, recreate and delete its container, change its settings and arguments (saving recreates the container) and see what it was refused;
- a **tool server** with the same name at `http://<name>:8790/mcp`, every tool on **Ask me**, given to no agent. Its first start downloads the package, so give it a minute, then **Test connection** on the Tools page, set its approvals and tick it on the agents that need it.

**How it is contained.** Each package server runs like a workbench: an unprivileged user, a read-only root filesystem (the package and its caches live in its own volume, `/work`), no capabilities, 1 CPU, 1 GB of memory, and only the `workbench` network. It reaches the outside only through the egress proxy, signed in with its own token: PyPI for every one (the workbench defaults), the npm registry for npm packages, plus the hosts you list. When the server needs a host you didn't list (the API it talks to, say), the request is refused and shows under **Blocked lately** on its page with an **Allow** button. Node servers go through the proxy too (`NODE_USE_ENV_PROXY=1`).

`mcp-proxy` turns the package's stdio into streamable HTTP and only answers requests that carry the server's own bearer token (generated at install, sent by Agent HQ as an encrypted header) and are addressed to its name. That matters because workbenches share its network: code an agent runs in a workbench can't call a package server directly and get around your approvals.

**Secrets.** With `AHQ_SECRET_KEY` set, settings marked secret (and any the catalog doesn't describe) are encrypted in the database and shown as `(saved, encrypted)`; leave that line as it is to keep the value. The container gets the real values in its environment, so anyone who can inspect containers on the host can read them.

**Versions.** A package server runs the version it was installed with. To move to a newer one, delete it and install it again from the catalog (its approvals start over).

### Web reader: feeds, pages and search

The `web-reader` service (in `docker-compose.yml`, code in `webreader/`) gives agents text-only web tools. It's much faster and cheaper than driving a browser, and works fine with small local models:

| Tool | What it returns |
|---|---|
| `read_feed(url, limit)` | The newest items of an RSS or Atom feed: title, link, date, short summary. A site's address works too if its page links to a feed. |
| `fetch_page(url, offset)` | A page's readable content as Markdown (the article or documentation, without menus, ads or scripts). Long pages come back in parts: call again with `next_offset`. |
| `search_web(query, limit, time_range, categories)` | Results from **your own SearXNG**: titles, links, snippets. Only offered when `SEARXNG_URL` is set. |

Register it under **Settings → Tools**: HTTP, `http://web-reader:8800/mcp`, untick **Prefix tool names**, leave **Controls an agent desktop** unticked. Then tick it on your researcher (or tick **All agents**). A good split is for the researcher to use feeds, pages and search first, and delegate to the desktop agent only for pages that need a login, JavaScript, or actually looking at them (charts, dashboards).

**Behaviour:**
- `fetch_page` honours `robots.txt` (`WEBREADER_RESPECT_ROBOTS=false` to turn that off) and returns at most `WEBREADER_MAX_CHARS` (12,000) characters per call.
- Downloads are capped at 5 MB, and redirects are followed hop by hop.
- **LAN addresses are refused by default.** Agents read untrusted web content, and a page could try to talk one into probing your network. To let agents read specific internal sites, list them in `WEBREADER_ALLOW_HOSTS` (for example `wiki.home.arpa`).

**Search with SearXNG.** `deploy/searxng/` runs a private SearXNG on any host:

```bash
cd deploy/searxng
cp .env.example .env          # set SEARXNG_SECRET: openssl rand -hex 32
docker compose up -d
curl 'http://localhost:8888/search?q=proxmox&format=json'   # JSON back = ready (403 = JSON not enabled)
```

Its `config/settings.yml` turns on JSON output, which the web reader needs, and turns off the bot limiter, which would otherwise throttle your own agents on a private instance. Then set `SEARXNG_URL=http://<that-host>:8888` in Agent HQ's `.env` and run `docker compose up -d web-reader`. Agents see `search_web` the next time they start a job.

### MQTT publisher: agents publish back to the lab

The `mqtt-publisher` service (in `docker-compose.yml`, code in `mqttpublisher/`) lets agents publish to your MQTT broker: their status for a dashboard such as Home Assistant, or commands to anything in the lab that listens on MQTT.

| Tool | What it does | Approval by default |
|---|---|---|
| `list_publish_topics()` | Shows the agent where it may publish, and the size and rate limits. | Allow (read-only) |
| `publish_status(name, state, detail, data, retain)` | Publishes JSON `{"agent", "state", "detail", "data", "at"}` to `agent-hq/agents/<name>/status`, retained so subscribers see the latest state straight away. | Allow |
| `publish(topic, payload, qos, retain)` | Publishes to one topic on the allow-list. Text is sent as-is; objects and lists are sent as JSON. Not retained unless asked. | **Ask** |

**Setup:**
1. On your broker, create a user for the publisher with **write access only to the topics you'll allow**. The broker then enforces the same limits as the publisher.
2. In `.env`, set `MQTT_PUBLISH_URL=mqtt://agent-hq-pub:<password>@<broker>:1883` (`mqtts://` for TLS; for a step-ca certificate, mount your root certificate and set `MQTT_PUBLISH_CA_FILE`). Set `MQTT_PUBLISH_ALLOW` to the topic filters `publish` may use, for example `agent-hq/agents/#,homelab/cmd/#`.
3. Run `docker compose up -d mqtt-publisher`.
4. Under **Settings → Tools**, register HTTP `http://mqtt-publisher:8810/mcp`, untick **Prefix tool names**, and leave **Controls an agent desktop** unticked. Then tick it on the agents that should publish, or tick **All agents** on it.

**Safeguards:**
- Topics with wildcards, broker system topics (`$SYS/…`) and anything matching `MQTT_PUBLISH_DENY` (default `agent-hq/notifications/#`, so agents can't fake your notifications) are refused.
- Payloads are capped at `MQTT_PUBLISH_MAX_BYTES` (16 KB), and the server sends at most `MQTT_PUBLISH_RATE_PER_MINUTE` (30) messages a minute across all agents.
- `publish` asks for your approval by default, with the usual card in the conversation and the Requests inbox. Once you trust an agent with a topic, set the tool to **Allow** under **Settings → Tools → (server) → Approvals**, or approve with "don't ask again" (an hour, for that agent in that conversation).
- **Watch for loops.** If an Events subscription matches a topic agents publish to, every publish wakes an agent again (the cooldown limits the damage). Keep publish topics outside your subscription filters.
- The agent passes its own name to `publish_status`; the server can't verify it, so status topics are informational and should not be used for access decisions.

## Lab tools: MCP servers for your homelab

`labtools/` is one image of MCP servers for common homelab systems: MikroTik, SNMP switches, Technitium DNS, Kea DHCP, Proxmox VE, TrueNAS SCALE, Zabbix, Prometheus/Alertmanager, Graylog, allow-listed SSH restarts, and ping/port probes. Run as many instances as you like. Each picks its **integrations** (`LABTOOLS_INTEGRATIONS`), **read or write access** (`LABTOOLS_ACCESS`) and **its own credentials**, so you decide which agents can see what and which can change anything.

Read tools are marked read-only. Write tools are typed and narrow, ask you first, and respect guards from the inventory: protected interfaces, SSH allow-lists, bounded silences, no node actions. [docs/labtools.md](labtools.md) covers integrations, configuration and credentials. [docs/lab-alerts.md](lab-alerts.md) covers getting each system's alerts into webhooks. [docs/examples/homelab-crew.md](examples/homelab-crew.md) is a worked example: two read-only watchers and a fixer.

Two tool-server settings help with any MCP server, yours or someone else's (Settings → Tools):
- **Shown** per tool: untick tools agents never need. Hidden tools aren't listed to the model at all, which matters for small local models; a *blocked* tool is still listed, it just can't run.
- **Only reads**: every tool of the server counts as read-only, as if it carried `readOnlyHint`, so webhook-started work doesn't ask before using it. Only tick it for servers you know can't change anything, e.g. `gitea-mcp -r`.

## Tool approvals

For every tool on every tool server, you choose under **Settings → Tools → (server) → Approvals**:
- **Allow:** the call runs.
- **Ask me:** the call waits for your OK.
- **Block:** the call is refused, and the agent is told it isn't allowed.

A rule on `*` covers every tool on that server that doesn't have its own rule. A server can suggest a mode for each tool in the tool's MCP metadata (`{"agent_hq/approval": "ask"}`); the workbench does this for the tools that run code. Your rules always win. A tool with no rule and no suggestion is allowed, so your existing tool servers behave exactly as before.

**When a call needs your OK:**
- **Where it shows up:** in the conversation, as a card showing exactly what the agent wants to run, with code and commands shown as code. It also appears in the **Requests** inbox (with the sidebar badge) and on your [notification channels](notifications.md#notifications-telegram-discord-and-mqtt) as a `tool_approval` notice, which you can approve or deny from Telegram or Discord.
- **Your options:** approve it; approve it and don't ask again for that tool, from that agent, in that conversation, for an hour; or deny it, optionally with a note the agent sees.
- **While it waits:** the agent's worker slot is freed for other work.
- **If you don't decide** within `AHQ_APPROVAL_TIMEOUT_MINUTES` (default 30), the call isn't made, and the agent is told it's waiting for approval.
- **Sub-agents** follow the same rules as everyone else.
- **Work started by a webhook** asks before any tool not marked read-only, even where the rule says allow (see [Inbound webhooks](architecture.md#inbound-webhooks)).

The same table has a **Safe in tests** column. It decides which tools may run for real in an eval when a test case has no recording for the call. Tools the server marks read-only (`readOnlyHint`) start ticked. Only tools set to Allow ever run in a test.
