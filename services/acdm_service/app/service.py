"""Логіка A-CDM: фіксація етапів, TOBT, розрахунок TSAT, формування DPI.

TSAT розраховує спрощений передвильотний секвенсор: рейси дня впорядковуються за TOBT,
і між сусідніми дозволами на запуск має бути не менше DEPARTURE_INTERVAL хвилин
(пропускна здатність смуги на виліт). TSAT = max(TOBT, попередній TSAT + інтервал).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from aerohub_common.events import Envelope, EventTypes, Topics, utcnow
from aerohub_common.outbox import add_to_outbox, mark_processed

from .models import OUTBOX, PROCESSED, AcdmFlight, DpiMessage, Milestone

SERVICE = "acdm-service"
DEPARTURE_INTERVAL = timedelta(minutes=int(os.getenv("DEPARTURE_INTERVAL_MIN", "3")))
DEP_CODES = {"TOBT", "TSAT", "ASBT", "ARDT", "AOBT", "ATOT"}
ARR_CODES = {"ELDT", "ALDT", "AIBT"}
MANUAL_CODES = {"ALDT", "ATOT", "ASBT", "ARDT"}  # етапи, які може вводити диспетчер вручну


class AcdmError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def _emit(s: Session, etype: str, f: AcdmFlight, extra: dict) -> None:
    add_to_outbox(s, OUTBOX, Topics.ACDM_MILESTONES, Envelope(
        type=etype, aggregate_id=str(f.flight_id), source=SERVICE,
        payload={"flight_id": str(f.flight_id), "flight_no": f.flight_no, **extra}))


def record(s: Session, f: AcdmFlight, code: str, value: datetime, source: str) -> None:
    s.add(Milestone(flight_id=f.flight_id, code=code, value=value, source=source))
    _emit(s, EventTypes.MILESTONE_REACHED, f, {"code": code, "value": value.isoformat(), "source": source})


def dpi(s: Session, f: AcdmFlight, kind: str) -> None:
    """Формує текст DPI у спрощеному форматі (поля за посібником EUROCONTROL A-CDM)."""
    fmt = lambda v: v.strftime("%H%M") if v else "----"  # noqa: E731
    body = f"-TITLE {kind} -ARCID {f.flight_no} -EOBT {fmt(f.estimated_at)} -TOBT {fmt(f.tobt)} -TSAT {fmt(f.tsat)} -STATUS {f.status}"
    s.add(DpiMessage(flight_id=f.flight_id, kind=kind, body=body))


def resequence(s: Session, on_date: date) -> list[AcdmFlight]:
    """Перераховує TSAT для всіх активних відправлень дня. Повертає рейси, у яких TSAT змінився."""
    flights = list(s.scalars(select(AcdmFlight).where(
        AcdmFlight.sched_date == on_date, AcdmFlight.direction == "D", AcdmFlight.tobt.is_not(None),
        AcdmFlight.status.not_in(["DEPARTED", "CANCELLED"])).order_by(AcdmFlight.tobt, AcdmFlight.flight_no)))
    changed, last = [], None
    for f in flights:
        tsat = f.tobt if last is None else max(f.tobt, last + DEPARTURE_INTERVAL)
        if f.tsat != tsat:
            f.tsat = tsat
            changed.append(f)
            _emit(s, EventTypes.TSAT_ISSUED, f, {"tsat": tsat.isoformat(), "tobt": f.tobt.isoformat()})
            dpi(s, f, "T-DPI-s")
        last = tsat
    return changed


def handle_flight_event(s: Session, env: Envelope) -> None:
    if not mark_processed(s, PROCESSED, env.event_id):
        return
    data = env.payload.get("flight")
    fid = uuid.UUID(env.aggregate_id)
    f = s.get(AcdmFlight, fid)
    if env.type == EventTypes.FLIGHT_DELETED:
        if f:
            s.delete(f)
            s.flush()
            resequence(s, f.sched_date)
        return
    est = datetime.fromisoformat(data.get("estimated_at") or data["scheduled_at"])
    new = f is None
    if new:
        f = AcdmFlight(flight_id=fid)
        s.add(f)
    old_status, old_est = (None, None) if new else (f.status, f.estimated_at)
    f.flight_no, f.direction = data["flight_no"], data["direction"]
    f.sched_date = date.fromisoformat(data["sched_date"])
    f.scheduled_at, f.estimated_at, f.status = datetime.fromisoformat(data["scheduled_at"]), est, data["status"]
    s.flush()

    if f.direction == "A":
        if new or old_est != est:
            record(s, f, "ELDT", est, "flight-service")
        if data["status"] == "ARRIVED" and old_status != "ARRIVED":
            record(s, f, "AIBT", datetime.fromisoformat(data.get("actual_at") or utcnow().isoformat()), "flight-service")
        return

    if new:
        f.tobt = est
        record(s, f, "TOBT", est, "initial=EOBT")
        dpi(s, f, "E-DPI")
    elif old_est != est and not f.tobt_confirmed:
        f.tobt = est
        record(s, f, "TOBT", est, "EOBT changed")
        _emit(s, EventTypes.TOBT_CHANGED, f, {"tobt": est.isoformat(), "reason": "EOBT changed"})
        dpi(s, f, "T-DPI-t")
    if data["status"] == "BOARDING" and old_status != "BOARDING":
        record(s, f, "ASBT", utcnow(), "flight-service")
    if data["status"] == "DEPARTED" and old_status != "DEPARTED":
        record(s, f, "AOBT", datetime.fromisoformat(data.get("actual_at") or utcnow().isoformat()), "flight-service")
        dpi(s, f, "A-DPI")
    if data["status"] == "CANCELLED" and old_status != "CANCELLED":
        dpi(s, f, "C-DPI")
    resequence(s, f.sched_date)


def handle_task_event(s: Session, env: Envelope) -> None:
    """Завершення завдання «готовність ПС» фіксує етап ARDT."""
    if env.type != EventTypes.TASK_COMPLETED or env.payload.get("template_code") != "READY":
        return
    if not mark_processed(s, PROCESSED, env.event_id):
        return
    f = s.get(AcdmFlight, uuid.UUID(env.payload["flight_id"]))
    if f:
        record(s, f, "ARDT", datetime.fromisoformat(env.payload["actual_end"]), "task-service")


def set_tobt(s: Session, flight_id: str, tobt: datetime, user: str) -> AcdmFlight:
    """Підтвердження або зміна TOBT відповідальною стороною (авіакомпанія / хендлер)."""
    f = s.get(AcdmFlight, uuid.UUID(flight_id), with_for_update=True)
    if f is None:
        raise AcdmError("Рейс невідомий A-CDM Service", 404)
    if f.direction != "D":
        raise AcdmError("TOBT задається лише для рейсів відправлення", 422)
    if f.status in ("DEPARTED", "CANCELLED"):
        raise AcdmError(f"Рейс у статусі {f.status}: TOBT змінювати не можна")
    if tobt < f.scheduled_at - timedelta(minutes=15):
        raise AcdmError("TOBT не може бути раніше ніж SOBT - 15 хв", 422)
    f.tobt, f.tobt_confirmed = tobt, True
    record(s, f, "TOBT", tobt, f"manual:{user}")
    _emit(s, EventTypes.TOBT_CHANGED, f, {"tobt": tobt.isoformat(), "reason": f"confirmed by {user}"})
    dpi(s, f, "T-DPI-t")
    s.flush()
    resequence(s, f.sched_date)
    return f


def add_milestone(s: Session, flight_id: str, code: str, value: datetime, user: str) -> Milestone:
    f = s.get(AcdmFlight, uuid.UUID(flight_id))
    if f is None:
        raise AcdmError("Рейс невідомий A-CDM Service", 404)
    code = code.upper()
    if code not in MANUAL_CODES or code not in (DEP_CODES if f.direction == "D" else ARR_CODES):
        raise AcdmError(f"Етап {code} не можна ввести вручну для цього рейсу", 422)
    record(s, f, code, value, f"manual:{user}")
    s.flush()
    return s.scalars(select(Milestone).where(Milestone.flight_id == f.flight_id).order_by(Milestone.id.desc())).first()


def flight_view(s: Session, f: AcdmFlight) -> dict:
    """Рейс з останнім значенням кожного етапу."""
    latest: dict[str, dict] = {}
    for m in s.scalars(select(Milestone).where(Milestone.flight_id == f.flight_id).order_by(Milestone.id)):
        latest[m.code] = m.to_dict()
    return {**f.to_dict(), "milestones": latest}
