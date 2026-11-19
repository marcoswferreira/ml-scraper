"""Tests for Mercado Livre coupon scraping, filtering, deduplication, formatting, and dispatch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ml_automation.integrations.telegram_client import format_coupon_telegram_html
from ml_automation.models.coupon import (
    MercadoLivreCoupon,
    classify_coupon_focus,
    filter_relevant_coupons,
)
from ml_automation.scheduler.worker import run_coupon_cycle
from ml_automation.scraper.coupon_scraper import parse_coupon_card_data
from ml_automation.storage.sent_store import SentCouponStore


def test_parse_coupon_card_data_percentage_and_limits() -> None:
    raw = {
        "title": "15% OFF em Celulares e Smartphones",
        "subtitles": [
            "Recomendados",
            "Em produtos selecionados",
            "Compra mínima R$ 500",
            "Limite de R$ 150",
            "Vence 31 de dezembro",
        ],
        "action_type": "Aplicar",
        "aria_label": "Aplicar cupom 15 por cento OFF",
        "image_url": "https://http2.mlstatic.com/D_Q_NP_123.jpg",
    }
    coupon = parse_coupon_card_data(raw)
    assert coupon is not None
    assert coupon.discount_pct == 15.0
    assert coupon.discount_text == "15% OFF"
    assert coupon.min_purchase_brl == 500.0
    assert coupon.max_discount_brl == 150.0
    assert coupon.expires_text == "Vence 31 de dezembro"
    assert coupon.scope_text == "Em produtos selecionados"
    assert coupon.action_type == "Aplicar"
    assert coupon.category_match == "tecnologia"
    assert len(coupon.coupon_id) == 16


def test_parse_coupon_card_data_brl_and_empty_title() -> None:
    assert parse_coupon_card_data({"title": "   "}) is None

    raw = {
        "title": "R$ 30 OFF em Tênis e Roupas Esportivas",
        "subtitles": ["Compra mínima R$ 199,90", "Vence hoje"],
        "action_type": "Conferir",
    }
    coupon = parse_coupon_card_data(raw)
    assert coupon is not None
    assert coupon.discount_brl == 30.0
    assert coupon.min_purchase_brl == 199.90
    assert coupon.category_match == "roupas"


def test_classify_and_filter_focus_tecnologia_e_roupas() -> None:
    assert classify_coupon_focus("10% OFF Notebooks Dell") == "tecnologia"
    assert classify_coupon_focus("R$ 40 OFF Moda e Calçados Nike") == "roupas"
    assert classify_coupon_focus("10% OFF em todo o site") == "geral"
    assert classify_coupon_focus("10% OFF Ração para Cães") is None

    coupons = [
        MercadoLivreCoupon(title="R$ 3 OFF Novo seguidor", discount_brl=3.0, scope_text="Em roupas"),
        MercadoLivreCoupon(title="15% OFF Ração Pet", discount_pct=15.0),
        MercadoLivreCoupon(title="35% OFF Supermercado Mega", discount_pct=35.0),
        MercadoLivreCoupon(title="15% OFF Notebooks e Celulares", discount_pct=15.0, action_type="Aplicar"),
        MercadoLivreCoupon(title="R$ 40 OFF Tênis Nike e Roupas", discount_brl=40.0),
        MercadoLivreCoupon(title="10% OFF em todo o site", discount_pct=10.0),
    ]
    filtered = filter_relevant_coupons(coupons, focus_only=True)
    titles = [c.title for c in filtered]
    assert "R$ 3 OFF Novo seguidor" not in titles
    assert "15% OFF Ração Pet" not in titles
    assert "35% OFF Supermercado Mega" in titles
    assert "15% OFF Notebooks e Celulares" in titles
    assert "R$ 40 OFF Tênis Nike e Roupas" in titles
    assert "10% OFF em todo o site" in titles
    # Tech & fashion must rank ahead of geral and mega other
    assert titles.index("15% OFF Notebooks e Celulares") < titles.index("10% OFF em todo o site")


def test_sent_coupon_store_cooldown(tmp_path: Path) -> None:
    store_path = tmp_path / "sent_coupons.json"
    store = SentCouponStore(path=store_path)
    coupon = MercadoLivreCoupon(title="20% OFF Fones JBL", discount_pct=20.0)

    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert store.is_eligible(coupon, cooldown_hours=24.0, now=now) is True

    store.mark_sent(coupon, message_id=42, chat_id="-100123", sent_at=now)

    # Reload from disk
    reloaded = SentCouponStore(path=store_path)
    assert reloaded.is_eligible(coupon, cooldown_hours=24.0, now=now + timedelta(hours=12)) is False
    assert reloaded.is_eligible(coupon, cooldown_hours=24.0, now=now + timedelta(hours=25)) is True


def test_format_coupon_telegram_html() -> None:
    coupon = MercadoLivreCoupon(
        title="15% OFF <Smartphones> & Fones",
        discount_pct=15.0,
        scope_text="Em produtos selecionados",
        min_purchase_brl=300.0,
        max_discount_brl=100.0,
        expires_text="Vence 30 de novembro",
        short_url="https://mercadolivre.com/sec/abc123",
    )
    rendered = format_coupon_telegram_html(coupon)
    assert "CUPOM MERCADO LIVRE" in rendered
    assert "TECNOLOGIA" in rendered
    assert "15% OFF &lt;Smartphones&gt; &amp; Fones" in rendered
    assert "Compra mínima: R$ 300,00" in rendered
    assert "Limite: R$ 100,00" in rendered
    assert "https://mercadolivre.com/sec/abc123" in rendered


def test_run_coupon_cycle_dispatches_and_deduplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_coupons = [
        MercadoLivreCoupon(
            title="20% OFF Notebooks Gamer",
            discount_pct=20.0,
            scope_text="Em produtos selecionados",
            action_type="Aplicar",
        ),
        MercadoLivreCoupon(
            title="R$ 50 OFF Camisetas e Tênis",
            discount_brl=50.0,
            scope_text="Em moda",
            action_type="Conferir",
        ),
    ]

    async def fake_scrape(**kwargs: object) -> list[MercadoLivreCoupon]:
        return sample_coupons

    async def fake_gen_links(**kwargs: object) -> list[object]:
        return []

    sent_payloads: list[str] = []

    class FakeTelegramClient:
        def __init__(self, **kwargs: object) -> None:
            self.base_url = "http://fake"

        def check_health(self) -> bool:
            return True

        def send_coupon_offer(self, coupon: MercadoLivreCoupon, disable_notification: bool = False) -> dict:
            sent_payloads.append(coupon.title)
            return {"message_id": len(sent_payloads), "chat_id": "-100999"}

    monkeypatch.setattr("ml_automation.scheduler.worker.scrape_ml_coupons", fake_scrape)
    monkeypatch.setattr("ml_automation.scheduler.worker.generate_affiliate_links", fake_gen_links)
    monkeypatch.setattr("ml_automation.scheduler.worker.TelegramRelayClient", FakeTelegramClient)

    sent_file = tmp_path / "sent_coupons.json"
    out_file = tmp_path / "cupons.json"

    first = asyncio.run(
        run_coupon_cycle(
            coupons_output=out_file,
            sent_coupons_path=sent_file,
            send_delay_seconds=0.0,
            logger=lambda _: None,
        )
    )
    assert first["coupons_scraped"] == 2
    assert first["coupons_eligible"] == 2
    assert first["coupons_sent"] == 2
    assert len(sent_payloads) == 2
    assert out_file.exists()

    # Second cycle should deduplicate both coupons
    second = asyncio.run(
        run_coupon_cycle(
            coupons_output=out_file,
            sent_coupons_path=sent_file,
            send_delay_seconds=0.0,
            logger=lambda _: None,
        )
    )
    assert second["coupons_eligible"] == 0
    assert second["coupons_sent"] == 0
    assert len(sent_payloads) == 2
