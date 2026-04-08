"""SQLite database setup with SQLAlchemy."""

from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "rfp_pipeline.db"
ENGINE = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=ENGINE)


class Base(DeclarativeBase):
    pass


def init_db():
    """Create all tables."""
    from app.models import (  # noqa: F401 — import to register models
        Restaurant, Recipe, Ingredient, RecipeIngredient,
        BLSCache, USDAPrice,
        Distributor, DistributorIngredient,
    )
    Base.metadata.create_all(ENGINE)
    _ensure_schema_columns()


def _ensure_schema_columns():
    """Lightweight SQLite migrations for newly added columns."""
    with ENGINE.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(usda_prices)")).fetchall()
        if not rows:
            return
        existing_cols = {r[1] for r in rows}
        needed = {
            "bls_series_id": "TEXT",
            "trend_direction": "TEXT",
            "trend_abs_change": "FLOAT",
            "trend_pct_change": "FLOAT",
            "trend_months": "INTEGER",
            "trend_summary": "TEXT",
            "trend_tags": "TEXT",
        }
        for col, col_type in needed.items():
            if col not in existing_cols:
                conn.execute(text(f"ALTER TABLE usda_prices ADD COLUMN {col} {col_type}"))

        dist_rows = conn.execute(text("PRAGMA table_info(distributor_ingredients)")).fetchall()
        if not dist_rows:
            return
        dist_existing_cols = {r[1] for r in dist_rows}
        dist_needed = {
            "delivery_charge": "FLOAT",
            "delivery_charge_unit": "TEXT",
            "delivery_charge_notes": "TEXT",
        }
        for col, col_type in dist_needed.items():
            if col not in dist_existing_cols:
                conn.execute(text(f"ALTER TABLE distributor_ingredients ADD COLUMN {col} {col_type}"))
