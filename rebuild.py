#!/usr/bin/env python3
"""
Rebuild Todoist project from local CSVs (safe, robust, /api/v1 endpoints).

- Normalizes different list response shapes (dict with 'results'/'items', list of dicts, list of ids).
- Uses BASE_DIR for CSV paths.
- Handles 410 API_DEPRECATED and surfaces error_extra.
- Adds small retry/backoff for create/delete operations.
"""
import os
import time
import json
import requests
import pandas as pd
from datetime import datetime

# --- Config ---
TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN")
if not TODOIST_TOKEN:
    print("❌ Error: TODOIST_TOKEN not set in environment.")
    raise SystemExit(1)

PROJECT_ID = "6fg2294Gpqqj6f79"
TARGET_GOAL = 30
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_HABIT_RECORD = os.path.join(BASE_DIR, "habit_record.csv")
CSV_HABIT_REFERENCE = os.path.join(BASE_DIR, "habit_reference.csv")

HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type": "application/json",
}

API_BASE = "https://api.todoist.com/api/v1"
URL_TASKS = f"{API_BASE}/tasks"

# --- Utility: exponential backoff wrapper for requests ---
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

# --- 1. LOAD DATA (scheduled before midnight) ---
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

# --- 2. SYNC & CALCULATE PRIORITIES (scheduled after midnight) ---
print("Calculating stats and priorities...")
today = pd.Timestamp.now().normalize()
seven_days_ago = today - pd.Timedelta(days=6)  # last 7 days inclusive

recent_df = habit_record[pd.to_datetime(habit_record['Date']) >= seven_days_ago].copy()
recent_unique_count = recent_df['Habit'].nunique()
remaining_goal = max(TARGET_GOAL - recent_unique_count, 0)

print(f"Habits in last 7 days: {recent_unique_count}")
print(f"Remaining target for today: {remaining_goal}")

# Build stats
if not habit_record.empty:
    stats = habit_record.groupby('Habit').agg(
        Latest_Date=('Date', 'max'),
        Count=('Date', 'count')
    ).reset_index()
else:
    stats = pd.DataFrame(columns=['Habit', 'Latest_Date', 'Count'])

# Ensure reference has expected columns
if 'Last_Date_Eaten' not in habit_reference.columns:
    habit_reference['Last_Date_Eaten'] = pd.NA
if 'Total_Count' not in habit_reference.columns:
    habit_reference['Total_Count'] = 0

stats_indexed = stats.set_index('Habit')
habit_reference['Last_Date_Eaten'] = habit_reference['Habit'].map(
    stats_indexed['Latest_Date']
).fillna(habit_reference['Last_Date_Eaten'])

habit_reference['Total_Count'] = habit_reference['Habit'].map(
    stats_indexed['Count']
).fillna(0).astype(int)

habit_reference['Days_Since_Eaten'] = (
    today - pd.to_datetime(habit_reference['Last_Date_Eaten'])
).dt.days.fillna(999).astype(int)

def get_priority(days):
    if days >= 6:
        return 4
    if days == 5:
        return 3
    if 3 <= days <= 4:
        return 2
    return 1

habit_reference['Todoist_Priority'] = habit_reference['Days_Since_Eaten'].apply(get_priority)
habit_reference = habit_reference.sort_values(by=['Todoist_Priority', 'Total_Count'], ascending=[False, False])

# --- 3. SAVE PROGRESS (persist updated reference) ---
habit_record.to_csv(CSV_HABIT_RECORD, index=False)
habit_reference.to_csv(CSV_HABIT_REFERENCE, index=False)
print("Local CSVs updated.")

# --- 4. REBUILD TODOIST PROJECT (use /api/v1 endpoints) ---
print("Cleaning and rebuilding Todoist project...")

# 4a. List existing tasks in the project (active tasks)
try:
    resp = with_retries(lambda: requests.get(URL_TASKS, headers=HEADERS, params={"project_id": PROJECT_ID}, timeout=30))
except requests.exceptions.RequestException as e:
    print("❌ Failed to list existing tasks:", e)
    raise SystemExit(1)

# Handle 410 API_DEPRECATED explicitly
if resp.status_code == 410:
    try:
        err = resp.json()
        extra = err.get("error_extra", {})
        print("❌ Todoist API returned 410 API_DEPRECATED. Details:", json.dumps(extra))
    except Exception:
        print("❌ Todoist API returned 410 API_DEPRECATED (no JSON body).")
    raise SystemExit(1)

try:
    resp.raise_for_status()
except requests.exceptions.HTTPError as e:
    print("❌ HTTP error when listing tasks:", e)
    print("Response body:", resp.text)
    raise SystemExit(1)

existing_tasks = resp.json()

# --- 4b. Normalize response shape to a list of task entries ---
if isinstance(existing_tasks, dict):
    if isinstance(existing_tasks.get("results"), list):
        source_list = existing_tasks["results"]
    elif isinstance(existing_tasks.get("items"), list):
        source_list = existing_tasks["items"]
    else:
        # find first list value if present
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

# Debugging info (helpful in CI logs)
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
        print("Warning: skipping unexpected task entry (not dict or id string):", repr(entry)[:200])

# 4c. Delete existing tasks (tolerant of different delete status codes)
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

# 4d. Create parent task (high priority summary)
parent_payload = {
    "content": f"Eat {remaining_goal} plant foods today ({datetime.now().strftime('%d %b')})",
    "project_id": PROJECT_ID,
    "due_string": "today",
    "priority": 4
}

def create_task(payload):
    return requests.post(URL_TASKS, headers=HEADERS, json=payload, timeout=30)

try:
    parent_resp = with_retries(lambda: create_task(parent_payload), max_attempts=4, base_delay=0.3)
except requests.exceptions.RequestException as e:
    print("❌ Failed to create parent task:", e)
    raise SystemExit(1)

if parent_resp.status_code == 410:
    try:
        err = parent_resp.json()
        extra = err.get("error_extra", {})
        print("❌ Create parent returned 410 API_DEPRECATED. Details:", json.dumps(extra))
    except Exception:
        print("❌ Create parent returned 410 API_DEPRECATED (no JSON body).")
    raise SystemExit(1)

try:
    parent_resp.raise_for_status()
except requests.exceptions.HTTPError as e:
    print("❌ Parent create HTTP error:", e)
    print("Response body:", parent_resp.text)
    raise SystemExit(1)

parent_task = parent_resp.json()
parent_id = parent_task.get("id")
if not parent_id:
    print("❌ Parent task created but no id returned:", parent_resp.text)
    raise SystemExit(1)

# 4e. Create child tasks from reference sheet
created_count = 0
for _, row in food_reference.iterrows():
    content = str(row.get('Food', '')).strip()
    if not content:
        continue
    child_payload = {
        "content": content,
        "project_id": PROJECT_ID,
        "parent_id": parent_id,
        "priority": int(row.get('Todoist_Priority', 1))
    }
    try:
        c_resp = with_retries(lambda: create_task(child_payload), max_attempts=3, base_delay=0.2)
    except requests.exceptions.RequestException as e:
        print(f"Error creating task '{content}': {e}")
        continue

    if c_resp.status_code == 410:
        try:
            err = c_resp.json()
            extra = err.get("error_extra", {})
            print("❌ Create child returned 410 API_DEPRECATED. Details:", json.dumps(extra))
        except Exception:
            print("❌ Create child returned 410 API_DEPRECATED (no JSON body).")
        raise SystemExit(1)

    if not (200 <= c_resp.status_code < 300):
        print(f"Warning: create child returned {c_resp.status_code} for '{content}': {c_resp.text}")
    else:
        created_count += 1
    time.sleep(0.18)

print(f"✨ Done. Created {created_count} child tasks under parent {parent_id}.")
