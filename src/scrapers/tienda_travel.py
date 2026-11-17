"""Tienda Travel (Banco de Chile) connector with Dólares Premio support."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from src.models import ProductSnapshot
from src.scrapers.http_client import parse_clp_price

logger = logging.getLogger(__name__)

TIENDA_TRAVEL_ASSEMBLER_URL = (
    "https://tienda.travel.cl/ccstore/v1/assembler/pages/Default/osf/catalog/_/"
)
TIENDA_TRAVEL_PRODUCTS_URL = "https://tienda.travel.cl/ccstore/v1/products"
TIENDA_TRAVEL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}


def fetch_json(
    url: str,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: int = 25,
) -> Optional[dict[str, Any]]:
    """Fetch JSON from Tienda Travel using minimal headers to avoid WAF tarpits."""
    try:
        resp = requests.get(
            url,
            params=params,
            headers=headers or TIENDA_TRAVEL_HEADERS,
            timeout=timeout,
        )
        if resp.status_code in (403, 429, 503):
            logger.warning("Tienda Travel returned HTTP %s for %s", resp.status_code, url)
            return None
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Failed fetching Tienda Travel %s: %s", url, exc)
        return None

# Exact dimension IDs mapped for the 5 requested areas:
# Tecnología (smartphones, tv, audio, gamer), Electrodomésticos, Ropa Hombre, Comida, Perfumes Hombre
CATEGORY_DIMENSIONS = [
    ("celulares", "3842720512"),
    ("tv", "2549298533"),
    ("audio", "1985651586"),
    ("consolas", "4132472718"),
    ("electrodomesticos", "2138023256"),
    ("ropa_hombre", "2253563421"),
    ("comida", "3493713426"),
    ("perfumes_hombre", "2572226493"),
]

# Priority keyword searches with Dólares Premio active
KEYWORD_QUERIES = [
    ("notebooks", "notebook"),
    ("cafe", "cafetera"),
    ("cafe", "cafe"),
    ("audio", "tocadiscos"),
    ("audio", "parlante"),
    ("camaras", "camara"),
    ("componentes", "monitor"),
]

DEFAULT_DP_CLP_RATE = 992.5


def _first_attr(attrs: dict[str, Any], key: str, default: str = "") -> str:
    val = attrs.get(key)
    if isinstance(val, list) and val:
        return str(val[0]).strip()
    if val is not None:
        return str(val).strip()
    return default


def _parse_occ_price(attrs: dict[str, Any], key: str) -> int | None:
    raw = _first_attr(attrs, key, "")
    if not raw:
        return None
    try:
        num = int(round(float(raw)))
        return num if num > 0 else None
    except ValueError:
        return parse_clp_price(raw)


def _fetch_batch_metadata(product_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch exact x_tasaDeCanje and brand in batches of 25 SKUs."""
    meta: dict[str, dict[str, Any]] = {}
    chunk_size = 25
    for idx in range(0, len(product_ids), chunk_size):
        chunk = product_ids[idx : idx + chunk_size]
        data = fetch_json(
            TIENDA_TRAVEL_PRODUCTS_URL,
            params={
                "productIds": ",".join(chunk),
                "fields": "id,x_tasaDeCanje,brand",
            },
            headers=TIENDA_TRAVEL_HEADERS,
            timeout=18,
        )
        if not isinstance(data, dict):
            continue
        for item in data.get("items") or []:
            pid = str(item.get("id") or "").strip()
            if pid:
                meta[pid] = item
    return meta


def fetch_tienda_travel_products(
    limit_per_category: int = 40,
    dp_clp_rate: float = DEFAULT_DP_CLP_RATE,
) -> list[ProductSnapshot]:
    """Fetch in-stock Tienda Travel products eligible for Dólares Premio redemption."""
    raw_entries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen_ids: set[str] = set()

    for category_name, dimension_id in CATEGORY_DIMENSIONS:
        data = fetch_json(
            TIENDA_TRAVEL_ASSEMBLER_URL,
            params={
                "N": dimension_id,
                "Nf": "product.x_tasaDeCanje|GT 0",
                "Nr": "AND(sku.availabilityStatus:INSTOCK,OR(product.x_accessFromTienda:agentAndStore,product.x_accessFromTienda:store))",
                "Nrpp": limit_per_category,
                "No": 0,
                "dimensionId": dimension_id,
            },
            headers=TIENDA_TRAVEL_HEADERS,
            timeout=18,
        )
        if not isinstance(data, dict):
            continue

        records = ((data.get("results") or {}).get("records")) or []
        for rec in records:
            parent_attrs = rec.get("attributes") or {}
            child_list = rec.get("records") or []
            child_attrs = (child_list[0].get("attributes") or {}) if child_list else {}

            prod_id = _first_attr(parent_attrs, "product.repositoryId")
            if not prod_id or prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)
            raw_entries.append((category_name, parent_attrs, child_attrs))

    for category_name, keyword in KEYWORD_QUERIES:
        data = fetch_json(
            TIENDA_TRAVEL_ASSEMBLER_URL,
            params={
                "Ntt": keyword,
                "Nf": "product.x_tasaDeCanje|GT 0",
                "Nr": "AND(sku.availabilityStatus:INSTOCK,OR(product.x_accessFromTienda:agentAndStore,product.x_accessFromTienda:store))",
                "Nrpp": min(limit_per_category, 25),
                "No": 0,
            },
            headers=TIENDA_TRAVEL_HEADERS,
            timeout=25,
        )
        if not isinstance(data, dict):
            continue

        records = ((data.get("results") or {}).get("records")) or []
        for rec in records:
            parent_attrs = rec.get("attributes") or {}
            child_list = rec.get("records") or []
            child_attrs = (child_list[0].get("attributes") or {}) if child_list else {}

            prod_id = _first_attr(parent_attrs, "product.repositoryId")
            if not prod_id or prod_id in seen_ids:
                continue
            seen_ids.add(prod_id)
            raw_entries.append((category_name, parent_attrs, child_attrs))

    if not raw_entries:
        return []

    batch_meta = _fetch_batch_metadata([entry[1]["product.repositoryId"][0] for entry in raw_entries if isinstance(entry[1].get("product.repositoryId"), list)])
    snapshots: list[ProductSnapshot] = []

    for category_name, parent_attrs, child_attrs in raw_entries:
        prod_id = _first_attr(parent_attrs, "product.repositoryId")
        title = _first_attr(parent_attrs, "product.displayName")
        route = _first_attr(parent_attrs, "product.route")
        img_rel = _first_attr(parent_attrs, "product.primaryFullImageURL")

        list_price = _parse_occ_price(child_attrs, "sku.listPrice")
        sale_price = _parse_occ_price(child_attrs, "sku.salePrice")
        active_price = _parse_occ_price(child_attrs, "sku.activePrice") or _parse_occ_price(
            parent_attrs, "sku.minActivePrice"
        )

        current_price = sale_price or active_price or list_price
        if not current_price or not title:
            continue
        normal_price = list_price or current_price

        prod_meta = batch_meta.get(prod_id, {})
        brand = str(prod_meta.get("brand") or "Tienda Travel").strip()
        try:
            dp_pct = int(float(prod_meta.get("x_tasaDeCanje") or 1))
        except (TypeError, ValueError):
            dp_pct = 1

        dp_clp_portion = round(current_price * (dp_pct / 100.0)) if dp_pct > 0 else 0
        dp_amount = round(dp_clp_portion / dp_clp_rate, 2) if dp_pct > 0 else 0.0
        copay_clp = max(0, current_price - dp_clp_portion) if dp_pct > 0 else current_price

        url = f"https://tienda.travel.cl{route}" if route.startswith("/") else route
        if not url:
            url = f"https://tienda.travel.cl/product/{prod_id}"
        image_url = (
            f"https://tienda.travel.cl{img_rel}"
            if img_rel.startswith("/")
            else img_rel
        )

        snapshots.append(
            ProductSnapshot(
                sku=f"TT-{prod_id}",
                store="Tienda Travel",
                title=title,
                brand=brand,
                category=category_name,
                current_price=current_price,
                card_price=None,
                normal_price=normal_price,
                seller_name="Tienda Travel (Banco de Chile)",
                is_official_seller=True,
                in_stock=True,
                url=url,
                image_url=image_url,
                dolares_premio_pct=dp_pct,
                dolares_premio_amount=dp_amount,
                copay_clp=copay_clp,
            )
        )

    return snapshots
