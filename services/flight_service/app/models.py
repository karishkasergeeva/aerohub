"""Модель даних Flight Service (AODB).

FLIGHT - єдиний запис про рейс; FLIGHT_EVENT - незмінний журнал змін (розділ 10.3 звіту).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aerohub_common.db import add_infra_tables


class Base(DeclarativeBase):
    metadata = MetaData()


OUTBOX, PROCESSED = add_infra_tables(Base.metadata)


class Flight(Base):
    __tablename__ = "flight"
    __table_args__ = (UniqueConstraint("airline", "number", "direction", "sched_date", name="uq_flight_natural_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    airline: Mapped[str] = mapped_column(String(3), nullable=False)
    number: Mapped[str] = mapped_column(String(5), nullable=False)
    direction: Mapped[str] = mapped_column(String(1), nullable=False)  # A - прибуття, D - відправлення
    sched_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    station: Mapped[str] = mapped_column(String(3), nullable=False)  # аеропорт призначення / відправлення
    aircraft_type: Mapped[str] = mapped_column(String(4), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    estimated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="SCHEDULED")
    terminal: Mapped[str | None] = mapped_column(String(3))
    schengen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    linked_flight_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("flight.id"))
    delay_code: Mapped[str | None] = mapped_column(String(4))
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="MANUAL")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        def iso(v):
            return v.isoformat() if v else None

        return {
            "id": str(self.id),
            "airline": self.airline,
            "number": self.number,
            "flight_no": f"{self.airline}{self.number}",
            "direction": self.direction,
            "sched_date": self.sched_date.isoformat(),
            "station": self.station,
            "aircraft_type": self.aircraft_type,
            "scheduled_at": iso(self.scheduled_at),
            "estimated_at": iso(self.estimated_at),
            "actual_at": iso(self.actual_at),
            "status": self.status,
            "terminal": self.terminal,
            "schengen": self.schengen,
            "linked_flight_id": str(self.linked_flight_id) if self.linked_flight_id else None,
            "delay_code": self.delay_code,
            "source": self.source,
            "version": self.version,
        }


class FlightEvent(Base):
    """Запис журналу змін рейсу: хто, коли, які поля і з яких значень на які."""

    __tablename__ = "flight_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    changes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(String(200))
    changed_by: Mapped[str] = mapped_column(String(60), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "changes": self.changes,
            "reason": self.reason,
            "changed_by": self.changed_by,
            "version": self.version,
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
        }
