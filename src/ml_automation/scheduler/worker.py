"""Periodic worker that scrapes, curates, generates affiliate links, and posts to telegram-bot."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ml_automation.integrations.telegram_client import (
    DEFAULT_TELEGRAM_API_URL,
    TelegramRelayClient,
)
from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct
from ml_automation.scraper.browser import DEFAULT_HUB_URL, DEFAULT_STORAGE_STATE
from ml_automation.scraper.hub_scraper import scrape_affiliate_hub
from ml_automation.scraper.link_generator import generate_affiliate_links
from ml_automation.storage.exporter import export_items
from ml_automation.storage.sent_store import (
    DEFAULT_SENT_HISTORY_PATH,
    SentProductStore,
)


async def dispatch_links_to_telegram(
    items: Sequence[AffiliateLinkResult | AffiliateProduct],
    client: TelegramRelayClient,
    store: SentProductStore,
    send_photo: bool = True,
    send_delay_seconds: float = 15.0,
    cooldown_hours: float = 72.0,
    respect_history: bool = True,
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
            resp = client.send_product_offer(item, send_photo=send_photo)
            msg_id = resp.get("message_id")
            chat_id = resp.get("chat_id")
            store.mark_sent(item, message_id=msg_id, chat_id=chat_id)
            sent_count += 1
            logger(
                f"[TELEGRAM] ({sent_count}/{total}) Enviado: {item.title[:55]} -> {short_or_aff} (msg_id={msg_id})"
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
    logger: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Execute one full cycle: scrape -> curate -> deduplicate -> generate links -> send to Telegram."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger(f"\n=== [CICLO INICIADO - {now_str}] ===")

    store = SentProductStore(path=sent_history_path)
    client = TelegramRelayClient(base_url=api_url, api_key=api_key, chat_id=chat_id)

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
    logger(f"[SCRAPE] {len(products)} produtos de alta conversão encontrados.")

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

    dispatch_stats = await dispatch_links_to_telegram(
        items=successful_links,
        client=client,
        store=store,
        send_photo=send_photo,
        send_delay_seconds=send_delay_seconds,
        cooldown_hours=cooldown_hours,
        respect_history=False,
        logger=logger,
    )

    return {
        "scraped_count": len(products),
        "eligible_count": len(eligible),
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
    logger: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    """Run the affiliate curation and Telegram dispatch loop every `interval_minutes` minutes."""
    cycle_num = 0
    history: list[dict[str, Any]] = []

    logger(
        f"[WORKER] Iniciando modo contínuo (intervalo={interval_minutes} min, "
        f"lote={batch_size} produtos/ciclo, telegram_api={api_url})"
    )

    while True:
        cycle_num += 1
        try:
            stats = await run_worker_cycle(
                url=url,
                storage_state_path=storage_state_path,
                products_output=products_output,
                links_output=links_output,
                sent_history_path=sent_history_path,
                api_url=api_url,
                api_key=api_key,
                chat_id=chat_id,
                max_scrolls=max_scrolls,
                batch_size=batch_size,
                send_delay_seconds=send_delay_seconds,
                cooldown_hours=cooldown_hours,
                send_photo=send_photo,
                headless=headless,
                category=category,
                search_query=search_query,
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
