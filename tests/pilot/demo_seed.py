"""Наповнення демонстраційними даними для показу та знімків екрана."""
import time
from datetime import date

import httpx

c = httpx.Client(base_url="http://localhost:8080", timeout=60)
tok = lambda u: {"Authorization": "Bearer " + c.post("/api/v1/auth/token", json={"username": u, "password": u}).json()["access_token"]}  # noqa: E731
adm, hnd = tok("admin"), tok("handler")
print(c.post("/api/v1/schedules/imports", headers=adm, files={"file": ("demo.ssim", open("docs/demo_schedule.ssim", encoding="utf-8").read())}).json())
time.sleep(6)
day = date.today().isoformat()
fl = {f["flight_no"]: f for f in c.get(f"/api/v1/flights?date={day}", headers=adm).json()}


def status(no, st, **kw):
    f = c.get(f"/api/v1/flights/{fl[no]['id']}", headers=adm).json()
    r = c.post(f"/api/v1/flights/{f['id']}/status", headers=adm, json={"status": st, "version": f["version"], **kw})
    print(no, st, r.status_code)


for a, d in [("TK458", "TK459"), ("LH1490", "LH1491"), ("BT403", "PS211")]:
    print("link", a, d, c.post(f"/api/v1/flights/{fl[a]['id']}/link", headers=adm, json={"other_flight_id": fl[d]["id"]}).status_code)
status("LO752", "ARRIVED", at=f"{day}T07:16:00Z")
status("PS101", "CHECK_IN"); status("PS101", "BOARDING"); status("PS101", "DEPARTED", at=f"{day}T07:41:00Z")
status("PS731", "CHECK_IN"); status("PS731", "BOARDING")
status("LH1491", "DELAYED", reason="93", at=f"{day}T13:20:00Z")
status("OS661", "DELAYED", reason="81", at=f"{day}T12:55:00Z")
status("PS885", "CHECK_IN")
f = c.get(f"/api/v1/flights/{fl['PS5']['id']}", headers=adm).json()
print("patch", c.patch(f"/api/v1/flights/{f['id']}", headers=adm, json={"version": f["version"], "aircraft_type": "73H", "reason": "Заміна ПС авіакомпанією"}).status_code)
print("tobt", c.put(f"/api/v1/acdm/flights/{fl['PS885']['id']}/tobt", headers=hnd, json={"tobt": f"{day}T13:45:00Z"}).status_code)
time.sleep(4)
for t in c.get(f"/api/v1/tasks?flight_id={fl['PS731']['id']}", headers=hnd).json():
    if t["template_code"] in ("CLEAN", "CATER", "FUEL"):
        c.post(f"/api/v1/tasks/{t['id']}/start", headers=hnd, json={"version": t["version"]})
        t = next(x for x in c.get(f"/api/v1/tasks?flight_id={fl['PS731']['id']}", headers=hnd).json() if x["id"] == t["id"])
        if t["template_code"] != "FUEL":
            c.post(f"/api/v1/tasks/{t['id']}/complete", headers=hnd, json={"version": t["version"]})
s01 = next(r for r in c.get("/api/v1/resources?type=STAND", headers=adm).json() if r["code"] == "S03")
print("close", c.post(f"/api/v1/resources/{s01['id']}/unavailability", headers=adm, json={"start_at": f"{day}T15:00:00Z", "end_at": f"{day}T18:00:00Z", "reason": "Ремонт покриття"}).json())
