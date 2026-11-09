"""Tests for Store (Task 7)."""

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from idx_reach.models import RowStatus
from idx_reach.store import Store, column_name
from idx_reach.xbrl import GENERAL_INFO_LABELS, XbrlReader

XBRL_FIXTURES = Path(__file__).parent / "fixtures" / "xbrl"
STAMP = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "idx.sqlite") as open_store:
        yield open_store


def info_for(name: str):
    return XbrlReader().read_general_info(
        (XBRL_FIXTURES / name).read_bytes(),
        source_url=f"https://www.idx.co.id/{name}",
        fetched_at=STAMP,
    )


def test_label_column_names_are_stable_slugs():
    assert column_name("Entity name") == "entity_name"
    assert column_name("Type of auditor's opinion") == "type_of_auditors_opinion"
    assert column_name("Prior 2 year end date") == "prior_2_year_end_date"


def test_the_schema_holds_a_raw_and_a_normalised_column_for_every_label(store):
    columns = store.general_info_columns()

    for label in GENERAL_INFO_LABELS:
        assert column_name(label) in columns
        assert f"raw_{column_name(label)}" in columns
    assert {"kode", "tahun", "status", "source_url", "fetched_at"} <= columns


def test_a_saved_row_keeps_raw_beside_normalised(store):
    store.save_general_info("TLKM", 2021, info_for("old_taxonomy_2021.xlsx"))

    row = store.general_info("TLKM", 2021)

    assert row["raw_type_of_auditors_opinion"] == "Wajar Tanpa Pengecualian"
    assert row["type_of_auditors_opinion"] == "UNMODIFIED"
    assert row["raw_date_of_auditors_opinion_or_result_of_review_report"] == "28 maret 2022"
    assert row["date_of_auditors_opinion_or_result_of_review_report"] == "2022-03-28"


def test_every_stored_row_carries_a_source_url_and_a_timestamp(store):
    """Global Constraint 4."""
    store.save_general_info("TLKM", 2021, info_for("old_taxonomy_2021.xlsx"))

    row = store.general_info("TLKM", 2021)

    assert row["source_url"].endswith("old_taxonomy_2021.xlsx")
    assert row["fetched_at"] == STAMP.isoformat()


def test_a_row_without_a_source_url_is_refused_by_the_schema(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute(
            "INSERT INTO general_info (kode, tahun, status) VALUES ('X', 2024, 'OK')"
        )


def test_a_corrupt_file_is_stored_as_a_status_row_with_no_fields(store):
    body = (XBRL_FIXTURES / "corrupt_165_bytes.bin").read_bytes()
    result = XbrlReader().read_general_info(
        body, source_url="https://www.idx.co.id/LUCK.xlsx", fetched_at=STAMP
    )

    store.save_general_info("LUCK", 2024, result)
    row = store.general_info("LUCK", 2024)

    assert row["status"] == RowStatus.FILE_CORRUPT
    assert row["entity_name"] is None
    assert row["detail"].startswith("not a ZIP")


def test_saving_the_same_issuer_year_twice_replaces_the_row(store):
    store.save_general_info("TLKM", 2021, info_for("old_taxonomy_2021.xlsx"))
    store.save_general_info("TLKM", 2021, info_for("new_taxonomy_2024.xlsx"))

    assert store.count("general_info") == 1
    assert store.general_info("TLKM", 2021)["raw_current_year_auditor"].startswith("KAP ")


def test_raw_fetch_records_the_url_status_checksum_and_body_path(store):
    store.record_fetch(
        url="https://www.idx.co.id/Portals/0/x.xlsx",
        fetched_at=STAMP,
        status=200,
        body=b"PK\x03\x04 synthetic",
        body_path="data/raw/x.xlsx",
    )

    row = store.connection.execute("SELECT * FROM raw_fetch").fetchone()

    assert row["status"] == 200
    assert row["sha256"] == hashlib.sha256(b"PK\x03\x04 synthetic").hexdigest()
    assert row["body_path"] == "data/raw/x.xlsx"
    assert row["fetched_at"] == STAMP.isoformat()


def test_pending_returns_only_the_gaps_after_a_partial_run(store):
    """Resumability: re-running a batch must not refetch what is already OK."""
    store.save_general_info("TLKM", 2021, info_for("old_taxonomy_2021.xlsx"))
    store.save_general_info("TLKM", 2024, info_for("new_taxonomy_2024.xlsx"))

    gaps = store.pending(["TLKM", "LUCK"], [2021, 2024])

    assert gaps == [("LUCK", 2021), ("LUCK", 2024)]


def test_a_field_empty_row_counts_as_done_because_the_source_is_empty(store):
    """§6.3: FIELD_EMPTY is a finding, not a gap. Refetching would change nothing."""
    store.save_general_info("TECH", 2024, info_for("new_taxonomy_2024_empty_opinion.xlsx"))

    assert store.pending(["TECH"], [2024]) == []


def test_a_network_error_row_stays_pending(store):
    """NETWORK_ERROR is worth retrying; NOT_LISTED and FILE_CORRUPT are not gaps in the data."""
    from idx_reach.models import FetchResult

    store.save_general_info(
        "TECH",
        2025,
        FetchResult(
            status=RowStatus.NETWORK_ERROR,
            value=None,
            source_url="https://www.idx.co.id/TECH.xlsx",
            fetched_at=STAMP,
            detail="HTTP 429",
        ),
    )

    assert store.pending(["TECH"], [2025]) == [("TECH", 2025)]


def test_a_not_listed_row_is_not_pending(store):
    from idx_reach.models import FetchResult

    store.save_general_info(
        "GONE",
        2021,
        FetchResult(
            status=RowStatus.NOT_LISTED,
            value=None,
            source_url="https://www.idx.co.id/GONE.xlsx",
            fetched_at=STAMP,
            detail="HTTP 404",
        ),
    )

    assert store.pending(["GONE"], [2021]) == []


def test_the_store_survives_being_reopened(store, tmp_path):
    store.save_general_info("TLKM", 2021, info_for("old_taxonomy_2021.xlsx"))
    store.close()

    with Store(tmp_path / "idx.sqlite") as reopened:
        assert reopened.pending(["TLKM"], [2021]) == []
