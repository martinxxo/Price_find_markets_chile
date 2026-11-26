"""CLI entrypoint and orchestration pipeline for ChileDeals Radar."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path
from typing import Callable, Iterable, Optional

from src.config_loader import load_settings, load_watchlist
from src.detector import DealDetector
from src.filters import is_valid_official_tech_product
from src.models import DealAlert, ProductSnapshot
from src.notifier import AlertNotifier
from src.price_reference import CrossStoreReferenceEngine
from src.scrapers.blackfriday_stores import fetch_blackfriday_stores_products
from src.scrapers.demo_data import get_demo_pass_one, get_demo_pass_two
from src.scrapers.falabella import fetch_falabella_products
from src.scrapers.knasta import fetch_knasta_products
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
    "tienda_travel": fetch_tienda_travel_products,
    "falabella": fetch_falabella_products,
    "paris": fetch_paris_products,
    "ripley": fetch_ripley_products,
    "mercadolibre": fetch_mercadolibre_products,
    "blackfriday_stores": fetch_blackfriday_stores_products,
    "knasta": fetch_knasta_products,
}


def process_snapshots(
    snapshots: Iterable[ProductSnapshot],
    storage: PriceStorage,
    detector: DealDetector,
    notifier: AlertNotifier,
    official_seller_only: bool = True,
    reference_engine: Optional[CrossStoreReferenceEngine] = None,
) -> list[DealAlert]:
    """Filter, validate against market, evaluate, persist, and notify product snapshots."""
    alerts: list[DealAlert] = []
    filtered_out = 0
    valid_items: list[tuple[ProductSnapshot, Optional[dict], Optional[int]]] = []
    seen_batch_skus: set[str] = set()

    for snapshot in snapshots:
        if snapshot.sku in seen_batch_skus:
            continue
        if not is_valid_official_tech_product(snapshot, official_seller_only=official_seller_only):
            filtered_out += 1
            continue
        seen_batch_skus.add(snapshot.sku)
        prev = storage.get_product(snapshot.sku)
        median_price = storage.get_median_historical_price(snapshot.sku) if prev is not None else None
        valid_items.append((snapshot, prev, median_price))

    # Concurrent market reference prefetch: validates all potential GLITCHes and checks unseen Black Friday items
    if reference_engine is not None and valid_items:
        prefetch_candidates: list[tuple[ProductSnapshot, bool, bool]] = []
        for snapshot, prev, median_price in valid_items:
            prelim = detector.evaluate(
                snapshot,
                prev,
                market_reference=None,
                median_historical_price=median_price,
            )
            needs_validation = prelim is not None and (
                prelim.alert_type == "GLITCH" or prelim.real_drop_pct >= 40.0
            )
            prefetch_candidates.append((snapshot, prev is None, needs_validation))
        reference_engine.prefetch_references(prefetch_candidates)

    processed = len(valid_items)
    pending_notifications: list[DealAlert] = []

    # Step 1: Persist all product snapshots, price_history, and active_deals in ONE atomic batch transaction
    with storage.batch_transaction():
        for snapshot, prev, median_price in valid_items:
            market_ref = (
                reference_engine.get_reference_price(
                    snapshot,
                    is_unseen=(prev is None),
                    allow_live_lookup=False,
                )
                if reference_engine is not None
                else None
            )
            alert = detector.evaluate(
                snapshot,
                prev,
                market_reference=market_ref,
                median_historical_price=median_price,
            )

            storage.upsert_product(snapshot, existing=prev, existing_loaded=True)

            if alert is not None:
                storage.upsert_active_deal(alert)
                alerts.append(alert)
                if alert.should_notify:
                    pending_notifications.append(alert)
            else:
                storage.remove_active_deal(snapshot.sku)

    # Step 2: Dispatch Telegram alerts AFTER DB commit, committing each notified signature immediately
    extra_drop = float(detector.thresholds.get("re_notify_extra_drop_pct", 15.0))
    for alert in pending_notifications:
        snap = alert.snapshot
        if storage.should_notify_signature(
            snap.store,
            snap.title,
            snap.effective_price,
            alert.alert_type,
            extra_drop_pct=extra_drop,
        ):
            sent = notifier.send_alert(alert)
            if sent:
                storage.mark_notified(
                    snap.sku,
                    snap.effective_price,
                    alert.detected_at,
                )
                storage.mark_signature_notified(
                    snap.store,
                    snap.title,
                    snap.sku,
                    snap.effective_price,
                    alert.alert_type,
                    alert.detected_at,
                )

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
    # Ensure synthetic demo entries and store-card-only offers never stay in live production feeds
    with storage.conn:
        storage.conn.execute("DELETE FROM products WHERE sku LIKE '%-DEMO-%'")
        storage.conn.execute("DELETE FROM active_deals WHERE sku LIKE '%-DEMO-%'")
        if settings.get("exclude_store_card_prices", True):
            storage.conn.execute("DELETE FROM active_deals WHERE payment_type = 'Tarjeta Tienda'")

    enabled_keys = [
        str(k).lower()
        for k in settings.get("stores_enabled", [])
        if str(k).lower() in SCRAPER_REGISTRY
    ]
    results_by_store: dict[str, list[ProductSnapshot]] = {}

    def _run_store(store_key: str) -> tuple[str, list[ProductSnapshot]]:
        fetcher = SCRAPER_REGISTRY[store_key]
        try:
            items = fetcher()
            logger.info("Store '%s' returned %d candidate items.", store_key, len(items))
            return store_key, items
        except Exception as exc:  # pragma: no cover - defensive guard per store
            logger.warning("Unexpected error scanning store '%s': %s", store_key, exc)
            return store_key, []

    with ThreadPoolExecutor(max_workers=max(1, len(enabled_keys))) as pool:
        futures = [pool.submit(_run_store, sk) for sk in enabled_keys]
        for fut in as_completed(futures):
            sk, items = fut.result()
            results_by_store[sk] = items

    # Preserve priority ordering configured in settings.json (e.g. tienda_travel first)
    all_snapshots: list[ProductSnapshot] = []
    for sk in enabled_keys:
        all_snapshots.extend(results_by_store.get(sk, []))

    ref_engine = None
    if settings.get("cross_reference_enabled", True):
        ref_engine = CrossStoreReferenceEngine(
            storage=storage,
            live_snapshots=all_snapshots,
            max_live_lookups=int(settings.get("max_live_cross_refs_per_scan", 60)),
        )

    return process_snapshots(
        all_snapshots,
        storage=storage,
        detector=detector,
        notifier=notifier,
        official_seller_only=settings.get("official_seller_only", True),
        reference_engine=ref_engine,
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
