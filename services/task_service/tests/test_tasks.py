"""Тести Task Service: генерація завдань, зсув за часом і TOBT, виконання, SLA, скасування."""

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from aerohub_common.auth import issue_token
from aerohub_common.bus import InMemoryBus
from aerohub_common.db import session_scope
from aerohub_common.events import Envelope
from app import service
from app.main import create_app
from app.models import Base

DB = os.getenv("TEST_TASK_DB", "postgresql+psycopg://aerohub:aerohub@localhost:55432/task_db")
H = {"Authorization": f"Bearer {issue_token('handler', ['handler'])}"}


@pytest.fixture()
def ctx():
    bus = InMemoryBus()
    app = create_app(bus=bus, database_url=DB, overdue_check_seconds=3600)
    Base.metadata.drop_all(app.state.engine)
    with TestClient(app) as client:
        yield client, bus, app


def flight(no="PS101", direction="D", at="2026-12-01T10:30:00+00:00", **kw):
    f = {"id": str(uuid.uuid4()), "flight_no": no, "direction": direction, "sched_date": at[:10], "scheduled_at": at,
         "estimated_at": None, "status": "SCHEDULED"}
    f.update(kw)
    return f


def send(app, etype, f):
    asyncio.run(app.state.on_flight(Envelope(type=etype, aggregate_id=f["id"], payload={"flight": f})))


def tasks(client, f):
    return {t["template_code"]: t for t in client.get(f"/api/v1/tasks?flight_id={f['id']}", headers=H).json()}


def test_tasks_generated_from_templates(ctx):
    client, _, app = ctx
    d, a = flight(), flight("LO1", "A")
    send(app, "FlightCreated", d)
    send(app, "FlightCreated", a)
    td, ta = tasks(client, d), tasks(client, a)
    assert set(td) == {"CLEAN", "CATER", "FUEL", "BAGLOAD", "READY", "PUSH"} and set(ta) == {"CHOCKS", "BAGUNLOAD", "BUS"}
    assert td["FUEL"]["planned_start"].startswith("2026-12-01T09:45") and td["FUEL"]["team"] == "fuel"


def test_delay_shifts_only_planned_tasks(ctx):
    client, _, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    clean = tasks(client, d)["CLEAN"]
    client.post(f"/api/v1/tasks/{clean['id']}/start", json={"version": clean["version"]}, headers=H)
    send(app, "FlightUpdated", {**d, "estimated_at": "2026-12-01T11:30:00+00:00"})
    t = tasks(client, d)
    assert t["FUEL"]["planned_start"].startswith("2026-12-01T10:45")
    assert t["CLEAN"]["planned_start"].startswith("2026-12-01T09:35")  # розпочате завдання не зсувається


def test_tobt_change_reschedules(ctx):
    client, _, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    asyncio.run(app.state.on_acdm(Envelope(type="TobtChanged", aggregate_id=d["id"],
                                           payload={"flight_id": d["id"], "tobt": "2026-12-01T10:50:00+00:00"})))
    assert tasks(client, d)["PUSH"]["planned_start"].startswith("2026-12-01T10:50")


def test_start_complete_and_version_conflict(ctx):
    client, bus, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    t = tasks(client, d)["READY"]
    r = client.post(f"/api/v1/tasks/{t['id']}/start", json={"version": t["version"]}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "IN_PROGRESS" and r.json()["assignee"] == "handler"
    assert client.post(f"/api/v1/tasks/{t['id']}/complete", json={"version": t["version"]}, headers=H).status_code == 409
    r = client.post(f"/api/v1/tasks/{t['id']}/complete", json={"version": t["version"] + 1}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "DONE"
    asyncio.run(app.state.relay.flush())
    assert any(e.type == "TaskCompleted" and e.payload["template_code"] == "READY" for _, _, e in bus.published)


def test_offline_completion_time_and_validation(ctx):
    client, _, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    t = tasks(client, d)["CATER"]
    r = client.post(f"/api/v1/tasks/{t['id']}/start", json={"version": 1, "at": "2026-12-01T09:41:00Z"}, headers=H)
    r = client.post(f"/api/v1/tasks/{t['id']}/complete", json={"version": 2, "at": "2026-12-01T09:30:00Z"}, headers=H)
    assert r.status_code == 422


def test_overdue_and_cancel(ctx):
    client, _, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    with session_scope(app.state.factory) as s:
        late = service.mark_overdue(s, now=datetime(2026, 12, 1, 10, 10, tzinfo=timezone.utc))
    assert {t.template_code for t in late} == {"CLEAN", "CATER"}
    assert len(client.get("/api/v1/tasks?overdue=true", headers=H).json()) == 2
    send(app, "FlightCancelled", {**d, "status": "CANCELLED"})
    assert {t["status"] for t in tasks(client, d).values()} == {"CANCELLED"}


def test_requires_handler_role(ctx):
    client, _, app = ctx
    d = flight()
    send(app, "FlightCreated", d)
    t = tasks(client, d)["FUEL"]
    agent = {"Authorization": f"Bearer {issue_token('agent', ['agent'])}"}
    assert client.post(f"/api/v1/tasks/{t['id']}/start", json={"version": 1}, headers=agent).status_code == 403


def test_departure_closes_open_tasks_and_arrival_uses_actual_time(ctx):
    client, _, app = ctx
    d, a = flight(), flight("LO1", "A", at="2026-12-01T08:00:00+00:00")
    send(app, "FlightCreated", d)
    send(app, "FlightCreated", a)
    send(app, "FlightStatusChanged", {**d, "status": "DEPARTED", "actual_at": "2026-12-01T10:34:00+00:00"})
    t = tasks(client, d)
    assert {x["status"] for x in t.values()} == {"DONE"} and t["CLEAN"]["assignee"] == "авто (AOBT)"
    send(app, "FlightStatusChanged", {**a, "status": "ARRIVED", "actual_at": "2026-12-01T07:40:00+00:00"})
    assert tasks(client, a)["CHOCKS"]["planned_start"].startswith("2026-12-01T07:40")
