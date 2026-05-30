import logging

import pytest
import requests

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler.http_client import HTTPClient, WAFBlockedError


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}


def _client(monkeypatch, status_code):
    session = requests.Session()

    def fake_request(method, url, timeout=None, **kwargs):
        return _FakeResponse(status_code)

    monkeypatch.setattr(session, "request", fake_request)
    return HTTPClient(
        crawler_policy=CrawlerPolicy(
            rate_limit_rps=1000.0, jitter_max=0.0,
            concurrency_per_domain=1, max_retries=1,
        ),
        session=session,
    )


def test_debug_logs_successful_request(monkeypatch, caplog):
    client = _client(monkeypatch, 200)
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.http_client"):
        client.get("https://example.com/page")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.http_client" and r.levelno == logging.DEBUG
    ]
    assert any(
        "GET" in r.getMessage()
        and "https://example.com/page" in r.getMessage()
        and "200" in r.getMessage()
        and "ms" in r.getMessage()
        for r in recs
    )


def test_debug_logs_waf_status_before_raise(monkeypatch, caplog):
    client = _client(monkeypatch, 403)
    with caplog.at_level(logging.DEBUG, logger="estimator_king.crawler.http_client"):
        with pytest.raises(WAFBlockedError):
            client.get("https://example.com/page")

    recs = [
        r for r in caplog.records
        if r.name == "estimator_king.crawler.http_client" and r.levelno == logging.DEBUG
    ]
    assert any("403" in r.getMessage() for r in recs)
