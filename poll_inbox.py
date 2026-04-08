"""Poll inbox for quote replies every 30 min during business hours ±2 hrs.

Runs 7 AM – 7 PM local time. Outside that window, sleeps until next window.
Usage: python poll_inbox.py [restaurant_id]
"""

import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.services.inbox_monitor import collect_quotes

load_dotenv()
init_db()

POLL_INTERVAL = 30 * 60  # 30 minutes
BIZ_START = 7   # 7 AM (business hours 9 AM minus 2)
BIZ_END = 19    # 7 PM (business hours 5 PM plus 2)

restaurant_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1


def in_business_window() -> bool:
    return BIZ_START <= datetime.now().hour < BIZ_END


def seconds_until_next_window() -> int:
    now = datetime.now()
    if now.hour >= BIZ_END:
        # Next window is tomorrow morning
        tomorrow = now.replace(hour=BIZ_START, minute=0, second=0)
        tomorrow = tomorrow.replace(day=now.day + 1)
        return int((tomorrow - now).total_seconds())
    else:
        # Next window is this morning
        start = now.replace(hour=BIZ_START, minute=0, second=0)
        return int((start - now).total_seconds())


print(f"Inbox poller started for restaurant {restaurant_id}")
print(f"Polling every {POLL_INTERVAL // 60} min, {BIZ_START}:00–{BIZ_END}:00")

while True:
    if not in_business_window():
        wait = seconds_until_next_window()
        print(f"Outside business hours. Sleeping {wait // 3600}h {(wait % 3600) // 60}m until next window.")
        time.sleep(wait)
        continue

    print(f"\n[{datetime.now().strftime('%H:%M')}] Checking inbox...")
    session = SessionLocal()
    try:
        updated = collect_quotes(session, restaurant_id, mock_recipient="demo")
        print(f"  {len(updated)} prices updated.")
    except Exception as e:
        print(f"  Error: {e}")
    finally:
        session.close()

    time.sleep(POLL_INTERVAL)
