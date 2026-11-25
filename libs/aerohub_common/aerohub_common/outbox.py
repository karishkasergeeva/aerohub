"""Шаблони надійної доставки подій.

Transactional Outbox: сервіс записує зміну даних і подію в одній транзакції
(функція :func:`add_to_outbox`), а фоновий процес :class:`OutboxRelay`
передає неопубліковані події в шину. Подія не губиться, навіть якщо брокер
тимчасово недоступний.

Ідемпотентний споживач: :func:`mark_processed` повертає False, якщо подію з таким
ідентифікатором уже оброблено, тож повторна доставка не дублює зміни.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import Table, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from .bus import EventBus
from .events import Envelope, utcnow

log = logging.getLogger("aerohub.outbox")


def add_to_outbox(session: Session, outbox: Table, topic: str, envelope: Envelope) -> None:
    """Записує подію в таблицю outbox у поточній транзакції."""
    session.execute(
        insert(outbox).values(topic=topic, key=envelope.aggregate_id, payload=envelope.__dict__)
    )


def mark_processed(session: Session, processed: Table, event_id: str) -> bool:
    """Позначає подію як оброблену. Повертає False, якщо її вже було оброблено."""
    inserted = session.execute(
        pg_insert(processed).values(event_id=event_id)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(processed.c.event_id)
    ).scalar()
    return inserted is not None


class OutboxRelay:
    """Фоновий процес передавання подій з outbox у шину."""

    def __init__(self, factory: sessionmaker[Session], outbox: Table, bus: EventBus, poll_seconds: float = 0.3):
        self.factory = factory
        self.outbox = outbox
        self.bus = bus
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None

    async def flush(self, batch: int = 100) -> int:
        """Публікує до `batch` неопублікованих подій. Повертає їх кількість."""
        published = 0
        while True:
            rows = await asyncio.to_thread(self._fetch, batch)
            if not rows:
                return published
            for row in rows:
                await self.bus.publish(row.topic, row.key, Envelope.from_json(row.payload))
                await asyncio.to_thread(self._mark, row.id)
                published += 1

    def _fetch(self, batch: int):
        with self.factory() as s:
            return s.execute(
                select(self.outbox).where(self.outbox.c.published_at.is_(None)).order_by(self.outbox.c.id).limit(batch)
            ).all()

    def _mark(self, row_id: int) -> None:
        with self.factory() as s, s.begin():
            s.execute(update(self.outbox).where(self.outbox.c.id == row_id).values(published_at=utcnow()))

    async def _loop(self) -> None:
        while True:
            try:
                await self.flush()
            except Exception:
                log.exception("Помилка публікації подій з outbox")
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
