"""A deliberately slow, cache-first HTTP client.

Both upstreams are small government sites with no API and no robots.txt. The
whole product depends on continued access to them, so the client is built to be
the least interesting traffic in their logs:

* one request at a time, with a floor on the gap between requests;
* on-disk response cache so a re-run costs zero requests;
* retry only on transient failures, with backoff;
* an honest User-Agent carrying a contact address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from .config import Settings, settings as default_settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Raised when a URL could not be fetched after exhausting retries."""


@dataclass(frozen=True)
class Response:
    url: str
    status_code: int
    text: str
    from_cache: bool = False


def _cache_key(method: str, url: str, data: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        {"m": method.upper(), "u": url, "d": dict(sorted((data or {}).items()))},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decode_html(raw: bytes, declared: str | None = None) -> str:
    """Decode a page body, tolerating bdlaws' UTF-16 pages.

    bdlaws.minlaw.gov.bd serves ``charset=UTF-16`` and really means it, while
    supremecourt.gov.bd serves UTF-8 with a stray BOM on many strings. Getting
    this wrong yields mojibake that silently breaks every downstream selector,
    so sniff rather than trust.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if declared and "utf-16" in declared.lower():
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    # A UTF-16LE body without a BOM shows up as ASCII interleaved with NULs.
    if raw[:400].count(b"\x00") > 40:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    return raw.decode("utf-8", errors="replace")


class PoliteClient:
    """Serial, rate-limited, caching HTTP client."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        use_cache: bool = True,
    ) -> None:
        self.settings = settings or default_settings
        self.settings.ensure_dirs()
        self.use_cache = use_cache
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            },
            transport=transport,
        )

    # -- context manager ------------------------------------------------
    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- cache ----------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.settings.cache_dir / f"{key}.html"

    def _read_cache(self, key: str) -> str | None:
        if not self.use_cache:
            return None
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > self.settings.cache_ttl_seconds:
            return None
        return path.read_text(encoding="utf-8")

    def _write_cache(self, key: str, text: str) -> None:
        if not self.use_cache:
            return
        self._cache_path(key).write_text(text, encoding="utf-8")

    # -- throttle -------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        gap = self.settings.request_delay_seconds - elapsed
        if gap > 0:
            time.sleep(gap)
        self._last_request_at = time.monotonic()

    # -- fetch ----------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: Mapping[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> Response:
        key = _cache_key(method, url, data)
        if not force_refresh:
            cached = self._read_cache(key)
            if cached is not None:
                log.debug("cache hit %s", url)
                return Response(url=url, status_code=200, text=cached, from_cache=True)

        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            self._throttle()
            try:
                resp = self._client.request(method, url, data=dict(data) if data else None)
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                log.warning("fetch %s failed (attempt %d): %s", url, attempt, exc)
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = FetchError(f"HTTP {resp.status_code} for {url}")
                    log.warning("fetch %s got %d (attempt %d)", url, resp.status_code, attempt)
                elif resp.status_code >= 400:
                    # 404 and friends are answers, not failures worth retrying.
                    raise FetchError(f"HTTP {resp.status_code} for {url}")
                else:
                    text = decode_html(resp.content, resp.headers.get("content-type"))
                    self._write_cache(key, text)
                    return Response(url=str(resp.url), status_code=resp.status_code, text=text)

            if attempt < self.settings.max_retries:
                time.sleep(2 ** attempt)

        raise FetchError(f"giving up on {url}: {last_error}")
