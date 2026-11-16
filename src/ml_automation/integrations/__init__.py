"""External service integrations (Telegram Relay Bot API)."""

from ml_automation.integrations.telegram_client import (
    TelegramRelayClient,
    format_telegram_html,
)

__all__ = ["TelegramRelayClient", "format_telegram_html"]
