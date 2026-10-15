# DECISIONS.md

1. **Indexes.** I create all of them at startup. The two unique ones protect the data, the rest make queries fast.
   - `employees.emp_code` (unique): two employees can never get the same code, and finding one employee is quick.
   - `employees.department + emp_code`: listing a department's employees, already sorted by code.
   - `employees.joined_on`: the summary only counts people who joined before the month ended.
   - `attendance_logs.emp_code + date` (unique): one record per person per day. Also used for one employee's monthly stats.
   - `attendance_logs.date + emp_code`: the attendance list is sorted this way, and the leaderboard reads one month by date.
   - `attendance_logs.emp_code + punch_in`: punch-out finds the person's latest punch-in, even if it was yesterday (night shift).
   - `attendance_logs.status + date + emp_code`: the attendance list filtered by status.

   I rejected an index on `status` alone. It has only five values, so it would still leave thousands of records to check.

2. **Punch-in race.** Both requests pass validation and both call `insert_one`. There is no "check first" step. The unique `emp_code + date` index lets only one insert win. The other gets `DuplicateKeyError` and I return 409.

3. **Ties.** `$rank` gives tied people the same rank and skips the next one (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even past `limit` rows.

4. **Headcount.** The pipeline starts from `employees` with `joined_on <= month end`, then left-joins the logs with `preserveNullAndEmptyArrays`. Each employee counts once, with or without logs.

5. **100x data.** I would keep daily department totals updated on every write, and store `department` on each log so the summary does not need a lookup per employee.
