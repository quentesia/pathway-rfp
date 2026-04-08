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
    status = st.status("Starting menu parsing...", expanded=True)
    session = SessionLocal()
    try:
        restaurant, recipes = parse_menu(
            session,
            restaurant_name=restaurant_name,
            menu_image_path=menu_image_path,
            location=restaurant_location,
            menu_url=menu_url,
            on_status=lambda msg: status.write(msg),
        )
        ps["restaurant_id"] = restaurant.id
        ps["step1_done"] = True
        status.update(label=f"Parsed {len(recipes)} recipes!", state="complete")
    except Exception as e:
        status.update(label="Error parsing menu", state="error")
        st.error(str(e))
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
    status = st.status("Fetching market price trends...", expanded=True)
    session = SessionLocal()
    try:
        total_ingredients = session.query(Ingredient).filter_by(restaurant_id=ps["restaurant_id"]).count()
        records = fetch_market_trends(
            session, restaurant_id=ps["restaurant_id"],
            on_status=lambda msg: status.write(msg),
        )
        ps["step2_done"] = True
        status.update(label=f"Fetched price data for {len(records)}/{total_ingredients} ingredients!", state="complete")
    except Exception as e:
        status.update(label="Error fetching prices", state="error")
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
    status = st.status("Searching for distributors...", expanded=True)
    session = SessionLocal()
    try:
        distributors = find_local_distributors(
            session, restaurant_location, restaurant_id=ps["restaurant_id"],
            on_status=lambda msg: status.write(msg),
        )
        ps["step3_done"] = True
        with_email = sum(1 for d in distributors if d.email and not d.email.startswith("form:"))
        with_form = sum(1 for d in distributors if d.email and d.email.startswith("form:"))
        parts = [f"Found {len(distributors)} distributors"]
        parts.append(f"{with_email} with email")
        if with_form:
            parts.append(f"{with_form} with contact form")
        status.update(label=" — ".join(parts), state="complete")
    except Exception as e:
        status.update(label="Error finding distributors", state="error")
        st.error(str(e))
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
st.caption("🧪 **DRY RUN MODE** — emails are sent to temporary Yopmail inboxes, not real distributors.")

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
    status = st.status("Preparing and sending RFP emails...", expanded=True)
    session = SessionLocal()
    try:
        processed = send_rfp_emails(
            session, ps["restaurant_id"], mock_recipient="demo",
            weekly_covers=weekly_covers,
            on_status=lambda msg: status.write(msg),
        )
        ps["step4_done"] = True
        sent = sum(1 for d in processed if d.rfp_status == "sent")
        forms = sum(1 for d in processed if d.rfp_status == "form_ready")
        skipped = sum(1 for d in processed if d.rfp_status == "skipped")
        parts = [f"{sent} emailed"]
        if forms:
            parts.append(f"{forms} forms ready")
        if skipped:
            parts.append(f"{skipped} skipped")
        status.update(label=f"RFP outreach: {' — '.join(parts)}", state="complete")
    except Exception as e:
        status.update(label="Error sending emails", state="error")
        st.error(str(e))
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

        # Load restaurant for email preview
        restaurant_obj = session.get(Restaurant, ps["restaurant_id"])

        # Load weekly quantities for email preview
        all_recipe_ings = session.query(RecipeIngredient).join(Recipe).filter(
            Recipe.restaurant_id == ps["restaurant_id"],
            RecipeIngredient.ingredient_id.in_(link_ing_ids),
        ).all() if link_ing_ids else []
        qty_map = {}
        for ri in all_recipe_ings:
            if ri.ingredient_id not in qty_map:
                qty_map[ri.ingredient_id] = (0.0, ri.unit)
            total, u = qty_map[ri.ingredient_id]
            qty_map[ri.ingredient_id] = (total + ri.quantity * weekly_covers, u)

        for dist in distributors:
            status_icon = {"sent": "📨", "completed": "✅", "needs_clarification": "⚠️",
                           "form_ready": "📋", "skipped": "⏭️", "failed": "❌"}.get(dist.rfp_status, "")
            with st.expander(f"{status_icon} [{dist.rfp_status}] {dist.name}"):
                links = [l for l in all_links if l.distributor_id == dist.id]
                ing_names = [ingredients_map[l.ingredient_id].name for l in links if l.ingredient_id in ingredients_map]
                st.write(f"**To:** {dist.email or 'N/A'}")
                st.write(f"**Ingredients requested:** {', '.join(ing_names)}")
                if dist.rfp_sent_at:
                    st.write(f"**Sent:** {dist.rfp_sent_at.strftime('%Y-%m-%d %H:%M')}")

                # Show email preview
                if restaurant_obj and ing_names:
                    ing_lines = []
                    for l in links:
                        if l.ingredient_id in ingredients_map:
                            ing = ingredients_map[l.ingredient_id]
                            qty_info = qty_map.get(l.ingredient_id)
                            if qty_info:
                                qty, unit = qty_info
                                ing_lines.append(f"  - {ing.name} — est. {qty:.0f} {unit}/week")
                            else:
                                ing_lines.append(f"  - {ing.name}")
                    st.markdown("**Email preview:**")
                    st.code(
                        f"Subject: Request for Proposal — Ingredient Pricing for {restaurant_obj.name}\n\n"
                        f"Dear {dist.name} Team,\n\n"
                        f"We are reaching out on behalf of {restaurant_obj.name} ({restaurant_obj.location}) to request\n"
                        f"competitive pricing on the following ingredients:\n\n"
                        + "\n".join(ing_lines),
                        language=None,
                    )

        # Yopmail inbox links (dry run only)
        from app.services.email_sender import _make_yopmail
        sent_dists = [d for d in distributors if d.rfp_status in ("sent", "completed", "needs_clarification")]
        if sent_dists:
            st.divider()
            st.markdown("🧪 **DRY RUN — Yopmail Inboxes**")
            st.caption("These are temporary inboxes where the demo emails were sent. Open them to see/reply to the RFP.")
            for dist in sent_dists:
                yopmail = _make_yopmail(dist.name)
                inbox_url = f"https://yopmail.com/en/?login={yopmail.split('@')[0]}"
                st.markdown(f"- **{dist.name}**: [`{yopmail}`]({inbox_url})")
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
    status = st.status("Connecting to inbox...", expanded=True)
    session = SessionLocal()
    try:
        def _on_status(msg):
            status.write(msg)

        updated = collect_quotes(
            session, ps["restaurant_id"], mock_recipient="demo",
            on_status=_on_status,
        )
        ps["step5_done"] = True
        if updated:
            status.update(label=f"Done — {len(updated)} ingredient prices updated", state="complete")
        else:
            status.update(label="Done — no new quotes found", state="complete")
    except Exception as e:
        status.update(label="Error checking inbox", state="error")
        st.error(str(e))
    finally:
        session.close()

if ps["step5_done"]:
    session = SessionLocal()
    try:
        # Load all distributor statuses
        all_distributors = session.query(Distributor).filter(
            Distributor.rfp_status != "pending"
        ).all()
        completed_dists = [d for d in all_distributors if d.rfp_status == "completed"]
        clarification_dists = [d for d in all_distributors if d.rfp_status == "needs_clarification"]
        sent_dists = [d for d in all_distributors if d.rfp_status == "sent"]

        # Summary metrics row
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Distributors Contacted", len(all_distributors))
        m2.metric("Fully Quoted", len(completed_dists))
        m3.metric("Awaiting Clarification", len(clarification_dists))
        m4.metric("No Reply Yet", len(sent_dists))

        # ── Awaiting Clarification (show prominently at top) ──
        if clarification_dists:
            st.warning(f"**{len(clarification_dists)} distributor(s) need follow-up** — a follow-up email was sent automatically.")
            clar_dist_ids = [d.id for d in clarification_dists]
            clar_links = session.query(DistributorIngredient).filter(
                DistributorIngredient.distributor_id.in_(clar_dist_ids),
                DistributorIngredient.supply_status == "unconfirmed",
            ).all()
            clar_ing_ids = {l.ingredient_id for l in clar_links}
            clar_ings = {i.id: i for i in session.query(Ingredient).filter(
                Ingredient.id.in_(clar_ing_ids)
            ).all()} if clar_ing_ids else {}

            for d in clarification_dists:
                links = [l for l in clar_links if l.distributor_id == d.id]
                names = [clar_ings[l.ingredient_id].name for l in links if l.ingredient_id in clar_ings]
                st.markdown(f"- **{d.name}** — missing: {', '.join(names) if names else 'unknown items'}")

        if sent_dists:
            st.info(f"**{len(sent_dists)} distributor(s) haven't replied yet.** Check back later or re-run this step.")

        # ── Quoted prices: all quotes from all distributors ──
        quoted = session.query(DistributorIngredient).filter(
            DistributorIngredient.quoted_price.isnot(None)
        ).all()

        if quoted:
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

            # ── Best Prices (primary table) ──
            st.subheader("Best Prices — Weekly Order Estimate")

            # Aggregate weekly quantities
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
                    "BLS Avg": "N/A",
                    "vs Market": "",
                }
                if bls and bls.price:
                    row["BLS Avg"] = f"${bls.price:.2f}/{bls.unit or 'unit'}"
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
                cost1, cost2 = st.columns(2)
                cost1.metric("Estimated Weekly Total", f"${total_weekly_cost:,.2f}")
                cost2.metric("Estimated Monthly Total", f"${total_weekly_cost * 4.33:,.2f}")

            # ── Full comparison (expandable) ──
            with st.expander("Full Quote Comparison (all distributors)", expanded=False):
                comparison = []
                for link in quoted:
                    dist = distributors_map[link.distributor_id]
                    ing = ingredients_map[link.ingredient_id]
                    bls = bls_map.get(link.ingredient_id)

                    if bls and bls.price and link.quoted_price:
                        bls_str = f"${bls.price:.2f}/{bls.unit or 'unit'}"
                        diff_pct = ((link.quoted_price - bls.price) / bls.price) * 100
                        if diff_pct > 20:
                            flag = f"+{diff_pct:.0f}% above retail"
                        elif diff_pct < -10:
                            flag = f"{diff_pct:.0f}% below retail"
                        else:
                            flag = "~ near retail"
                    else:
                        bls_str = "N/A"
                        flag = ""

                    comparison.append({
                        "Distributor": dist.name,
                        "Ingredient": ing.name,
                        "Quoted Price": f"${link.quoted_price:.2f}",
                        "Unit": link.quoted_unit or "N/A",
                        "BLS Avg": bls_str,
                        "vs Market": flag,
                        "Delivery": link.delivery_terms or "N/A",
                    })
                st.dataframe(comparison, use_container_width=True)

            # ── Items not supplied ──
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
                with st.expander(f"Items Not Supplied ({len(not_supplied)})", expanded=False):
                    omitted_data = []
                    for link in not_supplied:
                        omitted_data.append({
                            "Distributor": ns_dists[link.distributor_id].name,
                            "Ingredient": ns_ings[link.ingredient_id].name,
                        })
                    st.table(omitted_data)
        else:
            st.info("No quotes received yet. Distributors may not have replied.")
    finally:
        session.close()

# ── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption("Pathway Take-Home Exercise — RFP Pipeline")
