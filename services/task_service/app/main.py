"""Task Service (TMS): завдання наземного обслуговування рейсів, виконання, контроль SLA."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from aerohub_common.auth import Principal, Roles, current_user, require_roles
from aerohub_common.bus import build_bus
from aerohub_common.config import get_settings
from aerohub_common.db import ensure_database, make_engine, make_session_factory, session_scope
from aerohub_common.events import Envelope, Topics
from aerohub_common.outbox import OutboxRelay

from . import service
from .models import OUTBOX, Base, GroundTask, TaskTemplate
from .service import TaskError

log = logging.getLogger("aerohub.tasks")


class TaskAction(BaseModel):
    version: int
    at: datetime | None = None


def create_app(bus=None, database_url: str | None = None, overdue_check_seconds: float = 30) -> FastAPI:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_session_factory(engine)
    bus = bus or build_bus(settings.event_bus, settings.kafka_bootstrap, "task-service")
    relay = OutboxRelay(factory, OUTBOX, bus, settings.outbox_poll_seconds)

    def handler(fn):
        async def run(env: Envelope) -> None:
            def work():
                with session_scope(factory) as s:
                    fn(s, env)
            await asyncio.to_thread(work)
        return run

    on_flight, on_acdm = handler(service.handle_flight_event), handler(service.handle_acdm_event)
    bus.subscribe([Topics.FLIGHT_EVENTS], "task-service", on_flight)
    bus.subscribe([Topics.ACDM_MILESTONES], "task-service-acdm", on_acdm)

    async def overdue_loop():
        while True:
            await asyncio.sleep(overdue_check_seconds)
            try:
                def work():
                    with session_scope(factory) as s:
                        return len(service.mark_overdue(s))
                await asyncio.to_thread(work)
            except Exception:
                log.exception("Помилка перевірки прострочених завдань")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_database(database_url or settings.database_url)
        Base.metadata.create_all(engine)
        with session_scope(factory) as s:
            service.seed_templates(s)
        await bus.start()
        relay.start()
        task = asyncio.create_task(overdue_loop())
        yield
        task.cancel()
        await relay.stop()
        await bus.stop()

    app = FastAPI(title="AeroHub Task Service (TMS)", version="1.0.0", lifespan=lifespan,
                  description="Завдання наземного обслуговування: генерація, виконання, SLA")
    app.state.engine, app.state.relay, app.state.on_flight, app.state.on_acdm, app.state.factory = engine, relay, on_flight, on_acdm, factory

    @app.exception_handler(TaskError)
    async def err(_: Request, exc: TaskError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    tx = lambda: session_scope(factory)  # noqa: E731

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "task-service"}

    @app.get("/api/v1/tasks", tags=["tasks"])
    def tasks(date_: date | None = Query(None, alias="date"), team: str | None = None, flight_id: str | None = None,
              status: str | None = None, overdue: bool | None = None, _=Depends(current_user)):
        with tx() as s:
            stmt = select(GroundTask).order_by(GroundTask.planned_start, GroundTask.flight_no)
            if date_:
                stmt = stmt.where(GroundTask.sched_date == date_)
            if team:
                stmt = stmt.where(GroundTask.team == team)
            if flight_id:
                stmt = stmt.where(GroundTask.flight_id == flight_id)
            if status:
                stmt = stmt.where(GroundTask.status == status)
            if overdue is not None:
                stmt = stmt.where(GroundTask.overdue.is_(overdue))
            return [t.to_dict() for t in s.scalars(stmt)]

    @app.get("/api/v1/tasks/templates", tags=["tasks"])
    def templates(_=Depends(current_user)):
        with tx() as s:
            return [{"code": t.code, "name": t.name, "direction": t.direction, "offset_min": t.offset_min,
                     "duration_min": t.duration_min, "team": t.team} for t in s.scalars(select(TaskTemplate).order_by(TaskTemplate.sort))]

    @app.post("/api/v1/tasks/{task_id}/start", tags=["tasks"])
    def start(task_id: str, data: TaskAction, user: Principal = Depends(require_roles(Roles.HANDLER, Roles.AOC))):
        with tx() as s:
            return service.act(s, task_id, "start", data.version, user.username, data.at).to_dict()

    @app.post("/api/v1/tasks/{task_id}/complete", tags=["tasks"])
    def complete(task_id: str, data: TaskAction, user: Principal = Depends(require_roles(Roles.HANDLER, Roles.AOC))):
        with tx() as s:
            return service.act(s, task_id, "complete", data.version, user.username, data.at).to_dict()

    return app
