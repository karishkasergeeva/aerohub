"""Правила та алгоритм розподілу ресурсів аеропорту.

Для кожного рейсу визначаються потрібні ресурси та часові вікна відносно
розрахункового (або планового) часу T:
  відправлення: стоянка [T-60; T+10], гейт [T-40; T], 2 стійки реєстрації [T-150; T-40];
  прибуття:     стоянка [T-10; T+50], стрічка видачі багажу [T+10; T+50].

Алгоритм - жадібний: для кожної потреби спочатку пробує ресурс, який уже був
призначений рейсу (стабільність плану), потім сумісні вільні ресурси за пріоритетом
(контактні стоянки першими). Якщо вільного ресурсу немає, фіксується конфлікт.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta  # noqa: F401

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Allocation, Conflict, FlightCache, Resource, ResourceType, Unavailability

# Категорія ПС за типом (спрощено, за розмахом крила ICAO: B - турбогвинтові, C - вузькофюзеляжні, E - широкофюзеляжні)
AIRCRAFT_CATEGORY = {
    "AT7": "B", "AT5": "B", "DH4": "C", "E75": "C", "E90": "C", "E95": "C", "319": "C", "320": "C", "321": "C",
    "32N": "C", "32Q": "C", "73H": "C", "738": "C", "7M8": "C", "332": "E", "333": "E", "359": "E", "788": "E",
    "789": "E", "77W": "E", "744": "F",
}
CATEGORY_ORDER = "ABCDEF"


def category(aircraft_type: str) -> str:
    return AIRCRAFT_CATEGORY.get(aircraft_type.upper(), "C")


@dataclass
class Need:
    kind: str
    count: int
    start: datetime
    end: datetime


def needs_for(f: FlightCache) -> list[Need]:
    t = f.ref_time
    m = lambda x: timedelta(minutes=x)  # noqa: E731
    if f.direction == "D":
        return [Need(ResourceType.STAND, 1, t - m(60), t + m(10)), Need(ResourceType.GATE, 1, t - m(40), t),
                Need(ResourceType.DESK, 2, t - m(150), t - m(40))]
    return [Need(ResourceType.STAND, 1, t - m(10), t + m(50)), Need(ResourceType.BELT, 1, t + m(10), t + m(50))]


def compatible(r: Resource, f: FlightCache) -> bool:
    if not r.active:
        return False
    if r.type == ResourceType.STAND and CATEGORY_ORDER.index(category(f.aircraft_type)) > CATEGORY_ORDER.index(r.max_category):
        return False
    if r.type == ResourceType.GATE and r.zone != "ANY" and r.zone != ("SCHENGEN" if f.schengen else "NON_SCHENGEN"):
        return False
    return True


def is_free(s: Session, resource_id: int, start: datetime, end: datetime, ignore_flight=None) -> bool:
    busy = select(Allocation.id).where(Allocation.resource_id == resource_id, Allocation.start_at < end, Allocation.end_at > start)
    if ignore_flight is not None:
        busy = busy.where(Allocation.flight_id != ignore_flight)
    closed = select(Unavailability.id).where(Unavailability.resource_id == resource_id,
                                            Unavailability.start_at < end, Unavailability.end_at > start)
    return s.scalar(busy.limit(1)) is None and s.scalar(closed.limit(1)) is None


def blocking_flight(s: Session, resource_id: int, start: datetime, end: datetime, ignore_flight) -> str | None:
    """Повертає номер рейсу, який займає ресурс у вказаному вікні (для повідомлення про конфлікт)."""
    row = s.execute(select(FlightCache.flight_no).join(Allocation, Allocation.flight_id == FlightCache.flight_id).where(
        Allocation.resource_id == resource_id, Allocation.start_at < end, Allocation.end_at > start,
        Allocation.flight_id != ignore_flight).limit(1)).first()
    if row:
        return row[0]
    if s.scalar(select(Unavailability.reason).where(Unavailability.resource_id == resource_id,
                                                    Unavailability.start_at < end, Unavailability.end_at > start).limit(1)):
        return "закриття ресурсу"
    return None


def _try_insert(s: Session, alloc: Allocation) -> bool:
    """Вставляє призначення; обмеження EXCLUDE - остання лінія захисту від перетину."""
    try:
        with s.begin_nested():
            s.add(alloc)
            s.flush()
        return True
    except IntegrityError:
        return False


def turnaround_pair(s: Session, f: FlightCache) -> tuple[FlightCache, FlightCache] | None:
    """Повертає (прибуття, відправлення), якщо рейс пов'язаний з рейсом того самого ПС."""
    if not f.linked_flight_id:
        return None
    other = s.get(FlightCache, f.linked_flight_id)
    if other is None or other.status in ("CANCELLED", "DIVERTED") or {f.direction, other.direction} != {"A", "D"}:
        return None
    arr, dep = (f, other) if f.direction == "A" else (other, f)
    return (arr, dep) if dep.ref_time > arr.ref_time else None


def stand_windows(arr: FlightCache, dep: FlightCache) -> dict:
    """Одна стоянка на весь час обробки ПС: прибуття займає її до середини інтервалу, відправлення - після."""
    split = arr.ref_time + (dep.ref_time - arr.ref_time) / 2
    return {arr.flight_id: (arr.ref_time - timedelta(minutes=10), split),
            dep.flight_id: (split, dep.ref_time + timedelta(minutes=10))}


def _allocate_pair_stand(s: Session, arr: FlightCache, dep: FlightCache) -> tuple[list[Allocation], list[Conflict]]:
    """Призначає одну стоянку рейсам прибуття і відправлення одного ПС (дефект D-12)."""
    windows = stand_windows(arr, dep)
    old = list(s.scalars(select(Allocation).where(Allocation.flight_id.in_(list(windows)), Allocation.kind == ResourceType.STAND)))
    preferred = [a.resource_id for a in old]
    for a in old:
        s.delete(a)
    s.flush()
    start, end = windows[arr.flight_id][0], windows[dep.flight_id][1]
    candidates = list(s.scalars(select(Resource).where(Resource.type == ResourceType.STAND).order_by(Resource.contact.desc(), Resource.code)))
    candidates.sort(key=lambda r: (r.id not in preferred,))
    for r in candidates:
        if not (compatible(r, arr) and compatible(r, dep)) or not is_free(s, r.id, start, end):
            continue
        pair = [Allocation(flight_id=fid, resource_id=r.id, kind=ResourceType.STAND, start_at=w[0], end_at=w[1]) for fid, w in windows.items()]
        if all(_try_insert(s, a) for a in pair):
            return pair, []
    conflicts = []
    for f in (arr, dep):
        c = Conflict(flight_id=f.flight_id, kind=ResourceType.STAND,
                     message=f"Немає вільної стоянки на весь час обробки ПС {arr.flight_no}/{dep.flight_no} ({start:%H:%M}-{end:%H:%M} UTC)")
        s.add(c)
        conflicts.append(c)
    return [], conflicts


def allocate_flight(s: Session, f: FlightCache) -> tuple[list[Allocation], list[Conflict]]:
    """Перераховує призначення рейсу. Ручні призначення зберігаються, якщо ресурс досі вільний.

    Для пов'язаних рейсів (turnaround) стоянка призначається спільно обом рейсам.
    """
    existing = list(s.scalars(select(Allocation).where(Allocation.flight_id == f.flight_id)))
    pair = turnaround_pair(s, f)
    s.execute(delete(Conflict).where(Conflict.flight_id.in_([f.flight_id] + ([pair[0].flight_id, pair[1].flight_id] if pair else [])),
                                     Conflict.resolved.is_(False)))
    result: list[Allocation] = []
    conflicts: list[Conflict] = []
    for need in needs_for(f):
        if need.kind == ResourceType.STAND and pair:
            allocs, confl = _allocate_pair_stand(s, *pair)
            result += [a for a in allocs if a.flight_id == f.flight_id]
            conflicts += confl
            continue
        mine = [a for a in existing if a.kind == need.kind]
        preferred = [a.resource_id for a in mine]
        manual = any(a.manual for a in mine)
        for a in mine:
            s.delete(a)
        s.flush()
        candidates = list(s.scalars(select(Resource).where(Resource.type == need.kind).order_by(
            Resource.contact.desc(), Resource.code)))
        candidates.sort(key=lambda r: (r.id not in preferred,))
        chosen: list[Allocation] = []
        for r in candidates:
            if len(chosen) == need.count:
                break
            if not compatible(r, f) or not is_free(s, r.id, need.start, need.end):
                continue
            alloc = Allocation(flight_id=f.flight_id, resource_id=r.id, kind=need.kind, start_at=need.start,
                               end_at=need.end, manual=manual and r.id in preferred,
                               created_by="manual" if manual and r.id in preferred else "auto")
            if _try_insert(s, alloc):
                chosen.append(alloc)
        result += chosen
        if len(chosen) < need.count:
            c = Conflict(flight_id=f.flight_id, kind=need.kind,
                         message=f"Немає вільного сумісного ресурсу типу {need.kind} для {f.flight_no} "
                                 f"у вікні {need.start:%H:%M}-{need.end:%H:%M} UTC")
            s.add(c)
            conflicts.append(c)
    s.flush()
    return result, conflicts


def release_flight(s: Session, flight_id) -> int:
    n = s.execute(delete(Allocation).where(Allocation.flight_id == flight_id)).rowcount
    s.execute(delete(Conflict).where(Conflict.flight_id == flight_id))
    return n


def seed_resources(s: Session) -> None:
    """Довідник ресурсів умовного аеропорту «Аерополіс» (підрозділ 3.1 звіту)."""
    if s.scalar(select(Resource.id).limit(1)):
        return
    items = []
    for i in range(1, 29):  # 28 стоянок: 1-8 контактні, 21-28 для широкофюзеляжних ПС
        items.append(Resource(code=f"S{i:02d}", type=ResourceType.STAND, contact=i <= 8, max_category="E" if i > 20 else "C"))
    for i in range(1, 17):  # 16 гейтів: 1-10 шенгенська зона, 11-16 нешенгенська
        items.append(Resource(code=f"G{i:02d}", type=ResourceType.GATE, zone="SCHENGEN" if i <= 10 else "NON_SCHENGEN"))
    for i in range(1, 25):
        items.append(Resource(code=f"D{i:02d}", type=ResourceType.DESK))
    for i in range(1, 7):
        items.append(Resource(code=f"B{i}", type=ResourceType.BELT))
    s.add_all(items)
    s.flush()

