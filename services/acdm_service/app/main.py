"""A-CDM Service: етапи обробки рейсу, TOBT/TSAT, DPI-повідомлення."""

from __future__ import annotations

import asyncio
import uuid
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
from .models import OUTBOX, AcdmFlight, Base, DpiMessage
from .service import AcdmError


class TobtIn(BaseModel):
    tobt: datetime


class MilestoneIn(BaseModel):
    code: str
    value: datetime


def create_app(bus=None, database_url: str | None = None) -> FastAPI:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_session_factory(engine)
    bus = bus or build_bus(settings.event_bus, settings.kafka_bootstrap, "acdm-service")
    relay = OutboxRelay(factory, OUTBOX, bus, settings.outbox_poll_seconds)

    def handler(fn):
        async def run(env: Envelope) -> None:
            def work():
                with session_scope(factory) as s:
                    fn(s, env)
            await asyncio.to_thread(work)
        return run

    on_flight, on_task = handler(service.handle_flight_event), handler(service.handle_task_event)
    bus.subscribe([Topics.FLIGHT_EVENTS], "acdm-service", on_flight)
    bus.subscribe([Topics.TASK_EVENTS], "acdm-service-tasks", on_task)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_database(database_url or settings.database_url)
        Base.metadata.create_all(engine)
        await bus.start()
        relay.start()
        yield
        await relay.stop()
        await bus.stop()

    app = FastAPI(title="AeroHub A-CDM Service", version="1.0.0", lifespan=lifespan,
                  description="Airport Collaborative Decision Making: етапи, TOBT, TSAT, DPI")
    app.state.engine, app.state.relay, app.state.on_flight, app.state.on_task = engine, relay, on_flight, on_task

    @app.exception_handler(AcdmError)
    async def err(_: Request, exc: AcdmError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    tx = lambda: session_scope(factory)  # noqa: E731

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "acdm-service"}

    @app.get("/api/v1/acdm/flights", tags=["acdm"])
    def flights(date_: date = Query(..., alias="date"), direction: str | None = None, _=Depends(current_user)):
        with tx() as s:
            stmt = select(AcdmFlight).where(AcdmFlight.sched_date == date_).order_by(AcdmFlight.estimated_at)
            if direction:
                stmt = stmt.where(AcdmFlight.direction == direction)
            return [service.flight_view(s, f) for f in s.scalars(stmt)]

    @app.get("/api/v1/acdm/flights/{flight_id}", tags=["acdm"])
    def flight(flight_id: str, _=Depends(current_user)):
        with tx() as s:
            f = s.get(AcdmFlight, uuid.UUID(flight_id))
            if f is None:
                raise AcdmError("Рейс не знайдено", 404)
            return service.flight_view(s, f)

    @app.put("/api/v1/acdm/flights/{flight_id}/tobt", tags=["acdm"])
    def tobt(flight_id: str, data: TobtIn, user: Principal = Depends(require_roles(Roles.HANDLER, Roles.AOC, Roles.AGENT))):
        with tx() as s:
            return service.flight_view(s, service.set_tobt(s, flight_id, data.tobt, user.username))

    @app.post("/api/v1/acdm/flights/{flight_id}/milestones", status_code=201, tags=["acdm"])
    def milestone(flight_id: str, data: MilestoneIn, user: Principal = Depends(require_roles(Roles.AOC))):
        with tx() as s:
            return service.add_milestone(s, flight_id, data.code, data.value, user.username).to_dict()

    @app.get("/api/v1/acdm/flights/{flight_id}/dpi", tags=["acdm"])
    def dpi(flight_id: str, _=Depends(current_user)):
        with tx() as s:
            rows = s.scalars(select(DpiMessage).where(DpiMessage.flight_id == uuid.UUID(flight_id)).order_by(DpiMessage.id))
            return [m.to_dict() for m in rows]

    return app
