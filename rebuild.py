#!/usr/bin/env python3
"""
Rebuild Todoist project from CSVs.

- Normalizes different list response shapes (dict with 'results'/'items', list of dicts, list of ids).
- Uses BASE_DIR for CSV paths.
- Handles 410 API_DEPRECATED and surfaces error_extra.
- Adds small retry/backoff for create/delete operations.
- Adds streak, best streak, and percentage to task descriptions.
"""
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

# --- Define exponential backoff wrapper for requests ---
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

# --- Define task info: streak, best streak, percentage ---
def compute_streak_best_pct(habit_name, habit_record_df, is_urgent):
    # Normalise dates
    dates = pd.to_datetime(
        habit_record_df.loc[habit_record_df['Habit'] == habit_name, 'Date'],
        errors='coerce'
    ).dropna().dt.normalize()

    date_set = set(d.date() for d in dates)
    yesterday = date.today() - timedelta(days=1)

    # Streak ending yesterday
    streak = 0
    cur = yesterday
    while cur in date_set:
        streak += 1
        cur -= timedelta(days=1)

    # Best streak
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

    # Percentage
    start = date(2026, 1, 1) if is_urgent else date(2026, 2, 14)
    if yesterday < start:
        total_days = 0
    else:
        total_days = (yesterday - start).days + 1

    days_completed = sum(1 for d in date_set if start <= d <= yesterday)
    pct = int(round((days_completed / total_days) * 100)) if total_days > 0 else 0

    return streak, best, pct, days_completed, total_days

# --- 1. LOAD DATA ---
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

# Ensure reference has expected columns
cols = {c.lower(): c for c in habit_reference.columns}
if "habit" not in cols:
    print("❌ Reference CSV must contain a 'Habit' column")
    raise SystemExit(1)
priority_col = cols.get("priority")

# --- 2. SAVE CSVs ---
habit_record.to_csv(CSV_HABIT_RECORD, index=False)
habit_reference.to_csv(CSV_HABIT_REFERENCE, index=False)
print("Local CSVs updated.")

# --- 3. REBUILD TODOIST PROJECT ---
print("Cleaning and rebuilding Todoist project...")

print("Deleting all tasks in project...")

# 3a. Get all tasks in the project
resp = requests.get(
    URL_TASKS,
    headers=HEADERS,
    params={"project_id": PROJECT_ID},
    timeout=30
)
resp.raise_for_status()

raw = resp.json()

# Normalise to a list of task dicts
if isinstance(raw, dict):
    # Todoist sometimes returns {"items": [...]}
    if "items" in raw:
        tasks = raw["items"]
    elif "results" in raw:
        tasks = raw["results"]
    else:
        # Unexpected dict shape → treat as empty
        print("Warning: unexpected task response shape:", raw)
        tasks = []
elif isinstance(raw, list):
    # Ensure each element is a dict
    tasks = [t for t in raw if isinstance(t, dict)]
else:
    print("Warning: unexpected task response type:", type(raw), raw)
    tasks = []

# 3b. Delete each task
for task in tasks:
    task_id = task.get("id")
    if not task_id:
        print("Warning: task missing 'id':", task)
        continue

    del_resp = requests.delete(
        f"{URL_TASKS}/{task_id}",
        headers=HEADERS,
        timeout=15
    )
    if 200 <= del_resp.status_code < 300:
        print(f"Deleted task {task_id}")
    else:
        print(f"Warning: failed to delete {task_id}: {del_resp.text}")

# --- Delete completed tasks ---
print("Deleting completed tasks...")

resp_completed = requests.get(
    "https://api.todoist.com/sync/v9/completed/get_all",
    headers=HEADERS,
    timeout=30
)
resp_completed.raise_for_status()

completed_raw = resp_completed.json()
completed_items = completed_raw.get("items", [])

# Filter only tasks belonging to this project
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
        timeout=15
    )
    if 200 <= del_resp.status_code < 300:
        print(f"Deleted completed task {task_id}")
    else:
        print(f"Warning: failed to delete completed task {task_id}: {del_resp.text}")

# 3c. Create tasks with description
def create_task(payload):
    return requests.post(URL_TASKS, headers=HEADERS, json=payload, timeout=30)

created_count = 0
for _, row in habit_reference.iterrows():
    habit_text = str(row.get(cols["habit"], "")).strip()
    if not habit_text:
        continue

    # Priority
    if priority_col:
        val = str(row.get(priority_col, "")).strip().lower()
        priority = 4 if val == "urgent" else 1
        is_urgent = (val == "urgent")
    else:
        priority = 1
        is_urgent = False

    # Compute streaks + percentage
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
        "description": description
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
