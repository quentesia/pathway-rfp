"""Step 4: Compose and send RFP emails to distributors via Gmail API."""

import os
import re
import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import mechanicalsoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app.models import (
    Distributor, DistributorIngredient, Ingredient, Restaurant,
    RFPEmail, USDAPrice,
)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

CREDENTIALS_DIR = Path(__file__).parent.parent.parent
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
CREDENTIALS_PATH = CREDENTIALS_DIR / "credentials.json"


def get_gmail_service():
    """Authenticate and return Gmail API service. Reuses OAuth flow from email-sentiment repo."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_PATH}. "
                    "Copy it from email-sentiment-analysis-reply/."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def compose_rfp_body(
    restaurant: Restaurant,
    distributor: Distributor,
    ingredients_with_info: list[tuple[Ingredient, str | None, str]],
    quote_deadline_days: int = 7,
) -> tuple[str, str]:
    """
    Compose an RFP email subject + body.
    ingredients_with_info: list of (Ingredient, usda_match_name, unit)
    """
    deadline = datetime.now(timezone.utc) + timedelta(days=quote_deadline_days)
    deadline_str = deadline.strftime("%B %d, %Y")

    lines = []
    for ing, usda_name, unit in ingredients_with_info:
        ref = f" (ref: {usda_name})" if usda_name else ""
        lines.append(f"  - {ing.name}{ref}")

    ingredient_list = "\n".join(lines)

    subject = f"Request for Proposal — Ingredient Pricing for {restaurant.name}"

    body = f"""Dear {distributor.name} Team,

We are reaching out on behalf of {restaurant.name} ({restaurant.location}) to request
competitive pricing on the following ingredients for our upcoming procurement cycle.

INGREDIENTS NEEDED:
{ingredient_list}

We would appreciate receiving your best pricing per unit, along with:
- Minimum order quantities
- Delivery schedule and lead times
- Any volume discounts available

Please submit your quote by {deadline_str}.

If you have questions about specifications or quantities, feel free to reply to this email.

Best regards,
{restaurant.name} Procurement Team
{os.getenv('GMAIL_SENDER', 'procurement@restaurant.com')}
"""
    return subject, body


def send_email(service, sender: str, to: str, subject: str, body: str) -> str | None:
    """Send an email via Gmail API. Returns the message ID."""
    message = MIMEText(body)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        result = service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return result.get("id")
    except Exception as e:
        print(f"  Failed to send to {to}: {e}")
        return None


def _fill_form_by_patterns(form, sender_email: str, rfp_body: str) -> dict[str, str]:
    """Fill form fields using common name patterns. Returns {field_name: value} of what was filled."""
    filled = {}
    for field_el in form.form.find_all(["input", "textarea", "select"]):
        name = (field_el.get("name") or "").lower()
        field_type = (field_el.get("type") or "").lower()

        if field_type in ("hidden", "submit", "button") or not field_el.get("name"):
            continue

        value = None
        if any(k in name for k in ("email", "mail")):
            value = sender_email
        elif any(k in name for k in ("name", "your-name", "full_name")):
            value = "Procurement Team"
        elif any(k in name for k in ("subject", "topic")):
            value = "Request for Proposal — Ingredient Pricing"
        elif any(k in name for k in ("message", "body", "comment", "inquiry", "text", "description")):
            value = rfp_body
        elif any(k in name for k in ("phone", "tel")):
            value = ""
        elif any(k in name for k in ("company", "organization", "business")):
            value = "Restaurant Procurement"

        if value is not None:
            form[field_el["name"]] = value
            filled[field_el["name"]] = value

    return filled


def _fill_form_with_claude(form_html: str, sender_email: str, rfp_body: str) -> dict[str, str]:
    """Ask Claude to map form fields to our data, returning {field_name: value}."""
    import anthropic

    data = {
        "email": sender_email,
        "name": "Procurement Team",
        "company": "Restaurant Procurement",
        "phone": "",
        "subject": "Request for Proposal — Ingredient Pricing",
        "message": rfp_body,
    }

    prompt = f"""Here is a contact form's HTML. Map each fillable field (input/textarea) to the correct value from the data below. Skip hidden, submit, and button fields.

DATA TO FILL:
- email: {sender_email}
- name: Procurement Team
- company: Restaurant Procurement
- subject: Request for Proposal — Ingredient Pricing
- message: (the RFP body text — use the key "message" to represent this)

FORM HTML:
{form_html}

Reply with ONLY a JSON object mapping field "name" attributes to either the literal value or the key "message" (I'll substitute the full text). Example:
{{"your-email": "{sender_email}", "field_3": "message", "contact-name": "Procurement Team"}}

If a field doesn't map to any of the data, omit it."""

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        mappings = json.loads(raw)

        # Substitute "message" placeholder with actual body
        result = {}
        for field_name, value in mappings.items():
            if value == "message":
                result[field_name] = rfp_body
            else:
                result[field_name] = value
        return result
    except Exception as e:
        print(f"    Claude form analysis failed: {e}")
        return {}


def _submit_contact_form(form_url: str, rfp_body: str, sender_email: str, submit: bool = True) -> bool:
    """Attempt to fill a website contact form.

    If submit=False, fills the form and prints the field mappings but does NOT submit.
    Strategy: pattern matching first, Claude fallback if that fails.
    """
    try:
        browser = mechanicalsoup.StatefulBrowser(
            user_agent="Mozilla/5.0",
            raise_on_404=True,
        )
        browser.open(form_url)
        forms = browser.get_current_page().find_all("form")
        if not forms:
            print(f"    No form found at {form_url}")
            return False

        # Score forms to find the most likely contact form (vs a search bar)
        best_idx = 0
        best_score = -1
        for idx, f in enumerate(forms):
            score = 0
            if f.find("textarea"):
                score += 5
            for inp in f.find_all("input"):
                name = (inp.get("name") or "").lower()
                typ = (inp.get("type") or "").lower()
                if "email" in name or typ == "email":
                    score += 3
                if "name" in name:
                    score += 1
            if score > best_score:
                best_score = score
                best_idx = idx

        # Select the highest-scoring form
        browser.select_form(forms[best_idx])
        form = browser.get_current_form()

        # Count fillable fields (excluding hidden/submit)
        fillable = [
            el for el in form.form.find_all(["input", "textarea"])
            if (el.get("type") or "").lower() not in ("hidden", "submit", "button")
            and el.get("name")
        ]
        fillable_names = [el.get("name") for el in fillable]

        # Try pattern matching first
        filled = _fill_form_by_patterns(form, sender_email, rfp_body)

        if len(filled) < len(fillable) // 2:
            # Pattern matching got less than half the fields — ask Claude
            print(f"    Pattern matching only filled {len(filled)}/{len(fillable)} fields, trying Claude...")
            form_html = str(forms[0])
            if len(form_html) > 8000:
                form_html = form_html[:8000] + "... (truncated)"

            mappings = _fill_form_with_claude(form_html, sender_email, rfp_body)
            if mappings:
                for field_name, value in mappings.items():
                    try:
                        form[field_name] = value
                        filled[field_name] = value
                    except Exception:
                        pass
                print(f"    Claude mapped {len(mappings)} fields")

        # Print detailed field report
        print(f"    --- Form Fields ({len(filled)}/{len(fillable)} filled) ---")
        for name in fillable_names:
            if name in filled:
                val = filled[name]
                print(f"      ✓ {name} = {val!r}")
            else:
                print(f"      ✗ {name} = (unfilled)")
        print(f"    ---")

        if not submit:
            print(f"    [DRY RUN] Form filled but NOT submitted at {form_url}")
            return True

        browser.submit_selected()
        print(f"    Form submitted at {form_url}")
        return True

    except Exception as e:
        print(f"    Pattern fill failed: {e}")
        try:
            print(f"    Retrying with Claude form analysis...")
            browser2 = mechanicalsoup.StatefulBrowser(
                user_agent="Mozilla/5.0",
                raise_on_404=True,
            )
            browser2.open(form_url)
            page_forms = browser2.get_current_page().find_all("form")
            if not page_forms:
                return False

            best_idx = 0
            best_score = -1
            for idx, f in enumerate(page_forms):
                score = 0
                if f.find("textarea"):
                    score += 5
                for inp in f.find_all("input"):
                    name = (inp.get("name") or "").lower()
                    typ = (inp.get("type") or "").lower()
                    if "email" in name or typ == "email":
                        score += 3
                    if "name" in name:
                        score += 1
                if score > best_score:
                    best_score = score
                    best_idx = idx

            form_html = str(page_forms[best_idx])
            if len(form_html) > 8000:
                form_html = form_html[:8000] + "... (truncated)"

            mappings = _fill_form_with_claude(form_html, sender_email, rfp_body)
            if not mappings:
                print(f"    Claude couldn't map the form either")
                return False

            browser2.select_form(page_forms[best_idx])
            form2 = browser2.get_current_form()
            
            fillable = [
                el for el in form2.form.find_all(["input", "textarea"])
                if (el.get("type") or "").lower() not in ("hidden", "submit", "button")
                and el.get("name")
            ]
            fillable_names = [el.get("name") for el in fillable]

            for field_name, value in mappings.items():
                try:
                    form2[field_name] = value
                except Exception:
                    pass

            print(f"    --- Form Fields ({len(mappings)}/{len(fillable)} filled) ---")
            for name in fillable_names:
                if name in mappings:
                    print(f"      ✓ {name} = {mappings[name]!r}")
                else:
                    print(f"      ✗ {name} = (unfilled)")
            print(f"    ---")

            if not submit:
                print(f"    [DRY RUN] Form filled via Claude but NOT submitted at {form_url}")
                return True

            browser2.submit_selected()
            print(f"    Form submitted via Claude fallback at {form_url}")
            return True
        except Exception as e2:
            print(f"    Claude fallback also failed: {e2}")
            return False


def _make_yopmail(distributor_name: str) -> str:
    """Generate a deterministic yopmail address from distributor name."""
    slug = re.sub(r"[^a-z0-9]", "", distributor_name.lower())[:20]
    return f"rfp-{slug}@yopmail.com"


def _get_ingredients_for_distributor(
    session: Session, dist: Distributor,
) -> list[tuple[Ingredient, str | None, str]]:
    """Get linked ingredients with USDA reference data for a distributor."""
    links = session.query(DistributorIngredient).filter_by(
        distributor_id=dist.id
    ).all()

    ingredients_with_info = []
    for link in links:
        ing = session.query(Ingredient).get(link.ingredient_id)
        usda_rec = session.query(USDAPrice).filter_by(
            ingredient_id=ing.id
        ).first()
        usda_name = usda_rec.usda_item_name if usda_rec else None
        unit = ing.base_unit or "lb"
        ingredients_with_info.append((ing, usda_name, unit))

    return ingredients_with_info


def send_rfp_emails(
    session: Session,
    restaurant_id: int,
    mock_recipient: str | None = None,
    submit_forms: bool = False,
) -> list[RFPEmail]:
    """
    Full Step 4 pipeline:
    1. Get restaurant and distributors
    2. Skip phone-only distributors (no email, no form)
    3. Send emails via Gmail or submit contact forms
    4. Log everything to DB

    If mock_recipient is set, all emails go to yopmail addresses instead.
    If submit_forms is False, forms are filled and verified but not actually submitted.
    """
    restaurant = session.query(Restaurant).get(restaurant_id)
    if not restaurant:
        print(f"Restaurant {restaurant_id} not found.")
        return []

    distributors = session.query(Distributor).all()
    if not distributors:
        print("No distributors found. Run Step 3 first.")
        return []

    sender = os.getenv("GMAIL_SENDER", "")
    if not sender:
        print("GMAIL_SENDER not set. Set it in .env")
        return []

    service = get_gmail_service()
    rfp_records = []
    sent_count = 0
    form_count = 0
    skipped_count = 0

    for dist in distributors:
        ingredients_with_info = _get_ingredients_for_distributor(session, dist)
        if not ingredients_with_info:
            continue

        subject, body = compose_rfp_body(restaurant, dist, ingredients_with_info)

        # Determine how to contact this distributor
        has_form = dist.email and dist.email.startswith("form:")
        has_email = dist.email and not has_form
        status = "draft"
        sent_at = None

        if has_email:
            # Send via Gmail — use yopmail in demo mode
            recipient = _make_yopmail(dist.name) if mock_recipient else dist.email
            print(f"  Emailing {dist.name} ({recipient})...")
            msg_id = send_email(service, sender, recipient, subject, body)
            status = "sent" if msg_id else "failed"
            sent_at = datetime.now(timezone.utc) if msg_id else None
            if msg_id:
                sent_count += 1

        elif has_form:
            # Try submitting the contact form
            form_url = dist.email[5:]  # strip "form:" prefix
            print(f"  Submitting form for {dist.name} ({form_url})...")
            success = _submit_contact_form(form_url, body, sender, submit=submit_forms)
            if not submit_forms:
                status = "form_ready" if success else "form_failed"
            else:
                status = "form_submitted" if success else "form_failed"
            sent_at = datetime.now(timezone.utc) if success else None
            if success:
                form_count += 1

        else:
            # Phone-only — skip
            print(f"  Skipping {dist.name} — phone only")
            skipped_count += 1
            status = "skipped"

        rfp = RFPEmail(
            distributor_id=dist.id,
            restaurant_id=restaurant.id,
            subject=subject,
            body=body,
            sent_at=sent_at,
            status=status,
        )
        session.add(rfp)
        rfp_records.append(rfp)

    session.commit()
    total = len(rfp_records)
    print(f"Step 4 complete: {sent_count} emailed, {form_count} forms submitted, {skipped_count} skipped (phone only) — {total} total")
    return rfp_records
