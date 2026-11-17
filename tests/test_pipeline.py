"""Unit and integration test suite for ChileDeals Radar."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config_loader import load_settings, load_watchlist
from src.detector import DealDetector
from src.filters import is_valid_official_tech_product
from src.main import process_snapshots, run_demo
from src.models import DealAlert, ProductSnapshot
from src.notifier import AlertNotifier
from src.scrapers.falabella import fetch_falabella_products
from src.scrapers.mercadolibre import fetch_mercadolibre_products
from src.scrapers.paris import fetch_paris_products
from src.scrapers.ripley import fetch_ripley_products
from src.scrapers.tienda_travel import fetch_tienda_travel_products
from src.storage import PriceStorage
from src.web_exporter import export_web_feed


class DummyNotifier(AlertNotifier):
    """In-memory notifier capturing sent alerts without making network calls."""

    def __init__(self) -> None:
        super().__init__(telegram_token="", telegram_chat_id="", public_web_url="")
        self.sent_skus: list[str] = []

    def send_alert(self, alert) -> bool:
        self.sent_skus.append(alert.snapshot.sku)
        return True


def make_snapshot(**overrides) -> ProductSnapshot:
    defaults = {
        "sku": "FAL-TEST-001",
        "store": "Falabella",
        "title": "Notebook Lenovo IdeaPad Slim 3 Ryzen 5 16GB",
        "brand": "Lenovo",
        "category": "notebooks",
        "current_price": 500000,
        "card_price": None,
        "normal_price": 550000,
        "seller_name": "Falabella",
        "is_official_seller": True,
        "in_stock": True,
        "url": "https://www.falabella.com/test",
        "image_url": "",
    }
    defaults.update(overrides)
    return ProductSnapshot(**defaults)


class TestFilters(unittest.TestCase):
    def test_blocks_accessories_third_party_and_out_of_stock(self) -> None:
        accessory = make_snapshot(title="Carcasa iPhone 15 MagSafe Original", current_price=9990)
        refurbished = make_snapshot(title="iPhone 14 Pro Reacondicionado 128GB", current_price=499990)
        third_party = make_snapshot(is_official_seller=False, seller_name="Tienda Random")
        out_of_stock = make_snapshot(in_stock=False)
        valid = make_snapshot()

        self.assertFalse(is_valid_official_tech_product(accessory))
        self.assertFalse(is_valid_official_tech_product(refurbished))
        self.assertFalse(is_valid_official_tech_product(third_party))
        self.assertFalse(is_valid_official_tech_product(out_of_stock))
        self.assertTrue(is_valid_official_tech_product(valid))


class TestDetectorAndStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = load_settings()
        self.watchlist = load_watchlist()
        self.detector = DealDetector(settings=self.settings, watchlist=self.watchlist)
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_prices.db"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_glitch_detection_by_historical_drop(self) -> None:
        with PriceStorage(self.db_path) as storage:
            baseline = make_snapshot(current_price=600000, normal_price=650000)
            storage.upsert_product(baseline)

            dropped = make_snapshot(current_price=240000, card_price=220000, normal_price=650000)
            prev = storage.get_product(dropped.sku)
            alert = self.detector.evaluate(dropped, prev)

            self.assertIsNotNone(alert)
            self.assertEqual(alert.alert_type, "GLITCH")
            self.assertGreaterEqual(alert.real_drop_pct, 50.0)
            self.assertEqual(dropped.payment_type, "Tarjeta Tienda")

    def test_glitch_detection_by_category_floor_first_seen(self) -> None:
        with PriceStorage(self.db_path) as storage:
            unseen_tv = make_snapshot(
                sku="RIP-FLOOR-TV",
                store="Ripley",
                title='Smart TV Samsung 50" 4K UHD',
                brand="Samsung",
                category="tv",
                current_price=89990,
                normal_price=399990,
            )
            alert = self.detector.evaluate(unseen_tv, storage.get_product(unseen_tv.sku))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.alert_type, "GLITCH")

    def test_flash_detection_historical_and_unseen_official_brand(self) -> None:
        with PriceStorage(self.db_path) as storage:
            baseline = make_snapshot(
                sku="FAL-AUDIO-1",
                title="Audífonos Sony WH-1000XM5",
                brand="Sony",
                category="audio",
                current_price=350000,
                normal_price=380000,
            )
            storage.upsert_product(baseline)

            flash = make_snapshot(
                sku="FAL-AUDIO-1",
                title="Audífonos Sony WH-1000XM5",
                brand="Sony",
                category="audio",
                current_price=245000,
                normal_price=380000,
            )
            alert = self.detector.evaluate(flash, storage.get_product(flash.sku))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.alert_type, "FLASH")
            self.assertAlmostEqual(alert.real_drop_pct, 30.0, places=1)

            unseen_brand = make_snapshot(
                sku="MLC-BRAND-1",
                store="MercadoLibre",
                title="Monitor Gamer LG UltraGear 27\"",
                brand="LG",
                category="componentes",
                current_price=199990,
                normal_price=369990,
            )
            unseen_alert = self.detector.evaluate(unseen_brand, None)
            self.assertIsNotNone(unseen_alert)
            self.assertEqual(unseen_alert.alert_type, "FLASH")

    def test_vip_watchlist_detection(self) -> None:
        with PriceStorage(self.db_path) as storage:
            ps5 = make_snapshot(
                sku="PAR-PS5-VIP",
                store="Paris",
                title="Consola PlayStation 5 Slim Digital 1TB",
                brand="Sony",
                category="consolas",
                current_price=389990,
                normal_price=529990,
            )
            alert = self.detector.evaluate(ps5, storage.get_product(ps5.sku))
            self.assertIsNotNone(alert)
            self.assertEqual(alert.alert_type, "VIP")

    def test_anti_spam_prevents_duplicate_notifications(self) -> None:
        notifier = DummyNotifier()
        with PriceStorage(self.db_path) as storage:
            s1 = make_snapshot(sku="FAL-SPAM-1", current_price=500000, normal_price=550000)
            process_snapshots([s1], storage, self.detector, notifier)
            self.assertEqual(len(notifier.sent_skus), 0)

            # First real drop -> notifies once
            s2 = make_snapshot(sku="FAL-SPAM-1", current_price=300000, normal_price=550000)
            alerts2 = process_snapshots([s2], storage, self.detector, notifier)
            self.assertEqual(len(alerts2), 1)
            self.assertEqual(len(notifier.sent_skus), 1)

            # Tiny extra drop (< 5%) -> kept in active_deals, NOT re-notified
            s3 = make_snapshot(sku="FAL-SPAM-1", current_price=295000, normal_price=550000)
            alerts3 = process_snapshots([s3], storage, self.detector, notifier)
            self.assertEqual(len(alerts3), 1)
            self.assertFalse(alerts3[0].should_notify)
            self.assertEqual(len(notifier.sent_skus), 1)

            # Significant extra drop (>= 5% vs last notified 300k) -> re-notifies
            s4 = make_snapshot(sku="FAL-SPAM-1", current_price=270000, normal_price=550000)
            alerts4 = process_snapshots([s4], storage, self.detector, notifier)
            self.assertEqual(len(alerts4), 1)
            self.assertTrue(alerts4[0].should_notify)
            self.assertEqual(len(notifier.sent_skus), 2)

            # Price goes back to normal -> removed from active_deals
            s5 = make_snapshot(sku="FAL-SPAM-1", current_price=510000, normal_price=550000)
            alerts5 = process_snapshots([s5], storage, self.detector, notifier)
            self.assertEqual(len(alerts5), 0)
            self.assertEqual(len(storage.get_active_deals()), 0)

    def test_demo_end_to_end_and_web_exporter(self) -> None:
        notifier = DummyNotifier()
        out_json = Path(self.tmp_dir.name) / "docs" / "data" / "deals.json"
        with PriceStorage(self.db_path) as storage:
            alerts = run_demo(storage, self.detector, notifier)
            payload = export_web_feed(storage, output_path=out_json, mode="demo")

            self.assertTrue(out_json.exists())
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["total_active_deals"], len(alerts))
            self.assertGreaterEqual(loaded["counts_by_type"]["GLITCH"], 2)
            self.assertGreaterEqual(loaded["counts_by_type"]["VIP"], 2)
            self.assertGreaterEqual(loaded["counts_by_type"]["FLASH"], 2)
            self.assertEqual(payload["deals"][0]["alert_type"], "GLITCH")
            # Ensure demo noise SKUs were never stored
            self.assertIsNone(storage.get_product("FAL-DEMO-NOISE-CASE"))
            self.assertIsNone(storage.get_product("PAR-DEMO-NOISE-3P"))
            self.assertIsNone(storage.get_product("RIP-DEMO-NOISE-OOS"))


class TestScraperParsingAndResilience(unittest.TestCase):
    def test_mercadolibre_parser_filters_non_official_and_handles_none(self) -> None:
        sample_payload = {
            "results": [
                {
                    "id": 12345,
                    "sku": "MLC123",
                    "name": "Apple iPhone 15 128GB",
                    "condition": "https://schema.org/NewCondition",
                    "external_url": "https://articulo.mercadolibre.cl/MLC123",
                    "picture_urls": ["https://http2.mlstatic.com/img.jpg"],
                    "active_registry": {
                        "is_available": True,
                        "normal_price": "899990.00",
                        "offer_price": "699990.00",
                    },
                },
                {
                    "id": 99999,
                    "sku": "MLC999",
                    "name": "iPhone 15 Reacondicionado",
                    "condition": "https://schema.org/RefurbishedCondition",
                    "active_registry": {
                        "is_available": True,
                        "normal_price": "500000.00",
                        "offer_price": "300000.00",
                    },
                },
            ]
        }
        with patch("src.scrapers.solotodo_client.fetch_json", return_value=sample_payload):
            items = fetch_mercadolibre_products(limit_per_category=10)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].sku, "MLC-MLC123")
            self.assertEqual(items[0].brand, "Apple")
            self.assertTrue(items[0].is_official_seller)
            self.assertEqual(items[0].effective_price, 699990)

        with patch("src.scrapers.solotodo_client.fetch_json", return_value=None):
            self.assertEqual(fetch_mercadolibre_products(), [])

    def test_retail_parsers_handle_1p_and_errors(self) -> None:
        fal_payload = {
            "data": {
                "results": [
                    {
                        "skuId": "17001",
                        "displayName": "Notebook Asus VivoBook",
                        "brand": "ASUS",
                        "sellerId": "FALABELLA_CHILE",
                        "sellerName": "Falabella",
                        "url": "/falabella-cl/product/17001",
                        "isOutOfStock": False,
                        "prices": [
                            {"type": "cmrPrice", "price": ["299.990"]},
                            {"type": "internetPrice", "price": ["329.990"]},
                            {"type": "normalPrice", "price": ["499.990"]},
                        ],
                    },
                    {
                        "skuId": "17002",
                        "displayName": "Notebook 3P",
                        "sellerId": "SELLER_3P",
                        "sellerName": "Externo",
                        "prices": [{"type": "internetPrice", "price": ["99.990"]}],
                    },
                ]
            }
        }
        with patch("src.scrapers.falabella.fetch_json", return_value=fal_payload):
            fal_items = fetch_falabella_products()
            self.assertEqual(len(fal_items), 1)
            self.assertEqual(fal_items[0].effective_price, 299990)

        paris_payload = {
            "results": [
                {
                    "sku": "P555",
                    "name": "Smart TV LG 55",
                    "condition": "https://schema.org/NewCondition",
                    "external_url": "https://www.paris.cl/tv-lg.html",
                    "active_registry": {
                        "is_available": True,
                        "normal_price": "699990.00",
                        "offer_price": "379990.00",
                    },
                }
            ]
        }
        with patch("src.scrapers.solotodo_client.fetch_json", return_value=paris_payload):
            par_items = fetch_paris_products()
            self.assertEqual(len(par_items), 1)
            self.assertEqual(par_items[0].effective_price, 379990)

        ripley_payload = {
            "results": [
                {
                    "sku": "20003999",
                    "name": "Consola Sony PS5",
                    "condition": "https://schema.org/NewCondition",
                    "external_url": "https://simple.ripley.cl/p/ps5",
                    "active_registry": {
                        "is_available": True,
                        "normal_price": "599990.00",
                        "offer_price": "429990.00",
                    },
                }
            ]
        }
        with patch("src.scrapers.solotodo_client.fetch_json", return_value=ripley_payload):
            rip_items = fetch_ripley_products()
            self.assertEqual(len(rip_items), 1)
            self.assertEqual(rip_items[0].effective_price, 429990)

    def test_persistent_brand_flash_deal_stays_active_without_spam(self) -> None:
        notifier = DummyNotifier()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "persist.db"
            settings = load_settings()
            detector = DealDetector(settings=settings, watchlist=[])
            with PriceStorage(db_path) as storage:
                deal = make_snapshot(
                    sku="FAL-PERSIST-1",
                    brand="Samsung",
                    category="celulares",
                    current_price=629990,
                    normal_price=1069990,
                )
                alerts1 = process_snapshots([deal], storage, detector, notifier)
                self.assertEqual(len(alerts1), 1)
                self.assertEqual(len(notifier.sent_skus), 1)

                # Second scan at exact same discounted price -> stays active in web, NO duplicate Telegram
                alerts2 = process_snapshots([deal], storage, detector, notifier)
                self.assertEqual(len(alerts2), 1)
                self.assertFalse(alerts2[0].should_notify)
                self.assertEqual(len(notifier.sent_skus), 1)
                self.assertEqual(len(storage.get_active_deals()), 1)

    def test_tienda_travel_dolares_premio_detection_and_parser(self) -> None:
        assembler_payload = {
            "results": {
                "records": [
                    {
                        "attributes": {
                            "product.repositoryId": ["SKU150522"],
                            "product.displayName": ["Smartphone Xiaomi Redmi 17"],
                            "product.route": ["/smartphones/redmi-17/SKU150522"],
                            "product.primaryFullImageURL": ["/img/redmi17.jpg"],
                        },
                        "records": [
                            {
                                "attributes": {
                                    "sku.listPrice": ["299990.000000"],
                                    "sku.salePrice": ["219990.000000"],
                                }
                            }
                        ],
                    }
                ]
            }
        }
        batch_payload = {
            "items": [
                {"id": "SKU150522", "x_tasaDeCanje": "40", "brand": "Xiaomi"}
            ]
        }

        def fake_fetch(url, params=None, headers=None, timeout=18):
            if "assembler" in url:
                return assembler_payload
            return batch_payload

        with patch("src.scrapers.tienda_travel.fetch_json", side_effect=fake_fetch):
            tt_items = fetch_tienda_travel_products(limit_per_category=5)
            self.assertEqual(len(tt_items), 1)
            item = tt_items[0]
            self.assertEqual(item.sku, "TT-SKU150522")
            self.assertEqual(item.dolares_premio_pct, 40)
            self.assertGreater(item.dolares_premio_amount, 0)

            settings = load_settings()
            detector = DealDetector(settings=settings, watchlist=[])
            alert = detector.evaluate(item, None)
            self.assertIsNotNone(alert)
            self.assertEqual(alert.alert_type, "DOLARES_PREMIO")

    def test_notifier_uses_send_photo_and_falls_back_to_send_message(self) -> None:
        import requests as req_lib

        snap = make_snapshot(image_url="https://media.falabella.com/falabellaCL/123/public")
        alert = DealAlert(
            snapshot=snap,
            alert_type="FLASH",
            previous_real_price=600000,
            min_historical_price=500000,
            real_drop_pct=30.0,
            store_discount_pct=30.0,
            savings_clp=100000,
            reason="Test photo",
        )
        notifier = AlertNotifier(telegram_token="tok", telegram_chat_id="123")

        class OkResp:
            def raise_for_status(self) -> None:
                return None

        called_urls: list[str] = []

        def ok_post(url, json=None, timeout=12):
            called_urls.append(url)
            return OkResp()

        with patch("src.notifier.requests.post", side_effect=ok_post):
            self.assertTrue(notifier.send_alert(alert))
            self.assertEqual(len(called_urls), 1)
            self.assertTrue(called_urls[0].endswith("/sendPhoto"))

        called_urls.clear()

        def fallback_post(url, json=None, timeout=12):
            called_urls.append(url)
            if url.endswith("/sendPhoto"):
                raise req_lib.RequestException("bad image")
            return OkResp()

        with patch("src.notifier.requests.post", side_effect=fallback_post):
            self.assertTrue(notifier.send_alert(alert))
            self.assertEqual(len(called_urls), 2)
            self.assertTrue(called_urls[0].endswith("/sendPhoto"))
            self.assertTrue(called_urls[1].endswith("/sendMessage"))

    def test_tv_is_glitch_only_and_priority_items_have_lower_flash_threshold(self) -> None:
        settings = load_settings()
        detector = DealDetector(settings=settings, watchlist=[])

        # TV with 45% normal discount should NOT trigger FLASH
        tv_flash = make_snapshot(
            sku="FAL-TV-FLASH",
            title='Smart TV Samsung 55" 4K',
            brand="Samsung",
            category="tv",
            current_price=550000,
            normal_price=1000000,
        )
        self.assertIsNone(detector.evaluate(tv_flash, None))

        # TV with GLITCH (>= 50% historical drop) SHOULD trigger GLITCH
        tv_glitch = make_snapshot(
            sku="FAL-TV-GLITCH",
            title='Smart TV LG 55" OLED',
            brand="LG",
            category="tv",
            current_price=350000,
            normal_price=1200000,
        )
        prev_tv = {
            "last_price": 1000000,
            "avg_price": 1000000.0,
            "min_price": 1000000,
            "max_price": 1000000,
            "last_notified_price": None,
        }
        glitch_alert = detector.evaluate(tv_glitch, prev_tv)
        self.assertIsNotNone(glitch_alert)
        self.assertEqual(glitch_alert.alert_type, "GLITCH")

        # Priority item (tocadiscos / audio) with 32% discount triggers FLASH
        turntable = make_snapshot(
            sku="TT-AUDIO-1",
            title="Tocadiscos Audio-Technica AT-LP60X",
            brand="Audio-Technica",
            category="audio",
            current_price=135000,
            normal_price=199990,
        )
        tt_alert = detector.evaluate(turntable, None)
        self.assertIsNotNone(tt_alert)
        self.assertEqual(tt_alert.alert_type, "FLASH")


if __name__ == "__main__":
    unittest.main()
