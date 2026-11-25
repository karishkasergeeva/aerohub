"""Тести Flight Service: CRUD, статуси, історія, оптимістичне блокування, SSIM, outbox.

Потрібна PostgreSQL: TEST_FLIGHT_DB (за замовчуванням локальний контейнер на порту 55432).
Шина подій - InMemoryBus, тому Kafka для тестів не потрібна.
"""

import asyncio
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aerohub_common.auth import issue_token
from aerohub_common.bus import InMemoryBus
from app.main import create_app
from app.models import Base
from app.ssim import build_record, parse_ssim

DB = os.getenv("TEST_FLIGHT_DB", "postgresql+psycopg://aerohub:aerohub@localhost:55432/flight_db")


@pytest.fixture()
def ctx():
    bus = InMemoryBus()
    app = create_app(bus=bus, database_url=DB)
    Base.metadata.drop_all(app.state.engine)
    with TestClient(app) as client:
        yield client, bus, app


def auth(role="planner"):
    roles = {"planner": ["planner"], "aoc": ["aoc_dispatcher"], "agent": ["agent"], "viewer": ["billing"]}[role]
    return {"Authorization": f"Bearer {issue_token(role, roles)}"}


FLIGHT = {"airline": "PS", "number": "101", "direction": "D", "station": "WAW", "aircraft_type": "738",
          "scheduled_at": "2026-12-01T10:30:00Z", "terminal": "D", "schengen": True}


def create(client, **over):
    r = client.post("/api/v1/flights", json={**FLIGHT, **over}, headers=auth())
    assert r.status_code == 201, r.text
    return r.json()


def test_requires_authentication(ctx):
    client, _, _ = ctx
    assert client.get("/api/v1/flights").status_code == 401


def test_token_endpoint(ctx):
    client, _, _ = ctx
    r = client.post("/api/v1/auth/token", json={"username": "planner", "password": "planner"})
    assert r.status_code == 200 and "access_token" in r.json()
    assert client.post("/api/v1/auth/token", json={"username": "planner", "password": "x"}).status_code == 401


def test_create_and_duplicate(ctx):
    client, _, _ = ctx
    f = create(client)
    assert f["status"] == "SCHEDULED" and f["version"] == 1 and f["flight_no"] == "PS101"
    r = client.post("/api/v1/flights", json=FLIGHT, headers=auth())
    assert r.status_code == 409


def test_agent_cannot_create(ctx):
    client, _, _ = ctx
    assert client.post("/api/v1/flights", json=FLIGHT, headers=auth("agent")).status_code == 403


def test_update_requires_version_and_reason(ctx):
    client, _, _ = ctx
    f = create(client)
    url = f"/api/v1/flights/{f['id']}"
    assert client.patch(url, json={"terminal": "F"}, headers=auth()).status_code == 428
    r = client.patch(url, json={"version": 1, "estimated_at": "2026-12-01T11:15:00Z"}, headers=auth())
    assert r.status_code == 422  # критичне поле без причини
    r = client.patch(url, json={"version": 1, "estimated_at": "2026-12-01T11:15:00Z", "reason": "93"}, headers=auth())
    assert r.status_code == 200 and r.json()["version"] == 2
    # друга зміна зі старою версією - конфлікт (оптимістичне блокування)
    r = client.patch(url, json={"version": 1, "terminal": "F"}, headers=auth())
    assert r.status_code == 409
    # версію можна передати і заголовком If-Match
    r = client.patch(url, json={"terminal": "F"}, headers={**auth(), "If-Match": '"2"'})
    assert r.status_code == 200 and r.json()["terminal"] == "F"


def test_status_lifecycle(ctx):
    client, _, _ = ctx
    f = create(client)
    url = f"/api/v1/flights/{f['id']}/status"
    assert client.post(url, json={"status": "ARRIVED"}, headers=auth("aoc")).status_code == 409
    assert client.post(url, json={"status": "DELAYED"}, headers=auth("aoc")).status_code == 422
    for st in ["CHECK_IN", "BOARDING", "DEPARTED"]:
        r = client.post(url, json={"status": st}, headers=auth("agent"))
        assert r.status_code == 200, r.text
    assert r.json()["actual_at"] is not None
    assert client.patch(f"/api/v1/flights/{f['id']}", json={"version": 4, "terminal": "F"}, headers=auth()).status_code == 200


def test_cancel_and_delete_rules(ctx):
    client, _, _ = ctx
    f1, f2 = create(client), create(client, number="102")
    client.post(f"/api/v1/flights/{f1['id']}/status", json={"status": "CHECK_IN"}, headers=auth("aoc"))
    assert client.delete(f"/api/v1/flights/{f1['id']}", headers=auth()).status_code == 409
    r = client.post(f"/api/v1/flights/{f1['id']}/cancel", json={"reason": "Погодні умови"}, headers=auth())
    assert r.json()["status"] == "CANCELLED"
    assert client.delete(f"/api/v1/flights/{f2['id']}", headers=auth()).status_code == 204
    assert client.get(f"/api/v1/flights/{f2['id']}", headers=auth()).status_code == 404


def test_history_records_every_change(ctx):
    client, _, _ = ctx
    f = create(client)
    client.patch(f"/api/v1/flights/{f['id']}", json={"version": 1, "aircraft_type": "73H", "reason": "Заміна ПС"}, headers=auth())
    client.post(f"/api/v1/flights/{f['id']}/status", json={"status": "DELAYED", "reason": "93", "at": "2026-12-01T12:00:00Z"}, headers=auth("aoc"))
    h = client.get(f"/api/v1/flights/{f['id']}/history", headers=auth("viewer")).json()
    assert [e["event_type"] for e in h] == ["FlightCreated", "FlightUpdated", "FlightStatusChanged"]
    assert h[1]["changes"] == [{"field": "aircraft_type", "old": "738", "new": "73H"}]
    assert h[2]["changed_by"] == "aoc" and h[2]["reason"] == "93"


def test_link_turnaround(ctx):
    client, _, _ = ctx
    arr = create(client, number="102", direction="A", scheduled_at="2026-12-01T09:20:00Z")
    dep = create(client)
    r = client.post(f"/api/v1/flights/{arr['id']}/link", json={"other_flight_id": dep["id"]}, headers=auth())
    assert r.status_code == 200 and r.json()["linked_flight_id"] == dep["id"]
    bad = create(client, number="103", direction="A", scheduled_at="2026-12-01T11:00:00Z")
    assert client.post(f"/api/v1/flights/{bad['id']}/link", json={"other_flight_id": dep["id"]}, headers=auth()).status_code == 422


def test_outbox_publishes_events(ctx):
    client, bus, app = ctx
    f = create(client)
    asyncio.run(app.state.relay.flush())
    types = [(t, e.type, e.aggregate_id) for t, _, e in bus.published]
    assert ("flight.events", "FlightCreated", f["id"]) in types
    with app.state.engine.connect() as c:
        assert c.execute(text("select count(*) from outbox where published_at is null")).scalar() == 0


def test_ssim_parser():
    rec = build_record("PS", "101", date(2026, 12, 1), date(2026, 12, 7), "1 3 5  ", "KBP", "1030", "WAW", "1150", "738")
    flights, errors = parse_ssim("1AIRLINE STANDARD SCHEDULE DATA SET\n" + rec, "KBP")
    assert not errors
    assert [f.scheduled_at.day for f in flights] == [2, 4, 7]  # 2.12.2026 - ср, 4.12 - пт, 7.12 - пн
    assert all(f.direction == "D" and f.station == "WAW" for f in flights)


def test_ssim_import_creates_and_updates(ctx):
    client, _, _ = ctx
    recs = "\n".join([
        build_record("PS", "101", date(2026, 12, 1), date(2026, 12, 3), "1234567", "KBP", "1030", "WAW", "1150", "738"),
        build_record("LO", "752", date(2026, 12, 1), date(2026, 12, 1), "1234567", "WAW", "0700", "KBP", "0915", "E95"),
        "3 XX 0001  BROKEN",
    ])
    r = client.post("/api/v1/schedules/imports", files={"file": ("s.ssim", recs)}, headers=auth())
    body = r.json()
    assert r.status_code == 200 and body["created"] == 4 and len(body["errors"]) == 1
    recs2 = build_record("PS", "101", date(2026, 12, 1), date(2026, 12, 3), "1234567", "KBP", "1100", "WAW", "1220", "73H")
    body = client.post("/api/v1/schedules/imports", files={"file": ("s.ssim", recs2)}, headers=auth()).json()
    assert body["updated"] == 3 and body["created"] == 0
    flights = client.get("/api/v1/flights?date=2026-12-02&direction=D", headers=auth()).json()
    assert flights[0]["aircraft_type"] == "73H" and flights[0]["scheduled_at"].startswith("2026-12-02T11:00")
