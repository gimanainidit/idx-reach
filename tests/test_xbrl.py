"""Tests for XbrlReader (Task 4).

The two-taxonomy regression test is the most important test in this suite.
Matching on the Indonesian label in column A silently produced entirely empty
results for 2021-2022 in the source session, with HTTP 200 and no error (§2.7).
"""

from pathlib import Path

import pytest

from idx_reach.models import RowStatus
from idx_reach.xbrl import GENERAL_INFO_LABELS, GENERAL_INFO_SHEET, XbrlReader

FIXTURES = Path(__file__).parent / "fixtures" / "xbrl"


@pytest.fixture
def reader():
    return XbrlReader()


def read(reader, name: str):
    return reader.read_general_info(
        (FIXTURES / name).read_bytes(), source_url=f"https://www.idx.co.id/{name}"
    )


def test_the_sheet_and_label_constants_match_the_plan():
    assert GENERAL_INFO_SHEET == "1000000"
    assert GENERAL_INFO_LABELS == (
        "Entity name",
        "Period of financial statements submissions",
        "Current period start date",
        "Current period end date",
        "Prior 2 year end date",
        "Type of report on financial statements",
        "Type of auditor's opinion",
        "Current year auditor",
        "Name of current year audit signing partner",
        "Date of auditor's opinion or result of review report",
        "Description of presentation currency",
        "Level of rounding used in financial statements",
    )


def test_both_taxonomies_yield_the_same_populated_field_set(reader):
    """THE regression test for §2.7.

    The 2021-2022 workbook has an empty column A; the 2023-2025 workbook has an
    Indonesian label there. Matching on column C makes the difference invisible.
    """
    old = read(reader, "old_taxonomy_2021.xlsx")
    new = read(reader, "new_taxonomy_2024.xlsx")

    assert old.status is RowStatus.OK
    assert new.status is RowStatus.OK
    assert old.value.populated_labels() == new.value.populated_labels()
    assert old.value.populated_labels() == set(GENERAL_INFO_LABELS)


def test_the_old_taxonomy_is_not_silently_empty(reader):
    """The exact failure mode: HTTP 200, no error, every field blank."""
    old = read(reader, "old_taxonomy_2021.xlsx")

    assert old.value.populated_labels() != set()
    assert old.value.raw("Entity name") == "PT Replika Sintetis Tbk"
    assert old.value.raw("Type of auditor's opinion") == "Wajar Tanpa Pengecualian"


def test_both_taxonomies_normalise_the_opinion_to_one_value(reader):
    """§2.9: `Wajar Tanpa Pengecualian` and `Wajar Tanpa Modifikasian` are both unmodified."""
    old = read(reader, "old_taxonomy_2021.xlsx")
    new = read(reader, "new_taxonomy_2024.xlsx")

    assert old.value.normalised("Type of auditor's opinion") == "UNMODIFIED"
    assert new.value.normalised("Type of auditor's opinion") == "UNMODIFIED"
    assert old.value.raw("Type of auditor's opinion") != new.value.raw("Type of auditor's opinion")


def test_both_taxonomies_fold_the_two_spellings_of_the_audit_firm(reader):
    old = read(reader, "old_taxonomy_2021.xlsx")
    new = read(reader, "new_taxonomy_2024.xlsx")

    assert old.value.normalised("Current year auditor") == new.value.normalised(
        "Current year auditor"
    )


def test_a_lowercase_indonesian_date_is_normalised(reader):
    old = read(reader, "old_taxonomy_2021.xlsx")

    assert old.value.raw("Date of auditor's opinion or result of review report") == "28 maret 2022"
    assert (
        old.value.normalised("Date of auditor's opinion or result of review report") == "2022-03-28"
    )


def test_the_sheet_is_resolved_through_the_workbook_rels(reader):
    """§2.7: `1000000` lived in sheet3.xml. Guessing the part name reads the wrong sheet."""
    old = read(reader, "old_taxonomy_2021.xlsx")

    assert old.value.sheet_part == "xl/worksheets/sheet3.xml"
    assert old.value.raw("Entity name") == "PT Replika Sintetis Tbk"


def test_the_corrupt_165_byte_body_is_file_corrupt_not_not_listed(reader):
    """§2.8: HTTP 200 and a spreadsheet content-type, but not a ZIP."""
    body = (FIXTURES / "corrupt_165_bytes.bin").read_bytes()
    assert len(body) == 165
    assert body[:8] == bytes.fromhex("0d41646d696e6973")

    result = reader.read_general_info(body, source_url="https://www.idx.co.id/LUCK-2024.xlsx")

    assert result.status is RowStatus.FILE_CORRUPT
    assert result.value is None
    assert "ZIP" in result.detail
    assert result.source_url == "https://www.idx.co.id/LUCK-2024.xlsx"


def test_an_empty_opinion_field_is_field_empty_not_none(reader):
    """§6.3: empty at source is a different fact from a failed read."""
    result = read(reader, "new_taxonomy_2024_empty_opinion.xlsx")

    assert result.status is RowStatus.FIELD_EMPTY
    assert result.value.status_of("Type of auditor's opinion") is RowStatus.FIELD_EMPTY
    assert result.value.raw("Type of auditor's opinion") is None
    assert "Entity name" in result.value.populated_labels()


def test_contradictory_fields_are_flagged_conflicting(reader):
    """§3 / §6.4: the PGJO 2024 shape -- `Unaudit` beside a full unmodified opinion."""
    result = read(reader, "new_taxonomy_2024_conflicting.xlsx")

    assert result.status is RowStatus.CONFLICTING
    assert result.value.raw("Type of report on financial statements") == "Tidak Diaudit / Unaudit"
    assert result.value.raw("Type of auditor's opinion") == "Wajar Tanpa Modifikasian"
    assert "Unaudit" in result.detail


def test_an_empty_body_is_file_corrupt(reader):
    result = reader.read_general_info(b"", source_url="https://www.idx.co.id/x.xlsx")

    assert result.status is RowStatus.FILE_CORRUPT


def test_a_zip_without_the_general_info_sheet_is_file_corrupt(reader, tmp_path):
    import zipfile

    path = tmp_path / "no_sheet.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")

    result = reader.read_general_info(path.read_bytes(), source_url="https://www.idx.co.id/x.xlsx")

    assert result.status is RowStatus.FILE_CORRUPT
    assert GENERAL_INFO_SHEET in result.detail
