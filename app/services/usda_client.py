"""Step 2: Fetch market price trends from the BLS Average Price Data API.

Uses the public BLS API v1 (no key required) to pull recent consumer prices
for common food items.  A single batch request fetches all series at once;
results are cached in the DB so we never need to call the API again for the
same pipeline run.

BLS Average Price series IDs follow the pattern APU0000XXXXXX where the
last digits identify the specific food item.
"""

import requests
import json
from datetime import datetime, date
from sqlalchemy.orm import Session
from pydantic import BaseModel
from app.models import Ingredient, USDAPrice, BLSCache

BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"


def _get_cached_data(session: Session) -> dict | None:
    """Return cached BLS data if it was fetched this month, else None."""
    current_month = date.today().strftime("%Y-%m")
    rows = session.query(BLSCache).filter_by(fetched_month=current_month).all()
    if not rows:
        return None
    return {
        row.series_id: {"description": row.description, "unit": row.unit, "data": json.loads(row.data_json)}
        for row in rows
    }


def _save_to_cache(session: Session, series_id: str, description: str,
                   unit: str, data_points: list):
    """Save BLS data to the cache for this month."""
    current_month = date.today().strftime("%Y-%m")
    existing = session.query(BLSCache).filter_by(
        series_id=series_id, fetched_month=current_month
    ).first()
    if existing:
        existing.description = description
        existing.unit = unit
        existing.data_json = json.dumps(data_points)
    else:
        session.add(BLSCache(
            series_id=series_id, fetched_month=current_month,
            description=description, unit=unit, data_json=json.dumps(data_points),
        ))

# ── BLS series ID → human-readable item + matching keywords ──────────────────
# Each entry: (series_id, bls_item_description, unit, [keywords])
# Keywords are used to fuzzy-match DB ingredients to BLS items.
# Full catalog scraped from https://download.bls.gov/pub/time.series/ap/ap.item

BLS_FOOD_SERIES = [
    # ── Grains & Bakery ──────────────────────────────────────────────────
    ("APU0000701111", "Flour, white, all purpose, per lb", "per lb",
     ["flour", "all purpose flour", "white flour", "ap flour"]),
    ("APU0000701311", "Rice, white, long grain, precooked, per lb", "per lb",
     ["precooked rice", "instant rice", "minute rice"]),
    ("APU0000701312", "Rice, white, long grain, uncooked, per lb", "per lb",
     ["rice", "white rice", "long grain rice", "jasmine rice", "basmati rice"]),
    ("APU0000701321", "Spaghetti, per lb", "per lb",
     ["spaghetti"]),
    ("APU0000701322", "Spaghetti and macaroni, per lb", "per lb",
     ["pasta", "macaroni", "noodle", "noodles", "penne", "linguine",
      "fettuccine", "rigatoni", "orzo", "farfalle", "fusilli"]),
    ("APU0000702111", "Bread, white, pan, per lb", "per lb",
     ["bread", "white bread"]),
    ("APU0000702112", "Bread, French, per lb", "per lb",
     ["french bread", "baguette", "ciabatta", "breadcrumbs"]),
    ("APU0000702212", "Bread, whole wheat, pan, per lb", "per lb",
     ["wheat bread", "whole wheat bread"]),
    ("APU0000702421", "Cookies, chocolate chip, per lb", "per lb",
     ["cookies", "chocolate chip cookies"]),
    ("APU0000702611", "Crackers, soda, salted, per lb", "per lb",
     ["crackers"]),

    # ── Beef ─────────────────────────────────────────────────────────────
    ("APU0000FC1101", "All uncooked ground beef, per lb", "per lb",
     ["ground beef", "hamburger"]),
    ("APU0000703111", "Ground chuck, 100% beef, per lb", "per lb",
     ["ground chuck", "chuck"]),
    ("APU0000703112", "Ground beef, 100% beef, per lb", "per lb",
     ["beef", "ground beef"]),
    ("APU0000703113", "Ground beef, lean and extra lean, per lb", "per lb",
     ["lean beef", "lean ground beef"]),
    ("APU0000FC2101", "All uncooked beef roasts, per lb", "per lb",
     ["beef roast", "roast beef"]),
    ("APU0000703213", "Chuck roast, USDA Choice, boneless, per lb", "per lb",
     ["chuck roast"]),
    ("APU0000703311", "Round roast, USDA Choice, boneless, per lb", "per lb",
     ["round roast"]),
    ("APU0000703411", "Rib roast, USDA Choice, bone-in, per lb", "per lb",
     ["rib roast", "prime rib"]),
    ("APU0000FC3101", "All uncooked beef steaks, per lb", "per lb",
     ["steak"]),
    ("APU0000703422", "Steak, T-Bone, USDA Choice, bone-in, per lb", "per lb",
     ["t-bone", "t bone steak"]),
    ("APU0000703425", "Steak, rib eye, USDA Choice, boneless, per lb", "per lb",
     ["rib eye", "ribeye"]),
    ("APU0000703431", "Short ribs, bone-in, per lb", "per lb",
     ["short ribs"]),
    ("APU0000703432", "Beef for stew, boneless, per lb", "per lb",
     ["beef stew", "stew beef", "stew meat"]),
    ("APU0000703511", "Steak, round, USDA Choice, boneless, per lb", "per lb",
     ["round steak"]),
    ("APU0000703613", "Steak, sirloin, USDA Choice, boneless, per lb", "per lb",
     ["sirloin", "sirloin steak"]),

    # ── Pork ─────────────────────────────────────────────────────────────
    ("APU0000704111", "Bacon, sliced, per lb", "per lb",
     ["bacon", "pancetta"]),
    ("APU0000FD3101", "All pork chops, per lb", "per lb",
     ["pork chop", "pork chops"]),
    ("APU0000704211", "Pork chops, center cut, bone-in, per lb", "per lb",
     ["center cut pork"]),
    ("APU0000704212", "Pork chops, boneless, per lb", "per lb",
     ["boneless pork"]),
    ("APU0000FD2101", "All ham, per lb", "per lb",
     ["ham", "prosciutto"]),
    ("APU0000704312", "Ham, boneless, excluding canned, per lb", "per lb",
     ["boneless ham"]),
    ("APU0000704413", "Shoulder picnic, bone-in, smoked, per lb", "per lb",
     ["pork shoulder"]),
    ("APU0000704421", "Sausage, fresh, loose, per lb", "per lb",
     ["sausage", "italian sausage", "pork sausage", "breakfast sausage"]),
    ("APU0000FD4101", "All other pork, per lb", "per lb",
     ["pork", "ground pork", "pork loin", "pork tenderloin"]),

    # ── Other Meats ──────────────────────────────────────────────────────
    ("APU0000705111", "Frankfurters, all meat, per lb", "per lb",
     ["hot dog", "frankfurter", "wiener"]),
    ("APU0000705121", "Bologna, all beef or mixed, per lb", "per lb",
     ["bologna", "lunch meat", "deli meat"]),
    ("APU0000705142", "Lamb and mutton, bone-in, per lb", "per lb",
     ["lamb", "lamb rack", "lamb chop", "mutton"]),

    # ── Poultry ──────────────────────────────────────────────────────────
    ("APU0000706111", "Chicken, fresh, whole, per lb", "per lb",
     ["whole chicken"]),
    ("APU0000706211", "Chicken breast, bone-in, per lb", "per lb",
     ["chicken breast"]),
    ("APU0000FF1101", "Chicken breast, boneless, per lb", "per lb",
     ["boneless chicken", "chicken tender", "chicken cutlet"]),
    ("APU0000706212", "Chicken legs, bone-in, per lb", "per lb",
     ["chicken leg", "chicken thigh", "drumstick", "chicken"]),
    ("APU0000706311", "Turkey, frozen, whole, per lb", "per lb",
     ["turkey"]),

    # ── Seafood ──────────────────────────────────────────────────────────
    ("APU0000707111", "Tuna, light, chunk, per lb", "per lb",
     ["tuna"]),

    # ── Dairy & Eggs ─────────────────────────────────────────────────────
    ("APU0000708111", "Eggs, grade A, large, per doz", "per dozen",
     ["egg", "eggs"]),
    ("APU0000709111", "Milk, fresh, whole, per half gal", "per half gal",
     ["milk", "whole milk"]),
    ("APU0000709112", "Milk, fresh, whole, per gal", "per gallon",
     ["gallon milk"]),
    ("APU0000FJ1101", "Milk, fresh, low-fat/skim, per gal", "per gallon",
     ["skim milk", "low fat milk", "2% milk", "reduced fat milk"]),
    ("APU0000710111", "Butter, salted, grade AA, per lb", "per lb",
     ["butter"]),
    ("APU0000FS1101", "Butter, stick, per lb", "per lb",
     ["butter stick"]),
    ("APU0000710122", "Yogurt, natural, fruit flavored, per 8 oz", "per 8 oz",
     ["yogurt"]),
    ("APU0000710211", "American processed cheese, per lb", "per lb",
     ["american cheese", "processed cheese"]),
    ("APU0000710212", "Cheddar cheese, natural, per lb", "per lb",
     ["cheddar", "cheddar cheese", "cheese", "swiss cheese",
      "mozzarella", "mozzarella cheese", "provolone", "gruyere",
      "parmesan", "parmesan cheese", "parmigiano", "pecorino",
      "ricotta", "ricotta cheese", "gorgonzola", "fontina",
      "gouda", "brie", "feta"]),
    ("APU0000710411", "Ice cream, prepackaged, per half gal", "per half gal",
     ["ice cream"]),

    # ── Fruits ───────────────────────────────────────────────────────────
    ("APU0000711111", "Apples, Red Delicious, per lb", "per lb",
     ["apple", "apples"]),
    ("APU0000711211", "Bananas, per lb", "per lb",
     ["banana", "bananas"]),
    ("APU0000711311", "Oranges, Navel, per lb", "per lb",
     ["orange", "oranges", "navel orange"]),
    ("APU0000711312", "Oranges, Valencia, per lb", "per lb",
     ["valencia orange"]),
    ("APU0000711411", "Grapefruit, per lb", "per lb",
     ["grapefruit"]),
    ("APU0000711412", "Lemons, per lb", "per lb",
     ["lemon", "lemons", "lemon juice"]),
    ("APU0000711413", "Pears, Anjou, per lb", "per lb",
     ["pear", "pears"]),
    ("APU0000711414", "Peaches, per lb", "per lb",
     ["peach", "peaches"]),
    ("APU0000711415", "Strawberries, per 12 oz", "per 12 oz",
     ["strawberry", "strawberries"]),
    ("APU0000711417", "Grapes, Thompson Seedless, per lb", "per lb",
     ["grape", "grapes"]),
    ("APU0000711418", "Cherries, per lb", "per lb",
     ["cherry", "cherries"]),

    # ── Vegetables (Fresh) ───────────────────────────────────────────────
    ("APU0000712112", "Potatoes, white, per lb", "per lb",
     ["potato", "potatoes", "sweet potato", "sweet potatoes", "yam", "yams"]),
    ("APU0000712211", "Lettuce, iceberg, per lb", "per lb",
     ["iceberg lettuce"]),
    ("APU0000FL2101", "Lettuce, romaine, per lb", "per lb",
     ["lettuce", "romaine", "romaine lettuce"]),
    ("APU0000712311", "Tomatoes, field grown, per lb", "per lb",
     ["tomato", "tomatoes", "cherry tomato", "cherry tomatoes"]),
    ("APU0000712401", "Cabbage, per lb", "per lb",
     ["cabbage"]),
    ("APU0000712402", "Celery, per lb", "per lb",
     ["celery"]),
    ("APU0000712403", "Carrots, short trimmed, per lb", "per lb",
     ["carrot", "carrots"]),
    ("APU0000712404", "Onions, dry yellow, per lb", "per lb",
     ["onion", "onions", "yellow onion", "shallots", "shallot"]),
    ("APU0000712406", "Peppers, sweet, per lb", "per lb",
     ["pepper", "bell pepper", "sweet pepper", "peppers"]),
    ("APU0000712407", "Corn on the cob, per lb", "per lb",
     ["corn"]),
    ("APU0000712409", "Cucumbers, per lb", "per lb",
     ["cucumber", "cucumbers"]),
    ("APU0000712410", "Beans, green, snap, per lb", "per lb",
     ["green beans", "snap beans", "string beans", "haricot verts"]),
    ("APU0000712411", "Mushrooms, per lb", "per lb",
     ["mushroom", "mushrooms", "mushroom caps"]),
    ("APU0000712412", "Broccoli, per lb", "per lb",
     ["broccoli"]),

    # ── Canned & Processed Vegetables/Fruits ─────────────────────────────
    ("APU0000714111", "Potatoes, frozen, French fried, per lb", "per lb",
     ["french fries", "frozen potatoes", "fries"]),
    ("APU0000714221", "Corn, canned, per lb", "per lb",
     ["canned corn"]),
    ("APU0000714231", "Tomatoes, canned, whole, per lb", "per lb",
     ["canned tomatoes", "crushed tomatoes", "diced tomatoes",
      "tomato sauce", "marinara"]),
    ("APU0000714233", "Beans, dried, any type, per lb", "per lb",
     ["beans", "dried beans", "black beans", "pinto beans",
      "kidney beans", "navy beans", "cannellini beans",
      "chickpeas", "garbanzo", "lentils"]),
    ("APU0000713111", "Orange juice, frozen concentrate, per 16 oz", "per 16 oz",
     ["orange juice"]),
    ("APU0000713311", "Apple sauce, per lb", "per lb",
     ["applesauce", "apple sauce"]),

    # ── Fats & Oils ──────────────────────────────────────────────────────
    ("APU0000716114", "Margarine, stick, per lb", "per lb",
     ["margarine"]),
    ("APU0000716121", "Shortening, vegetable oil, per lb", "per lb",
     ["shortening"]),
    ("APU0000716141", "Peanut butter, creamy, per lb", "per lb",
     ["peanut butter"]),

    # ── Sugar & Sweets ───────────────────────────────────────────────────
    ("APU0000715211", "Sugar, white, all sizes, per lb", "per lb",
     ["sugar", "white sugar", "granulated sugar"]),

    # ── Beverages ────────────────────────────────────────────────────────
    ("APU0000717114", "Cola, nondiet, per 2 liters", "per 2 liters",
     ["cola", "soda", "coke"]),
    ("APU0000FN1101", "All soft drinks, per 2 liters", "per 2 liters",
     ["soft drink", "sprite", "fanta"]),
    ("APU0000717311", "Coffee, 100%, ground roast, per lb", "per lb",
     ["coffee"]),
    ("APU0000717327", "Coffee, instant, plain, per lb", "per lb",
     ["instant coffee"]),
    ("APU0000718311", "Potato chips, per 16 oz", "per 16 oz",
     ["potato chips", "chips"]),

    # ── Alcohol ──────────────────────────────────────────────────────────
    ("APU0000720111", "Malt beverages, all types, per 16 oz", "per 16 oz",
     ["beer", "ale", "lager", "malt"]),
    ("APU0000720311", "Wine, red and white table, per 1 liter", "per liter",
     ["wine", "red wine", "white wine", "pinot grigio", "cabernet",
      "chardonnay", "merlot", "pinot noir", "sauvignon blanc",
      "port wine", "marsala"]),
    ("APU0000720222", "Vodka, all types, per 1 liter", "per liter",
     ["vodka", "spirits", "liquor"]),

    # ── Misc Prepared ────────────────────────────────────────────────────
    ("APU0000718631", "Pork and beans, canned, per 16 oz", "per 16 oz",
     ["pork and beans", "baked beans"]),

]


def _fetch_bls_prices(series_ids: list[str], start_year: int, end_year: int) -> dict:
    """
    Single batch call to BLS API v1. Returns {series_id: [data_points]}.
    BLS v1 allows up to 25 series per request, no key needed.
    """
    all_data: dict[str, list] = {}

    # Chunk into batches of 25 (BLS v1 limit)
    for i in range(0, len(series_ids), 25):
        batch = series_ids[i:i + 25]
        payload = {
            "seriesid": batch,
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        result = None
        for attempt in range(3):
            try:
                resp = requests.post(BLS_API_URL, json=payload, timeout=60)
                resp.raise_for_status()
                result = resp.json()
                break
            except requests.exceptions.Timeout:
                print(f"  BLS API timeout (attempt {attempt + 1}/3), retrying...")
        if not result:
            print(f"  BLS API failed after 3 attempts, skipping batch")
            continue

        if result.get("status") != "REQUEST_SUCCEEDED":
            print(f"  BLS API warning: {result.get('message', 'unknown error')}")
            continue

        for series in result.get("Results", {}).get("series", []):
            sid = series["seriesID"]
            all_data[sid] = series.get("data", [])

    return all_data


class BLSIngredientMatch(BaseModel):
    ingredient_name: str
    series_id: str | None

class BLSMatchResult(BaseModel):
    matches: list[BLSIngredientMatch]

_BLS_SCHEMA_JSON = json.dumps(BLSMatchResult.model_json_schema(), indent=2)


def _match_ingredients_with_claude(ingredient_names: list[str]) -> dict[str, tuple | None]:
    """
    Use Claude to match ingredient names to BLS series.
    Sends one batch request with all ingredients and all BLS items.
    Returns {ingredient_name: (series_id, description, unit) or None}.
    """
    import os
    import anthropic

    # Build the BLS items list for the prompt (deduplicated by series_id)
    seen = set()
    bls_items_text = []
    series_lookup = {}
    for series_id, description, unit, _ in BLS_FOOD_SERIES:
        if series_id not in seen:
            seen.add(series_id)
            bls_items_text.append(f"- {series_id}: {description}")
            series_lookup[series_id] = (series_id, description, unit)

    bls_list = "\n".join(bls_items_text)
    ingredients_list = "\n".join(f"- {name}" for name in ingredient_names)

    from app.services.prompts import get_bls_match_prompt
    prompt = get_bls_match_prompt(bls_list, ingredients_list, _BLS_SCHEMA_JSON)

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — falling back to keyword matching")
        return {name: _keyword_match(name) for name in ingredient_names}

    client = anthropic.Anthropic(api_key=api_key)
    print("  Asking Claude to match ingredients to BLS series...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    # Parse the JSON response with Pydantic
    from app.utils import strip_json_fences
    response_text = strip_json_fences(response.content[0].text)

    try:
        parsed = BLSMatchResult.model_validate_json(response_text)
        matches = {m.ingredient_name: m.series_id for m in parsed.matches}
    except Exception as e:
        print(f"  Claude response failed validation: {e}")
        print("  Falling back to keyword matching")
        return {name: _keyword_match(name) for name in ingredient_names}

    # Convert to our tuple format
    result = {}
    for name in ingredient_names:
        series_id = matches.get(name)
        if series_id and series_id in series_lookup:
            result[name] = series_lookup[series_id]
        else:
            result[name] = None

    return result


def _keyword_match(ingredient_name: str) -> tuple | None:
    """Fallback keyword matcher if Claude is unavailable."""
    name_lower = ingredient_name.lower().strip()
    best_match = None
    best_score = 0

    for series_id, description, unit, keywords in BLS_FOOD_SERIES:
        for kw in keywords:
            kw_lower = kw.lower()
            if name_lower == kw_lower:
                return (series_id, description, unit)
            if kw_lower in name_lower:
                score = len(kw_lower) / len(name_lower)
                if score > best_score:
                    best_score = score
                    best_match = (series_id, description, unit)
            elif name_lower in kw_lower:
                score = len(name_lower) / len(kw_lower) * 0.8
                if score > best_score:
                    best_score = score
                    best_match = (series_id, description, unit)

    if best_score >= 0.3:
        return best_match
    return None


def fetch_market_trends(session: Session, restaurant_id: int = None) -> list[USDAPrice]:
    """
    Full Step 2 pipeline:
    1. Check bls_cache table for this month's data (persists across pipeline resets)
    2. If cache miss, make ONE batch BLS API call and store in cache
    3. Match ingredients to cached series and write USDAPrice records
    """
    if restaurant_id:
        ingredients = session.query(Ingredient).filter_by(restaurant_id=restaurant_id).all()
    else:
        ingredients = session.query(Ingredient).all()
    if not ingredients:
        print("No ingredients found. Run Step 1 first.")
        return []

    # ── Check / populate the persistent BLS cache ────────────────────────
    cached = _get_cached_data(session)

    if cached:
        print(f"Step 2: Using cached BLS data ({len(cached)} series, "
              f"fetched {date.today().strftime('%B %Y')}).")
    else:
        # Need to fetch from BLS API
        all_series_ids = list({s[0] for s in BLS_FOOD_SERIES})
        current_year = datetime.now().year
        print(f"Fetching BLS price data for {len(all_series_ids)} food series "
              f"({current_year - 1}–{current_year})...")
        bls_data = _fetch_bls_prices(all_series_ids, current_year - 1, current_year)

        # Build a lookup from series_id -> (description, unit)
        series_info = {s[0]: (s[1], s[2]) for s in BLS_FOOD_SERIES}

        # Save each series to cache
        cached = {}
        for series_id, data_points in bls_data.items():
            desc, unit = series_info.get(series_id, ("Unknown", ""))
            _save_to_cache(session, series_id, desc, unit, data_points)
            cached[series_id] = {"description": desc, "unit": unit, "data": data_points}
        session.commit()
        print(f"  Cached {len(cached)} series to bls_cache")

    # ── Clear any old USDAPrice rows (pipeline DB may have been reset) ───
    session.query(USDAPrice).delete()
    session.flush()

    # ── Match ingredients to cached BLS data (Claude or fallback) ──────
    # Only call Claude for ingredients that don't already have a usda_id
    needs_matching = [ing for ing in ingredients if not ing.usda_id]
    already_matched = [ing for ing in ingredients if ing.usda_id]

    if already_matched:
        print(f"  {len(already_matched)} ingredients already have BLS IDs")

    # Build the match map from existing + new Claude matches
    # Deduplicated lookup for series_id -> (series_id, description, unit)
    series_lookup = {s[0]: (s[0], s[1], s[2]) for s in BLS_FOOD_SERIES}

    match_map: dict[str, tuple | None] = {}
    for ing in already_matched:
        match_map[ing.name] = series_lookup.get(ing.usda_id)

    if needs_matching:
        claude_matches = _match_ingredients_with_claude([ing.name for ing in needs_matching])
        match_map.update(claude_matches)
        # Persist usda_id on each ingredient
        for ing in needs_matching:
            match = claude_matches.get(ing.name)
            if match:
                ing.usda_id = match[0]  # series_id
        session.flush()

    records = []
    for ing in ingredients:
        match = match_map.get(ing.name)
        if not match:
            print(f"  No BLS price match for '{ing.name}' — skipping")
            continue

        series_id, _, _ = match
        cache_entry = cached.get(series_id)
        if not cache_entry:
            continue

        description = cache_entry["description"]
        unit = cache_entry["unit"]
        data_points = cache_entry["data"]

        # Filter out unavailable data points (value == "-")
        data_points = [dp for dp in data_points if dp.get("value", "-") != "-"]
        if not data_points:
            print(f"  No price data for '{ing.name}' ({series_id})")
            continue

        # Most recent data point
        latest = data_points[0]  # BLS returns newest first
        price_val = float(latest.get("value", 0))
        period = latest.get("periodName", "")
        year = latest.get("year", "")
        date_str = f"{period} {year}"
        try:
            price_date = datetime.strptime(f"1 {date_str}", "%d %B %Y").date()
        except ValueError:
            price_date = date.today()

        # Build a trend summary from available data points
        trend_prices = [float(dp["value"]) for dp in data_points[:6] if dp.get("value")]
        if len(trend_prices) >= 2:
            change = trend_prices[0] - trend_prices[-1]
            direction = "↑" if change > 0 else "↓" if change < 0 else "→"
            trend_note = f"{direction} ${abs(change):.2f} over {len(trend_prices)} months"
        else:
            trend_note = "insufficient data for trend"

        record = USDAPrice(
            ingredient_id=ing.id,
            usda_item_name=description,
            price=price_val,
            unit=unit,
            date=price_date,
            source=f"BLS Average Price | {series_id} | Trend: {trend_note}",
        )
        session.add(record)
        records.append(record)
        print(f"  {ing.name} → '{description}' | ${price_val:.2f} {unit} ({date_str}) {trend_note}")

    session.commit()
    print(f"Step 2 complete: {len(records)} price records stored.")
    return records

