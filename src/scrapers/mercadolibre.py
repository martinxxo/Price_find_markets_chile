"""MercadoLibre Chile (MLC) official stores connector."""

from __future__ import annotations

from src.models import ProductSnapshot
from src.scrapers.solotodo_client import fetch_solotodo_store_products

# SoloTodo verified MercadoLibre Chile official stores:
# 195 = Mercado Libre LG, 225 = Mercado Libre Samsung, 260 = Mercado Libre Oficial
ML_OFFICIAL_STORE_IDS = [195, 225, 260]


def fetch_mercadolibre_products(limit_per_category: int = 50) -> list[ProductSnapshot]:
    """Fetch new products from MercadoLibre Chile official stores."""
    return fetch_solotodo_store_products(
        store_ids=ML_OFFICIAL_STORE_IDS,
        store_label="MercadoLibre",
        sku_prefix="MLC",
        seller_name="MercadoLibre Tienda Oficial",
        page_size=limit_per_category,
    )
