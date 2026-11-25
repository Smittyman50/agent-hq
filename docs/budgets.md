# Budgets

**Reports → Budgets** shows where your agents' work ran and what it cost, per provider and per standing goal, and lets you set budgets that warn you before an agent or a provider runs away with your GPU time, your plan's limits or your API bill.

It reads the same usage ledger as **Reports → Usage history**: one row per agent run (a chat turn, a delegated task or a sub-agent task) with its model, model requests, tokens and wall-clock time. Usage history totals the ledger by agent, model and day. Budgets adds what those numbers mean for you:

| Usage history | Budgets |
|---|---|
| Run time, requests and tokens by agent, model and day | The same, split by **where the work ran**: your hardware, a plan, a per-token API |
| Nothing about money | **Estimated API cost**, from a price list that updates itself |
| Nothing per goal | **Totals per standing goal**, delegates and sub-agents included |
| No limits | **Budgets** per agent or per provider, with warnings on your phone |

Agents that choose between models also get a `check_budget` tool (below).

## Where the work ran

Each provider is sorted into one of three kinds:

- **Your hardware:** Ollama, and OpenAI-compatible servers on your LAN (a private IP address, `localhost`, a name without dots, or a name ending in `.lan`, `.local`, `.home.arpa`, `.internal`, `.home`, `.localdomain` or `.intranet`). Run time here is the stand-in for GPU time. It is wall-clock time, tool calls included, so it leans high.
- **Plans:** your ChatGPT or Grok subscription. Flat rate, so what matters is requests against the plan's usage limits, not money.
- **Per-token APIs:** OpenAI, Anthropic, and OpenAI-compatible services on the internet (OpenRouter, Groq, xAI's API and so on). These have an estimated cost.

The sub-agent request card and `check_budget` use the same sorting, so an OpenAI-compatible provider pointing at OpenRouter now shows as a cloud API rather than your hardware.

## Estimated cost and the price list

Nothing to maintain by hand. Once a day the API process downloads [LiteLLM's public model price list](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) (about 4,500 models with per-token prices) and keeps the input and output prices in the database. If a download fails, the last good list stays and the page says why.

- Costs are worked out when you open the page, from each run's input and output tokens at the **current** list price.
- The ledger doesn't split out cached input tokens, so for providers with prompt caching the estimate is on the high side.
- A model the list doesn't know shows as **no known price** and isn't counted; the page says how many such runs there were.
- Models are matched by name: `gpt-4.1` on an OpenAI provider, `claude-sonnet-4-5` on an Anthropic provider, and `openrouter/<model>`, `xai/<model>`, `groq/<model>` (and a few others) for OpenAI-compatible services on those hosts.
- Your hardware and your plans never have a cost.

At the bottom of the page: turn cost estimates off (no downloads at all), or **Update now**. The API has the rest: `GET/PUT /api/budgets/settings` (`pricing`, `pricing_url`, `refresh_hours`, 1 to 168) and `POST /api/budgets/prices/refresh`. The download needs outbound HTTPS from the `hq` container to `raw.githubusercontent.com`.

## Goals

The **Goals** table lists every standing goal that ran something in the chosen period, with its totals since it started: everything that ran in the goal's conversation, which includes its turns, the agents it delegated to and the sub-agents on its team. It shows run time on your hardware, cloud requests (plans and APIs) and estimated cost, so you can see what a goal cost before you confirm it's done.

## Budgets

**Add budget** sets a limit on:

- **an agent**, which counts its own runs and those of the sub-agents it creates, or **a provider**, which counts every run on it;
- **per day, week (from Monday) or month**, in your time zone (`AHQ_TIMEZONE`);
- **model requests, tokens, minutes on your hardware, or estimated API cost (USD)**.

Each budget has an **early warning** point (80% unless you change it).

**Budgets only warn.** When a finished run takes a budget past its early-warning point you get one notice, and one more when it reaches 100%. Straight past both at once sends only the 100% one. Each level is sent once per period; a new day, week or month starts over, and so does a budget you change. Nothing is stopped, refused or moved to another model: sometimes the frontier model is the right call, and a run shouldn't die halfway because of a number you set.

The warnings are notices of type **Budget warnings** (`budget_warning`), sent to whichever channels' rules take them (Settings → Notifications). New channels take them by default. An existing Telegram, Discord or MQTT channel whose rule lists types needs **Budget warnings** ticked. The 80% warning arrives as *warning*, the 100% one as *critical*; each links to Reports → Budgets.

Deleting an agent deletes its budgets (the delete prompt counts them), and so does deleting a provider.

## check_budget

Agents that choose between models, meaning sub-agents set to *Ask me first* or *Allowed*, or a fallback model set, get `check_budget`. It returns:

- the agent's own budgets, with how much is used;
- for each model it may use (its own, its fallback, the ones it may give sub-agents): where it runs, how many of the provider's request slots are in use, the price per million tokens for a per-token API, and that provider's budgets.

Its instructions say to check before escalating, picking a cloud model or asking for sub-agents, to prefer local models while they have room, and to use a cloud or frontier model when the work needs it. Going over a budget never stops it; it's told to say so in its reply when it does.

The sub-agent request card (Requests → Sub-agents) also lists, under *Runs on*, the budgets each proposed model would count toward.
