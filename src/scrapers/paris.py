"""Paris.cl 1P official catalog connector."""

from __future__ import annotations

from src.models import ProductSnapshot
from src.scrapers.solotodo_client import fetch_solotodo_store_products

# SoloTodo Store ID 11 = Paris 1P official (excludes 8148 Paris Marketplace)
PARIS_1P_STORE_ID = 11


def fetch_paris_products(limit_per_category: int = 40) -> list[ProductSnapshot]:
    """Fetch official 1P Paris.cl technology products."""
    return fetch_solotodo_store_products(
        store_ids=[PARIS_1P_STORE_ID],
        store_label="Paris",
        sku_prefix="PAR",
        seller_name="Paris",
        page_size=limit_per_category,
    )
