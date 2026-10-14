import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from pymongo import MongoClient
import concurrent.futures

os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, client, IST, create_indexes

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    create_indexes()
    yield
    client.drop_database("attendance_test_db")

@pytest.fixture(autouse=True)
def clear_db():
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    create_indexes()
    yield

@pytest.fixture
def api_client():
    return TestClient(app)

def make_epoch_ms(year, month, day, hour, minute, second=0):
    dt = datetime(year, month, day, hour, minute, second, tzinfo=IST)
    return int(dt.timestamp() * 1000)

# --- 1. Health ---
def test_health(api_client):
    res = api_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}

# --- 2. Employees ---
def test_create_and_list_employees(api_client):
    # Valid employee
    payload = {
        "emp_code": "EMP0001",
        "name": "Alice Smith",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    }
    res = api_client.post("/employees", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["created_at"], int)
    assert "_id" not in data

    # Duplicate emp_code -> 409
    res_dup = api_client.post("/employees", json=payload)
    assert res_dup.status_code == 409

    # Same shift_start and shift_end -> 422
    payload_invalid = dict(payload, emp_code="EMP0002", shift_end="09:30")
    res_inv = api_client.post("/employees", json=payload_invalid)
    assert res_inv.status_code == 422

    # Second valid employee in Sales
    api_client.post("/employees", json=dict(payload, emp_code="EMP0002", department="Sales", joined_on="2026-02-01"))

    # List employees
    res_list = api_client.get("/employees")
    assert res_list.status_code == 200
    body = res_list.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["emp_code"] == "EMP0001"
    assert body["items"][1]["emp_code"] == "EMP0002"

    # Filter by department
    res_filter = api_client.get("/employees?department=Engineering")
    assert res_filter.status_code == 200
    assert res_filter.json()["total"] == 1
    assert res_filter.json()["items"][0]["emp_code"] == "EMP0001"

# --- 3. Punch In (R1, R2) ---
def test_punch_in_rules(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Unknown employee -> 404
    res = api_client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404

    # Grace period exact: 09:40:00 -> late_minutes = 0
    t_094000 = make_epoch_ms(2026, 7, 6, 9, 40, 0)
    res1 = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_094000})
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0
    assert res1.json()["date"] == "2026-07-06"
    assert res1.json()["work_hours"] is None
    assert res1.json()["punch_out"] is None

    # Duplicate punch-in for same date -> 409
    res_dup = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_094000 + 1000})
    assert res_dup.status_code == 409

def test_punch_in_late_minutes(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Next day: 09:40:01 -> strictly > 10 min -> late_minutes = 10
    t_094001 = make_epoch_ms(2026, 7, 7, 9, 40, 1)
    res = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_094001})
    assert res.status_code == 201
    assert res.json()["late_minutes"] == 10

    # Next day: 10:15:59 -> late_minutes = 45
    t_101559 = make_epoch_ms(2026, 7, 8, 10, 15, 59)
    res2 = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_101559})
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 45

def test_punch_in_overnight_shift(api_client):
    # Overnight shift: 22:00 - 06:00
    api_client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Eve",
        "email": "eve@example.com",
        "department": "Security",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in at 02:00 on 2026-07-07 (< 06:00) -> belongs to shift date 2026-07-06!
    t_0200 = make_epoch_ms(2026, 7, 7, 2, 0, 0)
    res = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": t_0200})
    assert res.status_code == 201
    assert res.json()["date"] == "2026-07-06"

# --- 4. Punch Out (R3, R4, R5) ---
def test_punch_out_rules(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch in at 09:28
    t_in = make_epoch_ms(2026, 7, 6, 9, 28, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Punch out earlier than punch in -> 422
    res_early = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in - 1000})
    assert res_early.status_code == 422

    # Punch out > 24h after -> 422
    res_late = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_in + 25 * 3600 * 1000})
    assert res_late.status_code == 422

    # Punch out at 18:35 (5 min overtime, < 30 min minimum -> overtime_minutes = 0)
    # Work hours: (18:35:00 - 09:28:00) = 9h 7m = 32820s / 3600 = 9.11666... -> 9.12
    t_out = make_epoch_ms(2026, 7, 6, 18, 35, 0)
    res_out = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})
    assert res_out.status_code == 200
    record = res_out.json()
    assert record["work_hours"] == 9.12
    assert record["overtime_minutes"] == 0
    assert record["half_day"] is False

    # Second punch out -> 409
    res_out_again = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out + 1000})
    assert res_out_again.status_code == 409

def test_punch_out_half_day_and_overtime(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Bob",
        "email": "bob@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Day 1: Half day (3.5 hours worked < 4.50)
    t_in = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    t_out = make_epoch_ms(2026, 7, 6, 13, 0, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in})
    res1 = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out})
    assert res1.status_code == 200
    assert res1.json()["work_hours"] == 3.50
    assert res1.json()["half_day"] is True

    # Day 2: Overtime (out at 19:10 = 40 min past 18:30, >= 30 min -> overtime = 40)
    t_in2 = make_epoch_ms(2026, 7, 7, 9, 30, 0)
    t_out2 = make_epoch_ms(2026, 7, 7, 19, 10, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t_in2})
    res2 = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": t_out2})
    assert res2.status_code == 200
    assert res2.json()["overtime_minutes"] == 40
    assert res2.json()["half_day"] is False

# --- 5. Regularize (PATCH) ---
def test_regularize_record(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    t_in = make_epoch_ms(2026, 7, 7, 10, 5, 0) # 35 min late
    t_out = make_epoch_ms(2026, 7, 7, 18, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})

    # Correct punch-in to 09:28 (late_minutes becomes 0)
    t_in_corrected = make_epoch_ms(2026, 7, 7, 9, 28, 0)
    patch_body = {
        "reason": "Biometric machine lag",
        "regularized_by": "manager.bob",
        "punch_in": t_in_corrected
    }
    res = api_client.patch("/attendance/EMP0001/2026-07-07", json=patch_body)
    assert res.status_code == 200
    rec = res.json()
    assert rec["late_minutes"] == 0
    assert rec["punch_in"] == t_in_corrected
    assert len(rec["history"]) == 1
    h = rec["history"][0]
    assert h["by"] == "manager.bob"
    assert h["reason"] == "Biometric machine lag"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0

    # Changing nothing -> 422
    res_noop = api_client.patch("/attendance/EMP0001/2026-07-07", json={
        "reason": "Accidental click",
        "regularized_by": "manager.bob",
        "punch_in": t_in_corrected
    })
    assert res_noop.status_code == 422

# --- 6. Analytics: Monthly ---
def test_employee_monthly_analytics(api_client):
    # Joined mid-month: 2026-07-15
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Alice",
        "email": "alice@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15"
    })

    # Working days in July 2026 starting from 2026-07-15:
    # July 2026 calendar: 15 (Wed), 16 (Thu), 17 (Fri), 20-24 (5), 27-31 (5) = 13 working days
    res = api_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    body = res.json()
    assert body["working_days"] == 13
    assert body["present_days"] == 0.0
    assert body["attendance_pct"] == 0.0

    # Add 1 present day (2026-07-15) and 1 half day (2026-07-16)
    t_in1 = make_epoch_ms(2026, 7, 15, 9, 30, 0)
    t_out1 = make_epoch_ms(2026, 7, 15, 18, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in1})
    api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out1})

    t_in2 = make_epoch_ms(2026, 7, 16, 9, 30, 0)
    t_out2 = make_epoch_ms(2026, 7, 16, 13, 0, 0) # 3.5h -> half day
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in2})
    api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out2})

    res2 = api_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res2.status_code == 200
    body2 = res2.json()
    # present_days = 1.0 + 0.5 = 1.5
    assert body2["present_days"] == 1.5
    # attendance_pct = 1.5 / 13 * 100 = 11.53846... -> 11.54
    assert body2["attendance_pct"] == 11.54

# --- 7. Analytics: Department Summary ---
def test_department_summary(api_client):
    # Two employees in Engineering: one with logs, one with zero logs (R9)
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    api_client.post("/employees", json={
        "emp_code": "EMP0002", "name": "Bob", "email": "b@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    # One employee who joined NEXT month (should be excluded from headcount in July)
    api_client.post("/employees", json={
        "emp_code": "EMP0003", "name": "Charlie", "email": "c@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"
    })

    t_in = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    t_out = make_epoch_ms(2026, 7, 6, 18, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})
    api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})

    res = api_client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    eng = items[0]
    assert eng["department"] == "Engineering"
    assert eng["headcount"] == 2 # EMP0001 and EMP0002; EMP0003 excluded
    assert eng["present_days"] == 1.0
    assert eng["avg_work_hours"] == 9.0

# --- 8. Analytics: Late Leaderboard (Standard Competition Ranking) ---
def test_late_leaderboard_competition_ranking(api_client):
    for i in range(1, 5):
        api_client.post("/employees", json={
            "emp_code": f"EMP000{i}", "name": f"Emp {i}", "email": f"emp{i}@ex.com",
            "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
        })

    # EMP0001: 50 min late
    # EMP0002: 30 min late
    # EMP0003: 30 min late (tied with EMP0002)
    # EMP0004: 10 min late (should get rank 4, not 3!)
    t1 = make_epoch_ms(2026, 7, 6, 10, 20, 0)
    t2 = make_epoch_ms(2026, 7, 6, 10, 0, 0)
    t3 = make_epoch_ms(2026, 7, 6, 10, 0, 0)
    t4 = make_epoch_ms(2026, 7, 6, 9, 40, 1)

    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t1})
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t2})
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": t3})
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": t4})

    res = api_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4
    assert items[0]["emp_code"] == "EMP0001" and items[0]["rank"] == 1
    assert items[1]["emp_code"] == "EMP0002" and items[1]["rank"] == 2
    assert items[2]["emp_code"] == "EMP0003" and items[2]["rank"] == 2
    assert items[3]["emp_code"] == "EMP0004" and items[3]["rank"] == 4 # skipped 3!

    # Test limit cutoff with ties: limit=2 returns EMP0001, EMP0002, and EMP0003 (3 rows because tie is at rank 2 <= limit)
    res_lim = api_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res_lim.status_code == 200
    lim_items = res_lim.json()["items"]
    assert len(lim_items) == 3

# --- 9. Analytics: Department Trend (Gap filling & moving average) ---
def test_department_trend(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })

    # Range: 2026-07-04 (Sat) to 2026-07-07 (Tue) = 4 days
    res = api_client.get("/analytics/departments/Engineering/trend?from=2026-07-04&to=2026-07-07")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4
    assert [x["date"] for x in items] == ["2026-07-04", "2026-07-05", "2026-07-06", "2026-07-07"]
    # Saturday & Sunday
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[1]["is_working_day"] is False
    assert items[1]["attendance_rate"] is None
    # Monday: working day with 0 records -> rate 0.0
    assert items[2]["is_working_day"] is True
    assert items[2]["attendance_rate"] == 0.0

# --- 10. Admin Explain (IXSCAN verification) ---
def test_admin_explain_endpoints(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })

    # 1. attendance_list
    res1 = api_client.get("/admin/explain/attendance_list?page=1&page_size=20")
    assert res1.status_code == 200
    assert res1.json()["collection"] == "attendance_logs"

    # 2. employee_monthly
    res2 = api_client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res2.status_code == 200
    assert res2.json()["collection"] == "attendance_logs"

    # 3. department_summary
    res3 = api_client.get("/admin/explain/department_summary?month=2026-07")
    assert res3.status_code == 200
    assert res3.json()["collection"] == "employees"

    # 4. late_leaderboard
    res4 = api_client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res4.status_code == 200
    assert res4.json()["collection"] == "attendance_logs"

    # 5. department_trend
    res5 = api_client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
    assert res5.status_code == 200
    assert res5.json()["collection"] == "attendance_logs"

# --- 11. Concurrency: Punch-in race condition ---
def test_concurrent_punch_in(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })

    t = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    status_codes = []

    def do_punch():
        client_local = TestClient(app)
        r = client_local.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t})
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            status_codes.append(f.result())

    # Exactly one 201 and remaining 409
    assert status_codes.count(201) == 1
    assert status_codes.count(409) == 4

def test_attendance_listing_and_filtering(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    api_client.post("/employees", json={
        "emp_code": "EMP0002", "name": "Bob", "email": "b@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })

    # Create logs
    # 2026-07-06: EMP0001 (PRESENT), EMP0002 (WFH)
    # 2026-07-07: EMP0001 (PRESENT)
    t1 = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    t2 = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    t3 = make_epoch_ms(2026, 7, 7, 9, 30, 0)

    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t1, "status": "PRESENT"})
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": t2, "status": "WFH"})
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t3, "status": "PRESENT"})

    # date_from > date_to -> 422
    res_bad = api_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert res_bad.status_code == 422

    # Listing sorted by date desc, emp_code asc
    res = api_client.get("/attendance")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3
    # First item: 2026-07-07 EMP0001
    assert items[0]["date"] == "2026-07-07" and items[0]["emp_code"] == "EMP0001"
    # Next items: 2026-07-06, EMP0001 then EMP0002
    assert items[1]["date"] == "2026-07-06" and items[1]["emp_code"] == "EMP0001"
    assert items[2]["date"] == "2026-07-06" and items[2]["emp_code"] == "EMP0002"

    # Filter by status WFH
    res_wfh = api_client.get("/attendance?status=WFH")
    assert res_wfh.status_code == 200
    assert res_wfh.json()["total"] == 1
    assert res_wfh.json()["items"][0]["emp_code"] == "EMP0002"

    # Pagination
    res_p1 = api_client.get("/attendance?page=1&page_size=2")
    assert res_p1.status_code == 200
    assert len(res_p1.json()["items"]) == 2
    assert res_p1.json()["total"] == 3

    res_p2 = api_client.get("/attendance?page=2&page_size=2")
    assert res_p2.status_code == 200
    assert len(res_p2.json()["items"]) == 1
    assert res_p2.json()["total"] == 3

def test_regularize_absent_leave_rules(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    t_in = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Cannot supply punch_in when setting ABSENT -> 422
    res_inv = api_client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "Sick leave requested",
        "regularized_by": "manager",
        "status": "ABSENT",
        "punch_in": t_in
    })
    assert res_inv.status_code == 422

    # Setting ABSENT clears punch times and sets derived fields to defaults
    res_absent = api_client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "Employee did not show up",
        "regularized_by": "manager",
        "status": "ABSENT"
    })
    assert res_absent.status_code == 200
    rec = res_absent.json()
    assert rec["status"] == "ABSENT"
    assert rec["punch_in"] is None
    assert rec["punch_out"] is None
    assert rec["work_hours"] is None
    assert rec["late_minutes"] == 0
    assert rec["overtime_minutes"] == 0
    assert rec["half_day"] is False
    assert len(rec["history"]) == 1

def test_concurrent_punch_out(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    t_in = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    t_out = make_epoch_ms(2026, 7, 6, 18, 30, 0)
    status_codes = []

    def do_punch_out():
        client_local = TestClient(app)
        r = client_local.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": t_out})
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(do_punch_out) for _ in range(5)]
        for f in concurrent.futures.as_completed(futures):
            status_codes.append(f.result())

    # Exactly one 200 and remaining 409
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 4

def test_explain_index_scan_verified(api_client):
    # Verify that the explain plans returned by /admin/explain contain IXSCAN and no COLLSCAN
    api_client.post("/employees", json={
        "emp_code": "EMP0001", "name": "Alice", "email": "a@ex.com",
        "department": "Engineering", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"
    })
    t_in = make_epoch_ms(2026, 7, 6, 9, 30, 0)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": t_in})

    # Test explain endpoints
    endpoints_to_test = [
        "/admin/explain/attendance_list?page=1&page_size=20",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07",
    ]

    for ep in endpoints_to_test:
        res = api_client.get(ep)
        assert res.status_code == 200, f"Explain endpoint failed: {ep}"
        body = res.json()
        assert "explain" in body
        explain_json = str(body["explain"])
        # Plan must contain IXSCAN
        assert "IXSCAN" in explain_json, f"Missing IXSCAN in {ep}"
        # Plan must not contain COLLSCAN
        assert "COLLSCAN" not in explain_json, f"Found COLLSCAN in {ep}"
