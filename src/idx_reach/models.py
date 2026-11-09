"""Shared models: the row-status enum, FetchResult, and value normalisation.

Two rules from the Global Constraints shape everything here:

- Row status is an enum, never a boolean (§3). The three "no data" cases --
  NOT_LISTED, FILE_CORRUPT, OUT_OF_WINDOW -- demand different researcher
  actions, so collapsing them into "unavailable" destroys the product's value.
- Never invent a value (§4). A field absent at source stays absent, and every
  normalised value sits beside the raw string it came from, so a researcher can
  trace back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


class RowStatus(StrEnum):
    """Per-row outcome. Plan §3."""

    OK = "OK"
    NOT_LISTED = "NOT_LISTED"  # HTTP 404: not yet, or no longer, listed
    FILE_CORRUPT = "FILE_CORRUPT"  # HTTP 200 but not a ZIP; fall back to PDF
    FIELD_EMPTY = "FIELD_EMPTY"  # parsed, but the field is empty at source
    CONFLICTING = "CONFLICTING"  # fields contradict; needs human review
    OUT_OF_WINDOW = "OUT_OF_WINDOW"  # outside the 3-year announcement window
    NETWORK_ERROR = "NETWORK_ERROR"  # 429, 503, or timeout


class OpinionKind(StrEnum):
    """Audit opinion, normalised across the SA 700 wording change (§2.9)."""

    UNMODIFIED = "UNMODIFIED"
    QUALIFIED = "QUALIFIED"
    ADVERSE = "ADVERSE"
    DISCLAIMER = "DISCLAIMER"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    """One fetch, with its status, its source URL and when it was read."""

    status: RowStatus
    value: T | None
    source_url: str
    fetched_at: datetime
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is RowStatus.OK


_INDONESIAN_MONTHS = {
    "januari": 1,
    "februari": 2,
    "maret": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "agustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "desember": 12,
}
_ENGLISH_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTHS = {**_INDONESIAN_MONTHS, **_ENGLISH_MONTHS}

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$")


def normalise_date(raw: str | None) -> str | None:
    """Return an ISO date string, or None when the shape is unrecognised.

    Four shapes were seen at source (§2.9): `2024-03-06`, `28 Februari 2023`,
    `February 23, 2022` and `28 maret 2022`. A wrong year at source is
    transcribed as-is: correcting it would be inventing a value.
    """
    if not raw:
        return None
    text = " ".join(str(raw).split())

    iso = _ISO.match(text)
    if iso:
        return _iso_or_none(int(iso[1]), int(iso[2]), int(iso[3]))

    dmy = _DAY_MONTH_YEAR.match(text)
    if dmy:
        return _iso_or_none(int(dmy[3]), _MONTHS.get(dmy[2].casefold()), int(dmy[1]))

    mdy = _MONTH_DAY_YEAR.match(text)
    if mdy:
        return _iso_or_none(int(mdy[3]), _MONTHS.get(mdy[1].casefold()), int(mdy[2]))

    return None


def _iso_or_none(year: int, month: int | None, day: int) -> str | None:
    if month is None:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def normalise_firm_name(raw: str | None) -> str | None:
    """Fold the spelling drift of one audit firm's name into a single key (§2.9).

    `Amir Abadi Jusuf, Aryanto, Mawar & Rekan` and
    `KAP Amir Abadi Jusuf, Aryanto, Mawar  Rekan` are the same firm.
    """
    if not raw:
        return None
    text = str(raw).replace("&", " ")
    text = " ".join(text.split()).casefold()
    text = re.sub(r"^(kap|k\.a\.p\.?)\s+", "", text)
    text = re.sub(r"\s+,", ",", text)
    return text or None


_UNMODIFIED = ("wajar tanpa pengecualian", "wajar tanpa modifikasian", "unmodified", "unqualified")
_QUALIFIED = ("wajar dengan pengecualian", "qualified")
_ADVERSE = ("tidak wajar", "adverse")
_DISCLAIMER = ("tidak menyatakan pendapat", "disclaimer")


def normalise_opinion(raw: str | None) -> OpinionKind | None:
    """Map opinion wording to one enum value, or None when the field is empty.

    An unrecognised non-empty wording is UNKNOWN, never a guess: the raw string
    stays beside it for a human to read.
    """
    if not raw or not str(raw).strip():
        return None
    text = " ".join(str(raw).split()).casefold()
    for needles, kind in (
        (_QUALIFIED, OpinionKind.QUALIFIED),
        (_UNMODIFIED, OpinionKind.UNMODIFIED),
        (_ADVERSE, OpinionKind.ADVERSE),
        (_DISCLAIMER, OpinionKind.DISCLAIMER),
    ):
        if any(needle in text for needle in needles):
            return kind
    return OpinionKind.UNKNOWN


#: Labels whose value is a date. Named, not sniffed: the audit-opinion date
#: label ends in "report", so a suffix heuristic misses exactly the field whose
#: four source spellings §2.9 warns about.
_DATE_LABELS = (
    "current period start date",
    "current period end date",
    "prior 2 year end date",
    "date of auditor's opinion or result of review report",
)
_OPINION_LABELS = ("type of auditor's opinion",)
_FIRM_LABELS = ("current year auditor",)


@dataclass(frozen=True)
class FieldValue:
    """One XBRL general-information field: raw beside normalised, with its own status."""

    label: str
    raw: str | None
    normalised: str | None
    status: RowStatus

    @classmethod
    def of(cls, label: str, raw: str | None) -> FieldValue:
        text = None if raw is None else str(raw).strip()
        if not text:
            return cls(label=label, raw=None, normalised=None, status=RowStatus.FIELD_EMPTY)
        return cls(
            label=label,
            raw=text,
            normalised=_normalise_for(label, text),
            status=RowStatus.OK,
        )


def _normalise_for(label: str, text: str) -> str | None:
    key = label.casefold()
    if key in _OPINION_LABELS:
        opinion = normalise_opinion(text)
        return None if opinion is None else opinion.value
    if key in _FIRM_LABELS:
        return normalise_firm_name(text)
    if key in _DATE_LABELS:
        return normalise_date(text)
    return text


@dataclass(frozen=True)
class GeneralInfo:
    """The `1000000` sheet of an XBRL workbook, keyed by English label (§2.7)."""

    fields: Mapping[str, FieldValue]
    sheet_part: str = ""

    def populated_labels(self) -> set[str]:
        return {label for label, value in self.fields.items() if value.status is RowStatus.OK}

    def raw(self, label: str) -> str | None:
        found = self.fields.get(label)
        return None if found is None else found.raw

    def normalised(self, label: str) -> str | None:
        found = self.fields.get(label)
        return None if found is None else found.normalised

    def status_of(self, label: str) -> RowStatus:
        found = self.fields.get(label)
        return RowStatus.FIELD_EMPTY if found is None else found.status


@dataclass(frozen=True)
class Profile:
    """A listed-company profile row (record §2.3). Current status only; no history."""

    kode: str
    nama: str
    sektor: str | None = None
    sub_sektor: str | None = None
    industri: str | None = None
    sub_industri: str | None = None
    papan_pencatatan: str | None = None
    tanggal_pencatatan: str | None = None


@dataclass(frozen=True)
class Attachment:
    """One file in a financial-report attachment list (record §2.2)."""

    file_name: str
    file_path: str
    file_size: int | None = None
    file_modified: str | None = None


@dataclass(frozen=True)
class Announcement:
    """One announcement. `kode` is space-padded at source and trimmed here."""

    tanggal: str | None
    nomor: str | None
    judul: str
    kode: str | None
    attachments: tuple[Attachment, ...] = ()
    status: RowStatus = RowStatus.OK

    @property
    def is_collective(self) -> bool:
        """§2.5: a mass announcement carries no issuer code at all."""
        return not self.kode


@dataclass(frozen=True)
class PageText:
    """One PDF page's text, with how it was obtained (§1.2)."""

    page_number: int
    text: str
    confidence: Literal["text_layer", "ocr"]


@dataclass(frozen=True)
class OpinionEvidence:
    """Where an audit opinion phrase was found in a PDF (§2.10)."""

    page_number: int
    phrase: str
    excerpt: str
    confidence: Literal["text_layer", "ocr"]
    opinion: OpinionKind | None = None
