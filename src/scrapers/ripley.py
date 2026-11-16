"""Ripley.cl 1P official catalog connector."""

from __future__ import annotations

from typing import Any

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price

RIPLEY_API_URL = "https://simple.ripley.cl/api/v2/products"

CATEGORY_PATHS = {
    "notebooks": "tecno/computacion/notebooks",
    "celulares": "tecno/celulares/smartphones",
    "consolas": "tecno/consolas-y-videojuegos",
    "tv": "tecno/television",
    "audio": "tecno/audio",
}


def fetch_ripley_products(pages_per_category: int = 1) -> list[ProductSnapshot]:
    """Fetch official 1P Ripley Chile technology products."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, category_path in CATEGORY_PATHS.items():
        for page in range(1, pages_per_category + 1):
            data = fetch_json(
                RIPLEY_API_URL,
                params={
                    "categoryPath": category_path,
                    "page": page,
                    "isMarketplace": "false",
                },
                headers={"Referer": "https://simple.ripley.cl/"},
            )
            if not isinstance(data, dict):
                break

            products: list[dict[str, Any]] = data.get("products") or []
            if not products:
                break

            for item in products:
                part_number = str(item.get("partNumber") or item.get("uniqueID") or "").strip()
                if not part_number:
                    continue
                sku = f"RIP-{part_number}"
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)

                is_marketplace = bool(item.get("isMarketplaceProduct", False))
                seller = str(item.get("sellerName") or "Ripley").strip()
                is_official = (not is_marketplace) and seller.lower() in ("ripley", "ripley.cl")
                if not is_official:
                    continue

                prices = item.get("prices") or {}
                current_price = parse_clp_price(prices.get("offerPrice") or prices.get("price"))
                card_price = parse_clp_price(prices.get("cardPrice"))
                normal_price = parse_clp_price(prices.get("listPrice"))
                if not current_price:
                    continue

                url = str(item.get("url") or "")
                if url.startswith("/"):
                    url = f"https://simple.ripley.cl{url}"

                snapshots.append(
                    ProductSnapshot(
                        sku=sku,
                        store="Ripley",
                        title=str(item.get("name") or "").strip(),
                        brand=str(item.get("brand") or "Marca").strip(),
                        category=category_name,
                        current_price=current_price,
                        card_price=card_price,
                        normal_price=normal_price or current_price,
                        seller_name="Ripley",
                        is_official_seller=True,
                        in_stock=not bool(item.get("isOutOfStock", False)),
                        url=url,
                        image_url=str(item.get("fullImage") or item.get("thumbnailImage") or ""),
                    )
                )
    return snapshots
