"""Шина подій.

KafkaBus - робоча реалізація на Apache Kafka (aiokafka).
InMemoryBus - реалізація в пам'яті для модульних тестів і запуску без Kafka.

Обидві реалізують однаковий інтерфейс: publish() і subscribe().
Обробник отримує конверт події; повторна доставка можлива, тому обробники
мають бути ідемпотентними (див. aerohub_common.outbox.idempotent).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

from .events import Envelope

log = logging.getLogger("aerohub.bus")

Handler = Callable[[Envelope], Awaitable[None]]


class EventBus(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def publish(self, topic: str, key: str, envelope: Envelope) -> None: ...
    def subscribe(self, topics: list[str], group_id: str, handler: Handler) -> None: ...


class InMemoryBus:
    """Шина в пам'яті: доставляє подію всім підписникам одразу після публікації."""

    def __init__(self) -> None:
        self._subs: list[tuple[set[str], str, Handler]] = []
        self.published: list[tuple[str, str, Envelope]] = []

    async def start(self) -> None:  # noqa: D401 - нічого запускати
        return None

    async def stop(self) -> None:
        return None

    async def publish(self, topic: str, key: str, envelope: Envelope) -> None:
        self.published.append((topic, key, envelope))
        for topics, _group, handler in list(self._subs):
            if topic in topics:
                await handler(envelope)

    def subscribe(self, topics: list[str], group_id: str, handler: Handler) -> None:
        self._subs.append((set(topics), group_id, handler))


class KafkaBus:
    """Шина на Apache Kafka.

    Події одного агрегату публікуються з однаковим ключем (наприклад, flightId),
    тому потрапляють в один розділ і обробляються в порядку виникнення.
    """

    def __init__(self, bootstrap: str, client_id: str) -> None:
        self.bootstrap = bootstrap
        self.client_id = client_id
        self._producer = None
        self._consumers: list = []
        self._tasks: list[asyncio.Task] = []
        self._pending: list[tuple[list[str], str, Handler]] = []

    async def start(self) -> None:
        from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

        self._producer = await _retry(lambda: _start(AIOKafkaProducer(
            bootstrap_servers=self.bootstrap, client_id=self.client_id, acks="all", enable_idempotence=True
        )))
        for topics, group, handler in self._pending:
            consumer = await _retry(lambda: _start(AIOKafkaConsumer(
                *topics,
                bootstrap_servers=self.bootstrap,
                group_id=group,
                enable_auto_commit=False,
                auto_offset_reset="earliest",
            )))
            self._consumers.append(consumer)
            self._tasks.append(asyncio.create_task(self._consume(consumer, handler)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for c in self._consumers:
            await c.stop()
        if self._producer:
            await self._producer.stop()

    async def publish(self, topic: str, key: str, envelope: Envelope) -> None:
        assert self._producer is not None, "KafkaBus не запущено"
        await self._producer.send_and_wait(topic, envelope.to_json(), key=key.encode())

    def subscribe(self, topics: list[str], group_id: str, handler: Handler) -> None:
        self._pending.append((topics, group_id, handler))

    async def _consume(self, consumer, handler: Handler) -> None:
        async for msg in consumer:
            try:
                await handler(Envelope.from_json(msg.value))
            except Exception:  # подію не підтверджуємо - її буде доставлено повторно
                log.exception("Помилка обробки події з %s", msg.topic)
                from aiokafka import TopicPartition

                await asyncio.sleep(1)
                consumer.seek(TopicPartition(msg.topic, msg.partition), msg.offset)
                continue
            await consumer.commit()


async def _start(client):
    await client.start()
    return client


async def _retry(factory, attempts: int = 30, delay: float = 2.0):
    """Повторює підключення до Kafka, поки брокер не стане доступним."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return await factory()
        except Exception as exc:  # брокер ще не готовий
            last = exc
            log.warning("Kafka недоступна, повтор через %.0f с: %s", delay, exc)
            await asyncio.sleep(delay)
    raise RuntimeError(f"Не вдалося підключитися до Kafka: {last}")


def build_bus(kind: str, bootstrap: str, client_id: str) -> EventBus:
    """Фабрика шини за налаштуванням EVENT_BUS."""
    if kind == "memory":
        return InMemoryBus()
    return KafkaBus(bootstrap, client_id)
