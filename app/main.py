"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)
"""
import calendar
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority over .env

log = logging.getLogger("attendance")

client = MongoClient(os.environ["MONGO_URI"], tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[os.environ["MONGO_DB"]]
employees = db.employees
logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE = ("ABSENT", "LEAVE")
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
INDEXES = {
    employees: [
        ([("emp_code", ASCENDING)], {"unique": True}),
        ([("department", ASCENDING), ("emp_code", ASCENDING)], {}),
        ([("joined_on", ASCENDING)], {}),
    ],
    logs: [
        ([("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True}),
        ([("date", DESCENDING), ("emp_code", ASCENDING)], {}),
        ([("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
        ([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {}),
    ],
}
indexes_ready = threading.Event()


def ensure_indexes_until_done() -> None:
    while not indexes_ready.is_set():
        try:
            for coll, specs in INDEXES.items():
                for keys, opts in specs:
                    coll.create_index(keys, **opts)
            indexes_ready.set()
            log.info("indexes ready")
        except PyMongoError as exc:
            log.error("index creation failed, retrying: %s", exc)
            indexes_ready.wait(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # A background thread keeps startup fast and survives Mongo being briefly unreachable;
    # /health reports 503 until the indexes (which back the race guarantees) exist.
    threading.Thread(target=ensure_indexes_until_done, daemon=True).start()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
def db_unavailable(_: Request, exc: ConnectionFailure):
    log.error("database unavailable: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Conversions and rounding
# --------------------------------------------------------------------------- #
def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, timezone.utc)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def half_up(value, places: int) -> float:
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def count_weekdays(start: date, end: date) -> int:
    return sum(1 for i in range((end - start).days + 1) if (start + timedelta(days=i)).weekday() < 5)


def validation_error(loc: tuple, msg: str, value=None):
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def parse_hhmm(s: str) -> time:
    return time.fromisoformat(s)


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST date of the punch; an overnight-shift punch before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(emp) and local.time() < parse_hhmm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_bounds(day: date, emp: dict) -> tuple[datetime, datetime]:
    start = datetime.combine(day, parse_hhmm(emp["shift_start"]), IST)
    end = datetime.combine(day, parse_hhmm(emp["shift_end"]), IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: date, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; floor minutes since shift start."""
    start, _ = shift_bounds(day, emp)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: hours rounded to 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, day: date, emp: dict) -> int:
    """R3: floor minutes after shift end, only when >= 30."""
    _, end = shift_bounds(day, emp)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_derived(status: str, punch_in, punch_out, day: date, emp: dict) -> dict:
    if status in NON_PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    derived = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        derived["work_hours"] = float(hours)
        derived["overtime_minutes"] = compute_overtime(punch_out, day, emp)
        derived["half_day"] = hours < HALF_DAY_HOURS  # R5 compares the rounded value
    return derived


def check_punch_out(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise validation_error(("body", "punch_out"), "punch_out must be after punch_in")
    if punch_out - punch_in > MAX_SHIFT:
        raise validation_error(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# Serializers (the only way documents leave the API: no _id, instants as ms)
# --------------------------------------------------------------------------- #
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _change_value(v):
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_history(entry: dict) -> dict:
    return {
        "at": to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {
            field: {"from": _change_value(c.get("from")), "to": _change_value(c.get("to"))}
            for field, c in entry.get("changes", {}).items()
        },
    }


def normalized_state(doc: dict) -> dict:
    """The seven tracked fields with legacy gaps filled (missing means null / 0 / false)."""
    return {
        "status": doc["status"],
        "punch_in": doc.get("punch_in"),
        "punch_out": doc.get("punch_out"),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def serialize_record(doc: dict) -> dict:
    state = normalized_state(doc)
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        **state,
        "punch_in": to_ms(state["punch_in"]),
        "punch_out": to_ms(state["punch_out"]),
        "history": [serialize_history(h) for h in doc.get("history") or []],
    }


# --------------------------------------------------------------------------- #
# Input types
# --------------------------------------------------------------------------- #
def _real_date(v: str) -> str:
    date.fromisoformat(v)
    return v


EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
DateStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_real_date)]
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


def _reject_null(v):
    if v is None:
        raise ValueError("may be omitted but not null")
    return v


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def shift_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"

    _no_null = field_validator("punched_at", mode="before")(_reject_null)


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None

    _no_null = field_validator("punched_at", mode="before")(_reject_null)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]

    _no_null = field_validator("status", "punch_in", "punch_out", mode="before")(_reject_null)


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Query and pipeline builders, shared by the endpoints and /admin/explain
# --------------------------------------------------------------------------- #
ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(("query", "date_from"), "date_from must be on or before date_to", date_from)
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def presence_weight(weekdays_only: bool) -> dict:
    """1 per presence record, 0.5 for a half day (R5, R6); optionally Mon-Fri only (R7)."""
    cond = {"$in": ["$status", list(PRESENCE)]}
    if weekdays_only:
        weekday = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
        cond = {"$and": [cond, weekday]}
    return {"$cond": [cond, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def count_if(cond: dict) -> dict:
    return {"$sum": {"$cond": [cond, 1, 0]}}


IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    m0, m1 = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m0.isoformat(), "$lte": m1.isoformat()}}},
        {"$group": {
            "_id": None,
            "present": {"$sum": presence_weight(weekdays_only=True)},
            "leave": count_if({"$eq": ["$status", "LEAVE"]}),
            "late_n": count_if(IS_LATE),
            "late_m": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "ot_m": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    m0, m1 = month_bounds(month)
    match = {"joined_on": {"$lte": m1.isoformat()}}  # R9
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        # Start from employees and left-join the month's logs so people with no logs still count.
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": m0.isoformat(), "$lte": m1.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present": {"$sum": presence_weight(weekdays_only=True)},
                    "wh_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "wh_n": count_if(has_hours),
                    "late_n": count_if(IS_LATE),
                    "late_m": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "leave_n": count_if({"$eq": ["$status", "LEAVE"]}),
                    "on_duty_n": count_if({"$eq": ["$status", "ON_DUTY"]}),
                }},
            ],
            "as": "s",
        }},
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present": {"$sum": "$s.present"},
            "wh_sum": {"$sum": "$s.wh_sum"},
            "wh_n": {"$sum": "$s.wh_n"},
            "late_n": {"$sum": "$s.late_n"},
            "late_m": {"$sum": "$s.late_m"},
            "leave_n": {"$sum": "$s.leave_n"},
            "on_duty_n": {"$sum": "$s.on_duty_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


def department_codes(department: str) -> list:
    return employees.distinct("emp_code", {"department": department})


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    m0, m1 = month_bounds(month)
    match = {"date": {"$gte": m0.isoformat(), "$lte": m1.isoformat()}, "late_minutes": {"$gt": 0}}
    if department is not None:
        match["emp_code"] = {"$in": department_codes(department)}
    return [
        {"$match": match},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "n": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # drops logs whose employee does not exist
        # Competition ranking (1, 2, 2, 4); limit is applied to the rank, so ties at the cutoff all stay.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$e.name",
            "department": "$e.department",
            "total_late_minutes": "$total",
            "late_count": "$n",
        }},
    ]


def _half_up_4(expr) -> dict:
    """Half-up to 4 decimals for non-negative values ($round is half-to-even)."""
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, 10000]}, Decimal128("0.5")]}}, 10000]}


def build_department_trend_pipeline(department: str, codes: list, start: date, end: date) -> list:
    start_dt = datetime.combine(start, time(), timezone.utc)
    end_excl = datetime.combine(end + timedelta(days=1), time(), timezone.utc)
    day_str = {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}
    is_working_day = {"$lte": [{"$isoDayOfWeek": "$d"}, 5]}
    rate = _half_up_4({"$divide": [{"$toDecimal": "$present"}, {"$toDecimal": "$headcount"}]})
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$date", "present": {"$sum": presence_weight(weekdays_only=False)}, "late": count_if(IS_LATE)}},
        {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": "$_id"}}, "present": 1, "late": 1}},
        # Anchor row so $densify still emits every day when the range has no records at all.
        {"$unionWith": {"pipeline": [{"$documents": [{"d": start_dt, "present": 0, "late": 0}]}]}},
        {"$group": {"_id": "$d", "present": {"$sum": "$present"}, "late": {"$sum": "$late"}}},
        {"$project": {"_id": 0, "d": "$_id", "present": 1, "late": 1}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_excl]}}},
        {"$lookup": {  # R9 headcount as of that day
            "from": "employees",
            "let": {"day": day_str},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "date": day_str,
            "is_working_day": is_working_day,
            "headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]},
            "present": {"$ifNull": ["$present", 0]},
            "late": {"$ifNull": ["$late", 0]},
        }},
        {"$set": {"rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rate, None]}}},
        {"$setWindowFields": {
            "sortBy": {"d": 1},
            "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": "$present",
            "late_count": "$late",
            "attendance_rate": "$rate",
            "moving_avg_7d": "$ma",
        }},
    ]


def trend_inputs(department: str, start: str, end: str) -> tuple[list, date, date]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0:
        raise validation_error(("query", "to"), "to must be on or after from", end)
    if (d1 - d0).days + 1 > MAX_TREND_DAYS:
        raise validation_error(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", end)
    codes = department_codes(department)
    if not codes:
        raise HTTPException(404, "department not found")
    return codes, d0, d1


# --------------------------------------------------------------------------- #
# System and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not indexes_ready.is_set():
        raise HTTPException(503, "indexes not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    cursor = employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": employees.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **compute_derived(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # the unique (emp_code, date) index decides concurrent punch-ins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_punch_out(rec["punch_in"], ts)
    derived = compute_derived(rec["status"], rec["punch_in"], ts, date.fromisoformat(rec["date"]), emp)
    derived.pop("late_minutes")  # set at punch-in, not touched by punch-out
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # loses to a concurrent punch-out -> None
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = build_attendance_filter(emp_code, date_from, date_to, status)
    cursor = logs.find(q).sort(list(ATTENDANCE_SORT.items())).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": logs.count_documents(q),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[DateStr, Path()], body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")
    day = datetime.strptime(date, "%Y-%m-%d").date()
    old = normalized_state(rec)

    status = body.status or old["status"]
    if status in NON_PRESENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(("body", "status"), f"{status} clears punch times; do not send them")
        new_in = new_out = None
    else:
        new_in = to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        if new_in is None:
            raise validation_error(("body", "punch_in"), f"{status} requires a punch_in")
        if attendance_date(new_in, emp) != day:
            raise validation_error(("body", "punch_in"), "punch_in must fall on the record's attendance date")
        new_out = to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if new_out is not None:
            check_punch_out(new_in, new_out)

    new = {"status": status, "punch_in": new_in, "punch_out": new_out,
           **compute_derived(status, new_in, new_out, day, emp)}
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if old[f] != new[f]}
    if not changes:
        raise validation_error(("body",), "the request changes nothing")

    # Optimistic concurrency: only apply if nobody changed the record since we read it.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
    }
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):
    emp = get_employee_or_404(emp_code)
    rows = list(logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    s = rows[0] if rows else {"present": 0, "leave": 0, "late_n": 0, "late_m": 0, "ot_m": 0}
    m0, m1 = month_bounds(month)
    start = max(m0, date.fromisoformat(emp["joined_on"]))
    working = count_weekdays(start, m1) if start <= m1 else 0
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": s["present"],
        "leave_days": s["leave"],
        "late_count": s["late_n"],
        "total_late_minutes": s["late_m"],
        "total_overtime_minutes": s["ot_m"],
        "attendance_pct": half_up(Decimal(str(s["present"])) * 100 / working, 2) if working else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):
    rows = employees.aggregate(build_department_summary_pipeline(month, department))
    items = []
    for r in rows:
        wh_sum = r["wh_sum"].to_decimal() if isinstance(r["wh_sum"], Decimal128) else Decimal(r["wh_sum"])
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": r["present"],
            "avg_work_hours": half_up(wh_sum / r["wh_n"], 2) if r["wh_n"] else None,
            "late_count": r["late_n"],
            "total_late_minutes": r["late_m"],
            "leave_count": r["leave_n"],
            "on_duty_count": r["on_duty_n"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Annotated[MonthStr, Query()], limit: Limit = 10, department: Optional[str] = None):
    items = list(logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: Annotated[DateStr, Query(alias="from")],
    end: Annotated[DateStr, Query(alias="to")],
):
    codes, d0, d1 = trend_inputs(department, start, end)
    items = []
    for r in logs.aggregate(build_department_trend_pipeline(department, codes, d0, d1)):
        rate, ma = r["attendance_rate"], r["moving_avg_7d"]
        items.append({
            **r,
            "attendance_rate": None if rate is None else half_up(rate, 4),
            "moving_avg_7d": None if ma is None else half_up(ma, 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def _require(**params):
    missing = [name for name, value in params.items() if value is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing]
        )


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[MonthStr], Query()] = None,
    department: Optional[str] = None,
    limit: Limit = 10,
    date_from: Annotated[Optional[DateStr], Query()] = None,
    date_to: Annotated[Optional[DateStr], Query()] = None,
    status: Optional[Status] = None,
    start: Annotated[Optional[DateStr], Query(alias="from")] = None,
    end: Annotated[Optional[DateStr], Query(alias="to")] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", build_employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = "employees", build_department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = "attendance_logs", build_late_leaderboard_pipeline(month, limit, department)
        else:
            _require(department=department, **{"from": start, "to": end})
            codes, d0, d1 = trend_inputs(department, start, end)
            collection, pipeline = "attendance_logs", build_department_trend_pipeline(department, codes, d0, d1)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
