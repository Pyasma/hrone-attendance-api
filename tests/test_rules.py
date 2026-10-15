from datetime import date, datetime, timedelta

import pytest

from app.main import IST, attendance_date, compute_derived, compute_late_minutes, compute_overtime, to_dt, to_ms

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(d: str, hms: str, ms: int = 0) -> int:
    dt = datetime.fromisoformat(f"{d}T{hms}").replace(tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms


def late(emp, d, hms, ms=0):
    t = to_dt(ist(d, hms, ms))
    return attendance_date(t, emp), compute_late_minutes(t, attendance_date(t, emp), emp)


@pytest.mark.parametrize("hms,ms,expected", [
    ("09:40:00", 0, 0),
    ("09:40:01", 0, 10),
    ("10:15:59", 0, 45),
    ("09:40:00", 900, 0),
    ("09:00:00", 0, 0),
])
def test_r2_late(hms, ms, expected):
    assert late(DAY, "2026-07-06", hms, ms)[1] == expected


def test_r1_truncates_to_seconds():
    assert to_ms(to_dt(ist("2026-07-06", "09:40:00", 900))) % 1000 == 0


@pytest.mark.parametrize("d,hms,exp_date,exp_late", [
    ("2026-07-07", "05:30:00", date(2026, 7, 6), 450),
    ("2026-07-06", "21:55:00", date(2026, 7, 6), 0),
    ("2026-07-07", "06:00:00", date(2026, 7, 7), 0),
    ("2026-07-06", "23:00:00", date(2026, 7, 6), 60),
])
def test_r1_overnight(d, hms, exp_date, exp_late):
    assert late(NIGHT, d, hms) == (exp_date, exp_late)


def test_r1_utc_input_is_converted_to_ist():
    # 04:15Z is 09:45 IST -> 15 minutes late
    t = datetime.fromisoformat("2026-07-06T04:15:00+00:00")
    assert compute_late_minutes(t, attendance_date(t, DAY), DAY) == 15


@pytest.mark.parametrize("emp,d,out_d,out_hms,expected", [
    (DAY, "2026-07-06", "2026-07-06", "18:59:59", 0),
    (DAY, "2026-07-06", "2026-07-06", "19:00:00", 30),
    (DAY, "2026-07-06", "2026-07-06", "18:35:00", 0),
    (NIGHT, "2026-07-06", "2026-07-07", "06:40:00", 40),
    (NIGHT, "2026-07-06", "2026-07-07", "05:00:00", 0),
])
def test_r3_overtime(emp, d, out_d, out_hms, expected):
    assert compute_overtime(to_dt(ist(out_d, out_hms)), date.fromisoformat(d), emp) == expected


@pytest.mark.parametrize("seconds,hours,half", [
    (16218, 4.51, False),
    (16182, 4.5, False),
    (16181, 4.49, True),
    (12600, 3.5, True),
    (32820, 9.12, False),
])
def test_r4_r5_hours_and_half_day(seconds, hours, half):
    t_in = to_dt(ist("2026-07-06", "09:00:00"))
    d = compute_derived("PRESENT", t_in, t_in + timedelta(seconds=seconds), date(2026, 7, 6), DAY)
    assert (d["work_hours"], d["half_day"]) == (hours, half)


def test_non_presence_derived_defaults():
    assert compute_derived("LEAVE", None, None, date(2026, 7, 6), DAY) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
    }
