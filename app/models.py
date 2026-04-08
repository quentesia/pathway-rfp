"""SQLAlchemy ORM models for the RFP pipeline."""

from datetime import date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, Date, ForeignKey, DateTime
)
from sqlalchemy.orm import relationship
from app.db import Base


def _today():
    return date.today()


# ── Core ─────────────────────────────────────────────────────────────────────

class Restaurant(Base):
    __tablename__ = "restaurants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    location = Column(String)
    menu_source_url = Column(String)
    created_at = Column(Date, default=_today)
    
    rfp_emails = relationship("RFPEmail", back_populates="restaurant")

    recipes = relationship("Recipe", back_populates="restaurant")
    # rfp_emails = relationship("RFPEmail", back_populates="restaurant")


class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False)
    dish_name = Column(String, nullable=False)
    dish_description = Column(Text)
    category = Column(String)
    estimated_servings = Column(Integer)
    created_at = Column(Date, default=_today)

    restaurant = relationship("Restaurant", back_populates="recipes")
    recipe_ingredients = relationship("RecipeIngredient", back_populates="recipe")


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    category = Column(String)
    base_unit = Column(String)
    perishable = Column(Boolean, default=True)
    usda_id = Column(String)  # BLS series ID matched by Claude
    created_at = Column(Date, default=_today)

    recipe_ingredients = relationship("RecipeIngredient", back_populates="ingredient")
    usda_prices = relationship("USDAPrice", back_populates="ingredient")
    distributor_links = relationship("DistributorIngredient", back_populates="ingredient")


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String, nullable=False)
    notes = Column(Text)

    recipe = relationship("Recipe", back_populates="recipe_ingredients")
    ingredient = relationship("Ingredient", back_populates="recipe_ingredients")


# ── Pricing (Step 2) ─────────────────────────────────────────────────────────

class USDAPrice(Base):
    __tablename__ = "usda_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False)
    usda_item_name = Column(String)
    price = Column(Float)
    unit = Column(String)
    date = Column(String)
    source = Column(String, default="BLS Average Price Data")
    created_at = Column(Date, default=_today)

    ingredient = relationship("Ingredient", back_populates="usda_prices")


# ── Step 3: Distributor Finding ──────────────────────────────────────────────

class Distributor(Base):
    __tablename__ = "distributors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    location = Column(String)
    phone = Column(String)
    email = Column(String)
    website = Column(String)
    source = Column(String)  # Google, SerpAPI, Claude
    categories_served = Column(String)

    ingredient_links = relationship("DistributorIngredient", back_populates="distributor")
    rfp_emails = relationship("RFPEmail", back_populates="distributor")


class DistributorIngredient(Base):
    __tablename__ = "distributor_ingredients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    distributor_id = Column(Integer, ForeignKey("distributors.id"), nullable=False)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False)

    distributor = relationship("Distributor", back_populates="ingredient_links")
    ingredient = relationship("Ingredient", back_populates="distributor_links")


# ── RFP (Steps 4-5) ─────────────────────────────────────────────────────────

class RFPEmail(Base):
    __tablename__ = "rfp_emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    distributor_id = Column(Integer, ForeignKey("distributors.id"), nullable=False)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id"), nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    sent_at = Column(DateTime)
    status = Column(String, default="draft")

    distributor = relationship("Distributor", back_populates="rfp_emails")
    restaurant = relationship("Restaurant", back_populates="rfp_emails")
    quotes = relationship("RFPQuote", back_populates="rfp_email")


class RFPQuote(Base):
    __tablename__ = "rfp_quotes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    rfp_email_id = Column(Integer, ForeignKey("rfp_emails.id"), nullable=False)
    distributor_id = Column(Integer, ForeignKey("distributors.id"), nullable=False)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False)
    quoted_price = Column(Float)
    unit = Column(String)
    delivery_terms = Column(String)
    raw_text = Column(Text)

    rfp_email = relationship("RFPEmail", back_populates="quotes")
    distributor = relationship("Distributor")
    ingredient = relationship("Ingredient")
