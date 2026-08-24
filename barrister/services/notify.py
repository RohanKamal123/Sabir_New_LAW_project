"""Notification delivery.

Alerts are the product's only job at Tier 0, so delivery is pluggable and
failure is loud but non-fatal: a Telegram outage must not lose the alert, it
just leaves it undelivered in the database for the next run to retry.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from ..config import Settings, settings as default_settings

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, recipient: str, subject: str, body: str) -> bool:
        """Deliver one alert. Return True only on confirmed delivery."""


class ConsoleNotifier:
    """Prints alerts. The default, and what the cron job uses when unconfigured."""

    def __init__(self, stream=None) -> None:
        import sys

        self.stream = stream or sys.stdout

    def send(self, recipient: str, subject: str, body: str) -> bool:
        print(f"\n=== {subject} ===\n(to: {recipient})\n{body}\n", file=self.stream)
        return True


class TelegramNotifier:
    """Telegram Bot API delivery.

    Telegram is the right first channel: no per-message cost, no business
    verification, and a bot can be added in the time it takes to explain it.
    WhatsApp needs Business API approval, so it is a later port of this same
    interface rather than the starting point.
    """

    API = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_LEN = 4096

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None) -> None:
        self.settings = settings or default_settings
        self._client = client or httpx.Client(timeout=20.0)

    def send(self, recipient: str, subject: str, body: str) -> bool:
        token = self.settings.telegram_bot_token
        if not token:
            log.warning("TELEGRAM_BOT_TOKEN not set; cannot deliver to %s", recipient)
            return False

        text = f"*{subject}*\n\n{body}"
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 20].rstrip() + "\n… (truncated)"

        try:
            response = self._client.post(
                self.API.format(token=token),
                data={"chat_id": recipient, "text": text, "parse_mode": "Markdown"},
            )
        except httpx.HTTPError as exc:
            log.error("telegram delivery failed for %s: %s", recipient, exc)
            return False

        if response.status_code != 200:
            log.error("telegram rejected message for %s: %s", recipient, response.text[:200])
            return False
        return True


class NullNotifier:
    """Records instead of sending. Used in tests and dry runs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, recipient: str, subject: str, body: str) -> bool:
        self.sent.append((recipient, subject, body))
        return True


def default_notifier(settings: Settings | None = None) -> Notifier:
    settings = settings or default_settings
    if settings.telegram_bot_token:
        return TelegramNotifier(settings)
    return ConsoleNotifier()
