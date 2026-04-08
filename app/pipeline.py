"""Orchestrator: runs the full RFP pipeline end-to-end."""

from dotenv import load_dotenv
from app.db import init_db, SessionLocal
from app.services.menu_parser import parse_menu
from app.services.usda_client import fetch_market_trends
from app.services.distributor_finder import find_local_distributors
from app.services.email_sender import send_rfp_emails
# from app.services.inbox_monitor import collect_quotes


def run_pipeline(
    menu_image_path: str,
    restaurant_name: str,
    restaurant_location: str = "",
    menu_url: str = "",
    skip_step2: bool = False,
) -> dict:
    """
    Run the full RFP pipeline.

    Returns a dict with results from each step.
    """
    load_dotenv()
    init_db()

    session = SessionLocal()
    results = {}

    try:
        # Step 1: Menu -> Recipes & Ingredients
        print("\n" + "=" * 60)
        print("STEP 1: Parsing Menu into Recipes & Ingredients")
        print("=" * 60)

        # Skip if DB already has data for this restaurant
        from app.models import Restaurant, Recipe
        existing = session.query(Restaurant).filter_by(name=restaurant_name).first()
        if existing and session.query(Recipe).filter_by(restaurant_id=existing.id).count() > 0:
            restaurant = existing
            recipes = session.query(Recipe).filter_by(restaurant_id=existing.id).all()
            print(f"  Skipping — found {len(recipes)} existing recipes for '{restaurant_name}'")
        else:
            restaurant, recipes = parse_menu(
                session, restaurant_name, menu_image_path,
                location=restaurant_location, menu_url=menu_url,
            )
        results["restaurant"] = restaurant
        results["recipes"] = recipes

        # Step 2: Market Price Trends
        if skip_step2:
            print("\n" + "=" * 60)
            print("STEP 2: Fetching Market Price Trends (BLS) [SKIPPED]")
            print("=" * 60)
            results["prices"] = []
        else:
            print("\n" + "=" * 60)
            print("STEP 2: Fetching Market Price Trends (BLS)")
            print("=" * 60)
            try:
                prices = fetch_market_trends(session)
                results["prices"] = prices
            except Exception as e:
                print(f"  Skipping Step 2: {e}")
                results["prices"] = []

        # Step 3: Find Distributors
        print("\n" + "=" * 60)
        print("STEP 3: Finding Local Distributors")
        print("=" * 60)
        import os
        location = restaurant_location or os.getenv("RESTAURANT_LOCATION", "Atlanta, GA")
        distributors = find_local_distributors(session, location)
        results["distributors"] = distributors

        # Step 4: Send RFP Emails
        print("\n" + "=" * 60)
        print("STEP 4: Sending RFP Emails")
        print("=" * 60)
        emails = send_rfp_emails(session, restaurant.id, mock_recipient="demo")
        results["emails"] = emails

        # # Step 5: Monitor Inbox (nice-to-have)
        # print("\n" + "=" * 60)
        # print("STEP 5: Monitoring Inbox for Quotes")
        # print("=" * 60)
        # quotes = collect_quotes(session, restaurant.id)
        # results["quotes"] = quotes

        print("\n" + "=" * 60)
        print("PIPELINE COMPLETE")
        print("=" * 60)

    finally:
        session.close()

    return results


if __name__ == "__main__":
    # ── CONFIG: edit these to test ───────────────────────────────
    RESTAURANT_NAME = "Irene's Cuisine"
    RESTAURANT_LOCATION = "New Orleans, LA"
    MENU_IMAGE = "img/irenes_nola.jpg"
    MENU_URL = ""
    # ─────────────────────────────────────────────────────────────

    run_pipeline(
        menu_image_path=MENU_IMAGE,
        restaurant_name=RESTAURANT_NAME,
        restaurant_location=RESTAURANT_LOCATION,
        menu_url=MENU_URL,
        skip_step2=True,
    )
