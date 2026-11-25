"""Reports → Budgets: the ledger by where the work ran (your hardware, a plan, a per-token
API), per goal, estimated API cost, and budgets that warn near a limit. The logic is in
app/budgets.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from .. import budgets, db
from ..models import AgentDef, SpendBudget, UsageRecord

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


@router.get("/report")
def report(days: int = 7) -> dict[str, Any]:
    days = max(1, min(days, 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    provs = budgets.providers()
    with db.session() as s:
        rows = list(s.exec(select(UsageRecord).where(UsageRecord.at >= since)).all())
    cfg = budgets.get_settings()
    return {"days": days, **budgets.split(rows, provs),
            "goals": budgets.goal_totals(since, provs),
            "budgets": budgets.all_status(),
            "pricing": {**budgets.price_info(), "enabled": cfg["pricing"]}}


@router.get("")
def list_budgets() -> list[dict[str, Any]]:
    return budgets.all_status()


@router.get("/targets")
def targets() -> dict[str, Any]:
    """What a budget can be on: your agents (not sub-agents) and your providers."""
    with db.session() as s:
        agents = s.exec(select(AgentDef).where(AgentDef.is_subagent == False)).all()  # noqa: E712
    provs = budgets.providers().values()
    return {"agents": sorted(({"id": a.id, "name": a.name} for a in agents), key=lambda a: a["name"].lower()),
            "providers": sorted(({"id": p.id, "name": p.name, "runs_on": budgets.classify(p)} for p in provs),
                                key=lambda p: p["name"].lower()),
            "metrics": budgets.METRICS, "periods": list(budgets.PERIODS)}


@router.post("", status_code=201)
def create(data: dict[str, Any]) -> dict[str, Any]:
    try:
        b = budgets.validate(data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    with db.session() as s:
        s.add(b)
        s.commit()
        s.refresh(b)
    return budgets.status(b)


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return {**budgets.get_settings(), "prices": budgets.price_info()}


@router.put("/settings")
def put_settings(data: dict[str, Any]) -> dict[str, Any]:
    return {**budgets.save_settings(data), "prices": budgets.price_info()}


@router.post("/prices/refresh")
async def refresh_prices() -> dict[str, Any]:
    return await budgets.refresh_prices()


@router.put("/{budget_id}")
def update(budget_id: str, data: dict[str, Any]) -> dict[str, Any]:
    with db.session() as s:
        b = s.get(SpendBudget, budget_id)
        if b is None:
            raise HTTPException(404, "No such budget")
        before = (b.scope, b.target_id, b.period, b.metric, b.limit, b.warn_pct)
        try:
            budgets.validate(data, b)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if (b.scope, b.target_id, b.period, b.metric, b.limit, b.warn_pct) != before:
            b.state = {}  # a changed budget warns afresh
        s.add(b)
        s.commit()
        s.refresh(b)
    return budgets.status(b)


@router.delete("/{budget_id}", status_code=204)
def delete(budget_id: str) -> None:
    with db.session() as s:
        b = s.get(SpendBudget, budget_id)
        if b is None:
            raise HTTPException(404, "No such budget")
        s.delete(b)
        s.commit()
