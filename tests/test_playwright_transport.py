"""Tests for the live adapter.

Two kinds live here:

- Offline tests of everything that does not need a browser: construction,
  idempotent teardown, the Transport protocol shape.
- `@pytest.mark.network` tests that drive the real idx.co.id. They are
  deselected by default (`addopts = "-m 'not network'"`, Global Constraint 1)
  and need a person to clear the Cloudflare challenge once in a visible window.

Run the live ones deliberately:

    uv run pytest -m network -s
"""

from __future__ import annotations

import time

import pytest

from idx_reach.playwright_transport import PlaywrightTransport
from idx_reach.transport import Transport

TLKM_2023 = (
    "https://www.idx.co.id/Portals/0/StaticData/ListedCompanies/Corporate_Actions/"
    "New_Info_JSX/Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan//"
    "Laporan%20Keuangan%20Tahun%202023/Audit/TLKM/FinancialStatement-2023-Tahunan-TLKM.xlsx"
)

#: Global Constraint 2. The empirically safe spacing: 330 consecutive requests
#: at 6.5 s produced zero HTTP 429 in the source session.
SPACING_SECONDS = 6.5


def test_it_satisfies_the_transport_protocol() -> None:
    assert isinstance(PlaywrightTransport(), Transport)


def test_construction_does_not_launch_a_browser() -> None:
    """Constructing is cheap and side-effect free; `start()` is what launches."""
    transport = PlaywrightTransport()
    assert transport._page is None
    assert transport._context is None


def test_close_is_idempotent() -> None:
    transport = PlaywrightTransport()
    transport.close()
    transport.close()
    assert transport._page is None


def test_it_carries_no_stealth_flags() -> None:
    """Hiding automation from bot detection is circumvention (Global Constraint 3)."""
    from pathlib import Path

    source = Path("src/idx_reach/playwright_transport.py").read_text(encoding="utf-8")
    launch_call = source.split("launch_persistent_context(", 1)[1].split(")", 1)[0]
    assert "args=" not in launch_call, "no browser flags may be passed at launch"


@pytest.mark.network
def test_live_probe_clears_cloudflare() -> None:
    with PlaywrightTransport(headless=False) as transport:
        reachable, detail = transport.probe()
        print(f"\nprobe: {reachable} -- {detail}")
        assert reachable, detail


@pytest.mark.network
def test_live_json_endpoint_returns_profiles() -> None:
    with PlaywrightTransport(headless=False) as transport:
        time.sleep(SPACING_SECONDS)
        response = transport.get_json(
            "/primary/ListedCompany/GetCompanyProfiles",
            {"emitenType": "s", "start": 0, "length": 2},
        )
        assert response.status == 200
        assert isinstance(response.body, dict)
        assert response.body["recordsTotal"] > 500


@pytest.mark.network
def test_live_static_xbrl_path_returns_a_zip() -> None:
    """The doubled slash and the escaped spaces must survive the round trip."""
    with PlaywrightTransport(headless=False) as transport:
        time.sleep(SPACING_SECONDS)
        response = transport.get_bytes(TLKM_2023)
        assert response.status == 200
        assert response.body[:4] == b"PK\x03\x04", "not a ZIP; see record §2.8"
        assert len(response.body) > 100_000


def test_challenge_platform_is_not_a_challenge_marker() -> None:
    """Regression: idx.co.id serves that script on normal pages.

    An earlier revision listed `challenge-platform` as an interstitial marker.
    `probe()` then reported a challenge while both live data tests passed in
    the same run -- a false negative on a working transport.
    """
    from idx_reach.playwright_transport import _CHALLENGE_MARKERS

    assert not any("challenge-platform" in marker for marker in _CHALLENGE_MARKERS)


@pytest.mark.network
def test_live_body_survives_the_base64_copy_out_byte_for_byte() -> None:
    """Prove the chunked base64 copy-out does not corrupt the body.

    A truncated or re-encoded body still begins `PK\x03\x04` and may still
    open, so neither the magic number nor a successful parse is evidence. The
    ZIP's own CRC-32, one per entry, is: `testzip()` returns the name of the
    first entry whose stored CRC does not match its decompressed bytes, and
    None when every entry checks out.

    This is preferred over asserting a hard-coded SHA-256: the digest would go
    stale the moment IDX reissues the workbook, and a stale digest fails for
    the wrong reason.
    """
    import io
    import zipfile

    from idx_reach.xbrl import XbrlReader

    with PlaywrightTransport(headless=False) as transport:
        time.sleep(SPACING_SECONDS)
        response = transport.get_bytes(TLKM_2023)

    assert response.status == 200
    archive = zipfile.ZipFile(io.BytesIO(response.body))
    assert archive.testzip() is None, "a ZIP entry failed its CRC-32 check"

    result = XbrlReader().read_general_info(response.body, source_url=TLKM_2023)
    info = result.value
    assert info is not None, result.detail
    entity = info.normalised("Entity name")
    period_end = info.normalised("Current period end date")
    print(f"\nentity: {entity!r}  period end: {period_end!r}")
    assert "Telkom" in (entity or "")
    assert period_end == "2023-12-31"
