"""Сценарій дослідної експлуатації: завантаження розкладу, паралельна робота операторів,
перевірка узгодженості даних між сервісами. Запуск: python tests/pilot/pilot.py"""
import concurrent.futures as cf
import os
import random
import sys
import time
from datetime import date

import httpx

BASE = os.getenv("AEROHUB_URL", "http://localhost:8080")


def token(c, user):
    r = c.post("/api/v1/auth/token", json={"username": user, "password": user})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def main():
    c = httpx.Client(base_url=BASE, timeout=60)
    h = token(c, "admin")
    path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "demo_schedule.ssim")
    t0 = time.time()
    r = c.post("/api/v1/schedules/imports", headers=h, files={"file": ("demo.ssim", open(path, encoding="utf-8").read())})
    print("import", r.status_code, r.json() if r.status_code == 200 else r.text[:200], f"{time.time() - t0:.1f}s")
    day = date.today().isoformat()
    flights = c.get(f"/api/v1/flights?date={day}", headers=h).json()
    print("flights today", len(flights))

    # 20 паралельних операторів змінюють статуси й затримки
    def op(i):
        f = random.choice(flights)
        cur = c.get(f"/api/v1/flights/{f['id']}", headers=h).json()
        nxt = {"D": {"SCHEDULED": "CHECK_IN", "CHECK_IN": "BOARDING"}, "A": {"SCHEDULED": "DELAYED"}}[cur["direction"]].get(cur["status"])
        if not nxt:
            return "skip"
        body = {"status": nxt, "version": cur["version"], "reason": "93" if nxt == "DELAYED" else None,
                "at": cur["scheduled_at"].replace("+00:00", "Z") if nxt == "DELAYED" else None}
        return c.post(f"/api/v1/flights/{f['id']}/status", headers=h, json=body).status_code

    t0 = time.time()
    with cf.ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(op, range(200)))
    print("ops", {k: codes.count(k) for k in set(codes)}, f"{time.time() - t0:.1f}s")

    # логін 20 операторів зміни з однієї IP-адреси (термінали за NAT)
    logins = [c.post("/api/v1/auth/token", json={"username": "dispatcher", "password": "dispatcher"}).status_code for _ in range(20)]
    print("shift logins", {k: logins.count(k) for k in set(logins)})

    time.sleep(5)
    fids = c.get(f"/api/v1/public/flights?date={day}").json()
    acdm = c.get(f"/api/v1/acdm/flights?date={day}", headers=h).json()
    tasks = c.get(f"/api/v1/tasks?date={day}", headers=h).json()
    allocs = c.get(f"/api/v1/allocations?date={day}", headers=h).json()
    fl = {f["id"]: f for f in c.get(f"/api/v1/flights?date={day}", headers=h).json()}
    mism = [f for f in fids if fl.get(f["id"], {}).get("status") != f["status"]]
    print("consistency: fids", len(fids), "acdm", len(acdm), "tasks", len(tasks), "allocations", len(allocs), "status mismatches", len(mism))
    linked = c.get(f"/api/v1/flights?date={day}", headers=h).json()
    return 0


if __name__ == "__main__":
    sys.exit(main())
