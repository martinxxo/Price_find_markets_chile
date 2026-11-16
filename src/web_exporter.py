"""Exports active deals and radar statistics to docs/data/deals.json for GitHub Pages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.models import utc_now_iso
from src.storage import PriceStorage


def export_web_feed(
    storage: PriceStorage,
    output_path: str | Path = "docs/data/deals.json",
    mode: str = "scan",
) -> dict[str, Any]:
    """Write docs/data/deals.json from current SQLite active deals and stats."""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    stats = storage.get_stats()
    deals = storage.get_active_deals()

    payload = {
        "generated_at": utc_now_iso(),
        "mode": mode,
        "total_monitored_products": stats["total_monitored_products"],
        "total_active_deals": stats["total_active_deals"],
        "counts_by_type": stats["counts_by_type"],
        "counts_by_store": stats["counts_by_store"],
        "deals": deals,
    }

    with target.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    return payload
