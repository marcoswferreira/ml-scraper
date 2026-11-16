"""Unit and integration tests for TelegramRelayClient, SentProductStore, and the continuous worker."""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from typing import Any

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

    # 1 hour later, same price -> ineligible
    assert (
        store.is_eligible(prod, cooldown_hours=72.0, now=t0 + timedelta(hours=1))
        is False
    )

    # 1 hour later, price dropped 15% (to 170.0) -> eligible again!
    cheaper = prod.model_copy(update={"price": 170.0})
    assert (
        store.is_eligible(cheaper, cooldown_hours=72.0, now=t0 + timedelta(hours=1))
        is True
    )

    # 73 hours later, same price -> eligible again!
    assert (
        store.is_eligible(prod, cooldown_hours=72.0, now=t0 + timedelta(hours=73))
        is True
    )


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
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
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

        # 1. Test sending photo + caption via /api/v1/messages/photo
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
        res = client.send_product_offer(offer, send_photo=True)
        assert res["ok"] is True
        assert len(received_requests) == 1
        assert received_requests[0]["path"] == "/api/v1/messages/photo"
        assert received_requests[0]["api_key"] == "secret-test-key"
        assert "multipart/form-data" in received_requests[0]["content_type"]
        assert "Fone Bluetooth Pro" in received_requests[0]["body_text"]
        assert "MOCKJPEGDATA" in received_requests[0]["body_text"]

        # 2. Test CLI `send-telegram` command
        links_file = tmp_path / "links.json"
        links_file.write_text(
            json.dumps([offer.model_dump(mode="json")]),
            encoding="utf-8",
        )
        history_file = tmp_path / "history.json"
        runner = CliRunner()
        cli_res = runner.invoke(
            app,
            [
                "send-telegram",
                "--input",
                str(links_file),
                "--api-url",
                base_url,
                "--api-key",
                "secret-test-key",
                "--send-delay",
                "0",
                "--history",
                str(history_file),
            ],
        )
        assert cli_res.exit_code == 0, cli_res.output
        assert "Enviados: 1" in cli_res.output
        assert len(received_requests) == 2
        saved_hist = json.loads(history_file.read_text(encoding="utf-8"))
        assert "MLB123456" in saved_hist
    finally:
        server.shutdown()
        server.server_close()
