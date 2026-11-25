"""Бізнес-логіка Flight Service.

Кожна операція змінює рейс, записує зміни в журнал FLIGHT_EVENT і додає доменну
подію в outbox - усе в одній транзакції.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aerohub_common.events import Envelope, EventTypes, Topics, utcnow
from aerohub_common.outbox import add_to_outbox

from .domain import CRITICAL_FIELDS, EDITABLE_FIELDS, FINAL_STATUSES, DomainError, Status, check_transition
from .models import OUTBOX, Flight, FlightEvent
from .schemas import FlightCreate, FlightPatch, StatusChange
from .ssim import PlannedFlight

SERVICE = "flight-service"

# Аеропорти поза Шенгенською зоною (спрощений довідник для імпорту розкладу)
NON_SCHENGEN = {"IST", "SAW", "LHR", "LGW", "DUB", "DXB", "TLV", "KBP", "LWO", "ODS", "CAI", "DOH", "JFK", "TBS", "EVN", "GYD"}
DEFAULT_TERMINAL = "D"


def _val(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def _record(session: Session, flight: Flight, event_type: str, changes: list[dict], user: str, reason: str | None) -> None:
    """Записує зміну в журнал і публікує подію через outbox."""
    session.add(FlightEvent(flight_id=flight.id, event_type=event_type, changes=changes,
                            reason=reason, changed_by=user, version=flight.version))
    session.flush()
    payload = {"flight": flight.to_dict(), "changes": changes, "reason": reason, "changed_by": user}
    add_to_outbox(session, OUTBOX, Topics.FLIGHT_EVENTS,
                  Envelope(type=event_type, aggregate_id=str(flight.id), payload=payload, source=SERVICE))


def get_flight(session: Session, flight_id: str | uuid.UUID, lock: bool = False) -> Flight:
    try:
        fid = uuid.UUID(str(flight_id))
    except ValueError as exc:
        raise DomainError("Некоректний ідентифікатор рейсу", 404) from exc
    stmt = select(Flight).where(Flight.id == fid)
    if lock:
        stmt = stmt.with_for_update()
    flight = session.scalar(stmt)
    if flight is None:
        raise DomainError("Рейс не знайдено", 404)
    return flight


def list_flights(session: Session, on_date: date | None, direction: str | None, status: str | None, q: str | None) -> list[Flight]:
    stmt = select(Flight).order_by(Flight.scheduled_at)
    if on_date:
        stmt = stmt.where(Flight.sched_date == on_date)
    if direction:
        stmt = stmt.where(Flight.direction == direction)
    if status:
        stmt = stmt.where(Flight.status == status)
    flights = list(session.scalars(stmt))
    if q:
        ql = q.upper().replace(" ", "")
        flights = [f for f in flights if ql in f"{f.airline}{f.number}" or ql in f.station]
    return flights


def create_flight(session: Session, data: FlightCreate, user: str, source: str = "MANUAL") -> Flight:
    sched_date = data.scheduled_at.date()
    exists = session.scalar(select(Flight).where(
        Flight.airline == data.airline, Flight.number == data.number,
        Flight.direction == data.direction, Flight.sched_date == sched_date))
    if exists:
        raise DomainError(f"Рейс {data.airline}{data.number} ({data.direction}) на {sched_date} уже існує", 409)
    flight = Flight(id=uuid.uuid4(), sched_date=sched_date, source=source, status=Status.SCHEDULED, version=1,
                    **data.model_dump())
    session.add(flight)
    session.flush()
    changes = [{"field": k, "old": None, "new": _val(v)} for k, v in data.model_dump().items()]
    _record(session, flight, EventTypes.FLIGHT_CREATED, changes, user, None)
    return flight


def _check_version(flight: Flight, version: int | None) -> None:
    if version is None:
        raise DomainError("Потрібно передати версію запису (поле version або заголовок If-Match)", 428)
    if version != flight.version:
        raise DomainError(
            f"Рейс уже змінено іншим користувачем (ваша версія {version}, поточна {flight.version}). Оновіть дані.", 409)


def update_flight(session: Session, flight_id: str, patch: FlightPatch, user: str) -> Flight:
    flight = get_flight(session, flight_id, lock=True)
    _check_version(flight, patch.version)
    if flight.status in FINAL_STATUSES:
        raise DomainError(f"Рейс у статусі {flight.status} не можна редагувати")
    changes = []
    for field, new in patch.model_dump(exclude_unset=True).items():
        if field not in EDITABLE_FIELDS:
            continue
        if isinstance(new, str) and field == "station":
            new = new.upper()
        old = getattr(flight, field)
        if _val(old) != _val(new):
            changes.append({"field": field, "old": _val(old), "new": _val(new)})
            setattr(flight, field, new)
    if not changes:
        return flight
    if any(c["field"] in CRITICAL_FIELDS for c in changes) and not patch.reason:
        raise DomainError("Для зміни часу або типу ПС потрібно вказати причину (reason)", 422)
    if any(c["field"] == "scheduled_at" for c in changes):
        flight.sched_date = flight.scheduled_at.date()
    flight.version += 1
    _record(session, flight, EventTypes.FLIGHT_UPDATED, changes, user, patch.reason)
    return flight


def change_status(session: Session, flight_id: str, req: StatusChange, user: str) -> Flight:
    flight = get_flight(session, flight_id, lock=True)
    if req.version is not None:
        _check_version(flight, req.version)
    new = req.status.upper()
    check_transition(flight.direction, flight.status, new)
    if new in (Status.DELAYED, Status.CANCELLED) and not req.reason:
        raise DomainError("Для статусу DELAYED або CANCELLED потрібно вказати причину", 422)
    changes = [{"field": "status", "old": flight.status, "new": new}]
    flight.status = new
    if new in (Status.DEPARTED, Status.ARRIVED):
        at = req.at or utcnow()
        changes.append({"field": "actual_at", "old": _val(flight.actual_at), "new": _val(at)})
        flight.actual_at = at
    if new == Status.DELAYED and req.at:
        changes.append({"field": "estimated_at", "old": _val(flight.estimated_at), "new": _val(req.at)})
        flight.estimated_at = req.at
    flight.version += 1
    event = EventTypes.FLIGHT_CANCELLED if new == Status.CANCELLED else EventTypes.FLIGHT_STATUS_CHANGED
    _record(session, flight, event, changes, user, req.reason)
    return flight


def delete_flight(session: Session, flight_id: str, user: str) -> None:
    """Фізичне видалення дозволене лише для помилково створених рейсів у статусі SCHEDULED."""
    flight = get_flight(session, flight_id, lock=True)
    if flight.status != Status.SCHEDULED:
        raise DomainError("Видалити можна лише рейс у статусі SCHEDULED; для інших використовуйте скасування")
    linked = session.scalar(select(Flight).where(Flight.linked_flight_id == flight.id))
    if linked:
        linked.linked_flight_id = None
    _record(session, flight, EventTypes.FLIGHT_DELETED, [{"field": "*", "old": "exists", "new": None}], user, "Видалено помилково створений рейс")
    session.delete(flight)


def link_flights(session: Session, flight_id: str, other_id: str, user: str) -> Flight:
    """Пов'язує рейс прибуття з рейсом відправлення того самого ПС (turnaround)."""
    a, b = get_flight(session, flight_id, lock=True), get_flight(session, other_id, lock=True)
    if {a.direction, b.direction} != {"A", "D"}:
        raise DomainError("Пов'язати можна лише рейс прибуття з рейсом відправлення", 422)
    arr, dep = (a, b) if a.direction == "A" else (b, a)
    if dep.scheduled_at <= arr.scheduled_at:
        raise DomainError("Рейс відправлення має бути пізніше за рейс прибуття", 422)
    for f, other in ((arr, dep), (dep, arr)):
        old = _val(f.linked_flight_id)
        f.linked_flight_id = other.id
        f.version += 1
        _record(session, f, EventTypes.FLIGHT_UPDATED, [{"field": "linked_flight_id", "old": old, "new": str(other.id)}], user, None)
    return a


def history(session: Session, flight_id: str) -> list[FlightEvent]:
    get_flight(session, flight_id)
    return list(session.scalars(select(FlightEvent).where(FlightEvent.flight_id == uuid.UUID(str(flight_id))).order_by(FlightEvent.id)))


def import_planned(session: Session, planned: list[PlannedFlight], user: str) -> tuple[int, int, int]:
    """Створює або оновлює рейси з розкладу SSIM. Повертає (створено, оновлено, без змін)."""
    created = updated = skipped = 0
    for p in planned:
        existing = session.scalar(select(Flight).where(
            Flight.airline == p.airline, Flight.number == p.number,
            Flight.direction == p.direction, Flight.sched_date == p.scheduled_at.date()))
        if existing is None:
            create_flight(session, FlightCreate(airline=p.airline, number=p.number, direction=p.direction,
                                                station=p.station, aircraft_type=p.aircraft_type,
                                                scheduled_at=p.scheduled_at, terminal=DEFAULT_TERMINAL,
                                                schengen=p.station not in NON_SCHENGEN), user, source="SSIM")
            created += 1
        elif existing.status == Status.SCHEDULED and (
                existing.scheduled_at != p.scheduled_at or existing.aircraft_type != p.aircraft_type or existing.station != p.station):
            update_flight(session, str(existing.id), FlightPatch(
                version=existing.version, reason="Оновлення сезонного розкладу (SSIM)",
                scheduled_at=p.scheduled_at, aircraft_type=p.aircraft_type, station=p.station), user)
            updated += 1
        else:
            skipped += 1
    return created, updated, skipped
