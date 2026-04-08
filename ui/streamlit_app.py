"""Streamlit UI: visualizes each step of the RFP pipeline."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st
from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.models import (
    Recipe, Ingredient, RecipeIngredient,
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

# ── Sidebar: Configuration ───────────────────────────────────────────────────

with st.sidebar:
    st.header("Configuration")
    restaurant_name = st.text_input("Restaurant Name", "My Restaurant")
    restaurant_location = st.text_input("Location", "Atlanta, GA")
    menu_url = st.text_input("Menu Source URL (optional)", "")

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

# ── Step 1: Menu -> Recipes ──────────────────────────────────────────────────

st.header("Step 1: Menu -> Recipes & Ingredients")
st.markdown("Upload a menu photo or paste menu text. The system will parse each dish into a structured recipe.")

input_tab1, input_tab2 = st.tabs(["Upload Photo", "Paste Text"])

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

menu_text = ""
with input_tab2:
    menu_text = st.text_area(
        "Paste restaurant menu here",
        height=200,
        placeholder="STARTERS\nCrispy Calamari - Lightly breaded, served with marinara - $12\n...",
    )

has_input = bool(menu_text) or menu_image_path is not None

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
        for recipe in recipes:
            with st.expander(f"{recipe.dish_name} ({recipe.category})"):
                ings = session.query(RecipeIngredient).filter_by(recipe_id=recipe.id).all()
                rows = []
                for ri in ings:
                    ing = session.query(Ingredient).get(ri.ingredient_id)
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

if st.button("Fetch Market Trends", disabled=not ps["step1_done"]):
    with st.spinner("Fetching market price trends from BLS API..."):
        session = SessionLocal()
        try:
            total_ingredients = session.query(Ingredient).count()
            records = fetch_market_trends(session)
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
        rows = []
        for r in records:
            ing = session.query(Ingredient).get(r.ingredient_id)
            # Parse trend info from source string
            source_parts = r.source.split(" | ")
            trend_info = source_parts[-1] if len(source_parts) > 1 else ""
            rows.append({
                "Ingredient": ing.name,
                "BLS Match": r.usda_item_name,
                "Price": f"${r.price:.2f}" if r.price else "N/A",
                "Unit": r.unit or "N/A",
                "Period": r.date or "N/A",
                "Trend": trend_info,
            })
        if rows:
            st.dataframe(rows, use_container_width=True)
    finally:
        session.close()

# ── Step 3: Find Distributors ────────────────────────────────────────────────

st.header("Step 3: Find Local Distributors")
st.markdown("Searches for food distributors in the restaurant's area.")

if st.button("Find Distributors", disabled=not ps["step1_done"]):
    with st.spinner("Searching for distributors..."):
        session = SessionLocal()
        try:
            distributors = find_local_distributors(session, restaurant_location)
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

        # Split into contactable vs phone-only
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

                links = session.query(DistributorIngredient).filter_by(
                    distributor_id=dist.id
                ).all()
                ing_names = []
                for link in links:
                    ing = session.query(Ingredient).get(link.ingredient_id)
                    ing_names.append(ing.name)
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

if st.button("Send Emails", disabled=not ps["step3_done"]):
    with st.spinner("Sending RFP emails..."):
        session = SessionLocal()
        try:
            processed = send_rfp_emails(
                session, ps["restaurant_id"], mock_recipient="demo"
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
        for dist in distributors:
            with st.expander(f"[{dist.rfp_status}] {dist.name}"):
                ing_links = session.query(DistributorIngredient).filter_by(
                    distributor_id=dist.id
                ).all()
                ing_names = [session.query(Ingredient).get(l.ingredient_id).name for l in ing_links]
                st.write(f"**Ingredients requested:** {', '.join(ing_names)}")
                if dist.rfp_sent_at:
                    st.write(f"**Sent:** {dist.rfp_sent_at.strftime('%Y-%m-%d %H:%M')}")
    finally:
        session.close()

# ── Step 5: Collect & Compare Quotes (Nice-to-have) ─────────────────────────

st.header("Step 5: Collect & Compare Quotes (Nice-to-have)")
st.markdown("Monitors inbox for distributor replies, parses quotes, and compiles a comparison.")

if st.button("Check Inbox", disabled=not ps["step4_done"]):
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
            comparison = []
            for link in quoted:
                dist = session.query(Distributor).get(link.distributor_id)
                ing = session.query(Ingredient).get(link.ingredient_id)

                # BLS market comparison
                bls = session.query(USDAPrice).filter_by(ingredient_id=link.ingredient_id).first()
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
            st.subheader("Best Prices")
            seen = {}
            for link in quoted:
                ing = session.query(Ingredient).get(link.ingredient_id)
                if ing.name not in seen or (link.quoted_price and link.quoted_price < seen[ing.name][1]):
                    dist = session.query(Distributor).get(link.distributor_id)
                    seen[ing.name] = (dist.name, link.quoted_price, link.quoted_unit)
            for ing_name, (dist_name, price, unit) in seen.items():
                st.write(f"**{ing_name}:** {dist_name} — ${price:.2f}/{unit or 'unit'}")

            # Items not supplied
            not_supplied = session.query(DistributorIngredient).filter_by(
                supply_status="does_not_supply"
            ).all()
            if not_supplied:
                st.subheader("Items Not Supplied")
                omitted_data = []
                for link in not_supplied:
                    dist = session.query(Distributor).get(link.distributor_id)
                    ing = session.query(Ingredient).get(link.ingredient_id)
                    omitted_data.append({"Distributor": dist.name, "Ingredient": ing.name})
                st.table(omitted_data)

            # Distributors needing clarification
            needs_clar = session.query(Distributor).filter_by(
                rfp_status="needs_clarification"
            ).all()
            if needs_clar:
                st.subheader("Awaiting Clarification")
                for d in needs_clar:
                    unquoted = session.query(DistributorIngredient).filter_by(
                        distributor_id=d.id, supply_status="unconfirmed"
                    ).all()
                    names = [session.query(Ingredient).get(l.ingredient_id).name for l in unquoted]
                    st.write(f"**{d.name}:** missing {', '.join(names)}")
        else:
            st.info("No quotes received yet. Distributors may not have replied.")
    finally:
        session.close()

# ── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption("Pathway Take-Home Exercise — RFP Pipeline")
