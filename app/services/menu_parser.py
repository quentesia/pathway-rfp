"""Step 1: Parse a restaurant menu photo into structured recipes using Claude vision.

Single call: image in → all recipes + deduplicated ingredients out.
Pydantic validates the response matches our schema exactly.
"""

import json
import os
import base64
import hashlib

import anthropic
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app.models import Restaurant, Recipe, Ingredient, RecipeIngredient

MODEL = "claude-sonnet-4-20250514"

VALID_UNITS = {"each", "pinch", "tsp", "tbsp", "cup", "pt", "qt", "gal", "ml", "l", "g", "kg", "oz", "lb"}
VALID_CATEGORIES = {
    "Produce", "Meat & Poultry", "Seafood", "Dairy & Eggs",
    "Dry Goods & Pantry", "Frozen Foods", "Bakery & Breads",
    "Beverages", "Oils, Fats & Sauces", "Other",
}


# ── Pydantic response schema ────────────────────────────────────────────────

class IngredientEntry(BaseModel):
    name: str
    quantity: float
    unit: str
    category: str
    perishable: bool = True
    notes: str | None = None

    @field_validator("unit")
    @classmethod
    def unit_must_be_canonical(cls, v: str) -> str:
        if v.lower() not in VALID_UNITS:
            return "each"
        return v.lower()

    @field_validator("category")
    @classmethod
    def category_must_be_valid(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            return "Other"
        return v


class RecipeEntry(BaseModel):
    dish_name: str
    description: str = ""
    price: str | None = None
    category: str = "Other"
    estimated_servings: int = 1
    ingredients: list[IngredientEntry]


class MenuParseResult(BaseModel):
    recipes: list[RecipeEntry]


# Build the JSON schema string from Pydantic to embed in the prompt
_SCHEMA_JSON = json.dumps(MenuParseResult.model_json_schema(), indent=2)


# ── Claude call ──────────────────────────────────────────────────────────────

def parse_menu_image(image_path: str) -> MenuParseResult:
    """Send menu photo to Claude, get back validated recipes."""
    with open(image_path, "rb") as f:
        file_bytes = f.read()

    # Read magic bytes to determine true MIME type
    if file_bytes.startswith(b'\xff\xd8'):
        media_type = "image/jpeg"
    elif file_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        media_type = "image/png"
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WEBP':
        media_type = "image/webp"
    else:
        # Fallback 
        media_type = "image/jpeg"

    image_data = base64.standard_b64encode(file_bytes).decode("utf-8")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    from app.services.prompts import get_menu_parse_prompt
    
    response = client.messages.create(
        model=MODEL,
        max_tokens=16384,
        system=get_menu_parse_prompt(_SCHEMA_JSON),
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_data}},
                {"type": "text", "text": "Parse this entire restaurant menu. Return every dish with full recipes."},
            ],
        }],
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude response truncated (hit max_tokens). "
            f"Got {len(response.content[0].text)} chars. Increase max_tokens."
        )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = json.loads(raw)
    return MenuParseResult.model_validate(data)


# ── DB storage ───────────────────────────────────────────────────────────────

def store_parsed_recipes(
    session: Session,
    restaurant_id: int,
    result: MenuParseResult,
) -> list[Recipe]:
    """Store validated recipes into DB with ingredient dedup."""
    created_recipes = []

    for entry in result.recipes:
        recipe = Recipe(
            restaurant_id=restaurant_id,
            dish_name=entry.dish_name,
            dish_description=entry.description,
            category=entry.category,
            estimated_servings=entry.estimated_servings,
        )
        session.add(recipe)
        session.flush()

        for ing in entry.ingredients:
            db_ingredient = session.query(Ingredient).filter(
                Ingredient.name == ing.name
            ).first()
            if not db_ingredient:
                db_ingredient = Ingredient(
                    name=ing.name,
                    category=ing.category,
                    base_unit=ing.unit,
                    perishable=ing.perishable,
                )
                session.add(db_ingredient)
                session.flush()

            ri = RecipeIngredient(
                recipe_id=recipe.id,
                ingredient_id=db_ingredient.id,
                quantity=ing.quantity,
                unit=ing.unit,
                notes=ing.notes,
            )
            session.add(ri)

        created_recipes.append(recipe)

    session.commit()
    return created_recipes


# ── Entry point ──────────────────────────────────────────────────────────────

def parse_menu(session: Session, restaurant_name: str,
               menu_image_path: str, location: str = "",
               menu_url: str = "") -> tuple[Restaurant, list[Recipe]]:
    """Menu photo → Claude vision → validated recipes → DB.

    Returns (restaurant, recipes). Skips Claude if this exact image
    was already parsed (matched by SHA-256 hash).
    """
    # Compute image hash for duplicate detection
    with open(menu_image_path, "rb") as f:
        image_bytes = f.read()
    menu_hash = hashlib.sha256(image_bytes).hexdigest()

    # Check if this exact image was already parsed
    existing = session.query(Restaurant).filter_by(menu_hash=menu_hash).first()
    if existing:
        recipes = session.query(Recipe).filter_by(restaurant_id=existing.id).all()
        if recipes:
            print(f"  Menu already parsed — found {len(recipes)} recipes (hash match)")
            return existing, recipes

    restaurant = Restaurant(
        name=restaurant_name,
        location=location,
        menu_source_url=menu_url,
        menu_hash=menu_hash,
    )
    session.add(restaurant)
    session.flush()
    print(f"Created restaurant: {restaurant.name} (id={restaurant.id})")

    print(f"Sending menu photo to Claude: {menu_image_path}")
    result = parse_menu_image(menu_image_path)

    all_ing_names = {ing.name for r in result.recipes for ing in r.ingredients}
    print(f"  Parsed {len(result.recipes)} recipes, {len(all_ing_names)} unique ingredients")

    recipes = store_parsed_recipes(session, restaurant.id, result)
    print(f"Menu parsing complete: {len(recipes)} recipes stored.")
    return restaurant, recipes
