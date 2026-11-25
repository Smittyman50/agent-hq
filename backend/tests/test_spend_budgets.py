"""Reports → Budgets: where the work ran (your hardware, a plan, a per-token API), estimated
API cost from a price list kept up to date by itself, totals per goal, budgets that only
warn, and check_budget for agents that choose between models.

(test_budget.py is something else: an agent's model-request limit within one run.)"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from sqlmodel import select

from app import budgets, db, notifications, subagents
from app.config import reload_settings
from app.models import AgentDef, ModelProvider, ProviderKind, SpendBudget, UsageRecord, WorkItem, WorkStatus

from .conftest import SCRIPTS, _tool_returns

UTC = timezone.utc

PRICE_LIST = {
    "sample_spec": {"input_cost_per_token": 0},
    "gpt-4.1": {"input_cost_per_token": 2e-06, "output_cost_per_token": 8e-06, "mode": "chat",
                "litellm_provider": "openai"},
    "claude-sonnet-4-5": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05, "mode": "chat"},
    "openrouter/qwen/qwen3-235b": {"input_cost_per_token": 1e-07, "output_cost_per_token": 6e-07},
    "text-embedding-3-small": {"input_cost_per_token": 2e-08, "mode": "embedding"},
    "dall-e-3": {"mode": "image_generation", "output_cost_per_image": 0.04},
}


@pytest.fixture(autouse=True)
def _fresh_price_cache():
    budgets._cache.update(at=0.0, table={})
    yield
    budgets._cache.update(at=0.0, table={})


@pytest.fixture
def sent(monkeypatch):
    """Every notice the budget checks send."""
    out: list[dict] = []

    async def notify(event):
        out.append(event)
        return True

    monkeypatch.setattr(subagents, "notify", notify)
    return out


def provider(name: str, kind: ProviderKind, base_url: str | None = None) -> ModelProvider:
    p = ModelProvider(name=name, kind=kind, base_url=base_url)
    with db.session() as s:
        s.add(p)
        s.commit()
        s.refresh(p)
    return p


def agent_row(name: str, **kw) -> AgentDef:
    a = AgentDef(name=name, model=kw.pop("model", "fake:echo"), **kw)
    with db.session() as s:
        s.add(a)
        s.commit()
        s.refresh(a)
    return a


def run(agent: AgentDef, p: ModelProvider | None = None, *, model="m", requests=1, tin=0, tout=0,
        seconds=1.0, at: datetime | None = None, thread_id=None, sub_of: AgentDef | None = None) -> UsageRecord:
    r = UsageRecord(agent_id=agent.id, agent_name=agent.name, is_subagent=sub_of is not None,
                    parent_agent_id=sub_of.id if sub_of else None, provider_id=p.id if p else None,
                    provider_name=p.name if p else "", model=model, requests=requests,
                    input_tokens=tin, output_tokens=tout, seconds=seconds, thread_id=thread_id,
                    at=at or datetime.now(UTC))
    with db.session() as s:
        s.add(r)
        s.commit()
        s.refresh(r)
    return r


def budget(**kw) -> SpendBudget:
    b = budgets.validate({"period": "day", "metric": "requests", "warn_pct": 80, **kw})
    with db.session() as s:
        s.add(b)
        s.commit()
        s.refresh(b)
    return b


# -------------------------------------------------------------------- where it runs

def test_providers_are_sorted_into_local_plan_and_api(hq):
    assert budgets.classify(None) == "unknown"
    assert budgets.classify(ModelProvider(name="a", kind=ProviderKind.ollama, base_url="http://10.0.0.5:11434")) == "local"
    for url in ("http://192.168.1.20:8000/v1", "http://einstein.home.arpa:8000/v1", "http://gpu:8000/v1",
                "http://localhost:1234/v1", "http://vllm.lan/v1", None):
        assert budgets.classify(ModelProvider(name="b", kind=ProviderKind.openai_compatible, base_url=url)) == "local", url
    assert budgets.classify(ModelProvider(name="c", kind=ProviderKind.openai_compatible,
                                          base_url="https://openrouter.ai/api/v1")) == "api"
    assert budgets.classify(ModelProvider(name="d", kind=ProviderKind.chatgpt)) == "plan"
    assert budgets.classify(ModelProvider(name="e", kind=ProviderKind.grok)) == "plan"
    assert budgets.classify(ModelProvider(name="f", kind=ProviderKind.openai)) == "api"
    assert budgets.classify(ModelProvider(name="g", kind=ProviderKind.anthropic)) == "api"
    # The sub-agent estimate says the same.
    assert subagents.where_it_runs(ModelProvider(name="c", kind=ProviderKind.openai_compatible,
                                                 base_url="https://openrouter.ai/api/v1")).startswith("cloud API")
    assert subagents.where_it_runs(ModelProvider(name="b", kind=ProviderKind.openai_compatible,
                                                 base_url="http://192.168.1.20:8000/v1")).startswith("your hardware")


# -------------------------------------------------------------------- prices

def test_price_list_is_slimmed_to_chat_prices():
    table = budgets._slim(PRICE_LIST)
    assert set(table) == {"gpt-4.1", "claude-sonnet-4-5", "openrouter/qwen/qwen3-235b"}
    assert table["gpt-4.1"] == [2e-06, 8e-06]


def test_prices_match_by_provider(hq):
    budgets.store_prices(budgets._slim(PRICE_LIST), source="test")
    oa = ModelProvider(name="OpenAI", kind=ProviderKind.openai)
    an = ModelProvider(name="Anthropic", kind=ProviderKind.anthropic)
    orr = ModelProvider(name="OpenRouter", kind=ProviderKind.openai_compatible, base_url="https://openrouter.ai/api/v1")
    local = ModelProvider(name="Ollama", kind=ProviderKind.ollama)
    plan = ModelProvider(name="ChatGPT", kind=ProviderKind.chatgpt)
    assert budgets.price_for(oa, "gpt-4.1") == (2e-06, 8e-06)
    assert budgets.price_for(an, "claude-sonnet-4-5") == (3e-06, 1.5e-05)
    assert budgets.price_for(orr, "qwen/qwen3-235b") == (1e-07, 6e-07)
    assert budgets.price_for(oa, "gpt-unknown") is None
    assert budgets.price_for(local, "gpt-4.1") is None   # your hardware costs nothing per token
    assert budgets.price_for(plan, "gpt-4.1") is None    # flat-rate plan
    r = UsageRecord(agent_id="x", model="gpt-4.1", input_tokens=1000, output_tokens=500)
    assert budgets.run_cost(r, oa) == pytest.approx(1000 * 2e-06 + 500 * 8e-06)
    budgets.save_settings({"pricing": False})
    budgets._cache.update(at=0.0)
    assert budgets.price_for(oa, "gpt-4.1") is None


async def test_prices_refresh_themselves_and_keep_the_last_good_list(hq):
    calls = []

    def ok(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=PRICE_LIST)

    assert budgets.prices_due()  # never fetched
    info = await budgets.refresh_prices(transport=httpx.MockTransport(ok))
    assert calls == [budgets.PRICE_LIST_URL]
    assert info["models"] == 3 and info["error"] == "" and info["fetched_at"]
    assert not budgets.prices_due()
    later = datetime.now(UTC) + timedelta(hours=25)
    assert budgets.prices_due(later)  # once a day by default
    budgets.save_settings({"refresh_hours": 48})
    assert not budgets.prices_due(later)
    budgets.save_settings({"pricing": False})
    assert not budgets.prices_due(later + timedelta(days=30))  # off: never downloads
    budgets.save_settings({"pricing": True})

    def down(request):
        return httpx.Response(503)

    info = await budgets.refresh_prices(transport=httpx.MockTransport(down))
    assert info["models"] == 3 and "503" in info["error"]  # the old list stays
    assert budgets.prices(fresh=True)["gpt-4.1"] == [2e-06, 8e-06]
    # Through the API too, and the settings.
    r = (await hq.http.get("/api/budgets/settings")).json()
    assert r["pricing"] is True and r["prices"]["models"] == 3
    r = (await hq.http.put("/api/budgets/settings", json={"refresh_hours": 0, "bogus": 1})).json()
    assert r["refresh_hours"] == 1 and "bogus" not in r


# -------------------------------------------------------------------- periods

def test_periods_follow_your_time_zone(hq, monkeypatch):
    monkeypatch.setenv("AHQ_TIMEZONE", "America/New_York")
    reload_settings()
    # 2026-11-03 02:30 UTC is still Nov 2 in New York (EST, UTC-5, since DST ended on Nov 1).
    now = datetime(2026, 11, 3, 2, 30, tzinfo=UTC)
    start, nxt, key = budgets.period_bounds("day", now)
    assert key == "2026-11-02" and start == datetime(2026, 11, 2, 5, tzinfo=UTC)
    assert nxt == datetime(2026, 11, 3, 5, tzinfo=UTC)
    start, nxt, key = budgets.period_bounds("week", now)  # from Monday
    assert key == "2026-11-02/w" and start == datetime(2026, 11, 2, 5, tzinfo=UTC)
    start, nxt, key = budgets.period_bounds("month", now)
    # Nov 1 midnight was still EDT (UTC-4); Dec 1 is EST (UTC-5).
    assert key == "2026-11" and start == datetime(2026, 11, 1, 4, tzinfo=UTC)
    assert nxt == datetime(2026, 12, 1, 5, tzinfo=UTC)


# -------------------------------------------------------------------- the API

async def test_budget_api_validates_and_lists(hq):
    duke = agent_row("Duke")
    sub = agent_row("Duke #1", is_subagent=True, parent_agent_id=duke.id)
    ol = provider("Ollama", ProviderKind.ollama, "http://einstein:11434")
    t = (await hq.http.get("/api/budgets/targets")).json()
    assert [a["name"] for a in t["agents"]] == ["Duke"]  # not sub-agents
    assert t["providers"] == [{"id": ol.id, "name": "Ollama", "runs_on": "local"}]
    assert set(t["metrics"]) == {"requests", "tokens", "local_minutes", "cost_usd"}

    async def post(**kw):
        return await hq.http.post("/api/budgets", json={"scope": "agent", "target_id": duke.id,
                                                       "period": "day", "metric": "requests",
                                                       "limit": 100, **kw})
    for bad, why in (({"metric": "dollars"}, "metric"), ({"limit": 0}, "more than 0"),
                     ({"warn_pct": 100}, "between"), ({"period": "year"}, "period"),
                     ({"scope": "team"}, "scope"), ({"target_id": "nope"}, "no such agent"),
                     ({"target_id": sub.id}, "sub-agents")):
        r = await post(**bad)
        assert r.status_code == 400 and why in r.json()["detail"], (bad, r.text)
    r = await post(note="keep Duke modest")
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["target_name"] == "Duke" and b["used"] == 0 and b["level"] == "ok" and b["pct"] == 0
    r = await hq.http.post("/api/budgets", json={"scope": "provider", "target_id": ol.id, "period": "week",
                                                 "metric": "local_minutes", "limit": 600})
    assert r.status_code == 201
    assert len((await hq.http.get("/api/budgets")).json()) == 2

    # Changing a budget clears its sent warnings, so it warns afresh.
    with db.session() as s:
        row = s.get(SpendBudget, b["id"])
        row.state = {"period": "x", "levels": [80]}
        s.add(row)
        s.commit()
    r = await hq.http.put(f"/api/budgets/{b['id']}", json={"note": "only the note"})
    with db.session() as s:
        assert s.get(SpendBudget, b["id"]).state == {"period": "x", "levels": [80]}
    r = await hq.http.put(f"/api/budgets/{b['id']}", json={"limit": 50})
    assert r.status_code == 200 and r.json()["limit"] == 50
    with db.session() as s:
        assert s.get(SpendBudget, b["id"]).state == {}
    assert (await hq.http.put(f"/api/budgets/{b['id']}", json={"limit": -1})).status_code == 400
    assert (await hq.http.put("/api/budgets/nope", json={"limit": 5})).status_code == 404
    assert (await hq.http.delete(f"/api/budgets/{b['id']}")).status_code == 204
    assert (await hq.http.delete(f"/api/budgets/{b['id']}")).status_code == 404
    assert len((await hq.http.get("/api/budgets")).json()) == 1


# -------------------------------------------------------------------- warnings

async def test_an_agent_budget_warns_once_per_level_and_period(hq, sent):
    duke = agent_row("Duke")
    sub = agent_row("Duke #1", is_subagent=True, parent_agent_id=duke.id)
    other = agent_row("Scarlett")
    b = budget(scope="agent", target_id=duke.id, limit=10)
    run(duke, requests=5)
    run(other, requests=50)  # someone else's runs don't count
    assert await budgets.check(duke.id, None, None) == []
    run(sub, requests=3, sub_of=duke)  # a sub-agent's runs count under its parent
    [w] = await budgets.check(sub.id, duke.id, None)
    assert w["type"] == "budget_warning" and w["severity"] == "warning" and w["level"] == 80
    assert w["agent"] == "Duke" and w["pct"] == 80 and "8 of its 10 model requests budget today" in w["text"]
    assert "nothing has been stopped" in w["text"]
    assert await budgets.check(duke.id, None, None) == []  # once
    run(duke, requests=4)
    [w] = await budgets.check(duke.id, None, None)
    assert w["severity"] == "critical" and w["level"] == 100 and w["pct"] == 120
    assert await budgets.check(duke.id, None, None) == []
    # A new day starts over.
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    run(duke, requests=9, at=tomorrow)
    [w] = await budgets.check(duke.id, None, None, now=tomorrow)
    assert w["level"] == 80
    with db.session() as s:
        assert s.get(SpendBudget, b.id).state["levels"] == [80]
    assert len(sent) == 3  # every one went to the notification channels
    # Straight past both levels at once: one notice, the critical one.
    b2 = budget(scope="agent", target_id=other.id, limit=10)
    [w] = await budgets.check(other.id, None, None)
    assert w["level"] == 100
    with db.session() as s:
        assert s.get(SpendBudget, b2.id).state["levels"] == [80, 100]
    # A disabled budget never warns.
    with db.session() as s:
        row = s.get(SpendBudget, b2.id)
        row.enabled, row.state = False, {}
        s.add(row)
        s.commit()
    assert await budgets.check(other.id, None, None) == []


async def test_provider_budgets_on_local_minutes_and_api_cost(hq, sent):
    budgets.store_prices(budgets._slim(PRICE_LIST), source="test")
    duke = agent_row("Duke")
    ol = provider("Ollama", ProviderKind.ollama, "http://einstein:11434")
    oa = provider("OpenAI", ProviderKind.openai)
    gpu = budget(scope="provider", target_id=ol.id, metric="local_minutes", limit=10)
    money = budget(scope="provider", target_id=oa.id, metric="cost_usd", limit=1.0, period="month")
    run(duke, ol, seconds=7 * 60)
    run(duke, oa, model="gpt-4.1", tin=100_000, tout=50_000, seconds=999)  # $0.60, not local
    assert await budgets.check(duke.id, None, ol.id) == []
    assert await budgets.check(duke.id, None, oa.id) == []
    run(duke, ol, seconds=2 * 60)
    [w] = await budgets.check(duke.id, None, ol.id)
    assert w["scope"] == "provider" and w["target"] == "Ollama" and w["agent"] == "Duke"
    assert "9 min of its 10 min on your hardware budget today" in w["text"]
    run(duke, oa, model="gpt-4.1", tin=100_000, tout=50_000)
    run(duke, oa, model="mystery-model", tin=10**6, tout=10**6)
    [w] = await budgets.check(duke.id, None, oa.id)
    assert w["level"] == 100 and "$1.20 of its $1.00" in w["text"]
    assert "1 run(s) on models without a known price" in w["text"]
    st = budgets.status(money)
    assert st["used"] == pytest.approx(1.2) and st["unpriced_runs"] == 1
    assert budgets.status(gpu)["used"] == pytest.approx(9.0)


async def test_a_finished_run_checks_its_budgets_by_itself(hq, sent):
    """End to end: a real turn lands on the ledger, and the warning follows."""
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:echo")
    r = await hq.http.post("/api/budgets", json={"scope": "agent", "target_id": a["id"], "period": "day",
                                                 "metric": "requests", "limit": 1})
    assert r.status_code == 201
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    await hq.wait_agent_replies(t["id"], 1)
    deadline = time.monotonic() + 10
    while not sent and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    [w] = [e for e in sent if e["type"] == "budget_warning"]
    assert w["agent"] == "Scarlett" and w["level"] == 100 and w["severity"] == "critical"
    # The reply itself was never in the way.
    assert (await hq.http.get("/api/budgets")).json()[0]["level"] == "over"


def test_the_notice_reads_well(hq):
    n = notifications.describe({"type": "budget_warning", "agent": "Duke", "severity": "critical",
                                "budget_id": "b1", "target": "OpenAI", "pct": 112.4,
                                "text": "OpenAI has used $11.24 of its $10.00 ..."})
    assert n.title == "🚨 Budget: OpenAI at 112%" and n.severity == "critical" and n.ref_id == "b1"
    assert n.body.startswith("OpenAI has used") and not n.actions
    assert notifications.TYPES["budget_warning"]["group"] == "alert"
    assert "budget_warning" in notifications.DEFAULT_CHAT_TYPES


# -------------------------------------------------------------------- the report

async def test_report_splits_by_where_it_ran_and_totals_goals(hq):
    budgets.store_prices(budgets._slim(PRICE_LIST), source="test")
    a = await hq.agent(name="Duke", model="fake:echo")
    duke = agent_row("Mainframe")
    ol = provider("Ollama", ProviderKind.ollama, "http://einstein:11434")
    gpt = provider("ChatGPT", ProviderKind.chatgpt)
    oa = provider("OpenAI", ProviderKind.openai)
    t = await hq.thread(a["id"])
    goal = WorkItem(kind="goal", title="Patch the Proxmox hosts", owner_agent_id=a["id"], thread_id=t["id"],
                    status=WorkStatus.in_progress, started_at=datetime.now(UTC) - timedelta(hours=2), turns=4)
    with db.session() as s:
        s.add(goal)
        s.commit()
        s.refresh(goal)
    run(duke, ol, requests=3, tin=1000, tout=200, seconds=120, thread_id=t["id"])
    run(duke, gpt, requests=2, tin=500, tout=100, seconds=30, thread_id=t["id"])
    run(duke, oa, model="gpt-4.1", requests=1, tin=1_000_000, tout=0, seconds=5, thread_id=t["id"])
    run(duke, ol, requests=9, seconds=60, thread_id=t["id"], at=datetime.now(UTC) - timedelta(hours=5))  # before the goal
    run(duke, oa, model="gpt-9", requests=1, tin=10, tout=10)
    run(duke, ol, requests=100, at=datetime.now(UTC) - timedelta(days=30))  # outside the window
    r = (await hq.http.get("/api/budgets/report?days=7")).json()
    c = r["by_class"]
    assert c["local"]["runs"] == 2 and c["local"]["seconds"] == 180 and c["local"]["requests"] == 12
    assert c["plan"]["requests"] == 2 and c["plan"]["cost_usd"] == 0
    assert c["api"]["runs"] == 2 and c["api"]["cost_usd"] == pytest.approx(2.0) and c["api"]["unpriced_runs"] == 1
    assert "unknown" not in c
    assert [p["provider"] for p in r["by_provider"]] == ["Ollama", "ChatGPT", "OpenAI"]
    oa_row = r["by_provider"][2]
    assert {m["model"]: m["price_per_mtok"] for m in oa_row["models"]} == {"gpt-4.1": [2.0, 8.0], "gpt-9": None}
    [g] = r["goals"]
    assert g["title"] == "Patch the Proxmox hosts" and g["agent"] == "Duke" and g["status"] == "in_progress"
    assert g["requests"] == 6 and g["local_seconds"] == 120 and g["cloud_requests"] == 3
    assert g["cost_usd"] == pytest.approx(2.0) and g["turns"] == 4
    assert r["pricing"]["models"] == 3 and r["pricing"]["enabled"] is True
    assert r["budgets"] == []


# -------------------------------------------------------------------- the agent tool

def budgeter(messages, info):
    """Calls check_budget if it has it, then replies with what came back."""
    if "check_budget" not in {t.name for t in info.function_tools}:
        return ModelResponse(parts=[TextPart("no budget tool")])
    returns = [r for r in _tool_returns(messages) if r.tool_name == "check_budget"]
    if not returns:
        return ModelResponse(parts=[ToolCallPart("check_budget", {})])
    return ModelResponse(parts=[TextPart(json.dumps(returns[-1].content))])


async def test_agents_that_choose_models_can_check_their_budgets(hq, monkeypatch):
    monkeypatch.setitem(SCRIPTS, "budgeter", budgeter)
    budgets.store_prices(budgets._slim(PRICE_LIST), source="test")
    await hq.start_worker()
    ol = provider("Ollama", ProviderKind.ollama, "http://einstein:11434")
    oa = provider("OpenAI", ProviderKind.openai)
    with db.session() as s:
        row = s.get(ModelProvider, ol.id)
        row.max_concurrent = 2
        s.add(row)
        s.commit()
    plain = await hq.agent(name="Plain", model="fake:budgeter")
    t = await hq.thread(plain["id"])
    await hq.say(t["id"], "what can I spend?")
    assert await hq.wait_agent_replies(t["id"], 1) == ["no budget tool"]

    limits = {**subagents.default_subagent_limits(),
              "models": [{"provider_id": oa.id, "model": "gpt-4.1"}, {"provider_id": ol.id, "model": "qwen3:8b"}]}
    a = await hq.agent(name="Duke", model="fake:budgeter", subagent_mode="ask", subagent_limits=limits)
    with db.session() as s:
        duke = s.get(AgentDef, a["id"])
    budget(scope="agent", target_id=a["id"], limit=100)
    budget(scope="provider", target_id=oa.id, metric="cost_usd", limit=5, period="month")
    run(duke, oa, model="gpt-4.1", tin=500_000, tout=0)  # $1
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "what can I spend?")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    got = json.loads(reply)
    assert got["your_budgets"][0]["what"].startswith("Duke: ") and "of 100 model requests today" in got["your_budgets"][0]["what"]
    models = {m["model"]: m for m in got["models"]}
    assert models["gpt-4.1"]["runs_on"] == "a per-token cloud API" and models["gpt-4.1"]["use"] == "for sub-agents"
    assert models["gpt-4.1"]["price_per_million_tokens"] == {"input": 2.0, "output": 8.0}
    assert models["gpt-4.1"]["budgets"][0]["pct"] == pytest.approx(20.0)
    assert models["qwen3:8b"]["runs_on"] == "your user's hardware"
    assert models["qwen3:8b"]["busy"] == "0 of 2 request slots in use" and "price_per_million_tokens" not in models["qwen3:8b"]
    assert "only warn" in got["note"]
    # The sub-agent request estimate shows the same headroom.
    lines = budgets.headroom(oa.id, a["id"])
    assert any(x.startswith("OpenAI: $1.00 of $5.00 estimated API cost") for x in lines)
    assert any(x.startswith("Duke: ") for x in lines)


# -------------------------------------------------------------------- clean-up

async def test_budgets_go_with_their_agent_or_provider(hq):
    a = await hq.agent(name="Duke", model="fake:echo")
    ol = provider("Ollama", ProviderKind.ollama, "http://einstein:11434")
    budget(scope="agent", target_id=a["id"], limit=10)
    budget(scope="provider", target_id=ol.id, limit=10)
    assert (await hq.http.delete(f"/api/agents/{a['id']}")).status_code == 204
    assert (await hq.http.get(f"/api/agents/{a['id']}/delete-plan")).json()["budgets"] == 1
    assert (await hq.http.delete(f"/api/agents/{a['id']}/permanent")).status_code in (200, 204)
    assert (await hq.http.delete(f"/api/providers/{ol.id}")).status_code in (200, 204)
    with db.session() as s:
        assert s.exec(select(SpendBudget)).all() == []
