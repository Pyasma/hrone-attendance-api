"""Cross-check the analytics endpoints of a running server against a plain-Python oracle.

Usage:  MONGO_DB=attendance_big python scripts/oracle_check.py [base_url]
"""
import calendar
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import httpx
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
api = httpx.Client(base_url=sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000", timeout=60)
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}

emps = {e["emp_code"]: e for e in db.employees.find()}
logs = list(db.attendance_logs.find())


def hu(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), ROUND_HALF_UP))


def weight(r):
    return (0.5 if r.get("half_day") else 1) if r["status"] in PRESENCE else 0


def wd(s):
    return date.fromisoformat(s).weekday() < 5


def month_logs(month):
    return [r for r in logs if r["date"].startswith(month)]


def check(name, got, want):
    if got != want:
        print(f"MISMATCH {name}\n  got  {got}\n  want {want}")
        return 1
    return 0


def monthly(code, month):
    y, m = map(int, month.split("-"))
    m0, m1 = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    rs = [r for r in month_logs(month) if r["emp_code"] == code]
    s = max(m0, date.fromisoformat(emps[code]["joined_on"]))
    working = sum(1 for i in range((m1 - s).days + 1) if (s + timedelta(i)).weekday() < 5) if s <= m1 else 0
    present = sum(weight(r) for r in rs if wd(r["date"]))
    return {"emp_code": code, "month": month, "working_days": working, "present_days": present,
            "leave_days": sum(r["status"] == "LEAVE" for r in rs),
            "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
            "total_late_minutes": sum(r.get("late_minutes") or 0 for r in rs),
            "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in rs),
            "attendance_pct": hu(Decimal(str(present)) * 100 / working, 2) if working else None}


def summary(month, dept=None):
    y, m = map(int, month.split("-"))
    m1 = date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    members = {c: e for c, e in emps.items() if e["joined_on"] <= m1 and (dept is None or e["department"] == dept)}
    out = defaultdict(lambda: {"headcount": 0, "present_days": 0, "wh": [], "late_count": 0,
                               "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0})
    for e in members.values():
        out[e["department"]]["headcount"] += 1
    for r in month_logs(month):
        if r["emp_code"] not in members:
            continue
        o = out[members[r["emp_code"]]["department"]]
        o["present_days"] += weight(r) if wd(r["date"]) else 0
        if r["status"] in PRESENCE and r.get("work_hours") is not None:
            o["wh"].append(Decimal(str(r["work_hours"])))
        o["late_count"] += (r.get("late_minutes") or 0) > 0
        o["total_late_minutes"] += r.get("late_minutes") or 0
        o["leave_count"] += r["status"] == "LEAVE"
        o["on_duty_count"] += r["status"] == "ON_DUTY"
    items = []
    for d in sorted(out):
        o = out[d]
        wh = o.pop("wh")
        items.append({"department": d, **o, "avg_work_hours": hu(sum(wh) / len(wh), 2) if wh else None})
    return {"month": month, "items": items}


def leaderboard(month, limit, dept=None):
    tot, n = defaultdict(int), defaultdict(int)
    for r in month_logs(month):
        e = emps.get(r["emp_code"])
        if e and (r.get("late_minutes") or 0) > 0 and (dept is None or e["department"] == dept):
            tot[r["emp_code"]] += r["late_minutes"]
            n[r["emp_code"]] += 1
    rows = sorted(tot, key=lambda c: (-tot[c], c))
    items = []
    for c in rows:
        rank = 1 + sum(1 for o in tot if tot[o] > tot[c])
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": emps[c]["name"], "department": emps[c]["department"],
                          "total_late_minutes": tot[c], "late_count": n[c]})
    return {"month": month, "items": items}


def trend(dept, d0, d1):
    codes = {c for c, e in emps.items() if e["department"] == dept}
    by_day = defaultdict(list)
    for r in logs:
        if r["emp_code"] in codes and d0.isoformat() <= r["date"] <= d1.isoformat():
            by_day[r["date"]].append(r)
    items, rates = [], []
    for i in range((d1 - d0).days + 1):
        d = d0 + timedelta(i)
        ds = d.isoformat()
        hc = sum(1 for c in codes if emps[c]["joined_on"] <= ds)
        present = sum(weight(r) for r in by_day[ds])
        working = d.weekday() < 5
        rate = hu(Decimal(str(present)) / hc, 4) if working and hc else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        items.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": present,
                      "late_count": sum((r.get("late_minutes") or 0) > 0 for r in by_day[ds]),
                      "attendance_rate": rate,
                      "moving_avg_7d": hu(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None})
    return {"department": dept, "items": items}


bad = 0
for month in ("2025-03", "2026-02", "2026-07", "2026-08"):
    for code in ("EMP0001", "EMP0007", "EMP0010", "EMP0017", "EMP0023", "EMP0034"):
        bad += check(f"monthly {code} {month}",
                     api.get(f"/analytics/employees/{code}/monthly", params={"month": month}).json(),
                     monthly(code, month))
    bad += check(f"summary {month}", api.get("/analytics/departments/summary", params={"month": month}).json(),
                 summary(month))
    bad += check(f"summary {month} Sales", api.get("/analytics/departments/summary",
                                                   params={"month": month, "department": "Sales"}).json(),
                 summary(month, "Sales"))
    for limit, dept in ((10, None), (3, None), (1, "engineering"), (50, "Ops")):
        params = {"month": month, "limit": limit, **({"department": dept} if dept else {})}
        bad += check(f"leaderboard {params}", api.get("/analytics/leaderboard/late", params=params).json(),
                     leaderboard(month, limit, dept))
for dept, d0, d1 in (("Engineering", date(2026, 6, 1), date(2026, 8, 31)), ("Ops", date(2026, 7, 1), date(2026, 7, 31)),
                     ("Sales", date(2024, 1, 1), date(2024, 1, 10))):
    bad += check(f"trend {dept} {d0}", api.get(f"/analytics/departments/{dept}/trend",
                                               params={"from": d0.isoformat(), "to": d1.isoformat()}).json(),
                 trend(dept, d0, d1))
print("all analytics match" if not bad else f"{bad} mismatches")
sys.exit(1 if bad else 0)
