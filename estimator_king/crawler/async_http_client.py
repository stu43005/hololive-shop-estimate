# pyright: reportMissingImports=false
# pyright: reportMissingModuleSource=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportMissingParameterType=false

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .. import __version__
from ..config_schema import CrawlerPolicy


class AsyncHTTPClientError(Exception):
    pass


class RateLimitError(AsyncHTTPClientError):
    url: str
    status_code: int
    retry_after: float | None

    def __init__(self, url: str, status_code: int, retry_after: float | None):
        super().__init__(f"rate limited: {status_code} {url}")
        self.url = url
        self.status_code = status_code
        self.retry_after = retry_after


class ServerError(AsyncHTTPClientError):
    url: str
    status_code: int

    def __init__(self, url: str, status_code: int):
        super().__init__(f"server error: {status_code} {url}")
        self.url = url
        self.status_code = status_code


class WAFBlockedError(AsyncHTTPClientError):
    url: str
    status_code: int

    def __init__(self, url: str, status_code: int):
        super().__init__(f"blocked (possible WAF): {status_code} {url}")
        self.url = url
        self.status_code = status_code


class CircuitBreakerOpenError(AsyncHTTPClientError):
    domain: str
    retry_in_seconds: float

    def __init__(self, domain: str, retry_in_seconds: float):
        super().__init__(
            f"circuit open for {domain} (retry in {retry_in_seconds:.1f}s)"
        )
        self.domain = domain
        self.retry_in_seconds = retry_in_seconds


def _parse_retry_after(value: str | None, *, now: Callable[[], float]) -> float | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(int(value))
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        epoch = dt.timestamp()
        return max(0.0, float(epoch - now()))
    except Exception:
        return None


def _wait_http(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        return float(exc.retry_after)
    exp = wait_exponential(multiplier=1.0, min=4.0, max=10.0)
    return float(exp(retry_state))


class AsyncDomainRateLimiter:
    def __init__(
        self,
        rate_limit_rps: float,
        jitter_max: float,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        if rate_limit_rps <= 0:
            raise ValueError("rate_limit_rps must be > 0")
        if jitter_max < 0:
            raise ValueError("jitter_max must be >= 0")

        self._base_delay: float = 1.0 / float(rate_limit_rps)
        self._jitter_max: float = float(jitter_max)
        self._sleep: Callable[[float], Awaitable[None]] = sleep_fn
        self._monotonic: Callable[[], float] = monotonic_fn
        self._uniform: Callable[[float, float], float] = uniform_fn
        self._last_request_by_domain: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        lock = self._domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = self._monotonic()
            last = self._last_request_by_domain.get(domain)
            if last is None:
                self._last_request_by_domain[domain] = now
                return

            required = self._base_delay + self._uniform(0.0, self._jitter_max)
            sleep_for = max(0.0, (last + required) - now)
            if sleep_for > 0:
                await self._sleep(sleep_for)
            self._last_request_by_domain[domain] = self._monotonic()


@dataclass
class _CircuitState:
    failures: int = 0
    state: str = "closed"
    open_until: float = 0.0


class AsyncDomainCircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_timeout_seconds: float = 60.0,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self._failure_threshold: int = failure_threshold
        self._open_timeout: float = open_timeout_seconds
        self._monotonic: Callable[[], float] = monotonic_fn
        self._state: dict[str, _CircuitState] = {}

    async def before_request(self, domain: str) -> None:
        st = self._state.get(domain)
        if not st or st.state != "open":
            return
        now = self._monotonic()
        if now >= st.open_until:
            st.state = "half_open"
            return
        raise CircuitBreakerOpenError(domain, retry_in_seconds=st.open_until - now)

    async def record_success(self, domain: str) -> None:
        st = self._state.setdefault(domain, _CircuitState())
        st.failures = 0
        st.state = "closed"
        st.open_until = 0.0

    async def record_waf_failure(self, domain: str) -> None:
        st = self._state.setdefault(domain, _CircuitState())
        st.failures += 1
        if st.failures >= self._failure_threshold:
            now = self._monotonic()
            st.state = "open"
            st.open_until = now + self._open_timeout
        elif st.state == "half_open":
            st.state = "closed"


def _domain_from_url(url: str) -> str:
    parts = urlsplit(url)
    return parts.hostname or parts.netloc or ""


class AsyncHTTPClient:
    def __init__(
        self,
        policy: CrawlerPolicy,
        *,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_open_timeout_seconds: float = 60.0,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        self._policy = policy
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}

        self._rate_limiter = AsyncDomainRateLimiter(
            rate_limit_rps=float(self._policy.rate_limit_rps),
            jitter_max=float(self._policy.jitter_max),
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            uniform_fn=uniform_fn,
        )
        self._circuit_breaker = AsyncDomainCircuitBreaker(
            failure_threshold=circuit_breaker_failure_threshold,
            open_timeout_seconds=circuit_breaker_open_timeout_seconds,
            monotonic_fn=monotonic_fn,
        )

        attempts = max(1, int(self._policy.max_retries))
        self._request_with_retry = retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            retry=retry_if_exception_type((RateLimitError, ServerError)),
            wait=_wait_http,
            sleep=sleep_fn,
        )(self._request_once)

    async def __aenter__(self) -> AsyncHTTPClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers={
                        "User-Agent": f"Mozilla/5.0 (compatible; EstimatorKing/{__version__})",
                        "Accept": "text/html,application/json",
                        "Accept-Language": "ja,en",
                        "Accept-Encoding": "gzip, deflate",
                    }
                )
        return self._session

    def _get_domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        return self._domain_semaphores.setdefault(
            domain,
            asyncio.Semaphore(int(self._policy.concurrency_per_domain)),
        )

    async def get(self, url: str) -> str:
        return await self._request_with_retry(url)

    async def _request_once(self, url: str) -> str:
        domain = _domain_from_url(url)
        await self._circuit_breaker.before_request(domain)

        semaphore = self._get_domain_semaphore(domain)
        async with semaphore:
            await self._rate_limiter.wait(domain)
            timeout = aiohttp.ClientTimeout(
                sock_connect=float(self._policy.timeout_connect),
                sock_read=float(self._policy.timeout_read),
            )
            session = await self._get_session()
            async with session.request("GET", url, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                if status in (403, 430):
                    await self._circuit_breaker.record_waf_failure(domain)
                    raise WAFBlockedError(url, status_code=status)

                if status == 429:
                    retry_after = _parse_retry_after(
                        resp.headers.get("Retry-After"), now=time.time
                    )
                    raise RateLimitError(
                        url,
                        status_code=status,
                        retry_after=retry_after,
                    )

                if 500 <= status <= 599:
                    raise ServerError(url, status_code=status)

                if 400 <= status <= 499:
                    raise HTTPClientError(url, status_code=status)

                await self._circuit_breaker.record_success(domain)
                return await resp.text()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
