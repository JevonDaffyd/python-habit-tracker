#!/usr/bin/env python3
import os
import requests
import pandas as pd
from datetime import datetime, timezone, time
import sys

TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN")
PROJECT_ID = "6fg2294Gpqqj6f79"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "habit_record.csv")

if not TODOIST_TOKEN:
    print("❌ TODOIST_TOKEN missing")
    sys.exit(1)

# Build UTC since/until for "today"
today_utc = datetime.now(timezone.utc).date()
since = datetime.combine(today_utc, time.min, tzinfo=timezone.utc).isoformat()
until = datetime.combine(today_utc, time.max, tzinfo=timezone.utc).isoformat()

# Updated to Sync API v9
URL = "https://api.todoist.com/sync/v9/completed/get_all"
headers = {"Authorization": f"Bearer {TODOIST_TOKEN}"}
params = {
    "project_id": PROJECT_ID,
    "since": since,
    "until": until,
    "limit": 200,
    "offset": 0
}

completed_items = []

print(f"Fetching completed tasks from {URL} for {today_utc.isoformat()} (UTC)...")

try:
    while True:
        r = requests.get(URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()

        data = r.json()
        items = data.get("items", [])
        if not items:
            break

        completed_items.extend(items)

        # Pagination: stop if fewer than limit returned
        returned = len(items)
        if returned < params["limit"]:
            break
        params["offset"] += returned

except requests.exceptions.HTTPError as e:
    if e.response.status_code == 410:
        print("❌ API_DEPRECATED: Endpoint is no longer available")
    else:
        print(f"❌ API Error: {e.response.status_code} - {e.response.text}")
    sys.exit(1)
except requests.exceptions.RequestException as e:
    print(f"❌ Request failed: {e}")
    sys.exit(1)

print(f"✅ Retrieved {len(completed_items)} completed items (raw)")

# Load or create CSV with proper columns
try:
    habit_record = pd.read_csv(CSV_PATH)
except FileNotFoundError:
    habit_record = pd.DataFrame(columns=["Date", "Habit", "TaskId", "CompletedAt", "Source"])

# Ensure required columns exist
required_columns = ["Date", "Habit", "TaskId", "CompletedAt", "Source"]
for col in required_columns:
    if col not in habit_record.columns:
        habit_record[col] = ""

today_str = today_utc.isoformat()
new_entries = []

for it in completed_items:
    content = (it.get("content") or "").strip()
    if not content:
        continue

    # Capture task id and completion timestamp
    task_id = str(it.get("id") or "")
    completed_at = it.get("completed_at") or ""

    # Dedupe by TaskId if available, otherwise by text+date
    if task_id:
        is_dup = (habit_record["TaskId"].astype(str) == task_id).any()
    else:
        is_dup = ((habit_record["Date"] == today_str) & (habit_record["Habit"] == content)).any()

    if not is_dup:
        new_entries.append({
            "Date": today_str,
            "Habit": content,
            "TaskId": task_id,
            "CompletedAt": completed_at,
            "Source": "todoist"
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
