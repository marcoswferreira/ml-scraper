"""Unit and E2E tests for TelegramRelayClient, SentProductStore, and the continuous watch worker."""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_playwright_pipeline import MOCK_HUB_HTML
from typer.testing import CliRunner

from ml_automation.cli import app
from ml_automation.integrations.telegram_client import (
    TelegramRelayClient,
    format_telegram_html,
)
from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct
from ml_automation.storage.sent_store import SentProductStore


def test_format_telegram_html_escapes_and_formats_offer() -> None:
    item = AffiliateLinkResult(
        item_id="MLB4667709197",
        title="Jaqueta <Puffer> & Corta Vento",
        price=58.61,
        original_price=129.90,
        discount_pct=54.0,
        rating=4.8,
        sold_text="+10mil vendidos",
        highlight="MAIS VENDIDO",
        product_url="https://produto.mercadolivre.com.br/MLB-4667709197",
        short_url="https://meli.la/1bv8pCX",
        status="success",
    )
    rendered = format_telegram_html(item)
    assert "<b>[MAIS VENDIDO] Jaqueta &lt;Puffer&gt; &amp; Corta Vento</b>" in rendered
    assert "⭐ 4.8 (+10mil vendidos)" in rendered
    assert "❌ De: <s>R$ 129,90</s>" in rendered
    assert "✅ Por: <b>R$ 58,61</b> <i>(54% OFF)</i>" in rendered
    assert 'href="https://meli.la/1bv8pCX"' in rendered


def test_sent_product_store_cooldown_and_price_drop(tmp_path: Path) -> None:
    history_file = tmp_path / "sent_history.json"
    history_file.write_text("invalid-json", encoding="utf-8")
    store = SentProductStore(path=history_file)

    t0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)
    prod = AffiliateProduct(
        item_id="MLB999",
        title="Cafeteira Expresso",
        price=200.0,
        product_url="https://produto.mercadolivre.com.br/MLB-999",
    )

    assert store.is_eligible(prod, cooldown_hours=72.0, now=t0) is True
    store.mark_sent(prod, message_id=10, chat_id="-100123", sent_at=t0)

    # Reload from disk
    reloaded = SentProductStore(path=history_file)
    assert reloaded.is_eligible(prod, cooldown_hours=72.0, now=t0 + timedelta(hours=1)) is False

    # Price dropped 15% (to 170.0) -> eligible again
    cheaper = prod.model_copy(update={"price": 170.0})
    assert reloaded.is_eligible(cheaper, cooldown_hours=72.0, now=t0 + timedelta(hours=1)) is True

    # 73 hours later -> eligible again
    assert reloaded.is_eligible(prod, cooldown_hours=72.0, now=t0 + timedelta(hours=73)) is True


def test_telegram_client_and_watch_worker_end_to_end(tmp_path: Path) -> None:
    received_requests: list[dict[str, Any]] = []

    class MockTelegramBotHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            elif self.path == "/mock-image.jpg":
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(b"\xff\xd8\xff\xe0MOCKJPEGDATA")
            elif self.path == "/hub":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(MOCK_HUB_HTML.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            if self.path == "/fail":
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"bad request"}')
                return

            received_requests.append(
                {
                    "path": self.path,
                    "api_key": self.headers.get("X-API-Key"),
                    "content_type": self.headers.get("Content-Type", ""),
                    "body_text": raw_body.decode("utf-8", errors="ignore"),
                }
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "ok": True,
                "message_id": 100 + len(received_requests),
                "chat_id": "-100999888",
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

    server = HTTPServer(("127.0.0.1", 0), MockTelegramBotHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"
        client = TelegramRelayClient(
            base_url=base_url,
            api_key="secret-test-key",
            chat_id="-100999888",
        )
        assert client.check_health() is True

        offline_client = TelegramRelayClient(base_url="http://127.0.0.1:1", timeout=0.5)
        assert offline_client.check_health() is False

        offer = AffiliateLinkResult(
            item_id="MLB123456",
            title="Fone Bluetooth Pro",
            price=99.90,
            original_price=199.90,
            discount_pct=50.0,
            rating=4.9,
            sold_text="+10mil vendidos",
            highlight="MAIS VENDIDO",
            product_url="https://produto.mercadolivre.com.br/MLB-123456",
            short_url="https://meli.la/test99",
            image_url=f"{base_url}/mock-image.jpg",
            status="success",
        )

        # 1. Photo + HTML caption via /api/v1/messages/photo
        res = client.send_product_offer(offer, send_photo=True)
        assert res["ok"] is True
        assert received_requests[-1]["path"] == "/api/v1/messages/photo"
        assert "multipart/form-data" in received_requests[-1]["content_type"]

        # 2. Text-only fallback via /api/v1/messages
        res_text = client.send_product_offer(offer, send_photo=False)
        assert res_text["ok"] is True
        assert received_requests[-1]["path"] == "/api/v1/messages"

        # 3. Error handling on 400
        with pytest.raises(RuntimeError, match="Erro HTTP 400"):
            client._request_with_retry(f"{base_url}/fail", body=b"{}", headers={}, max_retries=0)

        # 4. E2E CLI `watch --max-cycles 1` against the mock Hub + mock Telegram API
        state_file = tmp_path / "storage_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "ssid",
                            "value": "123",
                            "domain": "127.0.0.1",
                            "path": "/",
                        }
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )
        products_out = tmp_path / "produtos.json"
        links_out = tmp_path / "produtos_com_links.json"
        history_file = tmp_path / "sent_history.json"

        runner = CliRunner()
        watch_res = runner.invoke(
            app,
            [
                "watch",
                "--max-cycles",
                "1",
                "--batch-size",
                "3",
                "--max-scrolls",
                "1",
                "--send-delay",
                "0",
                "--url",
                f"{base_url}/hub",
                "--api-url",
                base_url,
                "--api-key",
                "secret-test-key",
                "--state",
                str(state_file),
                "--products-output",
                str(products_out),
                "--links-output",
                str(links_out),
                "--history",
                str(history_file),
                "--headless",
            ],
        )
        assert watch_res.exit_code == 0, watch_res.output
        assert "Finalizado após 1 ciclo(s)" in watch_res.output
        assert products_out.exists()
        assert links_out.exists()
        assert history_file.exists()
        saved_hist = json.loads(history_file.read_text(encoding="utf-8"))
        assert "MLB4455667788" in saved_hist
    finally:
        server.shutdown()
        server.server_close()
