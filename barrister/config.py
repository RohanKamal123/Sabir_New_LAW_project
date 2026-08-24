"""Runtime configuration.

Everything is overridable by environment variable so the same code runs from a
laptop, a cron box, or a container without a settings file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


DATA_DIR = Path(_env("BARRISTER_DATA_DIR", str(Path.home() / ".barrister")))


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------
    data_dir: Path = DATA_DIR
    db_path: Path = field(default_factory=lambda: DATA_DIR / "barrister.db")
    cache_dir: Path = field(default_factory=lambda: DATA_DIR / "http-cache")

    # --- upstreams -----------------------------------------------------
    supreme_court_base: str = _env(
        "BARRISTER_SC_BASE", "https://www.supremecourt.gov.bd/web/"
    )
    bdlaws_base: str = _env("BARRISTER_BDLAWS_BASE", "http://bdlaws.minlaw.gov.bd/")

    # --- crawl politeness ----------------------------------------------
    # Neither upstream publishes a robots.txt (both 404 as of 2026-08-24), so
    # there is no machine-readable crawl budget to read. We self-impose one:
    # a full cause-list sweep is ~60 bench pages once a day, which is far below
    # what a single human browsing the site generates.
    request_delay_seconds: float = _env_float("BARRISTER_REQUEST_DELAY", 1.5)
    request_timeout_seconds: float = _env_float("BARRISTER_REQUEST_TIMEOUT", 30.0)
    max_retries: int = int(_env("BARRISTER_MAX_RETRIES", "3"))
    cache_ttl_seconds: float = _env_float("BARRISTER_CACHE_TTL", 6 * 3600)

    # Identify the crawler honestly and give the site owner a way to reach us.
    # A court IT team that can email you does not have to block you.
    contact_email: str = _env("BARRISTER_CONTACT_EMAIL", "")
    user_agent_product: str = _env("BARRISTER_UA", "BarristerTools/0.1")

    # --- drafting ------------------------------------------------------
    # Two providers are supported. "auto" picks Anthropic when its key is
    # present, else DeepSeek, else no model at all (templates still render).
    drafting_provider: str = _env("BARRISTER_DRAFTING_PROVIDER", "auto")

    anthropic_api_key: str = _env("ANTHROPIC_API_KEY", "")
    drafting_model: str = _env("BARRISTER_DRAFTING_MODEL", "claude-opus-5")

    deepseek_api_key: str = _env("DEEPSEEK_API_KEY", "")
    deepseek_model: str = _env("BARRISTER_DEEPSEEK_MODEL", "deepseek-chat")
    deepseek_base_url: str = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Ceiling on a single draft. Petitions are long; this is deliberately
    # generous, and the backends stream so it cannot trip an HTTP timeout.
    drafting_max_tokens: int = int(_env("BARRISTER_DRAFTING_MAX_TOKENS", "64000"))

    # --- notifications -------------------------------------------------
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN", "")

    @property
    def user_agent(self) -> str:
        if self.contact_email:
            return f"{self.user_agent_product} (+mailto:{self.contact_email})"
        return self.user_agent_product

    def resolve_provider(self) -> str:
        """Which drafting backend to use given the keys actually configured."""
        if self.drafting_provider != "auto":
            return self.drafting_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.deepseek_api_key:
            return "deepseek"
        return "none"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
