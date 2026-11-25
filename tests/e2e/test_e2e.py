"""Наскрізний тест на піднятому docker compose (через API Gateway і справжню Kafka).

Запуск:  docker compose up -d --build && pytest tests/e2e -q
Адреса шлюзу: AEROHUB_URL (за замовчуванням http://localhost:8080).
"""

import asyncio
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import websockets

BASE = os.getenv("AEROHUB_URL", "http://localhost:8080")
pytestmark = pytest.mark.skipif(os.getenv("AEROHUB_E2E") != "1", reason="Потрібен запущений docker compose (AEROHUB_E2E=1)")


def login(c: httpx.Client, user: str) -> dict:
    r = c.post("/api/v1/auth/token", json={"username": user, "password": user})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def wait_for(fn, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        result = fn()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError("Умова не виконалась за відведений час")


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=10) as c:
        yield c


def test_full_flow(client):
    planner, agent = login(client, "planner"), login(client, "agent")
    day = (date.today() + timedelta(days=30)).isoformat()
    number = str(random.randint(1000, 9999))
    r = client.post("/api/v1/flights", headers=planner, json={
        "airline": "PS", "number": number, "direction": "D", "station": "WAW", "aircraft_type": "738",
        "scheduled_at": f"{day}T10:30:00Z", "terminal": "D", "schengen": True})
    assert r.status_code == 201, r.text
    flight = r.json()

    # Resource Service отримав подію з Kafka і призначив стоянку, гейт і стійки
    def allocated():
        rows = client.get(f"/api/v1/allocations?date={day}", headers=planner).json()
        return {a["kind"] for a in rows if a["flight_id"] == flight["id"]} == {"STAND", "GATE", "DESK"}
    wait_for(allocated)

    # FIDS Service зібрав рядок табло з подій обох сервісів
    row = wait_for(lambda: next((x for x in client.get(f"/api/v1/public/flights/PS{number}").json() if x.get("gate")), None))
    assert row["remark_en"] == "On time" and row["city_en"] == "Warsaw"

    async def delay_and_listen():
        uri = BASE.replace("http", "ws") + "/ws/fids"
        async with websockets.connect(uri) as ws:
            r = await asyncio.to_thread(client.post, f"/api/v1/flights/{flight['id']}/status", headers=planner,
                                        json={"status": "DELAYED", "reason": "93", "at": f"{day}T11:20:00Z"})
            assert r.status_code == 200, r.text
            deadline = time.time() + 15
            while time.time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                if msg.get("type") == "fids.update" and msg["flight"]["id"] == flight["id"] and msg["flight"]["status"] == "DELAYED":
                    return msg["flight"]
        raise AssertionError("Оновлення табло не надійшло")

    pushed = asyncio.run(delay_and_listen())
    assert pushed["remark_uk"] == "Затримується до 13:20"  # 11:20 UTC за київським часом (зима, UTC+2)

    # вікна ресурсів зсунулися відповідно до нового часу
    def shifted():
        rows = client.get(f"/api/v1/allocations?date={day}", headers=planner).json()
        stand = next((a for a in rows if a["flight_id"] == flight["id"] and a["kind"] == "STAND"), None)
        return stand and stand["start_at"].startswith(f"{day}T10:20")
    wait_for(shifted)

    for st in ("CHECK_IN", "BOARDING", "DEPARTED"):
        assert client.post(f"/api/v1/flights/{flight['id']}/status", headers=agent, json={"status": st}).status_code == 200
    history = client.get(f"/api/v1/flights/{flight['id']}/history", headers=agent).json()
    assert [h["event_type"] for h in history][-1] == "FlightStatusChanged" and len(history) == 5
    assert client.delete(f"/api/v1/flights/{flight['id']}", headers=planner).status_code == 409


def test_ssim_import_through_gateway(client):
    planner = login(client, "planner")
    text = open(os.path.join(os.path.dirname(__file__), "..", "..", "docs", "demo_schedule.ssim"), encoding="utf-8").read()
    r = client.post("/api/v1/schedules/imports", headers=planner, files={"file": ("demo.ssim", text)})
    assert r.status_code == 200 and (r.json()["created"] + r.json()["skipped"] + r.json()["updated"]) > 0


def test_acdm_and_ground_tasks(client):
    planner, handler = login(client, "planner"), login(client, "handler")
    day = (date.today() + timedelta(days=31)).isoformat()
    number = str(random.randint(1000, 9999))
    flight = client.post("/api/v1/flights", headers=planner, json={
        "airline": "PS", "number": number, "direction": "D", "station": "VIE", "aircraft_type": "73H",
        "scheduled_at": f"{day}T14:00:00Z", "schengen": True}).json()

    view = wait_for(lambda: (r := client.get(f"/api/v1/acdm/flights/{flight['id']}", headers=handler)).status_code == 200 and r.json())
    assert view["tobt"].startswith(f"{day}T14:00") and view["tsat"]

    tasks = wait_for(lambda: {t["template_code"]: t for t in client.get(f"/api/v1/tasks?flight_id={flight['id']}", headers=handler).json()} or None)
    assert len(tasks) == 6

    # хендлер підтверджує TOBT пізніше - завдання зсуваються
    r = client.put(f"/api/v1/acdm/flights/{flight['id']}/tobt", headers=handler, json={"tobt": f"{day}T14:20:00Z"})
    assert r.status_code == 200
    wait_for(lambda: client.get(f"/api/v1/tasks?flight_id={flight['id']}", headers=handler).json()[-1]["planned_start"].startswith(f"{day}T14:20"))

    ready = next(t for t in client.get(f"/api/v1/tasks?flight_id={flight['id']}", headers=handler).json() if t["template_code"] == "READY")
    r = client.post(f"/api/v1/tasks/{ready['id']}/complete", headers=handler, json={"version": ready["version"]})
    assert r.status_code == 200
    wait_for(lambda: "ARDT" in client.get(f"/api/v1/acdm/flights/{flight['id']}", headers=handler).json()["milestones"])
