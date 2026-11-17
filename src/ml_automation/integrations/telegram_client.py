"""HTTP client for the Rust telegram-bot relay service (/api/v1/messages & /api/v1/messages/photo)."""

from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from ml_automation.models.product import AffiliateLinkResult, AffiliateProduct

DEFAULT_TELEGRAM_API_URL = "http://localhost:8080"


def _fmt_brl(val: float) -> str:
    formatted = f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def format_telegram_html(item: AffiliateLinkResult | AffiliateProduct) -> str:
    """Format an affiliate product offer using Telegram-compatible HTML."""
    raw_title = (item.title or "Oferta Mercado Livre").strip()
    safe_title = html.escape(raw_title)
    highlight = getattr(item, "highlight", None)
    badge = f"[{html.escape(highlight)}] " if highlight else ""

    lines: list[str] = []
    if getattr(item, "is_lowest_price", False):
        prev_low = getattr(item, "lowest_price_seen", None)
        ref_str = f" (Histórico: {_fmt_brl(prev_low)})" if prev_low else ""
        lines.append(f"🚨 <b>MENOR PREÇO MONITORADO!</b>{ref_str}")

    lines.append(f"🔥 <b>{badge}{safe_title}</b>")

    rating = getattr(item, "rating", None)
    sold_text = getattr(item, "sold_text", None)
    if rating or sold_text:
        proof_parts: list[str] = []
        if rating:
            proof_parts.append(f"⭐ {rating}")
        if sold_text:
            proof_parts.append(f"({html.escape(sold_text)})")
        lines.append(" ".join(proof_parts))

    lines.append("")

    price = getattr(item, "price", None)
    original_price = getattr(item, "original_price", None)
    discount_pct = getattr(item, "discount_pct", None)

    if original_price and price and original_price > price:
        lines.append(f"❌ De: <s>{_fmt_brl(original_price)}</s>")
    if price:
        disc_str = f" <i>({int(discount_pct)}% OFF)</i>" if discount_pct else ""
        lines.append(f"✅ Por: <b>{_fmt_brl(price)}</b>{disc_str}")

    coupon_text = getattr(item, "coupon_text", None)
    if coupon_text:
        lines.append(f"🎟️ <b>Cupom:</b> <code>{html.escape(coupon_text)}</code>")

    link = item.short_url or item.affiliate_url or item.product_url
    safe_link = html.escape(link, quote=True)
    lines.append("")
    lines.append(f'🛒 <a href="{safe_link}">Comprar no Mercado Livre</a>')
    lines.append(f"🔗 {html.escape(link)}")

    return "\n".join(lines)


def _encode_multipart_formdata(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    """Encode fields and a binary file into a multipart/form-data payload."""
    boundary = f"----MLAutomationBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(f"{value}\r\n".encode("utf-8"))

    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(
            "utf-8"
        )
    )
    chunks.append(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_bytes)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(chunks)
    header = f"multipart/form-data; boundary={boundary}"
    return body, header


class TelegramRelayClient:
    """Client for the local/remote Rust telegram-bot HTTP relay."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("TELEGRAM_BOT_API_URL") or DEFAULT_TELEGRAM_API_URL
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.environ.get("TELEGRAM_BOT_API_KEY")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout = timeout

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def check_health(self) -> bool:
        """Return True if GET /health on telegram-bot returns 200 OK (auto-resolving Docker host if needed)."""
        candidates = [self.base_url]
        if "://localhost" in self.base_url or "://127.0.0.1" in self.base_url:
            docker_internal = self.base_url.replace(
                "://localhost", "://host.docker.internal"
            ).replace("://127.0.0.1", "://host.docker.internal")
            docker_service = self.base_url.replace(
                "://localhost", "://telegram-bot"
            ).replace("://127.0.0.1", "://telegram-bot")
            for candidate in (docker_internal, docker_service):
                if candidate not in candidates:
                    candidates.append(candidate)

        for candidate in candidates:
            req = urllib.request.Request(
                f"{candidate}/health",
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    if resp.status == 200:
                        self.base_url = candidate
                        return True
            except Exception:
                continue
        return False

    def _download_image(self, image_url: str) -> Optional[tuple[bytes, str, str]]:
        """Download product image bytes and return (bytes, filename, content_type)."""
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status != 200:
                    return None
                content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                ext = "webp" if "webp" in content_type else "jpg"
                data = resp.read()
                if not data or len(data) > 5 * 1024 * 1024:
                    return None
                return data, f"product.{ext}", content_type
        except Exception:
            return None

    def send_text_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
        disable_notification: bool = False,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Send a text message via POST /api/v1/messages."""
        payload: dict[str, Any] = {
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
            "disable_notification": disable_notification,
        }
        if self.chat_id:
            payload["chat_id"] = self.chat_id

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self._auth_headers(),
        }
        return self._request_with_retry(
            f"{self.base_url}/api/v1/messages",
            body=body,
            headers=headers,
            max_retries=max_retries,
        )

    def send_photo_message(
        self,
        file_bytes: bytes,
        caption: str,
        filename: str = "product.jpg",
        content_type: str = "image/jpeg",
        parse_mode: str = "HTML",
        disable_notification: bool = False,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Send a photo + caption via POST /api/v1/messages/photo (multipart/form-data)."""
        fields: dict[str, str] = {
            "caption": caption[:1024],
            "parse_mode": parse_mode,
            "disable_notification": "true" if disable_notification else "false",
        }
        if self.chat_id:
            fields["chat_id"] = self.chat_id

        body, content_header = _encode_multipart_formdata(
            fields=fields,
            file_field="photo",
            filename=filename,
            content_type=content_type,
            file_bytes=file_bytes,
        )
        headers = {
            "Content-Type": content_header,
            **self._auth_headers(),
        }
        return self._request_with_retry(
            f"{self.base_url}/api/v1/messages/photo",
            body=body,
            headers=headers,
            max_retries=max_retries,
        )

    def send_product_offer(
        self,
        item: AffiliateLinkResult | AffiliateProduct,
        send_photo: bool = True,
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        """Send an affiliate product offer with photo + HTML caption (falling back to text)."""
        caption = format_telegram_html(item)
        image_url = getattr(item, "image_url", None)

        if send_photo and image_url:
            downloaded = self._download_image(image_url)
            if downloaded is not None:
                file_bytes, filename, content_type = downloaded
                try:
                    return self.send_photo_message(
                        file_bytes=file_bytes,
                        caption=caption,
                        filename=filename,
                        content_type=content_type,
                        parse_mode="HTML",
                        disable_notification=disable_notification,
                    )
                except Exception:
                    pass

        return self.send_text_message(
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=False,
            disable_notification=disable_notification,
        )

    def send_session_expired_alert(self) -> Optional[dict[str, Any]]:
        """Send an administrative alert when Mercado Livre cookies expire."""
        if not self.check_health():
            return None
        alert_html = (
            "⚠️ <b>[ML-AUTOMATION] Sessão do Mercado Livre Expirada</b>\n\n"
            "Os cookies de autenticação em <code>.state/storage_state.json</code> expiraram.\n"
            "Execute <code>ml-automation login</code> ou <code>ml-automation sync-cookies</code> "
            "para renovar a sessão."
        )
        try:
            return self.send_text_message(
                text=alert_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            return None

    def _request_with_retry(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        max_retries: int = 2,
    ) -> dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {"ok": True}
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502, 503) and attempt < max_retries:
                    time.sleep(2.0 * (attempt + 1))
                    last_exc = exc
                    continue
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8")
                except Exception:
                    pass
                raise RuntimeError(
                    f"Erro HTTP {exc.code} ao chamar {url}: {err_body or exc.reason}"
                ) from exc
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Falha ao conectar em {url}: {exc}") from exc

        raise RuntimeError(f"Falha ao enviar para {url}: {last_exc}")
