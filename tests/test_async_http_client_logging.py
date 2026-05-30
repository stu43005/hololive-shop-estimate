import logging

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.async_http_client import (
    AsyncHTTPClient,
    ClientError,
    WAFBlockedError,
)


class _FakeResponse:
    def __init__(self, status, text_value="", headers=None):
        self.status = status
        self._text_value = text_value
        self.headers = headers or {}

    async def text(self):
        return self._text_value


class _FakeCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, status):
        self._status = status
        self.closed = False

    def request(self, method, url, timeout=None, **kwargs):
        return _FakeCtx(_FakeResponse(self._status, "ok"))

    async def close(self):
        self.closed = True


def _policy():
    return CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0,
        concurrency_per_domain=1, max_retries=1,
    )


@pytest.mark.asyncio
async def test_debug_logs_successful_request(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *a, **k: _FakeSession(200),
    )
    client = AsyncHTTPClient(_policy())
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.async_http_client"):
        await client.get("https://shop.example/products/1")
    await client.close()

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.async_http_client"
        and r.levelno == logging.DEBUG
    ]
    assert any(
        "https://shop.example/products/1" in r.getMessage()
        and "200" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )


@pytest.mark.asyncio
async def test_debug_logs_error_status_before_raise(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *a, **k: _FakeSession(404),
    )
    client = AsyncHTTPClient(_policy())
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.async_http_client"):
        with pytest.raises(ClientError):
            await client.get("https://shop.example/products/x")
    await client.close()

    recs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("404" in r.getMessage() for r in recs)


@pytest.mark.asyncio
async def test_debug_logs_waf_status_before_raise(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *a, **k: _FakeSession(403),
    )
    client = AsyncHTTPClient(_policy())
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.async_http_client"):
        with pytest.raises(WAFBlockedError):
            await client.get("https://shop.example/products/blocked")
    await client.close()

    recs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("403" in r.getMessage() for r in recs)
