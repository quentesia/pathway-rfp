"""Step 5: Monitor inbox for distributor quote replies, parse them, auto-reply."""

import json
import os
import base64
from datetime import datetime, timezone

import anthropic
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.models import Distributor, Ingredient, DistributorIngredient
from app.services.email_sender import get_gmail_service, send_email, _make_yopmail

MODEL = "claude-sonnet-4-20250514"


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
    """Use Claude to extract structured quote data from an email reply."""
    from app.services.prompts import get_quote_parse_prompt
    schema_json = json.dumps(ParsedQuoteList.model_json_schema(), indent=2)
    req_str = "\n".join(f"- {i}" for i in requested_ingredients)
    prompt = get_quote_parse_prompt(email_body, req_str, schema_json)

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
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

Could you please provide updated pricing for these items at your earliest convenience?

Best regards,
Procurement Team
"""


def collect_quotes(
    session: Session,
    restaurant_id: int,
    mock_recipient: str | None = None,
    mock_replies: list[dict] | None = None,
) -> list[DistributorIngredient]:
    """
    Full Step 5 pipeline:
    1. Check inbox for RFP replies
    2. Parse quotes with Claude
    3. Update DistributorIngredient rows with prices
    4. If all ingredients quoted → thank you reply, mark completed
    5. If some missing → follow-up reply, mark needs_clarification
    """
    service = get_gmail_service()

    if mock_replies is not None:
        print("Using mock replies for testing...")
        replies = mock_replies
    else:
        print("Checking inbox for RFP replies...")
        replies = get_reply_messages(service)

    if not replies:
        print("  No replies found yet.")
        return []

    print(f"  Found {len(replies)} potential replies.")
    updated_links = []
    sender = os.getenv("GMAIL_SENDER", "me")

    for reply in replies:
        print(f"  Processing reply from: {reply['from']}")

        # Match reply to a distributor
        distributor = None
        if mock_recipient == "demo" and "@yopmail.com" in reply["from"].lower():
            for dist in session.query(Distributor).all():
                if _make_yopmail(dist.name) in reply["from"].lower():
                    distributor = dist
                    break
        else:
            for dist in session.query(Distributor).all():
                if dist.email and dist.email.lower() in reply["from"].lower():
                    distributor = dist
                    break
                if dist.name.lower() in reply["from"].lower():
                    distributor = dist
                    break

        if not distributor:
            print("    Could not match reply to a known distributor.")
            continue

        if distributor.rfp_status == "completed":
            print(f"    Already completed for {distributor.name}, skipping.")
            continue

        # Get this distributor's ingredient links
        links = session.query(DistributorIngredient).filter_by(
            distributor_id=distributor.id
        ).all()
        requested_names = [l.ingredient.name for l in links]

        parsed = parse_quote_from_email(reply["body"], requested_names)
        if not parsed:
            continue

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

        to_email = _make_yopmail(distributor.name) if mock_recipient == "demo" else distributor.email
        subject = f"Re: Request for Proposal — {distributor.name}"

        if not needs_clarification:
            # All done — send thank you
            distributor.rfp_status = "completed"
            print(f"    All items quoted for {distributor.name} — sending thank you")
            body = _compose_thank_you(distributor.name)
            send_email(service, sender, to_email, subject, body)
        else:
            # Missing items — send follow-up
            distributor.rfp_status = "needs_clarification"
            print(f"    {len(needs_clarification)} items need clarification from {distributor.name}")
            body = _compose_followup(distributor.name, needs_clarification)
            send_email(service, sender, to_email, subject, body)

    session.commit()
    print(f"Step 5 complete: {len(updated_links)} ingredient prices updated.")
    return updated_links
