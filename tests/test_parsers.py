"""Unit tests for BRL price, discount, commission, review/sales, and API polycard parsing."""

from ml_automation.scraper.hub_scraper import (
    parse_brl_price,
    parse_commission_info,
    parse_discount_pct,
    parse_polycard_from_api,
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
    assert parse_review_and_sold("5.0 | +2 milhões vendidos") == (
        5.0,
        "+2 milhões vendidos",
        2_000_000,
    )
    assert parse_review_and_sold(None) == (None, None, None)


def test_parse_polycard_from_api() -> None:
    raw_polycard = {
        "metadata": {
            "id": "MLB7467427796",
            "product_id": "MLB78875859",
            "url": "www.mercadolivre.com.br/cascata-natal/p/MLB78875859",
            "url_params": "?pdp_filters=deal",
            "url_fragments": "#polycard_client=affiliates",
            "extra_commission": "true",
        },
        "pictures": {"pictures": [{"id": "676961-MLA117051602166_102026"}]},
        "components": [
            {
                "type": "highlight",
                "highlight": {"text": "{black_friday_icon} MAIS VENDIDO"},
            },
            {
                "type": "title",
                "title": {"text": "Cascata Natal 400 Leds"},
            },
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
                "chip": {
                    "pill": {"text": "{ganancia} {extra}"},
                    "label": {"text": "20%"},
                },
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

    product = parse_polycard_from_api(raw_polycard, category_hint="Casa")
    assert product is not None
    assert product.item_id == "MLB78875859"
    assert product.title == "Cascata Natal 400 Leds"
    assert product.highlight == "MAIS VENDIDO"
    assert product.rating == 4.9
    assert product.sold_quantity == 10_000
    assert product.commission_pct == 20.0
    assert product.extra_commission is True
    assert product.price == 67.9
    assert product.original_price == 249.9
    assert product.discount_pct == 72.0
    assert product.category == "Casa"
    assert product.sales_score >= 90.0

    assert parse_polycard_from_api({}) is None
