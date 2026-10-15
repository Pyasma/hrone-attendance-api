# REVIEW.md

Line numbers refer to the original starter `app/main.py`.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | module, L22-23 | Falls back to a hard-coded `mongodb://localhost:27017` / `attendance_db` when the env vars are missing. | Unset `MONGO_URI`: the app silently connects to a local DB instead of failing. | Read `os.environ["MONGO_URI"]` / `["MONGO_DB"]` with no default (after `load_dotenv()`, which never overrides real env vars). |
| 2 | module, L22 | `MongoClient` without `tz_aware=True` returns naive datetimes; default 30 s server-selection timeout. | Instants read back naive, so `aware - naive` raises `TypeError`; `/health` hangs ~30 s when Mongo is down. | `MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2000)`. |
| 3 | module | No indexes are created, so no uniqueness guarantees and every query is a collection scan. | `explain` shows `COLLSCAN`; duplicate employees/records are possible under concurrency. | `lifespan` starts a thread that creates all indexes idempotently (retrying if Mongo is not up yet); `/health` is 503 until they exist. |
| 4 | `compute_late_minutes`, L35 | `punch_in.replace(hour, minute)` builds the shift start in the punch's own time zone and date, not IST. | Punch at 04:15Z (09:45 IST) is compared with 09:30 **UTC**: negative, returns 0 instead of 15. | Shift start = `datetime.combine(attendance_date, shift_start, IST)`. |
| 5 | `compute_late_minutes`, L36-37 | Grace check is done on whole minutes (`minutes > 10`). | 09:40:01: `int(10.02) = 10`, not > 10, returns 0; R2 says 10. | Late iff elapsed **seconds** > 600, then `seconds // 60`. |
| 6 | `compute_late_minutes`, L33-37 | Ignores overnight shifts. | Shift 22:00-06:00, punch 05:30 IST on the 7th: measured against 22:00 on the 7th, returns 0; should be 450 (date 6th). | Late is measured from the shift start on the R1 attendance date. |
| 7 | all helpers / punch-in | Instants are never truncated to whole seconds (R1). | `punched_at` ending in `.900` is stored and returned with milliseconds; 09:40:00.900 must count as 09:40:00. | `to_dt(ms)` = `fromtimestamp(ms // 1000, utc)` at the API boundary; that is what is stored. |
| 8 | `compute_work_hours`, L41 | Float `round()` is neither half-up nor exact. | 16218 s = 4.505 h, `round` gives 4.5 instead of 4.51. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 9 | `compute_overtime`, L47 | No 30-minute minimum. | Punch-out 18:35 for an 18:30 shift returns 5; R3 says 0. | `minutes if minutes >= 30 else 0`. |
| 10 | `compute_overtime`, L46-47 | Overnight shift end is put on the same day; mixing a naive punch-out with an aware shift end raises. | 22:00-06:00 shift gets ~16 h overtime, or a 500 (`TypeError: can't subtract offset-naive and offset-aware`). | `shift_bounds()` adds one day to the end when `shift_end <= shift_start`; all datetimes aware. |
| 11 | `EmployeeIn`, L53-60 | No validation at all. | `emp_code: "x"`, shift `"25:99"`, email `"nope"`, `joined_on: "2026-02-30"`, `shift_start == shift_end` all return 201. | Patterns and lengths from the contract, real-date check, model validator for `shift_start != shift_end`. |
| 12 | `PunchInIn`, L63-66 | Lax `int` accepts `"1783312500000"`, `1.7833125e12`, `true` and seconds values; `status` accepts any string. | `punched_at: 1783312500` (seconds) or `status: "ABSENT"` returns 201. | `EpochMs` = strict int in `[1e11, 4102444800000]`; `status` is `Literal["PRESENT","WFH","ON_DUTY"]`; explicit `null` rejected. |
| 13 | `health`, L72-74 | Always 200. | Stop Mongo, `/health` still says ok. | `admin.command("ping")` (plus indexes ready), 503 otherwise. A global `ConnectionFailure` handler turns DB outages into 503 instead of 500 elsewhere. |
| 14 | `create_employee`, L79-83 | Check-then-insert race. | 20 parallel POSTs with the same `emp_code`: several get 201. | Unique index on `emp_code`; insert and map `DuplicateKeyError` to 409. |
| 15 | `create_employee`, L82 | `datetime.now()` is naive local time and is returned as an ISO string. | Response has `"created_at": "2026-..."`, not epoch ms; stored value off by the server's UTC offset. | Store `datetime.now(timezone.utc)` (whole seconds), return ms. |
| 16 | `list_employees`, L93 | `skip = page * page_size`. | `page=1` starts at the 21st employee. | `(page - 1) * page_size`. |
| 17 | `list_employees`, L94 | `total` counts the whole collection. | `?department=Sales` still reports the global total (R10). | `count_documents(q)`. |
| 18 | `list_employees`, L89, L95 | No sort and no bounds on `page`/`page_size`. | Order is arbitrary; `page=0` gives a negative skip, which is a 500; `page_size=0` means no limit. | `.sort("emp_code")`, `Query(ge=1)` / `Query(ge=1, le=100)`. |
| 19 | `punch_in`, L101, L113 | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises. | Unknown `emp_code` returns 500 instead of 404. | 404 when the employee does not exist. |
| 20 | `punch_in`, L102 | `fromtimestamp()` / `now()` without a tz give naive server-local times; PyMongo stores naive values as if UTC. | On an IST machine, stored instants are 5 h 30 m off. | `datetime.fromtimestamp(ms // 1000, timezone.utc)`. |
| 21 | `punch_in`, L103 | `date` is the server-local calendar date; no IST, no overnight rule. | Punch at 00:30 IST on a UTC server lands on the previous date; overnight 05:30 punch lands on the wrong day. | `attendance_date()` implements R1. |
| 22 | `punch_in`, L104-118 | Check-then-insert race on `(emp_code, date)`. | 20 parallel punch-ins: several 201s and duplicate records. | Unique `(emp_code, date)` index; `DuplicateKeyError` gives 409. |
| 23 | `punch_in` L119, `list_attendance` L149 | Responses contain `id` (stringified `_id`) and datetimes instead of epoch ms. | Response has `"id"` and ISO strings. | One `serialize_record()` with an explicit field whitelist; converts every instant, including those nested in `history[].changes`. |
| 24 | `list_attendance`, L144-147 | Loads the whole collection into memory, then sorts and pages in Python. | Slow and memory-heavy at 100k records; nothing for an index to do. | `find(q).sort(date desc, emp_code asc).skip().limit()` backed by indexes, plus `count_documents(q)`. |
| 25 | `list_attendance`, L145 | No secondary sort on `emp_code`. | Records on the same date come back in arbitrary order; pages overlap or skip records. | Sort `date` desc, then `emp_code` asc. |
| 26 | `list_attendance`, L125-132 | No validation of dates, status or page bounds. | `date_from=2026-08-01&date_to=2026-07-01` returns 200 with nothing; `date_from=2026-02-30` or `status=NOPE` silently match nothing; `page=0` slices from -20. | Real-date params, `Status` literal, `date_from > date_to` is 422, page bounds as in #18. |
| 27 | `list_attendance`, L149 | Legacy records without `half_day` / `history` are returned as stored. | Sample doc 9 is missing required response fields. | Serializer fills defaults (`false`, `[]`, `0`). |

## Looked at and decided were fine

- `load_dotenv()` defaults to `override=False`, so real environment variables already win over `.env`.
- The `IST` constant (`UTC+05:30`, no DST) is correct.
- Plain `def` endpoints run in FastAPI's threadpool and `MongoClient` is thread-safe, so concurrency is decided by the database (unique indexes, conditional updates), not by the event loop.
- Comparing `YYYY-MM-DD` strings with `$gte`/`$lte` is correct: lexical order equals date order for this fixed-width format.
- `doc.pop("_id")` in `create_employee` did work; it is replaced by the shared serializer only for consistency.
- The helper split (late / work hours / overtime) is a sensible shape; the helpers were kept but rewritten to take the attendance date and the employee's shift.
