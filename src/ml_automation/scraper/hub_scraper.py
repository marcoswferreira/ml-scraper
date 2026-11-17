"""Scraper and UI interaction engine for the Mercado Livre Affiliates Hub."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

from playwright.async_api import Page

from ml_automation.models.product import (
    AffiliateProduct,
    filter_high_converting_products,
)
from ml_automation.scraper.browser import (
    DEFAULT_HUB_URL,
    DEFAULT_NAV_TIMEOUT_MS,
    DEFAULT_STORAGE_STATE,
    SessionExpiredError,
    open_authenticated_context,
)
from ml_automation.scraper.http_api import CATEGORY_IDS, MlHttpSession

_PRICE_RE = re.compile(r"R\$\s*([\d.]+)(?:\s*,\s*(\d{2}))?")
_DISCOUNT_RE = re.compile(r"(\d+)\s*%")
_COMMISSION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_RATING_RE = re.compile(r"\b([1-5][.,]\d)\b")
_SOLD_RE = re.compile(r"\+?\s*(\d+(?:[.,]\d+)?)\s*(mil|milh[õo]es|k)?\s*vendidos", re.IGNORECASE)

SORT_LABELS: dict[str, str] = {
    "relevance": "Mais relevantes",
    "lowest_price": "Menor preço",
    "highest_price": "Maior preço",
}

PROFITABLE_CATEGORIES: tuple[str, ...] = (
    "Eletrônicos, Áudio e Vídeo",
    "Celulares e Telefones",
    "Eletrodomésticos",
    "Informática",
    "Casa, Móveis e Decoração",
    "Beleza e Cuidado Pessoal",
    "Ferramentas",
    "Esportes e Fitness",
)


def parse_brl_price(text: Optional[str]) -> Optional[float]:
    """Parse a Brazilian Real price string (e.g. 'R$ 1.299,90') into float."""
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    integer_part = match.group(1).replace(".", "")
    decimal_part = match.group(2) or "00"
    return float(f"{integer_part}.{decimal_part}")


def parse_discount_pct(text: Optional[str]) -> Optional[float]:
    """Parse discount percentage from text (e.g. '25% OFF') into float."""
    if not text:
        return None
    match = _DISCOUNT_RE.search(text)
    return float(match.group(1)) if match else None


def parse_commission_info(text: Optional[str]) -> tuple[Optional[float], bool]:
    """Extract (commission_pct, extra_commission) from commission chip text."""
    if not text:
        return None, False
    match = _COMMISSION_RE.search(text)
    pct = float(match.group(1).replace(",", ".")) if match else None
    is_extra = "EXTRA" in text.upper()
    return pct, is_extra


def parse_review_and_sold(
    text: Optional[str],
) -> tuple[Optional[float], Optional[str], Optional[int]]:
    """Extract (rating, sold_text, sold_quantity) from compact review text (e.g. '4.9 | +10mil vendidos')."""
    if not text:
        return None, None, None

    rating: Optional[float] = None
    rating_match = _RATING_RE.search(text)
    if rating_match:
        rating = float(rating_match.group(1).replace(",", "."))

    sold_text: Optional[str] = None
    sold_qty: Optional[int] = None
    sold_match = _SOLD_RE.search(text)
    if sold_match:
        sold_text = sold_match.group(0).strip()
        base_num = float(sold_match.group(1).replace(",", "."))
        multiplier_word = (sold_match.group(2) or "").lower()
        if multiplier_word in ("mil", "k"):
            sold_qty = int(base_num * 1_000)
        elif multiplier_word.startswith("milh"):
            sold_qty = int(base_num * 1_000_000)
        else:
            sold_qty = int(base_num)

    return rating, sold_text, sold_qty


def parse_polycard_from_api(
    polycard: dict[str, Any],
    category_hint: Optional[str] = None,
) -> Optional[AffiliateProduct]:
    """Convert a raw polycard JSON item from /affiliate-program/api/hub/search into AffiliateProduct."""
    meta = polycard.get("metadata") or {}
    raw_url = meta.get("url") or ""
    if not raw_url:
        return None
    url_params = meta.get("url_params") or ""
    url_fragments = meta.get("url_fragments") or ""
    product_url = (
        raw_url if raw_url.startswith("http") else f"https://{raw_url}{url_params}{url_fragments}"
    )

    item_id = meta.get("product_id") or meta.get("id")
    extra_comm = str(meta.get("extra_commission", "")).lower() == "true"

    pictures = (polycard.get("pictures") or {}).get("pictures") or []
    pic_id = pictures[0].get("id") if pictures and isinstance(pictures[0], dict) else None
    image_url = f"https://http2.mlstatic.com/D_Q_NP_2X_{pic_id}-AB.webp" if pic_id else None

    title = ""
    highlight: Optional[str] = None
    coupon_text: Optional[str] = None
    rating: Optional[float] = None
    sold_text: Optional[str] = None
    sold_qty: Optional[int] = None
    commission_text: Optional[str] = None
    commission_pct: Optional[float] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None

    for comp in polycard.get("components") or []:
        comp_type = comp.get("type")
        comp_id = comp.get("id") or ""
        if comp_type == "title":
            title = (comp.get("title") or {}).get("text") or ""
        elif comp_type == "highlight":
            raw_hl = (comp.get("highlight") or {}).get("text") or ""
            highlight = re.sub(r"\{[^}]+\}\s*", "", raw_hl).strip() or None
        elif comp_type in ("coupons", "coupon", "rebates"):
            c_obj = comp.get(comp_type) or {}
            raw_cp = c_obj.get("text") or ""
            coupon_text = re.sub(r"\{[^}]+\}\s*", "", raw_cp).strip() or None
        elif comp_type == "review_compacted":
            rev = comp.get("review_compacted") or {}
            values = rev.get("values") or []
            parts = [
                (v.get("label") or {}).get("text", "")
                for v in values
                if isinstance(v, dict) and v.get("type") == "label"
            ]
            combined_rev = " ".join(p for p in parts if p)
            rating, sold_text, sold_qty = parse_review_and_sold(combined_rev)
        elif comp_type == "chip":
            chip = comp.get("chip") or {}
            pill = chip.get("pill") or {}
            pill_text = pill.get("text") or ""
            if "coupon" in comp_id.lower() or "cupom" in pill_text.lower():
                coupon_text = re.sub(r"\{[^}]+\}\s*", "", pill_text).strip() or None
                continue
            if "{ganancia}" in pill_text:
                pill_text = "GANHOS EXTRAS"
            label_text = ((chip.get("label") or {}).get("text") or "").strip()
            commission_text = f"{pill_text} {label_text}".strip()
            commission_pct, parsed_extra = parse_commission_info(commission_text)
            extra_comm = extra_comm or parsed_extra
        elif comp_type == "price":
            pr = comp.get("price") or {}
            curr = pr.get("current_price") or {}
            prev = pr.get("previous_price") or {}
            disc = pr.get("discount_label") or {}
            if curr.get("value") is not None:
                price = float(curr["value"])
            if prev.get("value") is not None:
                original_price = float(prev["value"])
            discount_pct = parse_discount_pct(disc.get("text"))

    if not title or not product_url:
        return None

    return AffiliateProduct(
        item_id=item_id,
        title=title.strip(),
        price=price,
        original_price=original_price,
        discount_pct=discount_pct,
        commission_text=commission_text,
        commission_pct=commission_pct,
        extra_commission=extra_comm,
        rating=rating,
        sold_text=sold_text,
        sold_quantity=sold_qty,
        highlight=highlight,
        coupon_text=coupon_text,
        product_url=product_url,
        image_url=image_url,
        category=category_hint,
    )


async def extract_products_from_page(
    page: Page,
    category_hint: Optional[str] = None,
) -> list[AffiliateProduct]:
    """Extract affiliate product cards from the current Hub page using resilient selectors."""
    raw_items = await page.evaluate(
        r"""() => {
            const cardSelectors = [
                'li.poly-card',
                'article.poly-card',
                '[data-testid*="product"]',
                '[class*="affiliate-card"]',
                '.poly-card',
                '.andes-card',
                'article'
            ];
            const mapCard = (card) => {
                const linkEl =
                    card.querySelector('a.poly-component__title') ||
                    card.querySelector('a[href*="mercadolivre"], a[href*="MLB"], a[href]');
                const titleEl =
                    card.querySelector('a.poly-component__title') ||
                    card.querySelector('h2, h3, [class*="title"], [class*="name"]');
                const priceEl =
                    card.querySelector('.poly-price__current .andes-money-amount') ||
                    card.querySelector('.andes-money-amount:not(s):not(del)') ||
                    card.querySelector('[class*="price"]:not(s):not(del)');
                const origPriceEl =
                    card.querySelector('s.andes-money-amount, .poly-price__previous') ||
                    card.querySelector('s, del, [class*="original"], [class*="previous"]');
                const discountEl =
                    card.querySelector('.poly-price__disc-label, .andes-money-amount__discount') ||
                    card.querySelector('[class*="discount"], [class*="off"]');
                const commissionEl =
                    card.querySelector('.poly-component__chip, .poly-pill__pill') ||
                    card.querySelector('[class*="commission"], [class*="comissao"], [class*="ganho"]');
                const reviewEl =
                    card.querySelector('.poly-component__review-compacted') ||
                    card.querySelector('[class*="review"], [class*="rating"]');
                const highlightEl =
                    card.querySelector('.poly-component__highlight') ||
                    card.querySelector('[class*="highlight"]');
                const couponEl =
                    card.querySelector('.poly-component__coupons, [class*="coupon"], [class*="cupom"]');
                const imgEl = card.querySelector('img');
                return {
                    title: titleEl ? titleEl.innerText.replace(/\s+/g, ' ').trim() : '',
                    product_url: linkEl ? linkEl.href : '',
                    price_text: priceEl ? priceEl.innerText.trim() : '',
                    orig_price_text: origPriceEl ? origPriceEl.innerText.trim() : '',
                    discount_text: discountEl ? discountEl.innerText.trim() : '',
                    commission_text: commissionEl ? commissionEl.innerText.replace(/\s+/g, ' ').trim() : null,
                    review_text: reviewEl ? reviewEl.innerText.replace(/\s+/g, ' ').trim() : null,
                    highlight_text: highlightEl ? highlightEl.innerText.replace(/\s+/g, ' ').trim() : null,
                    coupon_text: couponEl ? couponEl.innerText.replace(/\s+/g, ' ').trim() : null,
                    image_url: imgEl ? (imgEl.src || imgEl.dataset.src || null) : null,
                };
            };
            for (const sel of cardSelectors) {
                const found = Array.from(document.querySelectorAll(sel));
                const mapped = found.map(mapCard).filter(item => item.title && item.product_url);
                if (mapped.length > 0) {
                    return mapped;
                }
            }
            return [];
        }"""
    )

    products: list[AffiliateProduct] = []
    seen_urls: set[str] = set()
    for item in raw_items:
        url = item["product_url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        comm_text = item.get("commission_text")
        comm_pct, is_extra = parse_commission_info(comm_text)
        rating, sold_text, sold_qty = parse_review_and_sold(item.get("review_text"))
        products.append(
            AffiliateProduct(
                title=item["title"],
                product_url=url,
                price=parse_brl_price(item.get("price_text")),
                original_price=parse_brl_price(item.get("orig_price_text")),
                discount_pct=parse_discount_pct(item.get("discount_text")),
                commission_text=comm_text,
                commission_pct=comm_pct,
                extra_commission=is_extra,
                rating=rating,
                sold_text=sold_text,
                sold_quantity=sold_qty,
                highlight=item.get("highlight_text") or None,
                coupon_text=item.get("coupon_text") or None,
                image_url=item.get("image_url"),
                category=category_hint,
            )
        )
    return products


async def _wait_tags_ready(page: Page) -> None:
    """Wait until Hub filter tags are no longer in disabled loading state."""
    try:
        await page.wait_for_function(
            "() => !document.querySelector('.recommendations-filters-desktop .andes-tag--Loading')",
            timeout=8_000,
        )
    except Exception:
        pass
    await page.wait_for_timeout(1200)


async def toggle_hub_quick_filter(
    page: Page,
    filter_id: Literal["best_seller", "extra_commission"],
    active: bool = True,
) -> bool:
    """Toggle 'Mais vendidos' (#best_seller) or 'Ganhos extras' (#extra_commission) on the Hub."""
    other_id = "extra_commission" if filter_id == "best_seller" else "best_seller"
    other_btn = page.locator(f"button#{other_id}").first
    if active and await other_btn.count() > 0:
        if await other_btn.get_attribute("aria-pressed") == "true":
            await other_btn.click()
            await _wait_tags_ready(page)

    target_btn = page.locator(f"button#{filter_id}").first
    if await target_btn.count() == 0:
        return False

    is_pressed = (await target_btn.get_attribute("aria-pressed")) == "true"
    if is_pressed != active:
        await target_btn.click()
        await _wait_tags_ready(page)
    return True


async def apply_hub_search(page: Page, query: str) -> bool:
    """Type a search query into the Affiliates Hub product search bar and submit."""
    search_input = page.locator(
        '.recommendations-filters-desktop input[placeholder*="Busque"]'
    ).first
    if await search_input.count() == 0:
        return False

    if not await search_input.is_visible():
        trigger = page.locator(
            ".recommendations-filters-desktop .andes-form-control__search-trigger"
        ).first
        if await trigger.count() > 0 and await trigger.is_visible():
            await trigger.click()
            await page.wait_for_timeout(300)

    await search_input.fill(query)
    await search_input.press("Enter")
    await page.wait_for_timeout(1800)
    return True


async def apply_hub_category(page: Page, category_name: str) -> bool:
    """Open the Hub 'Categorias' dropdown and select a category by name."""
    cat_btn = page.locator(
        '.recommendations-filters-desktop button.choice-selector__button:has-text("Categorias")'
    ).first
    if await cat_btn.count() == 0:
        return False

    await cat_btn.click()
    await page.wait_for_timeout(600)

    option = page.locator(f'.radio-list__label:has-text("{category_name}")').first
    if await option.count() == 0:
        await page.keyboard.press("Escape")
        return False

    await option.click()
    await page.wait_for_timeout(1800)
    return True


async def apply_hub_sort(
    page: Page,
    sort_by: Literal["relevance", "lowest_price", "highest_price"],
) -> bool:
    """Change the Hub sort order ('Mais relevantes', 'Menor preço', 'Maior preço')."""
    label_text = SORT_LABELS.get(sort_by)
    if not label_text:
        return False

    sort_btn = page.locator(
        ".recommendations-filters-desktop__right-box button.choice-selector__button"
    ).first
    if await sort_btn.count() == 0:
        return False

    await sort_btn.click()
    await page.wait_for_timeout(600)

    option = page.locator(f'.radio-list__label:has-text("{label_text}")').first
    if await option.count() == 0:
        return False

    await option.click()
    await page.wait_for_timeout(1800)
    return True


async def _scroll_and_collect(
    page: Page,
    collected: dict[str, AffiliateProduct],
    max_scrolls: int,
    category_hint: Optional[str] = None,
) -> None:
    """Scroll down the page in steps, collecting cards from the DOM at each step."""
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(600)

    for item in await extract_products_from_page(page, category_hint=category_hint):
        key = item.item_id or item.product_url
        collected.setdefault(key, item)

    for _ in range(max_scrolls):
        await page.evaluate("window.scrollBy(0, 1100)")
        await page.wait_for_timeout(1200)
        for item in await extract_products_from_page(page, category_hint=category_hint):
            key = item.item_id or item.product_url
            collected.setdefault(key, item)


async def _scrape_via_http_api(
    url: str,
    storage_state_path: Path,
    max_scrolls: int,
    explore_tabs: bool,
    quick_filter: Optional[Literal["best_seller", "extra_commission"]],
    category: Optional[str],
    search_query: Optional[str],
    sort_by: Optional[Literal["relevance", "lowest_price", "highest_price"]],
) -> list[AffiliateProduct]:
    """Fast HTTP-only Hub scraping using authenticated cookies and /api/hub/search."""
    session = MlHttpSession(storage_state_path=storage_state_path)
    session.bootstrap_hub(url)

    base_filters: list[dict[str, str]] = []
    if category and category in CATEGORY_IDS:
        base_filters.append({"id": "category", "value": CATEGORY_IDS[category], "name": category})

    collected: dict[str, AffiliateProduct] = {}
    pages_to_fetch = max(1, min(max_scrolls, 6))
    sort_id = sort_by or "relevance"
    query_str = search_query or ""

    filter_sets: list[list[dict[str, str]]] = []
    if quick_filter:
        filter_sets.append([*base_filters, {"id": quick_filter}])
    else:
        filter_sets.append(base_filters)
        if explore_tabs:
            filter_sets.append([*base_filters, {"id": "best_seller"}])
            filter_sets.append([*base_filters, {"id": "extra_commission"}])

    for active_filters in filter_sets:
        for page_idx in range(pages_to_fetch):
            offset = page_idx * 18
            try:
                polycards = session.search_hub(
                    search=query_str,
                    sort=sort_id,
                    filters=active_filters,
                    offset=offset,
                )
            except Exception:
                break
            if not polycards:
                break
            for pc in polycards:
                parsed = parse_polycard_from_api(pc, category_hint=category)
                if parsed is not None:
                    key = parsed.item_id or parsed.product_url
                    collected.setdefault(key, parsed)

    session.save_refreshed_cookies()
    return list(collected.values())


async def scrape_affiliate_hub(
    url: str = DEFAULT_HUB_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    max_scrolls: int = 8,
    headless: bool = True,
    only_high_converting: bool = False,
    explore_tabs: bool = False,
    quick_filter: Optional[Literal["best_seller", "extra_commission"]] = None,
    category: Optional[str] = None,
    search_query: Optional[str] = None,
    sort_by: Optional[Literal["relevance", "lowest_price", "highest_price"]] = None,
    min_rating: float = 4.5,
    min_sold: int = 500,
    min_commission_pct: float = 8.0,
    min_discount_pct: float = 15.0,
    min_sales_score: float = 55.0,
) -> list[AffiliateProduct]:
    """Open the Mercado Livre Affiliates Hub, interact with filters/scroll, and extract products."""
    if "mercadolivre.com.br" in url:
        try:
            all_products = await _scrape_via_http_api(
                url=url,
                storage_state_path=storage_state_path,
                max_scrolls=max_scrolls,
                explore_tabs=explore_tabs,
                quick_filter=quick_filter,
                category=category,
                search_query=search_query,
                sort_by=sort_by,
            )
            if all_products:
                if only_high_converting:
                    return filter_high_converting_products(
                        all_products,
                        min_rating=min_rating,
                        min_sold=min_sold,
                        min_commission_pct=min_commission_pct,
                        min_discount_pct=min_discount_pct,
                        min_sales_score=min_sales_score,
                    )
                return sorted(
                    all_products,
                    key=lambda p: (p.is_lowest_price, p.sales_score),
                    reverse=True,
                )
        except SessionExpiredError:
            raise
        except Exception:
            # Fall back to Playwright browser automation if direct HTTP API fails
            pass

    collected: dict[str, AffiliateProduct] = {}

    async with open_authenticated_context(
        storage_state_path=storage_state_path, headless=headless
    ) as (_, page):

        async def _on_search_response(response: Any) -> None:
            if "api/hub/search" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    polycards = (data.get("polycard_client_model") or {}).get("polycards") or []
                    for pc in polycards:
                        parsed = parse_polycard_from_api(pc, category_hint=category)
                        if parsed is not None:
                            key = parsed.item_id or parsed.product_url
                            collected.setdefault(key, parsed)
                except Exception:
                    pass

        page.on("response", _on_search_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_NAV_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        if "login" in page.url.lower() or "auth" in page.url.lower():
            raise SessionExpiredError(
                "Sessão expirada ou não autenticada. Faça login no Brave e rode 'sync-cookies'."
            )

        try:
            await page.wait_for_selector("li.poly-card, .poly-card, article", timeout=30_000)
        except Exception:
            pass

        if search_query:
            await apply_hub_search(page, search_query)
        if category:
            await apply_hub_category(page, category)
        if sort_by:
            await apply_hub_sort(page, sort_by)
        if quick_filter:
            await toggle_hub_quick_filter(page, quick_filter, active=True)

        await _scroll_and_collect(page, collected, max_scrolls=max_scrolls, category_hint=category)

        if explore_tabs and not quick_filter:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(500)
            if await toggle_hub_quick_filter(page, "best_seller", active=True):
                await _scroll_and_collect(
                    page,
                    collected,
                    max_scrolls=max(3, max_scrolls // 2),
                    category_hint=category,
                )
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                await toggle_hub_quick_filter(page, "best_seller", active=False)

            if await toggle_hub_quick_filter(page, "extra_commission", active=True):
                await _scroll_and_collect(
                    page,
                    collected,
                    max_scrolls=max(3, max_scrolls // 2),
                    category_hint=category,
                )
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                await toggle_hub_quick_filter(page, "extra_commission", active=False)

    all_products = list(collected.values())
    if only_high_converting:
        return filter_high_converting_products(
            all_products,
            min_rating=min_rating,
            min_sold=min_sold,
            min_commission_pct=min_commission_pct,
            min_discount_pct=min_discount_pct,
            min_sales_score=min_sales_score,
        )

    return sorted(all_products, key=lambda p: (p.is_lowest_price, p.sales_score), reverse=True)
