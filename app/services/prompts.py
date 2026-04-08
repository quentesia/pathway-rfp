"""Centralized prompts for the RFP Pipeline AI agents."""

def get_menu_parse_prompt(schema_json: str) -> str:
    return f"""You are a professional chef and food scientist.
You will be given a photo of a restaurant menu.

Your job: for EVERY dish on the menu, produce a complete recipe with
realistic ingredients and quantities for ONE serving.

CRITICAL RULES:
- ONLY include dishes that are clearly visible and readable on the menu. Do NOT invent or hallucinate dishes.
- Include ALL dishes visible on the menu. Do not skip any.
- Use the dish description from the menu as-is for the "description" field.
- DEDUPLICATE ingredients: if multiple dishes use the same ingredient,
  use the EXACT same "name" string every time. e.g. always "Olive Oil",
  never sometimes "Olive Oil" and sometimes "Extra Virgin Olive Oil"
  unless they are truly different products. Decide this based on how you would request a distiibutor for items.
- It should always be items attainable from a food distributor. Else, break it down into items that can be attained from a food distributor.
  e.g. If the menu says "Mixed Cheeses", break it down into cheeses that typicall go into the meal. 
- Infer realistic restaurant-scale quantities per serving.
- Estimate a "popularity_multiplier" for each dish: how many times more (or fewer)
  orders this dish gets compared to the average dish. Use 1.0 as the baseline average.
  A popular staple like a burger or pasta might be 2.0–3.0. A niche/expensive item
  like a lobster special might be 0.2–0.5. A mid-range entrée stays near 1.0.
- Include everything a kitchen would need: proteins, produce, seasonings,
  oils, garnishes, sauces.
- Units MUST be one of: each, pinch, tsp, tbsp, cup, pt, qt, gal, ml, l, g, kg, oz, lb
- Categories MUST be one of: Produce, Meat & Poultry, Seafood, Dairy & Eggs,
  Dry Goods & Pantry, Frozen Foods, Bakery & Breads, Beverages, Oils, Fats & Sauces, Other

Your response MUST be valid JSON conforming to this exact schema:

{schema_json}

Return ONLY the JSON. No markdown fences, no commentary."""


def get_bls_match_prompt(bls_list: str, ingredients_list: str, schema_json: str) -> str:
    return f"""Match each restaurant ingredient to the MOST APPROPRIATE BLS food price series.

RULES:
- Only match if the BLS item is a reasonable price proxy for the ingredient. The price market trends
  should be expected to follow the one of the BLS item. If its even loosely similar, match it. 
  If they fall in the same bracket of food type like condiments etc., match it.
- "Eggplant" should NOT match "Eggs" — they are completely different foods
- "Chicken Stock" or "Beef Stock" are liquid products — do NOT match to raw meat prices
- "Red Wine Vinegar" is vinegar, NOT wine — do not match to wine
- "Olive Oil" is cooking oil — match it if there's a suitable oil/fat series
- Spices, herbs, exotic items (saffron, capers, etc.) → NONE
- Compound items like "Ricotta Spinach Ravioli" → NONE (it's a prepared product)
- Stocks/broths → NONE (not the same as raw meat)

BLS PRICE SERIES:
{bls_list}

INGREDIENTS TO MATCH:
{ingredients_list}

Your response MUST be valid JSON conforming to this exact schema:

{schema_json}

Return ONLY the JSON. No markdown fences, no commentary."""


DISTRIBUTOR_PROMPT = """You are a restaurant supply chain expert. Given a city/region and a list
of ingredient categories, identify real, well-known food distributors that operate in that area.

Your response MUST be valid JSON conforming to this exact schema:

{schema_json}

Return ONLY the JSON. No markdown fences, no commentary.

Rules:
- Return 3-6 distributors
- Include a mix of broadline (e.g., Sysco, US Foods) and specialty distributors
- Prefer distributors known to operate in the specified region
- Each distributor should list which ingredient categories they serve
- Use realistic contact info formats

City/Region: {location}
Ingredient categories needed: {categories}
"""


def get_quote_parse_prompt(email_body: str, requested_ingredients: str, schema_json: str) -> str:
    return f"""You are a procurement assistant. Parse the following email reply from a food distributor into structured quote data.

We requested quotes for these specific ingredients:
{requested_ingredients}

RULES:
- Extract all pricing, units, delivery notes, and delivery charge details for any mentioned ingredients.
- For `not_supplied`: include ONLY ingredients the distributor EXPLICITLY says they cannot supply.
- For `clarification_needed`: include ingredients that are missing key details (price/unit), OR were omitted entirely from the reply.
- Do NOT assume omitted ingredients are not supplied unless the distributor explicitly said so.

Email body you must parse:
{email_body}

Your response MUST be valid JSON conforming to this exact schema:

{schema_json}

Return ONLY the JSON. No markdown fences, no commentary."""
