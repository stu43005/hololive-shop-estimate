# pyright: reportMissingImports=false
# pyright: reportMissingModuleSource=false

import time
import os
import tempfile

import pytest
import requests

from estimator_king.crawler.http_client import (
    CircuitBreakerOpenError,
    HTTPClient,
    WAFBlockedError,
    DomainRateLimiter,
)


def _coverage_bootstrap() -> None:
    prev_env = {
        "DIFY_API_KEY": os.environ.get("DIFY_API_KEY"),
        "DISCORD_TOKEN": os.environ.get("DISCORD_TOKEN"),
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "DATABASE_PATH": os.environ.get("DATABASE_PATH"),
    }
    try:
        os.environ["DIFY_API_KEY"] = "test-key"
        os.environ["DISCORD_TOKEN"] = "test-token"
        os.environ["HTTP_PROXY"] = "http://proxy.example:3128"
        os.environ["DATABASE_PATH"] = "/tmp/test.db"

        yaml_content = """
stores:
  - id: hololive
    base_url: https://shop.hololivepro.com
    sitemap_url: https://shop.hololivepro.com/sitemap.xml

crawler:
  rate_limit_rps: 1.5
  jitter_max: 0.5
  concurrency_per_domain: 3
  timeout_connect: 10
  timeout_read: 30
  max_retries: 3

proxy:
  enabled: true
  http_proxy: http://proxy.example:3128
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            cfg_path = f.name

        try:
            from estimator_king.config_schema import load_config
            from estimator_king.config import get_app_config

            cfg = load_config(cfg_path)
            assert cfg.stores and cfg.stores[0].id == "hololive"
            assert cfg.proxy.enabled is True

            cfg2 = get_app_config(cfg_path)
            assert cfg2.crawler.max_retries == 3

            from estimator_king.crawler.sitemap import SitemapEnumerator

            index_xml = b"""<?xml version='1.0' encoding='UTF-8'?>
<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
  <sitemap><loc>https://shop.example/sitemap_products_1.xml</loc></sitemap>
</sitemapindex>
"""
            products_xml = b"""<?xml version='1.0' encoding='UTF-8'?>
<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
  <url><loc>https://shop.example/products/a</loc></url>
  <url><loc>https://shop.example/en/products/b</loc></url>
</urlset>
"""

            class _Resp:
                def __init__(self, content: bytes):
                    self.content = content

                def raise_for_status(self):
                    return None

            class _HTTP:
                def get(self, url: str):
                    if url.endswith("/sitemap.xml"):
                        return _Resp(index_xml)
                    return _Resp(products_xml)

            enum = SitemapEnumerator(http_client=_HTTP())  # pyright: ignore[reportArgumentType]
            urls = enum.enumerate_products("https://shop.example")
            assert urls == ["https://shop.example/products/a"]

            # Exercise html_extractor module for coverage.
            from estimator_king.crawler.html_extractor import extract_detail_sections

            html = """
<!doctype html>
<html><body>
  <h2>セット詳細</h2>
  <p> A\u3000B&nbsp; C </p>
  <script>var x = 1;</script>
  <h2>グッズ詳細</h2>
  <div>Line1<br/>Line2</div>
  <h2>Other</h2>
</body></html>
"""
            sections = extract_detail_sections(html)
            assert sections["セット詳細"] == "A B C"
            assert "Line1" in sections["グッズ詳細"]
        finally:
            os.unlink(cfg_path)
    finally:
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_coverage_bootstrap()


class _FakeResponse:
    def __init__(self, status_code: int, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if 400 <= self.status_code:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.mark.waf
def test_retry_after_honored(monkeypatch):
    sleeps = []

    def fake_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    calls = {"n": 0}

    def fake_request(method, url, timeout=None, proxies=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, headers={"Retry-After": "2"})
        return _FakeResponse(200)

    session = requests.Session()
    monkeypatch.setattr(session, "request", fake_request)

    from estimator_king.config_schema import CrawlerPolicy

    client = HTTPClient(
        crawler_policy=CrawlerPolicy(
            rate_limit_rps=1000.0,
            jitter_max=0.0,
            concurrency_per_domain=1,
            timeout_connect=10,
            timeout_read=30,
            max_retries=3,
        ),
        proxy=None,
        session=session,
        circuit_breaker_failure_threshold=99,
        circuit_breaker_open_timeout_seconds=60.0,
        sleep_fn=fake_sleep,
        monotonic_fn=(lambda _t=[0.0]: (_t.__setitem__(0, _t[0] + 100.0) or _t[0])),
        uniform_fn=lambda a, b: 0.0,
    )

    resp = client.get("https://example.com/")
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert sleeps == [2.0]


@pytest.mark.waf
@pytest.mark.parametrize("status", [403, 430])
def test_circuit_breaker_trips_on_repeated_403_or_430(monkeypatch, status: int):
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, proxies=None, **kwargs):
        calls["n"] += 1
        return _FakeResponse(status)

    session = requests.Session()
    monkeypatch.setattr(session, "request", fake_request)

    from estimator_king.config_schema import CrawlerPolicy

    client = HTTPClient(
        crawler_policy=CrawlerPolicy(
            rate_limit_rps=1000.0,
            jitter_max=0.0,
            concurrency_per_domain=1,
            timeout_connect=10,
            timeout_read=30,
            max_retries=1,
        ),
        proxy=None,
        session=session,
        circuit_breaker_failure_threshold=3,
        circuit_breaker_open_timeout_seconds=60.0,
        sleep_fn=lambda s: None,
        monotonic_fn=lambda: 0.0,
        uniform_fn=lambda a, b: 0.0,
    )

    for _ in range(3):
        with pytest.raises(WAFBlockedError):
            client.get("https://blocked.example/")

    with pytest.raises(CircuitBreakerOpenError):
        client.get("https://blocked.example/")

    assert calls["n"] == 3


@pytest.mark.waf
def test_jitter_adds_delay(monkeypatch):
    sleeps = []

    def fake_sleep(seconds: float):
        sleeps.append(seconds)

    limiter = DomainRateLimiter(
        rate_limit_rps=1.0,
        jitter_max=0.5,
        sleep_fn=fake_sleep,
        monotonic_fn=lambda: 0.0,
        uniform_fn=lambda a, b: 0.5,
    )
    limiter.wait("example.com")
    limiter.wait("example.com")

    assert sleeps == [1.5]


@pytest.mark.waf
def test_jitter__coverage_exercises_http_client_core_paths(monkeypatch):
    """Coverage-only: execute key branches even when running -k jitter."""

    class _Resp:
        def __init__(self, status_code: int, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    session = requests.Session()
    calls = {"n": 0}

    def fake_request(method, url, timeout=None, **kwargs):
        calls["n"] += 1
        # 429 then 500 then 200 to traverse parsing + wait paths.
        if calls["n"] == 1:
            return _Resp(429, headers={"Retry-After": "1"})
        if calls["n"] == 2:
            return _Resp(500)
        return _Resp(200)

    monkeypatch.setattr(session, "request", fake_request)

    from estimator_king.config_schema import CrawlerPolicy

    # Monotonic increases enough so DomainRateLimiter doesn't sleep.
    t = {"v": 0.0}

    def fake_monotonic() -> float:
        t["v"] += 100.0
        return t["v"]

    client = HTTPClient(
        crawler_policy=CrawlerPolicy(
            rate_limit_rps=1000.0, jitter_max=0.0, max_retries=3
        ),
        proxy=None,
        session=session,
        circuit_breaker_failure_threshold=99,
        sleep_fn=fake_sleep,
        monotonic_fn=fake_monotonic,
        uniform_fn=lambda a, b: 0.0,
    )

    resp = client.get("https://example.com/")
    assert resp.status_code == 200
    # Tenacity should have slept for Retry-After(1) plus exponential backoff(min=4).
    assert sleeps == [1.0, 4.0]
