"""Tests for the shared models: the row-status enum and value normalisation (§2.9, §3)."""

from datetime import UTC, datetime

import pytest

from idx_reach.models import (
    FetchResult,
    FieldValue,
    GeneralInfo,
    OpinionKind,
    RowStatus,
    normalise_date,
    normalise_firm_name,
    normalise_opinion,
)


def test_row_status_has_exactly_the_seven_documented_values():
    assert [s.value for s in RowStatus] == [
        "OK",
        "NOT_LISTED",
        "FILE_CORRUPT",
        "FIELD_EMPTY",
        "CONFLICTING",
        "OUT_OF_WINDOW",
        "NETWORK_ERROR",
    ]


def test_row_status_is_a_string_enum_so_it_serialises_as_itself():
    assert RowStatus.NOT_LISTED == "NOT_LISTED"
    assert f"{RowStatus.OUT_OF_WINDOW}" == "OUT_OF_WINDOW"


def test_fetch_result_always_carries_a_source_url_and_a_timestamp():
    """Global Constraint 4."""
    result = FetchResult(
        status=RowStatus.OK,
        value=[1, 2],
        source_url="https://www.idx.co.id/x",
        fetched_at=datetime(2026, 11, 9, tzinfo=UTC),
    )

    assert result.ok is True
    assert result.source_url == "https://www.idx.co.id/x"
    assert result.fetched_at.year == 2026


def test_a_non_ok_fetch_result_is_not_ok():
    result = FetchResult(
        status=RowStatus.NOT_LISTED,
        value=None,
        source_url="https://www.idx.co.id/x",
        fetched_at=datetime(2026, 11, 9, tzinfo=UTC),
        detail="HTTP 404",
    )

    assert result.ok is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-03-06", "2024-03-06"),
        ("28 Februari 2023", "2023-02-28"),
        ("February 23, 2022", "2022-02-23"),
        ("28 maret 2022", "2022-03-28"),
        ("6 Maret 2024", "2024-03-06"),
        ("31 Desember 2021", "2021-12-31"),
    ],
)
def test_normalise_date_handles_every_shape_seen_at_source(raw, expected):
    assert normalise_date(raw) == expected


def test_normalise_date_returns_none_rather_than_guessing():
    """Global Constraint 4: never invent a value."""
    assert normalise_date("entah kapan") is None
    assert normalise_date("") is None
    assert normalise_date(None) is None


def test_normalise_firm_name_folds_the_two_spellings_of_one_firm():
    """§2.9: the same KAP is written two ways across years."""
    a = normalise_firm_name("Amir Abadi Jusuf, Aryanto, Mawar & Rekan")
    b = normalise_firm_name("KAP Amir Abadi Jusuf, Aryanto, Mawar  Rekan")

    assert a == b
    assert a == "amir abadi jusuf, aryanto, mawar rekan"


@pytest.mark.parametrize(
    "raw",
    ["Wajar Tanpa Pengecualian", "Wajar Tanpa Modifikasian", "Unmodified"],
)
def test_both_eras_of_unmodified_wording_normalise_to_one_enum_value(raw):
    """§2.9: wording changed with SA 700; the meaning did not."""
    assert normalise_opinion(raw) is OpinionKind.UNMODIFIED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Wajar Dengan Pengecualian", OpinionKind.QUALIFIED),
        ("Tidak Wajar", OpinionKind.ADVERSE),
        ("Tidak Menyatakan Pendapat", OpinionKind.DISCLAIMER),
        ("sesuatu yang lain", OpinionKind.UNKNOWN),
        ("", None),
        (None, None),
    ],
)
def test_normalise_opinion_covers_the_other_opinion_types(raw, expected):
    assert normalise_opinion(raw) is expected


def test_field_value_keeps_the_raw_string_beside_the_normalised_one():
    field = FieldValue.of("Type of auditor's opinion", "Wajar Tanpa Pengecualian")

    assert field.raw == "Wajar Tanpa Pengecualian"
    assert field.normalised == "UNMODIFIED"
    assert field.status is RowStatus.OK


def test_an_empty_source_field_is_field_empty_not_none():
    """§6.3: a field empty at source is a different fact from a failed read."""
    field = FieldValue.of("Type of auditor's opinion", "")

    assert field.status is RowStatus.FIELD_EMPTY
    assert field.raw is None
    assert field.normalised is None


def test_general_info_reports_which_labels_are_populated():
    info = GeneralInfo(
        fields={
            "Entity name": FieldValue.of("Entity name", "PT Telkom Indonesia (Persero) Tbk"),
            "Type of auditor's opinion": FieldValue.of("Type of auditor's opinion", ""),
        }
    )

    assert info.populated_labels() == {"Entity name"}
    assert info.raw("Entity name") == "PT Telkom Indonesia (Persero) Tbk"
    assert info.raw("Type of auditor's opinion") is None
    assert info.status_of("Type of auditor's opinion") is RowStatus.FIELD_EMPTY
