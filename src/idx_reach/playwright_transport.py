"""Task 9 -- `PlaywrightTransport`: the live adapter, and the only code here
allowed to touch idx.co.id.

Why a browser at all: a plain HTTP client cannot reach IDX. `curl` with a Chrome
User-Agent answers HTTP 403 with a 6354-byte Cloudflare JavaScript challenge page
(record §1.1). Changing the User-Agent does not help -- the challenge has to be
executed. The only thing that worked was `fetch()` run *inside* a loaded
www.idx.co.id page with `credentials: "include"` (record §1.2).

Never export `cf_clearance` or `__cf_bm` to another HTTP client. That is
bot-check circumvention, and the tokens are bound to User-Agent and IP anyway,
so it is fragile as well as wrong (Global Constraint 3, record §1.3).

For the same reason this adapter carries **no stealth flags** and does not
pretend to be a human-driven browser. It runs as an ordinary automated browser;
the Cloudflare challenge is cleared once, by a person, in a visible window, and
the persistent profile keeps that result. If IDX decides to refuse automated
clients, that refusal is theirs to make and this tool must surface it, not
defeat it.

Exactly one URL per `evaluate` call; `RateLimiter` paces the queue. Awaiting a
queued batch inside one `evaluate` hit
`CDP sendCommand "Runtime.evaluate" timed out after 45000ms` while the in-page
queue kept running underneath, and seven files were downloaded twice
(record §7.1).

Everything above this seam -- `IdxClient`, `XbrlReader`, `Store`, the CLI -- is
tested against `ReplayTransport` and `FakeTransport` and does not change when
this adapter is swapped in.
"""

from __future__ import annotations

import base64
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Any

from .transport import BASE_URL, BytesResponse, JsonResponse, build_url, utc_now

USER_DATA_DIR = Path.home() / ".idx-reach" / "browser"

#: Markers of the Cloudflare *interstitial*, used by `probe`.
#:
#: `challenge-platform` is deliberately NOT here. idx.co.id loads
#: `/cdn-cgi/challenge-platform/scripts/precursor/main.js` on every normal page,
#: so matching it reports a challenge on a page that served fine. An earlier
#: revision did exactly that and `probe()` failed while both live data tests
#: passed in the same run.
_CHALLENGE_MARKERS = ("Just a moment", "cf-browser-verification", "cf_chl_opt")

#: A cheap, real request used to settle `probe` by behaviour rather than by
#: sniffing markup.
_PROBE_PATH = "/primary/ListedCompany/GetCompanyProfiles"
_PROBE_PARAMS = {"emitenType": "s", "start": 0, "length": 1}

#: One request per call. The body is copied out in chunks: a per-byte
#: `String.fromCharCode` loop over a 1.2 MB workbook is both slow and liable to
#: blow the argument limit when spread into `fromCharCode`.
_FETCH_JS = """
async (url) => {
    const response = await fetch(url, { credentials: "include" });
    const bytes = new Uint8Array(await response.arrayBuffer());
    let binary = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    const headers = {};
    response.headers.forEach((value, key) => { headers[key] = value; });
    return {
        status: response.status,
        headers: headers,
        body_b64: btoa(binary),
    };
}
"""


class BrowserUnavailableError(RuntimeError):
    """Raised when the browser context cannot be started or cannot reach IDX."""


class PlaywrightTransport:
    """A persistent browser context with one page kept open on www.idx.co.id.

    The context is persistent on purpose: the Cloudflare handshake is solved once
    -- by hand on first run, in a visible window -- and reused afterwards. The
    page stays open between requests; navigating away loses the solved state.
    """

    def __init__(
        self,
        user_data_dir: Path = USER_DATA_DIR,
        headless: bool = False,
        navigation_timeout_ms: int = 120_000,
        request_timeout_ms: int = 180_000,
    ) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.request_timeout_ms = request_timeout_ms
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> PlaywrightTransport:
        """Launch the persistent context and park a page on the IDX home page."""
        if self._page is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - import guard
            raise BrowserUnavailableError(
                "playwright is not installed. Install it with: uv add playwright "
                "&& uv run playwright install chromium"
            ) from exc

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        try:
            # No stealth flags. `--disable-blink-features=AutomationControlled`
            # and friends exist to hide automation from bot detection, which is
            # circumvention, not configuration (Global Constraint 3). This runs
            # as what it is: an automated browser, with a human clearing the
            # Cloudflare challenge once in a visible window.
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            self.close()
            raise BrowserUnavailableError(f"could not launch a browser context: {exc}") from exc

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_navigation_timeout(self.navigation_timeout_ms)
        self._page.set_default_timeout(self.request_timeout_ms)
        self._page.goto(BASE_URL, wait_until="domcontentloaded")
        return self

    def close(self) -> None:
        """Close the page, the context and the driver. Safe to call twice."""
        for attr in ("_context", "_playwright"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            with contextlib.suppress(Exception):  # teardown is best effort
                obj.close() if attr == "_context" else obj.stop()
            setattr(self, attr, None)
        self._page = None

    def __enter__(self) -> PlaywrightTransport:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- Transport ---------------------------------------------------------

    def get_json(self, path: str, params: Mapping[str, str | int]) -> JsonResponse:
        url = build_url(path, params)
        raw = self._fetch(url)
        body: Any
        try:
            body = json.loads(raw.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # A non-JSON body comes back as None with the real status. Deciding
            # what a 404 or a Varnish 503 means belongs to IdxClient, not here.
            body = None
        return JsonResponse(status=raw.status, body=body, url=url, fetched_at=raw.fetched_at)

    def get_bytes(self, url: str) -> BytesResponse:
        return self._fetch(url)

    def _fetch(self, url: str) -> BytesResponse:
        page = self._require_page()
        result = page.evaluate(_FETCH_JS, url)
        headers = {str(k).lower(): str(v) for k, v in dict(result.get("headers") or {}).items()}
        return BytesResponse(
            status=int(result["status"]),
            body=base64.b64decode(result["body_b64"]),
            url=url,
            fetched_at=utc_now(),
            content_type=headers.get("content-type", ""),
        )

    def _require_page(self) -> Any:
        if self._page is None:
            self.start()
        if self._page is None:  # pragma: no cover - start() raises on failure
            raise BrowserUnavailableError("browser page is not available")
        return self._page

    # -- doctor ------------------------------------------------------------

    def probe(self) -> tuple[bool, str]:
        """Report (reachable, detail) for `doctor`.

        Settled by behaviour, not by markup: the question that matters is
        whether a real request to an IDX endpoint comes back as JSON. Marker
        sniffing alone is unreliable, because the site serves Cloudflare's
        `challenge-platform` script on pages that load perfectly well.
        """
        try:
            page = self._require_page()
        except BrowserUnavailableError as exc:
            return False, str(exc)
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded")
            content = page.content()
        except Exception as exc:  # pragma: no cover - environment dependent
            return False, f"could not load {BASE_URL}: {exc}"

        interstitial = next((m for m in _CHALLENGE_MARKERS if m in content), None)

        try:
            response = self.get_json(_PROBE_PATH, _PROBE_PARAMS)
        except Exception as exc:  # pragma: no cover - environment dependent
            return False, f"page loaded but the probe request failed: {exc}"

        if response.status == 200 and isinstance(response.body, dict):
            total = response.body.get("recordsTotal")
            return True, (
                f"loaded {page.url} ({len(content)} chars); "
                f"probe request returned HTTP 200 with recordsTotal={total}"
            )

        if interstitial is not None:
            return False, (
                f"Cloudflare interstitial is showing (marker {interstitial!r}) and the "
                f"probe request returned HTTP {response.status}. Run once with "
                "headless=False and clear the challenge by hand; the persistent "
                "context keeps the result."
            )
        return False, (
            f"page loaded but the probe request returned HTTP {response.status} "
            f"with a {'non-JSON' if response.body is None else 'JSON'} body"
        )
