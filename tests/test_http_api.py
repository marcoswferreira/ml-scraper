"""Unit tests for the direct HTTP API client (MlHttpSession, _scrape_via_http_api, generate_links_via_http)."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ml_automation.scraper.browser import SessionExpiredError
from ml_automation.scraper.http_api import MlHttpSession, generate_links_via_http
from ml_automation.scraper.hub_scraper import _scrape_via_http_api


class _FakeResponse:
    def __init__(
        self, body: str, url: str = "https://www.mercadolivre.com.br/afiliados/hub"
    ) -> None:
        self._body = body.encode("utf-8")
        self._url = url

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _make_state_file(tmp_path: Path) -> Path:
    state_file = tmp_path / "storage_state.json"
    state_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "ssid",
                        "value": "ghy-123",
                        "domain": ".mercadolivre.com.br",
                        "path": "/",
                        "expires": 1800000000,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    return state_file


def test_ml_http_session_scrape_and_create_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = _make_state_file(tmp_path)

    hub_html = (
        '<html><head><meta name="csrf-token" content="csrf-xyz"></head>'
        '<body><script>{"tag":"marc0swferreira","in_use":true}</script></body></html>'
    )
    sample_polycard = {
        "metadata": {
            "id": "MLB7467427796",
            "product_id": "MLB78875859",
            "url": "www.mercadolivre.com.br/cascata-natal/p/MLB78875859",
            "extra_commission": "true",
        },
        "pictures": {"pictures": [{"id": "676961-MLA117051602166_102026"}]},
        "components": [
            {"type": "highlight", "highlight": {"text": "MAIS VENDIDO"}},
            {"type": "title", "title": {"text": "Cascata Natal 400 Leds"}},
            {
                "type": "review_compacted",
                "review_compacted": {
                    "values": [
                        {"type": "label", "label": {"text": "4.9"}},
                        {"type": "label", "label": {"text": "| +10mil vendidos"}},
                    ]
                },
            },
            {
                "type": "chip",
                "chip": {"pill": {"text": "{ganancia} {extra}"}, "label": {"text": "20%"}},
            },
            {
                "type": "price",
                "price": {
                    "previous_price": {"value": 249.9},
                    "current_price": {"value": 67.9},
                    "discount_label": {"text": "72% OFF"},
                },
            },
        ],
    }

    def fake_open(self: Any, req: Any, timeout: int = 25) -> _FakeResponse:
        url = req.full_url
        if "api/hub/search" in url:
            body = req.data.decode("utf-8") if req.data else "{}"
            if '"offset": 0' in body:
                return _FakeResponse(
                    json.dumps({"polycard_client_model": {"polycards": [sample_polycard]}})
                )
            return _FakeResponse(json.dumps({"polycard_client_model": {"polycards": []}}))
        if "affiliates/createLink" in url:
            return _FakeResponse(
                json.dumps(
                    {
                        "status": 200,
                        "urls": [
                            {
                                "short_url": "https://meli.la/fast123",
                                "long_url": "https://www.mercadolivre.com.br/social/marc0swferreira",
                                "text": "Acesse https://meli.la/fast123",
                            }
                        ],
                    }
                )
            )
        return _FakeResponse(hub_html)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_open)

    products = asyncio.run(
        _scrape_via_http_api(
            url="https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true",
            storage_state_path=state_file,
            max_scrolls=2,
            explore_tabs=True,
            quick_filter=None,
            category="Eletrônicos, Áudio e Vídeo",
            search_query="cascata",
            sort_by="relevance",
        )
    )
    assert len(products) == 1
    assert products[0].item_id == "MLB78875859"
    assert products[0].sales_score >= 90.0

    links = generate_links_via_http(products, storage_state_path=state_file)
    assert len(links) == 1
    assert links[0].status == "success"
    assert links[0].short_url == "https://meli.la/fast123"
    assert links[0].promo_copy is not None


def test_ml_http_session_expired_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_file = _make_state_file(tmp_path)
    session = MlHttpSession(storage_state_path=state_file)

    def fake_login_redirect(self: Any, req: Any, timeout: int = 25) -> _FakeResponse:
        return _FakeResponse("<html>Login</html>", url="https://www.mercadolivre.com.br/login")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", fake_login_redirect)
    with pytest.raises(SessionExpiredError):
        session.bootstrap_hub()
