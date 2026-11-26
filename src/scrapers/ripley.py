"""Ripley.cl 1P official direct SSR catalog connector (with automatic cloud fallback)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
from typing import Any, Optional

import requests

from src.models import ProductSnapshot
from src.scrapers.http_client import DEFAULT_HEADERS, DEFAULT_TIMEOUT, get_http_session, parse_clp_price
from src.scrapers.solotodo_client import fetch_solotodo_store_products

logger = logging.getLogger(__name__)

RIPLEY_1P_STORE_ID = 18

RIPLEY_DIRECT_URLS = [
    ("notebooks", "https://simple.ripley.cl/tecno/computacion/notebooks?s=mdco"),
    ("celulares", "https://simple.ripley.cl/search/smartphone?s=mdco"),
    ("consolas", "https://simple.ripley.cl/search/consola?s=mdco"),
    ("tv", "https://simple.ripley.cl/tecno/television/smart-tv?s=mdco"),
    ("audio", "https://simple.ripley.cl/tecno/audio-y-musica/audifonos?s=mdco"),
    ("audio", "https://simple.ripley.cl/search/parlante?s=mdco"),
    ("audio", "https://simple.ripley.cl/search/tocadiscos?s=mdco"),
    ("componentes", "https://simple.ripley.cl/tecno/computacion/proyectores-y-monitores?s=mdco"),
    ("camaras", "https://simple.ripley.cl/search/camara?s=mdco"),
    ("cafe", "https://simple.ripley.cl/electro/electrodomesticos/cafeteras?s=mdco"),
]

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def fetch_json(url: str) -> Optional[dict[str, Any]]:
    """Fetch a Ripley.cl page directly and extract its embedded findabilityProps products."""
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        resp = get_http_session().get(url, headers=headers, timeout=DEFAULT_TIMEOUT + 4)
        if resp.status_code in (403, 429, 503):
            logger.warning("Ripley.cl returned HTTP %s for %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        match = _NEXT_DATA_RE.search(resp.text)
        if not match:
            return None
        parsed = json.loads(match.group(1))
        page_props = (parsed.get("props") or {}).get("pageProps") or {}
        products = (
            ((page_props.get("findabilityProps") or {}).get("data") or {}).get("products")
            or (page_props.get("catalog") or {}).get("products")
            or []
        )
        return {"products": products}
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed fetching Ripley.cl %s: %s", url, exc)
        return None


def _parse_ripley_item(item: dict[str, Any], category_name: str) -> Optional[ProductSnapshot]:
    raw_sku = str(
        item.get("parentProductID")
        or item.get("partNumber")
        or item.get("sku")
        or item.get("uniqueID")
        or ""
    ).strip()
    if not raw_sku:
        return None

    is_marketplace = bool(item.get("isMarketplaceProduct", False))
    seller = str(item.get("seller") or item.get("sellerName") or "Ripley").strip()
    is_official = (not is_marketplace) and seller.lower() in ("ripley", "ripley.cl")
    if not is_official:
        return None

    prices = item.get("prices") if isinstance(item.get("prices"), dict) else {}
    # Strictly use Todo Medio de Pago (priceNumber / offerPrice) and ignore ripleyPriceNumber / cardPrice
    current_price = parse_clp_price(
        item.get("priceNumber")
        or item.get("price")
        or prices.get("offerPrice")
        or prices.get("price")
        or prices.get("listPrice")
    )
    normal_price = parse_clp_price(
        item.get("masterPriceNumber")
        or item.get("oldPrice")
        or prices.get("listPrice")
    ) or current_price

    if not current_price:
        return None

    title = str(item.get("name") or "").strip()
    if not title:
        return None

    raw_url = str(item.get("url") or "").strip()
    if raw_url.startswith("http"):
        url = raw_url
    elif raw_url.startswith("/"):
        url = f"https://simple.ripley.cl{raw_url}"
    else:
        url = f"https://simple.ripley.cl/p/{raw_sku}"

    image_url = str(
        item.get("primaryImage")
        or item.get("fullImage")
        or item.get("thumbnailImage")
        or ""
    ).strip()
    if image_url.startswith("//"):
        image_url = f"https:{image_url}"

    in_stock = bool(item.get("inStock", not item.get("isOutOfStock", False)))

    return ProductSnapshot(
        sku=f"RIP-{raw_sku}",
        store="Ripley",
        title=title,
        brand=str(item.get("brand") or "Marca").strip(),
        category=category_name,
        current_price=current_price,
        card_price=None,
        normal_price=normal_price,
        seller_name="Ripley",
        is_official_seller=True,
        in_stock=in_stock,
        url=url,
        image_url=image_url,
    )


def fetch_ripley_products(pages_per_category: int = 1) -> list[ProductSnapshot]:
    """Fetch official 1P Ripley Chile products directly from simple.ripley.cl SSR (3-worker parallel)."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    def _fetch_one(task: tuple[str, str]) -> tuple[str, Optional[dict[str, Any]]]:
        cat_name, url = task
        return cat_name, fetch_json(url)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_fetch_one, t) for t in RIPLEY_DIRECT_URLS]
        for fut in futures:
            category_name, data = fut.result()
            if not isinstance(data, dict):
                continue

            for item in data.get("products") or []:
                snap = _parse_ripley_item(item, category_name)
                if snap is None or snap.sku in seen_skus:
                    continue
                seen_skus.add(snap.sku)
                snapshots.append(snap)

    if not snapshots:
        logger.info("Ripley direct SSR returned 0 items; falling back to SoloTodo 1P feed.")
        return fetch_solotodo_store_products(
            store_ids=[RIPLEY_1P_STORE_ID],
            store_label="Ripley",
            sku_prefix="RIP",
            seller_name="Ripley",
            page_size=40 * max(1, pages_per_category),
            use_offer_price=False,
        )

    return snapshots
