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
from app.utils import (
    DEMAND_TIER_BASE_COVERS,
    aggregate_quantities,
    category_tag,
    estimate_category_weekly_covers,
    load_category_cover_overrides_from_env,
    load_demand_tier_from_env,
    normalize_category_name,
    normalize_category_list,
)

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


def _get_tiered_category_covers(restaurant_id: int, baseline_weekly_covers: int) -> dict[str, int]:
    """Estimate category covers from demand tier + category factors + env overrides."""
    session = SessionLocal()
    try:
        recipes = session.query(Recipe).filter(
            Recipe.restaurant_id == restaurant_id
        ).all()
        env_overrides = load_category_cover_overrides_from_env()
        return estimate_category_weekly_covers(
            recipes,
            baseline_weekly_covers,
            env_overrides=env_overrides,
        )
    finally:
        session.close()

# ── Sidebar: Configuration ───────────────────────────────────────────────────

category_weekly_covers: dict[str, int] = {}
sidebar_restaurant_id = st.session_state.get("pipeline_state", {}).get("restaurant_id")

with st.sidebar:
    st.header("Configuration")
    restaurant_name = st.text_input("Restaurant Name", "My Restaurant")
    restaurant_location = st.text_input("Location", "Atlanta, GA")
    menu_url = st.text_input("Menu Source URL (optional)", "")
    run_mode = st.selectbox(
        "Outreach Mode",
        ["Dry Run", "Live"],
        index=0,
        help="Dry Run sends to Yopmail demo inboxes. Live sends to discovered distributor contacts.",
    )
    is_dry_run = run_mode == "Dry Run"
    tier_options = list(DEMAND_TIER_BASE_COVERS.keys())
    default_tier = load_demand_tier_from_env()
    demand_tier = st.selectbox(
        "Demand Tier",
        tier_options,
        index=tier_options.index(default_tier) if default_tier in tier_options else tier_options.index("Standard"),
        help="Baseline demand profile only. Dish-level differentiation still comes from Claude popularity multipliers and category factors.",
    )
    baseline_weekly_covers = DEMAND_TIER_BASE_COVERS[demand_tier]
    st.caption(
        f"Tier baseline: {baseline_weekly_covers} weekly covers per average dish. "
        "Claude dish popularity and category factors still differentiate individual items."
    )
    if sidebar_restaurant_id:
        category_defaults = _get_tiered_category_covers(sidebar_restaurant_id, baseline_weekly_covers)
        if category_defaults:
            with st.expander("Weekly Covers by Category", expanded=False):
                st.caption(
                    f"Auto-filled from demand tier ({demand_tier}) + category factors; "
                    "edit before sending if needed. "
                    "Optional env override: RFP_WEEKLY_COVERS_BY_CATEGORY (JSON)."
                )
                for cat, default_val in category_defaults.items():
                    category_weekly_covers[cat] = int(st.number_input(
                        cat,
                        min_value=0,
                        max_value=2000,
                        value=default_val,
                        step=1,
                        key=f"weekly_covers_cat_{sidebar_restaurant_id}_{cat}",
                    ))

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
        all_tags = sorted({tag for r in records for tag in r.trend_tags_list})
        selected_tags = st.multiselect(
            "Filter Trend Tags",
            all_tags,
            key="step2_trend_tags_filter",
            help="Filter Step 2 rows by structured trend tags.",
        )
        tag_search = st.text_input(
            "Search Trend Tags",
            value="",
            key="step2_trend_tags_search",
            help="Case-insensitive contains search over trend tags.",
        ).strip().lower()

        ing_ids = {r.ingredient_id for r in records}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(ing_ids)
        ).all()}
        rows = []
        for r in records:
            tags = r.trend_tags_list
            if selected_tags and not all(t in tags for t in selected_tags):
                continue
            if tag_search and not any(tag_search in t.lower() for t in tags):
                continue
            ing = ingredients_map[r.ingredient_id]
            rows.append({
                "Ingredient": ing.name,
                "BLS Match": r.usda_item_name,
                "BLS Series": r.bls_series_id or "N/A",
                "Price": f"${r.price:.2f}" if r.price else "N/A",
                "Unit": r.unit or "N/A",
                "Period": r.date.strftime("%B %Y") if r.date else "N/A",
                "Trend": r.trend_summary or "N/A",
                "Trend Direction": r.trend_direction or "N/A",
                "Trend %": f"{r.trend_pct_change:+.1f}%" if r.trend_pct_change is not None else "N/A",
                "Trend Tags": ", ".join(tags) if tags else "N/A",
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

        dist_category_map: dict[int, list[str]] = {}
        for dist in distributors:
            linked_categories = {
                normalize_category_name(ingredients_map[l.ingredient_id].category)
                for l in all_links
                if l.distributor_id == dist.id and l.ingredient_id in ingredients_map
            }
            served_categories = set(normalize_category_list(dist.categories_served_list))
            combined = sorted(served_categories | linked_categories)
            dist_category_map[dist.id] = combined or ["Other"]

        all_step3_categories = sorted({c for cats in dist_category_map.values() for c in cats})
        selected_step3_categories = st.multiselect(
            "Filter Distributors by Category",
            all_step3_categories,
            key="step3_category_filter",
        )

        def _matches_selected_categories(dist: Distributor) -> bool:
            if not selected_step3_categories:
                return True
            cats = dist_category_map.get(dist.id, [])
            return any(cat in selected_step3_categories for cat in cats)

        contactable = [d for d in distributors if d.email and _matches_selected_categories(d)]
        phone_only = [d for d in distributors if not d.email and _matches_selected_categories(d)]

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
                categories = dist_category_map.get(dist.id, [])
                tags = [category_tag(c) for c in categories]
                st.write(f"**Categories:** {', '.join(categories)}")
                st.write(f"**Category Tags:** {', '.join(tags)}")

                links = [l for l in all_links if l.distributor_id == dist.id]
                ing_names = [ingredients_map[l.ingredient_id].name for l in links if l.ingredient_id in ingredients_map]
                if ing_names:
                    st.write(f"**Possible Supplies:** {', '.join(ing_names)}")

        if selected_step3_categories:
            st.caption(f"Showing distributors matching: {', '.join(selected_step3_categories)}")
        if not contactable and not phone_only:
            st.info("No distributors match the selected category filter.")

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
if is_dry_run:
    st.caption("🧪 **DRY RUN MODE** — emails are sent to temporary Yopmail inboxes, not real distributors.")
else:
    st.caption("🚀 **LIVE MODE** — emails are sent to distributor email/contact-form targets from Step 3.")

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
            session, ps["restaurant_id"], mock_recipient="demo" if is_dry_run else None,
            weekly_covers=baseline_weekly_covers,
            weekly_covers_by_category=category_weekly_covers or None,
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
        ri_recipe_ids = {ri.recipe_id for ri in all_recipe_ings}
        recipes_map = {r.id: r for r in session.query(Recipe).filter(Recipe.id.in_(ri_recipe_ids)).all()} if ri_recipe_ids else {}
        qty_map = aggregate_quantities(
            all_recipe_ings,
            baseline_weekly_covers,
            ingredients_map,
            recipes_map,
            weekly_covers_by_category=category_weekly_covers or None,
        )

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
        if is_dry_run:
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
            session, ps["restaurant_id"], mock_recipient="demo" if is_dry_run else None,
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

        # Shared Step 5 data for both views
        all_dist_ids = [d.id for d in all_distributors]
        all_links = session.query(DistributorIngredient).filter(
            DistributorIngredient.distributor_id.in_(all_dist_ids)
        ).all() if all_dist_ids else []
        all_ing_ids = {l.ingredient_id for l in all_links}
        ingredients_map = {i.id: i for i in session.query(Ingredient).filter(
            Ingredient.id.in_(all_ing_ids)
        ).all()} if all_ing_ids else {}
        ingredient_category_map = {
            ing_id: normalize_category_name(ing.category)
            for ing_id, ing in ingredients_map.items()
        }
        all_step5_categories = sorted(set(ingredient_category_map.values()))
        selected_step5_categories = st.multiselect(
            "Filter Step 5 by Category",
            all_step5_categories,
            key="step5_category_filter",
        )

        filtered_links = [
            l for l in all_links
            if not selected_step5_categories
            or ingredient_category_map.get(l.ingredient_id, "Other") in selected_step5_categories
        ]
        if filtered_links:
            ingredient_ids_in_scope = sorted({l.ingredient_id for l in filtered_links})
            best_by_ingredient: dict[int, tuple[float, int, str | None]] = {}
            for l in filtered_links:
                if l.quoted_price is None:
                    continue
                current = best_by_ingredient.get(l.ingredient_id)
                if current is None or l.quoted_price < current[0]:
                    best_by_ingredient[l.ingredient_id] = (l.quoted_price, l.distributor_id, l.quoted_unit)

            covered_ids = sorted(best_by_ingredient.keys())
            uncovered_ids = [iid for iid in ingredient_ids_in_scope if iid not in best_by_ingredient]

            st.subheader("Coverage Snapshot")
            c1, c2, c3 = st.columns(3)
            c1.metric("Ingredients In Scope", len(ingredient_ids_in_scope))
            c2.metric("With Provider Quote", len(covered_ids))
            c3.metric("Without Provider Quote", len(uncovered_ids))

            if covered_ids:
                bls_map_snapshot = {b.ingredient_id: b for b in session.query(USDAPrice).filter(
                    USDAPrice.ingredient_id.in_(covered_ids)
                ).all()}
                weekly_qty_map_snapshot = {}
                if ps["restaurant_id"]:
                    recipe_ings_snapshot = session.query(RecipeIngredient).join(Recipe).filter(
                        Recipe.restaurant_id == ps["restaurant_id"],
                        RecipeIngredient.ingredient_id.in_(covered_ids),
                    ).all()
                    recipe_ids_snapshot = {ri.recipe_id for ri in recipe_ings_snapshot}
                    recipes_map_snapshot = {r.id: r for r in session.query(Recipe).filter(
                        Recipe.id.in_(recipe_ids_snapshot)
                    ).all()} if recipe_ids_snapshot else {}
                    ing_qty_map_snapshot = {
                        iid: ingredients_map[iid]
                        for iid in covered_ids
                        if iid in ingredients_map
                    }
                    weekly_qty_map_snapshot = aggregate_quantities(
                        recipe_ings_snapshot,
                        baseline_weekly_covers,
                        ing_qty_map_snapshot,
                        recipes_map_snapshot,
                        weekly_covers_by_category=category_weekly_covers or None,
                    )

                best_price_rows = []
                for iid in covered_ids:
                    price, dist_id, unit = best_by_ingredient[iid]
                    ing = ingredients_map.get(iid)
                    dist = next((d for d in all_distributors if d.id == dist_id), None)
                    if not ing or not dist:
                        continue
                    cat = ingredient_category_map.get(iid, "Other")
                    bls = bls_map_snapshot.get(iid)
                    weekly_qty, _ = weekly_qty_map_snapshot.get(iid, (None, unit or "unit"))
                    weekly_delta = "N/A"
                    if bls and bls.price and weekly_qty:
                        bls_weekly_est = (bls.price * 0.5) * weekly_qty
                        provider_weekly_est = price * weekly_qty
                        diff = provider_weekly_est - bls_weekly_est
                        sign = "+" if diff > 0 else ""
                        weekly_delta = f"{sign}${diff:,.0f}"
                    best_price_rows.append({
                        "Ingredient": ing.name,
                        "Category": cat,
                        "Category Tag": category_tag(cat),
                        "Best Provider": dist.name,
                        "Best Price": f"${price:.2f}/{unit or 'unit'}",
                        "Weekly Δ vs BLS Est": weekly_delta,
                    })
                best_price_rows.sort(key=lambda r: (r["Category"], r["Ingredient"]))
                st.dataframe(best_price_rows, use_container_width=True)

            if uncovered_ids:
                missing_rows = []
                for iid in uncovered_ids:
                    ing = ingredients_map.get(iid)
                    if not ing:
                        continue
                    cat = ingredient_category_map.get(iid, "Other")
                    missing_rows.append({
                        "Ingredient": ing.name,
                        "Category": cat,
                        "Category Tag": category_tag(cat),
                    })
                missing_rows.sort(key=lambda r: (r["Category"], r["Ingredient"]))
                with st.expander(f"Ingredients Without A Provider Quote Yet ({len(missing_rows)})", expanded=False):
                    st.table(missing_rows)

        view_mode = st.radio(
            "Step 5 View",
            ["By Ingredient (Price Comparison)", "By Provider (Coverage Status)"],
            horizontal=True,
            key="step5_view_mode",
        )

        if view_mode == "By Provider (Coverage Status)":
            if not filtered_links:
                st.info("No provider items match the selected category filter.")
            else:
                status_order = {
                    "needs_clarification": 0,
                    "sent": 1,
                    "completed": 2,
                    "form_ready": 3,
                    "skipped": 4,
                    "failed": 5,
                }
                provider_rows = []
                for dist in all_distributors:
                    links = [l for l in filtered_links if l.distributor_id == dist.id]
                    if not links:
                        continue
                    confirmed = [l for l in links if l.supply_status == "confirmed"]
                    unconfirmed = [l for l in links if l.supply_status == "unconfirmed"]
                    not_supplied = [l for l in links if l.supply_status == "does_not_supply"]
                    provider_rows.append({
                        "Provider": dist.name,
                        "RFP Status": dist.rfp_status,
                        "Confirmed": len(confirmed),
                        "Unconfirmed": len(unconfirmed),
                        "Not Supplied": len(not_supplied),
                        "Coverage %": f"{(len(confirmed) / len(links) * 100):.0f}%" if links else "0%",
                    })
                provider_rows.sort(key=lambda r: (status_order.get(r["RFP Status"], 99), r["Provider"]))
                st.dataframe(provider_rows, use_container_width=True)

                for dist in all_distributors:
                    links = [l for l in filtered_links if l.distributor_id == dist.id]
                    if not links:
                        continue
                    confirmed = [l for l in links if l.supply_status == "confirmed"]
                    unconfirmed = [l for l in links if l.supply_status == "unconfirmed"]
                    not_supplied = [l for l in links if l.supply_status == "does_not_supply"]

                    with st.expander(f"[{dist.rfp_status}] {dist.name}"):
                        st.write(
                            f"**Counts:** confirmed={len(confirmed)}, "
                            f"unconfirmed={len(unconfirmed)}, "
                            f"does_not_supply={len(not_supplied)}"
                        )

                        def _item_rows(items):
                            rows = []
                            for l in items:
                                ing = ingredients_map.get(l.ingredient_id)
                                if not ing:
                                    continue
                                cat = ingredient_category_map.get(l.ingredient_id, "Other")
                                rows.append({
                                    "Ingredient": ing.name,
                                    "Category": cat,
                                    "Category Tag": category_tag(cat),
                                    "Quoted Price": f"${l.quoted_price:.2f}" if l.quoted_price is not None else "N/A",
                                    "Unit": l.quoted_unit or "N/A",
                                    "Delivery": l.delivery_terms or "N/A",
                                })
                            return rows

                        st.markdown("**Confirmed**")
                        st.table(_item_rows(confirmed) or [{"Ingredient": "None"}])
                        st.markdown("**Unconfirmed**")
                        st.table(_item_rows(unconfirmed) or [{"Ingredient": "None"}])
                        st.markdown("**Does Not Supply**")
                        st.table(_item_rows(not_supplied) or [{"Ingredient": "None"}])
        else:
            # ── Quoted prices: ingredient-centric comparison ──
            ingredient_ids_in_scope = sorted({l.ingredient_id for l in filtered_links})
            if not ingredient_ids_in_scope:
                st.info("No items in scope for the current category filter.")
            else:
                quoted = [l for l in filtered_links if l.quoted_price is not None]
                dist_map = {d.id: d for d in all_distributors}
                bls_map = {b.ingredient_id: b for b in session.query(USDAPrice).filter(
                    USDAPrice.ingredient_id.in_(ingredient_ids_in_scope)
                ).all()}

                # Aggregate weekly quantities (for total and selected ingredient BLS estimate)
                weekly_qty_map = {}
                if ps["restaurant_id"]:
                    recipe_ings = session.query(RecipeIngredient).join(Recipe).filter(
                        Recipe.restaurant_id == ps["restaurant_id"],
                        RecipeIngredient.ingredient_id.in_(ingredient_ids_in_scope),
                    ).all()
                    recipe_ids = {ri.recipe_id for ri in recipe_ings}
                    recipes_map = {r.id: r for r in session.query(Recipe).filter(
                        Recipe.id.in_(recipe_ids)
                    ).all()} if recipe_ids else {}
                    ing_qty_map = {iid: ingredients_map[iid] for iid in ingredient_ids_in_scope if iid in ingredients_map}
                    weekly_qty_map = aggregate_quantities(
                        recipe_ings,
                        baseline_weekly_covers,
                        ing_qty_map,
                        recipes_map,
                        weekly_covers_by_category=category_weekly_covers or None,
                    )

                # ── Best Prices (primary table) ──
                st.subheader("Best Prices — Weekly Order Estimate")
                st.caption("Market reference uses a wholesale proxy: 50% of BLS retail average.")
                if not quoted:
                    st.info("No quoted items for current category filter. Use the selector below to inspect provider status by ingredient.")
                else:
                    seen = {}
                    for link in quoted:
                        ing = ingredients_map[link.ingredient_id]
                        if ing.name not in seen or (link.quoted_price and link.quoted_price < seen[ing.name][1]):
                            dist = dist_map.get(link.distributor_id)
                            if not dist:
                                continue
                            seen[ing.name] = (dist.name, link.quoted_price, link.quoted_unit, link.ingredient_id)
                    best_rows = []
                    total_weekly_cost = 0.0
                    for ing_name, (dist_name, price, unit, ing_id) in seen.items():
                        bls = bls_map.get(ing_id)
                        weekly_qty, _ = weekly_qty_map.get(ing_id, (None, unit or "unit"))
                        weekly_cost = price * weekly_qty if weekly_qty else None
                        if weekly_cost:
                            total_weekly_cost += weekly_cost

                        market_flag = ""
                        if bls and bls.price:
                            wholesale_ref = bls.price * 0.5
                            diff_pct = ((price - wholesale_ref) / wholesale_ref) * 100 if wholesale_ref else 0.0
                            if diff_pct > 20:
                                market_flag = f"+{diff_pct:.0f}%"
                            elif diff_pct < -10:
                                market_flag = f"{diff_pct:.0f}%"
                            else:
                                market_flag = "~ near wholesale ref"

                        best_rows.append({
                            "Category": ingredient_category_map.get(ing_id, "Other"),
                            "Ingredient": ing_name,
                            "Best Distributor": dist_name,
                            "Unit Price": f"${price:.2f}/{unit or 'unit'}",
                            "Weekly Cost": f"${weekly_cost:.2f}" if weekly_cost else "N/A",
                            "vs Market": market_flag,
                        })
                    best_rows.sort(key=lambda r: (r["Category"], r["Ingredient"]))
                    st.dataframe(best_rows, use_container_width=True)

                    if total_weekly_cost > 0:
                        cost1, cost2 = st.columns(2)
                        cost1.metric("Estimated Weekly Total", f"${total_weekly_cost:,.2f}")
                        cost2.metric("Estimated Monthly Total", f"${total_weekly_cost * 4.33:,.2f}")

                # ── Ingredient -> Provider status matrix ──
                ingredient_options = {
                    f"{ingredients_map[iid].name} ({ingredient_category_map.get(iid, 'Other')})": iid
                    for iid in ingredient_ids_in_scope
                    if iid in ingredients_map
                }
                selected_ing_label = st.selectbox(
                    "Inspect One Ingredient Across Providers",
                    sorted(ingredient_options.keys()),
                    key="step5_ing_provider_selector",
                )
                selected_ing_id = ingredient_options[selected_ing_label]
                selected_ing = ingredients_map[selected_ing_id]
                selected_ing_links = [l for l in filtered_links if l.ingredient_id == selected_ing_id]

                bls = bls_map.get(selected_ing_id)
                bls_est_str = "N/A"
                if bls and bls.price:
                    bls_est_str = f"${bls.price * 0.5:.2f}/{bls.unit or 'unit'}"
                weekly_qty, weekly_unit = weekly_qty_map.get(selected_ing_id, (None, selected_ing.base_unit or "unit"))
                weekly_bls_est = None
                if bls and bls.price and weekly_qty:
                    weekly_bls_est = (bls.price * 0.5) * weekly_qty

                st.markdown(f"**BLS Estimated Cost ({selected_ing.name})**")
                st.table([{
                    "Ingredient": selected_ing.name,
                    "Category": ingredient_category_map.get(selected_ing_id, "Other"),
                    "Wholesale Ref (50% BLS)": bls_est_str,
                    "Estimated Weekly Qty": f"{weekly_qty:.0f} {weekly_unit}" if weekly_qty else "N/A",
                    "Estimated Weekly BLS Cost": f"${weekly_bls_est:,.0f}" if weekly_bls_est is not None else "N/A",
                }])

                provider_rows = []
                for l in selected_ing_links:
                    dist = dist_map.get(l.distributor_id)
                    if not dist:
                        continue
                    if l.supply_status == "confirmed":
                        provider_signal = "✅ Provides"
                    elif l.supply_status == "does_not_supply":
                        provider_signal = "❌ Confirmed No"
                    else:
                        provider_signal = "❓ May Provide"

                    if dist.rfp_status in ("completed", "needs_clarification"):
                        reply_signal = "📩 Replied"
                    elif dist.rfp_status == "sent":
                        reply_signal = "⏳ Yet To Reply"
                    else:
                        reply_signal = dist.rfp_status

                    if dist.rating and dist.rating_count:
                        weighted = dist.rating * min(dist.rating_count / 50, 1.0)
                        rating_str = f"{dist.rating:.1f} ({dist.rating_count}) w:{weighted:.1f}"
                    elif dist.rating:
                        rating_str = f"{dist.rating:.1f}"
                    else:
                        rating_str = "N/A"

                    provider_rows.append({
                        "Provider": dist.name,
                        "Reply": reply_signal,
                        "Supply Status": provider_signal,
                        "Rating": rating_str,
                        "Quoted Price": f"${l.quoted_price:.2f}/{l.quoted_unit or 'unit'}" if l.quoted_price is not None else "N/A",
                        "Delivery Terms": l.delivery_terms or "N/A",
                    })
                provider_rows.sort(key=lambda r: (r["Supply Status"], r["Provider"]))
                st.dataframe(provider_rows, use_container_width=True)
    finally:
        session.close()

# ── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption("Pathway Take-Home Exercise — RFP Pipeline")
