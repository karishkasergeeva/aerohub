"""FIDS Service: модель для читання (CQRS), публічний API статусу рейсів і WebSocket-оновлення.

Сервіс не звертається до баз інших сервісів: він будує власну таблицю для табло
з подій flight.events і resource.allocations. Кожна зміна одразу надсилається
підключеним табло, дашбордам і застосункам через WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import JSON, Date, DateTime, MetaData, String, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aerohub_common.bus import build_bus
from aerohub_common.config import get_settings
from aerohub_common.db import ensure_database, add_infra_tables, make_engine, make_session_factory, session_scope
from aerohub_common.events import Envelope, EventTypes, Topics
from aerohub_common.outbox import mark_processed

from .display import build_row

log = logging.getLogger("aerohub.fids")


class Base(DeclarativeBase):
    metadata = MetaData()


_OUTBOX, PROCESSED = add_infra_tables(Base.metadata)


class FidsFlight(Base):
    __tablename__ = "fids_flight"

    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    flight_no: Mapped[str | None] = mapped_column(String(8), index=True)
    direction: Mapped[str | None] = mapped_column(String(1))
    sched_date: Mapped[date | None] = mapped_column(Date, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flight: Mapped[dict | None] = mapped_column(JSON)
    allocations: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def row(self) -> dict | None:
        return build_row(self.flight, self.allocations or []) if self.flight else None


class Hub:
    """Реєстр WebSocket-підключень і розсилання оновлень."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)


def apply_event(s, env: Envelope) -> dict | None:
    """Оновлює модель читання. Повертає повідомлення для розсилання або None."""
    if not mark_processed(s, PROCESSED, env.event_id):
        return None
    fid = uuid.UUID(env.aggregate_id)
    rec = s.get(FidsFlight, fid)
    if env.type == EventTypes.FLIGHT_DELETED:
        if rec:
            s.delete(rec)
        return {"type": "fids.remove", "id": str(fid)}
    if env.type == EventTypes.RESOURCE_CONFLICT_DETECTED:
        return {"type": "resource.conflict", **env.payload}
    if rec is None:
        rec = FidsFlight(flight_id=fid, allocations=[])
        s.add(rec)
    if "flight" in env.payload:
        f = env.payload["flight"]
        rec.flight, rec.flight_no, rec.direction = f, f["flight_no"], f["direction"]
        rec.sched_date, rec.scheduled_at = date.fromisoformat(f["sched_date"]), datetime.fromisoformat(f["scheduled_at"])
    if "allocations" in env.payload:
        rec.allocations = env.payload["allocations"]
        rec.flight_no = rec.flight_no or env.payload.get("flight_no")
    s.flush()
    row = rec.row()
    return {"type": "fids.update", "event": env.type, "flight": row} if row else None


def create_app(bus=None, database_url: str | None = None) -> FastAPI:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_session_factory(engine)
    bus = bus or build_bus(settings.event_bus, settings.kafka_bootstrap, "fids-service")
    hub = Hub()

    async def on_event(env: Envelope) -> None:
        def work():
            with session_scope(factory) as s:
                return apply_event(s, env)
        message = await asyncio.to_thread(work)
        if message:
            await hub.broadcast(message)

    bus.subscribe([Topics.FLIGHT_EVENTS, Topics.RESOURCE_ALLOCATIONS], "fids-service", on_event)

    async def relay_event(env: Envelope) -> None:
        """Роль Realtime Gateway: події A-CDM і завдань пересилаються клієнтам без збереження."""
        await hub.broadcast({"type": "event", "event": env.type, "payload": env.payload})

    bus.subscribe([Topics.ACDM_MILESTONES, Topics.TASK_EVENTS], "fids-service-realtime", relay_event)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_database(database_url or settings.database_url)
        Base.metadata.create_all(engine)
        await bus.start()
        yield
        await bus.stop()

    app = FastAPI(title="AeroHub FIDS Service", version="0.1.0", lifespan=lifespan,
                  description="Публічна інформація про рейси, контент табло, WebSocket-оновлення")
    app.state.engine, app.state.on_event, app.state.hub, app.state.relay_event = engine, on_event, hub, relay_event

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "fids-service", "ws_clients": len(hub.clients)}

    @app.get("/api/v1/public/flights", tags=["public"])
    def board(direction: str | None = Query(None, pattern="^[AD]$"), date_: date | None = Query(None, alias="date")):
        with session_scope(factory) as s:
            stmt = select(FidsFlight).where(FidsFlight.flight.is_not(None)).order_by(FidsFlight.scheduled_at)
            if direction:
                stmt = stmt.where(FidsFlight.direction == direction)
            if date_:
                stmt = stmt.where(FidsFlight.sched_date == date_)
            return [r.row() for r in s.scalars(stmt)]

    @app.get("/api/v1/public/flights/{flight_no}", tags=["public"])
    def find(flight_no: str, date_: date | None = Query(None, alias="date")):
        with session_scope(factory) as s:
            stmt = select(FidsFlight).where(FidsFlight.flight_no == flight_no.upper().replace(" ", ""), FidsFlight.flight.is_not(None))
            if date_:
                stmt = stmt.where(FidsFlight.sched_date == date_)
            return [r.row() for r in s.scalars(stmt.order_by(FidsFlight.scheduled_at))]

    @app.websocket("/ws/fids")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            while True:
                await websocket.receive_text()  # клієнт може надсилати ping
        except WebSocketDisconnect:
            hub.clients.discard(websocket)

    return app
