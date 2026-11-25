"""Budgets: where the usage ledger's numbers go, and warnings when they near a limit.

Reports → Budgets shows the ledger (UsageRecord, one row per run) by *where the work ran*:

* **local**: your own hardware (Ollama, or an OpenAI-compatible server on your LAN). Run
  time here is the stand-in for GPU time.
* **plan**: your ChatGPT or Grok subscription. Flat rate, so it's requests against the
  plan's usage limits, not money.
* **api**: a provider billed per token (OpenAI, Anthropic, or an OpenAI-compatible service
  on the internet). Cost is estimated from a public price list (below).

It also totals each standing goal (everything that ran in the goal's conversation,
delegates and sub-agents included) and checks budgets: a limit per agent or per provider,
per day, week or month, on requests, tokens, local minutes or estimated cost.

**Budgets only warn.** Crossing the early-warning point and crossing 100% each send one
notice per period (type budget_warning, to whichever notification channels take it).
Nothing is stopped, refused or moved to another model: sometimes the frontier model is the
right call, and a run shouldn't die halfway because of a number.

**Prices are maintained automatically.** The API process downloads LiteLLM's public model
price list (model_prices_and_context_window.json) once a day and keeps the per-token
prices in the database. Costs are worked out when a report is read, at the current list
price, from the ledger's input and output tokens. The ledger doesn't split out cached
input tokens, so for providers with prompt caching the estimate is on the high side. A
model the list doesn't have shows as unpriced; there is nothing to enter by hand.

Agents that choose models (sub-agents allowed, or a fallback model set) get check_budget:
their budgets, and for each model they may use, where it runs, how busy its provider is
and what it costs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from pydantic_ai import FunctionToolset, RunContext
from sqlmodel import col, or_, select

from . import clock, db
from .models import (
    AgentDef,
    ModelProvider,
    ProviderKind,
    SpendBudget,
    SystemSetting,
    UsageRecord,
    WorkItem,
    WorkStatus,
)

log = logging.getLogger("agent_hq.budgets")

PRICE_LIST_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
                  "model_prices_and_context_window.json")
DEFAULTS: dict[str, Any] = {
    "pricing": True,              # download the price list and estimate API costs
    "pricing_url": PRICE_LIST_URL,
    "refresh_hours": 24,
}
SETTINGS_KEY = "budgets"
PRICES_KEY = "model_prices"

SCOPES = ("agent", "provider")
PERIODS = ("day", "week", "month")
METRICS: dict[str, str] = {
    "requests": "model requests",
    "tokens": "tokens",
    "local_minutes": "minutes on your hardware",
    "cost_usd": "estimated API cost (USD)",
}
# How an amount reads in a sentence: "9 min of its 10 min on your hardware budget".
NOUNS: dict[str, str] = {
    "requests": "model requests",
    "tokens": "tokens",
    "local_minutes": "on your hardware",
    "cost_usd": "estimated API cost",
}
CLASSES = ("local", "plan", "api", "unknown")

# Host of an OpenAI-compatible cloud service -> its prefix in the price list.
HOST_PREFIX = {
    "openrouter.ai": "openrouter", "api.x.ai": "xai", "api.groq.com": "groq",
    "api.mistral.ai": "mistral", "api.deepseek.com": "deepseek", "api.together.xyz": "together_ai",
    "api.fireworks.ai": "fireworks_ai", "generativelanguage.googleapis.com": "gemini",
    "api.perplexity.ai": "perplexity", "api.cerebras.ai": "cerebras",
}
LOCAL_SUFFIXES = (".lan", ".local", ".home.arpa", ".internal", ".home", ".localdomain", ".intranet")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- settings

def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, SETTINGS_KEY)
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    try:
        hours = int(merged.get("refresh_hours"))
    except (TypeError, ValueError):
        hours = DEFAULTS["refresh_hours"]
    merged["refresh_hours"] = max(1, min(hours, 24 * 7))
    merged["pricing"] = bool(merged.get("pricing"))
    merged["pricing_url"] = str(merged.get("pricing_url") or PRICE_LIST_URL).strip()
    with db.session() as s:
        row = s.get(SystemSetting, SETTINGS_KEY) or SystemSetting(key=SETTINGS_KEY)
        row.value = merged
        s.add(row)
        s.commit()
    return merged


# --------------------------------------------------------------------------- where it runs

def _private_host(base_url: str | None) -> bool:
    host = (urlparse(base_url or "http://localhost").hostname or "localhost").lower()
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES)


def classify(p: ModelProvider | None) -> str:
    """local | plan | api | unknown: where a provider's models run, for the reports."""
    if p is None:
        return "unknown"
    if p.kind == ProviderKind.ollama:
        return "local"
    if p.kind == ProviderKind.openai_compatible:
        return "local" if _private_host(p.base_url) else "api"
    if p.kind in (ProviderKind.chatgpt, ProviderKind.grok):
        return "plan"
    return "api"


def providers() -> dict[str, ModelProvider]:
    with db.session() as s:
        return {p.id: p for p in s.exec(select(ModelProvider)).all()}


# --------------------------------------------------------------------------- prices

_cache: dict[str, Any] = {"at": 0.0, "table": {}}
CACHE_SECONDS = 600


def _slim(raw: dict[str, Any]) -> dict[str, list[float]]:
    """Keep what costs a chat run: {model key: [input $/token, output $/token]}."""
    out: dict[str, list[float]] = {}
    for k, v in (raw or {}).items():
        if not isinstance(v, dict) or k == "sample_spec":
            continue
        if v.get("mode") not in (None, "chat", "responses", "completion"):
            continue
        i, o = v.get("input_cost_per_token"), v.get("output_cost_per_token")
        try:
            if i is None and o is None:
                continue
            out[k] = [float(i or 0), float(o or 0)]
        except (TypeError, ValueError):
            continue
    return out


def price_info() -> dict[str, Any]:
    """{fetched_at, source, models, error, tried_at} (no table)."""
    with db.session() as s:
        row = s.get(SystemSetting, PRICES_KEY)
    v = dict(row.value) if row else {}
    return {"fetched_at": v.get("fetched_at"), "source": v.get("source"),
            "models": len(v.get("prices") or {}), "error": v.get("error") or "",
            "tried_at": v.get("tried_at")}


def prices(*, fresh: bool = False) -> dict[str, list[float]]:
    if not fresh and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["table"]
    with db.session() as s:
        row = s.get(SystemSetting, PRICES_KEY)
    table = dict((row.value or {}).get("prices") or {}) if row else {}
    _cache.update(at=time.monotonic(), table=table)
    return table


def store_prices(table: dict[str, list[float]], *, source: str) -> None:
    now = _now().isoformat()
    with db.session() as s:
        row = s.get(SystemSetting, PRICES_KEY) or SystemSetting(key=PRICES_KEY)
        row.value = {"prices": table, "fetched_at": now, "tried_at": now, "source": source, "error": ""}
        s.add(row)
        s.commit()
    _cache.update(at=time.monotonic(), table=table)


def _note_failure(error: str) -> None:
    with db.session() as s:
        row = s.get(SystemSetting, PRICES_KEY) or SystemSetting(key=PRICES_KEY)
        row.value = {**(row.value or {}), "error": error[:300], "tried_at": _now().isoformat()}
        s.add(row)
        s.commit()


async def refresh_prices(transport=None) -> dict[str, Any]:
    """Download the price list now. Keeps the last good list if this one fails.
    (transport: an httpx transport, for the tests.)"""
    import httpx

    url = get_settings()["pricing_url"]
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, transport=transport) as c:
            r = await c.get(url)
            r.raise_for_status()
            table = _slim(r.json())
        if not table:
            raise ValueError("the price list had no model prices")
        store_prices(table, source=url)
        log.info("model prices refreshed: %d models", len(table))
    except Exception as e:
        log.warning("could not refresh model prices from %s: %s", url, e)
        _note_failure(f"{type(e).__name__}: {e}")
    return price_info()


def prices_due(now: datetime | None = None) -> bool:
    cfg = get_settings()
    if not cfg["pricing"]:
        return False
    info = price_info()
    last = info["tried_at"]
    if not last:
        return True
    try:
        at = _aware(datetime.fromisoformat(last))
    except ValueError:
        return True
    return (now or _now()) - at >= timedelta(hours=int(cfg["refresh_hours"]))


async def loop(tick: float = 900.0) -> None:
    """API process: keep the price list fresh (on start, then every refresh_hours)."""
    while True:
        try:
            if prices_due():
                await refresh_prices()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("price refresh failed")
        await asyncio.sleep(tick)


def price_for(p: ModelProvider | None, model: str) -> tuple[float, float] | None:
    """(input $/token, output $/token) for a model on an API provider; None if unknown or
    if the provider isn't billed per token."""
    if classify(p) != "api" or not model or not get_settings()["pricing"]:
        return None
    table = prices()
    if not table:
        return None
    model = model.strip()
    cands: list[str] = []
    if p.kind == ProviderKind.openai:
        cands = [model, f"openai/{model}"]
    elif p.kind == ProviderKind.anthropic:
        cands = [model, f"anthropic/{model}"]
    else:
        host = (urlparse(p.base_url or "").hostname or "").lower()
        prefix = next((v for h, v in HOST_PREFIX.items() if host == h or host.endswith("." + h)), None)
        if prefix:
            cands.append(f"{prefix}/{model}")
        cands.append(model)
    for c in cands:
        if c in table:
            i, o = table[c]
            return float(i), float(o)
    return None


def run_cost(r: UsageRecord, p: ModelProvider | None) -> float | None:
    price = price_for(p, r.model)
    if price is None:
        return None
    return r.input_tokens * price[0] + r.output_tokens * price[1]


# --------------------------------------------------------------------------- totals

def _blank() -> dict[str, Any]:
    return {"runs": 0, "requests": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0,
            "cost_usd": 0.0, "unpriced_runs": 0}


def _add(g: dict[str, Any], r: UsageRecord, cls: str, cost: float | None) -> None:
    g["runs"] += 1
    g["requests"] += r.requests
    g["input_tokens"] += r.input_tokens
    g["output_tokens"] += r.output_tokens
    g["seconds"] += r.seconds
    if cls == "api":
        if cost is None:
            g["unpriced_runs"] += 1
        else:
            g["cost_usd"] += cost


def _round(g: dict[str, Any]) -> dict[str, Any]:
    g["seconds"] = round(g["seconds"], 1)
    g["cost_usd"] = round(g["cost_usd"], 4)
    return g


def split(rows: list[UsageRecord], provs: dict[str, ModelProvider]) -> dict[str, Any]:
    """Totals by where the work ran, and by provider."""
    by_class = {c: _blank() for c in CLASSES}
    by_provider: dict[str, dict[str, Any]] = {}
    for r in rows:
        p = provs.get(r.provider_id or "")
        cls = classify(p)
        cost = run_cost(r, p) if cls == "api" else None
        _add(by_class[cls], r, cls, cost)
        key = r.provider_id or ""
        g = by_provider.setdefault(key, {"provider_id": r.provider_id,
                                         "provider": p.name if p else (r.provider_name or "(none)"),
                                         "kind": p.kind.value if p else "", "runs_on": cls,
                                         "models": {}, **_blank()})
        _add(g, r, cls, cost)
        m = g["models"].setdefault(r.model or "", {"model": r.model or "", **_blank()})
        _add(m, r, cls, cost)
        if cls == "api":
            price = price_for(p, r.model)
            m["price_per_mtok"] = [round(price[0] * 1e6, 4), round(price[1] * 1e6, 4)] if price else None
    provs_out = []
    for g in by_provider.values():
        g["models"] = sorted((_round(m) for m in g["models"].values()), key=lambda m: -m["requests"])
        provs_out.append(_round(g))
    provs_out.sort(key=lambda g: (CLASSES.index(g["runs_on"]), -g["requests"]))
    return {"by_class": {c: _round(g) for c, g in by_class.items() if g["runs"] or c != "unknown"},
            "by_provider": provs_out}


def goal_totals(since: datetime, provs: dict[str, ModelProvider], limit: int = 50) -> list[dict[str, Any]]:
    """Every goal that ran something since `since`, with its totals since it started (all
    the runs in its conversation: its turns, delegates and sub-agent team members)."""
    with db.session() as s:
        recent = s.exec(select(UsageRecord.thread_id).where(UsageRecord.at >= since,
                                                             col(UsageRecord.thread_id).is_not(None))
                        .distinct()).all()
        tids = [t for t in recent if t]
        if not tids:
            return []
        goals = s.exec(select(WorkItem).where(WorkItem.kind == "goal",
                                              col(WorkItem.thread_id).in_(tids))).all()
        names = {a.id: a.name for a in s.exec(select(AgentDef)).all()}
        out = []
        for g in goals:
            q = select(UsageRecord).where(UsageRecord.thread_id == g.thread_id)
            if g.started_at:
                q = q.where(UsageRecord.at >= g.started_at)
            rows = list(s.exec(q).all())
            t = _blank()
            local_s = 0.0
            for r in rows:
                p = provs.get(r.provider_id or "")
                cls = classify(p)
                _add(t, r, cls, run_cost(r, p) if cls == "api" else None)
                if cls == "local":
                    local_s += r.seconds
            t["local_seconds"] = round(local_s, 1)
            t["cloud_requests"] = sum(r.requests for r in rows
                                      if classify(provs.get(r.provider_id or "")) in ("plan", "api"))
            status = g.status.value if isinstance(g.status, WorkStatus) else str(g.status)
            out.append({"id": g.id, "title": g.title, "status": status,
                        "agent": names.get(g.owner_agent_id or "", ""),
                        "started_at": g.started_at, "finished_at": g.finished_at,
                        "turns": g.turns, **_round(t)})
    out.sort(key=lambda g: (g["status"] in ("done", "dropped"), -g["requests"]))
    return out[:limit]


# --------------------------------------------------------------------------- budgets

def period_bounds(period: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """(start, next start, key) of the current day, week (from Monday) or month, in your
    time zone (AHQ_TIMEZONE)."""
    tz = clock.zone()
    local = (now or _now()).astimezone(tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = day - timedelta(days=day.weekday())
        nxt = start + timedelta(days=7)
        key = f"{start.date().isoformat()}/w"
    elif period == "month":
        start = day.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        key = start.strftime("%Y-%m")
    else:
        start, nxt, key = day, day + timedelta(days=1), day.date().isoformat()
    # ZoneInfo works out each boundary's own offset, so a DST change inside the period
    # doesn't shift midnight.
    return start.astimezone(timezone.utc), nxt.astimezone(timezone.utc), key


def _rows_for(b: SpendBudget, since: datetime) -> list[UsageRecord]:
    with db.session() as s:
        q = select(UsageRecord).where(UsageRecord.at >= since)
        if b.scope == "provider":
            q = q.where(UsageRecord.provider_id == b.target_id)
        else:  # an agent, with the sub-agents it created
            q = q.where(or_(UsageRecord.agent_id == b.target_id,
                            UsageRecord.parent_agent_id == b.target_id))
        return list(s.exec(q).all())


def measure(metric: str, rows: list[UsageRecord], provs: dict[str, ModelProvider]) -> tuple[float, int]:
    """(value, unpriced runs) of one metric over some ledger rows."""
    if metric == "requests":
        return float(sum(r.requests for r in rows)), 0
    if metric == "tokens":
        return float(sum(r.input_tokens + r.output_tokens for r in rows)), 0
    if metric == "local_minutes":
        return sum(r.seconds for r in rows if classify(provs.get(r.provider_id or "")) == "local") / 60, 0
    total, unpriced = 0.0, 0
    for r in rows:
        p = provs.get(r.provider_id or "")
        if classify(p) != "api":
            continue
        c = run_cost(r, p)
        if c is None:
            unpriced += 1
        else:
            total += c
    return total, unpriced


def _target_name(b: SpendBudget, provs: dict[str, ModelProvider], agents: dict[str, AgentDef]) -> str:
    if b.scope == "provider":
        p = provs.get(b.target_id)
        return p.name if p else "(deleted provider)"
    a = agents.get(b.target_id)
    return a.name if a else "(deleted agent)"


def status(b: SpendBudget, *, provs: dict[str, ModelProvider] | None = None,
           agents: dict[str, AgentDef] | None = None, now: datetime | None = None) -> dict[str, Any]:
    provs = provs if provs is not None else providers()
    if agents is None:
        with db.session() as s:
            agents = {a.id: a for a in s.exec(select(AgentDef)).all()}
    start, nxt, key = period_bounds(b.period, now)
    used, unpriced = measure(b.metric, _rows_for(b, start), provs)
    pct = round(100 * used / b.limit, 1) if b.limit > 0 else 0.0
    level = "over" if pct >= 100 else ("warn" if pct >= b.warn_pct else "ok")
    return {"id": b.id, "scope": b.scope, "target_id": b.target_id,
            "target_name": _target_name(b, provs, agents), "period": b.period, "metric": b.metric,
            "metric_label": METRICS.get(b.metric, b.metric),
            "metric_noun": NOUNS.get(b.metric, b.metric), "limit": b.limit, "warn_pct": b.warn_pct,
            "enabled": b.enabled, "note": b.note, "used": round(used, 4 if b.metric == "cost_usd" else 1),
            "pct": pct, "level": level if b.enabled else "off", "unpriced_runs": unpriced,
            "period_start": start, "resets_at": nxt, "period_key": key, "created_at": b.created_at}


def all_status(now: datetime | None = None) -> list[dict[str, Any]]:
    provs = providers()
    with db.session() as s:
        agents = {a.id: a for a in s.exec(select(AgentDef)).all()}
        rows = s.exec(select(SpendBudget).order_by(SpendBudget.created_at)).all()
    return [status(b, provs=provs, agents=agents, now=now) for b in rows]


def validate(data: dict[str, Any], b: SpendBudget | None = None) -> SpendBudget:
    b = b or SpendBudget(target_id="")
    for k in ("scope", "target_id", "period", "metric", "limit", "warn_pct", "enabled", "note"):
        if k in data and data[k] is not None:
            setattr(b, k, data[k])
    if b.scope not in SCOPES:
        raise ValueError("scope must be agent or provider")
    if b.period not in PERIODS:
        raise ValueError("period must be day, week or month")
    if b.metric not in METRICS:
        raise ValueError("metric must be one of " + ", ".join(METRICS))
    try:
        b.limit = float(b.limit)
        b.warn_pct = int(b.warn_pct)
    except (TypeError, ValueError):
        raise ValueError("limit and warn_pct must be numbers") from None
    if b.limit <= 0:
        raise ValueError("the limit must be more than 0")
    if not 1 <= b.warn_pct <= 99:
        raise ValueError("the early warning must be between 1% and 99%")
    with db.session() as s:
        ok = (s.get(ModelProvider, b.target_id) if b.scope == "provider" else s.get(AgentDef, b.target_id))
    if ok is None:
        raise ValueError(f"no such {b.scope}")
    if b.scope == "agent" and ok.is_subagent:
        raise ValueError("budget the agent that creates sub-agents; their runs count under it")
    b.note = (b.note or "")[:500]
    return b


# --------------------------------------------------------------------------- warnings

_pending: set[asyncio.Task] = set()


def after_run(row: UsageRecord) -> None:
    """Called once a run is on the ledger: check the budgets it counts toward, in the
    background (never in the way of the run)."""
    try:
        loop_ = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop_.create_task(_check_quietly(row.agent_id, row.parent_agent_id, row.provider_id))
    _pending.add(t)
    t.add_done_callback(_pending.discard)


async def _check_quietly(*args) -> None:
    try:
        await check(*args)
    except Exception as e:
        log.warning("budget check failed: %s", e)


def _applies(agent_id: str | None, parent_id: str | None, provider_id: str | None) -> list[SpendBudget]:
    conds = []
    ids = [i for i in (agent_id, parent_id) if i]
    if ids:
        conds.append((SpendBudget.scope == "agent") & col(SpendBudget.target_id).in_(ids))
    if provider_id:
        conds.append((SpendBudget.scope == "provider") & (SpendBudget.target_id == provider_id))
    if not conds:
        return []
    with db.session() as s:
        return list(s.exec(select(SpendBudget).where(SpendBudget.enabled == True,  # noqa: E712
                                                     or_(*conds))).all())


def _fmt(metric: str, v: float) -> str:
    if metric == "cost_usd":
        return f"${v:,.2f}"
    if metric == "local_minutes":
        return f"{v:,.0f} min"
    return f"{v:,.0f}"


PERIOD_WORD = {"day": "today", "week": "this week", "month": "this month"}


async def check(agent_id: str | None, parent_id: str | None, provider_id: str | None,
                *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Send any warnings now due for the budgets these runs count toward. Each level (the
    early warning, then 100%) is sent once per period. Returns the warnings sent."""
    sent = []
    budgets = _applies(agent_id, parent_id, provider_id)
    if not budgets:
        return sent
    provs = providers()
    with db.session() as s:
        agents = {a.id: a for a in s.exec(select(AgentDef)).all()}
    for b in budgets:
        st = status(b, provs=provs, agents=agents, now=now)
        crossed = [lvl for lvl in (b.warn_pct, 100) if st["pct"] >= lvl]
        if not crossed:
            continue
        # Claim the level under a row lock, so two workers finishing together send one notice.
        with db.session() as s:
            row = s.exec(select(SpendBudget).where(SpendBudget.id == b.id).with_for_update()).first()
            if row is None or not row.enabled:
                continue
            state = dict(row.state or {})
            warned = list(state.get("levels") or []) if state.get("period") == st["period_key"] else []
            new = [lvl for lvl in crossed if lvl not in warned]
            if not new:
                continue
            row.state = {"period": st["period_key"], "levels": sorted(set(warned + new))}
            s.add(row)
            s.commit()
        level = max(new)
        over = level >= 100
        who = st["target_name"]
        # Routing rules filter by agent: the budgeted agent, or for a provider budget the
        # agent whose run tipped it over.
        agent_name = who if b.scope == "agent" else (agents[agent_id].name if agent_id in agents else "")
        text = (f"{who} has used {_fmt(b.metric, st['used'])} of its {_fmt(b.metric, b.limit)} "
                f"{NOUNS[b.metric]} budget {PERIOD_WORD[b.period]} ({st['pct']:.0f}%).")
        if st["unpriced_runs"]:
            text += f" {st['unpriced_runs']} run(s) on models without a known price aren't counted."
        text += " This is a warning only; nothing has been stopped."
        event = {"type": "budget_warning", "agent": agent_name, "severity": "critical" if over else "warning",
                 "budget_id": b.id, "scope": b.scope, "target": who, "metric": b.metric,
                 "period": b.period, "used": st["used"], "limit": b.limit, "pct": st["pct"],
                 "level": level, "text": text}
        from . import notifications, subagents

        event["link"] = notifications.link("/reports/budgets")
        await subagents.notify(event)
        sent.append(event)
    return sent


# --------------------------------------------------------------------------- the agent tool

BUDGET_RULES = (
    "## Budgets\n"
    "check_budget shows the user's usage budgets that apply to you and, for each model you may "
    "use (your own, your fallback, the ones you may give sub-agents), where it runs (your user's "
    "hardware, a flat-rate plan, or a per-token API), how busy its provider is, its price and "
    "its budgets. Check it before you escalate, pick a cloud model or ask for sub-agents. "
    "Prefer local models while they have room and aren't busy; use a cloud or frontier model "
    "when the work needs it. Budgets only warn the user: going over never stops you, but say "
    "so in your reply when you choose to.")


async def overview_for(agent: AgentDef, now: datetime | None = None) -> dict[str, Any]:
    from . import slots

    provs = providers()
    with db.session() as s:
        agents = {a.id: a for a in s.exec(select(AgentDef)).all()}
        mine = s.exec(select(SpendBudget).where(SpendBudget.enabled == True)).all()  # noqa: E712
    choices: list[tuple[str, str | None, str]] = [("your model", agent.provider_id, agent.model)]
    if agent.fallback_model:
        choices.append(("your fallback", agent.fallback_provider_id, agent.fallback_model))
    for m in (agent.subagent_limits or {}).get("models") or []:
        choices.append(("for sub-agents", m.get("provider_id"), m.get("model", "")))

    def budget_line(b: SpendBudget) -> dict[str, Any]:
        st = status(b, provs=provs, agents=agents, now=now)
        return {"what": f"{st['target_name']}: {_fmt(b.metric, st['used'])} of {_fmt(b.metric, b.limit)} "
                        f"{NOUNS[b.metric]} {PERIOD_WORD[b.period]}", "pct": st["pct"], "level": st["level"]}

    models, seen = [], set()
    for use, pid, model in choices:
        if (pid, model) in seen or not model:
            continue
        seen.add((pid, model))
        p = provs.get(pid or "")
        cls = classify(p)
        item: dict[str, Any] = {"model": model, "use": use, "provider": p.name if p else "(none)",
                                "runs_on": {"local": "your user's hardware", "plan": "a flat-rate plan",
                                            "api": "a per-token cloud API", "unknown": "unknown"}[cls]}
        if p and p.max_concurrent:
            busy = await slots.in_use(p.id)
            item["busy"] = f"{busy} of {p.max_concurrent} request slots in use"
        price = price_for(p, model)
        if price:
            item["price_per_million_tokens"] = {"input": round(price[0] * 1e6, 3), "output": round(price[1] * 1e6, 3)}
        elif cls == "api":
            item["price_per_million_tokens"] = "unknown"
        pb = [budget_line(b) for b in mine if b.scope == "provider" and b.target_id == pid]
        if pb:
            item["budgets"] = pb
        models.append(item)
    own = [budget_line(b) for b in mine if b.scope == "agent" and b.target_id == agent.id]
    return {"your_budgets": own or "none set", "models": models,
            "note": "Budgets only warn; nothing is stopped when one is reached."}


def tool_applies(agent: AgentDef) -> bool:
    """Agents that choose between models: sub-agents allowed or asked for, or a fallback."""
    from .models import SubagentMode

    mode = agent.subagent_mode.value if hasattr(agent.subagent_mode, "value") else agent.subagent_mode
    return not agent.is_subagent and (mode != SubagentMode.off.value or bool(agent.fallback_model))


def toolset() -> FunctionToolset:
    async def check_budget(ctx: RunContext) -> dict:
        """Your usage budgets, and for each model you may use: where it runs, how busy it is,
        its price and its budgets. Check before choosing a cloud model or asking for sub-agents."""
        from .runtime import load_agent

        return await overview_for(load_agent(ctx.deps.agent_id))

    return FunctionToolset([check_budget], id="budgets")


def headroom(provider_id: str | None, agent_id: str | None) -> list[str]:
    """One line per enabled budget a sub-agent on this provider would count toward (for the
    sub-agent request estimate)."""
    out = []
    budgets = _applies(agent_id, None, provider_id)
    if not budgets:
        return out
    provs = providers()
    for b in budgets:
        st = status(b, provs=provs)
        out.append(f"{st['target_name']}: {_fmt(b.metric, st['used'])} of {_fmt(b.metric, b.limit)} "
                   f"{NOUNS[b.metric]} {PERIOD_WORD[b.period]} ({st['pct']:.0f}%)")
    return out
