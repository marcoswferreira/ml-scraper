"""Affiliate link generator automation for Mercado Livre Affiliates Hub (Direct API + UI fallback)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence, Union

from playwright.async_api import Page

from ml_automation.models.product import (
    AffiliateLinkResult,
    AffiliateProduct,
    build_promo_copy,
    extract_item_id,
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
    result.coupon_text = product.coupon_text
    result.lowest_price_seen = product.lowest_price_seen
    result.is_lowest_price = product.is_lowest_price
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
            coupon_text=product.coupon_text,
            is_lowest_price=product.is_lowest_price,
            lowest_price_seen=product.lowest_price_seen,
        )
        product.affiliate_url = result.affiliate_url
        product.short_url = result.short_url
        product.share_text = result.share_text
        product.promo_copy = result.promo_copy
    return result


async def _generate_link_via_api(
    page: Page,
    product_url: str,
    item_id: Optional[str] = None,
    extra_commission: bool = False,
) -> Optional[dict[str, Any]]:
    """Attempt fast direct link generation via POST /affiliate-program/api/v2/affiliates/createLink."""
    if not (page.url or "").startswith("http"):
        return None

    clean_url = product_url.split("#")[0].split("?")[0]
    if clean_url.startswith("https://"):
        clean_url = clean_url[len("https://") :]
    elif clean_url.startswith("http://"):
        clean_url = clean_url[len("http://") :]

    resolved_id = item_id or extract_item_id(product_url) or ""

    try:
        resp_data = await page.evaluate(
            """async ({ cleanUrl, itemId, extraComm }) => {
                const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const tagBtn = document.querySelector('.andes-dropdown__trigger');
                let tag = tagBtn ? tagBtn.innerText.trim() : '';
                if (!tag) {
                    const htmlText = document.documentElement.innerHTML;
                    const m = htmlText.match(/"tag"\\s*:\\s*"([^"]+)"\\s*,\\s*"in_use"\\s*:\\s*true/);
                    if (m) tag = m[1];
                }
                if (!tag) return null;

                const payload = {
                    itemId: itemId,
                    type: "product",
                    extraCommission: extraComm ? "true" : "false",
                    itemAddToList: itemId,
                    tag: tag,
                    urls: [cleanUrl]
                };
                const headers = { "Content-Type": "application/json" };
                if (csrf) headers["x-csrf-token"] = csrf;

                const resp = await fetch("/affiliate-program/api/v2/affiliates/createLink", {
                    method: "POST",
                    headers,
                    body: JSON.stringify(payload)
                });
                if (!resp.ok) return null;
                const data = await resp.json();
                if (data && Array.isArray(data.urls) && data.urls.length > 0) {
                    return data.urls[0];
                }
                return null;
            }""",
            {
                "cleanUrl": clean_url,
                "itemId": resolved_id,
                "extraComm": extra_commission,
            },
        )
        if isinstance(resp_data, dict) and (
            resp_data.get("short_url") or resp_data.get("long_url")
        ):
            return resp_data
    except Exception:
        pass
    return None


async def _generate_single_link(
    page: Page,
    item: Union[str, AffiliateProduct],
    prefer_direct_api: bool = True,
) -> AffiliateLinkResult:
    """Generate an affiliate link via direct API first, falling back to the Linkbuilder UI."""
    product = item if isinstance(item, AffiliateProduct) else None
    product_url = item.product_url if isinstance(item, AffiliateProduct) else str(item)

    if prefer_direct_api:
        direct = await _generate_link_via_api(
            page,
            product_url=product_url,
            item_id=product.item_id if product else None,
            extra_commission=bool(product.extra_commission) if product else False,
        )
        if direct:
            short_url = direct.get("short_url")
            long_url = direct.get("long_url") or short_url
            return _attach_product_metadata(
                AffiliateLinkResult(
                    product_url=product_url,
                    affiliate_url=long_url,
                    short_url=short_url,
                    share_text=direct.get("text"),
                    status="success",
                ),
                product,
            )

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
