"""Tests for seam #1, Transport (Task 3).

Three adapters make this seam real. Two of them live here; the third,
PlaywrightTransport, is the only code allowed to touch idx.co.id and is never
exercised by the default suite.
"""

import json
from datetime import datetime

import pytest

from idx_reach.transport import (
    BASE_URL,
    BytesResponse,
    FakeTransport,
    JsonResponse,
    MissingFixtureError,
    ReplayTransport,
    ScriptedResponse,
    Transport,
    fixture_key,
)


def test_both_adapters_satisfy_the_transport_interface():
    assert isinstance(FakeTransport([]), Transport)
    assert isinstance(ReplayTransport(fixture_dir="."), Transport)


def test_a_non_200_is_returned_not_raised():
    """IdxClient decides what a 404 or a 503 means; the transport stays dumb."""
    transport = FakeTransport([ScriptedResponse(status=503, json_body=None)])

    response = transport.get_json("/primary/ListedCompany/GetAnnouncement", {"pageSize": 1})

    assert isinstance(response, JsonResponse)
    assert response.status == 503


def test_get_json_builds_the_url_from_base_path_and_params():
    transport = FakeTransport([ScriptedResponse(status=200, json_body={"ResultCount": 0})])

    response = transport.get_json("/primary/ListedCompany/GetAnnouncement", {"pageSize": 1})

    assert response.url == f"{BASE_URL}/primary/ListedCompany/GetAnnouncement?pageSize=1"
    assert transport.requested == [response.url]


def test_every_response_carries_its_url_and_fetch_timestamp():
    """Global Constraint 4: every stored row carries its source URL and fetch time."""
    transport = FakeTransport([ScriptedResponse(status=200, body=b"PK\x03\x04")])

    response = transport.get_bytes("https://www.idx.co.id/Portals/0/x.xlsx")

    assert isinstance(response, BytesResponse)
    assert response.url == "https://www.idx.co.id/Portals/0/x.xlsx"
    assert isinstance(response.fetched_at, datetime)
    assert response.fetched_at.tzinfo is not None


def test_get_bytes_leaves_the_url_exactly_as_given():
    """§2.1: the doubled slash and the escaped spaces must survive the transport."""
    url = f"{BASE_URL}/Portals/0/a//Laporan%20Keuangan%20Tahun%202021/x.xlsx"
    transport = FakeTransport([ScriptedResponse(status=404, body=b"")])

    response = transport.get_bytes(url)

    assert response.url == url
    assert transport.requested == [url]


def test_fake_transport_runs_out_of_script_loudly():
    transport = FakeTransport([])

    with pytest.raises(AssertionError) as excinfo:
        transport.get_bytes("https://www.idx.co.id/x")

    assert "https://www.idx.co.id/x" in str(excinfo.value)


def test_replay_returns_the_recorded_status_and_json_body(tmp_path):
    url = f"{BASE_URL}/primary/ListedCompany/GetCompanyProfiles?emitenType=s"
    key = fixture_key(url)
    (tmp_path / f"{key}.json").write_text(json.dumps([{"KodeEmiten": "TLKM"}]), encoding="utf-8")
    (tmp_path / f"{key}.meta.json").write_text(
        json.dumps({"url": url, "status": 200, "headers": {"content-type": "application/json"}}),
        encoding="utf-8",
    )
    transport = ReplayTransport(fixture_dir=tmp_path)

    response = transport.get_json("/primary/ListedCompany/GetCompanyProfiles", {"emitenType": "s"})

    assert response.status == 200
    assert response.body == [{"KodeEmiten": "TLKM"}]
    assert response.url == url


def test_replay_returns_the_recorded_status_and_byte_body(tmp_path):
    url = f"{BASE_URL}/Portals/0/broken.xlsx"
    key = fixture_key(url)
    (tmp_path / f"{key}.bin").write_bytes(b"\rAdministrator")
    (tmp_path / f"{key}.meta.json").write_text(
        json.dumps(
            {
                "url": url,
                "status": 200,
                "headers": {
                    "content-type": (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    transport = ReplayTransport(fixture_dir=tmp_path)

    response = transport.get_bytes(url)

    assert response.status == 200
    assert response.body == b"\rAdministrator"
    assert "spreadsheetml" in response.content_type


def test_a_missing_fixture_raises_an_error_naming_the_url(tmp_path):
    """Never silently return empty: that is how a dataset goes quietly wrong."""
    transport = ReplayTransport(fixture_dir=tmp_path)
    url = f"{BASE_URL}/Portals/0/absent.xlsx"

    with pytest.raises(MissingFixtureError) as excinfo:
        transport.get_bytes(url)

    message = str(excinfo.value)
    assert url in message
    assert fixture_key(url) in message
    assert "record" in message
