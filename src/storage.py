"""SQLite persistence for historical prices and active deals."""

from __future__ import annotations

import re
import sqlite3
import statistics
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from src.models import DealAlert, ProductSnapshot, utc_now_iso

_COLOR_WORDS = {
    "negro", "negra", "blanco", "blanca", "azul", "gris", "plata", "silver",
    "black", "white", "blue", "gray", "grey", "rojo", "roja", "verde",
    "dorado", "rosado", "rosa", "celeste", "morado", "violeta",
}


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def make_product_signature(store: str, title: str) -> str:
    """Build a normalized deduplication key across color variants and duplicate SKUs."""
    decomposed = unicodedata.normalize("NFKD", title or "")
    clean = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    clean = re.sub(r"[^a-z0-9\s]", " ", clean)
    tokens = [t for t in clean.split() if t not in _COLOR_WORDS]
    return f"{(store or '').strip().lower()}::{' '.join(tokens)}"


class PriceStorage:
    """Stores product price history and currently active deal alerts in SQLite."""

    def __init__(self, db_path: str | Path = "data/prices.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self._in_batch = False
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    sku TEXT PRIMARY KEY,
                    store TEXT NOT NULL,
                    title TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    category TEXT NOT NULL,
                    last_price INTEGER NOT NULL,
                    card_price INTEGER,
                    normal_price INTEGER,
                    min_price INTEGER NOT NULL,
                    max_price INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    observations_count INTEGER NOT NULL DEFAULT 1,
                    last_notified_price INTEGER,
                    last_notified_at TEXT,
                    url TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_deals (
                    sku TEXT PRIMARY KEY,
                    store TEXT NOT NULL,
                    title TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    category TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    effective_price INTEGER NOT NULL,
                    current_price INTEGER NOT NULL,
                    card_price INTEGER,
                    normal_price INTEGER,
                    payment_type TEXT NOT NULL,
                    previous_real_price INTEGER NOT NULL,
                    min_historical_price INTEGER NOT NULL,
                    real_drop_pct REAL NOT NULL,
                    store_discount_pct REAL NOT NULL,
                    savings_clp INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    seller_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    image_url TEXT DEFAULT '',
                    dolares_premio_pct INTEGER DEFAULT 0,
                    dolares_premio_amount REAL DEFAULT 0.0,
                    copay_clp INTEGER DEFAULT 0,
                    detected_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing_cols = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(active_deals)").fetchall()
            }
            for col_name, col_def in (
                ("dolares_premio_pct", "INTEGER DEFAULT 0"),
                ("dolares_premio_amount", "REAL DEFAULT 0.0"),
                ("copay_clp", "INTEGER DEFAULT 0"),
            ):
                if col_name not in existing_cols:
                    self.conn.execute(
                        f"ALTER TABLE active_deals ADD COLUMN {col_name} {col_def}"
                    )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reference_prices (
                    query_key TEXT PRIMARY KEY,
                    ref_price INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    matched_title TEXT NOT NULL,
                    sample_count INTEGER DEFAULT 1,
                    is_verified_history INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            ref_cols = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(reference_prices)").fetchall()
            }
            for col_name, col_def in (
                ("sample_count", "INTEGER DEFAULT 1"),
                ("is_verified_history", "INTEGER DEFAULT 0"),
            ):
                if col_name not in ref_cols:
                    self.conn.execute(
                        f"ALTER TABLE reference_prices ADD COLUMN {col_name} {col_def}"
                    )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    observed_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_sku ON price_history (sku, id DESC)"
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notified_signatures (
                    product_key TEXT PRIMARY KEY,
                    last_notified_price INTEGER NOT NULL,
                    last_notified_at TEXT NOT NULL,
                    last_alert_type TEXT NOT NULL,
                    last_sku TEXT NOT NULL
                )
                """
            )
            sig_count = self.conn.execute("SELECT COUNT(*) FROM notified_signatures").fetchone()[0]
            if sig_count == 0:
                prev_notified = self.conn.execute(
                    """
                    SELECT sku, store, title, last_notified_price, last_notified_at
                    FROM products
                    WHERE last_notified_price IS NOT NULL AND last_notified_price > 0
                    """
                ).fetchall()
                for row in prev_notified:
                    p_key = make_product_signature(str(row["store"]), str(row["title"]))
                    self.conn.execute(
                        """
                        INSERT INTO notified_signatures (
                            product_key, last_notified_price, last_notified_at, last_alert_type, last_sku
                        ) VALUES (?, ?, ?, 'FLASH', ?)
                        ON CONFLICT(product_key) DO UPDATE SET
                            last_notified_price = MIN(last_notified_price, excluded.last_notified_price)
                        """,
                        (
                            p_key,
                            int(row["last_notified_price"]),
                            str(row["last_notified_at"] or utc_now_iso()),
                            str(row["sku"]),
                        ),
                    )

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        self.conn.close()

    def __enter__(self) -> "PriceStorage":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def batch_transaction(self) -> Iterator[None]:
        """Group bulk product/deal writes into a single atomic SQLite transaction."""
        if self._in_batch:
            yield
            return
        self._in_batch = True
        try:
            with self.conn:
                yield
        finally:
            self._in_batch = False

    def _maybe_commit_ctx(self):
        """Return a no-op context inside batch_transaction, or self.conn otherwise."""
        if self._in_batch:
            return _null_context()
        return self.conn

    def get_product(self, sku: str) -> Optional[dict[str, Any]]:
        """Fetch historical price record for a SKU, or None if unseen."""
        row = self.conn.execute(
            "SELECT * FROM products WHERE sku = ?",
            (sku,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_product(
        self,
        snapshot: ProductSnapshot,
        existing: Optional[dict[str, Any]] = None,
        existing_loaded: bool = False,
    ) -> None:
        """Insert or update historical price statistics for a product snapshot."""
        price = snapshot.effective_price
        if not existing_loaded and existing is None:
            existing = self.get_product(snapshot.sku)
        now = snapshot.timestamp or utc_now_iso()

        with self._maybe_commit_ctx():
            if existing is None:
                new_count = 1
                self.conn.execute(
                    """
                    INSERT INTO products (
                        sku, store, title, brand, category,
                        last_price, card_price, normal_price,
                        min_price, max_price, avg_price, observations_count,
                        last_notified_price, last_notified_at,
                        url, image_url, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        snapshot.sku,
                        snapshot.store,
                        snapshot.title,
                        snapshot.brand,
                        snapshot.category,
                        price,
                        snapshot.card_price,
                        snapshot.normal_price,
                        price,
                        price,
                        float(price),
                        snapshot.url,
                        snapshot.image_url,
                        now,
                    ),
                )
            else:
                count = int(existing["observations_count"])
                new_count = count + 1
                new_avg = ((float(existing["avg_price"]) * count) + price) / new_count
                new_min = min(int(existing["min_price"]), price)
                new_max = max(int(existing["max_price"]), price)
                self.conn.execute(
                    """
                    UPDATE products
                    SET store = ?,
                        title = ?,
                        brand = ?,
                        category = ?,
                        last_price = ?,
                        card_price = ?,
                        normal_price = ?,
                        min_price = ?,
                        max_price = ?,
                        avg_price = ?,
                        observations_count = ?,
                        url = ?,
                        image_url = ?,
                        updated_at = ?
                    WHERE sku = ?
                    """,
                    (
                        snapshot.store,
                        snapshot.title,
                        snapshot.brand,
                        snapshot.category,
                        price,
                        snapshot.card_price,
                        snapshot.normal_price,
                        new_min,
                        new_max,
                        round(new_avg, 2),
                        new_count,
                        snapshot.url,
                        snapshot.image_url,
                        now,
                        snapshot.sku,
                    ),
                )
            self.conn.execute(
                "INSERT INTO price_history (sku, price, observed_at) VALUES (?, ?, ?)",
                (snapshot.sku, price, now),
            )
            # Keep at most the latest 30 observations per SKU to keep DB compact
            if new_count > 30:
                self.conn.execute(
                    """
                    DELETE FROM price_history
                    WHERE sku = ?
                      AND id NOT IN (
                          SELECT id FROM price_history
                          WHERE sku = ?
                          ORDER BY id DESC
                          LIMIT 30
                      )
                    """,
                    (snapshot.sku, snapshot.sku),
                )

    def get_median_historical_price(self, sku: str) -> Optional[int]:
        """Compute spike-resistant median price from recent price_history observations."""
        rows = self.conn.execute(
            "SELECT price FROM price_history WHERE sku = ? ORDER BY id DESC LIMIT 30",
            (sku,),
        ).fetchall()
        prices = [int(r["price"]) for r in rows if int(r["price"]) > 0]
        if len(prices) < 2:
            return None
        return int(round(statistics.median(prices)))

    def mark_notified(self, sku: str, notified_price: int, notified_at: Optional[str] = None) -> None:
        """Record the last notified price and timestamp for anti-spam throttling."""
        ts = notified_at or utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                UPDATE products
                SET last_notified_price = ?,
                    last_notified_at = ?
                WHERE sku = ?
                """,
                (int(notified_price), ts, sku),
            )

    def should_notify_signature(
        self,
        store: str,
        title: str,
        effective_price: int,
        alert_type: str,
        extra_drop_pct: float = 15.0,
    ) -> bool:
        """Prevent duplicate Telegram alerts across color variants or duplicate SKUs."""
        p_key = make_product_signature(store, title)
        row = self.conn.execute(
            "SELECT last_notified_price, last_alert_type FROM notified_signatures WHERE product_key = ?",
            (p_key,),
        ).fetchone()
        if row is None:
            return True

        prev_price = int(row["last_notified_price"])
        prev_type = str(row["last_alert_type"])
        # Always allow escalation to GLITCH if previously only FLASH/VIP/DP
        if alert_type == "GLITCH" and prev_type != "GLITCH":
            return True

        cutoff = float(prev_price) * (1.0 - (extra_drop_pct / 100.0))
        return float(effective_price) <= cutoff

    def mark_signature_notified(
        self,
        store: str,
        title: str,
        sku: str,
        notified_price: int,
        alert_type: str,
        notified_at: Optional[str] = None,
    ) -> None:
        """Record normalized product signature after sending a Telegram alert."""
        ts = notified_at or utc_now_iso()
        p_key = make_product_signature(store, title)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO notified_signatures (
                    product_key, last_notified_price, last_notified_at, last_alert_type, last_sku
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(product_key) DO UPDATE SET
                    last_notified_price = excluded.last_notified_price,
                    last_notified_at = excluded.last_notified_at,
                    last_alert_type = excluded.last_alert_type,
                    last_sku = excluded.last_sku
                """,
                (p_key, int(notified_price), ts, alert_type, sku),
            )

    def upsert_active_deal(self, alert: DealAlert) -> None:
        """Insert or update an active deal for the web dashboard feed."""
        s = alert.snapshot
        now = utc_now_iso()
        with self._maybe_commit_ctx():
            self.conn.execute(
                """
                INSERT INTO active_deals (
                    sku, store, title, brand, category, alert_type,
                    effective_price, current_price, card_price, normal_price,
                    payment_type, previous_real_price, min_historical_price,
                    real_drop_pct, store_discount_pct, savings_clp,
                    reason, seller_name, url, image_url,
                    dolares_premio_pct, dolares_premio_amount, copay_clp,
                    detected_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku) DO UPDATE SET
                    store = excluded.store,
                    title = excluded.title,
                    brand = excluded.brand,
                    category = excluded.category,
                    alert_type = excluded.alert_type,
                    effective_price = excluded.effective_price,
                    current_price = excluded.current_price,
                    card_price = excluded.card_price,
                    normal_price = excluded.normal_price,
                    payment_type = excluded.payment_type,
                    previous_real_price = excluded.previous_real_price,
                    min_historical_price = excluded.min_historical_price,
                    real_drop_pct = excluded.real_drop_pct,
                    store_discount_pct = excluded.store_discount_pct,
                    savings_clp = excluded.savings_clp,
                    reason = excluded.reason,
                    seller_name = excluded.seller_name,
                    url = excluded.url,
                    image_url = excluded.image_url,
                    dolares_premio_pct = excluded.dolares_premio_pct,
                    dolares_premio_amount = excluded.dolares_premio_amount,
                    copay_clp = excluded.copay_clp,
                    updated_at = excluded.updated_at
                """,
                (
                    s.sku,
                    s.store,
                    s.title,
                    s.brand,
                    s.category,
                    alert.alert_type,
                    s.effective_price,
                    s.current_price,
                    s.card_price,
                    s.normal_price,
                    s.payment_type,
                    alert.previous_real_price,
                    alert.min_historical_price,
                    alert.real_drop_pct,
                    alert.store_discount_pct,
                    alert.savings_clp,
                    alert.reason,
                    s.seller_name,
                    s.url,
                    s.image_url,
                    s.dolares_premio_pct,
                    s.dolares_premio_amount,
                    s.copay_clp,
                    alert.detected_at,
                    now,
                ),
            )

    def remove_active_deal(self, sku: str) -> None:
        """Remove a deal that is no longer discounted or out of stock."""
        with self._maybe_commit_ctx():
            self.conn.execute("DELETE FROM active_deals WHERE sku = ?", (sku,))

    def get_reference_price(self, query_key: str) -> Optional[dict[str, Any]]:
        """Fetch a cached cross-store market reference price."""
        row = self.conn.execute(
            "SELECT * FROM reference_prices WHERE query_key = ?",
            (query_key,),
        ).fetchone()
        return dict(row) if row else None

    def upsert_reference_price(
        self,
        query_key: str,
        ref_price: int,
        source: str,
        matched_title: str,
        sample_count: int = 1,
        is_verified_history: bool = False,
    ) -> None:
        """Cache a cross-store market reference price in SQLite."""
        now = utc_now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO reference_prices (
                    query_key, ref_price, source, matched_title,
                    sample_count, is_verified_history, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    ref_price = excluded.ref_price,
                    source = excluded.source,
                    matched_title = excluded.matched_title,
                    sample_count = excluded.sample_count,
                    is_verified_history = excluded.is_verified_history,
                    updated_at = excluded.updated_at
                """,
                (
                    query_key,
                    int(ref_price),
                    source,
                    matched_title,
                    int(sample_count),
                    1 if is_verified_history else 0,
                    now,
                ),
            )

    def get_all_products_brief(self) -> list[dict[str, Any]]:
        """Return lightweight product rows for building the cross-store reference index."""
        rows = self.conn.execute(
            """
            SELECT sku, store, title, brand, category,
                   last_price, normal_price, min_price, max_price, observations_count
            FROM products
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_active_deals(self) -> list[dict[str, Any]]:
        """Return all active deals sorted by urgency (GLITCH > VIP > FLASH) and drop %."""
        rows = self.conn.execute(
            """
            SELECT * FROM active_deals
            ORDER BY
                CASE alert_type
                    WHEN 'GLITCH' THEN 1
                    WHEN 'VIP' THEN 2
                    WHEN 'FLASH' THEN 3
                    WHEN 'DOLARES_PREMIO' THEN 4
                    ELSE 5
                END ASC,
                real_drop_pct DESC,
                dolares_premio_pct DESC,
                savings_clp DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, Any]:
        """Compute summary counters for monitored products and active deals."""
        total_products = int(
            self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        )
        type_rows = self.conn.execute(
            "SELECT alert_type, COUNT(*) AS cnt FROM active_deals GROUP BY alert_type"
        ).fetchall()
        store_rows = self.conn.execute(
            "SELECT store, COUNT(*) AS cnt FROM active_deals GROUP BY store"
        ).fetchall()

        counts_by_type = {"GLITCH": 0, "VIP": 0, "FLASH": 0, "DOLARES_PREMIO": 0}
        for row in type_rows:
            counts_by_type[str(row["alert_type"])] = int(row["cnt"])

        counts_by_store: dict[str, int] = {}
        for row in store_rows:
            counts_by_store[str(row["store"])] = int(row["cnt"])

        return {
            "total_monitored_products": total_products,
            "total_active_deals": sum(counts_by_type.values()),
            "counts_by_type": counts_by_type,
            "counts_by_store": counts_by_store,
        }
