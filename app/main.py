"""
Employee Attendance & Analytics API

FastAPI application backed by MongoDB following OpenAPI contract v2.0.0 and business rules R1-R10.
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

    # attendance_logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: Union[float, Decimal], places: int = 2) -> float:
    """R8 Rounding: exact half-up rounding (not banker's rounding)."""
    d = Decimal(str(value))
    q = Decimal("10") ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert UTC or IST datetime to integer epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    """
    Convert integer epoch milliseconds to UTC datetime,
    truncated to whole seconds per R1.
    """
    if ms is None:
        return None
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def truncate_dt_to_seconds(dt: datetime) -> datetime:
    """R1: Truncate instant to whole seconds."""
    return dt.replace(microsecond=0)


def get_attendance_date(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))

    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    if is_overnight and punch_in_ist.time() < time(eh_h, eh_m):
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str, date_str: str) -> int:
    """
    R2 Late. Grace period 10 minutes.
    Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) elapsed since shift_start.
    Otherwise 0.
    """
    punch_in_ist = punch_in_dt.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    d = date.fromisoformat(date_str)
    start_dt = datetime(d.year, d.month, d.day, sh_h, sh_m, 0, tzinfo=IST)
    diff_sec = (punch_in_ist - start_dt).total_seconds()
    if diff_sec > 600:  # strictly more than 10:00 (600 seconds)
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """
    R4 Work hours. (punch_out - punch_in) in seconds / 3600,
    rounded to 2 decimals half-up.
    """
    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    d_sec = Decimal(str(int(diff_sec)))
    return float((d_sec / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime_minutes(
    punch_out_dt: datetime, shift_start: str, shift_end: str, date_str: str
) -> int:
    """
    R3 Overtime. Whole minutes (floor) between shift_end and punch_out,
    counted only when that is >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_out_ist = punch_out_dt.astimezone(IST)
    sh_h, sh_m = map(int, shift_start.split(":"))
    eh_h, eh_m = map(int, shift_end.split(":"))
    d = date.fromisoformat(date_str)
    is_overnight = (eh_h, eh_m) <= (sh_h, sh_m)
    end_date = d + timedelta(days=1) if is_overnight else d
    end_dt = datetime(end_date.year, end_date.month, end_date.day, eh_h, eh_m, 0, tzinfo=IST)
    diff_sec = (punch_out_ist - end_dt).total_seconds()
    if diff_sec >= 1800:  # at least 30 minutes
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5 Half day. true when the rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7 Working days. Monday - Friday, counted from joined_on for mid-month joiners.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    first_day = date(year, month, 1)
    last_day = date(year, month, num_days)
    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(first_day, joined_date)
    if start_date > last_day:
        return 0
    working_days = 0
    cur = start_date
    while cur <= last_day:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def format_history_entry(entry: dict) -> dict:
    """Format MongoDB history entry for API response."""
    at_val = entry["at"]
    at_ms = dt_to_epoch_ms(at_val) if isinstance(at_val, datetime) else at_val
    changes_formatted = {}
    for k, v in entry.get("changes", {}).items():
        from_val = v.get("from")
        to_val = v.get("to")
        if k in ("punch_in", "punch_out"):
            if isinstance(from_val, datetime):
                from_val = dt_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = dt_to_epoch_ms(to_val)
        changes_formatted[k] = {"from": from_val, "to": to_val}
    return {
        "at": at_ms,
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes_formatted,
    }


def format_attendance_record(doc: dict) -> dict:
    """Format MongoDB attendance document for API response."""
    punch_in_ms = dt_to_epoch_ms(doc.get("punch_in"))
    punch_out_ms = dt_to_epoch_ms(doc.get("punch_out"))
    history_docs = doc.get("history") or []
    history_formatted = [format_history_entry(h) for h in history_docs]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": punch_in_ms,
        "punch_out": punch_out_ms,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_formatted,
    }


def format_employee(doc: dict) -> dict:
    """Format MongoDB employee document for API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database unreachable: {str(e)}",
        )


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Unique emp_code enforced by database unique index."""
    now_dt = truncate_dt_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_dt
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in. Race-safe via unique index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        ts_utc = epoch_ms_to_dt(body.punched_at)
    else:
        ts_utc = truncate_dt_to_seconds(datetime.now(timezone.utc))

    date_str = get_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(ts_utc, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409, "already punched in for this date"
        )

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes most recent record whose punch_in <= punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        out_dt = epoch_ms_to_dt(body.punched_at)
    else:
        out_dt = truncate_dt_to_seconds(datetime.now(timezone.utc))

    # Find the most recent record that has a punch-in
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(404, "No punch-in found")

    in_dt = record["punch_in"]
    if in_dt.tzinfo is None:
        in_dt = in_dt.replace(tzinfo=timezone.utc)

    # Validation rules: punched_at > punch_in and <= 24 hours
    if out_dt <= in_dt:
        raise HTTPException(
            422, "punched_at must be strictly after punch_in"
        )

    if (out_dt - in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            422,
            "punched_at cannot be more than 24 hours after punch_in",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(409, "Record is already punched out")

    work_hours = compute_work_hours(in_dt, out_dt)
    overtime_minutes = compute_overtime_minutes(
        out_dt, emp["shift_start"], emp["shift_end"], record["date"]
    )
    half_day = compute_half_day(work_hours)

    # Atomic update to avoid race conditions: ensure punch_out is still None
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            409, "Record was already punched out concurrently"
        )

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422, "date_from must not be greater than date_to"
        )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date_str: str = Path(..., alias="date_str"),
    body: RegularizeRequest = ...,
):
    """
    Regularize (manually correct) one day's record.
    Recomputes derived fields and appends audit trail to history.
    """
    # Validate date format in path
    try:
        date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(422, "Invalid date format in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not record:
        raise HTTPException(404, "Attendance record not found")

    # Business rule: setting status ABSENT or LEAVE clears both punch times
    if body.status in ("ABSENT", "LEAVE") and (
        body.punch_in is not None or body.punch_out is not None
    ):
        raise HTTPException(
            422,
            "Supplying punch_in or punch_out when setting status ABSENT/LEAVE is not allowed",
        )

    target_status = body.status if body.status is not None else record["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_in_dt = None
        new_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in (supplied or stored)
        if body.punch_in is not None:
            new_in_dt = epoch_ms_to_dt(body.punch_in)
        else:
            new_in_dt = record.get("punch_in")
            if new_in_dt and new_in_dt.tzinfo is None:
                new_in_dt = new_in_dt.replace(tzinfo=timezone.utc)

        if new_in_dt is None:
            raise HTTPException(
                422,
                "Presence status requires a punch_in timestamp",
            )

        # punch_in must stay on the record's attendance date per R1
        calc_date = get_attendance_date(new_in_dt, emp["shift_start"], emp["shift_end"])
        if calc_date != date_str:
            raise HTTPException(
                422,
                f"punch_in instant belongs to attendance date {calc_date}, not {date_str}",
            )

        if body.punch_out is not None:
            new_out_dt = epoch_ms_to_dt(body.punch_out)
        else:
            new_out_dt = record.get("punch_out")
            if new_out_dt and new_out_dt.tzinfo is None:
                new_out_dt = new_out_dt.replace(tzinfo=timezone.utc)

        if new_out_dt is not None:
            if new_out_dt <= new_in_dt:
                raise HTTPException(
                    422, "punch_out must be strictly after punch_in"
                )
            if (new_out_dt - new_in_dt).total_seconds() > 24 * 3600:
                raise HTTPException(
                    422,
                    "punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_in_dt, new_out_dt)
            new_overtime_minutes = compute_overtime_minutes(
                new_out_dt, emp["shift_start"], emp["shift_end"], date_str
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(new_in_dt, emp["shift_start"], date_str)

    # Compute changes between old and new state
    old_in_dt = record.get("punch_in")
    if old_in_dt and old_in_dt.tzinfo is None:
        old_in_dt = old_in_dt.replace(tzinfo=timezone.utc)

    old_out_dt = record.get("punch_out")
    if old_out_dt and old_out_dt.tzinfo is None:
        old_out_dt = old_out_dt.replace(tzinfo=timezone.utc)

    old_status = record.get("status")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes: Dict[str, Dict[str, Any]] = {}

    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}

    old_in_ms = dt_to_epoch_ms(old_in_dt)
    new_in_ms = dt_to_epoch_ms(new_in_dt)
    if old_in_ms != new_in_ms:
        changes["punch_in"] = {"from": old_in_dt, "to": new_in_dt}

    old_out_ms = dt_to_epoch_ms(old_out_dt)
    new_out_ms = dt_to_epoch_ms(new_out_dt)
    if old_out_ms != new_out_ms:
        changes["punch_out"] = {"from": old_out_dt, "to": new_out_dt}

    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}

    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}

    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {
            "from": old_overtime_minutes,
            "to": new_overtime_minutes,
        }

    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if len(changes) == 0:
        raise HTTPException(
            422, "A request that changes nothing is invalid"
        )

    now_dt = truncate_dt_to_seconds(datetime.now(timezone.utc))
    history_entry = {
        "at": now_dt,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic locking: ensure history size matches to avoid lost updates
    existing_history = record.get("history") or []
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(existing_history)},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_in_dt,
                "punch_out": new_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            409, "Record was modified concurrently; please retry"
        )

    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Pipelines Builders for Analytics & Explain
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
            }
        },
        {
            "$project": {
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    # Last day of month
    y, m = map(int, month.split("-"))
    last_d = calendar.monthrange(y, m)[1]
    end_of_month = f"{month}-{last_d:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_of_month}}
    if department is not None:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", f"{month}-31"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_present_days": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$$this.day_of_week",
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                                {
                                                    "$in": [
                                                        "$$this.status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$$this.half_day", True]},
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "emp_late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "emp_total_late_minutes": {
                    "$reduce": {
                        "input": "$logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.late_minutes"]},
                    }
                },
                "emp_leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "emp_on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
                "emp_work_hours_sum": {
                    "$reduce": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$$l.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$$l.work_hours", None]},
                                    ]
                                },
                            }
                        },
                        "initialValue": 0.0,
                        "in": {"$add": ["$$value", "$$this.work_hours"]},
                    }
                },
                "emp_work_hours_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$and": [
                                    {
                                        "$in": [
                                            "$$l.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$$l.work_hours", None]},
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present_days"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_total_late_minutes"},
                "leave_count": {"$sum": "$emp_leave_count"},
                "on_duty_count": {"$sum": "$emp_on_duty_count"},
                "total_work_hours": {"$sum": "$emp_work_hours_sum"},
                "total_work_count": {"$sum": "$emp_work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$total_work_count", 0]},
                        {"$divide": ["$total_work_hours", "$total_work_count"]},
                        None,
                    ]
                },
            }
        },
    ]


def build_late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str] = None
) -> List[Dict[str, Any]]:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": f"{month}-31"},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    # Window function to calculate standard competition ranking (1, 2, 2, 4)
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def build_department_trend_pipeline(
    department: str, from_str: str, to_str: str
) -> List[Dict[str, Any]]:
    from_d = date.fromisoformat(from_str)
    to_d = date.fromisoformat(to_str)
    from_dt = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    upper_dt = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": from_str, "$lte": to_str}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, upper_dt],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
            }
        },
        {
            "$addFields": {
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]}
            }
        },
        {"$sort": {"date": 1}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee (aggregation)."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    working_days = count_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month (cross-collection aggregation)."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        avg_wh = (
            round_half_up(r["avg_work_hours"], 2)
            if r.get("avg_work_hours") is not None
            else None
        )
        items.append(
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": float(r["present_days"]),
                "avg_work_hours": avg_wh,
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard with standard competition ranking ties."""
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    """Daily series with gap filling and 7-day moving average."""
    # Validate dates
    try:
        f_d = date.fromisoformat(from_date)
        t_d = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Invalid date format")

    if t_d < f_d:
        raise HTTPException(
            422, "'to' date must not be earlier than 'from' date"
        )

    if (t_d - f_d).days + 1 > 92:
        raise HTTPException(
            422, "Date range must not exceed 92 days"
        )

    # 404 if department has no employees
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    rows = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in rows:
        att_rate = (
            round_half_up(r["attendance_rate"], 4)
            if r.get("attendance_rate") is not None
            else None
        )
        mov_avg = (
            round_half_up(r["moving_avg_7d"], 4)
            if r.get("moving_avg_7d") is not None
            else None
        )
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": int(r["headcount"]),
                "present_count": float(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": att_rate,
                "moving_avg_7d": mov_avg,
            }
        )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    MongoDB explain output for an endpoint's main query or pipeline.
    Must demonstrate index usage (IXSCAN).
    """
    valid_endpoints = [
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    if endpoint not in valid_endpoints:
        raise HTTPException(
            422,
            f"Invalid endpoint. Must be one of: {valid_endpoints}",
        )

    month_pattern = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422, "date_from must not be greater than date_to"
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        sort_obj = {"date": -1, "emp_code": 1}
        raw_explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": sort_obj,
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month or not month_pattern.match(month):
            raise HTTPException(
                422,
                "emp_code and a valid month (YYYY-MM) are required for employee_monthly",
            )
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month or not month_pattern.match(month):
            raise HTTPException(
                422,
                "A valid month (YYYY-MM) is required for department_summary",
            )
        pipeline = build_department_summary_pipeline(month, department)
        raw_explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month or not month_pattern.match(month):
            raise HTTPException(
                422,
                "A valid month (YYYY-MM) is required for late_leaderboard",
            )
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422,
                "department, from, and to dates are required for department_trend",
            )
        try:
            f_d = date.fromisoformat(from_date)
            t_d = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "Invalid date format")

        if t_d < f_d or (t_d - f_d).days + 1 > 92:
            raise HTTPException(
                422, "Invalid date range for department_trend"
            )

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        raw_explain = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw_explain)),
    }

