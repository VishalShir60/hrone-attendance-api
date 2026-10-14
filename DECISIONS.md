# DECISIONS.md

1. **Indexes.**
I created unique `emp_code` on `employees` and `(emp_code, date)` on `attendance_logs` for integrity. To avoid in-memory sorts, I added `(department, joined_on)` on employees, plus `(date, emp_code)`, `(status, date, emp_code)`, and `(date, late_minutes, emp_code)` on logs. I rejected an index on `punch_in` because queries always filter on date and employee code first; indexing raw timestamps adds write overhead without helping query plans.

2. **Punch-in race.**
If two identical punch-in requests arrive simultaneously, both calculate the same IST date and run `insert_one`. The unique index on `(emp_code, date)` guarantees atomicity: the first insert succeeds (201), while the second triggers a `DuplicateKeyError`. My code catches this and returns a clean 409 Conflict.

3. **Ties.**
Using `$setWindowFields` with `$rank` (1-2-2-4 competition ranking), I filter `rank <= limit`. If a tie lands on the cutoff, all tied employees are returned. Dropping someone arbitrarily when they have identical late minutes would be incorrect.

4. **Headcount.**
The pipeline starts on `employees` (`joined_on <= month_end`), then `$lookup`s `attendance_logs`. Grouping happens on employee records, so staff with zero logs still count toward department headcount.

5. **One thing you would change for 100x the data.**
Precompute daily department rollups during punch-out instead of scanning raw logs on every request, and shard `attendance_logs` by hashed `emp_code`.
