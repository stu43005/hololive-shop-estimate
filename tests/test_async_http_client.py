import asyncio

import pytest

from estimator_king.config_schema import CrawlerPolicy, ProxyConfig
from estimator_king.crawler.async_http_client import (
    AsyncDomainRateLimiter,
    AsyncHTTPClient,
    CircuitBreakerOpenError,
    WAFBlockedError,
)


class _FakeResponse:
    def __init__(
        self, status: int, text_value: str = "", headers: dict[str, str] | None = None
    ):
        self.status = status
        self._text_value = text_value
        self.headers = headers or {}

    async def text(self) -> str:
        return self._text_value


class _FakeRequestContextManager:
    def __init__(self, response: _FakeResponse, on_enter=None, on_exit=None):
        self._response = response
        self._on_enter = on_enter
        self._on_exit = on_exit

    async def __aenter__(self):
        if self._on_enter:
            await self._on_enter()
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        if self._on_exit:
            await self._on_exit()
        return False


class _FakeSession:
    def __init__(self, request_factory):
        self._request_factory = request_factory
        self.closed = False
        self.last_kwargs: dict[str, object] = {}

    def request(self, method: str, url: str, timeout=None, **kwargs):  # pyright: ignore[reportUnusedParameter]
        self.last_kwargs = kwargs
        return self._request_factory(method, url)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_semaphore_limits_per_domain_concurrency(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=2, max_retries=1
    )

    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def on_enter():
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        await asyncio.sleep(0.02)

    async def on_exit():
        nonlocal active
        async with lock:
            active -= 1

    def request_factory(method: str, url: str):
        return _FakeRequestContextManager(
            _FakeResponse(200, "ok"), on_enter=on_enter, on_exit=on_exit
        )

    fake_session = _FakeSession(request_factory)
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    client = AsyncHTTPClient(policy)
    results = await asyncio.gather(
        *[client.get("https://example.com/products/1") for _ in range(6)]
    )
    await client.close()

    assert results == ["ok"] * 6
    assert max_active == 2


@pytest.mark.asyncio
async def test_get_returns_response_text(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )

    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(
            _FakeResponse(200, "hello async")
        )
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    client = AsyncHTTPClient(policy)
    body = await client.get("https://shop.example/products/a")
    await client.close()

    assert body == "hello async"


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_repeated_failures(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    calls = {"n": 0}

    def request_factory(method: str, url: str):
        calls["n"] += 1
        return _FakeRequestContextManager(_FakeResponse(403, "blocked"))

    fake_session = _FakeSession(request_factory)
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    client = AsyncHTTPClient(policy)

    for _ in range(3):
        with pytest.raises(WAFBlockedError):
            await client.get("https://blocked.example/item")

    with pytest.raises(CircuitBreakerOpenError):
        await client.get("https://blocked.example/item")

    await client.close()
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_rate_limiter_delays_requests_to_same_domain():
    now = {"t": 0.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    limiter = AsyncDomainRateLimiter(
        rate_limit_rps=1.0,
        jitter_max=0.5,
        sleep_fn=fake_sleep,
        monotonic_fn=lambda: now["t"],
        uniform_fn=lambda a, b: 0.5,
    )

    await limiter.wait("example.com")
    await limiter.wait("example.com")

    assert sleeps == [1.5]


@pytest.mark.asyncio
async def test_async_context_manager_closes_session(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )

    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    async with AsyncHTTPClient(policy) as client:
        assert await client.get("https://example.com/") == "ok"

    assert fake_session.closed is True


@pytest.mark.asyncio
async def test_proxy_selected_by_target_scheme(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=True, http_proxy="http://hp:8080", https_proxy="http://sp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://hp:8080"
    assert fake_session.last_kwargs.get("proxy_auth") is None

    await client.get("https://secure.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://sp:8080"

    await client.close()


@pytest.mark.asyncio
async def test_proxy_disabled_sends_no_proxy(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=False, http_proxy="http://hp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert "proxy" not in fake_session.last_kwargs

    await client.close()


@pytest.mark.asyncio
async def test_proxy_enabled_but_selected_value_empty_sends_no_proxy(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    # enabled, but https_proxy is empty and target is https -> no proxy
    proxy = ProxyConfig(enabled=True, http_proxy="http://hp:8080", https_proxy="")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("https://secure.example/x")
    assert "proxy" not in fake_session.last_kwargs

    await client.close()


@pytest.mark.asyncio
async def test_proxy_credentials_split_into_basic_auth(monkeypatch):
    from aiohttp import BasicAuth

    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=1
    )
    fake_session = _FakeSession(
        lambda method, url: _FakeRequestContextManager(_FakeResponse(200, "ok"))
    )
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )
    proxy = ProxyConfig(enabled=True, http_proxy="http://user:pass@hp:8080")
    client = AsyncHTTPClient(policy, proxy=proxy)

    await client.get("http://plain.example/x")
    assert str(fake_session.last_kwargs["proxy"]) == "http://hp:8080"
    assert fake_session.last_kwargs["proxy_auth"] == BasicAuth("user", "pass")

    await client.close()


@pytest.mark.asyncio
async def test_retry_after_header_honored_on_429(monkeypatch):
    policy = CrawlerPolicy(
        rate_limit_rps=1000.0, jitter_max=0.0, concurrency_per_domain=1, max_retries=3
    )
    calls = {"n": 0}

    def request_factory(method: str, url: str):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRequestContextManager(
                _FakeResponse(429, "slow down", headers={"Retry-After": "2"})
            )
        return _FakeRequestContextManager(_FakeResponse(200, "ok"))

    fake_session = _FakeSession(request_factory)
    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = AsyncHTTPClient(policy, sleep_fn=fake_sleep)

    body = await client.get("https://example.com/")
    await client.close()

    assert body == "ok"
    assert calls["n"] == 2
    assert 2.0 in sleeps  # Retry-After header value honored by tenacity wait
