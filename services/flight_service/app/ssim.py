"""Спрощений парсер сезонного розкладу у форматі IATA SSIM (глава 7, записи типу 3).

Використовуються позиції полів запису типу 3 (нумерація з 1):
  1      тип запису ('3')
  3-5    код авіакомпанії
  6-9    номер рейсу
  15-21  початок періоду дії (DDMMMYY)
  22-28  кінець періоду дії (DDMMMYY)
  29-35  дні виконання (1..7 або пробіл)
  37-39  аеропорт відправлення
  40-43  час відправлення (пасажирський, HHMM)
  55-57  аеропорт прибуття
  62-65  час прибуття (пасажирський, HHMM)
  73-75  тип повітряного судна

Обмеження прототипу: час трактується як UTC, варіанти маршрутів і багатосегментні
рейси не обробляються.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


@dataclass
class PlannedFlight:
    airline: str
    number: str
    direction: str
    station: str
    aircraft_type: str
    scheduled_at: datetime


@dataclass
class ParseError:
    line: int
    message: str


def _date(s: str) -> date:
    return date(2000 + int(s[5:7]), MONTHS[s[2:5].upper()], int(s[0:2]))


def _hhmm(s: str) -> time:
    return time(int(s[:2]), int(s[2:4]))


def parse_ssim(text: str, home_airport: str) -> tuple[list[PlannedFlight], list[ParseError]]:
    """Розбирає текст SSIM і повертає рейси аеропорту `home_airport` на кожну дату періоду."""
    flights: list[PlannedFlight] = []
    errors: list[ParseError] = []
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.startswith("3"):
            continue
        line = raw.ljust(200)
        try:
            airline = line[2:5].strip()
            number = line[5:9].strip().lstrip("0") or "0"
            start, end = _date(line[14:21]), _date(line[21:28])
            days = {int(c) for c in line[28:35] if c.isdigit()}
            dep, std = line[36:39].strip(), _hhmm(line[39:43])
            arr, sta = line[54:57].strip(), _hhmm(line[61:65])
            ac = line[72:75].strip() or "320"
            if end < start:
                raise ValueError("кінець періоду раніше за початок")
        except (ValueError, KeyError, IndexError) as exc:
            errors.append(ParseError(n, f"Некоректний запис типу 3: {exc}"))
            continue
        if home_airport not in (dep, arr):
            errors.append(ParseError(n, f"Рейс {airline}{number} не стосується аеропорту {home_airport}"))
            continue
        d = start
        while d <= end:
            if d.isoweekday() in days:
                if dep == home_airport:
                    flights.append(PlannedFlight(airline, number, "D", arr, ac, datetime.combine(d, std, timezone.utc)))
                if arr == home_airport:
                    arr_day = d + timedelta(days=1) if sta < std else d
                    flights.append(PlannedFlight(airline, number, "A", dep, ac, datetime.combine(arr_day, sta, timezone.utc)))
            d += timedelta(days=1)
    return flights, errors


def build_record(airline: str, number: str, start: date, end: date, days: str, dep: str, std: str, arr: str, sta: str, ac: str) -> str:
    """Формує запис типу 3 (використовується в тестах і демо-даних)."""
    def ddmmmyy(d: date) -> str:
        return f"{d.day:02d}{list(MONTHS)[d.month - 1]}{d.year % 100:02d}"

    line = [" "] * 200
    def put(pos: int, value: str) -> None:
        for i, ch in enumerate(value):
            line[pos - 1 + i] = ch

    put(1, "3"); put(3, airline.ljust(3)); put(6, number.rjust(4, "0")); put(10, "01"); put(12, "01"); put(14, "J")
    put(15, ddmmmyy(start)); put(22, ddmmmyy(end)); put(29, days.ljust(7)); put(37, dep); put(40, std); put(44, std)
    put(48, "+0000"); put(55, arr); put(58, sta); put(62, sta); put(66, "+0000"); put(73, ac)
    return "".join(line).rstrip()
