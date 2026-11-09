"""Seam #1 -- Transport: the only place that performs network I/O.

The interface is deliberately tiny and deliberately ignorant. A non-200 is
*returned*, not raised, because what a 404 or a 503 means is IDX-specific
knowledge and that knowledge belongs above this seam, in `IdxClient`.

Three adapters make the seam real:

- `PlaywrightTransport` (`idx_reach.playwright_transport`) -- the live site.
- `ReplayTransport` -- recorded fixtures; the default in tests.
- `FakeTransport` -- scripted responses for error-path tests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

BASE_URL = "https://www.idx.co.id"


def utc_now() -> datetime:
    """A single place to read the wall clock, so responses can be stamped in tests."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class JsonResponse:
    """A decoded JSON response. `body` is None when the body was not JSON."""

    status: int
    body: Any
    url: str
    fetched_at: datetime


@dataclass(frozen=True)
class BytesResponse:
    """A raw byte response, e.g. an XBRL workbook or a PDF."""

    status: int
    body: bytes
    url: str
    fetched_at: datetime
    content_type: str = ""


@runtime_checkable
class Transport(Protocol):
    """The seam. Two methods, no IDX knowledge."""

    def get_json(self, path: str, params: Mapping[str, str | int]) -> JsonResponse: ...

    def get_bytes(self, url: str) -> BytesResponse: ...


def build_url(path: str, params: Mapping[str, str | int]) -> str:
    """Join `BASE_URL`, an API path and a query string."""
    query = urlencode(dict(params))
    return f"{BASE_URL}{path}?{query}" if query else f"{BASE_URL}{path}"


def fixture_key(url: str) -> str:
    """The fixture filename stem for a URL: its SHA-256.

    Hashing keeps parameterised URLs out of filenames and out of logs, which the
    source session found were filtered as query-string data (record §7.4).
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class MissingFixtureError(LookupError):
    """Raised when a replayed request has no recorded fixture."""


@dataclass
class ScriptedResponse:
    """One entry in a `FakeTransport` script."""

    status: int
    json_body: Any = None
    body: bytes = b""
    content_type: str = ""


class FakeTransport:
    """Programmable responses for error-path tests: 429 streaks, 503, truncated bodies."""

    def __init__(self, script: Sequence[ScriptedResponse]) -> None:
        self._script = list(script)
        self.requested: list[str] = []

    def get_json(self, path: str, params: Mapping[str, str | int]) -> JsonResponse:
        url = build_url(path, params)
        scripted = self._next(url)
        return JsonResponse(
            status=scripted.status, body=scripted.json_body, url=url, fetched_at=utc_now()
        )

    def get_bytes(self, url: str) -> BytesResponse:
        scripted = self._next(url)
        return BytesResponse(
            status=scripted.status,
            body=scripted.body,
            url=url,
            fetched_at=utc_now(),
            content_type=scripted.content_type,
        )

    def _next(self, url: str) -> ScriptedResponse:
        self.requested.append(url)
        if not self._script:
            raise AssertionError(f"FakeTransport script exhausted; next request was {url}")
        return self._script.pop(0)


@dataclass
class ReplayTransport:
    """Serves recorded fixtures from a directory. The default in tests.

    Layout per request: `<sha256>.json` or `<sha256>.bin` for the body, plus a
    `<sha256>.meta.json` sidecar holding the URL, the status and the headers.
    """

    fixture_dir: Path | str
    requested: list[str] = field(default_factory=list)

    def get_json(self, path: str, params: Mapping[str, str | int]) -> JsonResponse:
        url = build_url(path, params)
        meta = self._meta(url)
        body_path = self._dir / f"{fixture_key(url)}.json"
        body = json.loads(body_path.read_text(encoding="utf-8")) if body_path.is_file() else None
        return JsonResponse(
            status=int(meta["status"]), body=body, url=url, fetched_at=self._fetched_at(meta)
        )

    def get_bytes(self, url: str) -> BytesResponse:
        meta = self._meta(url)
        body_path = self._dir / f"{fixture_key(url)}.bin"
        body = body_path.read_bytes() if body_path.is_file() else b""
        headers = {k.lower(): v for k, v in dict(meta.get("headers") or {}).items()}
        return BytesResponse(
            status=int(meta["status"]),
            body=body,
            url=url,
            fetched_at=self._fetched_at(meta),
            content_type=headers.get("content-type", ""),
        )

    @property
    def _dir(self) -> Path:
        return Path(self.fixture_dir)

    def _meta(self, url: str) -> dict[str, Any]:
        self.requested.append(url)
        key = fixture_key(url)
        meta_path = self._dir / f"{key}.meta.json"
        if not meta_path.is_file():
            raise MissingFixtureError(
                f"no recorded fixture for {url}\n"
                f"expected {meta_path} (key {key}).\n"
                "Capture it with: idx-reach record <url>"
            )
        return json.loads(meta_path.read_text(encoding="utf-8"))

    @staticmethod
    def _fetched_at(meta: Mapping[str, Any]) -> datetime:
        recorded = meta.get("fetched_at")
        if isinstance(recorded, str):
            return datetime.fromisoformat(recorded)
        return utc_now()


def write_fixture(
    fixture_dir: Path,
    url: str,
    status: int,
    headers: Mapping[str, str],
    body: bytes,
    fetched_at: datetime | None = None,
) -> Path:
    """Write a fixture triple for `url`. Used by `idx-reach record` and by the fixture builder."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    key = fixture_key(url)
    content_type = next(
        (v for k, v in headers.items() if k.lower() == "content-type"),
        "",
    )
    suffix = "json" if "json" in content_type.lower() else "bin"
    body_path = fixture_dir / f"{key}.{suffix}"
    body_path.write_bytes(body)
    (fixture_dir / f"{key}.meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "status": status,
                "headers": dict(headers),
                "fetched_at": (fetched_at or utc_now()).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return body_path
