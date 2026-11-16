"""Unit tests for Pydantic models, conversion scoring, and JSON/CSV exporter."""

import csv
import json
from pathlib import Path

from ml_automation.models.product import (
    AffiliateLinkResult,
    AffiliateProduct,
    filter_high_converting_products,
    is_high_converting_product,
)
from ml_automation.storage.exporter import export_items


def test_affiliate_product_extracts_mlb_id_and_computes_score() -> None:
    product = AffiliateProduct(
        title="Fone Bluetooth XYZ",
        price=199.9,
        original_price=299.9,
        discount_pct=33.0,
        commission_pct=20.0,
        extra_commission=True,
        rating=4.9,
        sold_quantity=10_000,
        highlight="MAIS VENDIDO",
        product_url="https://produto.mercadolivre.com.br/MLB-1234567890-fone-xyz-_JM",
    )
    assert product.item_id == "MLB1234567890"
    assert product.estimated_commission_brl == 39.98
    assert product.sales_score >= 85.0
    assert is_high_converting_product(product) is True


def test_filter_high_converting_products_excludes_low_sales_or_rating() -> None:
    champion = AffiliateProduct(
        title="Mochila Reforçada",
        price=46.88,
        original_price=78.90,
        discount_pct=40.0,
        commission_pct=28.0,
        extra_commission=True,
        rating=4.8,
        sold_quantity=100_000,
        highlight="MAIS VENDIDO",
        product_url="https://produto.mercadolivre.com.br/MLB-111-mochila-_JM",
    )
    weak = AffiliateProduct(
        title="Produto Sem Vendas",
        price=500.0,
        discount_pct=0.0,
        commission_pct=5.0,
        rating=3.8,
        sold_quantity=5,
        product_url="https://produto.mercadolivre.com.br/MLB-222-fraco-_JM",
    )
    curated = filter_high_converting_products([weak, champion])
    assert len(curated) == 1
    assert curated[0].item_id == "MLB111"


def test_export_items_json_and_csv(tmp_path: Path) -> None:
    items = [
        AffiliateLinkResult(
            product_url="https://produto.mercadolivre.com.br/MLB-111-item-_JM",
            affiliate_url="https://mercadolivre.com/sec/abc123",
            short_url="https://mercadolivre.com/sec/abc123",
            status="success",
        )
    ]

    json_path = export_items(items, tmp_path / "links.json", fmt="json")
    loaded_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(loaded_json) == 1
    assert loaded_json[0]["affiliate_url"] == "https://mercadolivre.com/sec/abc123"

    csv_path = export_items(items, tmp_path / "links.csv", fmt="csv")
    with csv_path.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
