"""Falabella Chile 1P official catalog connector (via __NEXT_DATA__ SSR)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from src.models import ProductSnapshot
from src.scrapers.http_client import DEFAULT_HEADERS, DEFAULT_TIMEOUT, parse_clp_price

logger = logging.getLogger(__name__)

CATEGORY_URLS = {
    "notebooks": "https://www.falabella.com/falabella-cl/category/cat70057/Notebooks",
    "celulares": "https://www.falabella.com/falabella-cl/category/cat2018/Celulares-y-Telefonos",
    "consolas": "https://www.falabella.com/falabella-cl/category/cat3101/Consolas",
    "tv": "https://www.falabella.com/falabella-cl/category/cat1012/TV-y-Video",
    "audio": "https://www.falabella.com/falabella-cl/category/cat1640001/Audio",
    "componentes": "https://www.falabella.com/falabella-cl/category/cat2062/Monitores",
    "camaras": "https://www.falabella.com/falabella-cl/category/cat2023/Camaras",
}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


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


def fetch_json(url: str, page: int = 1) -> Optional[dict[str, Any]]:
    """Fetch a Falabella category page and extract its embedded __NEXT_DATA__ results."""
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        req_params = {"page": page} if page > 1 else None
        resp = requests.get(
            url,
            params=req_params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT + 8,
        )
        if resp.status_code in (403, 429, 503):
            logger.warning("Falabella returned HTTP %s for %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        match = _NEXT_DATA_RE.search(resp.text)
        if not match:
            return None
        parsed = json.loads(match.group(1))
        results = ((parsed.get("props") or {}).get("pageProps") or {}).get("results") or []
        return {"data": {"results": results}}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed fetching Falabella %s: %s", url, exc)
        return None


def fetch_falabella_products(pages_per_category: int = 1) -> list[ProductSnapshot]:
    """Fetch 1P official Falabella Chile technology listings."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, category_url in CATEGORY_URLS.items():
        for page in range(1, pages_per_category + 1):
            data = fetch_json(category_url, page=page)
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
                is_official = (
                    seller_id in ("FALABELLA", "FALABELLA_CHILE")
                    or seller_name.lower() == "falabella"
                )
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
                availability = item.get("availability") or {}
                out_of_stock = bool(item.get("isOutOfStock", False))
                if isinstance(availability, dict) and availability.get("internationalShipping") is None:
                    # Check explicit out-of-stock flags if present
                    pass

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
