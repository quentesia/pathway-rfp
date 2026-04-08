"""SQLite database setup with SQLAlchemy."""

from sqlalchemy import create_engine
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
        # USDAPrice, Distributor, DistributorIngredient,
        # RFPEmail, RFPQuote,
    )
    Base.metadata.create_all(ENGINE)


def get_session():
    """Yield a database session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
