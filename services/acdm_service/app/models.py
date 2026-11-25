"""Модель даних A-CDM Service: стан рейсу для спільного прийняття рішень, етапи, DPI-повідомлення."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, MetaData, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aerohub_common.db import add_infra_tables


class Base(DeclarativeBase):
    metadata = MetaData()


OUTBOX, PROCESSED = add_infra_tables(Base.metadata)


class AcdmFlight(Base):
    """Поточний стан рейсу з погляду A-CDM."""

    __tablename__ = "acdm_flight"

    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    flight_no: Mapped[str] = mapped_column(String(8), nullable=False)
    direction: Mapped[str] = mapped_column(String(1), nullable=False)
    sched_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # SOBT / SIBT
    estimated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # EOBT / ELDT
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    tobt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tobt_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # TOBT підтверджено вручну
    tsat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict:
        iso = lambda v: v.isoformat() if v else None  # noqa: E731
        return {"flight_id": str(self.flight_id), "flight_no": self.flight_no, "direction": self.direction,
                "sched_date": self.sched_date.isoformat(), "scheduled_at": iso(self.scheduled_at),
                "estimated_at": iso(self.estimated_at), "status": self.status, "tobt": iso(self.tobt),
                "tobt_confirmed": self.tobt_confirmed, "tsat": iso(self.tsat)}


class Milestone(Base):
    """Зафіксований етап A-CDM (журнал: нове значення додається новим записом)."""

    __tablename__ = "milestone"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    value: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {"code": self.code, "value": self.value.isoformat(), "source": self.source,
                "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None}


class DpiMessage(Base):
    """Сформоване повідомлення DPI для EUROCONTROL Network Manager (у прототипі не надсилається)."""

    __tablename__ = "dpi_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {"kind": self.kind, "body": self.body, "created_at": self.created_at.isoformat() if self.created_at else None}
