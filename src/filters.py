"""Strict quality and official-seller filters to eliminate noise and accessories."""

from __future__ import annotations

import re
import unicodedata

from src.models import ProductSnapshot

BLOCKED_KEYWORDS = (
    "funda",
    "fundas",
    "carcasa",
    "carcasas",
    "mica",
    "micas",
    "vidrio templado",
    "lamina",
    "laminas",
    "protector de pantalla",
    "cable",
    "cables",
    "adaptador",
    "adaptadores",
    "cargador",
    "cargadores",
    "soporte",
    "soportes",
    "estuche",
    "estuches",
    "bolso",
    "bolsos",
    "mochila",
    "correa",
    "correas",
    "reacondicionado",
    "reacondicionada",
    "reacondicionados",
    "refurbished",
    "usado",
    "usada",
    "usados",
    "semi nuevo",
    "seminuevo",
    "open box",
    "openbox",
    "repuesto",
    "repuestos",
    "control remoto",
)

_BLOCKED_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in BLOCKED_KEYWORDS) + r")\b",
    flags=re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def contains_blocked_keyword(title: str) -> bool:
    """Return True if the product title indicates an accessory or used/refurbished item."""
    if not title:
        return True
    clean_title = _strip_accents(title).lower()
    return bool(_BLOCKED_PATTERN.search(clean_title))


def is_valid_official_tech_product(
    snapshot: ProductSnapshot,
    official_seller_only: bool = True,
) -> bool:
    """Filter out out-of-stock items, third-party sellers, and accessories/refurbished goods."""
    if not snapshot.in_stock:
        return False
    if snapshot.effective_price <= 1000:
        return False
    if official_seller_only and not snapshot.is_official_seller:
        return False
    if contains_blocked_keyword(snapshot.title):
        return False
    return True
