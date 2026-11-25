"""Доменні правила рейсу: життєвий цикл статусів і критичні поля (вимоги FR-05, FR-08)."""

from __future__ import annotations


class Status:
    SCHEDULED = "SCHEDULED"   # заплановано
    CHECK_IN = "CHECK_IN"     # реєстрація
    BOARDING = "BOARDING"     # посадка
    DEPARTED = "DEPARTED"     # відправлено
    RETURNED = "RETURNED"     # повернувся на стоянку
    ARRIVED = "ARRIVED"       # прибув
    DELAYED = "DELAYED"       # затримано
    CANCELLED = "CANCELLED"   # скасовано
    DIVERTED = "DIVERTED"     # перенаправлено


# Допустимі переходи статусів для рейсів відправлення (D) і прибуття (A)
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "D": {
        Status.SCHEDULED: {Status.CHECK_IN, Status.DELAYED, Status.CANCELLED},
        Status.CHECK_IN: {Status.BOARDING, Status.DELAYED, Status.CANCELLED},
        Status.BOARDING: {Status.DEPARTED, Status.DELAYED, Status.CANCELLED},
        Status.DELAYED: {Status.DELAYED, Status.CHECK_IN, Status.BOARDING, Status.DEPARTED, Status.CANCELLED},
        Status.DEPARTED: {Status.RETURNED},
        Status.RETURNED: {Status.BOARDING, Status.DELAYED, Status.CANCELLED},
        Status.CANCELLED: set(),
    },
    "A": {
        Status.SCHEDULED: {Status.DELAYED, Status.ARRIVED, Status.DIVERTED, Status.CANCELLED},
        Status.DELAYED: {Status.DELAYED, Status.ARRIVED, Status.DIVERTED, Status.CANCELLED},
        Status.ARRIVED: set(),
        Status.DIVERTED: set(),
        Status.CANCELLED: set(),
    },
}

FINAL_STATUSES = {Status.CANCELLED, Status.ARRIVED, Status.DIVERTED}

# Поля, зміна яких потребує зазначення причини (код затримки IATA або текст)
CRITICAL_FIELDS = {"scheduled_at", "estimated_at", "aircraft_type"}

# Поля, які дозволено змінювати через PATCH
EDITABLE_FIELDS = {"station", "aircraft_type", "scheduled_at", "estimated_at", "actual_at", "terminal", "schengen", "delay_code"}


class DomainError(Exception):
    """Порушення бізнес-правила; API повертає 409 або 422."""

    def __init__(self, message: str, code: int = 409):
        super().__init__(message)
        self.code = code


def check_transition(direction: str, current: str, new: str) -> None:
    allowed = TRANSITIONS[direction].get(current, set())
    if new not in allowed:
        raise DomainError(
            f"Недопустимий перехід статусу {current} → {new} для рейсу "
            f"{'відправлення' if direction == 'D' else 'прибуття'}. Дозволено: {', '.join(sorted(allowed)) or 'немає'}"
        )
