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
    delivery_charge: float | None = Field(None, description="Delivery fee amount when explicitly provided")
    delivery_charge_unit: str | None = Field(None, description="e.g. per order, per mile, per case")
    delivery_charge_notes: str | None = Field(None, description="Extra details like thresholds or waived conditions")

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


def _compose_followup(
    distributor_name: str,
    missing_ingredients: list[str],
    missing_delivery_items: list[str],
) -> str:
    sections = [f"Dear {distributor_name} Team,", "", "Thank you for your quote."]

    if missing_ingredients:
        ingredient_lines = "\n".join(f"  - {item}" for item in missing_ingredients)
        sections.extend(
            [
                "",
                "We still need availability/pricing confirmation for these ingredients:",
                "",
                ingredient_lines,
                "",
                "Please provide price and unit details for the items above.",
            ]
        )

    if missing_delivery_items:
        delivery_lines = "\n".join(f"  - {item}" for item in missing_delivery_items)
        sections.extend(
            [
                "",
                "For the following confirmed items, we still need delivery details:",
                "",
                delivery_lines,
                "",
                "Please share delivery schedule/lead times and delivery charges/fees "
                "(or explicitly mark them as TBD).",
            ]
        )

    sections.extend(
        [
            "",
            "Also, if available, please share:",
            "- Minimum order quantities (MOQs)",
            "- Any bulk/volume discount tiers",
            "- Payment terms",
            "",
            "Best regards,",
            "Procurement Team",
            "",
        ]
    )
    return "\n".join(sections)


def _has_textual_tbd_or_equivalent(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip().lower()
    markers = (
        "tbd",
        "to be determined",
        "pending",
        "unknown",
        "n/a",
        "na",
        "included",
        "free",
        "no charge",
        "waived",
    )
    return any(m in v for m in markers)


def _is_delivery_charge_resolved(link: DistributorIngredient) -> bool:
    # Numeric quote is the strongest signal.
    if link.delivery_charge is not None:
        return True
    # Allow explicit textual status for demo/procurement workflows.
    if _has_textual_tbd_or_equivalent(link.delivery_charge_notes):
        return True
    if _has_textual_tbd_or_equivalent(link.delivery_terms):
        return True
    return False


def _is_delivery_terms_resolved(link: DistributorIngredient) -> bool:
    if link.delivery_terms and link.delivery_terms.strip():
        return True
    # Fallback: if charge notes explicitly encode TBD/known status, treat as acknowledged.
    if _has_textual_tbd_or_equivalent(link.delivery_charge_notes):
        return True
    return False


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
                    link.delivery_charge = q.delivery_charge
                    link.delivery_charge_unit = q.delivery_charge_unit
                    link.delivery_charge_notes = q.delivery_charge_notes
                    updated_links.append(link)
                    break

        # Mark items they explicitly don't supply
        for name in parsed.not_supplied:
            for link in links:
                if name.lower() in link.ingredient.name.lower():
                    link.supply_status = "does_not_supply"
                    break

        # Check completeness: all ingredients must be resolved as confirmed or does_not_supply.
        unresolved_supply = [
            l for l in links
            if l.supply_status not in ("confirmed", "does_not_supply")
        ]
        # Preserve explicit model-requested clarification signals only if item remains unresolved.
        unresolved_names = [l.ingredient.name for l in unresolved_supply]
        parsed_clarification_names = []
        for item_name in parsed.clarification_needed:
            if any(
                item_name.lower() in name.lower() or name.lower() in item_name.lower()
                for name in unresolved_names
            ):
                parsed_clarification_names.append(item_name)
        needs_clarification = parsed_clarification_names + unresolved_names

        # For confirmed items, delivery terms + delivery charges must be provided
        # (or explicitly marked TBD/unknown/included).
        missing_delivery_info = []
        for l in links:
            if l.supply_status != "confirmed":
                continue
            if not _is_delivery_terms_resolved(l) or not _is_delivery_charge_resolved(l):
                missing_delivery_info.append(l.ingredient.name)

        needs_clarification = sorted(set(needs_clarification))
        missing_delivery_info = sorted(set(missing_delivery_info))
        needs_followup = bool(needs_clarification or missing_delivery_info)

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
                f"{len(missing_delivery_info)} delivery clarifications)"
            )
            body = _compose_followup(
                distributor.name,
                sorted(set(needs_clarification)),
                sorted(set(missing_delivery_info)),
            )
            send_email(service, sender, to_email, subject, body)

    if restaurant:
        restaurant.last_inbox_check = datetime.now(timezone.utc)
    session.commit()
    print(f"Step 5 complete: {len(updated_links)} ingredient prices updated.")
    return updated_links
