"""MercadoLibre Chile (MLC) official stores connector."""

from __future__ import annotations

from typing import Any

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price

MLC_API_URL = "https://api.mercadolibre.com/sites/MLC/search"

CATEGORY_MAP = {
    "notebooks": "MLC1648",
    "celulares": "MLC1051",
    "consolas": "MLC1144",
    "tv": "MLC1002",
    "audio": "MLC1000",
}


def _extract_brand(item: dict[str, Any]) -> str:
    for attr in item.get("attributes", []) or []:
        if attr.get("id") == "BRAND" and attr.get("value_name"):
            return str(attr["value_name"]).strip()
    return str(item.get("official_store_name", "")).strip() or "Marca"


def fetch_mercadolibre_products(limit_per_category: int = 50) -> list[ProductSnapshot]:
    """Fetch new products from MercadoLibre Chile official stores."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, category_id in CATEGORY_MAP.items():
        data = fetch_json(
            MLC_API_URL,
            params={
                "category": category_id,
                "official_store": "all",
                "ITEM_CONDITION": "2230284",
                "limit": limit_per_category,
            },
        )
        if not isinstance(data, dict):
            continue

        for item in data.get("results", []) or []:
            item_id = str(item.get("id", "")).strip()
            if not item_id:
                continue
            sku = f"MLC-{item_id}"
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            current_price = parse_clp_price(item.get("price"))
            if not current_price:
                continue
            normal_price = parse_clp_price(item.get("original_price")) or current_price
            condition = str(item.get("condition", "new")).lower()
            is_official = bool(item.get("official_store_id"))
            if condition != "new" or not is_official:
                continue

            seller_name = (
                str(item.get("official_store_name") or "").strip()
                or str((item.get("seller") or {}).get("nickname") or "Tienda Oficial ML")
            )
            thumb = str(item.get("thumbnail") or "").replace("http://", "https://")

            snapshots.append(
                ProductSnapshot(
                    sku=sku,
                    store="MercadoLibre",
                    title=str(item.get("title", "")).strip(),
                    brand=_extract_brand(item),
                    category=category_name,
                    current_price=current_price,
                    card_price=None,
                    normal_price=normal_price,
                    seller_name=seller_name,
                    is_official_seller=is_official,
                    in_stock=int(item.get("available_quantity", 1) or 0) > 0,
                    url=str(item.get("permalink", "")),
                    image_url=thumb,
                )
            )
    return snapshots
