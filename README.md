# ML Automation — Mercado Livre Afiliados Scraper, Curador & Worker Telegram

Automação contínua (daemon/worker headless) em Python + Playwright para:

1. Explorar o Hub de Afiliados do Mercado Livre (`https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true#menu-user`) com scroll infinito e interação com as abas **Mais vendidos**, **Ganhos extras**, **Categorias**, **Busca** e **Ordenação**.
2. Pontuar (`sales_score` 0–100) e filtrar apenas **produtos de alta conversão** que realmente geram vendas.
3. Gerar links curtos de afiliado (`https://meli.la/...`) e copy promocional formatada.
4. Rodar continuamente a cada **15 minutos** (`ml-automation watch`) enviando **Foto + Texto HTML** para o grupo/canal do Telegram através da API [`telegram-bot`](https://github.com/marcoswferreira/telegram-bot/blob/main/openapi.yaml) (`POST /api/v1/messages/photo` e fallback `POST /api/v1/messages`), com controle anti-duplicidade em `.state/sent_history.json`.

## Instalação (Windows / PowerShell)

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
playwright install chromium
```

## Fluxo de Uso

### 1. Autenticação (`.state/storage_state.json`)

- **Login interativo**:

  ```powershell
  ml-automation login
  ```

- **Importar cookies exportados (ex: Cookie-Editor JSON)**:

  ```powershell
  ml-automation import-cookies --input cookies.json
  ```

- **Sincronizar direto do Brave** (no Windows exige terminal como Administrador devido à criptografia App-Bound v130+):

  ```powershell
  ml-automation sync-cookies
  ```

### 2. Worker Contínuo Integrado ao Telegram (`watch` — Padrão: a cada 15 min)

Roda continuamente em segundo plano (headless), varrendo o Hub a cada **15 minutos**, filtrando produtos inéditos (cooldown de 72h, ou reenvio automático se o preço cair `>= 10%`), gerando os links `https://meli.la/...` e postando **Foto + Legenda HTML** na API do `telegram-bot`:

```powershell
# Usando variáveis de ambiente ou parâmetros CLI
$env:TELEGRAM_BOT_API_URL = "http://localhost:8080"
$env:TELEGRAM_BOT_API_KEY = "sua-api-key"
$env:TELEGRAM_CHAT_ID = "-1001234567890"

ml-automation watch --interval-minutes 15 --batch-size 5 --send-delay 15
```

Para rodar apenas 1 ciclo de teste e encerrar:

```powershell
ml-automation watch --max-cycles 1 --batch-size 5
```

### 3. Enviar Links Já Gerados para o Telegram (`send-telegram`)

```powershell
ml-automation send-telegram `
  --input output/produtos_com_links.json `
  --api-url http://localhost:8080 `
  --api-key "sua-api-key"
```

### 4. Curadoria Local sem Envio (`curate` / `scrape` / `generate-links`)

```powershell
ml-automation curate --max-scrolls 8 --top-links 15 --headless
ml-automation scrape --category "Eletrônicos, Áudio e Vídeo" --output output/eletronicos.json
ml-automation generate-links --input output/produtos.json --output output/produtos_com_links.json --limit 15
```

## Testes Automatizados

```powershell
.\.venv\Scripts\pytest.exe -v
```
