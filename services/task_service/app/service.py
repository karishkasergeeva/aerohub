"""Логіка наземного обслуговування: генерація завдань за шаблонами, зсув за TOBT, виконання, SLA."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from aerohub_common.events import Envelope, EventTypes, Topics, utcnow
from aerohub_common.outbox import add_to_outbox, mark_processed

from .models import OUTBOX, PROCESSED, GroundTask, TaskStatus, TaskTemplate

SERVICE = "task-service"
OVERDUE_GRACE = timedelta(minutes=5)

DEFAULT_TEMPLATES = [
    # code, назва, напрямок, зсув, тривалість, бригада
    ("CLEAN", "Прибирання салону", "D", -55, 15, "cleaning"),
    ("CATER", "Кейтеринг", "D", -50, 20, "catering"),
    ("FUEL", "Заправлення паливом", "D", -45, 20, "fuel"),
    ("BAGLOAD", "Завантаження багажу", "D", -35, 25, "ramp"),
    ("READY", "Підтвердження готовності ПС", "D", -5, 5, "ramp"),
    ("PUSH", "Буксирування / відхід зі стоянки", "D", 0, 10, "ramp"),
    ("CHOCKS", "Зустріч ПС, колодки, трап", "A", 0, 5, "ramp"),
    ("BAGUNLOAD", "Вивантаження багажу", "A", 5, 20, "ramp"),
    ("BUS", "Доставка пасажирів до терміналу", "A", 5, 15, "bus"),
]


class TaskError(Exception):
    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def seed_templates(s: Session) -> None:
    if s.scalar(select(TaskTemplate.code).limit(1)):
        return
    for i, (code, name, d, off, dur, team) in enumerate(DEFAULT_TEMPLATES):
        s.add(TaskTemplate(code=code, name=name, direction=d, offset_min=off, duration_min=dur, team=team, sort=i))
    s.flush()


def _emit(s: Session, etype: str, t: GroundTask) -> None:
    add_to_outbox(s, OUTBOX, Topics.TASK_EVENTS, Envelope(type=etype, aggregate_id=str(t.flight_id), payload=t.to_dict(), source=SERVICE))


def _plan(s: Session, flight_id: uuid.UUID, flight_no: str, direction: str, sched_date: date, ref: datetime) -> None:
    """Створює відсутні завдання і зсуває ще не розпочаті під новий опорний час."""
    existing = {t.template_code: t for t in s.scalars(select(GroundTask).where(GroundTask.flight_id == flight_id))}
    for tpl in s.scalars(select(TaskTemplate).where(TaskTemplate.direction == direction).order_by(TaskTemplate.sort)):
        start = ref + timedelta(minutes=tpl.offset_min)
        end = start + timedelta(minutes=tpl.duration_min)
        t = existing.get(tpl.code)
        if t is None:
            t = GroundTask(id=uuid.uuid4(), flight_id=flight_id, flight_no=flight_no, sched_date=sched_date,
                           template_code=tpl.code, name=tpl.name, team=tpl.team, planned_start=start, planned_end=end)
            s.add(t)
            s.flush()
            _emit(s, EventTypes.TASK_CREATED, t)
        elif t.status == TaskStatus.PLANNED and (t.planned_start != start or t.planned_end != end):
            t.planned_start, t.planned_end, t.overdue = start, end, False
            t.version += 1
            _emit(s, EventTypes.TASK_UPDATED, t)


def handle_flight_event(s: Session, env: Envelope) -> None:
    if not mark_processed(s, PROCESSED, env.event_id):
        return
    f = env.payload.get("flight") or {}
    fid = uuid.UUID(env.aggregate_id)
    if env.type == EventTypes.FLIGHT_DELETED or f.get("status") in ("CANCELLED", "DIVERTED"):
        for t in s.scalars(select(GroundTask).where(GroundTask.flight_id == fid, GroundTask.status.in_([TaskStatus.PLANNED, TaskStatus.IN_PROGRESS]))):
            t.status = TaskStatus.CANCELLED
            t.version += 1
            _emit(s, EventTypes.TASK_UPDATED, t)
        return
    if f["direction"] == "D" and f.get("status") == "DEPARTED":
        close_on_departure(s, fid, datetime.fromisoformat(f.get("actual_at") or utcnow().isoformat()))
        return
    # для прибуття опорним є фактичний час, коли він відомий (дефект D-14)
    ref = datetime.fromisoformat((f.get("actual_at") if f["direction"] == "A" else None) or f.get("estimated_at") or f["scheduled_at"])
    _plan(s, fid, f["flight_no"], f["direction"], date.fromisoformat(f["sched_date"]), ref)


def close_on_departure(s: Session, flight_id: uuid.UUID, aobt: datetime) -> int:
    """Після відходу ПС незакриті завдання закриваються автоматично (дефект D-14).

    Ознака overdue зберігається, тому порушення SLA залишаються видимими у звітах.
    """
    n = 0
    for t in s.scalars(select(GroundTask).where(GroundTask.flight_id == flight_id,
                                                GroundTask.status.in_([TaskStatus.PLANNED, TaskStatus.IN_PROGRESS]))):
        t.actual_start = t.actual_start or min(t.planned_start, aobt)
        t.actual_end = max(t.actual_start, min(t.planned_end, aobt))
        t.status, t.assignee = TaskStatus.DONE, t.assignee or "авто (AOBT)"
        t.version += 1
        _emit(s, EventTypes.TASK_COMPLETED, t)
        n += 1
    return n


def handle_acdm_event(s: Session, env: Envelope) -> None:
    """Підтверджений TOBT стає опорним часом для завдань відправлення."""
    if env.type != EventTypes.TOBT_CHANGED or not mark_processed(s, PROCESSED, env.event_id):
        return
    fid = uuid.UUID(env.payload["flight_id"])
    first = s.scalars(select(GroundTask).where(GroundTask.flight_id == fid).limit(1)).first()
    if first is None:
        return
    _plan(s, fid, first.flight_no, "D", first.sched_date, datetime.fromisoformat(env.payload["tobt"]))


def act(s: Session, task_id: str, action: str, version: int, user: str, at: datetime | None) -> GroundTask:
    """Початок або завершення завдання. `at` дозволяє синхронізувати дії, виконані офлайн."""
    t = s.get(GroundTask, uuid.UUID(task_id), with_for_update=True)
    if t is None:
        raise TaskError("Завдання не знайдено", 404)
    if version != t.version:
        raise TaskError(f"Завдання вже змінено (ваша версія {version}, поточна {t.version})")
    when = at or utcnow()
    if action == "start":
        if t.status != TaskStatus.PLANNED:
            raise TaskError(f"Розпочати можна лише заплановане завдання (зараз {t.status})")
        t.status, t.actual_start, t.assignee = TaskStatus.IN_PROGRESS, when, user
        etype = EventTypes.TASK_UPDATED
    elif action == "complete":
        if t.status not in (TaskStatus.PLANNED, TaskStatus.IN_PROGRESS):
            raise TaskError(f"Завершити можна лише активне завдання (зараз {t.status})")
        if t.actual_start and when < t.actual_start:
            raise TaskError("Час завершення раніше за час початку", 422)
        t.actual_start = t.actual_start or when
        t.status, t.actual_end, t.assignee = TaskStatus.DONE, when, t.assignee or user
        etype = EventTypes.TASK_COMPLETED
    else:
        raise TaskError("Невідома дія", 422)
    t.version += 1
    _emit(s, etype, t)
    return t


def mark_overdue(s: Session, now: datetime | None = None) -> list[GroundTask]:
    """Позначає прострочені завдання (кінець плану + 5 хв минув, а завдання не виконано)."""
    now = now or utcnow()
    late = list(s.scalars(select(GroundTask).where(
        GroundTask.status.in_([TaskStatus.PLANNED, TaskStatus.IN_PROGRESS]), GroundTask.overdue.is_(False),
        GroundTask.planned_end < now - OVERDUE_GRACE)))
    for t in late:
        t.overdue = True
        t.version += 1
        _emit(s, EventTypes.TASK_OVERDUE, t)
    return late
