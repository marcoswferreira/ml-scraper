"""Persistent deduplication store for products already sent to Telegram."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Optional

from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct

DEFAULT_SENT_HISTORY_PATH = Path(".state/sent_history.json")


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

        # Allow re-sending if price dropped significantly (>= 10% cheaper than last sent price)
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
