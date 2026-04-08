"""Streamlit UI: visualizes each step of the RFP pipeline."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.models import (
    Restaurant, Recipe, Ingredient, RecipeIngredient,
    USDAPrice, Distributor, DistributorIngredient,
)
from app.services.menu_parser import parse_menu
from app.services.usda_client import fetch_market_trends
from app.services.distributor_finder import find_local_distributors
from app.services.email_sender import send_rfp_emails
from app.services.inbox_monitor import collect_quotes

load_dotenv()
init_db()

st.set_page_config(page_title="Pathway RFP Pipeline", layout="wide")
st.title("Pathway RFP Pipeline")
st.markdown("End-to-end: Menu parsing, USDA pricing, distributor search, and RFP emails.")

# ── Red skip button styling ─────────────────────────────────────────────────
st.markdown("""
<style>
/* Make skip buttons red instead of default blue primary */
button[kind="primary"] {
    background-color: #d32f2f !important;
    border-color: #d32f2f !important;
}
button[kind="primary"]:hover {
    background-color: #b71c1c !important;
    border-color: #b71c1c !important;
}
</style>
""", unsafe_allow_html=True)


def _check_existing_data(restaurant_id: int) -> dict:
    """Check which pipeline steps already have data in DB for this restaurant."""
    session = SessionLocal()
    try:
        ingredient_ids = [ri.ingredient_id for ri in
            session.query(RecipeIngredient).join(Recipe).filter(
                Recipe.restaurant_id == restaurant_id
            ).all()]
        has_step2 = bool(ingredient_ids and session.query(USDAPrice).filter(
            USDAPrice.ingredient_id.in_(ingredient_ids)
        ).first())
        has_step3 = session.query(Distributor).first() is not None
        has_step4 = session.query(Distributor).filter(
            Distributor.rfp_status != "pending"
        ).first() is not None
        has_step5 = session.query(DistributorIngredient).filter(
            DistributorIngredient.quoted_price.isnot(None)
        ).first() is not None
        return {"step2": has_step2, "step3": has_step3, "step4": has_step4, "step5": has_step5}
    finally:
        session.close()

# ── Sidebar: Configuration ───────────────────────────────────────────────────

with st.sidebar:
    st.header("Configuration")
    restaurant_name = st.text_input("Restaurant Name", "My Restaurant")
    restaurant_location = st.text_input("Location", "Atlanta, GA")
    menu_url = st.text_input("Menu Source URL (optional)", "")
    weekly_covers = st.number_input(
        "Est. weekly covers per dish",
        min_value=1, max_value=500, value=40,
        help="How many times each dish is ordered per week. Used to estimate procurement quantities.",
    )

# ── Session state ────────────────────────────────────────────────────────────

if "pipeline_state" not in st.session_state:
    st.session_state.pipeline_state = {
        "restaurant_id": None,
        "step1_done": False,
        "step2_done": False,
        "step3_done": False,
        "step4_done": False,
        "step5_done": False,
    }

ps = st.session_state.pipeline_state
existing = st.session_state.get("existing_data", {})

# ── Step 1: Menu -> Recipes ──────────────────────────────────────────────────

st.header("Step 1: Menu -> Recipes & Ingredients")
st.markdown("Upload a menu photo or select an existing restaurant from the database.")

input_tab1, input_tab2 = st.tabs(["Upload Photo", "Use Existing"])

menu_image_path = None
with input_tab1:
    uploaded_file = st.file_uploader("Upload menu photo", type=["png", "jpg", "jpeg", "webp"])
    if uploaded_file:
        import tempfile
        ext = Path(uploaded_file.name).suffix or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(uploaded_file.getvalue())
            menu_image_path = tmp.name
        st.image(uploaded_file, caption="Uploaded menu", use_container_width=True)

with input_tab2:
    session = SessionLocal()
    try:
        existing_restaurants = session.query(Restaurant).all()
    finally:
        session.close()

    if existing_restaurants:
        options = {r.name: r.id for r in existing_restaurants}
        selected = st.selectbox("Select a restaurant", list(options.keys()))
        if st.button("Load Restaurant"):
            ps["restaurant_id"] = options[selected]
            ps["step1_done"] = True
            st.session_state.existing_data = _check_existing_data(options[selected])
            st.rerun()
    else:
        st.info("No restaurants in database yet. Parse a menu first.")

has_input = menu_image_path is not None

if st.button("Parse Menu", disabled=not has_input):
    with st.spinner("Parsing menu into structured recipes..."):
        session = SessionLocal()
        try:
            restaurant, recipes = parse_menu(
                session,
                restaurant_name=restaurant_name,
                menu_image_path=menu_image_path,
                location=restaurant_location,
                menu_url=menu_url,
            )
            ps["restaurant_id"] = restaurant.id
            ps["step1_done"] = True
            st.success(f"Parsed {len(recipes)} recipes!")
        finally:
            session.close()

# Show Step 1 results
if ps["step1_done"]:
    session = SessionLocal()
    try:
        recipes = session.query(Recipe).filter_by(restaurant_id=ps["restaurant_id"]).all()
        # Batch load all ingredients for all recipes
        recipe_ids = [r.id for r in recipes]
        all_ris = session.query(RecipeIngredient).filter(
            RecipeIngredient.recipe_id.in_(recipe_ids)
        ).all()
        ing_ids = {ri.ingredient_id for ri in all_ris}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(ing_ids)
        ).all()}

        for recipe in recipes:
            with st.expander(f"{recipe.dish_name} ({recipe.category})"):
                ris = [ri for ri in all_ris if ri.recipe_id == recipe.id]
                rows = []
                for ri in ris:
                    ing = ingredients_map[ri.ingredient_id]
                    rows.append({
                        "Ingredient": ing.name,
                        "Quantity": ri.quantity,
                        "Unit": ri.unit,
                        "Category": ing.category,
                        "Perishable": ing.perishable,
                    })
                if rows:
                    st.table(rows)
    finally:
        session.close()

# ── Step 2: Market Price Trends ──────────────────────────────────────────────

st.header("Step 2: Market Price Trends")
st.markdown("Fetches recent consumer price data from the BLS Average Price Data API to gauge ingredient costs.")

_s2_has_existing = existing.get("step2") and not ps["step2_done"]
if _s2_has_existing:
    col_run, col_skip = st.columns([3, 1])
    with col_run:
        _s2_run = st.button("Fetch Market Trends", disabled=not ps["step1_done"])
    with col_skip:
        if st.button("⏭ Skip — use existing", key="skip2", type="primary", disabled=not ps["step1_done"]):
            ps["step2_done"] = True
            st.rerun()
else:
    _s2_run = st.button("Fetch Market Trends", disabled=not ps["step1_done"])

if _s2_run:
    with st.spinner("Fetching market price trends from BLS API..."):
        session = SessionLocal()
        try:
            total_ingredients = session.query(Ingredient).filter_by(restaurant_id=ps["restaurant_id"]).count()
            records = fetch_market_trends(session, restaurant_id=ps["restaurant_id"])
            ps["step2_done"] = True
            st.success(f"Fetched price data for {len(records)} out of {total_ingredients} ingredients!")
        except Exception as e:
            st.error(str(e))
        finally:
            session.close()

if ps["step2_done"]:
    session = SessionLocal()
    try:
        records = session.query(USDAPrice).all()
        ing_ids = {r.ingredient_id for r in records}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(ing_ids)
        ).all()}
        rows = []
        for r in records:
            ing = ingredients_map[r.ingredient_id]
            source_parts = r.source.split(" | ")
            trend_info = source_parts[-1] if len(source_parts) > 1 else ""
            rows.append({
                "Ingredient": ing.name,
                "BLS Match": r.usda_item_name,
                "Price": f"${r.price:.2f}" if r.price else "N/A",
                "Unit": r.unit or "N/A",
                "Period": r.date.strftime("%B %Y") if r.date else "N/A",
                "Trend": trend_info,
            })
        if rows:
            st.dataframe(rows, use_container_width=True)
    finally:
        session.close()

# ── Step 3: Find Distributors ────────────────────────────────────────────────

st.header("Step 3: Find Local Distributors")
st.markdown("Searches for food distributors in the restaurant's area.")

_s3_has_existing = existing.get("step3") and not ps["step3_done"]
if _s3_has_existing:
    col_run, col_skip = st.columns([3, 1])
    with col_run:
        _s3_run = st.button("Find Distributors", disabled=not ps["step1_done"])
    with col_skip:
        if st.button("⏭ Skip — use existing", key="skip3", type="primary", disabled=not ps["step1_done"]):
            ps["step3_done"] = True
            st.rerun()
else:
    _s3_run = st.button("Find Distributors", disabled=not ps["step1_done"])

if _s3_run:
    with st.spinner("Searching for distributors..."):
        session = SessionLocal()
        try:
            distributors = find_local_distributors(session, restaurant_location, restaurant_id=ps["restaurant_id"])
            ps["step3_done"] = True
            with_email = sum(1 for d in distributors if d.email and not d.email.startswith("form:"))
            with_form = sum(1 for d in distributors if d.email and d.email.startswith("form:"))
            parts = [f"Found {len(distributors)} distributors"]
            parts.append(f"{with_email} with email")
            if with_form:
                parts.append(f"{with_form} with contact form")
            st.success(" — ".join(parts) + "!")
        finally:
            session.close()

if ps["step3_done"]:
    session = SessionLocal()
    try:
        distributors = session.query(Distributor).all()
        # Batch load all ingredient links and ingredients
        dist_ids = [d.id for d in distributors]
        all_links = session.query(DistributorIngredient).filter(
            DistributorIngredient.distributor_id.in_(dist_ids)
        ).all()
        link_ing_ids = {l.ingredient_id for l in all_links}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(link_ing_ids)
        ).all()} if link_ing_ids else {}

        contactable = [d for d in distributors if d.email]
        phone_only = [d for d in distributors if not d.email]

        def _show_distributor(dist):
            if dist.rating and dist.rating_count:
                confidence = min(dist.rating_count / 50, 1.0)
                weighted = dist.rating * confidence
                rating_str = f"{dist.rating} ({dist.rating_count} reviews) — weighted: {weighted:.1f}/5"
            elif dist.rating:
                rating_str = f"{dist.rating} (no review count)"
            else:
                rating_str = "N/A"

            with st.expander(f"{dist.name} — {dist.location}"):
                st.write(f"**Rating:** {rating_str}")
                st.write(f"**Phone:** {dist.phone or 'N/A'}")
                if dist.email and dist.email.startswith("form:"):
                    form_url = dist.email[5:]
                    st.write(f"**Email:** [Contact Form]({form_url})")
                else:
                    st.write(f"**Email:** {dist.email or 'N/A'}")
                st.write(f"**Website:** {dist.website or 'N/A'}")
                st.write(f"**Source:** {dist.source}")

                links = [l for l in all_links if l.distributor_id == dist.id]
                ing_names = [ingredients_map[l.ingredient_id].name for l in links if l.ingredient_id in ingredients_map]
                if ing_names:
                    st.write(f"**Possible Supplies:** {', '.join(ing_names)}")

        for dist in contactable:
            _show_distributor(dist)

        if phone_only:
            st.subheader(f"Phone Only ({len(phone_only)})")
            st.caption("No email or website found — requires a phone call.")
            for dist in phone_only:
                _show_distributor(dist)
    finally:
        session.close()

# ── Step 4: Send RFP Emails ─────────────────────────────────────────────────

st.header("Step 4: Send RFP Emails")
st.markdown("Composes and sends RFP emails to each distributor requesting price quotes.")

_s4_has_existing = existing.get("step4") and not ps["step4_done"]
if _s4_has_existing:
    col_run, col_skip = st.columns([3, 1])
    with col_run:
        _s4_run = st.button("Send Emails", disabled=not ps["step3_done"])
    with col_skip:
        if st.button("⏭ Skip — use existing", key="skip4", type="primary", disabled=not ps["step3_done"]):
            ps["step4_done"] = True
            st.rerun()
else:
    _s4_run = st.button("Send Emails", disabled=not ps["step3_done"])

if _s4_run:
    with st.spinner("Sending RFP emails..."):
        session = SessionLocal()
        try:
            processed = send_rfp_emails(
                session, ps["restaurant_id"], mock_recipient="demo",
                weekly_covers=weekly_covers,
            )
            ps["step4_done"] = True
            sent = sum(1 for d in processed if d.rfp_status == "sent")
            forms = sum(1 for d in processed if d.rfp_status == "form_ready")
            skipped = sum(1 for d in processed if d.rfp_status == "skipped")
            parts = [f"{sent} emailed"]
            if forms:
                parts.append(f"{forms} forms ready")
            if skipped:
                parts.append(f"{skipped} skipped (phone only)")
            st.success(f"RFP outreach: {' — '.join(parts)} out of {len(processed)} distributors")
        finally:
            session.close()

if ps["step4_done"]:
    session = SessionLocal()
    try:
        distributors = session.query(Distributor).filter(
            Distributor.rfp_status != "pending"
        ).all()
        # Batch load ingredient links and names
        dist_ids = [d.id for d in distributors]
        all_links = session.query(DistributorIngredient).filter(
            DistributorIngredient.distributor_id.in_(dist_ids)
        ).all()
        link_ing_ids = {l.ingredient_id for l in all_links}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(link_ing_ids)
        ).all()} if link_ing_ids else {}

        for dist in distributors:
            with st.expander(f"[{dist.rfp_status}] {dist.name}"):
                links = [l for l in all_links if l.distributor_id == dist.id]
                ing_names = [ingredients_map[l.ingredient_id].name for l in links if l.ingredient_id in ingredients_map]
                st.write(f"**Ingredients requested:** {', '.join(ing_names)}")
                if dist.rfp_sent_at:
                    st.write(f"**Sent:** {dist.rfp_sent_at.strftime('%Y-%m-%d %H:%M')}")
    finally:
        session.close()

# ── Step 5: Collect & Compare Quotes ─────────────────────────

st.header("Step 5: Collect & Compare Quotes")
st.markdown("Monitors inbox for distributor replies, parses quotes, and compiles a comparison.")

_s5_has_existing = existing.get("step5") and not ps["step5_done"]
if _s5_has_existing:
    col_run, col_skip = st.columns([3, 1])
    with col_run:
        _s5_run = st.button("Check Inbox", disabled=not ps["step4_done"])
    with col_skip:
        if st.button("⏭ Skip — use existing", key="skip5", type="primary", disabled=not ps["step4_done"]):
            ps["step5_done"] = True
            st.rerun()
else:
    _s5_run = st.button("Check Inbox", disabled=not ps["step4_done"])

if _s5_run:
    with st.spinner("Monitoring inbox for replies..."):
        session = SessionLocal()
        try:
            updated = collect_quotes(session, ps["restaurant_id"], mock_recipient="demo")
            ps["step5_done"] = True
            st.success(f"Updated prices for {len(updated)} ingredients!")
        finally:
            session.close()

if ps["step5_done"]:
    session = SessionLocal()
    try:
        # Get all quoted ingredients
        quoted = session.query(DistributorIngredient).filter(
            DistributorIngredient.quoted_price.isnot(None)
        ).all()

        if quoted:
            # Batch load distributors, ingredients, and BLS prices
            dist_ids = {l.distributor_id for l in quoted}
            ing_ids = {l.ingredient_id for l in quoted}
            distributors_map = {d.id: d for d in session.query(Distributor).filter(
                Distributor.id.in_(dist_ids)
            ).all()}
            ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
                Ingredient.id.in_(ing_ids)
            ).all()}
            bls_map = {b.ingredient_id: b for b in session.query(USDAPrice).filter(
                USDAPrice.ingredient_id.in_(ing_ids)
            ).all()}

            comparison = []
            for link in quoted:
                dist = distributors_map[link.distributor_id]
                ing = ingredients_map[link.ingredient_id]
                bls = bls_map.get(link.ingredient_id)

                if bls and bls.price and link.quoted_price:
                    bls_str = f"${bls.price:.2f}/{bls.unit or 'unit'}"
                    diff_pct = ((link.quoted_price - bls.price) / bls.price) * 100
                    if diff_pct > 20:
                        flag = f"+{diff_pct:.0f}% vs retail"
                    elif diff_pct < -10:
                        flag = f"{diff_pct:.0f}% below retail"
                    else:
                        flag = "~ near retail"
                    trend = ""
                    if bls.source and " | " in bls.source:
                        trend = bls.source.split(" | ")[-1]
                else:
                    bls_str = "N/A"
                    flag = ""
                    trend = ""

                comparison.append({
                    "Distributor": dist.name,
                    "Ingredient": ing.name,
                    "Quoted Price": f"${link.quoted_price:.2f}",
                    "Unit": link.quoted_unit or "N/A",
                    "BLS Avg (Retail)": bls_str,
                    "vs Market": flag,
                    "Trend": trend,
                    "Delivery Terms": link.delivery_terms or "N/A",
                })
            st.dataframe(comparison, use_container_width=True)

            # Best price per ingredient
            st.subheader("Best Prices — Weekly Order Estimate")

            # Aggregate weekly quantities: per-serving qty × weekly_covers, summed across recipes
            weekly_qty_map = {}
            if ps["restaurant_id"]:
                recipe_ings = session.query(RecipeIngredient).join(Recipe).filter(
                    Recipe.restaurant_id == ps["restaurant_id"],
                    RecipeIngredient.ingredient_id.in_(list(ing_ids)),
                ).all()
                for ri in recipe_ings:
                    if ri.ingredient_id not in weekly_qty_map:
                        weekly_qty_map[ri.ingredient_id] = (0.0, ri.unit)
                    total, u = weekly_qty_map[ri.ingredient_id]
                    weekly_qty_map[ri.ingredient_id] = (total + ri.quantity * weekly_covers, u)

            seen = {}
            for link in quoted:
                ing = ingredients_map[link.ingredient_id]
                if ing.name not in seen or (link.quoted_price and link.quoted_price < seen[ing.name][1]):
                    dist = distributors_map[link.distributor_id]
                    seen[ing.name] = (dist.name, link.quoted_price, link.quoted_unit, link.ingredient_id)
            best_rows = []
            total_weekly_cost = 0.0
            for ing_name, (dist_name, price, unit, ing_id) in seen.items():
                bls = bls_map.get(ing_id)
                weekly_qty, qty_unit = weekly_qty_map.get(ing_id, (None, unit or "unit"))
                weekly_cost = price * weekly_qty if weekly_qty else None
                if weekly_cost:
                    total_weekly_cost += weekly_cost

                row = {
                    "Ingredient": ing_name,
                    "Best Distributor": dist_name,
                    "Unit Price": f"${price:.2f}/{unit or 'unit'}",
                    "Weekly Qty": f"{weekly_qty:.0f} {qty_unit}" if weekly_qty else "N/A",
                    "Weekly Cost": f"${weekly_cost:.2f}" if weekly_cost else "N/A",
                    "BLS Avg (Retail)": "N/A",
                    "vs Market": "",
                }
                if bls and bls.price:
                    row["BLS Avg (Retail)"] = f"${bls.price:.2f}/{bls.unit or 'unit'}"
                    diff_pct = ((price - bls.price) / bls.price) * 100
                    if diff_pct > 20:
                        row["vs Market"] = f"+{diff_pct:.0f}%"
                    elif diff_pct < -10:
                        row["vs Market"] = f"{diff_pct:.0f}%"
                    else:
                        row["vs Market"] = "~ retail"
                best_rows.append(row)
            st.dataframe(best_rows, use_container_width=True)
            if total_weekly_cost > 0:
                st.metric("Estimated Weekly Total", f"${total_weekly_cost:.2f}")

            # Items not supplied
            not_supplied = session.query(DistributorIngredient).filter_by(
                supply_status="does_not_supply"
            ).all()
            if not_supplied:
                ns_dist_ids = {l.distributor_id for l in not_supplied}
                ns_ing_ids = {l.ingredient_id for l in not_supplied}
                ns_dists = {d.id: d for d in session.query(Distributor).filter(
                    Distributor.id.in_(ns_dist_ids)
                ).all()}
                ns_ings = {i.id: i for i in session.query(Ingredient).filter(
                    Ingredient.id.in_(ns_ing_ids)
                ).all()}
                st.subheader("Items Not Supplied")
                omitted_data = []
                for link in not_supplied:
                    omitted_data.append({
                        "Distributor": ns_dists[link.distributor_id].name,
                        "Ingredient": ns_ings[link.ingredient_id].name,
                    })
                st.table(omitted_data)

            # Distributors needing clarification
            needs_clar = session.query(Distributor).filter_by(
                rfp_status="needs_clarification"
            ).all()
            if needs_clar:
                clar_dist_ids = [d.id for d in needs_clar]
                clar_links = session.query(DistributorIngredient).filter(
                    DistributorIngredient.distributor_id.in_(clar_dist_ids),
                    DistributorIngredient.supply_status == "unconfirmed",
                ).all()
                clar_ing_ids = {l.ingredient_id for l in clar_links}
                clar_ings = {i.id: i for i in session.query(Ingredient).filter(
                    Ingredient.id.in_(clar_ing_ids)
                ).all()} if clar_ing_ids else {}

                st.subheader("Awaiting Clarification")
                for d in needs_clar:
                    links = [l for l in clar_links if l.distributor_id == d.id]
                    names = [clar_ings[l.ingredient_id].name for l in links if l.ingredient_id in clar_ings]
                    st.write(f"**{d.name}:** missing {', '.join(names)}")
        else:
            st.info("No quotes received yet. Distributors may not have replied.")
    finally:
        session.close()

# ── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption("Pathway Take-Home Exercise — RFP Pipeline")
