"""Knasta.cl Chilean price-history & multi-store aggregator connector."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from src.models import ProductSnapshot
from src.scrapers.http_client import DEFAULT_HEADERS, DEFAULT_TIMEOUT, parse_clp_price
from src.scrapers.solotodo_client import infer_brand_from_title

logger = logging.getLogger(__name__)

KNASTA_RESULTS_URL = "https://knasta.cl/results"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Verified official Chilean retailers covered on Knasta (excludes unknown marketplaces/groceries)
ALLOWED_RETAILS = {
    "falabella": "Falabella",
    "paris": "Paris",
    "ripley": "Ripley",
    "mercadolibre": "MercadoLibre",
    "lider": "Lider",
    "pcfactory": "PC Factory",
    "hites": "Hites",
    "abc": "ABC",
    "lapolar": "La Polar",
    "tecnomas": "Tecnomas",
}

KNASTA_QUERIES: list[tuple[Optional[str], dict[str, Any]]] = [
    (None, {"knastaday": 1}),
    ("notebooks", {"q": "notebook", "knastaday": 1}),
    ("audio", {"q": "audifonos", "knastaday": 1}),
    ("audio", {"q": "parlante", "knastaday": 1}),
    ("audio", {"q": "tocadiscos", "order": "percent"}),
    ("componentes", {"q": "monitor", "knastaday": 1}),
    ("componentes", {"q": "teclado", "knastaday": 1}),
    ("componentes", {"q": "mouse", "knastaday": 1}),
    ("camaras", {"q": "camara", "knastaday": 1}),
    ("cafe", {"q": "cafetera", "knastaday": 1}),
]


def _infer_category_from_title(title: str) -> Optional[str]:
    low = title.lower()
    if any(k in low for k in ("smart tv", "televisor", "oled", "qled", "nanocell", "tv 4k", "tv led")):
        return "tv"
    if any(k in low for k in ("notebook", "laptop", "macbook", "chromebook", "thinkpad", "vivobook")):
        return "notebooks"
    if any(k in low for k in ("audifono", "audífono", "headset", "earbuds", "parlante", "soundbar", "tocadiscos", "tornamesa")):
        return "audio"
    if any(k in low for k in ("monitor", "teclado", "mouse", "rtx", "tarjeta de video", "ssd")):
        return "componentes"
    if any(k in low for k in ("camara", "cámara", "gopro", "mirrorless", "reflex")):
        return "camaras"
    if any(k in low for k in ("cafetera", "espresso", "molinillo", "capsulas de cafe")):
        return "cafe"
    if any(k in low for k in ("playstation", "ps5", "nintendo switch", "xbox")):
        return "consolas"
    if any(k in low for k in ("iphone", "galaxy s", "smartphone", "celular", "redmi", "motorola")):
        return "celulares"
    return None


def fetch_knasta_page(params: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Fetch a Knasta search/deals page and parse its embedded __NEXT_DATA__ JSON."""
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        resp = requests.get(
            KNASTA_RESULTS_URL,
            params=params,
            headers=headers,
            timeout=DEFAULT_TIMEOUT + 6,
        )
        if resp.status_code in (403, 429, 503):
            logger.warning("Knasta returned HTTP %s for params %s", resp.status_code, params)
            return None
        resp.raise_for_status()
        match = _NEXT_DATA_RE.search(resp.text)
        if not match:
            return None
        parsed = json.loads(match.group(1))
        return ((parsed.get("props") or {}).get("pageProps") or {}).get("initialData") or {}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed fetching Knasta %s: %s", params, exc)
        return None


def fetch_knasta_products() -> list[ProductSnapshot]:
    """Fetch Todo Medio de Pago deals from Knasta across major Chilean retailers."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for default_category, params in KNASTA_QUERIES:
        data = fetch_knasta_page(params)
        if not isinstance(data, dict):
            continue

        for item in data.get("products") or []:
            retail_key = str(item.get("retail") or "").strip().lower()
            if retail_key not in ALLOWED_RETAILS:
                continue
            store_label = ALLOWED_RETAILS[retail_key]

            raw_id = str(item.get("product_id") or item.get("kid") or "").strip()
            if not raw_id:
                continue
            sku = f"KN-{retail_key.upper()}-{raw_id}"
            if sku in seen_skus:
                continue

            title = str(item.get("title") or "").strip()
            if not title:
                continue

            category = _infer_category_from_title(title) or default_category
            if not category:
                continue

            price_internet = parse_clp_price(item.get("price_internet"))
            price_card = parse_clp_price(item.get("price_card"))
            raw_current = parse_clp_price(item.get("current_price"))

            # Strictly exclude store card prices (CMR / Cencosud / Ripley)
            if price_internet is not None:
                todo_medio_price = price_internet
            elif price_card is not None and (raw_current is None or price_card == raw_current):
                # Price is exclusively a store card price -> skip!
                continue
            else:
                todo_medio_price = raw_current

            if not todo_medio_price:
                continue

            descriptive = item.get("descriptive") or {}
            habitual = parse_clp_price(descriptive.get("habitual_price"))
            last_var = parse_clp_price(item.get("last_variation_price"))
            normal_price = max(
                [p for p in (habitual, last_var, todo_medio_price) if p is not None]
            )

            seen_skus.add(sku)
            brand = str(item.get("brand") or "").strip() or infer_brand_from_title(title)

            snapshots.append(
                ProductSnapshot(
                    sku=sku,
                    store=store_label,
                    title=title,
                    brand=brand,
                    category=category,
                    current_price=todo_medio_price,
                    card_price=None,
                    normal_price=normal_price,
                    seller_name=f"{store_label} (via Knasta)",
                    is_official_seller=True,
                    in_stock=True,
                    url=str(item.get("url") or ""),
                    image_url=str(item.get("image") or item.get("thumbnail_image") or ""),
                )
            )

    return snapshots
