"""Evals: test cases, runs and their results, and settings (Settings → Evals)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import func, select

from .. import db, evals
from ..models import AgentDef, EvalCase, EvalResult, EvalRun, Message, Role

router = APIRouter(tags=["evals"])


@router.get("/api/evals/settings")
def get_settings():
    return evals.get_settings()


@router.put("/api/evals/settings")
def put_settings(body: dict[str, Any]):
    try:
        return evals.save_settings(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None


# --------------------------------------------------------------------------- cases


class CaseIn(BaseModel):
    name: str = ""
    agent_id: str
    kind: str = "turn"
    prompt: str
    context: str = ""
    checks: list[dict] = Field(default_factory=list)
    recording: list[dict] | None = None
    skill_names: list[str] = Field(default_factory=list)
    status: str = "active"


def _validate(body: CaseIn) -> None:
    if not body.prompt.strip():
        raise HTTPException(422, "a test case needs a request")
    if body.status not in ("draft", "active", "archived"):
        raise HTTPException(422, "status is draft, active or archived")
    if body.kind not in ("turn", "task"):
        raise HTTPException(422, "kind is turn or task")
    with db.session() as s:
        if s.get(AgentDef, body.agent_id) is None:
            raise HTTPException(422, "no such agent")
    if body.status == "active" and not evals.clean_checks(body.checks):
        raise HTTPException(422, "add at least one check before keeping it")


@router.get("/api/evals/cases")
def list_cases(status: str | None = None, agent_id: str | None = None):
    q = select(EvalCase).order_by(EvalCase.created_at.desc())
    if status:
        q = q.where(EvalCase.status == status)
    if agent_id:
        q = q.where(EvalCase.agent_id == agent_id)
    with db.session() as s:
        rows = list(s.exec(q).all())
        counts = dict(s.exec(select(EvalCase.status, func.count()).group_by(EvalCase.status)).all())
    return {"cases": rows, "counts": counts}


@router.post("/api/evals/cases", status_code=201)
def create_case(body: CaseIn):
    _validate(body)
    data = body.model_dump()
    data["checks"] = evals.clean_checks(body.checks)
    data["recording"] = body.recording or []
    case = EvalCase(source="manual", **data)
    case.name = case.name.strip() or case.prompt.strip().splitlines()[0][:80]
    with db.session() as s:
        s.add(case)
        s.commit()
        s.refresh(case)
    return case


class FromMessageIn(BaseModel):
    message_id: str


@router.post("/api/evals/cases/from-message", status_code=201)
def case_from_message(body: FromMessageIn):
    """'Make this a test': a draft case from an agent reply (its request and tool results)."""
    with db.session() as s:
        m = s.get(Message, body.message_id)
    if m is None:
        raise HTTPException(404)
    if m.role != Role.agent or not (m.meta or {}).get("job_id"):
        raise HTTPException(422, "only agent replies from a job can become test cases")
    fb = (m.meta or {}).get("feedback") or {}
    rubric = ""
    if fb.get("comment"):
        rubric = f"The user rated an earlier answer {'down' if fb.get('rating') == 'down' else 'up'}: “{fb['comment']}”."
    try:
        return evals.case_from_job(m.meta["job_id"], source="reply", message_id=m.id, rubric=rubric)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/api/evals/cases/{case_id}")
def get_case(case_id: str):
    with db.session() as s:
        c = s.get(EvalCase, case_id)
        if c is None:
            raise HTTPException(404)
        results = list(s.exec(select(EvalResult).where(EvalResult.case_id == case_id)
                              .order_by(EvalResult.created_at.desc()).limit(10)).all())
    return {**c.model_dump(), "results": results}


@router.put("/api/evals/cases/{case_id}")
def update_case(case_id: str, body: CaseIn):
    _validate(body)
    with db.session() as s:
        c = s.get(EvalCase, case_id)
        if c is None:
            raise HTTPException(404)
        for k, v in body.model_dump().items():
            if k == "recording" and v is None:
                continue
            setattr(c, k, evals.clean_checks(v) if k == "checks" else v)
        c.name = c.name.strip() or c.prompt.strip().splitlines()[0][:80]
        c.updated_at = evals._now()
        s.add(c)
        s.commit()
        s.refresh(c)
    return c


@router.delete("/api/evals/cases/{case_id}")
def delete_case(case_id: str):
    with db.session() as s:
        c = s.get(EvalCase, case_id)
        if c is None:
            raise HTTPException(404)
        s.delete(c)
        s.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- runs


class RunIn(BaseModel):
    case_ids: list[str] = Field(default_factory=list)  # empty = every active case
    agent_id: str | None = None                          # or: every active case of this agent
    label: str = ""


@router.post("/api/evals/runs", status_code=202)
def start_run(body: RunIn):
    ids = body.case_ids
    if not ids:
        q = select(EvalCase.id).where(EvalCase.status == "active")
        if body.agent_id:
            q = q.where(EvalCase.agent_id == body.agent_id)
        with db.session() as s:
            ids = list(s.exec(q).all())
    if not ids:
        raise HTTPException(422, "no test cases to run (keep a draft or add one first)")
    label = body.label or (f"{len(ids)} case{'s' if len(ids) != 1 else ''}")
    return evals.queue_run(ids, label)


@router.get("/api/evals/runs")
def list_runs(limit: int = 50):
    with db.session() as s:
        return list(s.exec(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(max(1, min(limit, 500)))).all())


@router.get("/api/evals/runs/{run_id}")
def get_run(run_id: str):
    with db.session() as s:
        r = s.get(EvalRun, run_id)
        if r is None:
            raise HTTPException(404)
        results = list(s.exec(select(EvalResult).where(EvalResult.run_id == run_id)
                              .order_by(EvalResult.created_at)).all())
        names = {c.id: c.name for c in s.exec(select(EvalCase).where(EvalCase.id.in_(r.case_ids))).all()}
    return {**r.model_dump(), "results": [{**x.model_dump(), "case_name": names.get(x.case_id, "(deleted)")}
                                          for x in results]}
