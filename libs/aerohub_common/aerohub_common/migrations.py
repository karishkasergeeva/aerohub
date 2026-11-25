"""Версійні міграції схеми бази даних.

Base.metadata.create_all() створює лише відсутні таблиці й не змінює наявні, тому зміни
структури після першого розгортання виконуються міграціями (дефект D-11). Кожна міграція
має номер версії; застосовані версії записуються в таблицю schema_migrations, отже
міграція виконується рівно один раз.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("aerohub.migrations")


def apply_migrations(engine: Engine, migrations: list[tuple[int, str, str]]) -> list[int]:
    """Застосовує міграції (версія, опис, SQL), яких ще немає в schema_migrations."""
    applied: list[int] = []
    with engine.begin() as c:
        c.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY, "
                       "description varchar(200) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"))
        c.execute(text("SELECT pg_advisory_xact_lock(727274)"))  # одночасний старт кількох екземплярів
        done = {r[0] for r in c.execute(text("SELECT version FROM schema_migrations"))}
        for version, description, sql in sorted(migrations):
            if version in done:
                continue
            c.execute(text(sql))
            c.execute(text("INSERT INTO schema_migrations(version, description) VALUES (:v, :d)"), {"v": version, "d": description})
            log.info("Застосовано міграцію %s: %s", version, description)
            applied.append(version)
    return applied
