"""Shared utilities for the RFP pipeline."""

from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Ingredient, Recipe, RecipeIngredient


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences from LLM JSON responses."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return text


# ── Unit conversion ────────────────────────────────────────────────────────

# Base values: weight in grams, volume in ml
_WEIGHT_TO_G = {
    "g": 1.0,
    "kg": 1000.0,
    "oz": 28.3495,
    "lb": 453.592,
}

_VOLUME_TO_ML = {
    "ml": 1.0,
    "l": 1000.0,
    "tsp": 4.92892,
    "tbsp": 14.7868,
    "cup": 236.588,
    "pt": 473.176,
    "qt": 946.353,
    "gal": 3785.41,
}

# Build full conversion table: {(from, to): multiplier}
_CONVERSIONS: dict[tuple[str, str], float] = {}

for _table in (_WEIGHT_TO_G, _VOLUME_TO_ML):
    for _a, _a_base in _table.items():
        for _b, _b_base in _table.items():
            if _a != _b:
                _CONVERSIONS[(_a, _b)] = _a_base / _b_base


def convert_quantity(qty: float, from_unit: str, to_unit: str) -> float | None:
    """Convert a quantity between compatible units.

    Returns the converted value, or None if the units are incompatible
    (e.g., weight to volume, or 'each' to anything).
    """
    from_unit = from_unit.lower()
    to_unit = to_unit.lower()
    if from_unit == to_unit:
        return qty
    factor = _CONVERSIONS.get((from_unit, to_unit))
    if factor is None:
        return None
    return qty * factor


def aggregate_quantities(
    recipe_ings: list[RecipeIngredient],
    weekly_covers: int,
    ingredients_map: dict[int, Ingredient],
    recipes_map: dict[int, Recipe] | None = None,
    weekly_covers_by_category: dict[str, int] | None = None,
) -> dict[int, tuple[float, str]]:
    """Aggregate per-serving quantities into weekly totals, normalizing units.

    Converts each quantity to the ingredient's base_unit before summing.
    If recipes_map is provided, applies each recipe's popularity_multiplier
    and optional category-level weekly cover overrides.
    Falls back to raw summation if units are incompatible.

    Returns {ingredient_id: (total_weekly_qty, unit_label)}.
    """
    qty_map: dict[int, tuple[float, str]] = {}
    for ri in recipe_ings:
        ing = ingredients_map.get(ri.ingredient_id)
        if not ing:
            continue
        target_unit = ing.base_unit or ri.unit
        converted = convert_quantity(ri.quantity, ri.unit, target_unit)
        if converted is None:
            # Incompatible units — sum as-is using first unit seen
            converted = ri.quantity
            if ri.ingredient_id in qty_map:
                target_unit = qty_map[ri.ingredient_id][1]
            # else target_unit stays as ri.unit

        # Apply per-dish popularity scaling and optional category overrides
        popularity = 1.0
        recipe_weekly_covers = weekly_covers
        if recipes_map:
            recipe = recipes_map.get(ri.recipe_id)
            if recipe:
                if weekly_covers_by_category and recipe.category in weekly_covers_by_category:
                    recipe_weekly_covers = weekly_covers_by_category[recipe.category]
                if recipe.popularity_multiplier:
                    popularity = recipe.popularity_multiplier

        scaled = converted * recipe_weekly_covers * popularity

        if ri.ingredient_id not in qty_map:
            qty_map[ri.ingredient_id] = (scaled, target_unit)
        else:
            total, unit = qty_map[ri.ingredient_id]
            qty_map[ri.ingredient_id] = (total + scaled, unit)

    return qty_map


def load_category_cover_overrides_from_env(
    env_var: str = "RFP_WEEKLY_COVERS_BY_CATEGORY",
) -> dict[str, int]:
    """Load optional category-level cover overrides from JSON in env.

    Expected format:
    {"Seafood": 8, "Bakery & Breads": 180}
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    cleaned: dict[str, int] = {}
    for k, v in parsed.items():
        if not isinstance(k, str):
            continue
        try:
            cleaned[k] = max(0, int(v))
        except (TypeError, ValueError):
            continue
    return cleaned


def estimate_category_weekly_covers(
    recipes: list[Recipe],
    baseline_weekly_covers: int,
    env_overrides: dict[str, int] | None = None,
) -> dict[str, int]:
    """Estimate per-category weekly covers from Claude popularity multipliers."""
    by_category: dict[str, list[float]] = {}
    for recipe in recipes:
        category = recipe.category or "Other"
        popularity = recipe.popularity_multiplier or 1.0
        est = max(0.0, baseline_weekly_covers * popularity)
        by_category.setdefault(category, []).append(est)

    estimated = {
        category: int(round(sum(vals) / len(vals)))
        for category, vals in by_category.items()
        if vals
    }

    if env_overrides:
        estimated.update(env_overrides)

    return dict(sorted(estimated.items()))
