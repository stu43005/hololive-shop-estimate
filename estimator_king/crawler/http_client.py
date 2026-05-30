"""HTTP client policies for crawling.

Policies:
- Per-domain rate limiting with jitter
- Retry for 429 (honor Retry-After) and retryable 5xx (exponential backoff)
- Circuit breaker for 403/430 blocks (WAF-style)
- Optional proxy support via config/env
"""

# pyright: reportMissingImports=false
# pyright: reportMissingModuleSource=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportMissingParameterType=false

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import logging

from .. import __version__
from ..config_schema import CrawlerPolicy, ProxyConfig

logger = logging.getLogger(__name__)


class HTTPClientError(Exception):
    """Base error for HTTP client policy failures."""


class RateLimitError(HTTPClientError):
    """Raised on HTTP 429 responses."""

    url: str
    status_code: int
    retry_after: float | None
    response: requests.Response | None

    def __init__(
        self,
        url: str,
        status_code: int,
        retry_after: float | None,
        response: requests.Response | None = None,
    ):
        super().__init__(f"rate limited: {status_code} {url}")
        self.url = url
        self.status_code = status_code
        self.retry_after = retry_after
        self.response = response


class ServerError(HTTPClientError):
    """Raised on retryable 5xx responses."""

    url: str
    status_code: int
    response: requests.Response | None

    def __init__(
        self, url: str, status_code: int, response: requests.Response | None = None
    ):
        super().__init__(f"server error: {status_code} {url}")
        self.url = url
        self.status_code = status_code
        self.response = response


class WAFBlockedError(HTTPClientError):
    """Raised on WAF/block-style responses (403/430)."""

    url: str
    status_code: int
    response: requests.Response | None

    def __init__(
        self, url: str, status_code: int, response: requests.Response | None = None
    ):
        super().__init__(f"blocked (possible WAF): {status_code} {url}")
        self.url = url
        self.status_code = status_code
        self.response = response


class CircuitBreakerOpenError(HTTPClientError):
    """Raised when circuit breaker is open for a domain."""

    domain: str
    retry_in_seconds: float

    def __init__(self, domain: str, retry_in_seconds: float):
        super().__init__(
            f"circuit open for {domain} (retry in {retry_in_seconds:.1f}s)"
        )
        self.domain = domain
        self.retry_in_seconds = retry_in_seconds


def _parse_retry_after(value: str | None, *, now: Callable[[], float]) -> float | None:
    """Parse Retry-After value.

    Supports delta-seconds per RFC 9110. If header is missing/invalid returns None.
    """

    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    # Most common: delta seconds
    if value.isdigit():
        return float(int(value))

    # HTTP-date form is rare; avoid hard dependency on date parsing.
    # Best-effort parsing using stdlib.
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        # Convert to epoch seconds
        epoch = dt.timestamp()
        delta = epoch - now()
        return max(0.0, float(delta))
    except Exception:
        return None


def _wait_http(retry_state) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, RateLimitError) and exc.retry_after is not None:
        return float(exc.retry_after)
    exp = wait_exponential(multiplier=1.0, min=4.0, max=10.0)
    return float(exp(retry_state))


class DomainRateLimiter:
    """Per-domain delay with jitter.

    Applies a minimum interval of (1 / rate_limit_rps) seconds between requests,
    plus a random jitter in [0, jitter_max].
    """

    def __init__(
        self,
        rate_limit_rps: float,
        jitter_max: float,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        if rate_limit_rps <= 0:
            raise ValueError("rate_limit_rps must be > 0")
        if jitter_max < 0:
            raise ValueError("jitter_max must be >= 0")

        self._base_delay: float = 1.0 / float(rate_limit_rps)
        self._jitter_max: float = float(jitter_max)
        self._sleep: Callable[[float], None] = sleep_fn
        self._monotonic: Callable[[], float] = monotonic_fn
        self._uniform: Callable[[float, float], float] = uniform_fn
        self._last_request_by_domain: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        now = self._monotonic()
        last = self._last_request_by_domain.get(domain)
        if last is None:
            self._last_request_by_domain[domain] = now
            return

        required = self._base_delay + self._uniform(0.0, self._jitter_max)
        sleep_for = max(0.0, (last + required) - now)
        if sleep_for > 0:
            self._sleep(sleep_for)
        self._last_request_by_domain[domain] = self._monotonic()


@dataclass
class _CircuitState:
    failures: int = 0
    state: str = "closed"  # closed, open, half_open
    open_until: float = 0.0


class DomainCircuitBreaker:
    """Per-domain circuit breaker for 403/430 blocks."""

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

    def before_request(self, domain: str) -> None:
        st = self._state.get(domain)
        if not st or st.state != "open":
            return
        now = self._monotonic()
        if now >= st.open_until:
            st.state = "half_open"
            return
        raise CircuitBreakerOpenError(domain, retry_in_seconds=st.open_until - now)

    def record_success(self, domain: str) -> None:
        st = self._state.setdefault(domain, _CircuitState())
        st.failures = 0
        st.state = "closed"
        st.open_until = 0.0

    def record_waf_failure(self, domain: str) -> None:
        st = self._state.setdefault(domain, _CircuitState())
        st.failures += 1
        if st.failures >= self._failure_threshold:
            now = self._monotonic()
            st.state = "open"
            st.open_until = now + self._open_timeout
        else:
            # stay closed or half_open until tripped
            if st.state == "half_open":
                st.state = "closed"


def _domain_from_url(url: str) -> str:
    parts = urlsplit(url)
    return parts.hostname or parts.netloc or ""


class HTTPClient:
    """HTTP client for crawling with policies applied."""

    def __init__(
        self,
        crawler_policy: CrawlerPolicy | None = None,
        proxy: ProxyConfig | None = None,
        *,
        session: requests.Session | None = None,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_open_timeout_seconds: float = 60.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        uniform_fn: Callable[[float, float], float] = random.uniform,
    ):
        self._policy: CrawlerPolicy = crawler_policy or CrawlerPolicy()
        self._proxy: ProxyConfig = proxy or ProxyConfig()

        self.session: requests.Session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": f"Mozilla/5.0 (compatible; EstimatorKing/{__version__})",
                "Accept": "text/html,application/json",
                "Accept-Language": "ja,en",
                "Accept-Encoding": "gzip, deflate",
            }
        )

        if self._proxy.enabled:
            proxies: dict[str, str] = {}
            if self._proxy.http_proxy:
                proxies["http"] = self._proxy.http_proxy
            if self._proxy.https_proxy:
                proxies["https"] = self._proxy.https_proxy
            if proxies:
                self.session.proxies.update(proxies)

        self._rate_limiter: DomainRateLimiter = DomainRateLimiter(
            rate_limit_rps=float(self._policy.rate_limit_rps),
            jitter_max=float(self._policy.jitter_max),
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            uniform_fn=uniform_fn,
        )
        self._circuit_breaker: DomainCircuitBreaker = DomainCircuitBreaker(
            failure_threshold=circuit_breaker_failure_threshold,
            open_timeout_seconds=circuit_breaker_open_timeout_seconds,
            monotonic_fn=monotonic_fn,
        )

        # Tenacity retry wrapper built dynamically to honor configured max_retries.
        attempts = max(1, int(self._policy.max_retries))

        self._request_with_retry: Callable[..., requests.Response] = retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            retry=retry_if_exception_type((RateLimitError, ServerError)),
            wait=_wait_http,
            sleep=sleep_fn,
        )(self._request_once)

    def get(self, url: str, **kwargs: Any) -> requests.Response:  # pyright: ignore[reportExplicitAny, reportAny]
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # pyright: ignore[reportExplicitAny, reportAny]
        return self._request_with_retry(method, url, **kwargs)

    def _request_once(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # pyright: ignore[reportExplicitAny, reportAny]
        domain = _domain_from_url(url)
        self._circuit_breaker.before_request(domain)
        self._rate_limiter.wait(domain)

        timeout = kwargs.pop(
            "timeout",
            (float(self._policy.timeout_connect), float(self._policy.timeout_read)),
        )
        start = time.monotonic()
        resp = self.session.request(method, url, timeout=timeout, **kwargs)  # pyright: ignore[reportAny]

        status = int(getattr(resp, "status_code", 0) or 0)
        logger.debug(
            "%s %s -> %s in %.0fms",
            method, url, status, (time.monotonic() - start) * 1000.0,
        )
        if status in (403, 430):
            self._circuit_breaker.record_waf_failure(domain)
            raise WAFBlockedError(url, status_code=status, response=resp)

        if status == 429:
            retry_after = _parse_retry_after(
                resp.headers.get("Retry-After"), now=time.time
            )
            raise RateLimitError(
                url, status_code=status, retry_after=retry_after, response=resp
            )

        if 500 <= status <= 599:
            raise ServerError(url, status_code=status, response=resp)

        # Consider any non-WAF success as clearing the breaker.
        self._circuit_breaker.record_success(domain)
        return resp
