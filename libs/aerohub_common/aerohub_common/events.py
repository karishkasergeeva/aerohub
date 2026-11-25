"""Конверт доменної події та перелік топіків.

Усі сервіси обмінюються подіями однакової структури (розділ 10.2 звіту):
ідентифікатор події, тип, версія схеми, час виникнення, ключ агрегату,
ідентифікатор кореляції та корисне навантаження.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


class Topics:
    """Назви топіків Apache Kafka."""

    FLIGHT_EVENTS = "flight.events"
    RESOURCE_ALLOCATIONS = "resource.allocations"
    ACDM_MILESTONES = "acdm.milestones"
    TASK_EVENTS = "task.events"


class EventTypes:
    """Типи доменних подій."""

    FLIGHT_CREATED = "FlightCreated"
    FLIGHT_UPDATED = "FlightUpdated"
    FLIGHT_STATUS_CHANGED = "FlightStatusChanged"
    FLIGHT_CANCELLED = "FlightCancelled"
    FLIGHT_DELETED = "FlightDeleted"
    RESOURCE_ALLOCATED = "ResourceAllocated"
    RESOURCE_RELEASED = "ResourceReleased"
    RESOURCE_CONFLICT_DETECTED = "ResourceConflictDetected"
    MILESTONE_REACHED = "MilestoneReached"
    TOBT_CHANGED = "TobtChanged"
    TSAT_ISSUED = "TsatIssued"
    TASK_CREATED = "TaskCreated"
    TASK_UPDATED = "TaskUpdated"
    TASK_COMPLETED = "TaskCompleted"
    TASK_OVERDUE = "TaskOverdue"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Envelope:
    """Конверт події, що передається через шину."""

    type: str
    aggregate_id: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = 1
    occurred_at: str = field(default_factory=lambda: utcnow().isoformat())
    correlation_id: str | None = None
    source: str | None = None

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False, default=str).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str | dict) -> "Envelope":
        data = raw if isinstance(raw, dict) else json.loads(raw)
        return cls(**data)
