"""Paris.cl 1P official direct SSR catalog connector (with automatic cloud fallback)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import requests

from src.models import ProductSnapshot
from src.scrapers.http_client import DEFAULT_HEADERS, DEFAULT_TIMEOUT, parse_clp_price
from src.scrapers.solotodo_client import fetch_solotodo_store_products

logger = logging.getLogger(__name__)

PARIS_1P_STORE_ID = 11

PARIS_DIRECT_URLS = [
    ("notebooks", "https://www.paris.cl/tecnologia/computadores/notebooks/"),
    ("celulares", "https://www.paris.cl/tecnologia/celulares/smartphones/"),
    ("consolas", "https://www.paris.cl/tecnologia/consolas-videojuegos/"),
    ("tv", "https://www.paris.cl/electro/television/"),
    ("audio", "https://www.paris.cl/electro/audio/"),
    ("audio", "https://www.paris.cl/search/?q=parlante"),
    ("audio", "https://www.paris.cl/search/?q=tocadiscos"),
    ("componentes", "https://www.paris.cl/search/?q=monitor"),
    ("camaras", "https://www.paris.cl/tecnologia/fotografia/"),
    ("cafe", "https://www.paris.cl/search/?q=cafetera"),
]

_NEXT_FLIGHT_RE = re.compile(
    r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>',
    re.DOTALL,
)


def extract_paris_ssr_products(html: str) -> list[dict[str, Any]]:
    """Extract the embedded productData.products JSON array from Paris.cl Next.js SSR HTML."""
    chunks = _NEXT_FLIGHT_RE.findall(html)
    stream = "".join(chunks) if chunks else html
    stream = stream.replace('\\"', '"').replace("\\\\", "\\")

    idx = stream.find('"products":[{')
    if idx < 0:
        return []
    arr_start = stream.find("[", idx)
    if arr_start < 0:
        return []

    depth = 0
    in_string = False
    escape = False
    limit = min(len(stream), arr_start + 600000)

    for i in range(arr_start, limit):
        ch = stream[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stream[arr_start : i + 1])
                except ValueError:
                    return []
    return []


def fetch_json(url: str) -> Optional[dict[str, Any]]:
    """Fetch a Paris.cl category/search page directly and extract its live SSR products."""
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    try:
        resp = requests.get(url, headers=headers, timeout=DEFAULT_TIMEOUT + 6)
        if resp.status_code in (403, 429, 503):
            logger.warning("Paris.cl returned HTTP %s for %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        products = extract_paris_ssr_products(resp.text)
        return {"results": products}
    except requests.RequestException as exc:
        logger.warning("Failed fetching Paris.cl %s: %s", url, exc)
        return None


def _parse_paris_item(item: dict[str, Any], category_name: str) -> Optional[ProductSnapshot]:
    master = item.get("masterVariant") if isinstance(item.get("masterVariant"), dict) else {}
    raw_sku = str(
        item.get("key")
        or item.get("id")
        or item.get("sku")
        or master.get("sku")
        or ""
    ).strip()
    if not raw_sku:
        return None

    sellers = item.get("sellers")
    if isinstance(sellers, list) and sellers:
        seller_name = str(sellers[0]).strip()
    else:
        seller_name = str(item.get("sellerName") or item.get("seller") or "Paris").strip()

    is_marketplace_flag = item.get("isMarketplace") is True
    seller_low = seller_name.lower()
    is_official = (not is_marketplace_flag) and (
        seller_low in ("paris", "paris.cl", "cencosud")
        or "tienda oficial" in seller_low
        or not raw_sku.upper().startswith("MK")
    )
    if not is_official:
        return None

    # Support both live SSR masterVariant.prices and flat test dicts
    prices_obj = master.get("prices") if isinstance(master.get("prices"), dict) else (
        item.get("prices") if isinstance(item.get("prices"), dict) else item
    )

    if "regular" in prices_obj or "offer" in prices_obj:
        reg_val = ((prices_obj.get("regular") or {}).get("value") or {}).get("centAmount")
        off_val = ((prices_obj.get("offer") or {}).get("value") or {}).get("centAmount")
        normal_price = parse_clp_price(reg_val)
        current_price = parse_clp_price(off_val) or normal_price
    else:
        # Flat fallback structure (explicitly ignoring cardPrice / cencosudPrice)
        current_price = parse_clp_price(
            prices_obj.get("offerPrice")
            or prices_obj.get("internetPrice")
            or prices_obj.get("price")
            or prices_obj.get("normalPrice")
        )
        normal_price = parse_clp_price(
            prices_obj.get("normalPrice") or prices_obj.get("listPrice")
        ) or current_price

    if not current_price:
        return None

    title = str(item.get("name") or item.get("title") or "").strip()
    if not title:
        return None

    slug = str(item.get("slug") or item.get("url") or "").strip()
    if slug.startswith("http"):
        url = slug
    elif slug:
        clean_slug = slug.lstrip("/")
        url = (
            f"https://www.paris.cl/{clean_slug}"
            if clean_slug.endswith(".html")
            else f"https://www.paris.cl/{clean_slug}.html"
        )
    else:
        url = f"https://www.paris.cl/{raw_sku}.html"

    images = master.get("images") or item.get("images") or []
    image_url = ""
    if isinstance(images, list) and images:
        first_img = images[0]
        image_url = str(first_img.get("url") if isinstance(first_img, dict) else first_img or "")
    if not image_url:
        image_url = str(item.get("image") or item.get("imageUrl") or "")

    return ProductSnapshot(
        sku=f"PAR-{raw_sku}",
        store="Paris",
        title=title,
        brand=str(item.get("brand") or "Marca").strip(),
        category=category_name,
        current_price=current_price,
        card_price=None,
        normal_price=normal_price or current_price,
        seller_name=seller_name or "Paris",
        is_official_seller=True,
        in_stock=bool(item.get("inStock", not item.get("isOutOfStock", False))),
        url=url,
        image_url=image_url,
    )


def fetch_paris_products(limit_per_category: int = 40) -> list[ProductSnapshot]:
    """Fetch official 1P Paris.cl products directly from Paris.cl SSR (with cloud fallback)."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    for category_name, url in PARIS_DIRECT_URLS:
        data = fetch_json(url)
        if not isinstance(data, dict):
            continue

        for item in (data.get("results") or [])[:limit_per_category]:
            snap = _parse_paris_item(item, category_name)
            if snap is None or snap.sku in seen_skus:
                continue
            seen_skus.add(snap.sku)
            snapshots.append(snap)

    if not snapshots:
        logger.info("Paris direct SSR returned 0 items; falling back to SoloTodo 1P feed.")
        return fetch_solotodo_store_products(
            store_ids=[PARIS_1P_STORE_ID],
            store_label="Paris",
            sku_prefix="PAR",
            seller_name="Paris",
            page_size=limit_per_category,
            use_offer_price=False,
        )

    return snapshots
