"""Бізнес-логіка Resource Service: реакція на події рейсів, ручне призначення, звіти."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from aerohub_common.events import Envelope, EventTypes, Topics
from aerohub_common.outbox import add_to_outbox, mark_processed

from .allocation import allocate_flight, blocking_flight, compatible, needs_for, release_flight, stand_windows, turnaround_pair
from .models import OUTBOX, PROCESSED, Allocation, Conflict, FlightCache, Resource, Unavailability

SERVICE = "resource-service"
RELEASE_STATUSES = {"CANCELLED", "DIVERTED"}


class ResourceError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def _publish_state(s: Session, f: FlightCache, conflicts: list[Conflict] | None = None) -> None:
    """Публікує поточний набір призначень рейсу (FIDS і дашборд беруть його цілком)."""
    allocs = list(s.scalars(select(Allocation).where(Allocation.flight_id == f.flight_id).order_by(Allocation.kind)))
    payload = {"flight_id": str(f.flight_id), "flight_no": f.flight_no, "allocations": [a.to_dict() for a in allocs]}
    etype = EventTypes.RESOURCE_ALLOCATED if allocs else EventTypes.RESOURCE_RELEASED
    add_to_outbox(s, OUTBOX, Topics.RESOURCE_ALLOCATIONS, Envelope(type=etype, aggregate_id=str(f.flight_id), payload=payload, source=SERVICE))
    for c in conflicts or []:
        add_to_outbox(s, OUTBOX, Topics.RESOURCE_ALLOCATIONS, Envelope(
            type=EventTypes.RESOURCE_CONFLICT_DETECTED, aggregate_id=str(f.flight_id),
            payload={"flight_id": str(f.flight_id), "flight_no": f.flight_no, "kind": c.kind, "message": c.message},
            source=SERVICE))


def handle_flight_event(s: Session, env: Envelope) -> None:
    """Обробник подій flight.events (ідемпотентний)."""
    if not mark_processed(s, PROCESSED, env.event_id):
        return
    data = env.payload.get("flight", {})
    fid = uuid.UUID(env.aggregate_id)
    if env.type == EventTypes.FLIGHT_DELETED:
        release_flight(s, fid)
        cached = s.get(FlightCache, fid)
        if cached:
            _publish_state(s, cached)
            s.delete(cached)
        return
    ref = data.get("estimated_at") or data.get("scheduled_at")
    cached = s.get(FlightCache, fid) or FlightCache(flight_id=fid)
    old_ref = cached.ref_time if cached.ref_time else None
    old_type = cached.aircraft_type
    old_link = cached.linked_flight_id
    cached.flight_no = data["flight_no"]
    cached.direction = data["direction"]
    cached.sched_date = date.fromisoformat(data["sched_date"])
    cached.ref_time = datetime.fromisoformat(ref)
    cached.aircraft_type = data["aircraft_type"]
    cached.schengen = data["schengen"]
    cached.status = data["status"]
    cached.linked_flight_id = uuid.UUID(data["linked_flight_id"]) if data.get("linked_flight_id") else None
    s.add(cached)
    s.flush()
    if cached.status in RELEASE_STATUSES:
        release_flight(s, fid)
        _publish_state(s, cached)
        return
    if (env.type == EventTypes.FLIGHT_CREATED or old_ref != cached.ref_time or old_type != cached.aircraft_type
            or old_link != cached.linked_flight_id):
        _, conflicts = allocate_flight(s, cached)
        _publish_state(s, cached, conflicts)
        partner = s.get(FlightCache, cached.linked_flight_id) if cached.linked_flight_id else None
        if partner:
            _publish_state(s, partner)  # спільна стоянка змінює і призначення пари


def assign_manual(s: Session, flight_id: str, kind: str, codes: list[str], user: str) -> list[Allocation]:
    """Ручне призначення ресурсу рейсу планувальником (сценарій ручного розподілу, FR-11)."""
    f = s.get(FlightCache, uuid.UUID(flight_id))
    if f is None:
        raise ResourceError("Рейс невідомий Resource Service", 404)
    need = next((n for n in needs_for(f) if n.kind == kind), None)
    if need is None:
        raise ResourceError(f"Рейсу {f.flight_no} не потрібен ресурс типу {kind}", 422)
    if len(codes) != need.count:
        raise ResourceError(f"Для {kind} потрібно вказати {need.count} ресурс(и)", 422)
    pair = turnaround_pair(s, f) if kind == "STAND" else None
    if pair:
        return _assign_pair_stand(s, pair, codes[0], user)
    resources = []
    for code in codes:
        r = s.scalar(select(Resource).where(Resource.code == code.upper(), Resource.type == kind))
        if r is None:
            raise ResourceError(f"Ресурс {code} типу {kind} не знайдено", 404)
        if not compatible(r, f):
            raise ResourceError(f"Ресурс {r.code} несумісний з рейсом {f.flight_no} (тип ПС або зона)", 422)
        who = blocking_flight(s, r.id, need.start, need.end, f.flight_id)
        if who:
            raise ResourceError(f"Конфлікт: {r.code} у вікні {need.start:%H:%M}-{need.end:%H:%M} UTC зайнятий ({who})", 409)
        resources.append(r)
    for a in s.scalars(select(Allocation).where(Allocation.flight_id == f.flight_id, Allocation.kind == kind)):
        s.delete(a)
    s.flush()
    result = []
    for r in resources:
        a = Allocation(flight_id=f.flight_id, resource_id=r.id, kind=kind, start_at=need.start, end_at=need.end,
                       manual=True, created_by=user)
        s.add(a)
        result.append(a)
    for c in s.scalars(select(Conflict).where(Conflict.flight_id == f.flight_id, Conflict.kind == kind, Conflict.resolved.is_(False))):
        c.resolved = True
    s.flush()
    _publish_state(s, f)
    return result


def _assign_pair_stand(s: Session, pair, code: str, user: str) -> list[Allocation]:
    """Ручне призначення стоянки парі рейсів одного ПС."""
    arr, dep = pair
    windows = stand_windows(arr, dep)
    start, end = windows[arr.flight_id][0], windows[dep.flight_id][1]
    r = s.scalar(select(Resource).where(Resource.code == code.upper(), Resource.type == "STAND"))
    if r is None:
        raise ResourceError(f"Стоянку {code} не знайдено", 404)
    if not (compatible(r, arr) and compatible(r, dep)):
        raise ResourceError(f"Стоянка {r.code} несумісна з типом ПС", 422)
    for a in s.scalars(select(Allocation).where(Allocation.flight_id.in_(list(windows)), Allocation.kind == "STAND")):
        s.delete(a)
    s.flush()
    who = blocking_flight(s, r.id, start, end, arr.flight_id)
    if who and who != dep.flight_no:
        raise ResourceError(f"Конфлікт: {r.code} у вікні {start:%H:%M}-{end:%H:%M} UTC зайнята ({who})", 409)
    result = []
    for fid, (a_start, a_end) in windows.items():
        a = Allocation(flight_id=fid, resource_id=r.id, kind="STAND", start_at=a_start, end_at=a_end, manual=True, created_by=user)
        s.add(a)
        result.append(a)
    for c in s.scalars(select(Conflict).where(Conflict.flight_id.in_(list(windows)), Conflict.kind == "STAND", Conflict.resolved.is_(False))):
        c.resolved = True
    s.flush()
    _publish_state(s, arr)
    _publish_state(s, dep)
    return result


def add_unavailability(s: Session, resource_id: int, start: datetime, end: datetime, reason: str) -> list[str]:
    """Закриває ресурс на період і перерозподіляє рейси, яких це зачіпає. Повертає їх номери."""
    r = s.get(Resource, resource_id)
    if r is None:
        raise ResourceError("Ресурс не знайдено", 404)
    if end <= start:
        raise ResourceError("Кінець періоду має бути пізніше за початок", 422)
    affected = list(s.scalars(select(Allocation).where(Allocation.resource_id == r.id, Allocation.start_at < end, Allocation.end_at > start)))
    flights = {a.flight_id for a in affected}
    for a in affected:
        s.delete(a)
    s.add(Unavailability(resource_id=r.id, start_at=start, end_at=end, reason=reason))
    s.flush()
    names = []
    for fid in flights:
        f = s.get(FlightCache, fid)
        _, conflicts = allocate_flight(s, f)
        _publish_state(s, f, conflicts)
        names.append(f.flight_no)
    return names


def reallocate_date(s: Session, on_date: date) -> int:
    flights = list(s.scalars(select(FlightCache).where(FlightCache.sched_date == on_date,
                                                       FlightCache.status.not_in(RELEASE_STATUSES)).order_by(FlightCache.ref_time)))
    for f in flights:
        _, conflicts = allocate_flight(s, f)
        _publish_state(s, f, conflicts)
    return len(flights)


def allocations_for(s: Session, on_date: date | None) -> list[dict]:
    stmt = select(Allocation, FlightCache.flight_no).join(FlightCache, FlightCache.flight_id == Allocation.flight_id)
    if on_date:
        day = datetime(on_date.year, on_date.month, on_date.day, tzinfo=timezone.utc)
        stmt = stmt.where(Allocation.start_at < day + timedelta(days=1), Allocation.end_at > day)
    return [{**a.to_dict(), "flight_no": no} for a, no in s.execute(stmt.order_by(Allocation.start_at))]


def conflicts_for(s: Session, on_date: date | None) -> list[dict]:
    stmt = select(Conflict, FlightCache.flight_no).join(FlightCache, FlightCache.flight_id == Conflict.flight_id).where(Conflict.resolved.is_(False))
    if on_date:
        stmt = stmt.where(FlightCache.sched_date == on_date)
    return [{**c.to_dict(), "flight_no": no} for c, no in s.execute(stmt)]
