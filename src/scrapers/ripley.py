"""Ripley.cl 1P official catalog connector."""

from __future__ import annotations

from src.models import ProductSnapshot
from src.scrapers.solotodo_client import fetch_solotodo_store_products

# SoloTodo Store ID 18 = Ripley 1P official (excludes 8510 Ripley Marketplace)
RIPLEY_1P_STORE_ID = 18


def fetch_ripley_products(pages_per_category: int = 1) -> list[ProductSnapshot]:
    """Fetch official 1P Ripley Chile technology products."""
    return fetch_solotodo_store_products(
        store_ids=[RIPLEY_1P_STORE_ID],
        store_label="Ripley",
        sku_prefix="RIP",
        seller_name="Ripley",
        page_size=40 * max(1, pages_per_category),
    )
