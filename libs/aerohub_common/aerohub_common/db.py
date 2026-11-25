"""Робота з базою даних: підключення, сесії, службові таблиці.

Кожен сервіс має власну базу даних (принцип database-per-service).
Службові таблиці outbox і processed_events додаються до метаданих сервісу
функцією :func:`add_infra_tables`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def ensure_database(url: str) -> None:
    """Створює базу даних сервісу, якщо її ще немає.

    Скрипт db/init.sql виконується PostgreSQL лише під час першої ініціалізації тому,
    тому сервіс, доданий пізніше, мусить створити свою базу сам (дефект D-08).
    """
    target = make_url(url)
    if not target.drivername.startswith("postgresql") or not target.database:
        return
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as c:
            exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}).scalar()
            if not exists:
                c.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        admin.dispose()


def make_engine(url: str) -> Engine:
    """Створює рушій SQLAlchemy з перевіркою з'єднань перед використанням."""
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Транзакційна сесія: commit при успіху, rollback при винятку."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def add_infra_tables(metadata: MetaData) -> tuple[Table, Table]:
    """Додає до метаданих таблиці outbox і processed_events.

    outbox - події, записані в одній транзакції зі зміною даних (Transactional Outbox);
    processed_events - ідентифікатори оброблених вхідних подій (ідемпотентний споживач).
    """
    outbox = Table(
        "outbox",
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("topic", String(100), nullable=False),
        Column("key", String(100), nullable=False),
        Column("payload", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=True, index=True),
    )
    processed = Table(
        "processed_events",
        metadata,
        Column("event_id", String(36), primary_key=True),
        Column("processed_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    return outbox, processed
