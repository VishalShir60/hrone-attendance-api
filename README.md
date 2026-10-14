# Employee Attendance & Analytics API

FastAPI service backed by MongoDB that handles attendance logging (punch-in, punch-out, manual corrections with audit history) and analytics aggregations (monthly stats, department summary, late leaderboard, and moving average trends). Conforms to the OpenAPI contract and business rules R1–R10.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (Tested locally on MongoDB 8.0)

## Quickstart

1. **Virtual environment & dependencies:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment configuration:**
   The service reads connection info from standard environment variables:
   - `MONGO_URI` (defaults to `mongodb://localhost:27017`)
   - `MONGO_DB` (defaults to `attendance_db`)

   Any environment variables exported in your shell or injected by the test runner take precedence over `.env`.

3. **Run the server:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The API will be available at `http://localhost:8000`. You can check server and database health at `GET /health`.

## Startup & Indexes

All compound and unique indexes on `employees` and `attendance_logs` are registered automatically inside FastAPI's `lifespan` handler at boot. You don't need to run manual migration scripts or create indexes by hand.

## Running Tests

To run the test suite (covering R1–R10 rules, race conditions, concurrency, and explain plan index scan verification):

```bash
pytest tests/
```

## Repository Structure

- `app/main.py`: The entire API implementation in a single file (endpoints, Pydantic schemas, helpers, and aggregation pipelines).
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect breakdown from reviewing the initial starter code, plus non-defects.
- `DECISIONS.md`: Design and architecture answers (indexes, concurrency, ranking ties, headcount, 100x scale).
