"""Step 3: Find local food distributors via Google Places API, SerpAPI, or LLM fallback."""

import json
import os

import anthropic
import requests
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
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
            
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
            return list(all_results.values())
        print("  No Serper results.")

    # print("  Using LLM inference fallback...")
    # return search_llm_fallback(location, categories)
    return []


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
            dist = Distributor(
                name=d["name"],
                location=d.get("location", ""),
                phone=d.get("phone"),
                email=d.get("email"),
                website=d.get("website"),
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
    return processed


def find_local_distributors(session: Session, location: str) -> list[Distributor]:
    """
    Full Step 3 pipeline:
    1. Gather all ingredient categories from DB
    2. Find distributors via best available API
    3. Store and link to ingredients
    """
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
    print(f"Step 3 complete: {len(distributors)} distributors stored.")
    return distributors
