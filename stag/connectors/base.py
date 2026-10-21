"""Shared connector plumbing: errors with a clear outcome meaning, an injectable HTTP transport and
a recorded transport for tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class ConnectorError(Exception):
    """The connector could not complete the call; the outcome at the destination is unknown."""


class ConnectorTimeout(ConnectorError):
    """No usable response (timeout, connection loss, 5xx): the effect may or may not have happened."""


class ConnectorRejected(ConnectorError):
    """The destination refused the request (4xx, validation): the effect definitely did not happen."""


class NoConnectorError(ConnectorError):
    """No configured connector accepts this operation or channel."""


def redact(value: str | None, keep: int = 4) -> str:
    """Show only the tail of a secret-bearing value."""
    if not value:
        return ""
    return "****" + value[-keep:] if len(value) > keep else "****"


@dataclass
class HttpResponse:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self, limit: int = 300) -> str:
        return (self.body if isinstance(self.body, str) else repr(self.body))[:limit]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> HttpResponse: ...


class HttpxTransport:
    """Real HTTP via httpx. Timeouts and transport failures become ``ConnectorTimeout``."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def request(self, method, url, *, headers=None, json=None, params=None, timeout=None):
        import httpx

        try:
            response = httpx.request(method, url, headers=headers, json=json, params=params, timeout=timeout or self.timeout)
        except httpx.TimeoutException as e:
            raise ConnectorTimeout(f"{method} {url}: timeout") from e
        except httpx.HTTPError as e:
            raise ConnectorTimeout(f"{method} {url}: {type(e).__name__}") from e
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return HttpResponse(response.status_code, body, dict(response.headers))


@dataclass
class RecordedCall:
    method: str
    url: str
    headers: dict[str, str]
    json: Any
    params: dict[str, Any] | None


class RecordedTransport:
    """Scripted responses for tests. Each entry is an ``HttpResponse``, an exception to raise, or a
    callable receiving the ``RecordedCall``. Every call is recorded for assertions."""

    def __init__(self, responses: list[HttpResponse | Exception | Callable[[RecordedCall], HttpResponse]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[RecordedCall] = []

    def request(self, method, url, *, headers=None, json=None, params=None, timeout=10.0):
        call = RecordedCall(method, url, dict(headers or {}), json, params)
        self.calls.append(call)
        if not self.responses:
            raise AssertionError(f"RecordedTransport: unexpected call {method} {url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt(call)
        return nxt
