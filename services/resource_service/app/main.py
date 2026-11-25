"""Resource Service (RMS): ресурси аеропорту, автоматичний і ручний розподіл, конфлікти."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from aerohub_common.auth import Principal, Roles, current_user, require_roles
from aerohub_common.bus import build_bus
from aerohub_common.config import get_settings
from aerohub_common.db import ensure_database, make_engine, make_session_factory, session_scope
from aerohub_common.events import Envelope, Topics
from aerohub_common.migrations import apply_migrations
from aerohub_common.outbox import OutboxRelay

from . import service
from .allocation import seed_resources
from .models import OUTBOX, Base, Resource, ResourceType
from .service import ResourceError


# Міграції схеми після першого випуску (версія, опис, SQL)
MIGRATIONS = [
    (1, "flight_cache.linked_flight_id для спільної стоянки пари рейсів",
     "ALTER TABLE flight_cache ADD COLUMN IF NOT EXISTS linked_flight_id uuid"),
]


class ResourceIn(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    type: str = Field(pattern="^(STAND|GATE|DESK|BELT)$")
    terminal: str = "D"
    zone: str = Field("ANY", pattern="^(SCHENGEN|NON_SCHENGEN|ANY)$")
    max_category: str = Field("C", pattern="^[A-F]$")
    contact: bool = False


class ResourcePatch(BaseModel):
    zone: str | None = Field(None, pattern="^(SCHENGEN|NON_SCHENGEN|ANY)$")
    max_category: str | None = Field(None, pattern="^[A-F]$")
    contact: bool | None = None
    active: bool | None = None


class ManualAssign(BaseModel):
    kind: str = Field(pattern="^(STAND|GATE|DESK|BELT)$")
    resource_codes: list[str] = Field(min_length=1)


class UnavailabilityIn(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=2, max_length=200)


def create_app(bus=None, database_url: str | None = None) -> FastAPI:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_session_factory(engine)
    bus = bus or build_bus(settings.event_bus, settings.kafka_bootstrap, "resource-service")
    relay = OutboxRelay(factory, OUTBOX, bus, settings.outbox_poll_seconds)

    async def on_flight_event(env: Envelope) -> None:
        def work():
            with session_scope(factory) as s:
                service.handle_flight_event(s, env)
        await asyncio.to_thread(work)

    bus.subscribe([Topics.FLIGHT_EVENTS], "resource-service", on_flight_event)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_database(database_url or settings.database_url)
        with engine.begin() as c:
            c.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        Base.metadata.create_all(engine)
        apply_migrations(engine, MIGRATIONS)
        with session_scope(factory) as s:
            seed_resources(s)
        await bus.start()
        relay.start()
        yield
        await relay.stop()
        await bus.stop()

    app = FastAPI(title="AeroHub Resource Service (RMS)", version="0.1.0", lifespan=lifespan,
                  description="Стоянки, гейти, стійки реєстрації, багажні стрічки; розподіл і конфлікти")
    app.state.factory, app.state.relay, app.state.engine, app.state.on_flight_event = factory, relay, engine, on_flight_event

    @app.exception_handler(ResourceError)
    async def res_error(_: Request, exc: ResourceError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def integrity(_: Request, exc: IntegrityError):
        return JSONResponse(status_code=409, content={"detail": "Конфлікт: ресурс уже зайнятий або код ресурсу повторюється"})

    tx = lambda: session_scope(factory)  # noqa: E731

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "resource-service"}

    @app.get("/api/v1/resources", tags=["resources"])
    def list_resources(type: str | None = None, _=Depends(current_user)):
        with tx() as s:
            stmt = select(Resource).order_by(Resource.type, Resource.code)
            if type:
                stmt = stmt.where(Resource.type == type.upper())
            return [r.to_dict() for r in s.scalars(stmt)]

    @app.post("/api/v1/resources", status_code=201, tags=["resources"])
    def create_resource(data: ResourceIn, _: Principal = Depends(require_roles(Roles.PLANNER))):
        with tx() as s:
            r = Resource(**{**data.model_dump(), "code": data.code.upper()})
            s.add(r)
            s.flush()
            return r.to_dict()

    @app.patch("/api/v1/resources/{resource_id}", tags=["resources"])
    def patch_resource(resource_id: int, data: ResourcePatch, _: Principal = Depends(require_roles(Roles.PLANNER))):
        with tx() as s:
            r = s.get(Resource, resource_id)
            if r is None:
                raise ResourceError("Ресурс не знайдено", 404)
            for k, v in data.model_dump(exclude_unset=True).items():
                setattr(r, k, v)
            return r.to_dict()

    @app.post("/api/v1/resources/{resource_id}/unavailability", tags=["resources"])
    def close_resource(resource_id: int, data: UnavailabilityIn, _: Principal = Depends(require_roles(Roles.PLANNER, Roles.AOC))):
        with tx() as s:
            affected = service.add_unavailability(s, resource_id, data.start_at, data.end_at, data.reason)
            return {"reallocated_flights": affected}

    @app.get("/api/v1/allocations", tags=["allocations"])
    def allocations(date_: date | None = Query(None, alias="date"), _=Depends(current_user)):
        with tx() as s:
            return service.allocations_for(s, date_)

    @app.get("/api/v1/allocations/conflicts", tags=["allocations"])
    def conflicts(date_: date | None = Query(None, alias="date"), _=Depends(current_user)):
        with tx() as s:
            return service.conflicts_for(s, date_)

    @app.put("/api/v1/allocations/{flight_id}", tags=["allocations"])
    def assign(flight_id: str, data: ManualAssign, user: Principal = Depends(require_roles(Roles.PLANNER, Roles.AOC))):
        with tx() as s:
            return [a.to_dict() for a in service.assign_manual(s, flight_id, data.kind, data.resource_codes, user.username)]

    @app.post("/api/v1/allocations/auto", tags=["allocations"])
    def auto(date_: date = Query(..., alias="date"), _: Principal = Depends(require_roles(Roles.PLANNER))):
        with tx() as s:
            return {"flights": service.reallocate_date(s, date_)}

    @app.get("/api/v1/resource-types", tags=["resources"])
    def types():
        return list(ResourceType.ALL)

    return app
