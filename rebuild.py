#!/usr/bin/env python3
import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, date, timedelta

# --- Config ---
TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN")
if not TODOIST_TOKEN:
    print("❌ Error: TODOIST_TOKEN not set in environment.")
    raise SystemExit(1)

PROJECT_ID = "6fg2294Gpqqj6f79"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_HABIT_RECORD = os.path.join(BASE_DIR, "habit_record.csv")
CSV_HABIT_REFERENCE = os.path.join(BASE_DIR, "habit_reference.csv")

HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type": "application/json",
}

API_BASE = "https://api.todoist.com/api/v1"
URL_TASKS = f"{API_BASE}/tasks"


def with_retries(func, max_attempts=4, base_delay=0.5, *args, **kwargs):
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt >= max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            print(f"Transient error: {e}. Retrying in {delay:.1f}s (attempt {attempt}/{max_attempts})...")
            time.sleep(delay)


def compute_streak_best_pct(habit_name, habit_record_df, is_urgent):
    dates = pd.to_datetime(
        habit_record_df.loc[habit_record_df['Habit'] == habit_name, 'Date'],
        errors='coerce'
    ).dropna().dt.normalize()

    date_set = set(d.date() for d in dates)
    yesterday = date.today() - timedelta(days=1)

    streak = 0
    cur = yesterday
    while cur in date_set:
        streak += 1
        cur -= timedelta(days=1)

    best = 0
    checked = set()
    for d in sorted(date_set):
        if d in checked:
            continue
        length = 0
        cur2 = d
        while cur2 in date_set:
            checked.add(cur2)
            length += 1
            cur2 = cur2 + timedelta(days=1)
        best = max(best, length)

    start = date(2026, 1, 1) if is_urgent else date(2026, 2, 14)
    if yesterday < start:
        total_days = 0
    else:
        total_days = (yesterday - start).days + 1

    days_completed = sum(1 for d in date_set if start <= d <= yesterday)
    pct = int(round((days_completed / total_days) * 100)) if total_days > 0 else 0

    return streak, best, pct, days_completed, total_days


# 1. Load data
print("Loading CSV data...")
try:
    habit_record = pd.read_csv(CSV_HABIT_RECORD)
except FileNotFoundError:
    habit_record = pd.DataFrame(columns=["Date", "Habit"])

try:
    habit_reference = pd.read_csv(CSV_HABIT_REFERENCE)
except FileNotFoundError:
    print(f"❗ {CSV_HABIT_REFERENCE} not found. Create it with a 'Habit' column.")
    raise SystemExit(1)

cols = {c.lower(): c for c in habit_reference.columns}
if "habit" not in cols:
    print("❌ Reference CSV must contain a 'Habit' column")
    raise SystemExit(1)
priority_col = cols.get("priority")

habit_record.to_csv(CSV_HABIT_RECORD, index=False)
habit_reference.to_csv(CSV_HABIT_REFERENCE, index=False)
print("Local CSVs updated.")

# 2. Delete all tasks in project
print("Cleaning and rebuilding Todoist project...")
print("Deleting all tasks in project...")

# 2a. List tasks (active)
try:
    resp = with_retries(
        lambda: requests.get(
            URL_TASKS,
            headers=HEADERS,
            params={"project_id": PROJECT_ID},
            timeout=30,
        )
    )
except requests.exceptions.RequestException as e:
    print("❌ Failed to list existing tasks:", e)
    raise SystemExit(1)

if resp.status_code == 410:
    try:
        err = resp.json()
        extra = err.get("error_extra", {})
        print("❌ Todoist API returned 410 API_DEPRECATED. Details:", json.dumps(extra))
    except Exception:
        print("❌ Todoist API returned 410 API_DEPRECATED (no JSON body).")
    raise SystemExit(1)

resp.raise_for_status()
existing_tasks = resp.json()

if isinstance(existing_tasks, dict):
    if isinstance(existing_tasks.get("results"), list):
        source_list = existing_tasks["results"]
    elif isinstance(existing_tasks.get("items"), list):
        source_list = existing_tasks["items"]
    else:
        found = None
        for v in existing_tasks.values():
            if isinstance(v, list):
                found = v
                break
        source_list = found or []
elif isinstance(existing_tasks, list):
    source_list = existing_tasks
else:
    source_list = []

print(f"DEBUG: normalized source_list length = {len(source_list)}; sample types: {[type(x) for x in source_list[:3]]}")


def extract_task_id(entry):
    if isinstance(entry, dict):
        return entry.get("id") or entry.get("task_id") or entry.get("id_str")
    if isinstance(entry, str):
        return entry
    return None


task_ids = []
for entry in source_list:
    tid = extract_task_id(entry)
    if tid:
        task_ids.append(tid)
    else:
        print("Warning: skipping unexpected task entry:", repr(entry)[:200])

deleted_count = 0
for task_id in task_ids:
    def do_delete():
        return requests.delete(f"{URL_TASKS}/{task_id}", headers=HEADERS, timeout=15)

    try:
        del_resp = with_retries(do_delete, max_attempts=3, base_delay=0.2)
    except requests.exceptions.RequestException as e:
        print(f"Error deleting task {task_id}: {e}")
        continue

    if del_resp.status_code == 410:
        try:
            err = del_resp.json()
            extra = err.get("error_extra", {})
            print("❌ Delete returned 410 API_DEPRECATED. Details:", json.dumps(extra))
        except Exception:
            print("❌ Delete returned 410 API_DEPRECATED (no JSON body).")
        raise SystemExit(1)

    if not (200 <= del_resp.status_code < 300):
        print(f"Warning: delete returned {del_resp.status_code} for task {task_id}: {del_resp.text}")
    else:
        deleted_count += 1

    time.sleep(0.12)

print(f"Deleted {deleted_count} existing tasks (attempted {len(task_ids)}).")

# 2b. Best-effort delete completed tasks
print("Deleting completed tasks (best-effort)...")
resp_completed = requests.get(
    "https://api.todoist.com/sync/v9/completed/get_all",
    headers=HEADERS,
    timeout=30,
)

if resp_completed.status_code == 410:
    print("Warning: completed tasks endpoint returned 410 API_DEPRECATED. Skipping completed-task deletion.")
else:
    resp_completed.raise_for_status()
    completed_raw = resp_completed.json()
    completed_items = completed_raw.get("items", [])

    completed_for_project = [
        item for item in completed_items
        if item.get("project_id") == PROJECT_ID
    ]

    print(f"Found {len(completed_for_project)} completed tasks to delete.")

    for item in completed_for_project:
        task_id = item.get("task_id")
        if not task_id:
            print("Warning: completed item missing task_id:", item)
            continue

        del_resp = requests.delete(
            f"{URL_TASKS}/{task_id}",
            headers=HEADERS,
            timeout=15,
        )
        if 200 <= del_resp.status_code < 300:
            print(f"Deleted completed task {task_id}")
        else:
            print(f"Warning: failed to delete completed task {task_id}: {del_resp.text}")

# 3. Recreate tasks
def create_task(payload):
    return requests.post(URL_TASKS, headers=HEADERS, json=payload, timeout=30)

created_count = 0
for _, row in habit_reference.iterrows():
    habit_text = str(row.get(cols["habit"], "")).strip()
    if not habit_text:
        continue

    if priority_col:
        val = str(row.get(priority_col, "")).strip().lower()
        priority = 4 if val == "urgent" else 1
        is_urgent = (val == "urgent")
    else:
        priority = 1
        is_urgent = False

    streak, best, pct, days_done, total_days = compute_streak_best_pct(
        habit_text, habit_record, is_urgent
    )

    description = (
        f"Streak: {streak} days (max: {best} days), {pct}%\n"
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    payload = {
        "content": habit_text,
        "project_id": PROJECT_ID,
        "priority": int(priority),
        "description": description,
    }

    try:
        c_resp = with_retries(lambda: create_task(payload), max_attempts=3, base_delay=0.2)
    except requests.exceptions.RequestException as e:
        print(f"Error creating task '{habit_text}': {e}")
        continue

    if c_resp.status_code == 410:
        try:
            err = c_resp.json()
            extra = err.get("error_extra", {})
            print("❌ Create returned 410 API_DEPRECATED. Details:", json.dumps(extra))
        except Exception:
            print("❌ Create returned 410 API_DEPRECATED (no JSON body).")
        raise SystemExit(1)

    if 200 <= c_resp.status_code < 300:
        created_count += 1
    else:
        print(f"Warning: create returned {c_resp.status_code} for '{habit_text}': {c_resp.text}")

    time.sleep(0.18)

print(f"✨ Done. Created {created_count} tasks in project {PROJECT_ID}.")
