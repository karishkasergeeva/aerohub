"""Тести Resource Service: автоматичний розподіл, конфлікти, ручне призначення, закриття ресурсу."""

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from aerohub_common.auth import issue_token
from aerohub_common.bus import InMemoryBus
from aerohub_common.events import Envelope
from app.main import create_app
from app.models import Base

DB = os.getenv("TEST_RESOURCE_DB", "postgresql+psycopg://aerohub:aerohub@localhost:55432/resource_db")
H = {"Authorization": f"Bearer {issue_token('planner', ['planner'])}"}


@pytest.fixture()
def ctx():
    bus = InMemoryBus()
    app = create_app(bus=bus, database_url=DB)
    Base.metadata.drop_all(app.state.engine)
    with TestClient(app) as client:
        yield client, bus, app


def flight(no="PS101", direction="D", at="2026-12-01T10:30:00+00:00", ac="738", schengen=True, status="SCHEDULED", est=None):
    return {"id": str(uuid.uuid4()), "flight_no": no, "direction": direction, "sched_date": at[:10], "scheduled_at": at,
            "estimated_at": est, "aircraft_type": ac, "schengen": schengen, "status": status}


def send(app, etype, f):
    asyncio.run(app.state.on_flight_event(Envelope(type=etype, aggregate_id=f["id"], payload={"flight": f})))


def allocs(client, f):
    return {a["kind"]: a for a in client.get("/api/v1/allocations?date=2026-12-01", headers=H).json() if a["flight_id"] == f["id"]}


def test_seed_and_auto_allocation(ctx):
    client, bus, app = ctx
    assert len(client.get("/api/v1/resources?type=STAND", headers=H).json()) == 28
    f = flight()
    send(app, "FlightCreated", f)
    a = allocs(client, f)
    assert set(a) == {"STAND", "GATE", "DESK"}
    assert a["STAND"]["resource_code"] == "S01"  # контактна стоянка першою
    assert a["GATE"]["resource_code"] == "G01"   # шенгенський гейт
    asyncio.run(app.state.relay.flush())
    assert any(e.type == "ResourceAllocated" for _, _, e in bus.published)


def test_non_schengen_and_widebody_rules(ctx):
    client, _, app = ctx
    f = flight("TK458", ac="333", schengen=False)
    send(app, "FlightCreated", f)
    a = allocs(client, f)
    assert a["GATE"]["resource_code"] == "G11" and a["STAND"]["resource_code"] == "S21"


def test_duplicate_event_is_ignored(ctx):
    client, _, app = ctx
    f = flight()
    env = Envelope(type="FlightCreated", aggregate_id=f["id"], payload={"flight": f})
    asyncio.run(app.state.on_flight_event(env))
    asyncio.run(app.state.on_flight_event(env))
    assert len([x for x in client.get("/api/v1/allocations", headers=H).json() if x["flight_id"] == f["id"]]) == 4


def test_overlap_moves_to_next_resource_and_conflict_when_full(ctx):
    client, _, app = ctx
    first, second = flight("PS101"), flight("PS103", at="2026-12-01T10:45:00+00:00")
    send(app, "FlightCreated", first)
    send(app, "FlightCreated", second)
    assert allocs(client, second)["STAND"]["resource_code"] == "S02"
    # лише 6 стрічок: сьомий рейс прибуття в той самий час отримує конфлікт
    arrivals = [flight(f"LO{70 + i}", "A", "2026-12-01T12:00:00+00:00") for i in range(7)]
    for f in arrivals:
        send(app, "FlightCreated", f)
    conflicts = client.get("/api/v1/allocations/conflicts?date=2026-12-01", headers=H).json()
    assert [c["kind"] for c in conflicts] == ["BELT"] and conflicts[0]["flight_no"] == "LO76"


def test_delay_shifts_windows_and_keeps_resource(ctx):
    client, _, app = ctx
    f = flight()
    send(app, "FlightCreated", f)
    before = allocs(client, f)["STAND"]
    f2 = {**f, "estimated_at": "2026-12-01T11:30:00+00:00", "status": "DELAYED"}
    send(app, "FlightStatusChanged", f2)
    after = allocs(client, f)["STAND"]
    assert after["resource_code"] == before["resource_code"] and after["start_at"].startswith("2026-12-01T10:30")


def test_cancel_releases(ctx):
    client, _, app = ctx
    f = flight()
    send(app, "FlightCreated", f)
    send(app, "FlightCancelled", {**f, "status": "CANCELLED"})
    assert allocs(client, f) == {}


def test_manual_assignment_and_conflict(ctx):
    client, _, app = ctx
    a, b = flight("PS101"), flight("PS105", at="2026-12-01T10:40:00+00:00")
    send(app, "FlightCreated", a)
    send(app, "FlightCreated", b)
    r = client.put(f"/api/v1/allocations/{b['id']}", json={"kind": "STAND", "resource_codes": ["S01"]}, headers=H)
    assert r.status_code == 409 and "PS101" in r.json()["detail"]
    r = client.put(f"/api/v1/allocations/{b['id']}", json={"kind": "STAND", "resource_codes": ["S07"]}, headers=H)
    assert r.status_code == 200 and r.json()[0]["manual"] is True
    r = client.put(f"/api/v1/allocations/{b['id']}", json={"kind": "GATE", "resource_codes": ["G12"]}, headers=H)
    assert r.status_code == 422  # нешенгенський гейт для шенгенського рейсу


def test_exclusion_constraint_blocks_overlap(ctx):
    _, _, app = ctx
    f = flight()
    send(app, "FlightCreated", f)
    with app.state.engine.connect() as c:
        rid = c.execute(text("select resource_id from allocation where kind='STAND'")).scalar()
        with pytest.raises(Exception):
            c.execute(text("insert into allocation(flight_id, resource_id, kind, start_at, end_at, manual, created_by) "
                           "values (:f, :r, 'STAND', '2026-12-01T10:00Z', '2026-12-01T10:20Z', true, 'x')"),
                      {"f": str(uuid.uuid4()), "r": rid})


def test_resource_closure_reallocates(ctx):
    client, _, app = ctx
    f = flight()
    send(app, "FlightCreated", f)
    s01 = next(r for r in client.get("/api/v1/resources?type=STAND", headers=H).json() if r["code"] == "S01")
    r = client.post(f"/api/v1/resources/{s01['id']}/unavailability",
                    json={"start_at": "2026-12-01T08:00:00Z", "end_at": "2026-12-01T14:00:00Z", "reason": "Ремонт покриття"}, headers=H)
    assert r.status_code == 200 and r.json()["reallocated_flights"] == ["PS101"]
    assert allocs(client, f)["STAND"]["resource_code"] == "S02"


def test_linked_turnaround_shares_one_stand(ctx):
    client, _, app = ctx
    arr = flight("LO751", "A", "2026-12-01T09:20:00+00:00")
    dep = flight("LO752", "D", "2026-12-01T10:30:00+00:00")
    send(app, "FlightCreated", arr)
    send(app, "FlightCreated", dep)
    send(app, "FlightUpdated", {**arr, "linked_flight_id": dep["id"]})
    send(app, "FlightUpdated", {**dep, "linked_flight_id": arr["id"]})
    a, d = allocs(client, arr)["STAND"], allocs(client, dep)["STAND"]
    assert a["resource_code"] == d["resource_code"]
    assert a["end_at"] == d["start_at"] and a["start_at"].startswith("2026-12-01T09:10") and d["end_at"].startswith("2026-12-01T10:40")
    r = client.put(f"/api/v1/allocations/{dep['id']}", json={"kind": "STAND", "resource_codes": ["S05"]}, headers=H)
    assert r.status_code == 200
    assert allocs(client, arr)["STAND"]["resource_code"] == "S05"
