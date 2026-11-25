"""Схеми запитів і відповідей REST API Flight Service."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TokenRequest(BaseModel):
    username: str
    password: str


class FlightCreate(BaseModel):
    airline: str = Field(min_length=2, max_length=3, examples=["PS"])
    number: str = Field(min_length=1, max_length=5, examples=["101"])
    direction: Literal["A", "D"]
    station: str = Field(min_length=3, max_length=3, examples=["WAW"])
    aircraft_type: str = Field(min_length=3, max_length=4, examples=["738"])
    scheduled_at: datetime
    terminal: str | None = None
    schengen: bool = True

    @field_validator("airline", "station")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("scheduled_at")
    @classmethod
    def aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("Час має містити часовий пояс (наприклад, 2026-12-01T10:30:00Z)")
        return v


class FlightPatch(BaseModel):
    """Зміна атрибутів рейсу. `version` - версія запису, яку бачив користувач."""

    version: int | None = None
    reason: str | None = Field(default=None, max_length=200)
    station: str | None = None
    aircraft_type: str | None = None
    scheduled_at: datetime | None = None
    estimated_at: datetime | None = None
    actual_at: datetime | None = None
    terminal: str | None = None
    schengen: bool | None = None
    delay_code: str | None = None


class StatusChange(BaseModel):
    status: str
    reason: str | None = None
    at: datetime | None = Field(default=None, description="Фактичний час події (для DEPARTED/ARRIVED)")
    version: int | None = None


class CancelRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=200)


class LinkRequest(BaseModel):
    other_flight_id: str


class ImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: list[dict]
