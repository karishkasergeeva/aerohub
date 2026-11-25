"""Налаштування сервісу, що зчитуються зі змінних оточення."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Параметри запуску мікросервісу.

    Значення за замовчуванням підходять для локального запуску через docker compose.
    """

    service_name: str = field(default_factory=lambda: os.getenv("SERVICE_NAME", "aerohub"))
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql+psycopg://aerohub:aerohub@localhost:5432/aerohub"
        )
    )
    # "kafka" - справжня шина Apache Kafka; "memory" - шина в пам'яті (тести, локальна розробка)
    event_bus: str = field(default_factory=lambda: os.getenv("EVENT_BUS", "kafka"))
    kafka_bootstrap: str = field(default_factory=lambda: os.getenv("KAFKA_BOOTSTRAP", "localhost:9092"))
    jwt_secret: str = field(default_factory=lambda: os.getenv("JWT_SECRET", "dev-only-secret-change-me-in-production-0123456789"))
    jwt_ttl_minutes: int = field(default_factory=lambda: int(os.getenv("JWT_TTL_MINUTES", "720")))
    home_airport: str = field(default_factory=lambda: os.getenv("HOME_AIRPORT", "KBP"))
    outbox_poll_seconds: float = field(default_factory=lambda: float(os.getenv("OUTBOX_POLL_SECONDS", "0.3")))


def get_settings() -> Settings:
    """Повертає налаштування (створюються заново, щоб тести могли змінювати оточення)."""
    return Settings()
