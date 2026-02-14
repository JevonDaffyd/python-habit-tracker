#!/usr/bin/env python3
import os
import requests
import pandas as pd
from datetime import datetime, timezone, time

TODOIST_TOKEN = os.environ.get("TODOIST_TOKEN")
PROJECT_ID = "6fxHrQ58f8jFXp24"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "food_record.csv")

if not TODOIST_TOKEN:
    print("❌ TODOIST_TOKEN missing")
    raise SystemExit(1)

# Build UTC since/until for "today" (adjust to local timezone if desired)
today_utc = datetime.now(timezone.utc).date()
since = datetime.combine(today_utc, time.min, tzinfo=timezone.utc).isoformat()
until = datetime.combine(today_utc, time.max, tzinfo=timezone.utc).isoformat()

URL = "https://api.todoist.com/api/v1/tasks/completed/by_completion_date"
headers = {"Authorization": f"Bearer {TODOIST_TOKEN}"}
params = {"since": since, "until": until, "limit": 200, "offset": 0}

completed_items = []

print(f"Fetching completed tasks from {URL} for {today_utc.isoformat()} (UTC)...")

while True:
    r = requests.get(URL, headers=headers, params=params, timeout=30)
    if r.status_code == 410:
        # Surface server guidance and exit
        try:
            err = r.json()
            extra = err.get("error_extra", {})
            print("❌ API_DEPRECATED:", extra)
        except Exception:
            print("❌ API_DEPRECATED (no JSON body)")
        raise SystemExit(1)
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

# Load or create CSV
try:
    food_record = pd.read_csv(CSV_PATH)
except FileNotFoundError:
    food_record = pd.DataFrame(columns=["Date", "Food"])

today_str = today_utc.isoformat()
new_entries = []

for it in completed_items:
    # Ensure project matches
    if str(it.get("project_id")) != str(PROJECT_ID):
        continue

    content = (it.get("content") or "").strip()
    if not content:
        continue

    # Option A: dedupe by text+date (keeps your original behavior)
    is_dup = ((food_record["Date"] == today_str) & (food_record["Food"] == content)).any()
    if not is_dup:
        new_entries.append({"Date": today_str, "Food": content})
        print(f"  ✓ Queued: {content}")

# Save
if new_entries:
    new_df = pd.DataFrame(new_entries)
    food_record = pd.concat([food_record, new_df], ignore_index=True)
    food_record.to_csv(CSV_PATH, index=False, encoding="utf-8")
    print(f"✅ Updated {CSV_PATH} with {len(new_entries)} entries")
else:
    print("ℹ No new items to log")
