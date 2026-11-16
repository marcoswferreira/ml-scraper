"""Integration and E2E tests for Playwright-based Hub scraping, UI interactions, and link generation."""

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright
from typer.testing import CliRunner

from ml_automation.cli import app
from ml_automation.scraper.hub_scraper import (
    apply_hub_category,
    apply_hub_search,
    apply_hub_sort,
    extract_products_from_page,
    toggle_hub_quick_filter,
)
from ml_automation.scraper.link_generator import _generate_single_link

MOCK_HUB_HTML = """
<!DOCTYPE html>
<html lang="pt-BR">
<head><title>Mercado Livre Afiliados Hub</title></head>
<body>
  <div class="recommendations-filters-desktop">
    <input class="andes-form-control__field" placeholder="Busque produtos" />
    <button id="best_seller" aria-pressed="false" onclick="this.setAttribute('aria-pressed', this.getAttribute('aria-pressed') === 'true' ? 'false' : 'true')">Mais vendidos</button>
    <button id="extra_commission" aria-pressed="false" onclick="this.setAttribute('aria-pressed', this.getAttribute('aria-pressed') === 'true' ? 'false' : 'true')">Ganhos extras</button>
    <button class="choice-selector__button">Categorias</button>
    <label class="radio-list__label">Eletrônicos, Áudio e Vídeo</label>
    <div class="recommendations-filters-desktop__right-box">
      <button class="choice-selector__button">Mais relevantes</button>
      <label class="radio-list__label">Menor preço</label>
    </div>
  </div>
  <section id="generator">
    <textarea id="url-0" placeholder="Ex: https://www.mercadolivre.com.br/"></textarea>
    <button type="button" class="links-form__button" onclick="document.getElementById('textfield-copyLink-1').value = 'https://meli.la/2xYz99'">Gerar</button>
    <textarea id="textfield-copyLink-1" readonly></textarea>
  </section>
  <main>
    <li class="poly-card">
      <span class="poly-component__highlight">MAIS VENDIDO</span>
      <a class="poly-component__title" href="https://produto.mercadolivre.com.br/MLB-4455667788-notebook-gamer-_JM">
        Notebook Gamer RTX 4060
      </a>
      <span class="poly-component__review-compacted">4.9 | +10mil vendidos</span>
      <div class="poly-price__current">
        <span class="andes-money-amount">R$ 5.499,90</span>
      </div>
      <s class="andes-money-amount poly-price__previous">R$ 6.999,00</s>
      <span class="poly-price__disc-label">21% OFF</span>
      <span class="poly-pill__pill">GANHOS EXTRAS 15%</span>
      <img src="https://http2.mlstatic.com/D_NQ_NP_12345.webp" />
    </li>
  </main>
</body>
</html>
"""


def test_playwright_extract_interactions_and_generate_link() -> None:
    async def _run() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_content(MOCK_HUB_HTML)

            # Test all Hub filter & search UI interactions
            assert await toggle_hub_quick_filter(page, "best_seller", active=True) is True
            assert await toggle_hub_quick_filter(page, "extra_commission", active=True) is True
            assert await apply_hub_search(page, "notebook") is True
            assert await apply_hub_category(page, "Eletrônicos, Áudio e Vídeo") is True
            assert await apply_hub_sort(page, "lowest_price") is True

            products = await extract_products_from_page(page, category_hint="Eletrônicos")
            assert len(products) == 1
            prod = products[0]
            assert prod.item_id == "MLB4455667788"
            assert prod.title == "Notebook Gamer RTX 4060"
            assert prod.price == 5499.90
            assert prod.original_price == 6999.00
            assert prod.discount_pct == 21.0
            assert prod.commission_pct == 15.0
            assert prod.extra_commission is True
            assert prod.rating == 4.9
            assert prod.sold_quantity == 10_000
            assert prod.highlight == "MAIS VENDIDO"
            assert prod.sales_score >= 75.0

            link_res = await _generate_single_link(page, prod)
            assert link_res.status == "success"
            assert link_res.affiliate_url == "https://meli.la/2xYz99"
            assert link_res.short_url == "https://meli.la/2xYz99"
            assert link_res.title == "Notebook Gamer RTX 4060"
            assert link_res.promo_copy is not None

            await browser.close()

    asyncio.run(_run())


def test_cli_end_to_end_workflow(tmp_path: Path) -> None:
    runner = CliRunner()

    cookies_file = tmp_path / "cookies_export.json"
    cookies_file.write_text(
        json.dumps([{"name": "ssid", "value": "test123", "domain": ".mercadolivre.com.br"}]),
        encoding="utf-8",
    )
    state_file = tmp_path / "storage_state.json"

    res_import = runner.invoke(
        app, ["import-cookies", "--input", str(cookies_file), "--output", str(state_file)]
    )
    assert res_import.exit_code == 0, res_import.output
    assert state_file.exists()

    mock_html = tmp_path / "hub.html"
    mock_html.write_text(MOCK_HUB_HTML, encoding="utf-8")
    mock_url = mock_html.as_uri()

    recon_dir = tmp_path / "recon"
    res_recon = runner.invoke(
        app,
        [
            "recon",
            "--url",
            mock_url,
            "--state",
            str(state_file),
            "--output-dir",
            str(recon_dir),
            "--headless",
        ],
    )
    assert res_recon.exit_code == 0, res_recon.output
    assert (recon_dir / "hub_screenshot.png").exists()
    assert (recon_dir / "hub_dom.html").exists()

    products_json = tmp_path / "produtos.json"
    res_scrape = runner.invoke(
        app,
        [
            "scrape",
            "--url",
            mock_url,
            "--state",
            str(state_file),
            "--output",
            str(products_json),
            "--max-scrolls",
            "1",
            "--search",
            "notebook",
            "--category",
            "Eletrônicos, Áudio e Vídeo",
            "--sort",
            "lowest_price",
            "--headless",
        ],
    )
    assert res_scrape.exit_code == 0, res_scrape.output
    scraped = json.loads(products_json.read_text(encoding="utf-8"))
    assert len(scraped) == 1
    assert scraped[0]["item_id"] == "MLB4455667788"
    assert scraped[0]["sales_score"] >= 75.0

    links_json = tmp_path / "links.json"
    res_links = runner.invoke(
        app,
        [
            "generate-links",
            "--input",
            str(products_json),
            "--output",
            str(links_json),
            "--url",
            mock_url,
            "--state",
            str(state_file),
            "--limit",
            "1",
            "--headless",
        ],
    )
    assert res_links.exit_code == 0, res_links.output
    generated = json.loads(links_json.read_text(encoding="utf-8"))
    assert len(generated) == 1
    assert generated[0]["status"] == "success"
    assert generated[0]["affiliate_url"] == "https://meli.la/2xYz99"
    assert generated[0]["title"] == "Notebook Gamer RTX 4060"
