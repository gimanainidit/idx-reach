"""XbrlReader: a deep module turning workbook bytes into a GeneralInfo.

Everything the source session learned the hard way about IDX's XBRL files is
hidden behind one method:

- The ZIP magic check, because a corrupt file arrives as HTTP 200 with a
  spreadsheet content-type (§2.8).
- Sheet resolution through `xl/workbook.xml` and `xl/_rels/workbook.xml.rels`,
  because sheet numbering does not follow display order: `1000000` lived in
  `xl/worksheets/sheet3.xml` (§2.7).
- Matching on the ENGLISH label in column C. The 2021-2022 taxonomy leaves
  column A empty, so matching the Indonesian label there yields every field
  empty with no error at all (§2.7). This is the worst bug of the source
  session and the reason this module exists.
- Cross-field consistency, so the PGJO 2024 shape is reported as CONFLICTING
  rather than silently resolved (§6.4).

No external spreadsheet library: an .xlsx is a ZIP of XML, and `zipfile` plus
`xml.etree` is the whole dependency.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .models import FetchResult, FieldValue, GeneralInfo, RowStatus
from .transport import utc_now

ZIP_MAGIC = b"PK\x03\x04"
GENERAL_INFO_SHEET = "1000000"

#: §2.7 -- verified present in both taxonomies. Matched in column C.
GENERAL_INFO_LABELS: tuple[str, ...] = (
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

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

_LABEL_COLUMN = "C"
_VALUE_COLUMN = "B"

_UNAUDITED_MARKERS = ("tidak diaudit", "unaudit")


class XbrlReader:
    """Workbook bytes in, `FetchResult[GeneralInfo]` out."""

    def read_general_info(
        self,
        data: bytes,
        *,
        source_url: str = "",
        fetched_at: datetime | None = None,
    ) -> FetchResult[GeneralInfo]:
        stamp = fetched_at or utc_now()

        if data[:4] != ZIP_MAGIC:
            return FetchResult(
                status=RowStatus.FILE_CORRUPT,
                value=None,
                source_url=source_url,
                fetched_at=stamp,
                detail=(
                    f"not a ZIP: {len(data)} bytes beginning {data[:8].hex(' ')!r}. "
                    "The audit report may still be reachable through the attachment list."
                ),
            )

        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                part = self._resolve_sheet_part(archive, GENERAL_INFO_SHEET)
                if part is None:
                    return FetchResult(
                        status=RowStatus.FILE_CORRUPT,
                        value=None,
                        source_url=source_url,
                        fetched_at=stamp,
                        detail=f"no sheet named {GENERAL_INFO_SHEET} in this workbook",
                    )
                shared = self._shared_strings(archive)
                cells = self._cells(archive.read(part), shared)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as error:
            return FetchResult(
                status=RowStatus.FILE_CORRUPT,
                value=None,
                source_url=source_url,
                fetched_at=stamp,
                detail=f"unreadable ZIP container: {error}",
            )

        info = GeneralInfo(fields=self._fields(cells), sheet_part=part)
        status, detail = self._verdict(info)
        return FetchResult(
            status=status, value=info, source_url=source_url, fetched_at=stamp, detail=detail
        )

    # -- sheet resolution (§2.7) -------------------------------------------------

    def _resolve_sheet_part(self, archive: zipfile.ZipFile, sheet_name: str) -> str | None:
        """Map a sheet *name* to its part path. Never guess from the name."""
        try:
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except KeyError:
            return None

        target_of = {
            element.get("Id"): element.get("Target", "") for element in rels if element.get("Id")
        }
        for sheet in workbook.iter(f"{_NS_MAIN}sheet"):
            if sheet.get("name") != sheet_name:
                continue
            target = target_of.get(sheet.get(_NS_REL_ATTR, ""), "")
            if not target:
                return None
            return self._normalise_target(target)
        return None

    @staticmethod
    def _normalise_target(target: str) -> str:
        """Rels targets may be relative to `xl/` or absolute from the package root."""
        if target.startswith("/"):
            return target.lstrip("/")
        return str(PurePosixPath("xl") / target)

    # -- cell reading ------------------------------------------------------------

    @staticmethod
    def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
        try:
            raw = archive.read("xl/sharedStrings.xml")
        except KeyError:
            return []
        table = ElementTree.fromstring(raw)
        return [
            "".join(node.text or "" for node in item.iter(f"{_NS_MAIN}t"))
            for item in table.iter(f"{_NS_MAIN}si")
        ]

    @staticmethod
    def _cells(sheet_xml: bytes, shared: list[str]) -> dict[str, str]:
        """Return {cell reference: text}, e.g. {"C7": "Type of auditor's opinion"}."""
        sheet = ElementTree.fromstring(sheet_xml)
        cells: dict[str, str] = {}
        for cell in sheet.iter(f"{_NS_MAIN}c"):
            reference = cell.get("r")
            if not reference:
                continue
            kind = cell.get("t")
            if kind == "s":
                value_node = cell.find(f"{_NS_MAIN}v")
                index = int(value_node.text) if value_node is not None and value_node.text else -1
                text = shared[index] if 0 <= index < len(shared) else ""
            elif kind == "inlineStr":
                text = "".join(node.text or "" for node in cell.iter(f"{_NS_MAIN}t"))
            else:
                value_node = cell.find(f"{_NS_MAIN}v")
                text = (value_node.text or "") if value_node is not None else ""
            cells[reference] = text
        return cells

    @staticmethod
    def _fields(cells: dict[str, str]) -> dict[str, FieldValue]:
        """Match the English label in column C; take the value from column B."""
        row_of_label: dict[str, str] = {}
        for reference, text in cells.items():
            if not reference.startswith(_LABEL_COLUMN):
                continue
            label = " ".join(text.split())
            if label in GENERAL_INFO_LABELS and label not in row_of_label:
                row_of_label[label] = reference[len(_LABEL_COLUMN) :]

        fields: dict[str, FieldValue] = {}
        for label in GENERAL_INFO_LABELS:
            row = row_of_label.get(label)
            raw = cells.get(f"{_VALUE_COLUMN}{row}") if row else None
            fields[label] = FieldValue.of(label, raw)
        return fields

    # -- cross-field consistency (§6.4) ------------------------------------------

    @staticmethod
    def _verdict(info: GeneralInfo) -> tuple[RowStatus, str]:
        report_type = (info.raw("Type of report on financial statements") or "").casefold()
        says_unaudited = any(marker in report_type for marker in _UNAUDITED_MARKERS)
        audit_evidence = [
            label
            for label in (
                "Type of auditor's opinion",
                "Current year auditor",
                "Name of current year audit signing partner",
                "Date of auditor's opinion or result of review report",
            )
            if info.status_of(label) is RowStatus.OK
        ]
        if says_unaudited and audit_evidence:
            return (
                RowStatus.CONFLICTING,
                (
                    "'Type of report on financial statements' says "
                    f"{info.raw('Type of report on financial statements')!r} while "
                    f"{len(audit_evidence)} audit fields are populated "
                    f"({', '.join(audit_evidence)}). Needs human review; "
                    "read the PDF audit report before using this row."
                ),
            )

        empty = sorted(set(GENERAL_INFO_LABELS) - info.populated_labels())
        if empty:
            return RowStatus.FIELD_EMPTY, f"empty at source: {', '.join(empty)}"
        return RowStatus.OK, ""
