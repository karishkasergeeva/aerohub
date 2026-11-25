"""Модель даних Task Service: шаблони і завдання наземного обслуговування рейсу."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Integer, MetaData, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aerohub_common.db import add_infra_tables


class Base(DeclarativeBase):
    metadata = MetaData()


OUTBOX, PROCESSED = add_infra_tables(Base.metadata)


class TaskStatus:
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskTemplate(Base):
    """Шаблон операції обслуговування. offset - зсув початку відносно опорного часу рейсу, хв."""

    __tablename__ = "task_template"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(1), nullable=False)
    offset_min: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False)
    team: Mapped[str] = mapped_column(String(20), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class GroundTask(Base):
    __tablename__ = "ground_task"
    __table_args__ = (UniqueConstraint("flight_id", "template_code", name="uq_task_per_flight"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    flight_no: Mapped[str] = mapped_column(String(8), nullable=False)
    sched_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    template_code: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    team: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=TaskStatus.PLANNED)
    assignee: Mapped[str | None] = mapped_column(String(60))
    overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        iso = lambda v: v.isoformat() if v else None  # noqa: E731
        return {"id": str(self.id), "flight_id": str(self.flight_id), "flight_no": self.flight_no,
                "sched_date": self.sched_date.isoformat(), "template_code": self.template_code, "name": self.name,
                "team": self.team, "planned_start": iso(self.planned_start), "planned_end": iso(self.planned_end),
                "actual_start": iso(self.actual_start), "actual_end": iso(self.actual_end), "status": self.status,
                "assignee": self.assignee, "overdue": self.overdue, "version": self.version}
