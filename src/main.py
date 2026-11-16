"""CLI entrypoint and orchestration pipeline for ChileDeals Radar."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Callable, Iterable

from src.config_loader import load_settings, load_watchlist
from src.detector import DealDetector
from src.filters import is_valid_official_tech_product
from src.models import DealAlert, ProductSnapshot
from src.notifier import AlertNotifier
from src.scrapers.demo_data import get_demo_pass_one, get_demo_pass_two
from src.scrapers.falabella import fetch_falabella_products
from src.scrapers.mercadolibre import fetch_mercadolibre_products
from src.scrapers.paris import fetch_paris_products
from src.scrapers.ripley import fetch_ripley_products
from src.scrapers.tienda_travel import fetch_tienda_travel_products
from src.storage import PriceStorage
from src.web_exporter import export_web_feed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("chiledeals")

SCRAPER_REGISTRY: dict[str, Callable[[], list[ProductSnapshot]]] = {
    "mercadolibre": fetch_mercadolibre_products,
    "falabella": fetch_falabella_products,
    "paris": fetch_paris_products,
    "ripley": fetch_ripley_products,
    "tienda_travel": fetch_tienda_travel_products,
}


def process_snapshots(
    snapshots: Iterable[ProductSnapshot],
    storage: PriceStorage,
    detector: DealDetector,
    notifier: AlertNotifier,
    official_seller_only: bool = True,
) -> list[DealAlert]:
    """Filter, evaluate, persist, and notify a batch of product snapshots."""
    alerts: list[DealAlert] = []
    filtered_out = 0
    processed = 0

    for snapshot in snapshots:
        if not is_valid_official_tech_product(snapshot, official_seller_only=official_seller_only):
            filtered_out += 1
            continue

        processed += 1
        prev = storage.get_product(snapshot.sku)
        alert = detector.evaluate(snapshot, prev)

        storage.upsert_product(snapshot)

        if alert is not None:
            storage.upsert_active_deal(alert)
            alerts.append(alert)
            if alert.should_notify:
                sent = notifier.send_alert(alert)
                if sent:
                    storage.mark_notified(
                        snapshot.sku,
                        snapshot.effective_price,
                        alert.detected_at,
                    )
        else:
            storage.remove_active_deal(snapshot.sku)

    logger.info(
        "Batch processed: %d valid products, %d filtered out, %d deals detected.",
        processed,
        filtered_out,
        len(alerts),
    )
    return alerts


def run_demo(
    storage: PriceStorage,
    detector: DealDetector,
    notifier: AlertNotifier,
    official_seller_only: bool = True,
) -> list[DealAlert]:
    """Run a clean two-pass demo simulation to seed history and detect deals."""
    with storage.conn:
        storage.conn.execute("DELETE FROM products WHERE sku LIKE '%-DEMO-%'")
        storage.conn.execute("DELETE FROM active_deals WHERE sku LIKE '%-DEMO-%'")

    logger.info("Demo Paso 1/2: Cargando precios históricos base...")
    process_snapshots(
        get_demo_pass_one(),
        storage=storage,
        detector=detector,
        notifier=notifier,
        official_seller_only=official_seller_only,
    )

    logger.info("Demo Paso 2/2: Simulando caídas de precio (glitches, VIP y ofertas flash)...")
    alerts = process_snapshots(
        get_demo_pass_two(),
        storage=storage,
        detector=detector,
        notifier=notifier,
        official_seller_only=official_seller_only,
    )
    return alerts


def run_live_scan(
    settings: dict,
    storage: PriceStorage,
    detector: DealDetector,
    notifier: AlertNotifier,
) -> list[DealAlert]:
    """Fetch snapshots from all enabled Chilean stores and process them."""
    # Ensure synthetic demo entries never leak into live production feeds
    with storage.conn:
        storage.conn.execute("DELETE FROM products WHERE sku LIKE '%-DEMO-%'")
        storage.conn.execute("DELETE FROM active_deals WHERE sku LIKE '%-DEMO-%'")

    all_snapshots: list[ProductSnapshot] = []
    for store_key in settings.get("stores_enabled", []):
        fetcher = SCRAPER_REGISTRY.get(store_key.lower())
        if fetcher is None:
            logger.warning("Unknown store '%s' in settings.json; skipping.", store_key)
            continue
        try:
            items = fetcher()
            logger.info("Store '%s' returned %d candidate items.", store_key, len(items))
            all_snapshots.extend(items)
        except Exception as exc:  # pragma: no cover - defensive guard per store
            logger.warning("Unexpected error scanning store '%s': %s", store_key, exc)

    return process_snapshots(
        all_snapshots,
        storage=storage,
        detector=detector,
        notifier=notifier,
        official_seller_only=settings.get("official_seller_only", True),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ChileDeals Radar CLI")
    parser.add_argument(
        "--mode",
        choices=("scan", "demo"),
        default="scan",
        help="Execution mode: 'scan' for live stores, 'demo' for two-pass simulation.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Shortcut for --mode demo.",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a test message to verify Telegram Bot credentials.",
    )
    parser.add_argument(
        "--db-path",
        default="data/prices.db",
        help="Path to SQLite historical database.",
    )
    parser.add_argument(
        "--output-json",
        default="docs/data/deals.json",
        help="Path to export the static web feed JSON.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "demo" if args.demo else args.mode

    settings = load_settings()
    watchlist = load_watchlist()
    notifier = AlertNotifier(public_web_url=settings.get("public_web_url", ""))

    if args.test_telegram:
        ok = notifier.send_test_message()
        return 0 if ok else 1

    detector = DealDetector(settings=settings, watchlist=watchlist)
    with PriceStorage(db_path=Path(args.db_path)) as storage:
        if mode == "demo":
            alerts = run_demo(
                storage=storage,
                detector=detector,
                notifier=notifier,
                official_seller_only=settings.get("official_seller_only", True),
            )
        else:
            alerts = run_live_scan(
                settings=settings,
                storage=storage,
                detector=detector,
                notifier=notifier,
            )

        payload = export_web_feed(storage, output_path=args.output_json, mode=mode)
        logger.info(
            "Web feed exported to %s (%d active deals across %d monitored products).",
            args.output_json,
            payload["total_active_deals"],
            payload["total_monitored_products"],
        )
        print(
            f"\nResumen ({mode}): {len(alerts)} alertas detectadas | "
            f"{payload['total_active_deals']} activas en {args.output_json}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
