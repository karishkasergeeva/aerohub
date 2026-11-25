"""Тести A-CDM Service: етапи з подій рейсу, TOBT, секвенсор TSAT, DPI, ARDT із завдань."""

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aerohub_common.auth import issue_token
from aerohub_common.bus import InMemoryBus
from aerohub_common.events import Envelope
from app.main import create_app
from app.models import Base

DB = os.getenv("TEST_ACDM_DB", "postgresql+psycopg://aerohub:aerohub@localhost:55432/acdm_db")
H = {"Authorization": f"Bearer {issue_token('handler', ['handler'])}"}
AOC = {"Authorization": f"Bearer {issue_token('aoc', ['aoc_dispatcher'])}"}


@pytest.fixture()
def ctx():
    bus = InMemoryBus()
    app = create_app(bus=bus, database_url=DB)
    Base.metadata.drop_all(app.state.engine)
    with TestClient(app) as client:
        yield client, bus, app


def flight(no="PS101", direction="D", at="2026-12-01T10:30:00+00:00", **kw):
    f = {"id": str(uuid.uuid4()), "flight_no": no, "direction": direction, "sched_date": at[:10], "scheduled_at": at,
         "estimated_at": None, "actual_at": None, "status": "SCHEDULED"}
    f.update(kw)
    return f


def send(app, etype, f):
    asyncio.run(app.state.on_flight(Envelope(type=etype, aggregate_id=f["id"], payload={"flight": f})))


def view(client, f):
    return client.get(f"/api/v1/acdm/flights/{f['id']}", headers=H).json()


def test_initial_tobt_and_tsat(ctx):
    client, _, app = ctx
    f = flight()
    send(app, "FlightCreated", f)
    v = view(client, f)
    assert v["tobt"].startswith("2026-12-01T10:30") and v["tsat"] == v["tobt"]
    assert v["milestones"]["TOBT"]["source"] == "initial=EOBT"
    assert [d["kind"] for d in client.get(f"/api/v1/acdm/flights/{f['id']}/dpi", headers=H).json()] == ["E-DPI", "T-DPI-s"]


def test_sequencer_spaces_departures(ctx):
    client, _, app = ctx
    a, b, c = flight("PS1"), flight("PS2"), flight("PS3", at="2026-12-01T10:31:00+00:00")
    for f in (a, b, c):
        send(app, "FlightCreated", f)
    tsats = [view(client, f)["tsat"][11:16] for f in (a, b, c)]
    assert tsats == ["10:30", "10:33", "10:36"]


def test_manual_tobt_resequences_and_survives_eobt_change(ctx):
    client, _, app = ctx
    a, b = flight("PS1"), flight("PS2")
    send(app, "FlightCreated", a)
    send(app, "FlightCreated", b)
    r = client.put(f"/api/v1/acdm/flights/{a['id']}/tobt", json={"tobt": "2026-12-01T10:50:00Z"}, headers=H)
    assert r.status_code == 200 and r.json()["tobt_confirmed"]
    assert view(client, b)["tsat"][11:16] == "10:30" and view(client, a)["tsat"][11:16] == "10:50"
    send(app, "FlightUpdated", {**a, "estimated_at": "2026-12-01T10:40:00+00:00"})
    assert view(client, a)["tobt"][11:16] == "10:50"  # підтверджений TOBT не перезаписується EOBT


def test_tobt_validation(ctx):
    client, _, app = ctx
    a, arr = flight(), flight("LO1", "A")
    send(app, "FlightCreated", a)
    send(app, "FlightCreated", arr)
    assert client.put(f"/api/v1/acdm/flights/{a['id']}/tobt", json={"tobt": "2026-12-01T09:00:00Z"}, headers=H).status_code == 422
    assert client.put(f"/api/v1/acdm/flights/{arr['id']}/tobt", json={"tobt": "2026-12-01T11:00:00Z"}, headers=H).status_code == 422
    assert client.put(f"/api/v1/acdm/flights/{a['id']}/tobt", json={"tobt": "2026-12-01T11:00:00Z"}, headers=AOC).status_code == 200


def test_status_milestones_and_arrival(ctx):
    client, _, app = ctx
    a, arr = flight(), flight("LO1", "A", at="2026-12-01T08:00:00+00:00")
    send(app, "FlightCreated", a)
    send(app, "FlightCreated", arr)
    send(app, "FlightStatusChanged", {**a, "status": "BOARDING"})
    send(app, "FlightStatusChanged", {**a, "status": "DEPARTED", "actual_at": "2026-12-01T10:41:00+00:00"})
    m = view(client, a)["milestones"]
    assert {"TOBT", "ASBT", "AOBT"} <= set(m) and m["AOBT"]["value"].startswith("2026-12-01T10:41")
    send(app, "FlightStatusChanged", {**arr, "status": "ARRIVED", "actual_at": "2026-12-01T08:05:00+00:00"})
    assert set(view(client, arr)["milestones"]) == {"ELDT", "AIBT"}
    r = client.post(f"/api/v1/acdm/flights/{arr['id']}/milestones", json={"code": "ALDT", "value": "2026-12-01T07:58:00Z"}, headers=AOC)
    assert r.status_code == 201
    assert client.post(f"/api/v1/acdm/flights/{arr['id']}/milestones", json={"code": "AOBT", "value": "2026-12-01T07:58:00Z"}, headers=AOC).status_code == 422


def test_ready_task_records_ardt(ctx):
    client, _, app = ctx
    a = flight()
    send(app, "FlightCreated", a)
    asyncio.run(app.state.on_task(Envelope(type="TaskCompleted", aggregate_id=a["id"], payload={
        "flight_id": a["id"], "template_code": "READY", "actual_end": "2026-12-01T10:24:00+00:00"})))
    assert view(client, a)["milestones"]["ARDT"]["source"] == "task-service"
