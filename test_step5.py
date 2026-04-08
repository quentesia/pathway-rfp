print("Starting test...")
"""Run Step 5 directly against existing DB to monitor inbox responses."""
from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.services.inbox_monitor import collect_quotes

load_dotenv()
init_db()

session = SessionLocal()
try:
    print("\n" + "=" * 60)
    print("STEP 5: Monitoring Inbox for Quotes (Dry Run)")
    print("=" * 60)
    
    # Executes the inbox monitoring
    collect_quotes(session, restaurant_id=1, mock_recipient="demo")
    
finally:
    session.close()
