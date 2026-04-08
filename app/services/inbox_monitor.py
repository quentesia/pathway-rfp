"""Step 5: Monitor inbox for distributor quote replies, parse them, auto-reply."""

import json
import os
import base64
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.models import Distributor, Ingredient, DistributorIngredient, Restaurant
from app.services.email_sender import get_gmail_service, send_email, _make_yopmail

def get_reply_messages(service, after_date: str = None) -> list[dict]:
    """Fetch replies matching RFP subject lines."""
    query = "subject:Request for Proposal in:inbox"
    if after_date:
        query += f" after:{after_date}"

    results = service.users().messages().list(userId="me", q=query).execute()
    messages = results.get("messages", [])

    replies = []
    for msg in messages:
        full = service.users().messages().get(userId="me", id=msg["id"]).execute()
        payload = full.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}

        body = ""
        parts = payload.get("parts", [])
        if not parts:
            data = payload.get("body", {}).get("data")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8")
        else:
            for part in parts:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data")
                    if data:
                        body += base64.urlsafe_b64decode(data).decode("utf-8")

        if body:
            soup = BeautifulSoup(body, "html.parser")
            body = soup.get_text(separator="\n", strip=True)

        replies.append({
            "message_id": msg["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body,
        })

    return replies


class QuoteItem(BaseModel):
    ingredient_name: str = Field(description="The exact name of the requested ingredient")
    quoted_price: float | None = Field(None, description="Null if price not clearly stated")
    unit: str | None = Field(None, description="e.g. lb, oz, case")
    delivery_terms: str | None = None

class ParsedQuoteList(BaseModel):
    quotes: list[QuoteItem] = Field(description="Items they explicitly priced")
    not_supplied: list[str] = Field(description="Items they explicitly said they don't carry")
    clarification_needed: list[str] = Field(description="Items mentioned but missing price/unit")


def parse_quote_from_email(email_body: str, requested_ingredients: list[str]) -> ParsedQuoteList | None:
    """Use LLM to extract structured quote data from an email reply."""
    from app.services.prompts import get_quote_parse_prompt
    from app.services.llm_client import generate_json_text
    schema_json = json.dumps(ParsedQuoteList.model_json_schema(), indent=2)
    req_str = "\n".join(f"- {i}" for i in requested_ingredients)
    prompt = get_quote_parse_prompt(email_body, req_str, schema_json)

    try:
        from app.utils import strip_json_fences
        text = strip_json_fences(generate_json_text(
            prompt,
            max_tokens=2048,
            task_label="quote-parse",
        ))
        return ParsedQuoteList.model_validate_json(text)
    except Exception as e:
        print(f"  Error parsing quote: {e}")
        return None


def _compose_thank_you(distributor_name: str) -> str:
    return f"""Dear {distributor_name} Team,

Thank you for providing your pricing. We have received all the information we need and will be in touch regarding next steps.

Best regards,
Procurement Team
"""


def _compose_followup(distributor_name: str, missing_items: list[str]) -> str:
    items = "\n".join(f"  - {item}" for item in missing_items)
    return f"""Dear {distributor_name} Team,

Thank you for your quote. We noticed the following items were missing or unclear:

{items}

Could you please confirm availability and provide updated pricing/unit details for these items?

Also, for the quoted items, please share:
- Minimum order quantities (MOQs)
- Any bulk/volume discount tiers
- Delivery schedule and lead times
- Payment terms

Best regards,
Procurement Team
"""


def collect_quotes(
    session: Session,
    restaurant_id: int,
    mock_recipient: str | None = None,
    mock_replies: list[dict] | None = None,
    on_status: callable = None,
) -> list[DistributorIngredient]:
    """
    Full Step 5 pipeline:
    1. Check inbox for RFP replies
    2. Parse quotes with Claude
    3. Update DistributorIngredient rows with prices
    4. If all ingredients quoted → thank you reply, mark completed
    5. If some missing → follow-up reply, mark needs_clarification
    """
    def _status(msg):
        print(f"  {msg}")
        if on_status:
            on_status(msg)

    service = get_gmail_service()
    restaurant = session.get(Restaurant, restaurant_id)

    if mock_replies is not None:
        _status("Using mock replies for testing...")
        replies = mock_replies
    else:
        after_date = None
        if restaurant and restaurant.last_inbox_check:
            after_date = restaurant.last_inbox_check.strftime("%Y/%m/%d")
            _status(f"Checking inbox for replies after {after_date}...")
        else:
            _status("Checking inbox for RFP replies (first scan)...")
        replies = get_reply_messages(service, after_date=after_date)

    if not replies:
        _status("No replies found yet.")
        return []

    _status(f"Found {len(replies)} potential replies. Processing...")
    updated_links = []
    sender = os.getenv("GMAIL_SENDER", "me")

    for i, reply in enumerate(replies, 1):
        _status(f"[{i}/{len(replies)}] Processing reply from: {reply['from']}")

        # Match reply to a distributor
        distributor = None
        if mock_recipient == "demo" and "@yopmail.com" in reply["from"].lower():
            for dist in session.query(Distributor).all():
                if _make_yopmail(dist.name) in reply["from"].lower():
                    distributor = dist
                    break
        else:
            for dist in session.query(Distributor).filter(
                Distributor.rfp_status.in_(("sent", "needs_clarification")),
                Distributor.email.isnot(None),
            ).all():
                if dist.email.lower() in reply["from"].lower():
                    distributor = dist
                    break

        if not distributor:
            _status(f"  Could not match reply to a known distributor, skipping.")
            continue

        if distributor.rfp_status == "completed":
            _status(f"  Already completed for {distributor.name}, skipping.")
            continue

        # Get this distributor's ingredient links
        links = session.query(DistributorIngredient).filter_by(
            distributor_id=distributor.id
        ).all()
        requested_names = [l.ingredient.name for l in links]

        _status(f"  Parsing quote from {distributor.name} with AI...")
        parsed = parse_quote_from_email(reply["body"], requested_names)
        if not parsed:
            _status(f"  Could not parse quote from {distributor.name}.")
            continue
        _status(f"  Parsed: {len(parsed.quotes)} priced, {len(parsed.not_supplied)} not supplied, {len(parsed.clarification_needed)} need clarification")

        # Update DistributorIngredient rows with quoted prices
        for q in parsed.quotes:
            for link in links:
                if (link.ingredient.name.lower() in q.ingredient_name.lower()
                        or q.ingredient_name.lower() in link.ingredient.name.lower()):
                    link.supply_status = "confirmed"
                    link.quoted_price = q.quoted_price
                    link.quoted_unit = q.unit
                    link.delivery_terms = q.delivery_terms
                    updated_links.append(link)
                    break

        # Mark items they explicitly don't supply
        for name in parsed.not_supplied:
            for link in links:
                if name.lower() in link.ingredient.name.lower():
                    link.supply_status = "does_not_supply"
                    break

        # Check completeness: are all ingredients accounted for?
        unquoted = [
            l for l in links
            if l.supply_status == "unconfirmed" and l.ingredient.name not in parsed.clarification_needed
        ]
        needs_clarification = list(parsed.clarification_needed) + [l.ingredient.name for l in unquoted]
        # If delivery terms are missing for confirmed items, request clarification.
        missing_delivery_terms = [
            l.ingredient.name for l in links
            if l.supply_status == "confirmed" and not l.delivery_terms
        ]
        needs_clarification = sorted(set(needs_clarification))
        missing_delivery_terms = sorted(set(missing_delivery_terms))
        needs_followup = bool(needs_clarification or missing_delivery_terms)

        to_email = _make_yopmail(distributor.name) if mock_recipient == "demo" else distributor.email
        subject = f"Re: Request for Proposal — {distributor.name}"

        if not needs_followup:
            distributor.rfp_status = "completed"
            _status(f"  {distributor.name}: all items quoted — sending thank you")
            body = _compose_thank_you(distributor.name)
            send_email(service, sender, to_email, subject, body)
        else:
            distributor.rfp_status = "needs_clarification"
            _status(
                f"  {distributor.name}: follow-up needed "
                f"({len(needs_clarification)} ingredient clarifications, "
                f"{len(missing_delivery_terms)} delivery-term clarifications)"
            )
            followup_items = sorted(set(needs_clarification + missing_delivery_terms))
            body = _compose_followup(distributor.name, followup_items)
            send_email(service, sender, to_email, subject, body)

    if restaurant:
        restaurant.last_inbox_check = datetime.now(timezone.utc)
    session.commit()
    print(f"Step 5 complete: {len(updated_links)} ingredient prices updated.")
    return updated_links
