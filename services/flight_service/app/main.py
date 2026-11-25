"""Flight Service (AODB): REST API рейсів, імпорт розкладу, видача демо-токенів."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from aerohub_common.auth import DEMO_USERS, Principal, Roles, current_user, issue_token, require_roles
from aerohub_common.bus import build_bus
from aerohub_common.config import get_settings
from aerohub_common.db import ensure_database, make_engine, make_session_factory, session_scope
from aerohub_common.outbox import OutboxRelay

from . import service
from .domain import DomainError
from .models import OUTBOX, Base
from .schemas import CancelRequest, FlightCreate, FlightPatch, ImportResult, LinkRequest, StatusChange, TokenRequest
from .ssim import parse_ssim

WRITE_ROLES = (Roles.PLANNER, Roles.AOC)


def create_app(bus=None, database_url: str | None = None) -> FastAPI:
    settings = get_settings()
    engine = make_engine(database_url or settings.database_url)
    factory = make_session_factory(engine)
    bus = bus or build_bus(settings.event_bus, settings.kafka_bootstrap, "flight-service")
    relay = OutboxRelay(factory, OUTBOX, bus, settings.outbox_poll_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ensure_database(database_url or settings.database_url)
        Base.metadata.create_all(engine)
        await bus.start()
        relay.start()
        yield
        await relay.stop()
        await bus.stop()

    app = FastAPI(title="AeroHub Flight Service (AODB)", version="0.1.0", lifespan=lifespan,
                  description="Єдиний запис рейсу, статуси, історія змін, імпорт розкладу SSIM")
    app.state.factory, app.state.relay, app.state.engine = factory, relay, engine

    @app.exception_handler(IntegrityError)
    async def integrity_error(_: Request, exc: IntegrityError):
        return JSONResponse(status_code=409, content={"detail": "Конфлікт даних: запис уже існує або порушено обмеження"})

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.code, content={"detail": str(exc)})

    def tx():
        """Транзакція на запит: фіксується до надсилання відповіді."""
        return session_scope(factory)

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "flight-service"}

    @app.post("/api/v1/auth/token", tags=["auth"], summary="Демо-автентифікація (замінюється Keycloak)")
    def token(req: TokenRequest):
        user = DEMO_USERS.get(req.username)
        if not user or user[0] != req.password:
            raise HTTPException(401, "Неправильний логін або пароль")
        return {"access_token": issue_token(req.username, user[1]), "token_type": "bearer", "roles": user[1]}

    @app.get("/api/v1/auth/me", tags=["auth"])
    def me(user: Principal = Depends(current_user)):
        return user

    @app.get("/api/v1/flights", tags=["flights"])
    def list_flights(date_: date | None = Query(None, alias="date"), direction: str | None = None,
                     status: str | None = None, q: str | None = None, _=Depends(current_user)):
        with tx() as s:
            return [f.to_dict() for f in service.list_flights(s, date_, direction, status, q)]

    @app.post("/api/v1/flights", status_code=201, tags=["flights"])
    def create(data: FlightCreate, user: Principal = Depends(require_roles(*WRITE_ROLES))):
        with tx() as s:
            return service.create_flight(s, data, user.username).to_dict()

    @app.get("/api/v1/flights/{flight_id}", tags=["flights"])
    def get(flight_id: str, _=Depends(current_user)):
        with tx() as s:
            return service.get_flight(s, flight_id).to_dict()

    @app.patch("/api/v1/flights/{flight_id}", tags=["flights"])
    def patch(flight_id: str, data: FlightPatch, if_match: str | None = Header(None),
              user: Principal = Depends(require_roles(*WRITE_ROLES))):
        if data.version is None and if_match:
            data.version = int(if_match.strip('"'))
        with tx() as s:
            return service.update_flight(s, flight_id, data, user.username).to_dict()

    @app.post("/api/v1/flights/{flight_id}/status", tags=["flights"])
    def status(flight_id: str, data: StatusChange,
               user: Principal = Depends(require_roles(Roles.AOC, Roles.PLANNER, Roles.AGENT, Roles.HANDLER))):
        with tx() as s:
            return service.change_status(s, flight_id, data, user.username).to_dict()

    @app.post("/api/v1/flights/{flight_id}/cancel", tags=["flights"])
    def cancel(flight_id: str, data: CancelRequest, user: Principal = Depends(require_roles(*WRITE_ROLES))):
        with tx() as s:
            return service.change_status(s, flight_id, StatusChange(status="CANCELLED", reason=data.reason), user.username).to_dict()

    @app.delete("/api/v1/flights/{flight_id}", status_code=204, tags=["flights"])
    def delete(flight_id: str, user: Principal = Depends(require_roles(Roles.PLANNER))):
        with tx() as s:
            service.delete_flight(s, flight_id, user.username)

    @app.post("/api/v1/flights/{flight_id}/link", tags=["flights"])
    def link(flight_id: str, data: LinkRequest, user: Principal = Depends(require_roles(*WRITE_ROLES))):
        with tx() as s:
            return service.link_flights(s, flight_id, data.other_flight_id, user.username).to_dict()

    @app.get("/api/v1/flights/{flight_id}/history", tags=["flights"])
    def history(flight_id: str, _=Depends(current_user)):
        with tx() as s:
            return [e.to_dict() for e in service.history(s, flight_id)]

    @app.post("/api/v1/schedules/imports", response_model=ImportResult, tags=["schedule"])
    async def import_ssim(file: UploadFile = File(...), user: Principal = Depends(require_roles(Roles.PLANNER))):
        text = (await file.read()).decode("utf-8", errors="replace")
        planned, errors = parse_ssim(text, get_settings().home_airport)
        with session_scope(factory) as s:
            created, updated, skipped = service.import_planned(s, planned, user.username)
        return ImportResult(created=created, updated=updated, skipped=skipped,
                            errors=[{"line": e.line, "message": e.message} for e in errors])

    return app

