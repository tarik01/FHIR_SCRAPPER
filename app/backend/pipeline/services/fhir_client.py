import asyncio
import logging
import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from django.conf import settings

log = logging.getLogger("pipeline.http")

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class FhirError(Exception):
    pass


class PermanentHttpError(FhirError):
    def __init__(self, step, status):
        super().__init__(f"{step}: HTTP {status}")
        self.step = step
        self.status = status


class RetriesExhausted(FhirError):
    def __init__(self, step, error, status=None):
        super().__init__(f"{step}: retries exhausted ({error})")
        self.step = step
        self.error = error
        self.status = status


class CircuitOpen(FhirError):
    pass


class UnsafeUrl(FhirError):
    pass


class CircuitBreaker:
    def __init__(self, threshold):
        self.threshold = threshold
        self.consecutive_failures = 0

    @property
    def is_open(self):
        return self.consecutive_failures >= self.threshold

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1


def retry_after_seconds(response):
    value = response.headers.get("Retry-After")
    if not value:
        return None
    if value.strip().isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


def backoff_seconds(attempt):
    return min(settings.RETRY_MAX_BACKOFF_SECONDS, 2**attempt) + random.uniform(0, 1)


class FhirClient:
    def __init__(self, base_url=None, http=None, sleep=asyncio.sleep, breaker=None):
        self.base_url = (base_url or settings.FHIR_BASE_URL).rstrip("/")
        self.http = http
        self.sleep = sleep
        self.breaker = breaker or CircuitBreaker(settings.CIRCUIT_BREAKER_THRESHOLD)
        self._owns_http = http is None

    async def __aenter__(self):
        if self.http is None:
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.HTTP_TIMEOUT_SECONDS, connect=5)
            )
        return self

    async def __aexit__(self, *exc):
        if self._owns_http:
            await self.http.aclose()

    def resolve(self, path_or_url):
        if "://" in path_or_url:
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"
        self.check_origin(url)
        return url

    def check_origin(self, url):
        base, target = urlsplit(self.base_url), urlsplit(url)
        same_host = (target.scheme, target.netloc) == (base.scheme, base.netloc)
        if not same_host or not target.path.startswith(base.path):
            raise UnsafeUrl(f"unexpected origin: {target.scheme}://{target.netloc}{target.path}")

    async def get(self, path_or_url, *, step, headers=None, params=None):
        url = self.resolve(path_or_url)
        error, status = None, None
        for attempt in range(1, settings.RETRY_MAX_ATTEMPTS + 1):
            if self.breaker.is_open:
                raise CircuitOpen(
                    f"circuit open after {self.breaker.consecutive_failures} consecutive failures"
                )
            try:
                response = await self.http.get(url, headers=headers, params=params)
            except httpx.TransportError as exc:
                error, status, wait = type(exc).__name__, None, backoff_seconds(attempt)
            else:
                status = response.status_code
                if response.is_success:
                    self.breaker.record_success()
                    return response
                if status not in TRANSIENT_STATUSES:
                    self.breaker.record_success()
                    raise PermanentHttpError(step, status)
                error = f"http_{status}"
                wait = retry_after_seconds(response)
                if wait is None:
                    wait = backoff_seconds(attempt)
            self.breaker.record_failure()
            if attempt == settings.RETRY_MAX_ATTEMPTS:
                break
            log.warning(
                "request_retry",
                extra={"step": step, "attempt": attempt, "http_status": status,
                       "error": error, "wait_s": round(wait, 2)},
            )
            await self.sleep(wait)
        raise RetriesExhausted(step, error, status)
