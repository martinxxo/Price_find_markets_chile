"""Configuration loader and validator for settings and VIP watchlist."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent


def load_settings(settings_path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate config/settings.json."""
    path = Path(settings_path) if settings_path else DEFAULT_BASE_DIR / "config" / "settings.json"
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    required_keys = ["thresholds", "category_floor_prices_clp", "stores_enabled"]
    for key in required_keys:
        if key not in data:
            raise ValueError(f"Missing required settings key: '{key}'")

    thresholds = data["thresholds"]
    for t_key in (
        "flash_drop_pct",
        "glitch_drop_pct",
        "first_seen_glitch_discount_pct",
        "re_notify_extra_drop_pct",
    ):
        if t_key not in thresholds:
            raise ValueError(f"Missing threshold '{t_key}' in settings.json")
        thresholds[t_key] = float(thresholds[t_key])

    floors = data["category_floor_prices_clp"]
    data["category_floor_prices_clp"] = {k.lower(): int(v) for k, v in floors.items()}
    data["stores_enabled"] = [str(s).lower() for s in data["stores_enabled"]]
    data["official_seller_only"] = bool(data.get("official_seller_only", True))
    data["public_web_url"] = str(data.get("public_web_url", ""))
    return data


def load_watchlist(watchlist_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and normalize config/watchlist.json."""
    path = Path(watchlist_path) if watchlist_path else DEFAULT_BASE_DIR / "config" / "watchlist.json"
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    raw_items = data.get("items", []) if isinstance(data, dict) else data
    normalized: list[dict[str, Any]] = []
    for entry in raw_items:
        if not isinstance(entry, dict) or "max_price_clp" not in entry:
            continue
        name = str(entry.get("name", "")).strip()
        keywords = [str(k).strip().lower() for k in entry.get("keywords", []) if str(k).strip()]
        if not keywords and name:
            keywords = [name.lower()]
        skus = [str(s).strip() for s in entry.get("skus", []) if str(s).strip()]
        normalized.append(
            {
                "name": name or (keywords[0] if keywords else "Producto VIP"),
                "keywords": keywords,
                "skus": skus,
                "max_price_clp": int(entry["max_price_clp"]),
                "category": str(entry.get("category", "")).strip().lower(),
            }
        )
    return normalized
