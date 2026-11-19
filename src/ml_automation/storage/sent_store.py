"""Persistent deduplication store and SQLite historical price tracker for affiliate products."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from ml_automation.models.coupon import MercadoLivreCoupon
from ml_automation.models.product import (
    AffiliateLinkResult,
    AffiliateProduct,
    compute_sales_score,
)

DEFAULT_SENT_HISTORY_PATH = Path(".state/sent_history.json")
DEFAULT_SENT_COUPONS_PATH = Path(".state/sent_coupons.json")
DEFAULT_PRICE_DB_PATH = Path(".state/price_history.db")


class PriceHistoryStore:
    """SQLite store tracking product prices across scraping cycles to detect historical lows."""

    def __init__(self, db_path: Path = DEFAULT_PRICE_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_summary (
                    item_id TEXT PRIMARY KEY,
                    min_price REAL NOT NULL,
                    max_price REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    last_price REAL NOT NULL,
                    observations INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    price REAL NOT NULL,
                    observed_at TEXT NOT NULL
                )
                """
            )

    def record_and_evaluate(
        self,
        products: Sequence[AffiliateProduct],
        drop_below_avg_pct: float = 10.0,
    ) -> int:
        """Record current prices and mark products hitting a new historical low."""
        now_iso = datetime.now(timezone.utc).isoformat()
        lowest_count = 0

        with self._connect() as conn:
            for product in products:
                if product.price is None or product.price <= 0:
                    continue
                key = product.item_id or product.product_url
                curr_price = float(product.price)

                row = conn.execute(
                    "SELECT min_price, max_price, avg_price, last_price, observations FROM price_summary WHERE item_id = ?",
                    (key,),
                ).fetchone()

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO price_summary (item_id, min_price, max_price, avg_price, last_price, observations, updated_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?)
                        """,
                        (key, curr_price, curr_price, curr_price, curr_price, now_iso),
                    )
                else:
                    prev_min, prev_max, prev_avg, prev_last, obs = row
                    threshold_avg = prev_avg * (1.0 - drop_below_avg_pct / 100.0)
                    if curr_price < prev_min or curr_price <= threshold_avg:
                        product.is_lowest_price = True
                        product.lowest_price_seen = float(max(prev_last, prev_avg, prev_min))
                        product.sales_score = compute_sales_score(product)
                        lowest_count += 1

                    new_obs = int(obs) + 1
                    new_min = min(float(prev_min), curr_price)
                    new_max = max(float(prev_max), curr_price)
                    new_avg = round(((float(prev_avg) * int(obs)) + curr_price) / new_obs, 2)

                    conn.execute(
                        """
                        UPDATE price_summary
                        SET min_price = ?, max_price = ?, avg_price = ?, last_price = ?, observations = ?, updated_at = ?
                        WHERE item_id = ?
                        """,
                        (
                            new_min,
                            new_max,
                            new_avg,
                            curr_price,
                            new_obs,
                            now_iso,
                            key,
                        ),
                    )

                conn.execute(
                    "INSERT INTO price_observations (item_id, price, observed_at) VALUES (?, ?, ?)",
                    (key, curr_price, now_iso),
                )

        return lowest_count


class SentProductStore:
    """Tracks sent affiliate products to prevent duplicate posts in Telegram."""

    def __init__(self, path: Path = DEFAULT_SENT_HISTORY_PATH) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._records = data
            else:
                self._records = {}
        except Exception:
            self._records = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _key(self, item: AffiliateProduct | AffiliateLinkResult | str) -> str:
        if isinstance(item, str):
            return item
        return item.item_id or item.product_url

    def is_eligible(
        self,
        product: AffiliateProduct,
        cooldown_hours: float = 72.0,
        price_drop_threshold_pct: float = 10.0,
        now: Optional[datetime] = None,
    ) -> bool:
        """Return True if product has not been sent within cooldown_hours (unless price dropped >= 10%)."""
        key = self._key(product)
        entry = self._records.get(key)
        if not entry:
            return True

        # Allow re-sending if flagged as historical low or price dropped >= 10%
        if product.is_lowest_price:
            return True

        prev_price = entry.get("price")
        if (
            prev_price is not None
            and product.price is not None
            and prev_price > 0
            and product.price <= prev_price * (1.0 - price_drop_threshold_pct / 100.0)
        ):
            return True

        sent_at_str = entry.get("sent_at")
        if not sent_at_str:
            return False

        try:
            sent_at = datetime.fromisoformat(sent_at_str)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return True

        current_time = now or datetime.now(timezone.utc)
        return (current_time - sent_at) >= timedelta(hours=cooldown_hours)

    def mark_sent(
        self,
        item: AffiliateProduct | AffiliateLinkResult,
        message_id: Optional[int] = None,
        chat_id: Optional[str] = None,
        sent_at: Optional[datetime] = None,
    ) -> None:
        """Record a product as sent and persist immediately."""
        key = self._key(item)
        timestamp = (sent_at or datetime.now(timezone.utc)).isoformat()
        self._records[key] = {
            "item_id": getattr(item, "item_id", None),
            "title": getattr(item, "title", None),
            "price": getattr(item, "price", None),
            "short_url": getattr(item, "short_url", None),
            "affiliate_url": getattr(item, "affiliate_url", None),
            "message_id": message_id,
            "chat_id": chat_id,
            "sent_at": timestamp,
        }
        self.save()


class SentCouponStore:
    """Tracks sent Mercado Livre coupons to prevent duplicate posts in Telegram."""

    def __init__(self, path: Path = DEFAULT_SENT_COUPONS_PATH) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._records = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._records = data if isinstance(data, dict) else {}
        except Exception:
            self._records = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def is_eligible(
        self,
        coupon: MercadoLivreCoupon | str,
        cooldown_hours: float = 24.0,
        now: Optional[datetime] = None,
    ) -> bool:
        """Return True if coupon has not been sent within cooldown_hours."""
        key = coupon if isinstance(coupon, str) else coupon.coupon_id
        entry = self._records.get(key)
        if not entry:
            return True

        sent_at_str = entry.get("sent_at")
        if not sent_at_str:
            return False

        try:
            sent_at = datetime.fromisoformat(sent_at_str)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return True

        current_time = now or datetime.now(timezone.utc)
        return (current_time - sent_at) >= timedelta(hours=cooldown_hours)

    def mark_sent(
        self,
        coupon: MercadoLivreCoupon,
        message_id: Optional[int] = None,
        chat_id: Optional[str] = None,
        sent_at: Optional[datetime] = None,
    ) -> None:
        """Record a coupon as sent and persist immediately."""
        timestamp = (sent_at or datetime.now(timezone.utc)).isoformat()
        self._records[coupon.coupon_id] = {
            "coupon_id": coupon.coupon_id,
            "title": coupon.title,
            "category_match": coupon.category_match,
            "short_url": coupon.short_url,
            "affiliate_url": coupon.affiliate_url,
            "message_id": message_id,
            "chat_id": chat_id,
            "sent_at": timestamp,
        }
        self.save()
