"""Store: SQLite, raw beside normalised, resumable.

Two tables:

- `raw_fetch` -- one row per request: url, fetched_at, HTTP status, the body's
  SHA-256, and where the body was written. This is the audit trail.
- `general_info` -- one row per issuer-year: the row status, `source_url`,
  `fetched_at`, and for every label in §2.7 both a normalised column and a
  `raw_` column. Both are NOT NULL at schema level, because Global Constraint 4
  is not a convention to be remembered at each call site.

Resumability is `pending()`. The source session's lesson (record §7.2) is that
a worker must be able to continue from the last row, because a 330-file run
takes about 36 minutes at the mandatory 6.5 s spacing and will be interrupted.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from .models import FetchResult, GeneralInfo, RowStatus
from .xbrl import GENERAL_INFO_LABELS

#: Statuses worth fetching again. Everything else is a finding, not a gap: a 404
#: will answer 404 at 6.5 s per attempt forever, and FILE_CORRUPT and
#: FIELD_EMPTY are facts about the source, not failures of the run.
RETRY_STATUSES: frozenset[RowStatus] = frozenset({RowStatus.NETWORK_ERROR})


def column_name(label: str) -> str:
    """A stable SQL column name for an English XBRL label."""
    slug = label.casefold().replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


_FIELD_COLUMNS = tuple(column_name(label) for label in GENERAL_INFO_LABELS)


def _schema() -> str:
    value_columns = "".join(
        f"    {column} TEXT,\n    raw_{column} TEXT,\n" for column in _FIELD_COLUMNS
    )
    return f"""
    CREATE TABLE IF NOT EXISTS raw_fetch (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        url         TEXT NOT NULL,
        fetched_at  TEXT NOT NULL,
        status      INTEGER NOT NULL,
        sha256      TEXT NOT NULL,
        body_path   TEXT
    );

    CREATE TABLE IF NOT EXISTS general_info (
        kode        TEXT NOT NULL,
        tahun       INTEGER NOT NULL,
        status      TEXT NOT NULL,
        detail      TEXT NOT NULL DEFAULT '',
{value_columns}        source_url  TEXT NOT NULL,
        fetched_at  TEXT NOT NULL,
        PRIMARY KEY (kode, tahun)
    );
    """


class Store:
    """A SQLite file. Use it as a context manager, or call `close()`."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(_schema())
        self.connection.commit()

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    # -- writes ------------------------------------------------------------------

    def record_fetch(
        self,
        *,
        url: str,
        fetched_at: datetime,
        status: int,
        body: bytes,
        body_path: str | None = None,
    ) -> None:
        """Append one row to the audit trail."""
        self.connection.execute(
            "INSERT INTO raw_fetch (url, fetched_at, status, sha256, body_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                url,
                fetched_at.isoformat(),
                status,
                hashlib.sha256(body).hexdigest(),
                body_path,
            ),
        )
        self.connection.commit()

    def save_general_info(self, kode: str, tahun: int, result: FetchResult[GeneralInfo]) -> None:
        """Upsert one issuer-year. A result with no value is stored as a status row."""
        info = result.value
        columns = ["kode", "tahun", "status", "detail", "source_url", "fetched_at"]
        values: list[object] = [
            kode.strip().upper(),
            tahun,
            str(result.status),
            result.detail,
            result.source_url,
            result.fetched_at.isoformat(),
        ]
        for label, column in zip(GENERAL_INFO_LABELS, _FIELD_COLUMNS, strict=True):
            columns += [column, f"raw_{column}"]
            values += (
                [info.normalised(label), info.raw(label)] if info is not None else [None, None]
            )

        placeholders = ", ".join("?" * len(columns))
        self.connection.execute(
            f"INSERT OR REPLACE INTO general_info ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.connection.commit()

    # -- reads -------------------------------------------------------------------

    def general_info(self, kode: str, tahun: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM general_info WHERE kode = ? AND tahun = ?",
            (kode.strip().upper(), tahun),
        ).fetchone()

    def general_info_columns(self) -> set[str]:
        return {row["name"] for row in self.connection.execute("PRAGMA table_info(general_info)")}

    def count(self, table: str) -> int:
        if table not in {"raw_fetch", "general_info"}:
            raise ValueError(f"unknown table {table!r}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def pending(
        self,
        kode_list: Sequence[str],
        years: Iterable[int],
        retry_statuses: frozenset[RowStatus] = RETRY_STATUSES,
    ) -> list[tuple[str, int]]:
        """Issuer-years still worth fetching, in input order.

        A row is settled unless its status is in `retry_statuses`. The plan's
        Task 7 says "only rows not already OK"; taken literally that re-fetches
        every 404 on every resumed run -- 37 of them in the source session, at
        6.5 s each, forever. NOT_LISTED, FILE_CORRUPT, FIELD_EMPTY and
        CONFLICTING are findings about the source, so they count as done. Pass
        `retry_statuses` to widen it.
        """
        settled = {
            (row["kode"], int(row["tahun"]))
            for row in self.connection.execute("SELECT kode, tahun, status FROM general_info")
            if RowStatus(row["status"]) not in retry_statuses
        }
        wanted = list(years)
        return [
            (kode.strip().upper(), year)
            for kode in kode_list
            for year in wanted
            if (kode.strip().upper(), year) not in settled
        ]
