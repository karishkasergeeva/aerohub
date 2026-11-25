"""Тести FIDS Service: побудова моделі читання з подій, тексти табло, WebSocket."""

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aerohub_common.bus import InMemoryBus
from aerohub_common.events import Envelope
from app.display import remark
from app.main import Base, create_app

DB = os.getenv("TEST_FIDS_DB", "postgresql+psycopg://aerohub:aerohub@localhost:55432/fids_db")


@pytest.fixture()
def ctx():
    app = create_app(bus=InMemoryBus(), database_url=DB)
    Base.metadata.drop_all(app.state.engine)
    with TestClient(app) as client:
        yield client, app


def flight(**kw):
    f = {"id": str(uuid.uuid4()), "airline": "PS", "flight_no": "PS101", "direction": "D", "sched_date": "2026-12-01",
         "station": "WAW", "aircraft_type": "738", "scheduled_at": "2026-12-01T10:30:00+00:00", "estimated_at": None,
         "actual_at": None, "status": "SCHEDULED", "terminal": "D", "version": 1}
    f.update(kw)
    return f


def emit(app, etype, agg, payload):
    asyncio.run(app.state.on_event(Envelope(type=etype, aggregate_id=agg, payload=payload)))


def test_remarks():
    assert remark(flight()) == ("За розкладом", "On time")
    assert remark(flight(status="DELAYED", estimated_at="2026-12-01T11:15:00+00:00")) == ("Затримується до 13:15", "Delayed to 13:15")  # 11:15 UTC = 13:15 за Києвом
    assert remark(flight(status="CANCELLED"))[1] == "Cancelled"


def test_board_combines_flight_and_allocations(ctx):
    client, app = ctx
    f = flight()
    emit(app, "FlightCreated", f["id"], {"flight": f})
    emit(app, "ResourceAllocated", f["id"], {"flight_id": f["id"], "flight_no": "PS101", "allocations": [
        {"kind": "GATE", "resource_code": "G03"}, {"kind": "DESK", "resource_code": "D02"}, {"kind": "DESK", "resource_code": "D01"}]})
    rows = client.get("/api/v1/public/flights?direction=D&date=2026-12-01").json()
    assert rows[0]["gate"] == "G03" and rows[0]["desks"] == "D01, D02" and rows[0]["city_uk"] == "Варшава"
    emit(app, "FlightStatusChanged", f["id"], {"flight": {**f, "status": "BOARDING", "version": 2}})
    row = client.get("/api/v1/public/flights/PS101").json()[0]
    assert row["remark_uk"] == "Посадка" and row["gate"] == "G03"


def test_allocation_before_flight_event(ctx):
    client, app = ctx
    f = flight()
    emit(app, "ResourceAllocated", f["id"], {"flight_id": f["id"], "flight_no": "PS101", "allocations": [{"kind": "GATE", "resource_code": "G05"}]})
    assert client.get("/api/v1/public/flights").json() == []  # без даних рейсу на табло не показуємо
    emit(app, "FlightCreated", f["id"], {"flight": f})
    assert client.get("/api/v1/public/flights").json()[0]["gate"] == "G05"


def test_websocket_receives_updates(ctx):
    client, app = ctx
    f = flight()
    with client.websocket_connect("/ws/fids") as ws:
        client.portal.call(app.state.on_event, Envelope(type="FlightCreated", aggregate_id=f["id"], payload={"flight": f}))
        msg = ws.receive_json()
        assert msg["type"] == "fids.update" and msg["flight"]["flight_no"] == "PS101"
        client.portal.call(app.state.on_event, Envelope(type="FlightDeleted", aggregate_id=f["id"], payload={"flight": f}))
        assert ws.receive_json() == {"type": "fids.remove", "id": f["id"]}


def test_realtime_relay_of_acdm_and_task_events(ctx):
    client, app = ctx
    with client.websocket_connect("/ws/fids") as ws:
        client.portal.call(app.state.relay_event, Envelope(type="TsatIssued", aggregate_id="x", payload={"flight_no": "PS101", "tsat": "2026-12-01T10:33:00+00:00"}))
        msg = ws.receive_json()
        assert msg == {"type": "event", "event": "TsatIssued", "payload": {"flight_no": "PS101", "tsat": "2026-12-01T10:33:00+00:00"}}
