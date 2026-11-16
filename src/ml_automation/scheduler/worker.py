"""Periodic worker that scrapes, curates, tracks historical prices, generates links, and posts to telegram-bot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence

from ml_automation.integrations.telegram_client import (
    DEFAULT_TELEGRAM_API_URL,
    TelegramRelayClient,
)
from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct
from ml_automation.scraper.browser import (
    DEFAULT_HUB_URL,
    DEFAULT_STORAGE_STATE,
    SessionExpiredError,
)
from ml_automation.scraper.hub_scraper import (
    PROFITABLE_CATEGORIES,
    scrape_affiliate_hub,
)
from ml_automation.scraper.link_generator import generate_affiliate_links
from ml_automation.storage.exporter import export_items
from ml_automation.storage.sent_store import (
    DEFAULT_PRICE_DB_PATH,
    DEFAULT_SENT_HISTORY_PATH,
    PriceHistoryStore,
    SentProductStore,
)

BRT_TZ = timezone(timedelta(hours=-3))


def is_quiet_hours(
    now: Optional[datetime] = None,
    start_hour: int = 0,
    end_hour: int = 8,
) -> bool:
    """Return True if the current time in Brasília (UTC-3) falls within [start_hour, end_hour)."""
    current = (now or datetime.now(timezone.utc)).astimezone(BRT_TZ)
    hour = current.hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


async def dispatch_links_to_telegram(
    items: Sequence[AffiliateLinkResult | AffiliateProduct],
    client: TelegramRelayClient,
    store: SentProductStore,
    send_photo: bool = True,
    send_delay_seconds: float = 15.0,
    cooldown_hours: float = 72.0,
    respect_history: bool = True,
    disable_notification: bool = False,
    logger: Callable[[str], None] = print,
) -> dict[str, int]:
    """Send a batch of affiliate link items to the telegram-bot API and record sent history."""
    sent_count = 0
    skipped_count = 0
    failed_count = 0

    if not client.check_health():
        logger(
            f"[WARN] API telegram-bot ({client.base_url}/health) indisponível no momento. "
            "Os links foram gerados e salvos localmente; o envio será retomado quando a API estiver ativa."
        )
        return {"sent": 0, "skipped": len(items), "failed": len(items)}

    total = len(items)
    for idx, item in enumerate(items):
        short_or_aff = item.short_url or item.affiliate_url
        if not short_or_aff:
            skipped_count += 1
            continue

        if respect_history and isinstance(item, AffiliateProduct):
            if not store.is_eligible(item, cooldown_hours=cooldown_hours):
                skipped_count += 1
                continue

        try:
            resp = client.send_product_offer(
                item,
                send_photo=send_photo,
                disable_notification=disable_notification,
            )
            msg_id = resp.get("message_id")
            chat_id = resp.get("chat_id")
            store.mark_sent(item, message_id=msg_id, chat_id=chat_id)
            sent_count += 1
            low_tag = " [🚨 MENOR PREÇO]" if getattr(item, "is_lowest_price", False) else ""
            logger(
                f"[TELEGRAM] ({sent_count}/{total}){low_tag} Enviado: {item.title[:50]} -> {short_or_aff} (msg_id={msg_id})"
            )
            if idx < total - 1 and send_delay_seconds > 0:
                await asyncio.sleep(send_delay_seconds)
        except Exception as exc:
            failed_count += 1
            logger(f"[ERRO TELEGRAM] Falha ao enviar '{item.title[:45]}': {exc}")

    return {"sent": sent_count, "skipped": skipped_count, "failed": failed_count}


async def run_worker_cycle(
    url: str = DEFAULT_HUB_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    products_output: Path = Path("output/produtos.json"),
    links_output: Path = Path("output/produtos_com_links.json"),
    sent_history_path: Path = DEFAULT_SENT_HISTORY_PATH,
    price_db_path: Path = DEFAULT_PRICE_DB_PATH,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    api_key: Optional[str] = None,
    chat_id: Optional[str] = None,
    max_scrolls: int = 8,
    batch_size: int = 5,
    send_delay_seconds: float = 15.0,
    cooldown_hours: float = 72.0,
    send_photo: bool = True,
    headless: bool = True,
    category: Optional[str] = None,
    search_query: Optional[str] = None,
    quiet_hours_mode: Literal["silent", "pause", "off"] = "silent",
    now: Optional[datetime] = None,
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Execute one full cycle: scrape -> price history -> curate -> deduplicate -> generate links -> send to Telegram."""
    current_dt = now or datetime.now(timezone.utc)
    now_str = current_dt.astimezone(BRT_TZ).strftime("%Y-%m-%d %H:%M:%S BRT")
    cat_label = f" | Categoria: {category}" if category else " | Feed Geral"
    logger(f"\n=== [CICLO INICIADO - {now_str}{cat_label}] ===")

    in_quiet = quiet_hours_mode != "off" and is_quiet_hours(now=current_dt)
    if in_quiet and quiet_hours_mode == "pause":
        logger("[QUIET HOURS] Horário de pausa (00:00–08:00 BRT). Ciclo ignorado.")
        return {
            "scraped_count": 0,
            "eligible_count": 0,
            "links_generated": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "paused_quiet_hours": True,
        }

    store = SentProductStore(path=sent_history_path)
    price_store = PriceHistoryStore(db_path=price_db_path)
    client = TelegramRelayClient(base_url=api_url, api_key=api_key, chat_id=chat_id)

    try:
        products = await scrape_affiliate_hub(
            url=url,
            storage_state_path=storage_state_path,
            max_scrolls=max_scrolls,
            headless=headless,
            only_high_converting=True,
            explore_tabs=True,
            category=category,
            search_query=search_query,
        )
    except SessionExpiredError as exc:
        logger(f"[ALERTA SESSÃO] {exc}")
        client.send_session_expired_alert()
        raise

    lowest_detected = price_store.record_and_evaluate(products)
    products.sort(
        key=lambda p: (
            p.is_lowest_price,
            p.sales_score,
            p.sold_quantity or 0,
            p.discount_pct or 0.0,
        ),
        reverse=True,
    )
    logger(
        f"[SCRAPE] {len(products)} produtos de alta conversão encontrados "
        f"({lowest_detected} com menor preço histórico)."
    )

    eligible = [p for p in products if store.is_eligible(p, cooldown_hours=cooldown_hours)]
    selected = eligible[:batch_size]
    logger(
        f"[CURADORIA] {len(eligible)} produtos inéditos/elegíveis | Selecionados para este ciclo: {len(selected)}"
    )

    if not selected:
        export_items(products, output_path=products_output, fmt="json")
        return {
            "scraped_count": len(products),
            "eligible_count": 0,
            "links_generated": 0,
            "sent": 0,
            "failed": 0,
        }

    link_results = await generate_affiliate_links(
        product_urls=selected,
        hub_url=url,
        storage_state_path=storage_state_path,
        headless=headless,
    )
    successful_links = [r for r in link_results if r.status == "success"]
    logger(f"[LINKS] {len(successful_links)}/{len(selected)} links de afiliado gerados.")

    export_items(products, output_path=products_output, fmt="json")
    export_items(link_results, output_path=links_output, fmt="json")
    export_items(link_results, output_path=links_output.with_suffix(".csv"), fmt="csv")

    silent_notify = in_quiet and quiet_hours_mode == "silent"
    dispatch_stats = await dispatch_links_to_telegram(
        items=successful_links,
        client=client,
        store=store,
        send_photo=send_photo,
        send_delay_seconds=send_delay_seconds,
        cooldown_hours=cooldown_hours,
        respect_history=False,
        disable_notification=silent_notify,
        logger=logger,
    )

    return {
        "scraped_count": len(products),
        "eligible_count": len(eligible),
        "lowest_price_count": lowest_detected,
        "links_generated": len(successful_links),
        **dispatch_stats,
    }


async def run_continuous_worker(
    interval_minutes: float = 15.0,
    max_cycles: Optional[int] = None,
    url: str = DEFAULT_HUB_URL,
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    products_output: Path = Path("output/produtos.json"),
    links_output: Path = Path("output/produtos_com_links.json"),
    sent_history_path: Path = DEFAULT_SENT_HISTORY_PATH,
    price_db_path: Path = DEFAULT_PRICE_DB_PATH,
    api_url: str = DEFAULT_TELEGRAM_API_URL,
    api_key: Optional[str] = None,
    chat_id: Optional[str] = None,
    max_scrolls: int = 8,
    batch_size: int = 5,
    send_delay_seconds: float = 15.0,
    cooldown_hours: float = 72.0,
    send_photo: bool = True,
    headless: bool = True,
    category: Optional[str] = None,
    rotate_categories: bool = True,
    search_query: Optional[str] = None,
    quiet_hours_mode: Literal["silent", "pause", "off"] = "silent",
    logger: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Run the affiliate curation and Telegram dispatch loop every `interval_minutes` minutes."""
    cycle_num = 0
    history: list[dict[str, Any]] = []

    logger(
        f"[WORKER] Iniciando modo contínuo (intervalo={interval_minutes} min, "
        f"lote={batch_size} produtos/ciclo, rotação_categorias={rotate_categories}, "
        f"quiet_hours={quiet_hours_mode}, telegram_api={api_url})"
    )

    while True:
        cycle_num += 1
        active_category = category
        if (
            active_category is None
            and rotate_categories
            and cycle_num > 1
            and PROFITABLE_CATEGORIES
        ):
            active_category = PROFITABLE_CATEGORIES[(cycle_num - 2) % len(PROFITABLE_CATEGORIES)]

        try:
            stats = await run_worker_cycle(
                url=url,
                storage_state_path=storage_state_path,
                products_output=products_output,
                links_output=links_output,
                sent_history_path=sent_history_path,
                price_db_path=price_db_path,
                api_url=api_url,
                api_key=api_key,
                chat_id=chat_id,
                max_scrolls=max_scrolls,
                batch_size=batch_size,
                send_delay_seconds=send_delay_seconds,
                cooldown_hours=cooldown_hours,
                send_photo=send_photo,
                headless=headless,
                category=active_category,
                search_query=search_query,
                quiet_hours_mode=quiet_hours_mode,
                logger=logger,
            )
            history.append(stats)
        except Exception as exc:
            logger(f"[ERRO CICLO #{cycle_num}] {exc}")
            history.append({"error": str(exc)})

        if max_cycles is not None and cycle_num >= max_cycles:
            logger(f"[WORKER] Finalizado após {cycle_num} ciclo(s).")
            break

        wait_seconds = max(1.0, interval_minutes * 60.0)
        logger(
            f"[WORKER] Ciclo #{cycle_num} concluído. Aguardando {interval_minutes} minutos até o próximo ciclo..."
        )
        await asyncio.sleep(wait_seconds)

    return history
