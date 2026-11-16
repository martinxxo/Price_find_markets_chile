"""Falabella Chile 1P official catalog connector."""

from __future__ import annotations

from typing import Any, Optional

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price

FALABELLA_LISTING_URL = "https://www.falabella.com/s/browse/v1/listing/cl"

CATEGORY_MAP = {
    "notebooks": "cat70057",
    "celulares": "cat2018",
    "consolas": "cat3101",
    "tv": "cat1012",
    "audio": "cat1640001",
}


def _extract_prices(price_entries: list[dict[str, Any]]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    current_price: Optional[int] = None
    card_price: Optional[int] = None
    normal_price: Optional[int] = None

    for entry in price_entries or []:
        p_type = str(entry.get("type", "")).lower()
        raw_list = entry.get("price") or []
        raw_val = raw_list[0] if isinstance(raw_list, list) and raw_list else entry.get("price")
        parsed = parse_clp_price(raw_val)
        if not parsed:
            continue

        if "cmr" in p_type or "card" in p_type:
            card_price = parsed
        elif "normal" in p_type or "regular" in p_type:
            normal_price = parsed
        elif "event" in p_type or "internet" in p_type or "offer" in p_type:
            if current_price is None or parsed < current_price:
                current_price = parsed

    if current_price is None:
        current_price = normal_price or card_price
    if normal_price is None:
        normal_price = current_price
    return current_price, card_price, normal_price


def fetch_falabella_products(pages_per_category: int = 1) -> list[ProductSnapshot]:
    """Fetch 1P official Falabella Chile technology listings."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, category_id in CATEGORY_MAP.items():
        for page in range(1, pages_per_category + 1):
            data = fetch_json(
                FALABELLA_LISTING_URL,
                params={
                    "categoryId": category_id,
                    "page": page,
                    "zone": "13",
                    "sortBy": "derived.price.event,asc",
                },
                headers={"Referer": "https://www.falabella.com/falabella-cl"},
            )
            if not isinstance(data, dict):
                break

            results = ((data.get("data") or {}).get("results")) or []
            if not results:
                break

            for item in results:
                raw_sku = str(item.get("skuId") or item.get("productId") or "").strip()
                if not raw_sku:
                    continue
                sku = f"FAL-{raw_sku}"
                if sku in seen_skus:
                    continue
                seen_skus.add(sku)

                seller_id = str(item.get("sellerId") or "").strip().upper()
                seller_name = str(item.get("sellerName") or seller_id or "").strip()
                is_official = seller_id == "FALABELLA" or seller_name.lower() == "falabella"
                if not is_official:
                    continue

                current_price, card_price, normal_price = _extract_prices(item.get("prices") or [])
                if not current_price:
                    continue

                url = str(item.get("url") or "")
                if url.startswith("/"):
                    url = f"https://www.falabella.com{url}"
                media = item.get("mediaUrls") or []
                image_url = str(media[0]) if isinstance(media, list) and media else ""
                out_of_stock = bool(item.get("isOutOfStock", False))

                snapshots.append(
                    ProductSnapshot(
                        sku=sku,
                        store="Falabella",
                        title=str(item.get("displayName") or "").strip(),
                        brand=str(item.get("brand") or "Marca").strip(),
                        category=category_name,
                        current_price=current_price,
                        card_price=card_price,
                        normal_price=normal_price,
                        seller_name=seller_name or "Falabella",
                        is_official_seller=is_official,
                        in_stock=not out_of_stock,
                        url=url,
                        image_url=image_url,
                    )
                )
    return snapshots
