"""Paris.cl 1P official catalog connector."""

from __future__ import annotations

from typing import Any

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price

PARIS_SEARCH_URL = "https://api-v2.paris.cl/products/search"

CATEGORY_QUERIES = {
    "notebooks": "notebook",
    "celulares": "smartphone",
    "consolas": "consola",
    "tv": "smart tv",
    "audio": "audifonos",
}


def fetch_paris_products(limit_per_category: int = 40) -> list[ProductSnapshot]:
    """Fetch official 1P Paris.cl technology products."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, query in CATEGORY_QUERIES.items():
        data = fetch_json(
            PARIS_SEARCH_URL,
            params={
                "term": query,
                "limit": limit_per_category,
                "page": 1,
                "seller": "Paris",
            },
            headers={"Origin": "https://www.paris.cl", "Referer": "https://www.paris.cl/"},
        )
        if not isinstance(data, dict):
            continue

        items: list[dict[str, Any]] = (
            data.get("results")
            or data.get("hits")
            or data.get("products")
            or []
        )
        for item in items:
            raw_sku = str(item.get("sku") or item.get("id") or item.get("masterId") or "").strip()
            if not raw_sku:
                continue
            sku = f"PAR-{raw_sku}"
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            seller_name = str(item.get("sellerName") or item.get("seller") or "Paris").strip()
            is_marketplace = bool(item.get("isMarketplace", False))
            is_official = (not is_marketplace) and seller_name.lower() in ("paris", "paris.cl", "cencosud")
            if not is_official:
                continue

            prices = item.get("prices") if isinstance(item.get("prices"), dict) else item
            current_price = parse_clp_price(
                prices.get("offerPrice") or prices.get("internetPrice") or prices.get("price")
            )
            card_price = parse_clp_price(prices.get("cardPrice") or prices.get("cencosudPrice"))
            normal_price = parse_clp_price(prices.get("normalPrice") or prices.get("listPrice"))
            if not current_price:
                continue

            url = str(item.get("url") or item.get("slug") or "")
            if url and not url.startswith("http"):
                url = f"https://www.paris.cl/{url.lstrip('/')}"

            snapshots.append(
                ProductSnapshot(
                    sku=sku,
                    store="Paris",
                    title=str(item.get("name") or item.get("title") or "").strip(),
                    brand=str(item.get("brand") or "Marca").strip(),
                    category=category_name,
                    current_price=current_price,
                    card_price=card_price,
                    normal_price=normal_price or current_price,
                    seller_name="Paris",
                    is_official_seller=True,
                    in_stock=bool(item.get("inStock", not item.get("isOutOfStock", False))),
                    url=url,
                    image_url=str(item.get("image") or item.get("imageUrl") or ""),
                )
            )
    return snapshots
