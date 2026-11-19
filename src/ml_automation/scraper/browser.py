"""Shared Playwright browser context helper with automatic session cookie refresh."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

DEFAULT_HUB_URL = "https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true#menu-user"
DEFAULT_STORAGE_STATE = Path(".state/storage_state.json")
DEFAULT_NAV_TIMEOUT_MS = int(os.environ.get("ML_NAV_TIMEOUT_MS", "180000"))
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class SessionExpiredError(RuntimeError):
    """Raised when Mercado Livre redirects an authenticated request to login/auth."""


def _resolve_chromium_executable() -> Optional[str]:
    """Return system Chromium executable path if configured or present (e.g. Alpine Docker)."""
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and Path(env_path).is_file():
        return env_path
    for candidate in ("/usr/bin/chromium-browser", "/usr/bin/chromium"):
        if Path(candidate).is_file():
            return candidate
    return None


@asynccontextmanager
async def open_authenticated_context(
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    headless: bool = True,
    auto_refresh_state: bool = True,
    block_heavy_assets: bool = True,
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Launch Chromium with an authenticated Playwright storage_state and refresh cookies on exit."""
    if not storage_state_path.exists():
        raise FileNotFoundError(
            f"Arquivo de sessão não encontrado em '{storage_state_path}'. "
            "Execute 'ml-automation sync-cookies' primeiro."
        )

    async with async_playwright() as p:
        exec_path = _resolve_chromium_executable()
        browser = await p.chromium.launch(
            headless=headless,
            executable_path=exec_path,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-extensions",
                "--disable-background-networking",
                "--js-flags=--max-old-space-size=512",
            ],
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
        page.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
        if block_heavy_assets:

            async def _abort_heavy(route: object) -> None:
                req = getattr(route, "request", None)
                if req and getattr(req, "resource_type", None) in ("image", "media", "font"):
                    await route.abort()  # type: ignore[attr-defined]
                else:
                    await route.continue_()  # type: ignore[attr-defined]

            await page.route("**/*", _abort_heavy)
        session_valid = True
        try:
            yield context, page
        except SessionExpiredError:
            session_valid = False
            raise
        finally:
            if auto_refresh_state and session_valid:
                try:
                    current_url = (page.url or "").lower()
                    if current_url.startswith("http") and "login" not in current_url:
                        tmp_path = storage_state_path.with_name(
                            f".{storage_state_path.name}.{os.getpid()}.tmp"
                        )
                        await context.storage_state(path=str(tmp_path))
                        os.replace(tmp_path, storage_state_path)
                except Exception:
                    pass
            await context.close()
            await browser.close()
