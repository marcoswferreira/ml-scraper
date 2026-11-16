"""Shared Playwright browser context helper."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, Page, async_playwright

DEFAULT_HUB_URL = "https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true#menu-user"
DEFAULT_STORAGE_STATE = Path(".state/storage_state.json")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@asynccontextmanager
async def open_authenticated_context(
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    headless: bool = True,
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Launch Chromium with an authenticated Playwright storage_state."""
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Arquivo de sessão não encontrado em '{storage_state_path}'. "
            "Execute 'ml-automation sync-cookies' primeiro."
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            storage_state=str(storage_state_path),
            locale="pt-BR",
            user_agent=DEFAULT_USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        try:
            yield context, page
        finally:
            await context.close()
            await browser.close()
