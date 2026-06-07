#!/usr/bin/env python3
"""
collect_habit_v1.py

Habit collector that mirrors the working v1 logic used by the food collector.
It fetches completed tasks for today using the Todoist v1 endpoint:
  https://api.todoist.com/api/v1/tasks/completed/by_completion_date

Saves new completions to habit_record.csv with columns:
  Date, Habit, TaskId, CompletedAt, Source

Configuration:
- TODOIST_TOKEN environment variable must be set.
- PROJECT_ID should be your habit project id.
- Script uses UTC "today" window (same as your food collector).
"""

import os
import sys
import requests
import pandas as pd
from datetime import datetime, timezone, time as dt_time

# --- Configuration (adjust PROJECT_ID if needed) ---
TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN")
PROJECT_ID = "6fg2294Gpqqj6f79"   # your habit project id (keep as in your v2 script)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "habit_record.csv")

if not TODOIST_TOKEN:
    print("❌ TODOIST_TOKEN missing")
    sys.exit(1)

# Build UTC since/until for "today"
today_utc = datetime.now(timezone.utc).date()
since = datetime.combine(today_utc, dt_time.min, tzinfo=timezone.utc).isoformat()
until = datetime.combine(today_utc, dt_time.max, tzinfo=timezone.utc).isoformat()

URL = "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
headers = {"Authorization": f"Bearer {TODOIST_TOKEN}"}
params = {"since": since, "until": until, "limit": 200, "offset": 0}

completed_items = []

print(f"Fetching completed tasks from {URL} for {today_utc.isoformat()} (UTC)...")

# Page through results (v1 uses offset/limit)
while True:
    r = requests.get(URL, headers=headers, params=params, timeout=30)

    # If the endpoint is deprecated, surface the message and exit (same as food script)
    if r.status_code == 410:
        try:
            err = r.json()
            extra = err.get("error_extra", {})
            print("❌ API_DEPRECATED:", extra)
        except Exception:
            print("❌ API_DEPRECATED (no JSON body)")
        sys.exit(1)

    # Raise for other HTTP errors
    r.raise_for_status()

    data = r.json()
    items = data.get("items", [])
    if not items:
        break

    completed_items.extend(items)

    # pagination: advance offset; stop if fewer than limit returned
    returned = len(items)
    if returned < params["limit"]:
        break
    params["offset"] += returned

print(f"✅ Retrieved {len(completed_items)} completed items (raw)")

# Load or create CSV with required columns
try:
    habit_record = pd.read_csv(CSV_PATH)
except FileNotFoundError:
    habit_record = pd.DataFrame(columns=["Date", "Habit", "TaskId", "CompletedAt", "Source"])

# Ensure required columns exist (non-destructive)
required_columns = ["Date", "Habit", "TaskId", "CompletedAt", "Source"]
for col in required_columns:
    if col not in habit_record.columns:
        habit_record[col] = ""

today_str = today_utc.isoformat()
new_entries = []

for it in completed_items:
    # Ensure project matches (v1 returns project_id as int or string)
    if str(it.get("project_id")) != str(PROJECT_ID):
        continue

    content = (it.get("content") or "").strip()
    if not content:
        continue

    # Capture task id and completion timestamp if present
    task_id = str(it.get("id") or "")
    completed_at = it.get("completed_at") or it.get("completed_date") or ""

    # Dedupe by text+date to mirror the working food collector behavior
    is_dup = ((habit_record["Date"] == today_str) & (habit_record["Habit"] == content)).any()
    if not is_dup:
        new_entries.append({
            "Date": today_str,
            "Habit": content,
            "TaskId": task_id,
            "CompletedAt": completed_at,
            "Source": "todoist-v1"
        })
        print(f"  ✓ Queued: {content} (id={task_id})")

# Save
if new_entries:
    new_df = pd.DataFrame(new_entries)
    habit_record = pd.concat([habit_record, new_df], ignore_index=True)
    habit_record.to_csv(CSV_PATH, index=False, encoding="utf-8")
    print(f"✅ Updated {CSV_PATH} with {len(new_entries)} entries")
else:
    print("ℹ No new items to log")

