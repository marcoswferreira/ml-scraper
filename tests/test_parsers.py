"""Unit tests for BRL price, discount, commission, and review/sales parsing."""

from ml_automation.scraper.hub_scraper import (
    parse_brl_price,
    parse_commission_info,
    parse_discount_pct,
    parse_review_and_sold,
)


def test_parse_brl_price() -> None:
    assert parse_brl_price("R$ 1.299,90") == 1299.90
    assert parse_brl_price("R$\n67\n,\n90") == 67.90
    assert parse_brl_price("R$ 59") == 59.0
    assert parse_brl_price("sem preço") is None
    assert parse_brl_price(None) is None


def test_parse_discount_pct() -> None:
    assert parse_discount_pct("35% OFF") == 35.0
    assert parse_discount_pct("Sem desconto") is None
    assert parse_discount_pct(None) is None


def test_parse_commission_info() -> None:
    assert parse_commission_info("GANHOS EXTRAS 28%") == (28.0, True)
    assert parse_commission_info("GANHOS 12%") == (12.0, False)
    assert parse_commission_info(None) == (None, False)


def test_parse_review_and_sold() -> None:
    assert parse_review_and_sold("4.9 | +10mil vendidos") == (
        4.9,
        "+10mil vendidos",
        10_000,
    )
    assert parse_review_and_sold("4.7 | +500 vendidos") == (
        4.7,
        "+500 vendidos",
        500,
    )
    assert parse_review_and_sold("4.8 | +1000 vendidos") == (
        4.8,
        "+1000 vendidos",
        1_000,
    )
    assert parse_review_and_sold(None) == (None, None, None)
