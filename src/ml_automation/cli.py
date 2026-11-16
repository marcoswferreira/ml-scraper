"""CLI entrypoint for ml-automation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal, Optional

import typer

from ml_automation.auth.brave_cookies import (
    export_brave_storage_state,
    import_cookies_from_json,
)
from ml_automation.integrations.telegram_client import (
    DEFAULT_TELEGRAM_API_URL,
    TelegramRelayClient,
)
from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct
from ml_automation.scheduler.worker import (
    dispatch_links_to_telegram,
    run_continuous_worker,
)
from ml_automation.scraper.browser import DEFAULT_HUB_URL, DEFAULT_STORAGE_STATE
from ml_automation.scraper.hub_scraper import scrape_affiliate_hub
from ml_automation.scraper.link_generator import generate_affiliate_links
from ml_automation.scraper.recon import run_recon
from ml_automation.storage.exporter import export_items
from ml_automation.storage.sent_store import (
    DEFAULT_SENT_HISTORY_PATH,
    SentProductStore,
)

app = typer.Typer(
    name="ml-automation",
    help="Scraper inteligente e gerador de links para o Mercado Livre Afiliados.",
    no_args_is_help=True,
)


@app.command("sync-cookies")
def sync_cookies(
    output: Path = typer.Option(
        DEFAULT_STORAGE_STATE,
        "--output",
        "-o",
        help="Caminho para salvar o storage_state.json do Playwright.",
    ),
    cookies_db: Optional[Path] = typer.Option(
        None,
        "--cookies-db",
        "-c",
        help="Caminho manual para o arquivo SQLite 'Cookies' do Brave.",
    ),
) -> None:
    """Extrai os cookies autenticados do Brave para o Playwright."""
    saved = export_brave_storage_state(output_path=output, cookies_db=cookies_db)
    typer.echo(f"Cookies salvos em: {saved}")


@app.command("import-cookies")
def import_cookies(
    input_file: Path = typer.Option(
        ...,
        "--input",
        "-i",
        help="Arquivo JSON exportado do Brave no Windows (ex: Cookie-Editor).",
    ),
    output: Path = typer.Option(
        DEFAULT_STORAGE_STATE,
        "--output",
        "-o",
        help="Caminho para salvar o storage_state.json do Playwright.",
    ),
) -> None:
    """Importa cookies exportados do Brave (útil no WSL2 ou Windows)."""
    saved = import_cookies_from_json(input_path=input_file, output_path=output)
    typer.echo(f"Cookies importados para: {saved}")


@app.command("login")
def login(
    url: str = typer.Option(DEFAULT_HUB_URL, "--url", help="URL de login/hub."),
    output: Path = typer.Option(
        DEFAULT_STORAGE_STATE,
        "--output",
        "-o",
        help="Caminho para salvar o storage_state.json após login.",
    ),
) -> None:
    """Abre o navegador para login manual (ex: QR Code) e salva a sessão."""
    from playwright.async_api import async_playwright

    async def _interactive_login() -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            context = await browser.new_context(
                locale="pt-BR", viewport={"width": 1440, "height": 900}
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            typer.echo(
                "Faça login no navegador aberto. "
                "Quando estiver no Hub de Afiliados, pressione ENTER aqui no terminal..."
            )
            input()
            await context.storage_state(path=str(output))
            await context.close()
            await browser.close()
        return output

    saved = asyncio.run(_interactive_login())
    typer.echo(f"Sessão salva em: {saved}")


@app.command("recon")
def recon(
    url: str = typer.Option(DEFAULT_HUB_URL, "--url", help="URL alvo para reconhecimento."),
    storage_state: Path = typer.Option(DEFAULT_STORAGE_STATE, "--state"),
    output_dir: Path = typer.Option(Path("output/recon"), "--output-dir"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Executa reconhecimento da página (screenshot, DOM e logs XHR/Fetch)."""
    report = asyncio.run(
        run_recon(
            url=url,
            storage_state_path=storage_state,
            output_dir=output_dir,
            headless=headless,
        )
    )
    typer.echo(json.dumps(report, indent=2, ensure_ascii=False))


@app.command("scrape")
def scrape(
    url: str = typer.Option(DEFAULT_HUB_URL, "--url"),
    storage_state: Path = typer.Option(DEFAULT_STORAGE_STATE, "--state"),
    output: Path = typer.Option(Path("output/produtos.json"), "--output", "-o"),
    fmt: Literal["json", "csv"] = typer.Option("json", "--format", "-f"),
    max_scrolls: int = typer.Option(8, "--max-scrolls"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    smart_filter: bool = typer.Option(
        True,
        "--smart-filter/--all-products",
        help="Filtra apenas produtos de alta conversão (avaliação, vendas, comissão e desconto).",
    ),
    explore_tabs: bool = typer.Option(
        True,
        "--explore-tabs/--single-view",
        help="Explora automaticamente as abas 'Mais vendidos' e 'Ganhos extras' além do feed principal.",
    ),
    quick_filter: Optional[Literal["best_seller", "extra_commission"]] = typer.Option(
        None,
        "--filter",
        help="Filtro rápido específico ('best_seller' ou 'extra_commission').",
    ),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Filtrar por nome de categoria do Hub (ex: 'Eletrônicos, Áudio e Vídeo').",
    ),
    search_query: Optional[str] = typer.Option(
        None,
        "--search",
        "-q",
        help="Buscar termo específico na barra de produtos do Hub.",
    ),
    sort_by: Optional[Literal["relevance", "lowest_price", "highest_price"]] = typer.Option(
        None,
        "--sort",
        help="Ordenação ('relevance', 'lowest_price', 'highest_price').",
    ),
    min_rating: float = typer.Option(4.5, "--min-rating", help="Nota mínima (ex: 4.5)."),
    min_sold: int = typer.Option(500, "--min-sold", help="Mínimo de vendas comprovadas."),
    min_commission: float = typer.Option(8.0, "--min-commission", help="Comissão mínima (%)."),
    min_discount: float = typer.Option(15.0, "--min-discount", help="Desconto mínimo (%)."),
    min_score: float = typer.Option(55.0, "--min-score", help="Score mínimo de conversão (0-100)."),
) -> None:
    """Extrai produtos da página de afiliados, aplica curadoria de vendas e exporta para JSON/CSV."""
    products = asyncio.run(
        scrape_affiliate_hub(
            url=url,
            storage_state_path=storage_state,
            max_scrolls=max_scrolls,
            headless=headless,
            only_high_converting=smart_filter,
            explore_tabs=explore_tabs,
            quick_filter=quick_filter,
            category=category,
            search_query=search_query,
            sort_by=sort_by,
            min_rating=min_rating,
            min_sold=min_sold,
            min_commission_pct=min_commission,
            min_discount_pct=min_discount,
            min_sales_score=min_score,
        )
    )
    saved = export_items(products, output_path=output, fmt=fmt)
    typer.echo(f"{len(products)} produtos salvos em: {saved}")


@app.command("generate-links")
def generate_links(
    input_file: Path = typer.Option(..., "--input", "-i", help="JSON de produtos extraídos."),
    output: Path = typer.Option(Path("output/produtos_com_links.json"), "--output", "-o"),
    fmt: Literal["json", "csv"] = typer.Option("json", "--format", "-f"),
    url: str = typer.Option(DEFAULT_HUB_URL, "--url", help="URL do Hub de Afiliados."),
    storage_state: Path = typer.Option(DEFAULT_STORAGE_STATE, "--state"),
    headless: bool = typer.Option(True, "--headless/--headed"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Limitar quantidade de produtos."),
) -> None:
    """Gera links de afiliado para uma lista de produtos extraídos."""
    raw = json.loads(input_file.read_text(encoding="utf-8"))
    products = [AffiliateProduct.model_validate(item) for item in raw]
    if limit is not None:
        products = products[:limit]

    results = asyncio.run(
        generate_affiliate_links(
            product_urls=products,
            hub_url=url,
            storage_state_path=storage_state,
            headless=headless,
        )
    )
    saved = export_items(results, output_path=output, fmt=fmt)
    typer.echo(f"{len(results)} links processados e salvos em: {saved}")


@app.command("curate")
def curate(
    storage_state: Path = typer.Option(DEFAULT_STORAGE_STATE, "--state"),
    products_output: Path = typer.Option(Path("output/produtos.json"), "--products-output"),
    links_output: Path = typer.Option(Path("output/produtos_com_links.json"), "--links-output"),
    max_scrolls: int = typer.Option(8, "--max-scrolls"),
    top_links: int = typer.Option(
        15, "--top-links", help="Quantidade de top produtos para gerar link."
    ),
    category: Optional[str] = typer.Option(None, "--category"),
    search_query: Optional[str] = typer.Option(None, "--search", "-q"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Executa o fluxo completo headless: extrai, filtra os campeões de venda e gera links."""
    products = asyncio.run(
        scrape_affiliate_hub(
            storage_state_path=storage_state,
            max_scrolls=max_scrolls,
            headless=headless,
            only_high_converting=True,
            explore_tabs=True,
            category=category,
            search_query=search_query,
        )
    )
    selected = products[:top_links]
    results = asyncio.run(
        generate_affiliate_links(
            product_urls=selected,
            storage_state_path=storage_state,
            headless=headless,
        )
    )
    saved_products = export_items(products, output_path=products_output, fmt="json")
    typer.echo(f"{len(products)} produtos de alta conversão salvos em: {saved_products}")
    saved_links = export_items(results, output_path=links_output, fmt="json")
    csv_links = links_output.with_suffix(".csv")
    export_items(results, output_path=csv_links, fmt="csv")
    typer.echo(f"{len(results)} links de afiliado gerados em: {saved_links} e {csv_links}")


@app.command("send-telegram")
def send_telegram(
    input_file: Path = typer.Option(
        Path("output/produtos_com_links.json"),
        "--input",
        "-i",
        help="Arquivo JSON com links de afiliado gerados.",
    ),
    api_url: str = typer.Option(
        DEFAULT_TELEGRAM_API_URL,
        "--api-url",
        envvar="TELEGRAM_BOT_API_URL",
        help="URL base da API telegram-bot (ex: http://localhost:8080).",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="TELEGRAM_BOT_API_KEY",
        help="Chave X-API-Key da API telegram-bot (opcional).",
    ),
    chat_id: Optional[str] = typer.Option(
        None,
        "--chat-id",
        envvar="TELEGRAM_CHAT_ID",
        help="ID do grupo/canal no Telegram (opcional se já configurado no telegram-bot).",
    ),
    send_photo: bool = typer.Option(
        True,
        "--photo/--text-only",
        help="Envia foto do produto + legenda HTML (padrão) ou apenas texto.",
    ),
    send_delay: float = typer.Option(
        15.0,
        "--send-delay",
        help="Intervalo em segundos entre cada mensagem enviada.",
    ),
    limit: Optional[int] = typer.Option(
        None,
        "--limit",
        help="Limitar quantidade de ofertas a enviar.",
    ),
    sent_history: Path = typer.Option(
        DEFAULT_SENT_HISTORY_PATH,
        "--history",
        help="Arquivo de histórico de produtos enviados.",
    ),
) -> None:
    """Envia produtos com links gerados para o grupo do Telegram via API telegram-bot."""
    raw = json.loads(input_file.read_text(encoding="utf-8"))
    items = [AffiliateLinkResult.model_validate(item) for item in raw]
    if limit is not None:
        items = items[:limit]

    client = TelegramRelayClient(base_url=api_url, api_key=api_key, chat_id=chat_id)
    store = SentProductStore(path=sent_history)

    stats = asyncio.run(
        dispatch_links_to_telegram(
            items=items,
            client=client,
            store=store,
            send_photo=send_photo,
            send_delay_seconds=send_delay,
            respect_history=False,
            logger=typer.echo,
        )
    )
    typer.echo(
        f"Resumo Telegram -> Enviados: {stats['sent']} | Ignorados: {stats['skipped']} | Falhas: {stats['failed']}"
    )


@app.command("watch")
def watch(
    interval_minutes: float = typer.Option(
        15.0,
        "--interval-minutes",
        "-m",
        help="Intervalo em minutos entre ciclos de varredura e envio (padrão: 15 min).",
    ),
    batch_size: int = typer.Option(
        5,
        "--batch-size",
        "-b",
        help="Quantidade máxima de produtos enviados por ciclo.",
    ),
    send_delay: float = typer.Option(
        15.0,
        "--send-delay",
        help="Segundos de pausa entre cada postagem no Telegram.",
    ),
    cooldown_hours: float = typer.Option(
        72.0,
        "--cooldown-hours",
        help="Horas sem repetir o mesmo produto (exceto se o preço cair >= 10%).",
    ),
    api_url: str = typer.Option(
        DEFAULT_TELEGRAM_API_URL,
        "--api-url",
        envvar="TELEGRAM_BOT_API_URL",
        help="URL base da API telegram-bot.",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="TELEGRAM_BOT_API_KEY",
        help="Chave X-API-Key da API telegram-bot.",
    ),
    chat_id: Optional[str] = typer.Option(
        None,
        "--chat-id",
        envvar="TELEGRAM_CHAT_ID",
        help="Sobrescrever chat_id do grupo/canal no Telegram.",
    ),
    send_photo: bool = typer.Option(
        True,
        "--photo/--text-only",
        help="Enviar foto do produto + texto HTML (padrão) ou somente texto.",
    ),
    max_scrolls: int = typer.Option(8, "--max-scrolls"),
    max_cycles: Optional[int] = typer.Option(
        None,
        "--max-cycles",
        help="Número máximo de ciclos (None = roda continuamente até Ctrl+C).",
    ),
    category: Optional[str] = typer.Option(None, "--category"),
    search_query: Optional[str] = typer.Option(None, "--search", "-q"),
    url: str = typer.Option(DEFAULT_HUB_URL, "--url"),
    storage_state: Path = typer.Option(DEFAULT_STORAGE_STATE, "--state"),
    products_output: Path = typer.Option(Path("output/produtos.json"), "--products-output"),
    links_output: Path = typer.Option(Path("output/produtos_com_links.json"), "--links-output"),
    sent_history: Path = typer.Option(DEFAULT_SENT_HISTORY_PATH, "--history"),
    headless: bool = typer.Option(True, "--headless/--headed"),
) -> None:
    """Roda continuamente em segundo plano (a cada 15 min) extraindo, gerando links e postando no Telegram."""
    try:
        asyncio.run(
            run_continuous_worker(
                interval_minutes=interval_minutes,
                max_cycles=max_cycles,
                url=url,
                storage_state_path=storage_state,
                products_output=products_output,
                links_output=links_output,
                sent_history_path=sent_history,
                api_url=api_url,
                api_key=api_key,
                chat_id=chat_id,
                max_scrolls=max_scrolls,
                batch_size=batch_size,
                send_delay_seconds=send_delay,
                cooldown_hours=cooldown_hours,
                send_photo=send_photo,
                headless=headless,
                category=category,
                search_query=search_query,
                logger=typer.echo,
            )
        )
    except KeyboardInterrupt:
        typer.echo("\n[WORKER] Encerrado pelo usuário.")
