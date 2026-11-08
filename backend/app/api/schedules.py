"""Schedules: agent tasks that run on a cadence (see app/scheduler.py)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import scheduler
from ..db import get_session
from ..models import AgentDef, Job, Schedule

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class ScheduleIn(BaseModel):
    name: str
    agent_id: str
    task: str
    frequency: str = "daily"
    interval_hours: int = 1
    minute: int = 0
    hour: int = 9
    weekdays: list[int] = [1]
    day_of_month: int = 1
    cron: str = ""
    timezone: str = "UTC"
    enabled: bool = True
    skip_if_running: bool = True


class PreviewIn(BaseModel):
    frequency: str = "daily"
    interval_hours: int = 1
    minute: int = 0
    hour: int = 9
    weekdays: list[int] = [1]
    day_of_month: int = 1
    cron: str = ""
    timezone: str = "UTC"


def _iso(dt: datetime | None) -> str | None:
    return scheduler.aware(dt).isoformat().replace("+00:00", "Z") if dt else None


def out(s: Schedule, ses: Session) -> dict:
    """A schedule for the UI: its fields, a plain-English cadence, and recent runs with the
    status of each run's job."""
    agent = ses.get(AgentDef, s.agent_id)
    runs = []
    for r in s.runs or []:
        status = "skipped" if r.get("skipped") else "queued"
        if r.get("job_id"):
            job = ses.get(Job, r["job_id"])
            status = job.status.value if job else "unknown"
        at = r.get("at")
        runs.append({**r, "at": _iso(datetime.fromisoformat(at)) if at else None, "status": status})
    return {**s.model_dump(exclude={"runs", "next_run_at", "last_run_at", "created_at"}),
            "summary": scheduler.describe(s), "agent_name": agent.name if agent else None,
            "next_run_at": _iso(s.next_run_at), "last_run_at": _iso(s.last_run_at),
            "created_at": _iso(s.created_at),
            "runs": runs, "last_status": runs[0]["status"] if runs else None}


def _validate_agent(ses: Session, agent_id: str) -> None:
    if not ses.get(AgentDef, agent_id):
        raise HTTPException(422, "choose an agent for this schedule")


@router.get("")
def list_schedules(ses: Session = Depends(get_session)):
    rows = ses.exec(select(Schedule).order_by(Schedule.name)).all()
    return [out(s, ses) for s in rows]


@router.post("/preview")
def preview(body: PreviewIn):
    """The cron expression, a description and the next few run times for a cadence."""
    try:
        draft = Schedule(name="preview", agent_id="", task="x", **body.model_dump(exclude={"cron"}))
        draft.cron = scheduler.build_cron(body.frequency, minute=body.minute, hour=body.hour,
                                          interval_hours=body.interval_hours, weekdays=body.weekdays,
                                          day_of_month=body.day_of_month, cron=body.cron)
        upcoming = scheduler.next_runs(draft.cron, body.timezone, count=3)
    except scheduler.ScheduleError as e:
        raise HTTPException(422, str(e)) from e
    return {"cron": draft.cron, "summary": scheduler.describe(draft), "next": [_iso(t) for t in upcoming]}


@router.post("")
def create_schedule(body: ScheduleIn, ses: Session = Depends(get_session)):
    _validate_agent(ses, body.agent_id)
    try:
        s = scheduler.apply(Schedule(name=body.name, agent_id=body.agent_id), body.model_dump())
    except scheduler.ScheduleError as e:
        raise HTTPException(422, str(e)) from e
    ses.add(s)
    ses.commit()
    ses.refresh(s)
    return out(s, ses)


@router.put("/{schedule_id}")
def update_schedule(schedule_id: str, body: ScheduleIn, ses: Session = Depends(get_session)):
    s = ses.get(Schedule, schedule_id)
    if not s:
        raise HTTPException(404)
    _validate_agent(ses, body.agent_id)
    try:
        scheduler.apply(s, body.model_dump())
    except scheduler.ScheduleError as e:
        raise HTTPException(422, str(e)) from e
    ses.add(s)
    ses.commit()
    ses.refresh(s)
    return out(s, ses)


@router.delete("/{schedule_id}")
def delete_schedule(schedule_id: str, ses: Session = Depends(get_session)):
    """Delete the schedule. Its conversation (and past results) stays in the sidebar."""
    s = ses.get(Schedule, schedule_id)
    if not s:
        raise HTTPException(404)
    ses.delete(s)
    ses.commit()
    return {"ok": True}


@router.post("/{schedule_id}/run")
async def run_now(schedule_id: str, request: Request, ses: Session = Depends(get_session)):
    """Run it now, once. Doesn't change when it next runs on its schedule."""
    s = ses.get(Schedule, schedule_id)
    if not s:
        raise HTTPException(404)
    entry = await scheduler.fire(request.app.state.runtime, s, trigger="manual")
    if entry.get("skipped"):
        raise HTTPException(409, f"not run: {entry['reason']}")
    ses.expire_all()
    return out(ses.get(Schedule, schedule_id), ses)
