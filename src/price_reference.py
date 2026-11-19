"""Cross-store price reference engine for evaluating unseen/new products against Chilean market prices."""

from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price
from src.scrapers.knasta import fetch_knasta_page
from src.storage import PriceStorage

logger = logging.getLogger(__name__)

SOLOTODO_PRODUCTS_URL = "https://publicapi.solotodo.com/products/"

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "con", "sin", "para", "por", "en", "y", "o",
    "un", "una", "negro", "negra", "blanco", "blanca", "azul", "gris", "plata", "silver",
    "black", "white", "blue", "gray", "grey", "rojo", "verde", "dorado", "rosado", "celeste",
    "nuevo", "nueva", "original", "oficial", "chile", "edicion", "edition", "modelo",
    "unidad", "unidades", "pack", "set", "combo", "gratis", "despacho", "envio",
    "smartphone", "celular", "telefono", "notebook", "laptop", "audifonos", "audifono",
    "auriculares", "headset", "parlante", "monitor", "cafetera", "perfume", "hombre",
    "mujer", "unisex", "edt", "edp", "smart", "tv", "gaming", "gamer", "wireless",
    "bluetooth", "inalambrico", "inalambrica", "portatil", "ps4", "ps5", "xbox", "pc",
    "usb", "hdmi", "wifi", "4k", "uhd", "fhd", "hd", "ips", "va", "led", "ai",
    "magic", "remote", "funciones", "ia", "cancelacion", "ruido", "ram", "ssd",
}

_UNIT_TOKEN_RE = re.compile(
    r"^(\d+)(gb|tb|mb|ml|gr|g|kg|w|hz|mah|pulg|p|mm|cm|l|oz)$",
    re.IGNORECASE,
)
_UNIT_SPACED_RE = re.compile(
    r"\b(\d+)\s+(gb|tb|mb|ml|gr|g|kg|w|hz|mah|pulgadas?|pulg)\b",
    re.IGNORECASE,
)
_INCH_RE = re.compile(r"\b(\d{2,3})\s*(?:\"|''|”|pulgadas?|pulg)\b", re.IGNORECASE)
_COMMON_SCREEN_SIZES = {
    "13", "14", "15", "16", "17", "22", "24", "25", "27", "29", "32", "34",
    "40", "43", "48", "49", "50", "55", "58", "60", "65", "70", "75", "77",
    "83", "85", "86", "98", "100",
}
_MODEL_CODE_RE = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9\-]{3,}$", re.IGNORECASE)

_TIER_TOKENS = {
    "oled", "qled", "qned", "nanocell", "nano", "miniled", "pro", "max", "ultra",
    "plus", "fe", "lite", "slim", "air", "mini", "evo", "studio",
}


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    clean = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    clean = _UNIT_SPACED_RE.sub(r"\1\2", clean)
    return re.sub(r"[^a-z0-9\s\-\"”']", " ", clean)


def _extract_specs(norm_text: str) -> dict[str, set[str]]:
    """Extract capacity, volume, screen inches, and product tier tokens to prevent mismatches."""
    specs: dict[str, set[str]] = {
        "storage": set(),
        "volume": set(),
        "inches": set(),
        "tier": set(),
    }
    for m in _INCH_RE.finditer(norm_text):
        specs["inches"].add(m.group(1))
    for tok in norm_text.split():
        cleaned = tok.strip(".,:;()[]{}\"'”")
        if cleaned in _COMMON_SCREEN_SIZES:
            specs["inches"].add(cleaned)
        um = _UNIT_TOKEN_RE.match(cleaned)
        if um:
            val, unit = um.group(1), um.group(2).lower()
            if unit in ("gb", "tb"):
                specs["storage"].add(f"{val}{unit}")
            elif unit in ("ml", "oz", "l"):
                specs["volume"].add(f"{val}{unit}")
            elif unit in ("p", "pulg"):
                specs["inches"].add(val)
        if cleaned in _TIER_TOKENS:
            specs["tier"].add(cleaned)
    return specs


def extract_title_signature(
    title: str,
    brand: str = "",
) -> tuple[set[str], set[str], str, dict[str, set[str]]]:
    """Return (significant_tokens, model_codes, compact_query_key, specs)."""
    norm = _normalize(title)
    specs = _extract_specs(norm)
    plain = re.sub(r"[\"'”]", " ", norm)
    raw_tokens = [t.strip("-") for t in plain.split() if t.strip("-")]
    sig_tokens: set[str] = set()
    model_codes: set[str] = set()
    query_parts: list[str] = []

    brand_norm = _normalize(brand).strip()
    if brand_norm and brand_norm not in ("marca", "tienda travel", "generico"):
        query_parts.append(brand_norm)

    for tok in raw_tokens:
        if len(tok) < 2 or tok in _STOPWORDS:
            continue
        if _UNIT_TOKEN_RE.match(tok):
            sig_tokens.add(tok)
            continue
        if _MODEL_CODE_RE.match(tok):
            compact_code = tok.replace("-", "")
            model_codes.add(compact_code)
            sig_tokens.add(compact_code)
            if compact_code not in query_parts:
                query_parts.append(tok)
        else:
            sig_tokens.add(tok)
            if tok not in query_parts and len(query_parts) < 5:
                query_parts.append(tok)

    for code in sorted(model_codes):
        if code not in _normalize(" ".join(query_parts)):
            query_parts.append(code)

    query_str = " ".join(query_parts[:5]).strip()
    return sig_tokens, model_codes, query_str, specs


def _specs_conflict(specs_a: dict[str, set[str]], specs_b: dict[str, set[str]]) -> bool:
    for key in ("storage", "volume", "inches"):
        if specs_a[key] and specs_b[key] and not (specs_a[key] & specs_b[key]):
            return True
    # Tier mismatch (e.g. OLED vs NanoCell/QNED, Pro vs Lite)
    if specs_a["tier"] != specs_b["tier"] and (specs_a["tier"] or specs_b["tier"]):
        # Allow if one is simply missing tier only when explicit model codes already match
        return True
    return False


def _titles_match(
    sig_a: tuple[set[str], set[str], str, dict[str, set[str]]],
    brand_a: str,
    title_b: str,
    brand_b: str = "",
) -> bool:
    tokens_a, codes_a, _, specs_a = sig_a
    tokens_b, codes_b, _, specs_b = extract_title_signature(title_b, brand_b)

    clean_ba = _normalize(brand_a).strip()
    clean_bb = _normalize(brand_b).strip()
    generic_brands = ("", "marca", "tienda travel", "generico")
    if (
        clean_ba not in generic_brands
        and clean_bb not in generic_brands
        and clean_ba != clean_bb
    ):
        return False

    # Reject mismatched capacities, screen inches, or panel/product tiers
    if _specs_conflict(specs_a, specs_b):
        return False

    # Check shared explicit alphanumeric model code (e.g. WH1000XM5, EC685, G24I, ATLP60X)
    shared_codes = codes_a & codes_b
    if shared_codes and len(tokens_a & tokens_b) >= 2:
        return True

    # If both have explicit model codes and none overlap, they are different models
    if codes_a and codes_b and not shared_codes:
        return False

    # Without an explicit shared model code, require a known matching brand and strong token overlap
    if clean_ba in generic_brands and clean_bb in generic_brands:
        return False

    union = tokens_a | tokens_b
    if not union:
        return False
    inter = tokens_a & tokens_b
    jaccard = len(inter) / len(union)
    return len(inter) >= 4 and jaccard >= 0.6


class CrossStoreReferenceEngine:
    """Resolves market reference prices across Chilean stores for unseen products."""

    def __init__(
        self,
        storage: PriceStorage,
        live_snapshots: list[ProductSnapshot],
        max_live_lookups: int = 25,
        cache_ttl_hours: int = 24,
    ) -> None:
        self.storage = storage
        self.max_live_lookups = max_live_lookups
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.live_lookups_used = 0
        self._index: list[dict[str, Any]] = []
        self._build_local_index(live_snapshots)

    def _build_local_index(self, live_snapshots: list[ProductSnapshot]) -> None:
        seen_skus: set[str] = set()
        for snap in live_snapshots:
            if snap.effective_price <= 0 or snap.sku in seen_skus:
                continue
            seen_skus.add(snap.sku)
            sig = extract_title_signature(snap.title, snap.brand)
            self._index.append(
                {
                    "sku": snap.sku,
                    "store": snap.store,
                    "category": snap.category.lower().strip(),
                    "title": snap.title,
                    "brand": snap.brand,
                    "price": snap.effective_price,
                    "sig": sig,
                }
            )

        for row in self.storage.get_all_products_brief():
            sku = str(row["sku"])
            if sku in seen_skus or int(row["last_price"]) <= 0:
                continue
            seen_skus.add(sku)
            title = str(row["title"])
            brand = str(row["brand"])
            self._index.append(
                {
                    "sku": sku,
                    "store": str(row["store"]),
                    "category": str(row.get("category") or "").lower().strip(),
                    "title": title,
                    "brand": brand,
                    "price": int(row["last_price"]),
                    "sig": extract_title_signature(title, brand),
                }
            )

    def _find_local_match(self, snapshot: ProductSnapshot) -> Optional[tuple[int, str]]:
        sig = extract_title_signature(snapshot.title, snapshot.brand)
        cat = snapshot.category.lower().strip()
        matched_prices: list[int] = []
        sample_label = ""
        for entry in self._index:
            if entry["store"] == snapshot.store:
                continue
            if cat and entry["category"] and entry["category"] != cat:
                continue
            if _titles_match(sig, snapshot.brand, entry["title"], entry["brand"]):
                matched_prices.append(int(entry["price"]))
                if not sample_label:
                    sample_label = f"{entry['store']} ({entry['title'][:40]})"
        if not matched_prices:
            return None
        ref = int(round(statistics.median(matched_prices)))
        return ref, sample_label

    def _query_external_reference(
        self,
        snapshot: ProductSnapshot,
        query: str,
    ) -> Optional[tuple[int, str, str]]:
        """Query Knasta (all categories) and SoloTodo (tech) for market reference price."""
        sig = extract_title_signature(snapshot.title, snapshot.brand)
        candidate_prices: list[int] = []
        matched_title = ""
        source_name = ""

        # 1. Try Knasta first (covers tech, appliances, perfumes, coffee, turntables)
        kn_data = fetch_knasta_page({"q": query, "order": "popular"})
        if isinstance(kn_data, dict):
            for item in (kn_data.get("products") or [])[:12]:
                title = str(item.get("title") or "")
                brand = str(item.get("brand") or "")
                if not _titles_match(sig, snapshot.brand, title, brand):
                    continue
                price_inet = parse_clp_price(item.get("price_internet"))
                price_card = parse_clp_price(item.get("price_card"))
                cur = parse_clp_price(item.get("current_price"))
                hab = parse_clp_price((item.get("descriptive") or {}).get("habitual_price"))
                ref = price_inet or (cur if (price_card is None or cur != price_card) else None) or hab
                if ref and ref > 1000:
                    candidate_prices.append(ref)
                    if not matched_title:
                        matched_title = title
                        source_name = f"Knasta ({item.get('retail_label') or item.get('retail')})"

        # 2. If tech category and no Knasta match, try SoloTodo search
        if not candidate_prices and snapshot.category in (
            "notebooks", "celulares", "consolas", "tv", "audio", "componentes", "camaras", "cafe"
        ):
            st_data = fetch_json(
                SOLOTODO_PRODUCTS_URL,
                params={"search": query, "page_size": 3},
                timeout=12,
            )
            if isinstance(st_data, dict):
                for prod in st_data.get("results") or []:
                    p_name = str(prod.get("name") or "")
                    if not _titles_match(sig, snapshot.brand, p_name, ""):
                        continue
                    ents = fetch_json(
                        f"{SOLOTODO_PRODUCTS_URL}{prod['id']}/entities/",
                        timeout=12,
                    )
                    if isinstance(ents, list):
                        for ent in ents:
                            reg = ent.get("active_registry") or {}
                            if not reg.get("is_available"):
                                continue
                            norm = parse_clp_price(reg.get("normal_price"))
                            if norm and norm > 1000:
                                candidate_prices.append(norm)
                        if candidate_prices:
                            matched_title = p_name
                            source_name = "SoloTodo"
                            break

        if not candidate_prices:
            return None
        return int(round(statistics.median(candidate_prices))), source_name, matched_title

    def get_reference_price(
        self,
        snapshot: ProductSnapshot,
        is_unseen: bool,
    ) -> Optional[tuple[int, str]]:
        """Return (market_reference_price, source_description) if available."""
        local = self._find_local_match(snapshot)
        if local is not None:
            return local

        if not is_unseen:
            return None

        _, _, query, _ = extract_title_signature(snapshot.title, snapshot.brand)
        if not query or len(query) < 4:
            return None

        cached = self.storage.get_reference_price(query)
        if cached is not None:
            try:
                updated = datetime.fromisoformat(cached["updated_at"])
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - updated < self.cache_ttl:
                    ref_val = int(cached["ref_price"])
                    if ref_val > 0:
                        return ref_val, f"{cached['source']} ({cached['matched_title'][:35]})"
                    return None
            except ValueError:
                pass

        if self.live_lookups_used >= self.max_live_lookups:
            return None

        if snapshot.store != "Tienda Travel" and snapshot.category not in (
            "notebooks", "audio", "componentes", "camaras", "cafe"
        ):
            return None

        self.live_lookups_used += 1
        ext = self._query_external_reference(snapshot, query)
        if ext is not None:
            ref_price, source_name, matched_title = ext
            self.storage.upsert_reference_price(query, ref_price, source_name, matched_title)
            return ref_price, f"{source_name} ({matched_title[:35]})"

        self.storage.upsert_reference_price(query, 0, "NONE", "")
        return None
