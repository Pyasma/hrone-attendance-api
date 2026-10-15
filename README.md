# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+, tested on 7.0). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db   # or put them in .env; real env vars win
uvicorn app.main:app --port 8000
```
Indexes are created at startup in a background thread; `GET /health` returns 503 until MongoDB answers a ping and
the indexes exist (about 2 s on 100k records).

## Tests
```bash
pip install pytest httpx
pytest                                                   # uses database `attendance_test` (dropped per test)
MONGO_DB=attendance_big python scripts/gen_data.py       # ~100k logs with edge cases (drops that DB's collections)
MONGO_DB=attendance_big uvicorn app.main:app --port 8000
MONGO_DB=attendance_big python scripts/oracle_check.py   # compares analytics with a plain-Python oracle
```

## Choices where the contract is open
- Trend range: more than 92 **days inclusive** is 422 (92 rows is fine, 93 is not).
- Trend `moving_avg_7d` averages the already-rounded daily rates, then rounds half-up to 4 decimals.
- PATCH: derived fields in the body are ignored; explicit `null` for `status`/`punch_in`/`punch_out` is 422
  (the schema is non-nullable); a punch-in on a date that already has an ABSENT/LEAVE record is 409 (fix it with PATCH).
- Punch-in before the employee's `joined_on` is allowed (not forbidden by the contract).
- Monthly/summary count stored records as they are, including any dated before `joined_on`.
- Leaderboard `late_count` includes weekend records (R7 restricts only `present_days`).
- Punch-out picks the latest record with `punch_in <= punched_at`; if that one is already closed it is 409, it never
  falls back to an older open record.
