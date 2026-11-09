"""Tests for IdxClient (Task 5), all against FakeTransport.

IdxClient is the deep module: four public methods, every IDX quirk from §2
hidden behind them.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from idx_reach.client import (
    SUSPENSION_KEYWORDS,
    WARRANT_EXCLUSION,
    IdxClient,
    IncompleteResultError,
    excluded_as_warrant,
    static_statement_url,
)
from idx_reach.models import RowStatus
from idx_reach.ratelimit import ManualClock, RateLimitBreakerTripped, RateLimiter
from idx_reach.transport import FakeTransport, ScriptedResponse

JSON_FIXTURES = Path(__file__).parent / "fixtures" / "json"
XBRL_FIXTURES = Path(__file__).parent / "fixtures" / "xbrl"


def body(name: str):
    return json.loads((JSON_FIXTURES / f"announcement_{name}.json").read_text(encoding="utf-8"))


def client_for(*script: ScriptedResponse) -> tuple[IdxClient, FakeTransport]:
    transport = FakeTransport(script)
    return (
        IdxClient(transport=transport, limiter=RateLimiter(clock=ManualClock())),
        transport,
    )


# -- §2.1 static XBRL path ------------------------------------------------------


def test_the_static_path_keeps_the_doubled_slash_and_escapes_spaces():
    url = static_statement_url("TLKM", 2021)

    assert "02_Soft_Copy_Laporan_Keuangan//Laporan%20Keuangan%20Tahun%202021" in url
    assert "/Audit/TLKM/FinancialStatement-2021-Tahunan-TLKM.xlsx" in url
    assert url.startswith("https://www.idx.co.id/Portals/0/StaticData/")
    assert "%2F" not in url  # encodeURI, not encodeURIComponent: slashes survive
    assert " " not in url


def test_financial_statement_requests_exactly_that_url():
    client, transport = client_for(ScriptedResponse(status=404))

    client.financial_statement("LUCK", 2024)

    assert transport.requested == [static_statement_url("LUCK", 2024)]


def test_a_404_on_the_statement_is_not_listed():
    client, _ = client_for(ScriptedResponse(status=404))

    result = client.financial_statement("TECH", 2021)

    assert result.status is RowStatus.NOT_LISTED
    assert result.value is None
    assert "404" in result.detail


def test_a_200_that_is_not_a_zip_is_file_corrupt_not_not_listed():
    """§2.8: the audit report existed in both real cases; the file did not."""
    client, _ = client_for(
        ScriptedResponse(
            status=200,
            body=(XBRL_FIXTURES / "corrupt_165_bytes.bin").read_bytes(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    )

    result = client.financial_statement("LUCK", 2024)

    assert result.status is RowStatus.FILE_CORRUPT
    assert result.value is None
    assert "attachment" in result.detail


def test_a_real_workbook_comes_back_ok_with_its_bytes():
    data = (XBRL_FIXTURES / "new_taxonomy_2024.xlsx").read_bytes()
    client, _ = client_for(ScriptedResponse(status=200, body=data))

    result = client.financial_statement("TLKM", 2024)

    assert result.status is RowStatus.OK
    assert result.value == data
    assert result.source_url == static_statement_url("TLKM", 2024)


def test_a_429_on_the_statement_is_a_network_error():
    client, _ = client_for(ScriptedResponse(status=429))

    result = client.financial_statement("TLKM", 2024)

    assert result.status is RowStatus.NETWORK_ERROR


def test_three_consecutive_429s_trip_the_breaker():
    client, _ = client_for(*[ScriptedResponse(status=429)] * 3)

    client.financial_statement("TLKM", 2023)
    client.financial_statement("TLKM", 2024)
    with pytest.raises(RateLimitBreakerTripped):
        client.financial_statement("TLKM", 2025)


# -- §2.2 ResultCount reconciliation -------------------------------------------


def test_result_count_745_with_250_rows_raises_incomplete_result():
    """§2.2: silent truncation is the single most dangerous failure here."""
    client, _ = client_for(
        ScriptedResponse(status=200, json_body=body("probe")),
        ScriptedResponse(status=200, json_body=body("truncated")),
    )

    with pytest.raises(IncompleteResultError) as excinfo:
        client.announcements(keyword="Pemantauan Khusus")

    message = str(excinfo.value)
    assert "745" in message
    assert "250" in message


def test_announcements_probe_with_page_size_1_then_refetch_at_result_count():
    client, transport = client_for(
        ScriptedResponse(status=200, json_body=body("collective")),
        ScriptedResponse(status=200, json_body=body("collective")),
    )

    client.announcements(keyword="Penghentian Sementara Perdagangan")

    assert "pageSize=1" in transport.requested[0]
    assert "pageSize=1&" in transport.requested[1] or transport.requested[1].endswith("pageSize=1")
    assert len(transport.requested) == 2


def test_announcements_asks_for_at_least_result_count_rows():
    truncated = body("truncated")
    probe = body("probe")
    rows = {**truncated, "ResultCount": 250}
    client, transport = client_for(
        ScriptedResponse(status=200, json_body={**probe, "ResultCount": 250}),
        ScriptedResponse(status=200, json_body=rows),
    )

    result = client.announcements(keyword="Pemantauan Khusus")

    assert "pageSize=250" in transport.requested[1]
    assert len(result.value) == 250


def test_a_collective_announcement_keeps_an_empty_code_and_is_flagged():
    """§2.5: only 6 of 745 carried a code; a per-issuer query misses the rest."""
    client, _ = client_for(
        ScriptedResponse(status=200, json_body=body("collective")),
        ScriptedResponse(status=200, json_body=body("collective")),
    )

    result = client.announcements(keyword="Penghentian Sementara Perdagangan")

    row = result.value[0]
    assert row.kode is None
    assert row.is_collective is True
    assert row.attachments[0].file_name == "Peng-S-00016.pdf"


def test_a_padded_issuer_code_is_trimmed():
    client, _ = client_for(
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 250}),
        ScriptedResponse(status=200, json_body=body("truncated")),
    )

    result = client.announcements(kode="TECH")
    coded = [row for row in result.value if row.kode]

    assert coded
    assert all(row.kode == "TECH" for row in coded)


# -- §2.3 the three-year window ------------------------------------------------


def test_the_live_window_is_parsed_from_search_params():
    client, _ = client_for(
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 0}),
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 0}),
    )

    client.announcements(keyword="Pemantauan Khusus")

    assert client.announcement_window == (date(2023, 11, 9), date(2026, 11, 9))


def test_a_year_before_the_window_yields_out_of_window_rows_not_an_empty_list():
    """§2.3 and §5: never report "no events" for an unverifiable range."""
    client, _ = client_for(
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 0}),
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 0}),
    )

    result = client.announcements(kode="TECH", tahun=(2021, 2022))

    assert result.status is RowStatus.OUT_OF_WINDOW
    assert [row.status for row in result.value] == [
        RowStatus.OUT_OF_WINDOW,
        RowStatus.OUT_OF_WINDOW,
    ]
    assert "ticmi" in result.detail.casefold()


def test_a_range_partly_inside_the_window_reports_out_of_window_for_the_gap():
    client, _ = client_for(
        ScriptedResponse(status=200, json_body={**body("probe"), "ResultCount": 1}),
        ScriptedResponse(status=200, json_body={**body("collective"), "ResultCount": 1}),
    )

    result = client.announcements(kode="TECH", tahun=(2022, 2025))
    out_of_window = [row for row in result.value if row.status is RowStatus.OUT_OF_WINDOW]

    assert result.status is RowStatus.OUT_OF_WINDOW
    assert [row.tanggal for row in out_of_window] == ["2022"]
    assert "TICMI" in out_of_window[0].judul
    assert any(row.status is RowStatus.OK for row in result.value)


# -- §2.4 503 is not rate limiting ---------------------------------------------


def test_a_503_is_a_network_error_and_does_not_trip_the_breaker():
    """§2.4: Varnish 503 means a rejected parameter, not a rate limit."""
    client, _ = client_for(*[ScriptedResponse(status=503, json_body=body("varnish_503"))] * 10)

    for _ in range(10):
        result = client.announcements(keyword="Pemantauan Khusus")
        assert result.status is RowStatus.NETWORK_ERROR

    assert client.limiter.tripped is False


def test_a_404_on_announcements_is_not_listed():
    client, _ = client_for(ScriptedResponse(status=404, json_body=body("not_found_404")))

    result = client.announcements(kode="TIDAKADA")

    assert result.status is RowStatus.NOT_LISTED


# -- profiles and attachments --------------------------------------------------


def test_profiles_maps_the_documented_fields():
    client, transport = client_for(
        ScriptedResponse(
            status=200,
            json_body={
                "data": [
                    {
                        "KodeEmiten": "TLKM",
                        "NamaEmiten": "PT Replika Sintetis Tbk",
                        "Sektor": "Infrastructures",
                        "SubSektor": "Telecommunication",
                        "Industri": "Telecommunication",
                        "SubIndustri": "Telecommunication Service",
                        "PapanPencatatan": "Utama",
                        "TanggalPencatatan": "1995-11-14T00:00:00",
                    }
                ]
            },
        )
    )

    result = client.profiles()

    assert result.status is RowStatus.OK
    assert result.value[0].kode == "TLKM"
    assert result.value[0].papan_pencatatan == "Utama"
    assert "length=9999" in transport.requested[0]


def test_attachments_lists_the_files_for_one_issuer_year():
    client, transport = client_for(
        ScriptedResponse(
            status=200,
            json_body={
                "Results": [
                    {
                        "Attachments": [
                            {
                                "File_Name": "FinancialStatement-2024-Tahunan-LUCK.pdf",
                                "File_Path": "/StaticData/x/replica.pdf",
                                "File_Size": 2048,
                                "File_Modified": "2025-03-06T00:00:00",
                            }
                        ]
                    }
                ]
            },
        )
    )

    result = client.attachments("LUCK", 2024)

    assert result.status is RowStatus.OK
    assert result.value[0].file_size == 2048
    assert "kodeEmiten=LUCK" in transport.requested[0]
    assert "periode=audit" in transport.requested[0]


def test_an_empty_attachment_list_is_ok_with_a_delisting_hint():
    """record §2.2: a delisted issuer returns an empty list, XBRL notwithstanding."""
    client, _ = client_for(ScriptedResponse(status=200, json_body={"Results": []}))

    result = client.attachments("GONE", 2024)

    assert result.status is RowStatus.OK
    assert result.value == []
    assert "delisted" in result.detail


# -- §2.6 keyword set and the warrant exclusion --------------------------------


def test_the_keyword_set_is_shipped_verbatim():
    assert SUSPENSION_KEYWORDS == (
        "Penghentian Sementara Perdagangan",
        "Pembukaan Suspensi",
        "Pencabutan",
        "Suspensi",
        "Pemantauan Khusus",
        "Sanksi Penyampaian Laporan Keuangan",
        "Free Float",
    )


def test_warrant_delistings_are_excluded_but_share_delistings_are_not():
    """§6.2: a naive `delisting` filter wrongly eliminated 13 healthy issuers."""
    assert WARRANT_EXCLUSION == "Waran Seri"
    assert (
        excluded_as_warrant("Mengingatkan Jadwal Penghapusan (Delisting) Efek Waran Seri I") is True
    )
    assert excluded_as_warrant("Penghentian Sementara Perdagangan Efek PT Contoh Tbk") is False


# -- pacing --------------------------------------------------------------------


def test_every_request_passes_through_the_rate_limiter():
    clock = ManualClock()
    limiter = RateLimiter(clock=clock)
    transport = FakeTransport([ScriptedResponse(status=404), ScriptedResponse(status=404)])
    client = IdxClient(transport=transport, limiter=limiter)

    client.financial_statement("TLKM", 2024)
    client.financial_statement("TLKM", 2025)

    assert clock.slept == [pytest.approx(6.5)]
