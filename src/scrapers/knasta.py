"""Knasta.cl Chilean price-history & multi-store aggregator connector (Black Friday expanded & concurrent)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
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

# Expanded official Black Friday Chile retailers covered on Knasta
ALLOWED_RETAILS = {
    "falabella": "Falabella",
    "paris": "Paris",
    "ripley": "Ripley",
    "mercadolibre": "MercadoLibre",
    "lider": "Lider",
    "lider_supermercado": "Lider Supermercado",
    "santaisabel": "Santa Isabel",
    "pcfactory": "PC Factory",
    "pccom-chile": "Pccom Chile",
    "casaroyal": "Casa Royal",
    "easy": "Easy",
    "hites": "Hites",
    "abc": "ABC",
    "lapolar": "La Polar",
    "tecnomas": "Tecnomas",
    "smartdeal": "Smartdeal",
    "ebest": "Ebest",
    "thehouseofmarley": "House of Marley",
    "thomas": "Thomas Chile",
    "siegen": "Siegen",
    "masterg": "Master-G",
    "hp": "HP Online",
    "samsung": "Samsung Shop",
    "xiaomi": "Xiaomi Chile",
    "paramicafe": "Paramicafe",
    "lindt": "Lindt",
    "emma": "Emma Chile",
}

KNASTA_QUERIES: list[tuple[Optional[str], dict[str, Any]]] = [
    (None, {"knastaday": 1}),
    ("notebooks", {"q": "notebook", "knastaday": 1}),
    ("notebooks", {"q": "tablet", "knastaday": 1}),
    ("celulares", {"q": "smartphone", "knastaday": 1}),
    ("consolas", {"q": "playstation 5", "order": "percent"}),
    ("consolas", {"q": "nintendo switch", "order": "percent"}),
    ("audio", {"q": "audifonos", "knastaday": 1}),
    ("audio", {"q": "parlante", "knastaday": 1}),
    ("audio", {"q": "soundbar", "order": "percent"}),
    ("audio", {"q": "tocadiscos", "order": "percent"}),
    ("componentes", {"q": "monitor", "knastaday": 1}),
    ("componentes", {"q": "teclado", "knastaday": 1}),
    ("componentes", {"q": "mouse", "knastaday": 1}),
    ("camaras", {"q": "camara", "knastaday": 1}),
    ("cafe", {"q": "cafetera", "knastaday": 1}),
    ("cafe", {"q": "cafe grano", "order": "percent"}),
    ("electrodomesticos", {"q": "air fryer", "knastaday": 1}),
    ("electrodomesticos", {"q": "aspiradora robot", "knastaday": 1}),
    ("electrodomesticos", {"q": "microondas", "knastaday": 1}),
    ("comida", {"q": "chocolate", "order": "percent"}),
]


def _infer_category_from_title(title: str) -> Optional[str]:
    low = title.lower()
    if any(k in low for k in ("smart tv", "televisor", "oled", "qled", "nanocell", "tv 4k", "tv led")):
        return "tv"
    if any(k in low for k in ("notebook", "laptop", "macbook", "chromebook", "thinkpad", "vivobook", "ipad", "tablet")):
        return "notebooks"
    if any(k in low for k in ("audifono", "audífono", "headset", "earbuds", "parlante", "soundbar", "barra de sonido", "tocadiscos", "tornamesa")):
        return "audio"
    if any(k in low for k in ("monitor", "teclado", "mouse", "rtx", "tarjeta de video", "ssd")):
        return "componentes"
    if any(k in low for k in ("camara", "cámara", "gopro", "insta360", "mirrorless", "reflex")):
        return "camaras"
    if any(k in low for k in ("cafetera", "espresso", "molinillo", "capsulas de cafe", "cafe en grano", "café en grano")):
        return "cafe"
    if any(k in low for k in ("playstation", "ps5", "nintendo switch", "xbox")):
        return "consolas"
    if any(k in low for k in ("iphone", "galaxy s", "smartphone", "celular", "redmi", "motorola")):
        return "celulares"
    if any(k in low for k in ("air fryer", "freidora", "aspiradora", "refrigerador", "microondas", "lavadora", "licuadora", "horno")):
        return "electrodomesticos"
    if any(k in low for k in ("colchon", "colchón", "cama ", "almohada", "termo", "stanley")):
        return "hogar"
    if any(k in low for k in ("chocolate", "bombones", "vino", "cerveza", "whisky", "gourmet")):
        return "comida"
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
    """Concurrently fetch Todo Medio de Pago deals from Knasta across Black Friday Chile retailers."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    def _run_query(q_pair: tuple[Optional[str], dict[str, Any]]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        return q_pair[0], fetch_knasta_page(q_pair[1])

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_run_query, qp) for qp in KNASTA_QUERIES]
        for fut in as_completed(futures):
            default_category, data = fut.result()
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
