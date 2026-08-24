"""The polite client: caching, throttling, retries, and encoding sniffing."""

from __future__ import annotations

import httpx
import pytest

from barrister.config import Settings
from barrister.http import FetchError, PoliteClient, decode_html


class TestDecoding:
    def test_utf16_with_bom(self):
        assert decode_html("বাংলাদেশ".encode("utf-16")) == "বাংলাদেশ"

    def test_utf16_from_declared_charset(self):
        raw = "hello".encode("utf-16-le")
        assert decode_html(raw, "text/html;charset=UTF-16") == "hello"

    def test_utf16le_without_bom_is_sniffed(self):
        raw = ("x" * 300).encode("utf-16-le")
        assert decode_html(raw) == "x" * 300

    def test_utf8_passes_through(self):
        assert decode_html("Supreme Court".encode("utf-8")) == "Supreme Court"

    def test_undecodable_bytes_do_not_raise(self):
        assert isinstance(decode_html(b"\xff\xfa\x01plain"), str)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "t.db",
        cache_dir=tmp_path / "cache",
        request_delay_seconds=0.0,
        max_retries=3,
    )


def _client(settings, handler, **kwargs):
    return PoliteClient(settings, transport=httpx.MockTransport(handler), **kwargs)


class TestFetching:
    def test_returns_decoded_text(self, settings):
        with _client(settings, lambda r: httpx.Response(200, text="<html>ok</html>")) as client:
            assert client.fetch("https://example.test/a").text == "<html>ok</html>"

    def test_identifies_itself_with_a_contact_address(self, tmp_path):
        settings = Settings(
            data_dir=tmp_path, cache_dir=tmp_path / "c", request_delay_seconds=0.0,
            contact_email="clerk@chambers.test",
        )
        seen = {}

        def handler(request):
            seen["ua"] = request.headers["user-agent"]
            return httpx.Response(200, text="ok")

        with _client(settings, handler) as client:
            client.fetch("https://example.test/a")
        assert "clerk@chambers.test" in seen["ua"]

    def test_second_fetch_is_served_from_cache(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="body")

        with _client(settings, handler) as client:
            client.fetch("https://example.test/a")
            second = client.fetch("https://example.test/a")

        assert calls["n"] == 1
        assert second.from_cache is True

    def test_force_refresh_bypasses_the_cache(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="body")

        with _client(settings, handler) as client:
            client.fetch("https://example.test/a")
            client.fetch("https://example.test/a", force_refresh=True)

        assert calls["n"] == 2

    def test_post_bodies_are_part_of_the_cache_key(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="body")

        with _client(settings, handler) as client:
            client.fetch("https://example.test/s", method="POST", data={"case": "1"})
            client.fetch("https://example.test/s", method="POST", data={"case": "2"})

        assert calls["n"] == 2

    def test_retries_a_transient_failure_then_succeeds(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, text="busy")
            return httpx.Response(200, text="ok")

        with _client(settings, handler) as client:
            assert client.fetch("https://example.test/a").text == "ok"
        assert calls["n"] == 2

    def test_gives_up_after_max_retries(self, settings):
        def handler(request):
            return httpx.Response(503, text="busy")

        with _client(settings, handler) as client:
            with pytest.raises(FetchError):
                client.fetch("https://example.test/a")

    def test_a_404_is_not_retried(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(404, text="gone")

        with _client(settings, handler) as client:
            with pytest.raises(FetchError):
                client.fetch("https://example.test/a")
        assert calls["n"] == 1

    def test_caching_can_be_disabled(self, settings):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, text="body")

        with _client(settings, handler, use_cache=False) as client:
            client.fetch("https://example.test/a")
            client.fetch("https://example.test/a")

        assert calls["n"] == 2
