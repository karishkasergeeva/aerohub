"""Модель даних Resource Service (RMS).

Перетин призначень одного ресурсу забороняє обмеження виключення PostgreSQL
(EXCLUDE USING gist), тому конфліктне призначення неможливо зберегти навіть
у разі помилки в коді (розділ 10.3 звіту).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from aerohub_common.db import add_infra_tables


class Base(DeclarativeBase):
    metadata = MetaData()


OUTBOX, PROCESSED = add_infra_tables(Base.metadata)


class ResourceType:
    STAND = "STAND"  # стоянка ПС
    GATE = "GATE"    # вихід на посадку
    DESK = "DESK"    # стійка реєстрації
    BELT = "BELT"    # стрічка видачі багажу
    ALL = (STAND, GATE, DESK, BELT)


class Resource(Base):
    __tablename__ = "resource"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(5), nullable=False, index=True)
    terminal: Mapped[str] = mapped_column(String(3), nullable=False, default="D")
    zone: Mapped[str] = mapped_column(String(12), nullable=False, default="ANY")  # SCHENGEN / NON_SCHENGEN / ANY
    max_category: Mapped[str] = mapped_column(String(1), nullable=False, default="C")  # категорія ПС ICAO
    contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # стоянка з телетрапом
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def to_dict(self) -> dict:
        return {"id": self.id, "code": self.code, "type": self.type, "terminal": self.terminal, "zone": self.zone,
                "max_category": self.max_category, "contact": self.contact, "active": self.active}


class Unavailability(Base):
    """Період недоступності ресурсу (ремонт, закриття)."""

    __tablename__ = "resource_unavailability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resource.id", ondelete="CASCADE"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)


class FlightCache(Base):
    """Копія даних рейсу, отримана з подій Flight Service."""

    __tablename__ = "flight_cache"

    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    flight_no: Mapped[str] = mapped_column(String(8), nullable=False)
    direction: Mapped[str] = mapped_column(String(1), nullable=False)
    sched_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    ref_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    aircraft_type: Mapped[str] = mapped_column(String(4), nullable=False)
    schengen: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    linked_flight_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # пара прибуття/відправлення


class Allocation(Base):
    __tablename__ = "allocation"
    __table_args__ = (
        ExcludeConstraint(
            ("resource_id", "="),
            (text("tstzrange(start_at, end_at)"), "&&"),
            name="ex_allocation_no_overlap",
            using="gist",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resource.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(5), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(60), nullable=False, default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    resource: Mapped[Resource] = relationship(lazy="joined")

    def to_dict(self) -> dict:
        return {"id": self.id, "flight_id": str(self.flight_id), "kind": self.kind, "resource_code": self.resource.code,
                "resource_id": self.resource_id, "start_at": self.start_at.isoformat(), "end_at": self.end_at.isoformat(),
                "manual": self.manual, "created_by": self.created_by}


class Conflict(Base):
    """Невирішена потреба в ресурсі (немає вільного сумісного ресурсу)."""

    __tablename__ = "conflict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(5), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def to_dict(self) -> dict:
        return {"id": self.id, "flight_id": str(self.flight_id), "kind": self.kind, "message": self.message,
                "resolved": self.resolved, "created_at": self.created_at.isoformat() if self.created_at else None}
