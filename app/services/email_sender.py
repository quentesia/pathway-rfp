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
    Distributor, DistributorIngredient, Ingredient, Recipe,
    RecipeIngredient, Restaurant, USDAPrice,
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
    ingredients_with_info: list[tuple],
    quote_deadline_days: int = 7,
) -> tuple[str, str]:
    """
    Compose an RFP email subject + body.
    ingredients_with_info: list of (Ingredient, usda_match_name, unit, quantity, qty_unit)
    """
    deadline = datetime.now(timezone.utc) + timedelta(days=quote_deadline_days)
    deadline_str = deadline.strftime("%B %d, %Y")

    lines = []
    for ing, usda_name, unit, qty, qty_unit in ingredients_with_info:
        if qty:
            lines.append(f"  - {ing.name} — est. {qty:.0f} {qty_unit}/week")
        else:
            lines.append(f"  - {ing.name}")

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


def _get_field_label(field_el) -> str:
    """Get the human-readable label for a form field by checking:
    1. <label for="field_id">
    2. Parent <label> wrapping the field
    3. Placeholder attribute
    4. aria-label attribute
    5. Fall back to field name
    """
    soup = field_el.find_parent("form") or field_el.parent
    field_id = field_el.get("id")

    # Check <label for="id">
    if field_id and soup:
        label = soup.find("label", attrs={"for": field_id})
        if label:
            return label.get_text(strip=True).lower()

    # Check parent <label>
    parent_label = field_el.find_parent("label")
    if parent_label:
        return parent_label.get_text(strip=True).lower()

    # Placeholder or aria-label
    for attr in ("placeholder", "aria-label", "title"):
        val = field_el.get(attr)
        if val:
            return val.lower()

    return (field_el.get("name") or "").lower()


def _fill_form_by_patterns(form, sender_email: str, rfp_body: str) -> dict[str, str]:
    """Fill form fields using label/name patterns. Returns {field_name: value} of what was filled."""
    filled = {}
    for field_el in form.form.find_all(["input", "textarea", "select"]):
        field_name = field_el.get("name")
        field_type = (field_el.get("type") or "").lower()

        if field_type in ("hidden", "submit", "button") or not field_name:
            continue

        # Check both the field name and its label
        name_lower = field_name.lower()
        label = _get_field_label(field_el)
        match_text = f"{name_lower} {label}"

        value = None
        if any(k in match_text for k in ("email", "mail")):
            value = sender_email
        elif any(k in match_text for k in ("first name", "your name", "full name", "contact name")):
            value = "Procurement Team"
        elif "last name" in match_text or "surname" in match_text:
            value = ""
        elif any(k in match_text for k in ("subject", "topic", "reason")):
            value = "Request for Proposal — Ingredient Pricing"
        elif any(k in match_text for k in ("message", "body", "comment", "inquiry", "question", "description", "details")):
            value = rfp_body
        elif any(k in match_text for k in ("phone", "tel", "mobile")):
            value = ""
        elif any(k in match_text for k in ("company", "organization", "business", "restaurant")):
            value = "Restaurant Procurement"
        elif any(k in match_text for k in ("city", "location")):
            value = ""
        elif any(k in match_text for k in ("state", "zip", "postal")):
            value = ""

        if value is not None:
            form[field_name] = value
            filled[field_name] = value

    return filled


def _fill_form_with_claude(form_html: str, sender_email: str, rfp_body: str) -> dict[str, str]:
    """Ask LLM to map form fields to our data, returning {field_name: value}."""

    data = {
        "email": sender_email,
        "name": "Procurement Team",
        "company": "Restaurant Procurement",
        "phone": "",
        "subject": "Request for Proposal — Ingredient Pricing",
        "message": rfp_body,
    }

    prompt = f"""Here is a contact form's HTML. Map each fillable field (input/textarea) to the correct value from the data below. Skip hidden, submit, and button fields.

IMPORTANT: Field "name" attributes may be GUIDs or opaque IDs (e.g. "fxb.57c55011...Fields[de0236e9...]").
Look at <label> elements (for= attribute or wrapping), placeholder text, aria-label, and surrounding context to determine what each field is for.
Use the EXACT "name" attribute value as the key in your JSON — do not simplify it.

DATA TO FILL:
- email: {sender_email}
- name: Procurement Team
- company: Restaurant Procurement
- phone: (leave empty string)
- subject: Request for Proposal — Ingredient Pricing
- message: (the RFP body text — use the key "message" to represent this)
- For any other fields (city, state, zip, etc.): use empty string

FORM HTML:
{form_html}

Reply with ONLY a JSON object mapping the EXACT field "name" attributes to either the literal value or the key "message" (I'll substitute the full text). Example:
{{"fxb.abc123.Fields[def456].Value": "{sender_email}", "fxb.abc123.Fields[ghi789].Value": "message"}}

Map ALL fillable fields, using empty string for fields you can't match."""

    try:
        from app.services.llm_client import generate_json_text
        import json
        from app.utils import strip_json_fences
        raw = strip_json_fences(generate_json_text(prompt, max_tokens=1024))
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
        print(f"    LLM form analysis failed: {e}")
        return {}


def _score_forms(forms: list) -> int:
    """Score HTML forms and return index of the most likely contact form."""
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
    return best_idx


def _fill_and_report(browser, forms, form_idx, sender_email, rfp_body, submit, use_claude_only=False):
    """Fill a form, print field report, optionally submit. Returns True on success."""
    browser.select_form(forms[form_idx])
    form = browser.get_current_form()

    fillable = [
        el for el in form.form.find_all(["input", "textarea"])
        if (el.get("type") or "").lower() not in ("hidden", "submit", "button")
        and el.get("name")
    ]
    fillable_names = [el.get("name") for el in fillable]

    if use_claude_only:
        filled = {}
    else:
        filled = _fill_form_by_patterns(form, sender_email, rfp_body)

    if len(filled) < len(fillable) // 2:
        label = "Claude-only" if use_claude_only else "Pattern matching"
        if not use_claude_only:
            print(f"    {label} only filled {len(filled)}/{len(fillable)} fields, trying Claude...")
        form_html = str(forms[form_idx])
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
        elif use_claude_only:
            print("    Claude couldn't map the form")
            return False

    print(f"    --- Form Fields ({len(filled)}/{len(fillable)} filled) ---")
    for name in fillable_names:
        if name in filled:
            print(f"      ✓ {name} = {filled[name]!r}")
        else:
            print(f"      ✗ {name} = (unfilled)")
    print("    ---")

    if not submit:
        print(f"    [DRY RUN] Form filled but NOT submitted")
        return True

    browser.submit_selected()
    print(f"    Form submitted")
    return True


def _submit_contact_form(form_url: str, rfp_body: str, sender_email: str, submit: bool = True) -> bool:
    """Attempt to fill a website contact form.

    If submit=False, fills the form and prints the field mappings but does NOT submit.
    Strategy: pattern matching first, Claude fallback if that fails.
    """
    try:
        browser = mechanicalsoup.StatefulBrowser(user_agent="Mozilla/5.0", raise_on_404=True)
        browser.open(form_url)
        forms = browser.get_current_page().find_all("form")
        if not forms:
            print(f"    No form found at {form_url}")
            return False

        best_idx = _score_forms(forms)
        return _fill_and_report(browser, forms, best_idx, sender_email, rfp_body, submit)

    except Exception as e:
        print(f"    Pattern fill failed: {e}")
        try:
            print("    Retrying with Claude form analysis...")
            browser2 = mechanicalsoup.StatefulBrowser(user_agent="Mozilla/5.0", raise_on_404=True)
            browser2.open(form_url)
            forms2 = browser2.get_current_page().find_all("form")
            if not forms2:
                return False
            best_idx = _score_forms(forms2)
            return _fill_and_report(browser2, forms2, best_idx, sender_email, rfp_body, submit, use_claude_only=True)
        except Exception as e2:
            print(f"    Claude fallback also failed: {e2}")
            return False


def _make_yopmail(distributor_name: str) -> str:
    """Generate a deterministic yopmail address from distributor name."""
    slug = re.sub(r"[^a-z0-9]", "", distributor_name.lower())[:20]
    return f"rfp-{slug}@yopmail.com"


def _get_ingredients_for_distributor(
    session: Session, dist: Distributor, restaurant_id: int,
    weekly_covers: int = 40,
    weekly_covers_by_category: dict[str, int] | None = None,
) -> list[tuple]:
    """Get linked ingredients with USDA reference data and aggregated quantities.

    Returns list of (Ingredient, usda_match_name, unit, total_qty, qty_unit).
    Quantities are per-serving × weekly_covers, summed across all recipes,
    with optional category-level weekly cover overrides.
    """
    links = session.query(DistributorIngredient).filter_by(
        distributor_id=dist.id
    ).all()
    ing_ids = [l.ingredient_id for l in links]
    if not ing_ids:
        return []

    ingredients = {i.id: i for i in session.query(Ingredient).filter(
        Ingredient.id.in_(ing_ids)
    ).all()}

    # Aggregate quantities: per-serving qty × weekly_covers × popularity, normalized to base_unit
    from app.utils import aggregate_quantities
    recipe_ings = session.query(RecipeIngredient).join(Recipe).filter(
        Recipe.restaurant_id == restaurant_id,
        RecipeIngredient.ingredient_id.in_(ing_ids),
    ).all()
    recipe_ids = {ri.recipe_id for ri in recipe_ings}
    recipes_map = {r.id: r for r in session.query(Recipe).filter(Recipe.id.in_(recipe_ids)).all()}
    qty_map = aggregate_quantities(
        recipe_ings,
        weekly_covers,
        ingredients,
        recipes_map,
        weekly_covers_by_category=weekly_covers_by_category,
    )

    usda_map = {u.ingredient_id: u for u in session.query(USDAPrice).filter(
        USDAPrice.ingredient_id.in_(ing_ids)
    ).all()}

    result = []
    for ing_id in ing_ids:
        ing = ingredients[ing_id]
        usda_rec = usda_map.get(ing_id)
        usda_name = usda_rec.usda_item_name if usda_rec else None
        unit = ing.base_unit or "lb"
        qty, qty_unit = qty_map.get(ing_id, (None, unit))
        result.append((ing, usda_name, unit, qty, qty_unit))

    return result


def send_rfp_emails(
    session: Session,
    restaurant_id: int,
    mock_recipient: str | None = None,
    submit_forms: bool = False,
    weekly_covers: int = 40,
    weekly_covers_by_category: dict[str, int] | None = None,
    on_status: callable = None,
) -> list[Distributor]:
    """
    Full Step 4 pipeline:
    1. Get restaurant and distributors
    2. Skip phone-only distributors (no email, no form)
    3. Send emails via Gmail or submit contact forms
    4. Update distributor rfp_status in DB

    If mock_recipient is set, all emails go to yopmail addresses instead.
    If submit_forms is False, forms are filled and verified but not actually submitted.
    """
    def _status(msg):
        print(f"  {msg}")
        if on_status:
            on_status(msg)

    restaurant = session.get(Restaurant, restaurant_id)
    if not restaurant:
        _status(f"Restaurant {restaurant_id} not found.")
        return []

    distributors = session.query(Distributor).filter_by(rfp_status="pending").all()
    total_count = session.query(Distributor).count()
    already_sent = total_count - len(distributors)

    if total_count == 0:
        _status("No distributors found. Run Step 3 first.")
        return []
    if not distributors:
        _status(f"All {total_count} distributors already contacted. No pending sends.")
        return []

    sender = os.getenv("GMAIL_SENDER", "")
    if not sender:
        _status("GMAIL_SENDER not set. Set it in .env")
        return []

    if already_sent > 0:
        _status(f"Skipping {already_sent} already-contacted distributors. Preparing RFPs for {len(distributors)} remaining...")
    else:
        _status(f"Preparing RFPs for {len(distributors)} distributors...")
    service = get_gmail_service()
    processed = []
    sent_count = 0
    form_count = 0
    skipped_count = 0

    for i, dist in enumerate(distributors, 1):
        ingredients_with_info = _get_ingredients_for_distributor(
            session,
            dist,
            restaurant_id,
            weekly_covers,
            weekly_covers_by_category=weekly_covers_by_category,
        )
        if not ingredients_with_info:
            continue

        subject, body = compose_rfp_body(restaurant, dist, ingredients_with_info)

        has_form = dist.email and dist.email.startswith("form:")
        has_email = dist.email and not has_form

        if has_email:
            recipient = _make_yopmail(dist.name) if mock_recipient else dist.email
            _status(f"[{i}/{len(distributors)}] Emailing {dist.name} ({recipient})...")
            msg_id = send_email(service, sender, recipient, subject, body)
            if msg_id:
                dist.rfp_status = "sent"
                dist.rfp_sent_at = datetime.now(timezone.utc)
                sent_count += 1
            else:
                dist.rfp_status = "failed"

        elif has_form:
            form_url = dist.email[5:]
            _status(f"[{i}/{len(distributors)}] Submitting contact form for {dist.name}...")
            success = _submit_contact_form(form_url, body, sender, submit=submit_forms)
            if success:
                dist.rfp_status = "form_ready" if not submit_forms else "sent"
                dist.rfp_sent_at = datetime.now(timezone.utc)
                form_count += 1
            else:
                dist.rfp_status = "form_failed"

        else:
            _status(f"[{i}/{len(distributors)}] Skipping {dist.name} — phone only")
            dist.rfp_status = "skipped"
            skipped_count += 1

        processed.append(dist)

    session.commit()
    _status(f"Done — {sent_count} emailed, {form_count} forms, {skipped_count} skipped.")
    return processed
