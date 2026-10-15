"""Generate ~100k attendance logs with awkward cases for local testing.

Usage:  MONGO_DB=attendance_big python scripts/gen_data.py
Drops and refills `employees` and `attendance_logs` in MONGO_DB. Never point it at real data.
"""
import os
import random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
db = MongoClient(os.environ["MONGO_URI"], tz_aware=True)[os.environ["MONGO_DB"]]
rng = random.Random(42)
IST = timezone(timedelta(hours=5, minutes=30))

START, END = date(2025, 3, 1), date(2026, 7, 31)
DEPTS = ["Engineering", "engineering", "Sales", "Support", "Ops"]


def employee(i: int) -> dict:
    overnight = i % 10 == 0
    joined = START - timedelta(days=30)
    if i % 17 == 0:
        joined = date(2026, 7, rng.randint(2, 28))  # mid-month joiner in the last month
    if i % 23 == 0:
        joined = date(2026, 8, 3)  # joins after the data ends
    return {
        "emp_code": f"EMP{i:04d}",
        "name": f"Person {i}",
        "email": f"p{i}@example.com",
        "department": DEPTS[i % len(DEPTS)],
        "shift_start": "22:00" if overnight else rng.choice(["09:00", "09:30", "10:00"]),
        "shift_end": "06:00" if overnight else "18:30",
        "joined_on": joined.isoformat(),
        "created_at": datetime.combine(joined, datetime.min.time(), timezone.utc),
    }


def log(emp: dict, d: date) -> dict:
    weekend = d.weekday() >= 5
    status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [70, 12, 5, 6, 7])[0]
    doc = {"emp_code": emp["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None,
           "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
           "half_day": False, "history": []}
    if status in ("PRESENT", "WFH", "ON_DUTY"):
        h, m = map(int, emp["shift_start"].split(":"))
        start = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
        punch_in = start + timedelta(seconds=rng.choice([-600, -60, 0, 300, 600, 601, 900, 1800, 3600]))
        late = rng.choice([0, 0, 0, 0, 10, 15, 30, 45, 60]) if not weekend or rng.random() < 0.5 else 0
        doc.update(punch_in=punch_in.astimezone(timezone.utc), late_minutes=late)
        if rng.random() > 0.03:  # some records stay open
            hours = rng.choice([3.5, 4.49, 4.5, 8.0, 9.0, 9.12, 10.25])
            doc.update(punch_out=(punch_in + timedelta(hours=hours)).astimezone(timezone.utc), work_hours=hours,
                       half_day=hours < 4.5, overtime_minutes=rng.choice([0, 0, 0, 30, 45, 90]))
    if rng.random() < 0.05:  # legacy shape
        del doc["half_day"], doc["history"]
    return doc


def main():
    emps = [employee(i) for i in range(1, 311)]
    docs = []
    for e in emps:
        if e["emp_code"] in ("EMP0007", "EMP0008"):
            continue  # employees with zero logs
        d = max(START, date.fromisoformat(e["joined_on"]))
        while d <= END:
            if (d.weekday() < 5 and rng.random() < 0.95) or (d.weekday() >= 5 and rng.random() < 0.08):
                docs.append(log(e, d))
            d += timedelta(days=1)
    for i in range(50):  # logs for employees that do not exist
        docs.append(log({"emp_code": f"EMP9{i:03d}", "shift_start": "09:30"}, date(2026, 7, 1 + i % 28)))
    db.employees.drop()
    db.attendance_logs.drop()
    db.employees.insert_many(emps)
    db.attendance_logs.insert_many(docs)
    print(f"{len(emps)} employees, {len(docs)} logs into {db.name}")


if __name__ == "__main__":
    main()
