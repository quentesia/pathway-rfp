"""Re-authenticate Gmail OAuth. Run this, click the browser link, done."""
from app.services.email_sender import get_gmail_service

service = get_gmail_service()
print("Auth complete! token.json saved.")
