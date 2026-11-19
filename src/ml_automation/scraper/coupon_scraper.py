"""Scraper and parser for Mercado Livre discount coupons (mercadolivre.com.br/cupons)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import Page

from ml_automation.models.coupon import (
    DEFAULT_COUPONS_URL,
    MercadoLivreCoupon,
    filter_relevant_coupons,
)
from ml_automation.scraper.browser import (
    DEFAULT_NAV_TIMEOUT_MS,
    DEFAULT_STORAGE_STATE,
    open_authenticated_context,
)

_PCT_OFF_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*(?:OFF|de\s+desconto)?", re.IGNORECASE)
_BRL_OFF_RE = re.compile(r"R\$\s*([\d.]+(?:\s*,\s*\d{2})?)\s*(?:OFF|de\s+desconto)?", re.IGNORECASE)
_MIN_PURCHASE_RE = re.compile(
    r"Compra\s+m[ií]nima(?:\s+de)?\s*R\$\s*([\d.]+(?:\s*,\s*\d{2})?)", re.IGNORECASE
)
_MAX_LIMIT_RE = re.compile(
    r"Limite\s+de\s*R\$\s*([\d.]+(?:\s*,\s*\d{2})?)", re.IGNORECASE
)
_EXPIRES_RE = re.compile(r"(Vence\s+[^|\n]+|V[áa]lido\s+at[ée]\s+[^|\n]+)", re.IGNORECASE)


def _parse_br_number(raw: str) -> float:
    cleaned = raw.replace(" ", "").replace(".", "").replace(",", ".")
    return float(cleaned)


def parse_coupon_card_data(
    raw: dict[str, Any],
    coupon_url: str = DEFAULT_COUPONS_URL,
) -> Optional[MercadoLivreCoupon]:
    """Parse raw DOM fields extracted from a .coupon-card into MercadoLivreCoupon."""
    title = " ".join(str(raw.get("title") or "").split()).strip()
    if not title:
        return None

    subtitles = [
        " ".join(str(s).split()).strip()
        for s in (raw.get("subtitles") or [])
        if str(s).strip()
    ]
    full_text = " ".join([title, *subtitles, str(raw.get("aria_label") or "")])

    discount_pct: Optional[float] = None
    discount_brl: Optional[float] = None
    discount_text: Optional[str] = None

    pct_match = _PCT_OFF_RE.search(title)
    if pct_match:
        discount_pct = float(pct_match.group(1).replace(",", "."))
        discount_text = f"{int(discount_pct) if discount_pct.is_integer() else discount_pct}% OFF"
    else:
        brl_match = _BRL_OFF_RE.search(title)
        if brl_match:
            discount_brl = _parse_br_number(brl_match.group(1))
            discount_text = f"R$ {brl_match.group(1).strip()} OFF"

    min_purchase_brl: Optional[float] = None
    max_discount_brl: Optional[float] = None
    expires_text: Optional[str] = None
    scope_parts: list[str] = []

    for sub in subtitles:
        min_m = _MIN_PURCHASE_RE.search(sub)
        if min_m:
            min_purchase_brl = _parse_br_number(min_m.group(1))
            continue

        max_m = _MAX_LIMIT_RE.search(sub)
        if max_m:
            max_discount_brl = _parse_br_number(max_m.group(1))
            continue

        exp_m = _EXPIRES_RE.search(sub)
        if exp_m:
            expires_text = exp_m.group(1).strip()
            continue

        if sub.lower() not in ("recomendados", "aplicar", "conferir"):
            scope_parts.append(sub)

    if expires_text is None:
        exp_full = _EXPIRES_RE.search(full_text)
        if exp_full:
            expires_text = exp_full.group(1).strip()

    scope_text = " · ".join(scope_parts) if scope_parts else None
    action_type = " ".join(str(raw.get("action_type") or "").split()).strip() or None
    image_url = str(raw.get("image_url") or "").strip() or None

    return MercadoLivreCoupon(
        title=title,
        discount_text=discount_text,
        discount_pct=discount_pct,
        discount_brl=discount_brl,
        scope_text=scope_text,
        min_purchase_brl=min_purchase_brl,
        max_discount_brl=max_discount_brl,
        expires_text=expires_text,
        action_type=action_type,
        image_url=image_url,
        coupon_url=coupon_url,
    )


async def extract_coupons_from_page(
    page: Page,
    coupon_url: str = DEFAULT_COUPONS_URL,
) -> list[MercadoLivreCoupon]:
    """Extract all visible .coupon-card items from the loaded /cupons page."""
    raw_cards = await page.evaluate(
        r"""() => {
            const cards = Array.from(document.querySelectorAll('.coupon-card'));
            return cards.map((card) => {
                const titleEl = card.querySelector('.title');
                const titleAttr = titleEl ? (titleEl.getAttribute('title') || '') : '';
                const titleSpan = titleEl ? (titleEl.querySelector('span:not(.sr-only)') || titleEl) : null;
                const title = (titleAttr || (titleSpan ? titleSpan.innerText : '') || '').replace(/\s+/g, ' ').trim();

                const subtitles = Array.from(card.querySelectorAll('.subtitles .subtitle, .footer .subtitle, [class*="expiration"], [class*="validity"]'))
                    .map((el) => (el.innerText || '').replace(/\s+/g, ' ').trim())
                    .filter(Boolean);

                // Fallback: capture any trailing card lines like "Vence ..." outside .subtitles
                const cardLines = (card.innerText || '')
                    .split('\n')
                    .map((l) => l.replace(/\s+/g, ' ').trim())
                    .filter((l) => /^Vence\s+/i.test(l) || /^V[áa]lido\s+/i.test(l));
                for (const line of cardLines) {
                    if (!subtitles.includes(line)) {
                        subtitles.push(line);
                    }
                }

                const btn = card.querySelector('button');
                const actionType = btn ? (btn.innerText || '').replace(/\s+/g, ' ').trim() : '';
                const ariaLabel = btn ? (btn.getAttribute('aria-label') || '') : '';

                const imgEl = card.querySelector('.badge image, image, img');
                const imageUrl = imgEl ? (imgEl.getAttribute('href') || imgEl.getAttribute('src') || '') : '';

                return {
                    title,
                    subtitles,
                    action_type: actionType,
                    aria_label: ariaLabel,
                    image_url: imageUrl,
                };
            });
        }"""
    )

    coupons: list[MercadoLivreCoupon] = []
    seen_ids: set[str] = set()
    for raw in raw_cards or []:
        parsed = parse_coupon_card_data(raw, coupon_url=coupon_url)
        if parsed and parsed.coupon_id not in seen_ids:
            seen_ids.add(parsed.coupon_id)
            coupons.append(parsed)
    return coupons


async def scrape_ml_coupons(
    url: str = DEFAULT_COUPONS_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    max_scrolls: int = 4,
    headless: bool = True,
    only_relevant: bool = True,
    focus_only: bool = True,
    min_discount_pct: float = 10.0,
    min_discount_brl: float = 15.0,
    min_score: float = 35.0,
) -> list[MercadoLivreCoupon]:
    """Scrape Mercado Livre coupons page using authenticated Playwright session."""
    async with open_authenticated_context(
        storage_state_path=storage_state_path,
        headless=headless,
        auto_refresh_state=False,
        block_heavy_assets=False,
    ) as (_, page):
        await page.goto(url, wait_until="domcontentloaded", timeout=min(DEFAULT_NAV_TIMEOUT_MS, 45_000))
        await page.wait_for_selector(".coupon-card", timeout=30_000)
        await page.wait_for_timeout(1500)

        for _ in range(max(0, max_scrolls)):
            await page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
            await page.wait_for_timeout(900)

        coupons = await extract_coupons_from_page(page, coupon_url=url)

    if only_relevant:
        return filter_relevant_coupons(
            coupons,
            min_discount_pct=min_discount_pct,
            min_discount_brl=min_discount_brl,
            min_score=min_score,
            exclude_new_follower=True,
            focus_only=focus_only,
        )
    return coupons
