"""Pydantic models, category focus matching, and relevance scoring for Mercado Livre coupons."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, Optional

from pydantic import BaseModel, Field

DEFAULT_COUPONS_URL = "https://www.mercadolivre.com.br/cupons"

_TECH_KEYWORDS: tuple[str, ...] = (
    "tecnologia",
    "eletronico",
    "eletronicos",
    "celular",
    "celulares",
    "smartphone",
    "iphone",
    "samsung",
    "xiaomi",
    "motorola",
    "notebook",
    "laptop",
    "informatica",
    "computador",
    "gamer",
    "game",
    "games",
    "playstation",
    "xbox",
    "nintendo",
    "televisao",
    "smart tv",
    "monitor",
    "tablet",
    "ipad",
    "fone",
    "headset",
    "audio",
    "caixa de som",
    "jbl",
    "smartwatch",
    "camera",
    "impressora",
    "ssd",
    "placa de video",
    "teclado",
    "mouse",
    "roteador",
    "maquininha",
    "apple",
    "lenovo",
    "asus",
    "acer",
    "dell",
    "philips",
    "sony",
)

_FASHION_KEYWORDS: tuple[str, ...] = (
    "roupa",
    "roupas",
    "moda",
    "vestuario",
    "calcado",
    "calcados",
    "tenis",
    "sapato",
    "sandalia",
    "bota",
    "camisa",
    "camiseta",
    "blusa",
    "jaqueta",
    "casaco",
    "calca",
    "jeans",
    "bermuda",
    "vestido",
    "saia",
    "moletom",
    "lingerie",
    "cueca",
    "bolsa",
    "mochila",
    "oculos",
    "nike",
    "adidas",
    "puma",
    "reserva",
    "hering",
    "levis",
    "lacoste",
    "vans",
    "converse",
    "mizuno",
    "asics",
    "olympikus",
    "under armour",
    "oakley",
    "urban denim",
    "malwee",
)

_GENERAL_SITE_KEYWORDS: tuple[str, ...] = (
    "todo o site",
    "em todo o site",
    "qualquer produto",
    "no app",
    "meli+",
)


def _normalize_text(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _contains_keyword(normalized_text: str, keywords: tuple[str, ...]) -> bool:
    for kw in keywords:
        if kw in normalized_text:
            return True
    return False


def classify_coupon_focus(title: str, scope_text: Optional[str] = None) -> Optional[str]:
    """Classify a coupon into 'tecnologia', 'roupas', 'geral', or None."""
    combined = _normalize_text(f"{title} {scope_text or ''}")
    if _contains_keyword(combined, _TECH_KEYWORDS):
        return "tecnologia"
    if _contains_keyword(combined, _FASHION_KEYWORDS):
        return "roupas"
    if _contains_keyword(combined, _GENERAL_SITE_KEYWORDS):
        return "geral"
    return None


def build_coupon_id(
    title: str,
    scope_text: Optional[str] = None,
    min_purchase_brl: Optional[float] = None,
    expires_text: Optional[str] = None,
) -> str:
    """Build a deterministic ID for deduplicating coupons across scraping cycles."""
    raw = "|".join(
        [
            _normalize_text(title).strip(),
            _normalize_text(scope_text or "").strip(),
            f"{min_purchase_brl:.2f}" if min_purchase_brl is not None else "",
            _normalize_text(expires_text or "").strip(),
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class MercadoLivreCoupon(BaseModel):
    """Represents a discount coupon scraped from Mercado Livre."""

    coupon_id: str = ""
    title: str
    discount_text: Optional[str] = None
    discount_pct: Optional[float] = None
    discount_brl: Optional[float] = None
    scope_text: Optional[str] = None
    min_purchase_brl: Optional[float] = None
    max_discount_brl: Optional[float] = None
    expires_text: Optional[str] = None
    action_type: Optional[str] = None
    category_match: Optional[str] = None
    image_url: Optional[str] = None
    coupon_url: str = DEFAULT_COUPONS_URL
    affiliate_url: Optional[str] = None
    short_url: Optional[str] = None
    score: float = 0.0
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_post_init(self, __context: object) -> None:
        if not self.coupon_id:
            self.coupon_id = build_coupon_id(
                title=self.title,
                scope_text=self.scope_text,
                min_purchase_brl=self.min_purchase_brl,
                expires_text=self.expires_text,
            )
        if self.category_match is None:
            self.category_match = classify_coupon_focus(self.title, self.scope_text)
        if self.score == 0.0:
            self.score = compute_coupon_score(self)


def compute_coupon_score(coupon: MercadoLivreCoupon) -> float:
    """Compute a 0-100 relevance score prioritizing technology, fashion, and strong discounts."""
    score = 0.0

    if coupon.discount_pct is not None and coupon.discount_pct > 0:
        score += min(float(coupon.discount_pct), 40.0) * 1.0  # up to 40 pts

    if coupon.discount_brl is not None and coupon.discount_brl > 0:
        score += min(float(coupon.discount_brl), 200.0) * 0.25  # up to 50 pts

    if coupon.category_match in ("tecnologia", "roupas"):
        score += 30.0
    elif coupon.category_match == "geral":
        score += 25.0

    if (coupon.action_type or "").strip().lower() == "aplicar":
        score += 5.0

    if (
        coupon.min_purchase_brl is not None
        and coupon.min_purchase_brl >= 1000.0
        and (coupon.discount_brl or 0.0) < 50.0
        and (coupon.discount_pct or 0.0) < 15.0
    ):
        score -= 10.0

    return round(max(0.0, min(score, 100.0)), 2)


def filter_relevant_coupons(
    coupons: Iterable[MercadoLivreCoupon],
    min_discount_pct: float = 10.0,
    min_discount_brl: float = 15.0,
    min_score: float = 35.0,
    exclude_new_follower: bool = True,
    focus_only: bool = True,
) -> list[MercadoLivreCoupon]:
    """Filter and rank coupons, focusing on technology, fashion, and high-value sitewide offers."""
    filtered: list[MercadoLivreCoupon] = []
    for coupon in coupons:
        norm_title = _normalize_text(coupon.title)
        norm_scope = _normalize_text(coupon.scope_text or "")
        if exclude_new_follower and ("novo seguidor" in norm_title or "novo seguidor" in norm_scope):
            continue

        has_pct = coupon.discount_pct is not None and coupon.discount_pct >= min_discount_pct
        has_brl = coupon.discount_brl is not None and coupon.discount_brl >= min_discount_brl
        if not (has_pct or has_brl):
            continue

        if focus_only and coupon.category_match not in ("tecnologia", "roupas", "geral"):
            continue

        if coupon.score < min_score:
            continue

        filtered.append(coupon)

    filtered.sort(
        key=lambda c: (
            c.category_match in ("tecnologia", "roupas"),
            c.score,
            c.discount_pct or 0.0,
            c.discount_brl or 0.0,
        ),
        reverse=True,
    )
    return filtered
