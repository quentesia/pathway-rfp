"""Run Step 4 directly against existing DB (steps 1-3 must be done)."""
from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.services.email_sender import send_rfp_emails, _make_yopmail
from app.models import Distributor

load_dotenv()
init_db()

session = SessionLocal()
try:
    # Reset rfp_status for all distributors
    for d in session.query(Distributor).all():
        d.rfp_status = "pending"
        d.rfp_sent_at = None
    session.commit()
    print("Reset all distributor RFP statuses.\n")

    processed = send_rfp_emails(session, restaurant_id=1, mock_recipient="demo")

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for dist in processed:
        if dist.rfp_status == "sent":
            recipient = _make_yopmail(dist.name)
            print(f"  [{dist.rfp_status}] {dist.name} -> {recipient}")
        elif dist.rfp_status == "form_ready":
            form_url = dist.email[5:] if dist.email and dist.email.startswith("form:") else "?"
            print(f"  [{dist.rfp_status}] {dist.name} -> {form_url}")
        else:
            print(f"  [{dist.rfp_status}] {dist.name}")
finally:
    session.close()
