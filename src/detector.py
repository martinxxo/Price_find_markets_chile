"""Deal detection engine for price glitches, flash sales, and VIP watchlist alerts."""

from __future__ import annotations

import unicodedata
from typing import Any, Optional

from src.models import DealAlert, ProductSnapshot, utc_now_iso

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
    "logitech",
    "razer",
    "hyperx",
    "corsair",
    "amd",
    "nvidia",
    "intel",
    "gigabyte",
    "hisense",
    "tcl",
    "philips",
    "canon",
    "nikon",
    "gopro",
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
}


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


class DealDetector:
    """Evaluates product snapshots against price history, category floors, and VIP rules."""

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
        for item in self.watchlist:
            item_cat = item.get("category", "")
            if item_cat and category and item_cat != category:
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
        return bool(clean and clean in RECOGNIZED_TECH_BRANDS)

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
        extra_drop = float(self.thresholds.get("re_notify_extra_drop_pct", 5.0))
        cutoff = float(last_notified) * (1.0 - (extra_drop / 100.0))
        return float(effective_price) <= cutoff

    def evaluate(
        self,
        snapshot: ProductSnapshot,
        previous_record: Optional[dict[str, Any]],
        market_reference: Optional[tuple[int, str]] = None,
    ) -> Optional[DealAlert]:
        """Return a DealAlert if snapshot triggers GLITCH, VIP, FLASH, or cross-store deals."""
        effective = snapshot.effective_price
        if effective <= 0:
            return None

        normal_price = snapshot.normal_price or 0
        store_discount_pct = self._calc_drop_pct(normal_price, effective)
        floor_price = self.floors.get(snapshot.category.lower().strip())

        glitch_drop_pct = float(self.thresholds["glitch_drop_pct"])
        flash_drop_pct = float(self.thresholds["flash_drop_pct"])
        first_seen_glitch_pct = float(self.thresholds["first_seen_glitch_discount_pct"])

        alert_type: Optional[str] = None
        reason = ""

        if previous_record is not None:
            last_price = int(previous_record["last_price"])
            avg_price = float(previous_record["avg_price"])
            min_hist = min(int(previous_record["min_price"]), effective)
            max_hist = int(previous_record["max_price"])
            reference_price = max(last_price, int(round(avg_price)))
            real_drop_pct = self._calc_drop_pct(reference_price, effective)
            drop_vs_max = self._calc_drop_pct(max_hist, effective)
            if drop_vs_max > real_drop_pct:
                reference_price = max_hist
                real_drop_pct = drop_vs_max
            previous_real_price = (
                last_price
                if last_price > effective
                else (max_hist if max_hist > effective else int(round(avg_price)))
            )
            savings_clp = max(0, reference_price - effective)

            if real_drop_pct >= glitch_drop_pct:
                alert_type = "GLITCH"
                reason = (
                    f"Caída brusca real de {real_drop_pct:.1f}% frente al historial "
                    f"(${reference_price:,})".replace(",", ".")
                )
            elif (
                floor_price is not None
                and effective <= floor_price
                and max(real_drop_pct, store_discount_pct) >= 60.0
            ):
                alert_type = "GLITCH"
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

            if (
                floor_price is not None
                and effective <= floor_price
                and store_discount_pct >= first_seen_glitch_pct
            ):
                alert_type = "GLITCH"
                reason = (
                    f"Nuevo en radar: rompe piso de categoría (${floor_price:,}) "
                    f"con {store_discount_pct:.1f}% bajo precio normal".replace(",", ".")
                )

        # Cross-store market reference check for GLITCH (catches unseen/unmarked glitches)
        market_drop_pct = 0.0
        market_ref_price = 0
        market_ref_source = ""
        if market_reference is not None:
            market_ref_price, market_ref_source = market_reference
            if market_ref_price > effective:
                market_drop_pct = self._calc_drop_pct(market_ref_price, effective)
                if alert_type is None and market_drop_pct >= glitch_drop_pct:
                    alert_type = "GLITCH"
                    previous_real_price = market_ref_price
                    real_drop_pct = market_drop_pct
                    savings_clp = market_ref_price - effective
                    reason = (
                        f"Cotejo mercado ({market_ref_source}): -{market_drop_pct:.1f}% "
                        f"bajo referencia (${market_ref_price:,})".replace(",", ".")
                    )

        # Rule 2: VIP watchlist check (if not already a GLITCH and not glitch-only category)
        category_key = snapshot.category.lower().strip()
        if category_key in self.glitch_only_categories:
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

        vip_match = self._match_watchlist(snapshot)
        if alert_type is None and vip_match is not None and effective <= int(vip_match["max_price_clp"]):
            alert_type = "VIP"
            max_target = int(vip_match["max_price_clp"])
            if previous_real_price <= effective and normal_price > effective:
                previous_real_price = normal_price
                savings_clp = normal_price - effective
                real_drop_pct = store_discount_pct
            reason = (
                f"Lista VIP '{vip_match['name']}': bajo tu meta de "
                f"${max_target:,}".replace(",", ".")
            )

        # Rule 3: Flash deal check (higher sensitivity for priority categories/keywords)
        is_priority = self._is_priority_product(snapshot)
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

        if alert_type is None:
            if previous_record is not None and real_drop_pct >= eff_flash_drop:
                alert_type = "FLASH"
                prefix = "Oferta Flash prioritaria" if is_priority else "Oferta Flash real"
                reason = (
                    f"{prefix}: bajó {real_drop_pct:.1f}% frente a su precio histórico"
                )
            elif (
                store_discount_pct >= eff_first_seen_flash
                and (self._is_recognized_brand(snapshot.brand) or is_priority)
            ):
                alert_type = "FLASH"
                if normal_price > effective and store_discount_pct > real_drop_pct:
                    real_drop_pct = store_discount_pct
                    previous_real_price = normal_price
                    savings_clp = normal_price - effective
                tag = f"Prioritario ({snapshot.brand})" if is_priority else f"Marca oficial ({snapshot.brand})"
                reason = (
                    f"{tag} con {store_discount_pct:.1f}% bajo precio normal"
                )
            elif market_ref_price > effective and market_drop_pct >= eff_flash_drop:
                alert_type = "FLASH"
                previous_real_price = market_ref_price
                real_drop_pct = market_drop_pct
                savings_clp = market_ref_price - effective
                dp_note = (
                    f" + hasta {snapshot.dolares_premio_pct}% Dólares Premio"
                    if snapshot.dolares_premio_pct > 0
                    else ""
                )
                reason = (
                    f"Cotejo mercado ({market_ref_source}): -{market_drop_pct:.1f}% "
                    f"bajo referencia (${market_ref_price:,}){dp_note}".replace(",", ".")
                )

        # Rule 4: Tienda Travel Dólares Premio special redemption deals
        if alert_type is None and snapshot.dolares_premio_pct > 0:
            tt_cfg = self.settings.get("tienda_travel", {})
            min_dp_discount = float(tt_cfg.get("min_discount_pct_for_dp_alert", 20.0))
            min_high_canje = int(tt_cfg.get("min_high_canje_pct", 50))
            best_drop = max(real_drop_pct, store_discount_pct)

            if best_drop >= min_dp_discount or (
                snapshot.dolares_premio_pct >= min_high_canje and best_drop >= 10.0
            ):
                alert_type = "DOLARES_PREMIO"
                if best_drop > real_drop_pct and normal_price > effective:
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
