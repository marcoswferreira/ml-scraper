"""Reconnaissance script for Mercado Livre Affiliates Hub (reconnaissance-then-action)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ml_automation.scraper.browser import (
    DEFAULT_HUB_URL,
    DEFAULT_STORAGE_STATE,
    open_authenticated_context,
)


async def run_recon(
    url: str = DEFAULT_HUB_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    output_dir: Path = Path("output/recon"),
    headless: bool = False,
) -> dict[str, Any]:
    """Navigate to the Affiliates Hub, capture screenshot/DOM, and log XHR/Fetch calls."""
    output_dir.mkdir(parents=True, exist_ok=True)
    network_log: list[dict[str, Any]] = []

    async with open_authenticated_context(
        storage_state_path=storage_state_path, headless=headless
    ) as (_, page):
        async def _on_response(response: Any) -> None:
            req = response.request
            if req.resource_type in ("xhr", "fetch"):
                entry: dict[str, Any] = {
                    "url": response.url,
                    "method": req.method,
                    "status": response.status,
                }
                if "json" in response.headers.get("content-type", ""):
                    try:
                        entry["json_preview"] = await response.json()
                    except Exception:
                        pass
                network_log.append(entry)

        page.on("response", _on_response)

        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            # SPAs sometimes keep background analytics sockets open; proceed after timeout.
            pass

        final_url = page.url
        title = await page.title()

        screenshot_path = output_dir / "hub_screenshot.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)

        html_path = output_dir / "hub_dom.html"
        html_path.write_text(await page.content(), encoding="utf-8")

        network_path = output_dir / "network_xhr.json"
        network_path.write_text(
            json.dumps(network_log, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        is_login_redirect = "login" in final_url.lower() or "auth" in final_url.lower()
        return {
            "final_url": final_url,
            "title": title,
            "authenticated": not is_login_redirect,
            "xhr_count": len(network_log),
            "screenshot": str(screenshot_path),
            "dom_html": str(html_path),
            "network_log": str(network_path),
        }
