"""Формування рядка табло FIDS з даних рейсу та призначених ресурсів (українською та англійською)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Табло показує місцевий час аеропорту; у базі та подіях час зберігається в UTC (дефект D-13)
DISPLAY_TZ = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "Europe/Kyiv"))

CITIES = {
    "WAW": ("Варшава", "Warsaw"), "KRK": ("Краків", "Krakow"), "VIE": ("Відень", "Vienna"), "IST": ("Стамбул", "Istanbul"),
    "FRA": ("Франкфурт", "Frankfurt"), "MUC": ("Мюнхен", "Munich"), "AMS": ("Амстердам", "Amsterdam"),
    "LHR": ("Лондон", "London"), "CDG": ("Париж", "Paris"), "RIX": ("Рига", "Riga"), "VNO": ("Вільнюс", "Vilnius"),
    "BCN": ("Барселона", "Barcelona"), "ROM": ("Рим", "Rome"), "FCO": ("Рим", "Rome"), "DXB": ("Дубай", "Dubai"),
    "TLV": ("Тель-Авів", "Tel Aviv"), "PRG": ("Прага", "Prague"), "BUD": ("Будапешт", "Budapest"), "LWO": ("Львів", "Lviv"),
    "ODS": ("Одеса", "Odesa"), "KBP": ("Київ", "Kyiv"),
}

STATUS_TEXT = {
    "SCHEDULED": ("За розкладом", "On time"),
    "CHECK_IN": ("Реєстрація", "Check-in"),
    "BOARDING": ("Посадка", "Boarding"),
    "DEPARTED": ("Відправлено", "Departed"),
    "RETURNED": ("Повернувся", "Returned"),
    "ARRIVED": ("Прибув", "Landed"),
    "CANCELLED": ("Скасовано", "Cancelled"),
    "DIVERTED": ("Перенаправлено", "Diverted"),
}


def _hm(iso: str | None) -> str | None:
    return datetime.fromisoformat(iso).astimezone(DISPLAY_TZ).strftime("%H:%M") if iso else None


def remark(flight: dict) -> tuple[str, str]:
    status = flight["status"]
    est, sched = flight.get("estimated_at"), flight["scheduled_at"]
    late = est and datetime.fromisoformat(est) - datetime.fromisoformat(sched) > timedelta(minutes=5)
    if status == "DELAYED" or (status == "SCHEDULED" and late):
        t = _hm(est)
        return (f"Затримується до {t}", f"Delayed to {t}") if t else ("Затримується", "Delayed")
    if status == "ARRIVED" and flight.get("actual_at"):
        t = _hm(flight["actual_at"])
        return (f"Прибув о {t}", f"Landed {t}")
    return STATUS_TEXT.get(status, (status, status))


def build_row(flight: dict, allocations: list[dict]) -> dict:
    by_kind: dict[str, list[str]] = {}
    for a in allocations:
        by_kind.setdefault(a["kind"], []).append(a["resource_code"])
    uk, en = remark(flight)
    city = CITIES.get(flight["station"], (flight["station"], flight["station"]))
    return {
        "id": flight["id"],
        "flight_no": flight["flight_no"],
        "airline": flight["airline"],
        "direction": flight["direction"],
        "sched_date": flight["sched_date"],
        "station": flight["station"],
        "city_uk": city[0],
        "city_en": city[1],
        "scheduled_at": flight["scheduled_at"],
        "time": _hm(flight["scheduled_at"]),
        "estimated_at": flight.get("estimated_at"),
        "estimated_time": _hm(flight.get("estimated_at")),
        "actual_time": _hm(flight.get("actual_at")),
        "status": flight["status"],
        "remark_uk": uk,
        "remark_en": en,
        "terminal": flight.get("terminal"),
        "gate": ", ".join(sorted(by_kind.get("GATE", []))) or None,
        "stand": ", ".join(sorted(by_kind.get("STAND", []))) or None,
        "desks": ", ".join(sorted(by_kind.get("DESK", []))) or None,
        "belt": ", ".join(sorted(by_kind.get("BELT", []))) or None,
        "aircraft_type": flight["aircraft_type"],
        "version": flight["version"],
    }
