"""Deal detection engine with anti-inflation market shielding and unseen-product Black Friday detection."""

from __future__ import annotations

import unicodedata
from typing import Any, Optional, Union

from src.models import DealAlert, ProductSnapshot, utc_now_iso
from src.price_reference import MarketReference, detect_product_subtype

RECOGNIZED_TECH_BRANDS = {
    "apple",
    "samsung",
    "lg",
    "sony",
    "lenovo",
    "asus",
    "hp",
    "acer",
    "dell",
    "msi",
    "nintendo",
    "playstation",
    "microsoft",
    "xbox",
    "xiaomi",
    "redmi",
    "poco",
    "motorola",
    "honor",
    "huawei",
    "jbl",
    "bose",
    "sennheiser",
    "audio-technica",
    "marshall",
    "sonos",
    "shokz",
    "anker",
    "soundcore",
    "harman",
    "logitech",
    "razer",
    "hyperx",
    "corsair",
    "steelseries",
    "keychron",
    "amd",
    "nvidia",
    "intel",
    "gigabyte",
    "hisense",
    "tcl",
    "philips",
    "aoc",
    "benq",
    "viewsonic",
    "canon",
    "nikon",
    "fujifilm",
    "gopro",
    "insta360",
    "dji",
    "garmin",
    "meta",
    "valve",
    "google",
    "tp-link",
    "kingston",
    "wd",
    "seagate",
    "sandisk",
    "crucial",
    "nespresso",
    "delonghi",
    "oster",
    "moulinex",
    "breville",
    "krups",
    "thomas",
    "thorben",
    "osoji",
    "tineco",
    "fensa",
    "mademsa",
    "sindelen",
    "wurden",
    "somela",
    "electrolux",
    "midea",
    "ursus",
    "trotter",
    "stanley",
    "emma",
    "cic",
    "rosen",
    "casio",
    "dolce",
    "gabbana",
    "dior",
    "montblanc",
    "burberry",
    "azzaro",
    "versace",
    "carolina",
    "herrera",
    "rabanne",
    "arman",
    "armani",
    "givenchy",
    "ysl",
    "lacoste",
    "calvin",
    "klein",
    "hugo",
    "boss",
}


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _unpack_market_ref(
    market_reference: Optional[Union[MarketReference, tuple[int, str]]],
) -> tuple[int, str, int, bool]:
    if market_reference is None:
        return 0, "", 0, False
    if isinstance(market_reference, MarketReference):
        return (
            int(market_reference.price),
            str(market_reference.source),
            int(market_reference.sample_count),
            bool(market_reference.is_verified_history),
        )
    price, source = market_reference
    return int(price), str(source), 2, True


class DealDetector:
    """Evaluates product snapshots against spike-resistant history, market value, floors, and VIP rules."""

    def __init__(
        self,
        settings: dict[str, Any],
        watchlist: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self.settings = settings
        self.thresholds = settings["thresholds"]
        self.floors = settings.get("category_floor_prices_clp", {})
        self.watchlist = watchlist or []
        self.glitch_only_categories = {
            str(c).lower().strip() for c in settings.get("glitch_only_categories", [])
        }
        self.priority_categories = {
            str(c).lower().strip() for c in settings.get("priority_categories", [])
        }
        self.priority_keywords = [
            _normalize_text(str(k)) for k in settings.get("priority_keywords", []) if str(k).strip()
        ]

    def _is_priority_product(self, snapshot: ProductSnapshot) -> bool:
        if snapshot.category.lower().strip() in self.priority_categories:
            return True
        clean_title = _normalize_text(snapshot.title)
        return any(kw in clean_title for kw in self.priority_keywords)

    def _match_watchlist(self, snapshot: ProductSnapshot) -> Optional[dict[str, Any]]:
        clean_title = _normalize_text(snapshot.title)
        category = snapshot.category.lower().strip()
        subtype = detect_product_subtype(snapshot.title)
        non_console_tokens = (
            "control ", "dualsense", "joy-con", "joycon", "estacion ", "audifono",
            "pulse ", "portal", "reproductor remoto", "unidad de disco", "disco externo",
            "bateria", "volante", "racing wheel", "formato fisico", "medios fisicos",
            "juego ", "videojuego", "grand theft auto", "gta ", "final fantasy",
            "wolverine", "split fiction", "digimon", "spider-man", "zelda", "mario",
        )
        for item in self.watchlist:
            item_cat = item.get("category", "")
            if item_cat and category and item_cat != category:
                continue
            max_target = int(item.get("max_price_clp") or 0)
            if max_target > 0 and snapshot.effective_price < int(max_target * 0.35):
                continue
            # Do not match games, controllers, or peripherals against console VIP targets
            if item_cat == "consolas":
                if subtype != "console" or any(tok in clean_title for tok in non_console_tokens):
                    continue
            if snapshot.sku in item.get("skus", []):
                return item
            for kw in item.get("keywords", []):
                if _normalize_text(kw) in clean_title:
                    return item
        return None

    @staticmethod
    def _is_recognized_brand(brand: str) -> bool:
        clean = _normalize_text(brand).strip()
        if not clean:
            return False
        if clean in RECOGNIZED_TECH_BRANDS:
            return True
        return any(tok in RECOGNIZED_TECH_BRANDS for tok in clean.replace("-", " ").split())

    @staticmethod
    def _calc_drop_pct(reference_price: int | float, current_price: int) -> float:
        if reference_price <= 0 or current_price >= reference_price:
            return 0.0
        return round(((reference_price - current_price) / reference_price) * 100.0, 1)

    def _should_notify(
        self,
        previous_record: Optional[dict[str, Any]],
        effective_price: int,
    ) -> bool:
        if not previous_record:
            return True
        last_notified = previous_record.get("last_notified_price")
        if last_notified is None or int(last_notified) <= 0:
            return True
        extra_drop = float(self.thresholds.get("re_notify_extra_drop_pct", 15.0))
        cutoff = float(last_notified) * (1.0 - (extra_drop / 100.0))
        return float(effective_price) <= cutoff

    def evaluate(
        self,
        snapshot: ProductSnapshot,
        previous_record: Optional[dict[str, Any]],
        market_reference: Optional[Union[MarketReference, tuple[int, str]]] = None,
        median_historical_price: Optional[int] = None,
    ) -> Optional[DealAlert]:
        """Return a DealAlert if snapshot triggers verified GLITCH, VIP, FLASH, or DP rules."""
        effective = snapshot.effective_price
        if effective <= 0:
            return None

        normal_price = snapshot.normal_price or 0
        store_discount_pct = self._calc_drop_pct(normal_price, effective)
        category_key = snapshot.category.lower().strip()
        floor_price = self.floors.get(category_key)
        subtype = detect_product_subtype(snapshot.title)

        glitch_drop_pct = float(self.thresholds["glitch_drop_pct"])
        flash_drop_pct = float(self.thresholds["flash_drop_pct"])
        first_seen_glitch_pct = float(self.thresholds["first_seen_glitch_discount_pct"])
        min_glitch_profit_clp = int(self.thresholds.get("min_glitch_profit_clp", 45000))
        min_flash_savings_clp = int(self.thresholds.get("min_flash_savings_clp", 15000))
        min_dp_savings_clp = int(self.thresholds.get("min_dp_savings_clp", 12000))
        if category_key in ("cafe", "comida"):
            min_flash_savings_clp = min(min_flash_savings_clp, 8000)
            min_dp_savings_clp = min(min_dp_savings_clp, 7000)

        is_priority = self._is_priority_product(snapshot)
        is_known_brand = self._is_recognized_brand(snapshot.brand) or self._is_recognized_brand(snapshot.title)
        eff_flash_drop = (
            float(self.thresholds.get("priority_flash_drop_pct", flash_drop_pct))
            if is_priority
            else flash_drop_pct
        )
        eff_first_seen_flash = (
            float(self.thresholds.get("priority_first_seen_flash_discount_pct", 40.0))
            if is_priority
            else 40.0
        )

        # Unpack market reference (if available from local cross-store index or Knasta/SoloTodo)
        m_price, m_source, m_samples, m_verified = _unpack_market_ref(market_reference)
        market_drop_pct = self._calc_drop_pct(m_price, effective) if m_price > effective else 0.0
        # Trust an upward market reference to CREATE a deal only if verified (Knasta 90d / 2+ stores)
        # or within a plausible range vs store normal price (blocks 1-store $1.99M outlier)
        market_trusted_for_creation = (
            m_price > effective
            and (
                m_verified
                or m_samples >= 2
                or (normal_price > 0 and m_price <= normal_price * 2.8)
            )
        )

        alert_type: Optional[str] = None
        alert_origin = ""  # "history", "floor", "market", "store_discount", "vip", "dp"
        reason = ""

        # Floor eligibility: strict guards against games/cheap non-brand gadgets breaking category floors
        can_use_floor = (
            floor_price is not None
            and effective <= floor_price
            and subtype not in ("game", "mousepad", "glasses", "security_cam")
            and is_known_brand
            and normal_price >= int(floor_price * 2.5)
            and (normal_price - effective) >= min_glitch_profit_clp
        )

        is_inflated_spike = False
        if previous_record is not None:
            last_price = int(previous_record["last_price"])
            avg_price = float(previous_record["avg_price"])
            prev_min = int(previous_record["min_price"])
            max_hist = int(previous_record["max_price"])
            min_hist = min(prev_min, effective)

            # Spike-resistant historical reference:
            # Detects temporary inflation spikes like $79k -> $709k -> $79k (just dropped back from a spike
            # above prev_min, or median stays near prev_min), WITHOUT misclassifying a real active glitch
            # staying at its crash price ($899k -> $299k -> $299k, where last_price == prev_min and median is $899k).
            just_returned_from_spike = prev_min > 0 and last_price > prev_min * 2.2
            median_confirms_low = (
                median_historical_price is None
                or median_historical_price <= prev_min * 1.4
            )
            is_inflated_spike = (
                prev_min > 0
                and max_hist > prev_min * 2.2
                and effective >= prev_min * 0.85
                and (just_returned_from_spike or median_confirms_low)
            )
            if is_inflated_spike:
                reference_price = prev_min
                real_drop_pct = 0.0
            elif median_historical_price is not None and median_historical_price > 0 and int(previous_record.get("observations_count", 0)) >= 3:
                # With >=3 history points, median ignores one-off price spikes AND preserves real glitch drops
                reference_price = median_historical_price
                real_drop_pct = self._calc_drop_pct(reference_price, effective)
            else:
                reference_price = max(last_price, int(round(avg_price)))
                real_drop_pct = self._calc_drop_pct(reference_price, effective)
                # Only extend to max_hist if max_hist is NOT an artificial spike
                if prev_min > 0 and max_hist <= prev_min * 2.2:
                    drop_vs_max = self._calc_drop_pct(max_hist, effective)
                    if drop_vs_max > real_drop_pct:
                        reference_price = max_hist
                        real_drop_pct = drop_vs_max

            previous_real_price = reference_price if reference_price > effective else effective
            savings_clp = max(0, reference_price - effective)

            if (
                real_drop_pct >= glitch_drop_pct
                and savings_clp >= min_glitch_profit_clp
                and (is_known_brand or savings_clp >= 80000)
            ):
                alert_type = "GLITCH"
                alert_origin = "history"
                reason = (
                    f"Ganga / Error de precio: -{real_drop_pct:.1f}% frente al historial "
                    f"(margen ${savings_clp:,})".replace(",", ".")
                )
            elif can_use_floor and max(real_drop_pct, store_discount_pct) >= 60.0:
                alert_type = "GLITCH"
                alert_origin = "floor"
                best_ref = max(real_drop_pct, store_discount_pct)
                real_drop_pct = best_ref
                if normal_price > reference_price:
                    previous_real_price = normal_price
                    savings_clp = max(0, normal_price - effective)
                reason = (
                    f"Rompe piso de categoría (${floor_price:,}) con {best_ref:.1f}% de descuento"
                    .replace(",", ".")
                )
        else:
            previous_real_price = normal_price if normal_price > effective else effective
            min_hist = effective
            real_drop_pct = store_discount_pct
            savings_clp = max(0, previous_real_price - effective)

            if can_use_floor and store_discount_pct >= first_seen_glitch_pct:
                alert_type = "GLITCH"
                alert_origin = "floor"
                reason = (
                    f"Nuevo en radar: rompe piso de categoría (${floor_price:,}) "
                    f"con {store_discount_pct:.1f}% bajo precio normal".replace(",", ".")
                )

        # Cross-store market reference check for GLITCH (catches high-margin Black Friday glitches!)
        if (
            alert_type is None
            and market_trusted_for_creation
            and market_drop_pct >= glitch_drop_pct
            and (m_price - effective) >= min_glitch_profit_clp
            and (is_known_brand or (m_price - effective) >= 80000)
        ):
            alert_type = "GLITCH"
            alert_origin = "market"
            previous_real_price = m_price
            real_drop_pct = market_drop_pct
            savings_clp = m_price - effective
            reason = (
                f"Ganga vs mercado ({m_source}): -{market_drop_pct:.1f}% "
                f"(margen ${savings_clp:,} bajo ${m_price:,})".replace(",", ".")
            )

        # PROTECTIVE SHIELD #1: Validate any GLITCH against real market value!
        # Blocks inflated-price glitches like Monitor $79.990 (was $709.990) when market is $82.990
        if alert_type == "GLITCH" and alert_origin in ("history", "floor") and m_price > 0:
            if market_drop_pct < 30.0:
                if (
                    market_drop_pct >= eff_flash_drop
                    and category_key not in self.glitch_only_categories
                ):
                    alert_type = "FLASH"
                    alert_origin = "market"
                    previous_real_price = m_price
                    real_drop_pct = market_drop_pct
                    savings_clp = max(0, m_price - effective)
                    reason = (
                        f"Verificado vs mercado ({m_source}): -{market_drop_pct:.1f}% "
                        f"bajo referencia (${m_price:,})".replace(",", ".")
                    )
                else:
                    # Confirmed NOT a glitch and NOT a real market discount -> discard!
                    alert_type = None
                    alert_origin = ""
                    reason = ""

        # Category restricted strictly to verified GLITCH (e.g. TVs)
        if category_key in self.glitch_only_categories:
            if alert_type != "GLITCH":
                return None
            return DealAlert(
                snapshot=snapshot,
                alert_type=alert_type,
                previous_real_price=previous_real_price,
                min_historical_price=min_hist,
                real_drop_pct=real_drop_pct,
                store_discount_pct=store_discount_pct,
                savings_clp=savings_clp,
                reason=reason,
                detected_at=utc_now_iso(),
                should_notify=self._should_notify(previous_record, effective),
            )

        # Rule 2: VIP watchlist check
        vip_match = self._match_watchlist(snapshot)
        if alert_type is None and vip_match is not None and effective <= int(vip_match["max_price_clp"]):
            alert_type = "VIP"
            alert_origin = "vip"
            max_target = int(vip_match["max_price_clp"])
            if previous_real_price <= effective and normal_price > effective:
                previous_real_price = normal_price
                savings_clp = normal_price - effective
                real_drop_pct = store_discount_pct
            reason = (
                f"Lista VIP '{vip_match['name']}': bajo tu meta de "
                f"${max_target:,}".replace(",", ".")
            )

        # Rule 3: Flash deal check (requires real CLP savings margin and demand, not dead-stock penny drops)
        if alert_type is None:
            if (
                previous_record is not None
                and real_drop_pct >= eff_flash_drop
                and savings_clp >= min_flash_savings_clp
            ):
                alert_type = "FLASH"
                alert_origin = "history"
                prefix = "Descuento Flash prioritario" if is_priority else "Descuento Flash real"
                reason = (
                    f"{prefix}: -{real_drop_pct:.1f}% frente a su historial (ahorro ${savings_clp:,})".replace(",", ".")
                )
            elif (
                market_trusted_for_creation
                and market_drop_pct >= eff_flash_drop
                and (m_price - effective) >= min_flash_savings_clp
            ):
                alert_type = "FLASH"
                alert_origin = "market"
                previous_real_price = m_price
                real_drop_pct = market_drop_pct
                savings_clp = m_price - effective
                dp_note = (
                    f" + hasta {snapshot.dolares_premio_pct}% Dólares Premio"
                    if snapshot.dolares_premio_pct > 0
                    else ""
                )
                reason = (
                    f"Cotejo mercado ({m_source}): -{market_drop_pct:.1f}% "
                    f"bajo referencia (${m_price:,}){dp_note}".replace(",", ".")
                )
            elif (
                not is_inflated_spike
                and store_discount_pct >= eff_first_seen_flash
                and is_known_brand
                and max(0, normal_price - effective) >= min_flash_savings_clp
                and (
                    previous_record is None
                    or int(previous_record.get("observations_count", 0)) <= 1
                    or (
                        int(previous_record.get("min_price") or 0) > 0
                        and normal_price <= int(previous_record["min_price"]) * 2.0
                        and (
                            effective >= int(previous_record["min_price"]) * 0.98
                            or savings_clp >= min_flash_savings_clp
                        )
                    )
                )
            ):
                alert_type = "FLASH"
                alert_origin = "store_discount"
                if normal_price > effective and store_discount_pct > real_drop_pct:
                    real_drop_pct = store_discount_pct
                    previous_real_price = normal_price
                    savings_clp = normal_price - effective
                tag = f"Prioritario ({snapshot.brand})" if is_priority else f"Marca oficial ({snapshot.brand})"
                reason = (
                    f"{tag} con -{store_discount_pct:.1f}% (ahorro ${savings_clp:,})".replace(",", ".")
                )

        # PROTECTIVE SHIELD #2: Block fake Black Friday FLASH discounts if market price is same/lower!
        if alert_type == "FLASH" and alert_origin in ("history", "store_discount") and m_price > 0:
            if market_drop_pct < 12.0 or (m_price - effective) < min_flash_savings_clp:
                alert_type = None
                alert_origin = ""
                reason = ""

        # Rule 4: Tienda Travel Dólares Premio special redemption deals
        if alert_type is None and snapshot.dolares_premio_pct > 0:
            tt_cfg = self.settings.get("tienda_travel", {})
            min_dp_discount = float(tt_cfg.get("min_discount_pct_for_dp_alert", 20.0))
            min_high_canje = int(tt_cfg.get("min_high_canje_pct", 50))
            best_drop = max(real_drop_pct, store_discount_pct, market_drop_pct)
            potential_savings = max(
                savings_clp,
                max(0, normal_price - effective),
                max(0, m_price - effective) if market_trusted_for_creation else 0,
            )

            if potential_savings >= min_dp_savings_clp and (
                best_drop >= min_dp_discount
                or (snapshot.dolares_premio_pct >= min_high_canje and best_drop >= 14.0)
            ):
                alert_type = "DOLARES_PREMIO"
                alert_origin = "dp"
                if market_trusted_for_creation and market_drop_pct >= best_drop:
                    real_drop_pct = market_drop_pct
                    previous_real_price = m_price
                    savings_clp = m_price - effective
                elif best_drop > real_drop_pct and normal_price > effective:
                    real_drop_pct = best_drop
                    previous_real_price = normal_price
                    savings_clp = normal_price - effective
                reason = (
                    f"Canje Dólares Premio (hasta {snapshot.dolares_premio_pct}% DP: "
                    f"~{snapshot.dolares_premio_amount:.2f} DP + ${snapshot.copay_clp:,})".replace(",", ".")
                )

        if alert_type is None:
            return None

        return DealAlert(
            snapshot=snapshot,
            alert_type=alert_type,
            previous_real_price=previous_real_price,
            min_historical_price=min_hist,
            real_drop_pct=real_drop_pct,
            store_discount_pct=store_discount_pct,
            savings_clp=savings_clp,
            reason=reason,
            detected_at=utc_now_iso(),
            should_notify=self._should_notify(previous_record, effective),
        )
