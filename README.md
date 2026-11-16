# ML Automation — Mercado Livre Afiliados Scraper & Curador Inteligente

Automação headless em Python + Playwright para explorar o Hub de Afiliados do Mercado Livre (`https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true#menu-user`), interagir com scroll infinito e filtros (*Mais vendidos*, *Ganhos extras*, *Categorias*, *Busca* e *Ordenação*), pontuar e filtrar apenas **produtos de alta conversão** (`sales_score` 0–100) e gerar links de afiliado (`https://meli.la/...`).

## Instalação (Windows / PowerShell)

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
playwright install chromium
```

## Fluxo de Uso

### 1. Autenticação (Sessão em `.state/storage_state.json`)
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

### 2. Curadoria Completa em 1 Comando (`curate` — Recomendado)
Roda em modo **headless**, faz scroll infinito no feed principal, interage automaticamente com as abas **Mais vendidos** e **Ganhos extras**, filtra apenas os produtos que realmente geram vendas (`rating >= 4.5`, `sold_quantity >= 500`, `commission_pct >= 8%`, `discount_pct >= 15%` ou comissão extra/selo de destaque, `sales_score >= 55`), ordena pelo `sales_score` (0–100) e gera os links curtos `https://meli.la/...`:

```powershell
ml-automation curate --max-scrolls 8 --top-links 15 --headless
```

### 3. Extrair Produtos com Filtros Customizados (`scrape`)
```powershell
# Curadoria padrão (explora abas + filtra alta conversão)
ml-automation scrape --output output/produtos.json --max-scrolls 8

# Filtrar por categoria ou busca específica no Hub
ml-automation scrape --category "Eletrônicos, Áudio e Vídeo" --output output/eletronicos.json
ml-automation scrape --search "fone bluetooth" --filter best_seller --output output/fones.json

# Exportar todos os produtos sem filtro de corte
ml-automation scrape --all-products --output output/todos_produtos.json
```

### 4. Gerar Links de Afiliado (`generate-links`)
```powershell
ml-automation generate-links --input output/produtos.json --output output/produtos_com_links.json --limit 20
```

## Critérios do `sales_score` (0–100)
- **Volume de vendas comprovado (`sold_quantity`)**: até **35 pts** (`+10mil`, `+50mil`, `+100mil vendidos`)
- **Desconto real (`discount_pct`)**: até **25 pts**
- **Comissão (`commission_pct`) + bônus `GANHOS EXTRAS`**: até **20 pts**
- **Avaliação dos compradores (`rating`)**: até **10 pts**
- **Selo de destaque (`MAIS VENDIDO`, `OFERTA IMPERDÍVEL`, `BLACK FRIDAY`, etc.)**: até **10 pts**

## Testes
```powershell
.\.venv\Scripts\pytest.exe -v
```
