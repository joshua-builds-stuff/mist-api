#!/usr/bin/env python3
"""Small, security-conscious client shared by the Mist API examples.

Requires Python 3.10+ and ``requests``.  The client validates the complete API
origin before it ever attaches the token to a request, does not follow
redirects, and retries only read-only HTTP methods unless explicitly enabled.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

DEFAULT_BASE_URL = "https://api.mist.com/api/v1"
OFFICIAL_API_HOSTS = frozenset(
    {
        "api.mist.com",
        "api.gc1.mist.com",
        "api.gc2.mist.com",
        "api.gc3.mist.com",
        "api.gc4.mist.com",
        "api.gc5.mist.com",
        "api.gc6.mist.com",
        "api.gc7.mist.com",
        "api.ac2.mist.com",
        "api.ac5.mist.com",
        "api.ac6.mist.com",
        "api.eu.mist.com",
    }
)
SAFE_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

logger = logging.getLogger("mist-client")


class MistAPIError(RuntimeError):
    """A deliberately secret-free Mist API failure."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def outcome_indeterminate(self) -> bool:
        """Transport errors and server failures do not prove a write rejected."""
        return (
            self.status_code is None
            or not 400 <= self.status_code < 500
            or self.status_code == 408
        )


def _json_payload(
    json_body: Mapping[str, Any] | Sequence[Any] | None,
) -> dict[str, Any] | list[Any] | None:
    """Shallow-copy a JSON object or array body; reject every other shape."""

    if json_body is None:
        return None
    if isinstance(json_body, Mapping):
        return dict(json_body)
    if isinstance(json_body, Sequence) and not isinstance(
        json_body, (str, bytes, bytearray)
    ):
        return list(json_body)
    raise ValueError("Mist API json_body must be a JSON object or array")


def api_path_segment(value: str) -> str:
    """Percent-encode an identifier before interpolating it into an API path."""

    if not isinstance(value, str) or not value:
        raise ValueError("Mist API path identifiers must be non-empty strings")
    return quote(value, safe="")


def normalize_base_url(base_url: str, *, allow_custom: bool = False) -> str:
    """Validate and normalize a Mist API base URL before authentication.

    Official regional hosts are accepted by default.  A private proxy or test
    service requires ``allow_custom=True`` and must still use HTTPS.
    """

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Mist base URL must be a non-empty HTTPS URL")

    candidate = base_url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise ValueError("Mist base URL must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Mist base URL must contain a host and no credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Mist base URL must not contain a query string or fragment")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Mist base URL contains an invalid port") from exc

    host = parsed.hostname.lower()
    if not allow_custom and (host not in OFFICIAL_API_HOSTS or port not in (None, 443)):
        raise ValueError(
            "Custom Mist API hosts require --allow-custom-base-url; "
            "use the regional api.*.mist.com host for the account"
        )

    path = parsed.path.rstrip("/")
    if path != "/api/v1":
        raise ValueError("Mist base URL path must be /api/v1")

    if not allow_custom:
        # Canonicalizing official origins also removes an unnecessary :443.
        netloc = host
    else:
        netloc = parsed.netloc.lower()
    return urlunsplit(("https", netloc, path, "", ""))


def _validate_api_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError(
            "Mist API path must be an origin-relative path beginning with one slash"
        )
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(
            "Mist API path must not change origins or contain query/fragment data"
        )
    if "\\" in path or any(ord(character) < 32 for character in path):
        raise ValueError("Mist API path contains unsafe characters")
    return path


class MistClient:
    """Reusable Mist API session with bounded, read-only retries."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        allow_custom_base_url: bool = False,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_retry_delay: float = 30.0,
        retry_jitter: float = 0.25,
        retry_unsafe_methods: bool = False,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        # URL validation intentionally precedes creation/mutation of the session
        # and construction of the Authorization header.
        validated_base_url = normalize_base_url(
            base_url,
            allow_custom=allow_custom_base_url,
        )
        if (
            not isinstance(token, str)
            or not token.strip()
            or "\n" in token
            or "\r" in token
        ):
            raise ValueError("Mist API token must be a non-empty single-line value")
        if not 0 < timeout <= 300:
            raise ValueError("timeout must be greater than 0 and at most 300 seconds")
        if not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be between 0 and 10")
        if not 0 < max_retry_delay <= 60:
            raise ValueError(
                "max_retry_delay must be greater than 0 and at most 60 seconds"
            )
        if not 0 <= retry_jitter <= 5:
            raise ValueError("retry_jitter must be between 0 and 5 seconds")

        self.base_url = validated_base_url
        self._authorization = f"Token {token.strip()}"
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_retry_delay = max_retry_delay
        self.retry_jitter = retry_jitter
        self.retry_unsafe_methods = retry_unsafe_methods
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._sleep = sleep
        self._random_value = random_value

    def __enter__(self) -> MistClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def _delay_for_retry(self, attempt: int, retry_after: str | None) -> float:
        delay: float | None = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = None

        if delay is None:
            delay = 0.5 * (2**attempt)
        delay = max(0.0, delay)
        jitter = max(0.0, min(1.0, self._random_value())) * self.retry_jitter
        return min(self.max_retry_delay, delay + jitter)

    def _can_retry(self, method: str) -> bool:
        return method in SAFE_RETRY_METHODS or self.retry_unsafe_methods

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> requests.Response:
        """Send one API request without exposing response bodies in errors."""

        normalized_method = method.upper()
        safe_path = _validate_api_path(path)
        url = f"{self.base_url}{safe_path}"
        payload = _json_payload(json_body)
        may_retry = self._can_retry(normalized_method)
        retry_count = self.max_retries if may_retry else 0

        for attempt in range(retry_count + 1):
            try:
                response = self._session.request(
                    normalized_method,
                    url,
                    headers={
                        "Authorization": self._authorization,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    params=dict(params) if params is not None else None,
                    json=payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= retry_count:
                    raise MistAPIError(
                        f"Mist API {normalized_method} request failed after "
                        f"{attempt + 1} attempt(s): {type(exc).__name__}"
                    ) from None
                delay = self._delay_for_retry(attempt, None)
                logger.warning(
                    "Mist API %s request failed transiently; retrying in %.2fs",
                    normalized_method,
                    delay,
                )
                self._sleep(delay)
                continue
            except requests.RequestException as exc:
                raise MistAPIError(
                    f"Mist API {normalized_method} request failed: {type(exc).__name__}"
                ) from None

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < retry_count:
                delay = self._delay_for_retry(
                    attempt, response.headers.get("Retry-After")
                )
                status_code = response.status_code
                response.close()
                logger.warning(
                    "Mist API %s returned HTTP %s; retrying in %.2fs",
                    normalized_method,
                    status_code,
                    delay,
                )
                self._sleep(delay)
                continue

            if not 200 <= response.status_code < 300:
                status_code = response.status_code
                response.close()
                raise MistAPIError(
                    f"Mist API {normalized_method} request failed with HTTP {status_code}",
                    status_code=status_code,
                )
            return response

        raise AssertionError("unreachable retry state")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> Any:
        response = self.request(method, path, params=params, json_body=json_body)
        try:
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise MistAPIError(
                    f"Mist API {method.upper()} response was not valid JSON"
                ) from None
        finally:
            response.close()

    def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = 100,
        max_pages: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every object from a page/limit Mist list endpoint."""

        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if not 1 <= max_pages <= 10000:
            raise ValueError("max_pages must be between 1 and 10000")

        base_params = dict(params or {})
        base_params.pop("page", None)
        base_params.pop("limit", None)
        for page in range(1, max_pages + 1):
            page_params = {**base_params, "limit": page_size, "page": page}
            payload = self.request_json("GET", path, params=page_params)
            if not isinstance(payload, list):
                raise MistAPIError("Mist API paginated response was not a JSON array")
            for item in payload:
                if not isinstance(item, dict):
                    raise MistAPIError(
                        "Mist API paginated response contained a non-object item"
                    )
                yield item
            if len(payload) < page_size:
                return

        # A full final page is only an overflow if another page has data.
        probe_params = {**base_params, "limit": page_size, "page": max_pages + 1}
        probe = self.request_json("GET", path, params=probe_params)
        if not isinstance(probe, list):
            raise MistAPIError("Mist API paginated response was not a JSON array")
        if not probe:
            return
        raise MistAPIError(
            f"Mist API pagination exceeded the {max_pages}-page safety limit"
        )
