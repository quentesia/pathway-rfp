"""Step 3: Find local food distributors via Google Places API, SerpAPI, or LLM fallback."""

import json
import os
import re

from urllib.parse import urljoin

import anthropic
import httpx
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.models import Ingredient, Distributor, DistributorIngredient

MODEL = "claude-sonnet-4-20250514"





# ── Serper (google.serper.dev) ────────────────────────────────────────────────

def search_serper_api(location: str, query: str, api_key: str, category: str = "") -> list[dict]:
    """Search Serper's Google Maps /places API for food distributors."""
    resp = requests.post(
        "https://google.serper.dev/places",
        headers={
            "X-API-KEY": api_key,
            "Content-Type": "application/json"
        },
        json={
            "q": query
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("places", [])

    distributors = []
    for r in results:
        distributors.append({
            "name": r.get("title", ""),
            "location": r.get("address", ""),
            "phone": r.get("phoneNumber"),
            "email": None,
            "website": r.get("website"),
            "rating": r.get("rating"),
            "rating_count": r.get("ratingCount"),
            "source": "Serper Places API",
            "categories_served": [category] if category else [],
        })
    return distributors


# ── LLM Fallback ─────────────────────────────────────────────────────────────

class DistributorResult(BaseModel):
    name: str = Field(description="real company name")
    location: str = Field(description="city, state")
    phone: str | None = None
    email: str | None = Field(None, description="use format: sales@companyname.com if unknown")
    website: str | None = None
    categories_served: list[str]

class DistributorList(BaseModel):
    distributors: list[DistributorResult]

_DIST_SCHEMA_JSON = json.dumps(DistributorList.model_json_schema(), indent=2)


def search_llm_fallback(location: str, categories: list[str]) -> list[dict]:
    """Use Claude to infer distributors when no API key is available."""
    from app.services.prompts import DISTRIBUTOR_PROMPT
    categories_str = ", ".join(sorted(set(categories)))
    prompt = DISTRIBUTOR_PROMPT.format(
        location=location,
        categories=categories_str,
        schema_json=_DIST_SCHEMA_JSON
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        from app.utils import strip_json_fences
        text = strip_json_fences(response.content[0].text)
        parsed = DistributorList.model_validate_json(text)
        distributors = []
        for d in parsed.distributors:
            d_dict = d.model_dump()
            d_dict["source"] = "Claude LLM inference"
            distributors.append(d_dict)
            
        return distributors
    except Exception as e:
        print(f"  Claude distributor search failed: {e}")
        return []


# ── Email scraping ──────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_SKIP_DOMAINS = {
    "example.com", "sentry.io", "wixpress.com", "googleapis.com",
    "wordpress.com", "squarespace.com", "w3.org", "schema.org",
    "googleusercontent.com", "gstatic.com",
}
_SKIP_PREFIXES = {"user@", "name@", "email@", "your@", "username@", "test@"}

# Preferred prefixes for RFP outreach, in priority order
_GOOD_PREFIXES = ["sales@", "orders@", "info@", "contact@", "hello@"]

_HTTP_CLIENT = httpx.Client(
    timeout=10, follow_redirects=True,
    headers={"User-Agent": "Mozilla/5.0"},
)


def _extract_best_email(html: str) -> str | None:
    """Pull the best contact email from HTML, filtering junk."""
    candidates = []
    for email in _EMAIL_RE.findall(html):
        email = email.lower()
        domain = email.split("@")[1]
        if domain in _SKIP_DOMAINS:
            continue
        if any(email.startswith(p) for p in _SKIP_PREFIXES):
            continue
        if email.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js")):
            continue
        candidates.append(email)

    if not candidates:
        return None

    # Pick by prefix priority
    for prefix in _GOOD_PREFIXES:
        for c in candidates:
            if c.startswith(prefix):
                return c
    return candidates[0]


def _find_contact_page_url(html: str, base_url: str) -> str | None:
    """Find a /contact or /contact-us link in the page."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = (a.get_text() or "").lower()
        if "contact" in href or "contact" in text:
            return urljoin(base_url, a["href"])
    return None


def _page_has_form(html: str) -> bool:
    """Check if the page has a form (likely a contact form)."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("form") is not None


def scrape_email_from_website(url: str) -> str | None:
    """Fetch a distributor's website and extract a contact email.

    Returns:
        "email@example.com" — if a real email was found
        "form:http://example.com/contact" — if only a contact form exists
        None — if nothing found or site unreachable
    """
    if not url:
        return None

    try:
        resp = _HTTP_CLIENT.get(url)
        resp.raise_for_status()
    except Exception:
        return None

    homepage_html = resp.text

    # Try homepage first
    email = _extract_best_email(homepage_html)
    if email:
        return email

    # Look for a contact page
    contact_url = _find_contact_page_url(homepage_html, str(resp.url))
    if contact_url:
        try:
            contact_resp = _HTTP_CLIENT.get(contact_url)
            contact_resp.raise_for_status()
            contact_html = contact_resp.text

            email = _extract_best_email(contact_html)
            if email:
                return email

            # No email but has a form — return form URL
            if _page_has_form(contact_html):
                return f"form:{contact_url}"
        except Exception:
            pass

    return None


# ── Orchestration ────────────────────────────────────────────────────────────

def find_distributors(location: str, categories: list[str]) -> list[dict]:
    """
    Find distributors using SerpAPI, with an LLM inference fallback.
    """
    serper_key = os.getenv("SERPER_API_KEY") or os.getenv("SERPAPI_KEY")

    if serper_key:
        print("  Using Serper API...")
        all_results = {}
        for cat in categories:
            query = f"{cat} wholesale distributor supplier near {location}"
            results = search_serper_api(location, query, serper_key, category=cat)
            
            # Take top 3 for each category to keep it concise
            for r in results[:3]:
                name_key = r["name"].lower()
                if name_key not in all_results:
                    all_results[name_key] = r
                else:
                    for c in r["categories_served"]:
                        if c not in all_results[name_key]["categories_served"]:
                            all_results[name_key]["categories_served"].append(c)

        if all_results:
            final_list = list(all_results.values())
            print("  Scraping distributor websites for real contact emails...")
            for d in final_list:
                if d.get("website") and not d.get("email"):
                    scraped_email = scrape_email_from_website(d["website"])
                    if scraped_email:
                        d["email"] = scraped_email
            return final_list
        print("  No Serper results.")

    print("  Using LLM inference fallback...")
    return search_llm_fallback(location, categories)


def store_distributors(
    session: Session,
    distributor_data: list[dict],
    ingredients: list[Ingredient],
) -> list[Distributor]:
    """Store distributors and link them to ingredients by category."""
    processed = []

    # Build category -> ingredient mapping
    cat_to_ingredients: dict[str, list[Ingredient]] = {}
    for ing in ingredients:
        cat = ing.category or "Other"
        cat_to_ingredients.setdefault(cat, []).append(ing)

    for d in distributor_data:
        # Check if distributor already exists by name (case-insensitive)
        dist = session.query(Distributor).filter(
            Distributor.name.ilike(d["name"])
        ).first()

        if not dist:
            # Try to find a contact email from their website
            email = d.get("email") or scrape_email_from_website(d.get("website"))
            if email and email.startswith("form:"):
                print(f"    {d['name']}: contact form at {email[5:]}")
            elif email:
                print(f"    {d['name']}: {email}")
            else:
                print(f"    {d['name']}: no email found")

            dist = Distributor(
                name=d["name"],
                location=d.get("location", ""),
                phone=d.get("phone"),
                email=email,
                website=d.get("website"),
                rating=d.get("rating"),
                rating_count=d.get("rating_count"),
                source=d.get("source", "Unknown"),
                categories_served=", ".join(d.get("categories_served", [])),
            )
            session.add(dist)
            session.flush()

        # Get existing links to avoid duplicates
        existing_links = {
            link.ingredient_id
            for link in session.query(DistributorIngredient).filter_by(distributor_id=dist.id).all()
        }

        # Link distributor to ingredients matching its served categories
        served_cats = d.get("categories_served", [])
        ingredients_to_link = []
        if served_cats:
            for cat in served_cats:
                ingredients_to_link.extend(cat_to_ingredients.get(cat, []))
        else:
            # If no categories specified (API results), link to all ingredients
            ingredients_to_link = ingredients

        for ing in ingredients_to_link:
            if ing.id not in existing_links:
                link = DistributorIngredient(
                    distributor_id=dist.id,
                    ingredient_id=ing.id,
                )
                session.add(link)
                existing_links.add(ing.id)

        if dist not in processed:
            processed.append(dist)

    session.commit()

    with_email = sum(1 for d in processed if d.email and not d.email.startswith("form:"))
    with_form = sum(1 for d in processed if d.email and d.email.startswith("form:"))
    print(f"  Contact info: {with_email}/{len(processed)} with email, {with_form} with contact form")

    return processed


class EmailLookupItem(BaseModel):
    name: str = Field(description="Distributor name")
    email: str | None = Field(None, description="Contact email, null if unknown")


class EmailLookupResult(BaseModel):
    results: list[EmailLookupItem]


def _ai_email_fallback(session: Session, distributors: list[Distributor]) -> None:
    """Use Claude to infer emails for distributors that have a website but no email."""
    missing = [d for d in distributors if d.website and not d.email]
    if not missing:
        return

    print(f"  AI fallback: looking up emails for {len(missing)} distributors...")

    lines = []
    for i, d in enumerate(missing, 1):
        lines.append(f"{i}. {d.name} | {d.location} | {d.website}")

    schema_json = json.dumps(EmailLookupResult.model_json_schema(), indent=2)
    prompt = (
        "Given these food distributors (name, location, website), "
        "find their contact email addresses. Return null for any you cannot determine.\n\n"
        + "\n".join(lines)
        + f"\n\nReturn ONLY valid JSON matching this schema:\n{schema_json}"
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        from app.utils import strip_json_fences
        text = strip_json_fences(response.content[0].text)
        parsed = EmailLookupResult.model_validate_json(text)

        for dist, result in zip(missing, parsed.results):
            if result.email:
                dist.email = result.email.lower()
                print(f"    AI found: {dist.name} → {dist.email}")
            else:
                print(f"    AI: {dist.name} → not found")

        session.commit()
    except Exception as e:
        print(f"  AI email fallback failed: {e}")


def find_local_distributors(session: Session, location: str, restaurant_id: int = None) -> list[Distributor]:
    """
    Full Step 3 pipeline:
    1. Gather all ingredient categories from DB
    2. Find distributors via best available API
    3. Store and link to ingredients
    4. AI fallback for missing emails
    """
    if restaurant_id:
        ingredients = session.query(Ingredient).filter_by(restaurant_id=restaurant_id).all()
    else:
        ingredients = session.query(Ingredient).all()
    if not ingredients:
        print("No ingredients found. Run Step 1 first.")
        return []

    categories = list({ing.category or "Other" for ing in ingredients})
    print(f"Finding distributors in {location} for categories: {categories}")

    distributor_data = find_distributors(location, categories)
    if not distributor_data:
        print("  No distributors found.")
        return []

    print(f"  Found {len(distributor_data)} distributors. Storing...")
    distributors = store_distributors(session, distributor_data, ingredients)

    # AI fallback for distributors with website but no email
    _ai_email_fallback(session, distributors)

    print(f"Step 3 complete: {len(distributors)} distributors stored.")
    return distributors
