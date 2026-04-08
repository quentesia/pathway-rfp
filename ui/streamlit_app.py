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
    RFPEmail, RFPQuote,
)
from app.services.menu_parser import run_step1
from app.services.usda_client import run_step2
from app.services.distributor_finder import run_step3
from app.services.email_sender import run_step4
from app.services.inbox_monitor import run_step5

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
    mock_email = st.text_input("Mock Email (for demo)", placeholder="your@email.com")

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
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(uploaded_file.read())
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

if st.button("Run Step 1: Parse Menu", disabled=not has_input):
    with st.spinner("Parsing menu with LLM..." + (" (running OCR first)" if menu_image_path else "")):
        session = SessionLocal()
        try:
            restaurant, recipes = run_step1(
                session, menu_text, restaurant_name, restaurant_location, menu_url,
                menu_image_path=menu_image_path,
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

if st.button("Run Step 2: Fetch Price Trends", disabled=not ps["step1_done"]):
    with st.spinner("Querying BLS Average Price Data API..."):
        session = SessionLocal()
        try:
            records = run_step2(session)
            ps["step2_done"] = True
            st.success(f"Fetched price data for {len(records)} ingredients!")
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

if st.button("Run Step 3: Find Distributors", disabled=not ps["step1_done"]):
    with st.spinner("Searching for distributors..."):
        session = SessionLocal()
        try:
            distributors = run_step3(session, restaurant_location)
            ps["step3_done"] = True
            st.success(f"Found {len(distributors)} distributors!")
        finally:
            session.close()

if ps["step3_done"]:
    session = SessionLocal()
    try:
        distributors = session.query(Distributor).all()
        for dist in distributors:
            with st.expander(f"{dist.name} — {dist.location}"):
                st.write(f"**Phone:** {dist.phone or 'N/A'}")
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
                    st.write(f"**Supplies:** {', '.join(ing_names)}")
    finally:
        session.close()

# ── Step 4: Send RFP Emails ─────────────────────────────────────────────────

st.header("Step 4: Send RFP Emails")
st.markdown("Composes and sends RFP emails to each distributor requesting price quotes.")

if st.button("Run Step 4: Send Emails", disabled=not ps["step3_done"]):
    if not mock_email:
        st.warning("Set a mock email address in the sidebar for demo mode.")
    else:
        with st.spinner("Sending RFP emails..."):
            session = SessionLocal()
            try:
                emails = run_step4(
                    session, ps["restaurant_id"], mock_recipient=mock_email
                )
                ps["step4_done"] = True
                sent = sum(1 for e in emails if e.status == "sent")
                st.success(f"Sent {sent}/{len(emails)} RFP emails!")
            finally:
                session.close()

if ps["step4_done"]:
    session = SessionLocal()
    try:
        emails = session.query(RFPEmail).filter_by(
            restaurant_id=ps["restaurant_id"]
        ).all()
        for email in emails:
            dist = session.query(Distributor).get(email.distributor_id)
            status_icon = {"sent": "sent", "failed": "failed", "draft": "draft"}.get(
                email.status, email.status
            )
            with st.expander(f"[{status_icon}] To: {dist.name}"):
                st.write(f"**Subject:** {email.subject}")
                st.code(email.body, language=None)
    finally:
        session.close()

# ── Step 5: Collect & Compare Quotes (Nice-to-have) ─────────────────────────

st.header("Step 5: Collect & Compare Quotes (Nice-to-have)")
st.markdown("Monitors inbox for distributor replies, parses quotes, and compiles a comparison.")

if st.button("Run Step 5: Check Inbox", disabled=not ps["step4_done"]):
    with st.spinner("Monitoring inbox for replies..."):
        session = SessionLocal()
        try:
            quotes = run_step5(session, ps["restaurant_id"])
            ps["step5_done"] = True
            st.success(f"Parsed {len(quotes)} quotes from replies!")
        finally:
            session.close()

if ps["step5_done"]:
    session = SessionLocal()
    try:
        quotes = session.query(RFPQuote).all()
        if quotes:
            comparison = []
            for q in quotes:
                dist = session.query(Distributor).get(q.distributor_id)
                ing = session.query(Ingredient).get(q.ingredient_id)
                comparison.append({
                    "Distributor": dist.name,
                    "Ingredient": ing.name,
                    "Quoted Price": f"${q.quoted_price:.2f}" if q.quoted_price else "N/A",
                    "Unit": q.unit or "N/A",
                    "Delivery Terms": q.delivery_terms or "N/A",
                })
            st.table(comparison)

            # Recommendation
            st.subheader("Recommendation")
            best = min(quotes, key=lambda q: q.quoted_price or float("inf"))
            best_dist = session.query(Distributor).get(best.distributor_id)
            st.success(f"Best overall price: **{best_dist.name}** at ${best.quoted_price:.2f}/{best.unit}")
        else:
            st.info("No quotes received yet. Distributors may not have replied.")
    finally:
        session.close()

# ── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption("Pathway Take-Home Exercise — RFP Pipeline")
