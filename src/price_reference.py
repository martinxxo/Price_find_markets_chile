"""Cross-store market price validation & reference engine (pre-Black Friday hardened)."""

from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.models import ProductSnapshot
from src.scrapers.http_client import fetch_json, parse_clp_price
from src.storage import PriceStorage

logger = logging.getLogger(__name__)

SOLOTODO_PRODUCTS_URL = "https://publicapi.solotodo.com/products/"

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "con", "sin", "para", "por", "en", "y", "o",
    "un", "una", "negro", "negra", "blanco", "blanca", "azul", "gris", "plata", "silver",
    "black", "white", "blue", "gray", "grey", "rojo", "verde", "dorado", "rosado", "celeste",
    "nuevo", "nueva", "original", "oficial", "chile", "edicion", "edition", "modelo",
    "unidad", "unidades", "pack", "set", "combo", "gratis", "despacho", "envio", "latam",
    "smartphone", "celular", "telefono", "notebook", "laptop", "audifonos", "audifono",
    "auriculares", "headset", "parlante", "monitor", "cafetera", "perfume", "hombre",
    "mujer", "unisex", "smart", "tv", "gaming", "gamer", "wireless",
    "bluetooth", "inalambrico", "inalambrica", "portatil", "ps4", "ps5", "xbox", "pc",
    "usb", "hdmi", "wifi", "4k", "uhd", "fhd", "hd", "ips", "va", "led", "ai",
    "magic", "remote", "funciones", "ia", "cancelacion", "ruido", "ram", "ssd",
    "compacta", "digital", "autofocus", "zoom", "exterior", "interior", "color",
    "luces", "recargable", "doble", "lente",
}

_UNIT_TOKEN_RE = re.compile(
    r"^(\d+)(gb|tb|mb|ml|gr|g|kg|w|hz|mah|pulg|p|mm|cm|l|oz|mp|x)$",
    re.IGNORECASE,
)
_UNIT_SPACED_RE = re.compile(
    r"\b(\d+)\s+(gb|tb|mb|ml|gr|g|kg|w|hz|mah|pulgadas?|pulg|mp)\b",
    re.IGNORECASE,
)
_INCH_RE = re.compile(r"\b(\d{2,3})\s*(?:\"|''|”|pulgadas?|pulg)\b", re.IGNORECASE)
_COMMON_SCREEN_SIZES = {
    "13", "14", "15", "16", "17", "22", "24", "25", "27", "29", "32", "34",
    "40", "43", "48", "49", "50", "55", "58", "60", "65", "70", "75", "77",
    "83", "85", "86", "98", "100",
}

# Technical spec tokens that look alphanumeric but are NOT unique product model codes
_GENERIC_SPEC_CODES = {
    "2k", "4k", "8k", "5g", "4g", "3d", "6e", "v2", "v3", "w10", "w11", "win11", "win10",
    "1080p", "720p", "1440p", "2160p", "60fps", "120fps", "ddr4", "ddr5", "gddr6", "gddr7",
    "wifi5", "wifi6", "wifi6e", "wifi7", "ip65", "ip66", "ip67", "ip68", "ipx4", "ipx5", "ipx7",
    "rj45", "cat6", "usb2", "usb3", "usbc", "typec", "gen2", "gen3", "gen4",
    "i3", "i5", "i7", "i9", "r3", "r5", "r7", "r9", "bt5", "bt53", "bt54",
    "ns2", "ps4", "ps5", "h1", "h2", "1ms", "05ms", "2ms", "5ms",
    "2x", "3x", "4x", "5x", "8x", "10x", "12x", "16x", "20x", "30x", "40x", "50x",
    "2mp", "3mp", "4mp", "5mp", "8mp", "12mp", "16mp", "24mp", "32mp", "44mp", "48mp", "50mp", "64mp", "108mp", "200mp",
}

_MODEL_CODE_RE = re.compile(r"^(?=.*[a-z])(?=.*\d)[a-z0-9\-]{2,}$", re.IGNORECASE)

_TIER_TOKENS = {
    "oled", "qled", "qned", "nanocell", "nano", "miniled", "sqd", "pro", "max", "ultra",
    "plus", "fe", "lite", "slim", "air", "mini", "evo", "studio", "deluxe",
    "edt", "edp", "parfum", "elixir", "intense", "cook", "chroma", "haste",
}

_GAME_PATTERNS = re.compile(
    r"\b(?:juego|videojuego|mario kart|god of war|last of us|ea sports fc|fifa|zelda|"
    r"super mario|call of duty|cyberpunk|resident evil|elden ring|pokemon|splatoon|"
    r"spider-man 2|mortal kombat|gran turismo|fc 25|fc 26)\b",
    re.IGNORECASE,
)
_CONSOLE_PATTERNS = re.compile(
    r"\b(?:consola|playstation 5|ps5 slim|ps5 pro|nintendo switch|xbox series|steam deck|rog ally)\b",
    re.IGNORECASE,
)
_SECURITY_CAM_PATTERNS = re.compile(
    r"\b(?:camara ip|seguridad|domo|ptz|vigilancia|ezviz|dahua|hikvision|imou|cctv|hdcvi|tapo)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class MarketReference:
    """Verified cross-store market reference price for a product."""

    price: int
    source: str
    sample_count: int = 1
    is_verified_history: bool = False

    def __getitem__(self, index: int) -> Any:
        # Backward compatibility for callers accessing ref[0] / ref[1]
        if index == 0:
            return self.price
        if index == 1:
            return self.source
        raise IndexError(index)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    clean = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    clean = _UNIT_SPACED_RE.sub(r"\1\2", clean)
    return re.sub(r"[^a-z0-9\s\-\"”']", " ", clean)


def detect_product_subtype(title: str) -> str:
    """Distinguish sub-types (e.g. game vs console, security cam vs photo cam, glasses vs cam)."""
    norm = _normalize(title)
    if any(k in norm for k in ("gafas", "lentes")):
        return "glasses"
    if any(k in norm for k in ("mouse pad", "mousepad", "alfombrilla")):
        return "mousepad"
    if _SECURITY_CAM_PATTERNS.search(norm):
        return "security_cam"
    has_console = bool(_CONSOLE_PATTERNS.search(norm))
    has_game = bool(_GAME_PATTERNS.search(norm))
    if has_game and "consola" not in norm and "+" not in title:
        return "game"
    if has_console:
        return "console"
    return ""


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
) -> tuple[set[str], set[str], str, dict[str, set[str]], str]:
    """Return (significant_tokens, model_codes, compact_query_key, specs, subtype)."""
    norm = _normalize(title)
    specs = _extract_specs(norm)
    subtype = detect_product_subtype(title)
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
        compact_code = tok.replace("-", "")
        if _MODEL_CODE_RE.match(tok) and compact_code not in _GENERIC_SPEC_CODES:
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
    return sig_tokens, model_codes, query_str, specs, subtype


def _specs_conflict(specs_a: dict[str, set[str]], specs_b: dict[str, set[str]]) -> bool:
    for key in ("storage", "volume", "inches"):
        if specs_a[key] and specs_b[key] and not (specs_a[key] & specs_b[key]):
            return True
    if specs_a["tier"] != specs_b["tier"] and (specs_a["tier"] or specs_b["tier"]):
        return True
    return False


def _titles_match(
    sig_a: tuple[set[str], set[str], str, dict[str, set[str]], str],
    brand_a: str,
    title_b: str,
    brand_b: str = "",
) -> bool:
    tokens_a, codes_a, _, specs_a, subtype_a = sig_a
    tokens_b, codes_b, _, specs_b, subtype_b = extract_title_signature(title_b, brand_b)

    # Never cross-match incompatible subtypes (e.g. game vs console, glasses vs camera)
    if subtype_a != subtype_b and (subtype_a or subtype_b):
        return False

    clean_ba = _normalize(brand_a).strip()
    clean_bb = _normalize(brand_b).strip()
    generic_brands = ("", "marca", "tienda travel", "generico")
    if clean_ba not in generic_brands and clean_bb not in generic_brands:
        if clean_ba != clean_bb:
            return False
    elif clean_ba not in generic_brands:
        if clean_ba not in tokens_b:
            return False
    elif clean_bb not in generic_brands:
        if clean_bb not in tokens_a:
            return False
    else:
        return False

    if _specs_conflict(specs_a, specs_b):
        return False

    # Model code consistency: e.g. C7L != X11, 24N3Q != 27N3Q, S24 != S25
    shared_codes = codes_a & codes_b
    if codes_a and codes_b and not shared_codes:
        return False

    if shared_codes and len(tokens_a & tokens_b) >= 2:
        return True

    # If one has an explicit specific model code (len >= 4) missing in the other, be cautious
    long_codes_a = {c for c in codes_a if len(c) >= 4}
    long_codes_b = {c for c in codes_b if len(c) >= 4}
    if (long_codes_a and not shared_codes and len(tokens_a & tokens_b) < 4) or (
        long_codes_b and not shared_codes and len(tokens_a & tokens_b) < 4
    ):
        return False

    union = tokens_a | tokens_b
    if not union:
        return False
    inter = tokens_a & tokens_b
    jaccard = len(inter) / len(union)
    return len(inter) >= 4 and jaccard >= 0.6


def compute_robust_market_price(prices: list[int]) -> tuple[int, int]:
    """Compute outlier-resistant market reference price (strips inflated spikes like $709k vs $89k)."""
    valid = sorted(p for p in prices if p > 1000)
    if not valid:
        return 0, 0
    if len(valid) == 1:
        return valid[0], 1

    # Discard inflated outliers (> 2.0x the lowest competitive market price)
    floor_ref = valid[0]
    cleaned = [p for p in valid if p <= floor_ref * 2.0]
    if not cleaned:
        cleaned = valid

    if len(cleaned) <= 2:
        return cleaned[0], len(cleaned)
    return int(round(statistics.median(cleaned))), len(cleaned)


class CrossStoreReferenceEngine:
    """Resolves verified market reference prices across Chilean stores and Knasta/SoloTodo."""

    def __init__(
        self,
        storage: PriceStorage,
        live_snapshots: list[ProductSnapshot],
        max_live_lookups: int = 60,
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
            last_p = int(row["last_price"])
            if sku in seen_skus or last_p <= 0:
                continue
            seen_skus.add(sku)
            title = str(row["title"])
            brand = str(row["brand"])
            # Use the lower of last_price and normal_price to avoid feeding spiked prices into the index
            min_p = int(row.get("min_price") or last_p)
            safe_price = last_p if last_p <= min_p * 2.0 else min_p
            self._index.append(
                {
                    "sku": sku,
                    "store": str(row["store"]),
                    "category": str(row.get("category") or "").lower().strip(),
                    "title": title,
                    "brand": brand,
                    "price": safe_price,
                    "sig": extract_title_signature(title, brand),
                }
            )

    def _find_local_match(self, snapshot: ProductSnapshot) -> Optional[MarketReference]:
        sig = extract_title_signature(snapshot.title, snapshot.brand)
        cat = snapshot.category.lower().strip()
        other_store_prices: list[int] = []
        same_store_alt_prices: list[int] = []
        sample_label = ""

        for entry in self._index:
            if entry["sku"] == snapshot.sku:
                continue
            if cat and entry["category"] and entry["category"] != cat:
                continue
            if _titles_match(sig, snapshot.brand, entry["title"], entry["brand"]):
                if entry["store"] != snapshot.store:
                    other_store_prices.append(int(entry["price"]))
                    if not sample_label:
                        sample_label = f"{entry['store']} ({entry['title'][:36]})"
                else:
                    same_store_alt_prices.append(int(entry["price"]))
                    if not sample_label:
                        sample_label = f"{entry['store']} ({entry['title'][:36]})"

        all_prices = other_store_prices + same_store_alt_prices
        if not all_prices:
            return None

        # Cross-store prices are trusted to both shield AND create deals;
        # same-store alternate SKUs are only reliable as a protective shield if cheaper than snapshot
        if other_store_prices:
            prices_for_ref = other_store_prices + [
                p for p in same_store_alt_prices if p <= snapshot.effective_price * 1.2
            ]
            ref_price, count = compute_robust_market_price(prices_for_ref)
            is_verified = count >= 2
        else:
            # Same-store only: use solely as a protective shield (never create a glitch from a same-store higher SKU)
            cheaper_same = [p for p in same_store_alt_prices if p <= snapshot.effective_price * 1.25]
            if not cheaper_same:
                return None
            ref_price, count = compute_robust_market_price(cheaper_same)
            is_verified = False

        if ref_price <= 0:
            return None

        if count >= 2:
            label = f"{count} tiendas ({sample_label})"
        else:
            label = sample_label

        return MarketReference(
            price=ref_price,
            source=label,
            sample_count=count,
            is_verified_history=is_verified,
        )

    def _query_external_reference(
        self,
        snapshot: ProductSnapshot,
        query: str,
    ) -> Optional[tuple[int, str, str, int, bool]]:
        """Query Knasta (90-day habitual_price + multi-retail) and SoloTodo for market value."""
        from src.scrapers.knasta import fetch_knasta_page

        sig = extract_title_signature(snapshot.title, snapshot.brand)
        habitual_prices: list[int] = []
        current_market_prices: list[int] = []
        matched_title = ""
        source_name = ""

        # 1. Knasta first: provides 90-day habitual_price (immune to pre-Black Friday inflation!)
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
                desc = item.get("descriptive") or {}
                hab = parse_clp_price(desc.get("habitual_price"))
                n_hist = int(desc.get("n_history") or 0)

                if hab and hab > 1000 and n_hist >= 7:
                    habitual_prices.append(hab)
                ref_cur = price_inet or (cur if (price_card is None or cur != price_card) else None)
                if ref_cur and ref_cur > 1000:
                    current_market_prices.append(ref_cur)
                if not matched_title and (hab or ref_cur):
                    matched_title = title
                    retail_lbl = item.get("retail_label") or item.get("retail") or "Chile"
                    source_name = f"Knasta 90d ({retail_lbl})" if (hab and n_hist >= 7) else f"Knasta ({retail_lbl})"

        # 2. SoloTodo for tech/appliances when Knasta had no 90d verified history
        if not habitual_prices and snapshot.category in (
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
                        st_prices: list[int] = []
                        for ent in ents:
                            reg = ent.get("active_registry") or {}
                            if not reg.get("is_available"):
                                continue
                            norm = parse_clp_price(reg.get("normal_price"))
                            if norm and norm > 1000:
                                st_prices.append(norm)
                        if st_prices:
                            current_market_prices.extend(st_prices)
                            if not matched_title:
                                matched_title = p_name
                                source_name = f"SoloTodo ({len(st_prices)} tiendas)"
                            break

        if habitual_prices:
            ref_price, count = compute_robust_market_price(habitual_prices)
            return ref_price, source_name or "Knasta 90d", matched_title, count, True

        if current_market_prices:
            ref_price, count = compute_robust_market_price(current_market_prices)
            return ref_price, source_name or "Mercado CL", matched_title, count, (count >= 2)

        return None

    def _get_cached_reference(self, query: str) -> tuple[bool, Optional[MarketReference]]:
        """Return (cache_hit, MarketReference_or_None)."""
        cached = self.storage.get_reference_price(query)
        if cached is None:
            return False, None
        try:
            updated = datetime.fromisoformat(cached["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated < self.cache_ttl:
                ref_val = int(cached["ref_price"])
                if ref_val > 0:
                    return True, MarketReference(
                        price=ref_val,
                        source=f"{cached['source']} ({cached['matched_title'][:35]})",
                        sample_count=int(cached.get("sample_count") or 1),
                        is_verified_history=bool(cached.get("is_verified_history")),
                    )
                return True, None
        except ValueError:
            pass
        return False, None

    def prefetch_references(
        self,
        candidates: list[tuple[ProductSnapshot, bool, bool]],
    ) -> None:
        """
        Concurrently fetch & cache external market references for high-priority items:
        candidates is a list of (snapshot, is_unseen, needs_glitch_validation).
        """
        # Prioritize glitch validations first (prevent false positives), then unseen Black Friday items
        ordered = sorted(
            candidates,
            key=lambda c: (
                0 if c[2] else 1,  # needs_glitch_validation first
                0 if c[0].store == "Tienda Travel" else 1,
                0 if c[0].category in ("notebooks", "audio", "componentes", "camaras", "cafe") else 1,
            ),
        )

        to_query: list[tuple[ProductSnapshot, str]] = []
        seen_queries: set[str] = set()

        for snap, is_unseen, needs_validation in ordered:
            if len(to_query) >= max(0, self.max_live_lookups - self.live_lookups_used):
                break
            if not needs_validation and not is_unseen:
                continue

            local = self._find_local_match(snap)
            if local is not None and (local.is_verified_history or not needs_validation):
                continue

            _, _, query, _, _ = extract_title_signature(snap.title, snap.brand)
            if not query or len(query) < 4 or query in seen_queries:
                continue

            hit, _ = self._get_cached_reference(query)
            if hit:
                continue

            seen_queries.add(query)
            to_query.append((snap, query))

        if not to_query:
            return

        logger.info("Prefetching %d market references concurrently (Knasta/SoloTodo)...", len(to_query))
        results: list[tuple[str, Optional[tuple[int, str, str, int, bool]]]] = []

        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {
                pool.submit(self._query_external_reference, snap, query): query
                for snap, query in to_query
            }
            for future in as_completed(future_map):
                query = future_map[future]
                try:
                    res = future.result()
                except Exception as exc:  # pragma: no cover
                    logger.warning("Reference prefetch failed for '%s': %s", query, exc)
                    res = None
                results.append((query, res))

        self.live_lookups_used += len(to_query)
        for query, ext in results:
            if ext is not None:
                ref_price, source_name, matched_title, sample_count, verified = ext
                self.storage.upsert_reference_price(
                    query,
                    ref_price,
                    source_name,
                    matched_title,
                    sample_count=sample_count,
                    is_verified_history=verified,
                )
            else:
                self.storage.upsert_reference_price(query, 0, "NONE", "", sample_count=0, is_verified_history=False)

    def get_reference_price(
        self,
        snapshot: ProductSnapshot,
        is_unseen: bool,
        allow_live_lookup: bool = True,
    ) -> Optional[MarketReference]:
        """Return verified MarketReference from cache, local cross-store index, or live lookup."""
        _, _, query, _, _ = extract_title_signature(snapshot.title, snapshot.brand)

        # 1. Prefer verified 90-day Knasta / SoloTodo cache if already available
        cached_ref: Optional[MarketReference] = None
        cache_hit = False
        if query and len(query) >= 4:
            cache_hit, cached_ref = self._get_cached_reference(query)
            if cached_ref is not None and cached_ref.is_verified_history:
                return cached_ref

        # 2. Fast local cross-store match across current scan & SQLite catalog
        local = self._find_local_match(snapshot)
        if local is not None and cached_ref is not None:
            # Combine conservatively: take the lower reference to avoid inflated outliers
            if local.price <= cached_ref.price:
                return local
            return cached_ref
        if local is not None:
            return local
        if cached_ref is not None:
            return cached_ref

        if not allow_live_lookup or not is_unseen or cache_hit:
            return None
        if not query or len(query) < 4:
            return None
        if self.live_lookups_used >= self.max_live_lookups:
            return None

        if snapshot.store != "Tienda Travel" and snapshot.category not in (
            "notebooks", "audio", "componentes", "camaras", "cafe"
        ):
            return None

        self.live_lookups_used += 1
        ext = self._query_external_reference(snapshot, query)
        if ext is not None:
            ref_price, source_name, matched_title, sample_count, verified = ext
            self.storage.upsert_reference_price(
                query,
                ref_price,
                source_name,
                matched_title,
                sample_count=sample_count,
                is_verified_history=verified,
            )
            return MarketReference(
                price=ref_price,
                source=f"{source_name} ({matched_title[:35]})",
                sample_count=sample_count,
                is_verified_history=verified,
            )

        self.storage.upsert_reference_price(query, 0, "NONE", "", sample_count=0, is_verified_history=False)
        return None
