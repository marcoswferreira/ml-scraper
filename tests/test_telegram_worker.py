"""Unit and E2E tests for TelegramRelayClient, PriceHistoryStore, SentProductStore, and the continuous watch worker."""

import asyncio
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
from ml_automation.scheduler.worker import is_quiet_hours, run_worker_cycle
from ml_automation.storage.sent_store import PriceHistoryStore, SentProductStore


def test_format_telegram_html_escapes_and_formats_offer_with_coupon_and_lowest_price() -> None:
    item = AffiliateLinkResult(
        item_id="MLB4667709197",
        title="Jaqueta <Puffer> & Corta Vento",
        price=58.61,
        original_price=129.90,
        discount_pct=54.0,
        rating=4.8,
        sold_text="+10mil vendidos",
        highlight="MAIS VENDIDO",
        coupon_text="R$ 15 OFF",
        is_lowest_price=True,
        lowest_price_seen=89.90,
        product_url="https://produto.mercadolivre.com.br/MLB-4667709197",
        short_url="https://meli.la/1bv8pCX",
        status="success",
    )
    rendered = format_telegram_html(item)
    assert "🚨 <b>MENOR PREÇO MONITORADO!</b> (Histórico: R$ 89,90)" in rendered
    assert "<b>[MAIS VENDIDO] Jaqueta &lt;Puffer&gt; &amp; Corta Vento</b>" in rendered
    assert "⭐ 4.8 (+10mil vendidos)" in rendered
    assert "❌ De: <s>R$ 129,90</s>" in rendered
    assert "✅ Por: <b>R$ 58,61</b> <i>(54% OFF)</i>" in rendered
    assert "🎟️ <b>Cupom:</b> <code>R$ 15 OFF</code>" in rendered
    assert 'href="https://meli.la/1bv8pCX"' in rendered


def test_price_history_store_detects_historical_low(tmp_path: Path) -> None:
    db_file = tmp_path / "prices.db"
    price_store = PriceHistoryStore(db_path=db_file)

    p1 = AffiliateProduct(
        item_id="MLB777",
        title="Monitor Gamer 144Hz",
        price=999.0,
        product_url="https://produto.mercadolivre.com.br/MLB-777",
    )
    assert price_store.record_and_evaluate([p1]) == 0
    assert p1.is_lowest_price is False

    # Next cycle: price drops to 799.0 -> flagged as historical lowest price!
    p2 = AffiliateProduct(
        item_id="MLB777",
        title="Monitor Gamer 144Hz",
        price=799.0,
        product_url="https://produto.mercadolivre.com.br/MLB-777",
    )
    assert price_store.record_and_evaluate([p2]) == 1
    assert p2.is_lowest_price is True
    assert p2.lowest_price_seen == 999.0


def test_quiet_hours_and_pause_mode(tmp_path: Path) -> None:
    # 03:00 BRT == 06:00 UTC (inside quiet hours 00:00-08:00 BRT)
    dt_quiet = datetime(2026, 11, 16, 6, 0, 0, tzinfo=timezone.utc)
    # 14:00 BRT == 17:00 UTC (outside quiet hours)
    dt_active = datetime(2026, 11, 16, 17, 0, 0, tzinfo=timezone.utc)

    assert is_quiet_hours(now=dt_quiet) is True
    assert is_quiet_hours(now=dt_active) is False

    stats = asyncio.run(
        run_worker_cycle(
            quiet_hours_mode="pause",
            now=dt_quiet,
            sent_history_path=tmp_path / "hist.json",
            price_db_path=tmp_path / "prices.db",
        )
    )
    assert stats["paused_quiet_hours"] is True
    assert stats["sent"] == 0


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
                html_with_tag = MOCK_HUB_HTML.replace(
                    "<head>",
                    '<head><meta name="csrf-token" content="csrf123"><script>window.__STATE__ = {"tag":"marc0swferreira","in_use":true};</script>',
                )
                self.wfile.write(html_with_tag.encode("utf-8"))
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

            if self.path == "/affiliate-program/api/v2/affiliates/createLink":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "status": 200,
                            "urls": [
                                {
                                    "short_url": "https://meli.la/directApi99",
                                    "long_url": "https://www.mercadolivre.com.br/social/marc0swferreira?ref=direct",
                                    "text": "Acesse https://meli.la/directApi99",
                                }
                            ],
                        }
                    ).encode("utf-8")
                )
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

        # 2. Session expired alert
        alert_res = client.send_session_expired_alert()
        assert alert_res is not None and alert_res["ok"] is True
        assert "Sessão do Mercado Livre Expirada" in received_requests[-1]["body_text"]

        # 3. Error handling on 400
        with pytest.raises(RuntimeError, match="Erro HTTP 400"):
            client._request_with_retry(f"{base_url}/fail", body=b"{}", headers={}, max_retries=0)

        # 4. E2E CLI `watch --max-cycles 1` using Direct API link generation + PriceHistoryStore
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
        price_db = tmp_path / "prices.db"

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
                "--quiet-hours-mode",
                "off",
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
                "--price-db",
                str(price_db),
                "--headless",
            ],
        )
        assert watch_res.exit_code == 0, watch_res.output
        assert "Finalizado após 1 ciclo(s)" in watch_res.output
        links_data = json.loads(links_out.read_text(encoding="utf-8"))
        assert links_data[0]["short_url"] == "https://meli.la/directApi99"
    finally:
        server.shutdown()
        server.server_close()
