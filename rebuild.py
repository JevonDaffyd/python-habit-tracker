#!/usr/bin/env python3
"""
Rebuild Todoist project from CSVs using the reliable parent-task cascade deletion model.
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


# --- 2. Delete ALL tasks by deleting the parent (cascade delete) ---
print("Cleaning and rebuilding Todoist project...")

# Fetch all tasks (active only, but parent deletion handles completed)
resp = with_retries(lambda: requests.get(
    URL_TASKS,
    headers=HEADERS,
    params={"project_id": PROJECT_ID},
    timeout=30
))
resp.raise_for_status()

tasks = resp.json()
if not isinstance(tasks, list):
    tasks = []

print(f"Fetched {len(tasks)} tasks")
for t in tasks:
    print(f"id={t['id']} content={t['content']} parent_id={t.get('parent_id')}")

# Identify parent task (the one with no parent_id)
parent_candidates = [t for t in tasks if not t.get("parent_id")]

if len(parent_candidates) > 1:
    print("⚠ Multiple parent tasks found. Deleting all top-level tasks.")
    parent_ids = [t["id"] for t in parent_candidates]
else:
    parent_ids = [t["id"] for t in parent_candidates] if parent_candidates else []

# Delete parent(s) → Todoist cascades and deletes ALL subtasks (including completed)
for pid in parent_ids:
    print(f"Deleting parent task {pid} (cascade delete)...")
    del_resp = with_retries(lambda: requests.delete(
        f"{URL_TASKS}/{pid}",
        headers=HEADERS,
        timeout=15
    ))
    print(f"Delete status: {del_resp.status_code}")

print("All tasks deleted via cascade.")


# --- 3. Create NEW parent task ---
parent_payload = {
    "content": f"Habits for {datetime.now().strftime('%d %b')}",
    "project_id": PROJECT_ID,
    "priority": 4
}

parent_resp = with_retries(lambda: requests.post(
    URL_TASKS,
    headers=HEADERS,
    json=parent_payload,
    timeout=30
))
parent_resp.raise_for_status()

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

    c_resp = with_retries(lambda: requests.post(
        URL_TASKS,
        headers=HEADERS,
        json=payload,
        timeout=30
    ))

    if 200 <= c_resp.status_code < 300:
        created_count += 1
    else:
        print(f"Warning: failed to create {habit_text}: {c_resp.text}")

    time.sleep(0.18)

print(f"✨ Done. Created {created_count} habit subtasks.")
