#!/usr/bin/env python3
"""
Rebuild Todoist project from CSVs using the reliable individual task deletion model.
Updated for Todoist API v1.
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

PROJECT_ID = "6fxHrQ58f8jFXp24"
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
            print(f"Transient error: {e}. Retrying in {delay:.1f}s...")
            time.sleep(delay)


# --- Streak logic unchanged ---
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


# --- 1. Load CSVs ---
print("Loading CSV data...")
try:
    habit_record = pd.read_csv(CSV_HABIT_RECORD)
except FileNotFoundError:
    habit_record = pd.DataFrame(columns=["Date", "Habit"])

try:
    habit_reference = pd.read_csv(CSV_HABIT_REFERENCE)
except FileNotFoundError:
    print(f"❗ {CSV_HABIT_REFERENCE} not found.")
    raise SystemExit(1)

cols = {c.lower(): c for c in habit_reference.columns}
priority_col = cols.get("priority")

habit_record.to_csv(CSV_HABIT_RECORD, index=False)
habit_reference.to_csv(CSV_HABIT_REFERENCE, index=False)
print("Local CSVs updated.")

import os
import requests

# TEMPORARY DIAGNOSTIC BLOCK
print("--- RUNNING ENVIRONMENT DIAGNOSTICS ---")
TOKEN = os.getenv("TODOIST_TOKEN")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# 1. Verify the Account Owner
try:
    user_info = requests.get("https://api.todoist.com/rest/v2/user", headers=HEADERS).json()
    print(f"👉 TOKEN OWNER: Account is registered to [{user_info.get('email')}] (Name: {user_info.get('name')})")
except Exception as e:
    print(f"❌ Failed to fetch user info: {e}")

# 2. Verify the Target Project
TARGET_PROJECT_ID = "6fxHrQ58f8jFXp24" 
try:
    proj_info = requests.get(f"https://api.todoist.com/rest/v2/projects/{TARGET_PROJECT_ID}", headers=HEADERS)
    if proj_info.status_code == 200:
        print(f"👉 TARGET PROJECT: ID maps to project named ['{proj_info.json().get('name')}']")
    else:
        print(f"❌ TARGET PROJECT: Server returned status {proj_info.status_code} for this project ID.")
except Exception as e:
    print(f"❌ Failed to fetch project details: {e}")

# 3. Peek at the "50 Tasks" before they get deleted
try:
    # Use the exact same task retrieval URL/syntax your current script uses here:
    tasks_resp = requests.get("https://api.todoist.com/rest/v2/tasks", headers=HEADERS, params={"project_id": TARGET_PROJECT_ID})
    tasks = tasks_resp.json()
    print(f"👉 TASK AUDIT: Found {len(tasks)} tasks total.")
    print("👉 SAMPLE OF TASKS FOUND:")
    for t in tasks[:5]: # Prints the first 5 tasks it intends to delete
        print(f"   - Text: '{t.get('content')}' | Belonging to Project ID: {t.get('project_id')}")
except Exception as e:
    print(f"❌ Failed to audit tasks: {e}")
print("---------------------------------------")

# --- 2. Delete ALL tasks (individual deletion) ---
print("Cleaning and rebuilding Todoist project...")

# 2a. List existing tasks in the project
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

# 2b. Normalize response shape to a list of task entries
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

# 2c. Delete existing tasks (tolerant of different delete status codes)
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


# --- 3. Create NEW parent task ---
parent_payload = {
    "content": f"Habits for {datetime.now().strftime('%d %b')}",
    "project_id": PROJECT_ID,
    "due_string": "today",
    "priority": 4
}

def create_task(payload):
    return requests.post(URL_TASKS, headers=HEADERS, json=payload, timeout=30)

try:
    parent_resp = with_retries(lambda: create_task(parent_payload))
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

parent_id = parent_resp.json().get("id")
if not parent_id:
    print("❌ Parent task created but no ID returned.")
    raise SystemExit(1)

print(f"Created parent task {parent_id}")


# --- 4. Create subtasks (habits) ---
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
        f"Streak: {streak} days (max: {best}), {pct}%\n"
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )

    payload = {
        "content": habit_text,
        "project_id": PROJECT_ID,
        "parent_id": parent_id,
        "priority": priority,
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
            print("❌ Create child returned 410 API_DEPRECATED. Details:", json.dumps(extra))
        except Exception:
            print("❌ Create child returned 410 API_DEPRECATED (no JSON body).")
        raise SystemExit(1)

    if 200 <= c_resp.status_code < 300:
        created_count += 1
    else:
        print(f"Warning: failed to create {habit_text}: {c_resp.text}")

    time.sleep(0.18)

print(f"✨ Done. Created {created_count} habit subtasks.")
