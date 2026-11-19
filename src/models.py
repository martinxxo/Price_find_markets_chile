"""Data models for ChileDeals Radar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ProductSnapshot:
    """Normalized representation of a product listing at a point in time."""

    sku: str
    store: str
    title: str
    brand: str
    category: str
    current_price: int
    card_price: Optional[int]
    normal_price: Optional[int]
    seller_name: str
    is_official_seller: bool
    in_stock: bool
    url: str
    image_url: str = ""
    timestamp: str = field(default_factory=utc_now_iso)
    dolares_premio_pct: int = 0
    dolares_premio_amount: float = 0.0
    copay_clp: int = 0

    @property
    def effective_price(self) -> int:
        """Return the lowest Todo Medio de Pago purchase price (store card prices excluded)."""
        return int(self.current_price)

    @property
    def payment_type(self) -> str:
        """Return payment method label (store card offers are excluded)."""
        return "Todo Medio de Pago"


@dataclass(slots=True)
class DealAlert:
    """Detected price glitch, flash deal, or VIP watchlist match."""

    snapshot: ProductSnapshot
    alert_type: str  # "GLITCH", "FLASH", "VIP"
    previous_real_price: int
    min_historical_price: int
    real_drop_pct: float
    store_discount_pct: float
    savings_clp: int
    reason: str
    detected_at: str = field(default_factory=utc_now_iso)
    should_notify: bool = True
