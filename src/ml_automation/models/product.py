"""Pydantic models and conversion scoring for Mercado Livre affiliate products and generated links."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Iterable, Optional

from pydantic import BaseModel, Field, field_validator


_MLB_PATTERN = re.compile(r"(MLB-?\d+)", re.IGNORECASE)
_HIGH_CONVERTING_BADGES = {
    "MAIS VENDIDO",
    "OFERTA IMPERDÍVEL",
    "OFERTA DO DIA",
    "BLACK FRIDAY",
    "MAIS BUSCADO",
    "MAIS COMPARTILHADO",
}


def extract_item_id(url: str) -> Optional[str]:
    """Extract normalized Mercado Livre item ID (e.g. MLB123456789) from a URL."""
    match = _MLB_PATTERN.search(url)
    if not match:
        return None
    return match.group(1).replace("-", "").upper()


class AffiliateProduct(BaseModel):
    """Represents a product scraped from the Mercado Livre Affiliates Hub."""

    item_id: Optional[str] = None
    title: str
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    commission_text: Optional[str] = None
    commission_pct: Optional[float] = None
    extra_commission: bool = False
    estimated_commission_brl: Optional[float] = None
    rating: Optional[float] = None
    sold_text: Optional[str] = None
    sold_quantity: Optional[int] = None
    highlight: Optional[str] = None
    sales_score: float = 0.0
    product_url: str
    affiliate_url: Optional[str] = None
    short_url: Optional[str] = None
    share_text: Optional[str] = None
    promo_copy: Optional[str] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("item_id", mode="before")
    @classmethod
    def _normalize_item_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.replace("-", "").upper()

    def model_post_init(self, __context: object) -> None:
        if not self.item_id and self.product_url:
            self.item_id = extract_item_id(self.product_url)
        if (
            self.estimated_commission_brl is None
            and self.price is not None
            and self.commission_pct is not None
        ):
            self.estimated_commission_brl = round(self.price * (self.commission_pct / 100.0), 2)
        if self.sales_score == 0.0:
            self.sales_score = compute_sales_score(self)
        if not self.promo_copy and (self.short_url or self.affiliate_url):
            self.promo_copy = build_promo_copy(
                title=self.title,
                price=self.price,
                original_price=self.original_price,
                discount_pct=self.discount_pct,
                rating=self.rating,
                sold_text=self.sold_text,
                highlight=self.highlight,
                link=self.short_url or self.affiliate_url or self.product_url,
            )


def _fmt_brl(val: float) -> str:
    formatted = f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def build_promo_copy(
    title: str,
    price: Optional[float],
    original_price: Optional[float],
    discount_pct: Optional[float],
    rating: Optional[float],
    sold_text: Optional[str],
    highlight: Optional[str],
    link: str,
) -> str:
    """Build a ready-to-share promotional message for WhatsApp / Telegram."""
    lines: list[str] = []
    badge = f"🔥 [{highlight}] " if highlight else "🔥 "
    lines.append(f"{badge}{title}")

    if rating or sold_text:
        proof_parts = []
        if rating:
            proof_parts.append(f"⭐ {rating}")
        if sold_text:
            proof_parts.append(f"({sold_text})")
        lines.append(" ".join(proof_parts))

    if original_price and price and original_price > price:
        lines.append(f"De: ~{_fmt_brl(original_price)}~")
    if price:
        disc_str = f" ({int(discount_pct)}% OFF)" if discount_pct else ""
        lines.append(f"Por: *{_fmt_brl(price)}*{disc_str}")

    lines.append(f"🛒 Compre aqui: {link}")
    return "\n".join(lines)


def compute_sales_score(product: AffiliateProduct) -> float:
    """Compute a 0-100 conversion & profitability score for an affiliate product.

    Weights:
    - Proven sales volume (sold_quantity): up to 35 pts
    - Discount attractiveness (discount_pct): up to 25 pts
    - Commission rate & extra commission bonus: up to 20 pts
    - Customer rating (rating >= 4.0): up to 10 pts
    - Highlight badge (MAIS VENDIDO / OFERTA IMPERDÍVEL / etc.): up to 10 pts
    """
    score = 0.0

    # 1. Sales volume (0 - 35 pts)
    sold = product.sold_quantity or 0
    if sold >= 10_000:
        score += 35.0
    elif sold >= 5_000:
        score += 30.0
    elif sold >= 1_000:
        score += 25.0
    elif sold >= 500:
        score += 18.0
    elif sold >= 100:
        score += 10.0
    elif sold > 0:
        score += 5.0

    # 2. Discount percentage (0 - 25 pts)
    discount = product.discount_pct or 0.0
    score += min(25.0, discount * 0.5)

    # 3. Affiliate commission % and Extra Commission bonus (0 - 20 pts)
    comm = product.commission_pct or 0.0
    score += min(15.0, comm * 0.6)
    if product.extra_commission:
        score += 5.0

    # 4. Customer rating quality (0 - 10 pts)
    if product.rating is not None and product.rating >= 4.0:
        score += min(10.0, max(0.0, (product.rating - 4.0) * 10.0))

    # 5. Highlight badge boost (0 - 10 pts)
    if product.highlight:
        hl_upper = product.highlight.upper()
        if any(b in hl_upper for b in ("MAIS VENDIDO", "OFERTA IMPERDÍVEL", "BLACK FRIDAY")):
            score += 10.0
        elif any(b in hl_upper for b in _HIGH_CONVERTING_BADGES):
            score += 8.0
        else:
            score += 4.0

    return round(min(100.0, score), 1)


def is_high_converting_product(
    product: AffiliateProduct,
    min_rating: float = 4.5,
    min_sold: int = 500,
    min_commission_pct: float = 8.0,
    min_discount_pct: float = 15.0,
    min_sales_score: float = 55.0,
) -> bool:
    """Return True if a product meets the criteria for high-probability affiliate sales."""
    if product.price is None or product.price <= 0:
        return False

    if product.rating is not None and product.rating < min_rating:
        return False

    sold = product.sold_quantity or 0
    if sold < min_sold:
        return False

    comm = product.commission_pct or 0.0
    if comm < min_commission_pct:
        return False

    discount = product.discount_pct or 0.0
    has_strong_badge = bool(
        product.highlight
        and any(b in product.highlight.upper() for b in _HIGH_CONVERTING_BADGES)
    )
    if discount < min_discount_pct and not product.extra_commission and not has_strong_badge:
        return False

    return product.sales_score >= min_sales_score


def filter_high_converting_products(
    products: Iterable[AffiliateProduct],
    min_rating: float = 4.5,
    min_sold: int = 500,
    min_commission_pct: float = 8.0,
    min_discount_pct: float = 15.0,
    min_sales_score: float = 55.0,
) -> list[AffiliateProduct]:
    """Filter and rank products by sales_score descending."""
    curated = [
        p
        for p in products
        if is_high_converting_product(
            p,
            min_rating=min_rating,
            min_sold=min_sold,
            min_commission_pct=min_commission_pct,
            min_discount_pct=min_discount_pct,
            min_sales_score=min_sales_score,
        )
    ]
    return sorted(
        curated,
        key=lambda p: (
            p.sales_score,
            p.sold_quantity or 0,
            p.discount_pct or 0.0,
            p.commission_pct or 0.0,
        ),
        reverse=True,
    )


class AffiliateLinkResult(BaseModel):
    """Represents the output of the affiliate link generator for a product URL."""

    item_id: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    commission_pct: Optional[float] = None
    estimated_commission_brl: Optional[float] = None
    rating: Optional[float] = None
    sold_text: Optional[str] = None
    highlight: Optional[str] = None
    sales_score: Optional[float] = None
    product_url: str
    affiliate_url: Optional[str] = None
    short_url: Optional[str] = None
    share_text: Optional[str] = None
    promo_copy: Optional[str] = None
    image_url: Optional[str] = None
    status: str = "pending"
    error: Optional[str] = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
