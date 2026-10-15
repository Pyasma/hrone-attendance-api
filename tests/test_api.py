from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app import main
from app.main import IST


def ist(d: str, hms: str) -> int:
    return int(datetime.fromisoformat(f"{d}T{hms}").replace(tzinfo=IST).timestamp()) * 1000


def emp(code="EMP0001", dept="Engineering", start="09:30", end="18:30", joined="2026-01-01", name=None):
    return {"emp_code": code, "name": name or code, "email": f"{code.lower()}@x.com", "department": dept,
            "shift_start": start, "shift_end": end, "joined_on": joined}


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def assert_clean(body):
    for k, v in walk(body):
        assert k not in ("_id", "id")
        if isinstance(v, int) and v >= 100_000_000_000:
            assert v % 1000 == 0


def parallel(fn, n=20):
    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(lambda _: fn(), range(n)))


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_employee_validation_and_duplicates(client):
    for bad in [emp(code="x"), {**emp(), "shift_start": "25:99"}, {**emp(), "email": "nope"},
                {**emp(), "shift_end": "09:30"}, {**emp(), "joined_on": "2026-02-30"}, {**emp(), "name": ""}]:
        assert client.post("/employees", json=bad).status_code == 422
    codes = [r.status_code for r in parallel(lambda: client.post("/employees", json=emp()))]
    assert sorted(codes) == [201] + [409] * 19
    r = client.get("/employees").json()
    assert r["total"] == 1 and isinstance(r["items"][0]["created_at"], int)
    assert_clean(r)


def test_list_employees_pagination(client):
    for i in range(1, 26):
        client.post("/employees", json=emp(code=f"EMP{i:04d}", dept="A" if i % 2 else "B"))
    r = client.get("/employees", params={"page": 1, "page_size": 10}).json()
    assert [e["emp_code"] for e in r["items"]][:2] == ["EMP0001", "EMP0002"] and r["total"] == 25
    assert client.get("/employees", params={"department": "A"}).json()["total"] == 13
    assert client.get("/employees", params={"page": 3, "page_size": 10}).json()["items"][-1]["emp_code"] == "EMP0025"
    for p in [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}]:
        assert client.get("/employees", params=p).status_code == 422


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, 4102444800001, None])
def test_bad_timestamps(client, value):
    client.post("/employees", json=emp())
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


def test_punch_in_rules_and_race(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    client.post("/employees", json=emp())
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    t = ist("2026-07-06", "09:45:00") + 900
    rs = parallel(lambda: client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t}))
    assert sorted(r.status_code for r in rs) == [201] + [409] * 19
    rec = next(r.json() for r in rs if r.status_code == 201)
    assert rec == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "punch_in": t - 900,
                   "punch_out": None, "work_hours": None, "late_minutes": 15, "overtime_minutes": 0,
                   "half_day": False, "history": []}


def test_punch_out_overnight_and_race(client):
    client.post("/employees", json=emp(start="22:00", end="06:00"))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-06", "21:55:00")})
    out = ist("2026-07-07", "06:40:00")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                      "punched_at": ist("2026-07-06", "21:55:00")}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001",
                                                      "punched_at": ist("2026-07-07", "22:00:00")}).status_code == 422
    rs = parallel(lambda: client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": out}))
    assert sorted(r.status_code for r in rs) == [200] + [409] * 19
    rec = next(r.json() for r in rs if r.status_code == 200)
    assert (rec["date"], rec["work_hours"], rec["overtime_minutes"], rec["half_day"]) == ("2026-07-06", 8.75, 40, False)
    assert rec["history"] == []


def test_regularize(client):
    client.post("/employees", json=emp())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-07", "10:05:00")})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-07", "18:30:00")})
    url = "/attendance/EMP0001/2026-07-07"
    body = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    assert client.patch("/attendance/EMP0001/2026-07-08", json={**body, "status": "WFH"}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-07-07", json={**body, "status": "WFH"}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-13-07", json={**body, "status": "WFH"}).status_code == 422
    assert client.patch(url, json=body).status_code == 422
    assert client.patch(url, json={**body, "status": "PRESENT"}).status_code == 422
    assert client.patch(url, json={**body, "status": "LEAVE", "punch_in": ist("2026-07-07", "09:00:00")}).status_code == 422
    assert client.patch(url, json={**body, "punch_in": ist("2026-07-08", "09:00:00")}).status_code == 422
    assert client.patch(url, json={**body, "punch_out": ist("2026-07-07", "09:00:00")}).status_code == 422
    assert client.patch(url, json={**body, "punch_out": None}).status_code == 422
    assert client.patch(url, json={**body, "reason": "abc", "status": "WFH"}).status_code == 422

    r = client.patch(url, json={**body, "punch_in": ist("2026-07-07", "09:28:00"), "work_hours": 1})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert_clean(rec)
    changes = rec["history"][0]["changes"]
    assert changes == {
        "punch_in": {"from": ist("2026-07-07", "10:05:00"), "to": ist("2026-07-07", "09:28:00")},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }
    r = client.patch(url, json={**body, "status": "LEAVE"}).json()
    assert (r["punch_in"], r["work_hours"], r["late_minutes"], len(r["history"])) == (None, None, 0, 2)
    assert client.patch(url, json={**body, "status": "PRESENT"}).status_code == 422


def test_regularize_race_keeps_all_history(client):
    client.post("/employees", json=emp())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist("2026-07-07", "09:00:00")})
    statuses = iter(["WFH", "ON_DUTY"] * 10)
    bodies = [{"reason": "fix it", "regularized_by": "hr", "status": next(statuses)} for _ in range(20)]
    with ThreadPoolExecutor(20) as ex:
        rs = list(ex.map(lambda b: client.patch("/attendance/EMP0001/2026-07-07", json=b), bodies))
    ok = sum(r.status_code == 200 for r in rs)
    assert ok >= 1 and all(r.status_code in (200, 409, 422) for r in rs)
    rec = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert len(rec["history"]) == ok


def test_list_attendance(client):
    for c in ("EMP0001", "EMP0002"):
        client.post("/employees", json=emp(code=c))
        for d in ("2026-07-06", "2026-07-07"):
            client.post("/attendance/punch-in", json={"emp_code": c, "punched_at": ist(d, "09:00:00")})
    r = client.get("/attendance").json()
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert client.get("/attendance", params={"date_from": "2026-07-07"}).json()["total"] == 2
    for p in [{"date_from": "2026-08-01", "date_to": "2026-07-01"}, {"status": "NOPE"}, {"date_to": "07/07/2026"}]:
        assert client.get("/attendance", params=p).status_code == 422


def seed_analytics():
    db = main.db
    db.employees.insert_many([
        {**emp("EMP0001", "Eng", name="A"), "created_at": datetime.now(IST)},
        {**emp("EMP0002", "Eng", name="B", joined="2026-07-15"), "created_at": datetime.now(IST)},  # mid-month
        {**emp("EMP0003", "Eng", name="C", joined="2026-08-02"), "created_at": datetime.now(IST)},  # after month
        {**emp("EMP0004", "Ops", name="D"), "created_at": datetime.now(IST)},  # no logs
        {**emp("EMP0005", "Ops", name="E"), "created_at": datetime.now(IST)},
        {**emp("EMP0006", "Ops", name="F"), "created_at": datetime.now(IST)},
    ])

    def rec(code, d, status="PRESENT", late=0, ot=0, wh=9.0, half=False, legacy=False):
        doc = {"emp_code": code, "date": d, "status": status, "punch_in": None, "punch_out": None,
               "work_hours": wh if status in main.PRESENCE else None, "late_minutes": late,
               "overtime_minutes": ot, "half_day": half, "history": []}
        if legacy:
            del doc["half_day"], doc["history"]
        return doc

    db.attendance_logs.insert_many([
        rec("EMP0001", "2026-07-01", late=20),                      # Wed
        rec("EMP0001", "2026-07-02", wh=3.5, half=True),            # Thu, half day
        rec("EMP0001", "2026-07-04", late=15, ot=45),               # Sat: late/ot count, not present
        rec("EMP0001", "2026-07-06", status="LEAVE"),
        rec("EMP0001", "2026-07-07", status="ON_DUTY", legacy=True),
        rec("EMP0002", "2026-07-15", late=35, wh=8.005),
        rec("EMP0005", "2026-07-01", late=35),
        rec("EMP0006", "2026-07-01", late=50),
        rec("EMP0004", "2026-06-30", late=500),                     # other month
        rec("EMP9999", "2026-07-01", late=999),                     # unknown employee
    ])


def test_employee_monthly(client):
    seed_analytics()
    r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert r == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 2.5,
                 "leave_days": 1, "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 45,
                 "attendance_pct": 10.87}
    r = client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["present_days"], r["attendance_pct"]) == (13, 1, 7.69)
    r = client.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert (r["working_days"], r["attendance_pct"], r["present_days"]) == (0, None, 0)
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422


def test_department_summary(client):
    seed_analytics()
    r = client.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    eng, ops = r["items"]
    assert eng == {"department": "Eng", "headcount": 2, "present_days": 3.5, "avg_work_hours": 7.7,
                   "late_count": 3, "total_late_minutes": 70, "leave_count": 1, "on_duty_count": 1}
    assert ops["headcount"] == 3 and ops["present_days"] == 2 and ops["total_late_minutes"] == 85
    r = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Ops"}).json()
    assert [i["department"] for i in r["items"]] == ["Ops"]


def test_leaderboard_ties(client):
    seed_analytics()
    main.db.attendance_logs.insert_many([
        {"emp_code": "EMP0004", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 35},
    ])
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in r] == [
        (1, "EMP0006", 50), (2, "EMP0001", 35), (2, "EMP0002", 35), (2, "EMP0004", 35),
        (2, "EMP0005", 35)]
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1, "department": "Eng"}).json()
    assert [(i["rank"], i["emp_code"]) for i in r["items"]] == [(1, "EMP0001"), (1, "EMP0002")]
    assert r["items"][0]["name"] == "A" and r["items"][0]["late_count"] == 2
    for lim in (0, 51):
        assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": lim}).status_code == 422


def test_trend(client):
    seed_analytics()
    r = client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-16"}).json()
    items = r["items"]
    assert len(items) == 16 and items[0]["date"] == "2026-07-01" and items[-1]["date"] == "2026-07-16"
    d = {i["date"]: i for i in items}
    assert d["2026-07-01"] == {"date": "2026-07-01", "is_working_day": True, "headcount": 1, "present_count": 1,
                               "late_count": 1, "attendance_rate": 1.0, "moving_avg_7d": 1.0}
    assert d["2026-07-02"]["attendance_rate"] == 0.5 and d["2026-07-02"]["moving_avg_7d"] == 0.75
    assert d["2026-07-04"]["attendance_rate"] is None and d["2026-07-04"]["present_count"] == 1
    assert d["2026-07-15"]["headcount"] == 2 and d["2026-07-15"]["attendance_rate"] == 0.5
    assert d["2026-07-14"]["headcount"] == 1
    empty = client.get("/analytics/departments/Eng/trend", params={"from": "2025-01-01", "to": "2025-01-03"}).json()
    assert [(i["headcount"], i["attendance_rate"], i["moving_avg_7d"]) for i in empty["items"]] == [(0, None, None)] * 3
    assert client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    ok92 = client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"})
    assert ok92.status_code == 200 and len(ok92.json()["items"]) == 92
    for p in [{"from": "2026-07-01", "to": "2026-10-01"}, {"from": "2026-07-02", "to": "2026-07-01"}, {"from": "2026-07-01"}]:
        assert client.get("/analytics/departments/Eng/trend", params=p).status_code == 422


@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001"}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Eng"}),
    ("late_leaderboard", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Eng"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_uses_indexes(client, endpoint, params):
    seed_analytics()
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    plan = str(r.json()["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


def test_explain_validation(client):
    assert client.get("/admin/explain/nope").status_code == 422
    assert client.get("/admin/explain/employee_monthly", params={"month": "2026-07"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": "Eng"}).status_code == 422
