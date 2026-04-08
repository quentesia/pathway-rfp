"""Run Step 4 directly against existing DB (steps 1-3 must be done)."""
from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.services.email_sender import send_rfp_emails, _make_yopmail
from app.models import Distributor, RFPEmail

load_dotenv()
init_db()

session = SessionLocal()
try:
    # Clear previous Step 4 results
    deleted = session.query(RFPEmail).delete()
    session.commit()
    if deleted:
        print(f"Cleared {deleted} previous RFP email records.\n")

    emails = send_rfp_emails(session, restaurant_id=1, mock_recipient="demo")

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    for e in emails:
        dist = session.query(Distributor).get(e.distributor_id)
        if e.status == "sent":
            recipient = _make_yopmail(dist.name)
            print(f"  [{e.status}] {dist.name} → {recipient}")
            print(f"    Subject: {e.subject}")
            body_preview = e.body.replace('\n', '\n      ')
            print(f"    Body: \n      {body_preview}")
        elif e.status in ("form_ready", "form_submitted", "form_failed"):
            form_url = dist.email[5:] if dist.email and dist.email.startswith("form:") else "?"
            print(f"  [{e.status}] {dist.name} → {form_url}")
            print(f"    Subject: {e.subject}")
            body_preview = e.body.replace('\n', '\n      ')
            print(f"    Message Payload: \n      {body_preview}")
        else:
            print(f"  [{e.status}] {dist.name}")
finally:
    session.close()
