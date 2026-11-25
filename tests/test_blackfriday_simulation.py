"""End-to-end Black Friday Chile stress simulation & readiness verification suite."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.config_loader import load_settings, load_watchlist
from src.detector import DealDetector
from src.main import process_snapshots
from src.models import ProductSnapshot
from src.notifier import AlertNotifier, build_telegram_html
from src.price_reference import CrossStoreReferenceEngine
from src.storage import PriceStorage
from src.web_exporter import export_web_feed


def _snap(
    sku: str,
    store: str,
    title: str,
    brand: str,
    category: str,
    current_price: int,
    normal_price: int,
    seller_name: str,
    url: str,
    *,
    card_price: int | None = None,
    is_official_seller: bool = True,
    in_stock: bool = True,
    image_url: str = "",
    dolares_premio_pct: int = 0,
) -> ProductSnapshot:
    return ProductSnapshot(
        sku=sku,
        store=store,
        title=title,
        brand=brand,
        category=category,
        current_price=current_price,
        card_price=card_price,
        normal_price=normal_price,
        is_official_seller=is_official_seller,
        seller_name=seller_name,
        in_stock=in_stock,
        url=url,
        image_url=image_url,
        dolares_premio_pct=dolares_premio_pct,
    )


class RecordingNotifier(AlertNotifier):
    """Captures every alert that would be sent to Telegram during simulation."""

    def __init__(self) -> None:
        super().__init__(telegram_token="", telegram_chat_id="")
        self.sent_alerts = []
        self.rendered_htmls = []

    def send_alert(self, alert) -> bool:
        self.sent_alerts.append(alert)
        self.rendered_htmls.append(build_telegram_html(alert))
        return True


class TestBlackFridaySimulation(unittest.TestCase):
    """Simulates a full Black Friday event: baseline -> midnight price drops -> anti-spam -> web export."""

    def test_full_black_friday_day_simulation(self) -> None:
        settings = load_settings("config/settings.json")
        watchlist = load_watchlist("config/watchlist.json")
        detector = DealDetector(settings, watchlist)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_path = tmp / "bf_sim.db"
            feed_path = tmp / "deals.json"
            notifier = RecordingNotifier()

            with PriceStorage(db_path) as storage:
                # ------------------------------------------------------------------
                # FASE 1: PRE-BLACK FRIDAY (Miércoles/Jueves — Base de precios reales)
                # ------------------------------------------------------------------
                baseline_items = [
                    # 1. Notebook Tienda Travel a precio normal ($899.990)
                    _snap(
                        "TT-BF-MACBOOK",
                        "Tienda Travel",
                        "Notebook Apple MacBook Air M3 13.6 pulgadas 16GB 256GB",
                        "Apple",
                        "notebooks",
                        899990,
                        949990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/macbook-air-m3",
                        image_url="https://tienda.travel.cl/img/macbook.jpg",
                        dolares_premio_pct=50,
                    ),
                    # 2. Monitor que sufrirá inflación falsa pre-BF ($109.990 -> $699.990 -> $109.990)
                    _snap(
                        "FAL-BF-INFLATED-MON",
                        "Falabella",
                        'Monitor Gamer LG UltraGear 24" 24GS60F IPS 180Hz',
                        "LG",
                        "componentes",
                        109990,
                        129990,
                        "Falabella",
                        "https://www.falabella.com/monitor-24gs60f",
                    ),
                    # 3. Referencias de mercado en otras tiendas para artículo NUEVO con glitch en BF
                    _snap(
                        "RIP-BF-REF-XM5",
                        "Ripley",
                        "Audífonos Bluetooth Sony WH-1000XM5 Noise Cancelling",
                        "Sony",
                        "audio",
                        329990,
                        359990,
                        "Ripley",
                        "https://simple.ripley.cl/sony-xm5",
                    ),
                    _snap(
                        "PAR-BF-REF-XM5",
                        "Paris",
                        "Audífonos Sony WH-1000XM5 Bluetooth ANC Negro",
                        "Sony",
                        "audio",
                        339990,
                        369990,
                        "Paris",
                        "https://www.paris.cl/sony-xm5",
                    ),
                    # 4. Smart TV para probar regla glitch-only (35% ignorado, 65% alerta!)
                    _snap(
                        "FAL-BF-TV-NORMAL-DROP",
                        "Falabella",
                        'Smart TV LG OLED 55" C4 4K UHD',
                        "LG",
                        "tv",
                        1199990,
                        1399990,
                        "Falabella",
                        "https://www.falabella.com/tv-lg-c4",
                    ),
                    _snap(
                        "PAR-BF-TV-GLITCH",
                        "Paris",
                        'Smart TV Samsung OLED 55" S90D 4K',
                        "Samsung",
                        "tv",
                        1099990,
                        1299990,
                        "Paris",
                        "https://www.paris.cl/tv-s90d",
                    ),
                    # 5. Producto barato hueso ($21.990) que bajará 55% ($9.990) -> NO debe ser GLITCH
                    _snap(
                        "TT-BF-DEADSTOCK",
                        "Tienda Travel",
                        "Parlante Portátil Philips TAS2307 Bluetooth",
                        "Philips",
                        "audio",
                        21990,
                        24990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/tas2307",
                    ),
                    # 6. Referencias para validar falsos cruces de gamas distintas
                    _snap(
                        "PCF-BF-MON-27",
                        "PC Factory",
                        'Monitor Lenovo 27" QHD Panel IPS',
                        "Lenovo",
                        "componentes",
                        289990,
                        299990,
                        "PC Factory",
                        "https://www.pcfactory.cl/mon-27",
                    ),
                    _snap(
                        "PAR-BF-OSTER-FULL",
                        "Paris",
                        "Oster Cafetera Espresso Perfect Brew",
                        "Oster",
                        "cafe",
                        599990,
                        629990,
                        "Paris",
                        "https://www.paris.cl/oster-full",
                    ),
                    _snap(
                        "FAL-BF-MACBOOK-PRO",
                        "Falabella",
                        "MacBook Pro 14 XDR Chip M5 Pro 24GB 1TB",
                        "Apple",
                        "notebooks",
                        3399990,
                        3499990,
                        "Falabella",
                        "https://www.falabella.com/macbook-pro",
                    ),
                ]

                for _ in range(3):
                    process_snapshots(baseline_items, storage=storage, detector=detector, notifier=notifier)

                # Simular inflación artificial el jueves por la noche en FAL-BF-INFLATED-MON ($109.990 -> $699.990)
                inflated_spike = [
                    _snap(
                        "FAL-BF-INFLATED-MON",
                        "Falabella",
                        'Monitor Gamer LG UltraGear 24" 24GS60F IPS 180Hz',
                        "LG",
                        "componentes",
                        699990,
                        699990,
                        "Falabella",
                        "https://www.falabella.com/monitor-24gs60f",
                    )
                ]
                process_snapshots(inflated_spike, storage=storage, detector=detector, notifier=notifier)
                notifier.sent_alerts.clear()
                notifier.rendered_htmls.clear()

                # ------------------------------------------------------------------
                # FASE 2: ARRANQUE BLACK FRIDAY 00:00 (Glitches, Nuevos, Trampas)
                # ------------------------------------------------------------------
                bf_midnight_batch = [
                    # A. GLITCH REAL en Tienda Travel: MacBook Air M3 cae de $899.990 a $299.990 (-66.7%, margen $600k)
                    _snap(
                        "TT-BF-MACBOOK",
                        "Tienda Travel",
                        "Notebook Apple MacBook Air M3 13.6 pulgadas 16GB 256GB",
                        "Apple",
                        "notebooks",
                        299990,
                        949990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/macbook-air-m3",
                        image_url="https://tienda.travel.cl/img/macbook.jpg",
                        dolares_premio_pct=50,
                    ),
                    # B. ARTÍCULO NUEVO NUNCA ANTES VISTO con GLITCH real en Tienda Travel (Sony XM5 a $99.990 vs $334.990 mercado)
                    _snap(
                        "TT-BF-NEW-XM5-BLK",
                        "Tienda Travel",
                        "Audífonos Bluetooth Sony WH-1000XM5 Noise Cancelling Negro",
                        "Sony",
                        "audio",
                        99990,
                        99990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/sony-xm5-negro",
                        image_url="https://tienda.travel.cl/img/xm5.jpg",
                    ),
                    # C. VARIANTE DE COLOR del mismo glitch en el mismo escaneo -> debe deduplicar Telegram (0 spam!)
                    _snap(
                        "TT-BF-NEW-XM5-SLV",
                        "Tienda Travel",
                        "Audífonos Bluetooth Sony WH-1000XM5 Noise Cancelling Plata",
                        "Sony",
                        "audio",
                        99990,
                        99990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/sony-xm5-plata",
                    ),
                    # D. TRAMPA INFLACIÓN PRE-BF: Monitor vuelve de $699.990 a $109.990 -> DEBE SER IGNORADO
                    _snap(
                        "FAL-BF-INFLATED-MON",
                        "Falabella",
                        'Monitor Gamer LG UltraGear 24" 24GS60F IPS 180Hz',
                        "LG",
                        "componentes",
                        109990,
                        699990,
                        "Falabella",
                        "https://www.falabella.com/monitor-24gs60f",
                    ),
                    # E. TRAMPA HUESO BAJO MARGEN: Philips $21.990 -> $9.990 (-54.6%, ahorro $12k) -> NO GLITCH
                    _snap(
                        "TT-BF-DEADSTOCK",
                        "Tienda Travel",
                        "Parlante Portátil Philips TAS2307 Bluetooth",
                        "Philips",
                        "audio",
                        9990,
                        24990,
                        "Tienda Travel (Banco de Chile)",
                        "https://tienda.travel.cl/tas2307",
                    ),
                    # F. TRAMPA TARJETA TIENDA: Solo barato con CMR (card_price), precio todo medio es normal
                    _snap(
                        "FAL-BF-CMR-TRAP",
                        "Falabella",
                        "Consola PlayStation 5 Slim 1TB Digital",
                        "Sony",
                        "consolas",
                        549990,
                        599990,
                        "Falabella",
                        "https://www.falabella.com/ps5-cmr",
                        card_price=249990,
                    ),
                    # G. SMART TV descuento normal (-35%) -> IGNORADA por glitch_only_categories
                    _snap(
                        "FAL-BF-TV-NORMAL-DROP",
                        "Falabella",
                        'Smart TV LG OLED 55" C4 4K UHD',
                        "LG",
                        "tv",
                        779990,
                        1399990,
                        "Falabella",
                        "https://www.falabella.com/tv-lg-c4",
                    ),
                    # H. SMART TV GLITCH REAL (-65%, margen $715k) -> DETECTADA COMO GLITCH!
                    _snap(
                        "PAR-BF-TV-GLITCH",
                        "Paris",
                        'Smart TV Samsung OLED 55" S90D 4K',
                        "Samsung",
                        "tv",
                        384990,
                        1299990,
                        "Paris",
                        "https://www.paris.cl/tv-s90d",
                    ),
                    # I. GUARDIA DE GAMAS: Winpy 21.5" NO debe cruzar con PC Factory 27" QHD
                    _snap(
                        "WIN-BF-MON-215",
                        "Winpy",
                        "Monitor Lenovo ThinkVision S22-4e de 21.5” (IPS, Full HD, 100Hz)",
                        "Lenovo",
                        "componentes",
                        103360,
                        108800,
                        "Winpy",
                        "https://www.winpy.cl/mon-215",
                    ),
                    # J. GUARDIA DE GAMAS: Oster Compacta NO debe cruzar con Oster Perfect Brew Full ($599k)
                    _snap(
                        "PAR-BF-OSTER-COMPACTA",
                        "Paris",
                        "Oster Cafetera Espresso Perfect Brew Compacta",
                        "Oster",
                        "cafe",
                        239990,
                        239990,
                        "Paris",
                        "https://www.paris.cl/oster-compacta",
                    ),
                    # K. GUARDIA DE SUBTIPO: iPad Pro 11" NO debe cruzar con MacBook Pro 14"
                    _snap(
                        "WIN-BF-IPAD-M5",
                        "Winpy",
                        "Apple iPad Pro de 11” (Chip M5, 16GB RAM, 1TB)",
                        "Apple",
                        "notebooks",
                        2584000,
                        2699000,
                        "Winpy",
                        "https://www.winpy.cl/ipad-m5",
                    ),
                    # L. OFERTA FLASH REAL Black Friday en tienda externa (PC Factory)
                    _snap(
                        "PCF-BF-FLASH-DEAL",
                        "PC Factory",
                        'Monitor Gamer ASUS TUF 27" VG27AQ 165Hz QHD',
                        "ASUS",
                        "componentes",
                        199990,
                        349990,
                        "PC Factory",
                        "https://www.pcfactory.cl/asus-vg27aq",
                    ),
                ]

                ref_engine = CrossStoreReferenceEngine(
                    storage=storage,
                    live_snapshots=bf_midnight_batch,
                    max_live_lookups=0,
                )
                alerts = process_snapshots(
                    bf_midnight_batch,
                    storage=storage,
                    detector=detector,
                    notifier=notifier,
                    reference_engine=ref_engine,
                )

                alerts_by_sku = {a.snapshot.sku: a for a in alerts}

                # 1. Glitches reales detectados inmediatamente
                self.assertIn("TT-BF-MACBOOK", alerts_by_sku)
                self.assertEqual(alerts_by_sku["TT-BF-MACBOOK"].alert_type, "GLITCH")
                self.assertGreaterEqual(alerts_by_sku["TT-BF-MACBOOK"].savings_clp, 500000)

                # 2. Artículo nuevo nunca visto con glitch detectado por cotejo de mercado
                self.assertIn("TT-BF-NEW-XM5-BLK", alerts_by_sku)
                self.assertEqual(alerts_by_sku["TT-BF-NEW-XM5-BLK"].alert_type, "GLITCH")

                # 3. Smart TV con glitch detectada, Smart TV con 35% ignorada
                self.assertIn("PAR-BF-TV-GLITCH", alerts_by_sku)
                self.assertEqual(alerts_by_sku["PAR-BF-TV-GLITCH"].alert_type, "GLITCH")
                self.assertNotIn("FAL-BF-TV-NORMAL-DROP", alerts_by_sku)

                # 4. Trampas bloqueadas (0 falsos positivos!)
                self.assertNotIn("FAL-BF-INFLATED-MON", alerts_by_sku)
                self.assertNotIn("TT-BF-DEADSTOCK", alerts_by_sku)
                self.assertNotIn("FAL-BF-CMR-TRAP", alerts_by_sku)
                self.assertNotIn("WIN-BF-MON-215", alerts_by_sku)
                self.assertNotIn("PAR-BF-OSTER-COMPACTA", alerts_by_sku)
                self.assertNotIn("WIN-BF-IPAD-M5", alerts_by_sku)

                # 5. Oferta Flash real Black Friday en PC Factory detectada
                self.assertIn("PCF-BF-FLASH-DEAL", alerts_by_sku)
                self.assertEqual(alerts_by_sku["PCF-BF-FLASH-DEAL"].alert_type, "FLASH")

                # 6. Anti-spam por firma: Sony XM5 Negro + Plata solo envió 1 alerta a Telegram!
                xm5_notified = [a for a in notifier.sent_alerts if "WH-1000XM5" in a.snapshot.title]
                self.assertEqual(len(xm5_notified), 1)

                # 7. Segunda pasada 5 minutos después con los mismos precios -> 0 alertas repetidas en Telegram
                before_count = len(notifier.sent_alerts)
                process_snapshots(
                    bf_midnight_batch,
                    storage=storage,
                    detector=detector,
                    notifier=notifier,
                    reference_engine=ref_engine,
                )
                self.assertEqual(len(notifier.sent_alerts), before_count)

                # 8. Verificar que el HTML de Telegram para GLITCH tenga formato de alto impacto
                glitch_htmls = [h for h in notifier.rendered_htmls if "ERROR DE PRECIO DETECTADO" in h]
                self.assertGreaterEqual(len(glitch_htmls), 3)

                # 9. Exportar feed web y validar estructura JSON para el dashboard Black Friday
                feed = export_web_feed(storage, output_path=feed_path, mode="black-friday-sim")
                saved = json.loads(feed_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["counts_by_type"]["GLITCH"], 4)  # MacBook + XM5 Blk/Slv + TV S90D
                self.assertEqual(saved["counts_by_type"]["FLASH"], 1)   # ASUS Monitor
                self.assertGreaterEqual(len(feed["deals"]), 5)


if __name__ == "__main__":
    unittest.main()
