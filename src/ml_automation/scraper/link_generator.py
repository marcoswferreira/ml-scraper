"""Affiliate link generator automation for Mercado Livre Affiliates Hub."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence, Union

from playwright.async_api import Page

from ml_automation.models.product import (
    AffiliateLinkResult,
    AffiliateProduct,
    build_promo_copy,
)
from ml_automation.scraper.browser import (
    DEFAULT_HUB_URL,
    DEFAULT_STORAGE_STATE,
    open_authenticated_context,
)

DEFAULT_LINKBUILDER_URL = "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"


def _attach_product_metadata(
    result: AffiliateLinkResult,
    product: AffiliateProduct | None,
) -> AffiliateLinkResult:
    if product is None:
        return result
    result.item_id = product.item_id
    result.title = product.title
    result.price = product.price
    result.original_price = product.original_price
    result.discount_pct = product.discount_pct
    result.commission_pct = product.commission_pct
    result.estimated_commission_brl = product.estimated_commission_brl
    result.rating = product.rating
    result.sold_text = product.sold_text
    result.highlight = product.highlight
    result.sales_score = product.sales_score
    result.image_url = product.image_url
    if result.status == "success" and (result.short_url or result.affiliate_url):
        link = result.short_url or result.affiliate_url or result.product_url
        result.promo_copy = build_promo_copy(
            title=product.title,
            price=product.price,
            original_price=product.original_price,
            discount_pct=product.discount_pct,
            rating=product.rating,
            sold_text=product.sold_text,
            highlight=product.highlight,
            link=link,
        )
        product.affiliate_url = result.affiliate_url
        product.short_url = result.short_url
        product.share_text = result.share_text
        product.promo_copy = result.promo_copy
    return result


async def _generate_single_link(
    page: Page,
    item: Union[str, AffiliateProduct],
) -> AffiliateLinkResult:
    """Attempt to generate an affiliate link for a single product URL or AffiliateProduct."""
    product = item if isinstance(item, AffiliateProduct) else None
    product_url = item.product_url if isinstance(item, AffiliateProduct) else str(item)

    input_selectors = (
        "textarea#url-0",
        'textarea[placeholder*="Ex:" i]',
        'input[placeholder*="link" i]',
        'input[placeholder*="url" i]',
        'input[placeholder*="cole" i]',
        'textarea[placeholder*="link" i]',
        'input[type="url"]',
    )
    target_input = None
    for selector in input_selectors:
        locator = page.locator(selector).first
        if await locator.count() > 0:
            target_input = locator
            break

    if target_input is None:
        return _attach_product_metadata(
            AffiliateLinkResult(
                product_url=product_url,
                status="error",
                error="Campo de entrada do gerador de links não encontrado. Rode 'ml-automation recon' para mapear o seletor.",
            ),
            product,
        )

    api_result: dict[str, Any] = {}

    async def _on_response(response: Any) -> None:
        if "linkbuilder/createLink" in response.url and response.status == 200:
            try:
                data = await response.json()
                urls = data.get("urls") or []
                if urls and isinstance(urls[0], dict):
                    api_result.update(urls[0])
            except Exception:
                pass

    page.on("response", _on_response)
    try:
        await target_input.click()
        await target_input.fill("")
        await target_input.fill(product_url)
        await page.wait_for_timeout(400)

        button_selectors = (
            "button.links-form__button",
            'button:has-text("Gerar")',
            'button:has-text("Criar")',
            'button:has-text("Encurtar")',
            'button[type="submit"]',
        )
        for btn_selector in button_selectors:
            btn = page.locator(btn_selector).first
            if await btn.count() > 0 and await btn.is_enabled():
                await btn.click()
                break

        await page.wait_for_timeout(2500)
    finally:
        page.remove_listener("response", _on_response)

    if api_result.get("short_url") or api_result.get("long_url"):
        short_url = api_result.get("short_url")
        long_url = api_result.get("long_url") or short_url
        share_text = api_result.get("text")
        return _attach_product_metadata(
            AffiliateLinkResult(
                product_url=product_url,
                affiliate_url=long_url,
                short_url=short_url,
                share_text=share_text,
                status="success",
            ),
            product,
        )

    generated = await page.evaluate(
        """() => {
            const candidates = Array.from(
                document.querySelectorAll(
                    'textarea[id^="textfield-copyLink"], input[readonly], input[value*="mercadolivre.com/sec"], a[href*="mercadolivre.com/sec"], [class*="generated"] input, [class*="result"] input'
                )
            );
            for (const el of candidates) {
                const val = el.value || el.href || el.innerText || '';
                if (val.includes('mercadolivre.com') || val.includes('meli.la')) {
                    return val.trim();
                }
            }
            return null;
        }"""
    )

    if not generated:
        return _attach_product_metadata(
            AffiliateLinkResult(
                product_url=product_url,
                status="error",
                error="Link gerado não detectado na página após submissão.",
            ),
            product,
        )

    return _attach_product_metadata(
        AffiliateLinkResult(
            product_url=product_url,
            affiliate_url=generated,
            short_url=generated if "/sec/" in generated or "meli.la" in generated else None,
            status="success",
        ),
        product,
    )


async def generate_affiliate_links(
    product_urls: Sequence[Union[str, AffiliateProduct]],
    hub_url: str = DEFAULT_HUB_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    headless: bool = True,
) -> list[AffiliateLinkResult]:
    """Generate affiliate links for a sequence of Mercado Livre product URLs or AffiliateProducts."""
    target_url = DEFAULT_LINKBUILDER_URL if hub_url == DEFAULT_HUB_URL else hub_url
    results: list[AffiliateLinkResult] = []
    async with open_authenticated_context(
        storage_state_path=storage_state_path, headless=headless
    ) as (_, page):
        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        for item in product_urls:
            results.append(await _generate_single_link(page, item))

    return results
