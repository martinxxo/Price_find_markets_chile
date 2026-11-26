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
    "bateria para",
    "baterias para",
    "pantalla tactil para",
    "pantalla para",
    "pantalla lcd para",
    "display para",
    "teclado para notebook",
    "teclado de repuesto",
    "bisagra",
    "bisagras",
    "ventilador para notebook",
    "tapa trasera",
    "control remoto",
    "aguja",
    "agujas",
    "mouse pad",
    "mousepad",
    "alfombrilla",
    "camara de seguridad",
    "camara ip",
    "camara espia",
    "gafas de camara",
    "ptz",
    "dahua",
    "hikvision",
    "ezviz",
    "imou",
    "hdcvi",
    "cctv",
    "parlante ninos",
    "parlante infantil",
    "solo con cmr",
    "exclusivo cmr",
    "solo tarjeta",
    "exclusivo tarjeta",
    "con tarjeta cencosud",
    "con tarjeta ripley",
)

_BLOCKED_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in BLOCKED_KEYWORDS) + r")\b",
    flags=re.IGNORECASE,
)


_MAIN_DEVICE_PREFIX_RE = re.compile(
    r"^(?:notebook|macbook|laptop|smartphone|celular|iphone|tablet|ipad|consola|camara|monitor|smart\s+tv)\b",
    flags=re.IGNORECASE,
)
_BUNDLE_ACCESSORY_RE = re.compile(
    r"\+\s*(?:[^+]*\b)?(?:funda|carcasa|mica|lamina|cable|adaptador|cargador|soporte|estuche|bolso|mochila|correa|mouse\s*pad|mousepad)\b",
    flags=re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def contains_blocked_keyword(title: str) -> bool:
    """Return True if the product title indicates an accessory or used/refurbished item."""
    if not title:
        return True
    clean_title = _strip_accents(title).lower().strip()
    # If the product is clearly a main device with a '+ accessory' bundle (e.g. 'Notebook Lenovo + Bolso'),
    # strip the trailing bundle accessory clause before checking blocked accessory keywords.
    if _MAIN_DEVICE_PREFIX_RE.match(clean_title) and "+" in clean_title:
        clean_title = _BUNDLE_ACCESSORY_RE.sub("", clean_title)
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
