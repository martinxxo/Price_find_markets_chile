"""Black Friday Chile specialized official 1P store connector (Tech, Gamer, Home, Appliances, Coffee, Food)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from typing import Any

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price
from src.scrapers.solotodo_client import infer_brand_from_title

logger = logging.getLogger(__name__)

SOLOTODO_ENTITIES_URL = "https://publicapi.solotodo.com/entities/"

# Official 1P Black Friday Chile participating stores in SoloTodo (excludes all 3P marketplaces):
# Electronics, Gamer, Home, Appliances, Supermarkets & Specialty Brands
BLACKFRIDAY_STORES: dict[int, tuple[str, str, bool]] = {
    # store_id: (store_label, sku_prefix, allow_offer_price_as_todo_medio)
    12: ("PC Factory", "PCF", True),
    86: ("SP Digital", "SPD", True),
    45: ("Winpy", "WPY", True),
    14: ("Wei", "WEI", True),
    755: ("Central Gamer", "CGM", True),
    23: ("MacOnline", "MAC", True),
    47: ("Sony Store", "SNY", True),
    223: ("Samsung Shop", "SAM", True),
    4781: ("LG Store", "LGS", True),
    3032: ("LG Store", "LGO", True),
    199: ("Lenovo Chile", "LEN", True),
    27: ("HP Online", "HPO", True),
    2570: ("ASUS Store", "ASU", True),
    3: ("Acer Store", "ACR", True),
    281: ("Huawei Shop", "HUA", True),
    6794: ("Xiaomi Online", "XIA", True),
    8411: ("Xiaomi Chile", "XIC", True),
    201: ("Casa Royal", "CRY", True),
    4616: ("Tienda Fensa", "FEN", True),
    # Retail / Supermarkets / Home Improvement (use normal_price to exclude store cards like Lider/CMR/Abcvisa)
    43: ("Lider", "LID", False),
    3956: ("Lider Supermercado", "LIDS", False),
    170: ("Tottus", "TOT", False),
    88: ("Easy", "EASY", False),
    67: ("Sodimac", "SOD", False),
    30: ("AbcDin", "ABCD", False),
    8015: ("ABC", "ABC", False),
    87: ("Hites", "HIT", False),
    5: ("La Polar", "LAP", False),
}

BLACKFRIDAY_CATEGORIES: list[tuple[str, int, int]] = [
    # (internal_category, solotodo_category_id, page_size)
    ("notebooks", 1, 60),
    ("celulares", 6, 60),
    ("consolas", 33, 50),
    ("componentes", 4, 60),        # Monitores
    ("audio", 50, 60),             # Audífonos y Headsets
    ("audio", 25, 60),             # Equipos de audio y parlantes
    ("componentes", 41, 50),       # Teclados
    ("componentes", 40, 50),       # Mouse
    ("componentes", 42, 40),       # Combos Teclado Mouse
    ("componentes", 2, 50),        # Tarjetas de video
    ("notebooks", 14, 50),         # Tablets
    ("cafe", 219, 60),             # Cafeteras
    ("electrodomesticos", 256, 50),# Air fryers
    ("electrodomesticos", 18, 50), # Aspiradoras
    ("electrodomesticos", 15, 40), # Refrigeradores
    ("electrodomesticos", 17, 40), # Hornos y Microondas
    ("electrodomesticos", 19, 40), # Lavadoras y Secadoras
]


def _fetch_category_batch(
    category_name: str,
    category_id: int,
    page_size: int,
) -> tuple[str, list[dict[str, Any]]]:
    params: list[tuple[str, Any]] = [("stores", sid) for sid in BLACKFRIDAY_STORES]
    params.extend(
        [
            ("categories", category_id),
            ("is_visible", "true"),
            ("is_available", "true"),
            ("ordering", "-last_pricing_update"),
            ("page_size", page_size),
        ]
    )
    data = fetch_json(SOLOTODO_ENTITIES_URL, params=params, timeout=16)
    if not isinstance(data, dict):
        return category_name, []
    return category_name, data.get("results") or []


def fetch_blackfriday_stores_products() -> list[ProductSnapshot]:
    """Concurrently fetch products across 25+ Black Friday Chile official stores."""
    snapshots: list[ProductSnapshot] = []
    seen_skus: set[str] = set()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(_fetch_category_batch, cat_name, cat_id, page_size)
            for cat_name, cat_id, page_size in BLACKFRIDAY_CATEGORIES
        ]
        for fut in as_completed(futures):
            category_name, items = fut.result()
            for item in items:
                condition = str(item.get("condition") or "")
                if condition and "NewCondition" not in condition:
                    continue

                store_id = int(item.get("store_id") or item.get("store") or 0)
                store_cfg = BLACKFRIDAY_STORES.get(store_id)
                if store_cfg is None:
                    continue
                store_label, sku_prefix, allow_offer = store_cfg

                raw_sku = str(item.get("sku") or item.get("id") or "").strip()
                if not raw_sku:
                    continue
                sku = f"{sku_prefix}-{raw_sku}"
                if sku in seen_skus:
                    continue

                registry = item.get("active_registry") or {}
                if not registry.get("is_available", True):
                    continue

                normal_price = parse_clp_price(registry.get("normal_price"))
                offer_price = parse_clp_price(registry.get("offer_price"))
                if allow_offer and offer_price:
                    current_price = min(offer_price, normal_price) if normal_price else offer_price
                else:
                    current_price = normal_price or offer_price
                if not current_price:
                    continue

                title = str(item.get("name") or "").strip()
                if not title:
                    continue

                seen_skus.add(sku)
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
                        card_price=None,
                        normal_price=normal_price or current_price,
                        seller_name=store_label,
                        is_official_seller=True,
                        in_stock=True,
                        url=str(item.get("external_url") or ""),
                        image_url=image_url,
                    )
                )

    return snapshots
