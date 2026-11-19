"""Direct HTTP API client for Mercado Livre Affiliates Hub (fast, zero-Chromium mode with Playwright fallback)."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Sequence

from ml_automation.models.product import (
    AffiliateLinkResult,
    AffiliateProduct,
    build_promo_copy,
    extract_item_id,
)
from ml_automation.scraper.browser import (
    DEFAULT_HUB_URL,
    DEFAULT_STORAGE_STATE,
    DEFAULT_USER_AGENT,
    SessionExpiredError,
)

CATEGORY_IDS: dict[str, str] = {
    "Casa, Móveis e Decoração": "MLB1574",
    "Calçados, Roupas e Bolsas": "MLB1430",
    "Eletrodomésticos": "MLB5726",
    "Joias e Relógios": "MLB3937",
    "Informática": "MLB1648",
    "Agro": "MLB271599",
    "Instrumentos Musicais": "MLB1182",
    "Beleza e Cuidado Pessoal": "MLB1246",
    "Brinquedos e Hobbies": "MLB1132",
    "Ferramentas": "MLB263532",
    "Construção": "MLB1500",
    "Bebês": "MLB1384",
    "Indústria e Comércio": "MLB1499",
    "Pet Shop": "MLB1071",
    "Eletrônicos, Áudio e Vídeo": "MLB1000",
    "Celulares e Telefones": "MLB1051",
    "Arte, Papelaria e Armarinho": "MLB1368",
    "Games": "MLB1144",
    "Festas e Lembrancinhas": "MLB12404",
    "Câmeras e Acessórios": "MLB1039",
    "Esportes e Fitness": "MLB1276",
    "Saúde": "MLB264586",
    "Acessórios para Veículos": "MLB5672",
}


class MlHttpSession:
    """Lightweight authenticated HTTP session backed by Playwright's storage_state.json."""

    def __init__(self, storage_state_path: Path = DEFAULT_STORAGE_STATE) -> None:
        if not storage_state_path.exists():
            raise FileNotFoundError(
                f"Arquivo de sessão não encontrado em '{storage_state_path}'. "
                "Execute 'ml-automation sync-cookies' primeiro."
            )
        self.storage_state_path = storage_state_path
        self._state = json.loads(storage_state_path.read_text(encoding="utf-8"))
        self._cookie_jar = http.cookiejar.CookieJar()
        for c in self._state.get("cookies", []):
            domain = str(c.get("domain", ""))
            if not domain:
                continue
            expires = c.get("expires")
            exp_int = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
            cookie = http.cookiejar.Cookie(
                version=0,
                name=str(c["name"]),
                value=str(c["value"]),
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=str(c.get("path") or "/"),
                path_specified=True,
                secure=bool(c.get("secure", False)),
                expires=exp_int,
                discard=exp_int is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None} if c.get("httpOnly") else {},
            )
            self._cookie_jar.set_cookie(cookie)
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )
        self.csrf_token: str = ""
        self.affiliate_tag: str = ""

    def save_refreshed_cookies(self) -> None:
        """Persist any refreshed Set-Cookie values back into storage_state.json."""
        existing = {
            (c.get("name"), c.get("domain"), c.get("path", "/")): c
            for c in self._state.get("cookies", [])
        }
        for c in self._cookie_jar:
            key = (c.name, c.domain, c.path)
            entry = existing.get(key, {})
            entry.update(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "expires": float(c.expires) if c.expires else -1,
                    "httpOnly": entry.get("httpOnly", False),
                    "secure": bool(c.secure),
                    "sameSite": entry.get("sameSite", "Lax"),
                }
            )
            existing[key] = entry
        self._state["cookies"] = list(existing.values())
        tmp_path = self.storage_state_path.with_name(
            f".{self.storage_state_path.name}.{os.getpid()}.tmp"
        )
        tmp_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.storage_state_path)

    def bootstrap_hub(self, hub_url: str = DEFAULT_HUB_URL) -> str:
        """Fetch the Hub SSR HTML, verify session is active, and extract csrf-token + affiliate tag."""
        req = urllib.request.Request(
            hub_url.split("#")[0],
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            method="GET",
        )
        with self._opener.open(req, timeout=25) as resp:
            final_url = (resp.geturl() or "").lower()
            if "login" in final_url or "auth" in final_url:
                raise SessionExpiredError(
                    "Sessão expirada ou não autenticada. Faça login no Brave e rode 'sync-cookies'."
                )
            html = resp.read().decode("utf-8", errors="replace")

        if "hubo un error accediendo" in html.lower():
            raise RuntimeError("Mercado Livre retornou página de erro para a requisição HTTP.")

        csrf_m = re.search(r'name="csrf-token" content="([^"]+)"', html)
        tag_m = re.search(r'"tag"\s*:\s*"([^"]+)"\s*,\s*"in_use"\s*:\s*true', html)
        self.csrf_token = csrf_m.group(1) if csrf_m else ""
        self.affiliate_tag = tag_m.group(1) if tag_m else ""
        return html

    def search_hub(
        self,
        search: str = "",
        sort: str = "relevance",
        filters: Optional[list[dict[str, str]]] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query POST /affiliate-program/api/hub/search and return raw polycards."""
        payload = {
            "search": search,
            "sort": sort,
            "filters": filters or [],
            "offset": offset,
        }
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true",
            "Origin": "https://www.mercadolivre.com.br",
        }
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token

        req = urllib.request.Request(
            "https://www.mercadolivre.com.br/affiliate-program/api/hub/search?is_affiliate=true&device=desktop",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._opener.open(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("polycard_client_model") or {}).get("polycards") or []

    def create_affiliate_link(
        self,
        item: str | AffiliateProduct,
    ) -> AffiliateLinkResult:
        """Call POST /affiliate-program/api/v2/affiliates/createLink directly."""
        product = item if isinstance(item, AffiliateProduct) else None
        product_url = item.product_url if isinstance(item, AffiliateProduct) else str(item)

        clean_url = product_url.split("#")[0].split("?")[0]
        if clean_url.startswith("https://"):
            clean_url = clean_url[len("https://") :]
        elif clean_url.startswith("http://"):
            clean_url = clean_url[len("http://") :]

        item_id = (product.item_id if product else None) or extract_item_id(product_url) or ""
        extra_comm = "true" if (product and product.extra_commission) else "false"

        payload = {
            "itemId": item_id,
            "type": "product",
            "extraCommission": extra_comm,
            "itemAddToList": item_id,
            "tag": self.affiliate_tag,
            "urls": [clean_url],
        }
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.mercadolivre.com.br/afiliados/hub?is_affiliate=true",
            "Origin": "https://www.mercadolivre.com.br",
        }
        if self.csrf_token:
            headers["x-csrf-token"] = self.csrf_token

        req = urllib.request.Request(
            "https://www.mercadolivre.com.br/affiliate-program/api/v2/affiliates/createLink",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with self._opener.open(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        urls = data.get("urls") or []
        if not urls or not isinstance(urls[0], dict):
            raise RuntimeError(f"Resposta inesperada do createLink: {data}")

        info = urls[0]
        short_url = info.get("short_url")
        long_url = info.get("long_url") or short_url
        res = AffiliateLinkResult(
            product_url=product_url,
            affiliate_url=long_url,
            short_url=short_url,
            share_text=info.get("text"),
            status="success",
        )
        if product is not None:
            res.item_id = product.item_id
            res.title = product.title
            res.price = product.price
            res.original_price = product.original_price
            res.discount_pct = product.discount_pct
            res.commission_pct = product.commission_pct
            res.estimated_commission_brl = product.estimated_commission_brl
            res.rating = product.rating
            res.sold_text = product.sold_text
            res.highlight = product.highlight
            res.coupon_text = product.coupon_text
            res.lowest_price_seen = product.lowest_price_seen
            res.is_lowest_price = product.is_lowest_price
            res.sales_score = product.sales_score
            res.image_url = product.image_url
            link = short_url or long_url or product_url
            res.promo_copy = build_promo_copy(
                title=product.title,
                price=product.price,
                original_price=product.original_price,
                discount_pct=product.discount_pct,
                rating=product.rating,
                sold_text=product.sold_text,
                highlight=product.highlight,
                link=link,
                coupon_text=product.coupon_text,
                is_lowest_price=product.is_lowest_price,
                lowest_price_seen=product.lowest_price_seen,
            )
            product.affiliate_url = long_url
            product.short_url = short_url
            product.share_text = res.share_text
            product.promo_copy = res.promo_copy
        return res


def generate_links_via_http(
    items: Sequence[str | AffiliateProduct],
    storage_state_path: Path = DEFAULT_STORAGE_STATE,
    hub_url: str = DEFAULT_HUB_URL,
) -> list[AffiliateLinkResult]:
    """Generate affiliate links for a batch of products via direct authenticated HTTP requests."""
    session = MlHttpSession(storage_state_path=storage_state_path)
    session.bootstrap_hub(hub_url)
    if not session.affiliate_tag:
        raise RuntimeError("Affiliate tag não encontrada no Hub HTML.")

    results: list[AffiliateLinkResult] = []
    for item in items:
        results.append(session.create_affiliate_link(item))
    session.save_refreshed_cookies()
    return results
