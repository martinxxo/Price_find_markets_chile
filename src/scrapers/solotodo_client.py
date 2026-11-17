"""Shared SoloTodo public API client for verified 1P Chilean store listings."""

from __future__ import annotations

import logging
from typing import Any

from src.detector import RECOGNIZED_TECH_BRANDS
from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price

logger = logging.getLogger(__name__)

SOLOTODO_ENTITIES_URL = "https://publicapi.solotodo.com/entities/"

SOLOTODO_CATEGORIES = [
    ("notebooks", 1),
    ("celulares", 6),
    ("consolas", 33),
    ("tv", 11),
    ("audio", 50),        # Audífonos y Headsets
    ("audio", 25),        # Equipos de audio y parlantes
    ("componentes", 4),   # Monitores
    ("componentes", 40),  # Mouse
    ("componentes", 41),  # Teclados
    ("componentes", 2),   # Tarjetas de video
    ("camaras", 615),     # Cámaras
    ("cafe", 219),        # Cafeteras
]


def infer_brand_from_title(title: str, fallback: str = "Marca") -> str:
    """Infer a recognized tech brand from product title when store API omits brand."""
    for token in title.replace("/", " ").replace("-", " ").split():
        cleaned = token.strip(".,:;()[]{}\"'").lower()
        if cleaned in RECOGNIZED_TECH_BRANDS:
            return cleaned.upper() if len(cleaned) <= 3 else cleaned.capitalize()
    return fallback


def fetch_solotodo_store_products(
    store_ids: list[int],
    store_label: str,
    sku_prefix: str,
    seller_name: str,
    page_size: int = 50,
) -> list[ProductSnapshot]:
    """Fetch in-stock, new, visible 1P products for specific SoloTodo store IDs."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, category_id in SOLOTODO_CATEGORIES:
        params: list[tuple[str, Any]] = [("stores", sid) for sid in store_ids]
        params.extend(
            [
                ("categories", category_id),
                ("is_visible", "true"),
                ("is_available", "true"),
                ("ordering", "-last_pricing_update"),
                ("page_size", page_size),
            ]
        )

        data = fetch_json(SOLOTODO_ENTITIES_URL, params=params, timeout=15)
        if not isinstance(data, dict):
            continue

        for item in data.get("results") or []:
            condition = str(item.get("condition") or "")
            if condition and "NewCondition" not in condition:
                continue

            raw_sku = str(item.get("sku") or item.get("id") or "").strip()
            if not raw_sku:
                continue
            sku = f"{sku_prefix}-{raw_sku}"
            if sku in seen_skus:
                continue
            seen_skus.add(sku)

            registry = item.get("active_registry") or {}
            if not registry.get("is_available", True):
                continue

            normal_price = parse_clp_price(registry.get("normal_price"))
            offer_price = parse_clp_price(registry.get("offer_price"))
            current_price = normal_price or offer_price
            if not current_price:
                continue

            card_price = offer_price if (offer_price and offer_price < current_price) else None
            title = str(item.get("name") or "").strip()
            if not title:
                continue

            pictures = item.get("picture_urls") or []
            image_url = ""
            for pic in pictures:
                if isinstance(pic, str) and "youtube.com" not in pic:
                    image_url = pic
                    break

            snapshots.append(
                ProductSnapshot(
                    sku=sku,
                    store=store_label,
                    title=title,
                    brand=infer_brand_from_title(title),
                    category=category_name,
                    current_price=current_price,
                    card_price=card_price,
                    normal_price=normal_price or current_price,
                    seller_name=seller_name,
                    is_official_seller=True,
                    in_stock=True,
                    url=str(item.get("external_url") or ""),
                    image_url=image_url,
                )
            )

    return snapshots
