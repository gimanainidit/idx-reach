"""Build the SYNTHETIC REPLICA fixtures used by the test suite.

These files are *not* captures from idx.co.id. They are hand-built to reproduce
the shapes the plan records, so the offline suite can exercise the parsing paths
that the source session found dangerous. See PROVENANCE.md.

Run from the repository root:

    uv run python tests/fixtures/make_fixtures.py

Everything it writes is deterministic, so re-running it produces identical bytes.
"""

from __future__ import annotations

import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from idx_reach.client import (  # noqa: E402
    ANNOUNCEMENTS_PATH,
    ATTACHMENTS_PATH,
    PROFILES_PATH,
    SUSPENSION_KEYWORDS,
    static_statement_url,
)
from idx_reach.transport import BASE_URL, build_url, write_fixture  # noqa: E402

FIXED_STAMP = datetime(2026, 11, 9, 0, 0, tzinfo=UTC)

FIXTURES = Path(__file__).resolve().parent
XBRL_DIR = FIXTURES / "xbrl"
REPLAY_DIR = FIXTURES / "replay"
PDF_DIR = FIXTURES / "pdf"
JSON_DIR = FIXTURES / "json"

# §2.7: the twelve labels verified present in both taxonomies.
REQUIRED_LABELS: tuple[str, ...] = (
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

# The Indonesian labels that sit in column A of the 2023-2025 taxonomy only.
INDONESIAN_LABELS: dict[str, str] = {
    "Entity name": "Nama entitas",
    "Period of financial statements submissions": "Periode penyampaian laporan keuangan",
    "Current period start date": "Tanggal awal periode berjalan",
    "Current period end date": "Tanggal akhir periode berjalan",
    "Prior 2 year end date": "Tanggal akhir 2 tahun sebelumnya",
    "Type of report on financial statements": "Jenis laporan atas laporan keuangan",
    "Type of auditor's opinion": "Jenis opini auditor",
    "Current year auditor": "Auditor tahun berjalan",
    "Name of current year audit signing partner": "Nama partner penandatangan audit",
    "Date of auditor's opinion or result of review report": "Tanggal opini auditor",
    "Description of presentation currency": "Deskripsi mata uang pelaporan",
    "Level of rounding used in financial statements": "Tingkat pembulatan",
}

OLD_TAXONOMY_VALUES: dict[str, str] = {
    "Entity name": "PT Replika Sintetis Tbk",
    "Period of financial statements submissions": "Tahunan / Annual",
    "Current period start date": "2021-01-01",
    "Current period end date": "2021-12-31",
    "Prior 2 year end date": "2019-12-31",
    "Type of report on financial statements": "Diaudit / Audited",
    "Type of auditor's opinion": "Wajar Tanpa Pengecualian",
    "Current year auditor": "Amir Abadi Jusuf, Aryanto, Mawar & Rekan",
    "Name of current year audit signing partner": "Replika Sintetis",
    "Date of auditor's opinion or result of review report": "28 maret 2022",
    "Description of presentation currency": "Rupiah / IDR",
    "Level of rounding used in financial statements": "Jutaan / Millions",
}

NEW_TAXONOMY_VALUES: dict[str, str] = {
    **OLD_TAXONOMY_VALUES,
    "Current period start date": "2024-01-01",
    "Current period end date": "2024-12-31",
    "Prior 2 year end date": "2022-12-31",
    "Type of auditor's opinion": "Wajar Tanpa Modifikasian",
    "Current year auditor": "KAP Amir Abadi Jusuf, Aryanto, Mawar  Rekan",
    "Date of auditor's opinion or result of review report": "2025-03-06",
}

CONTENT_TYPE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_CT_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_CT_BOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
_CT_SST = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
_SHEET_OPEN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<worksheet xmlns="{_NS_MAIN}"><sheetData>'
)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _inline_sheet(rows: list[tuple[str, str, str]]) -> str:
    """A worksheet using t="inlineStr" cells."""
    parts = [_SHEET_OPEN]
    for index, row in enumerate(rows, start=1):
        cells = []
        for column, value in zip("ABC", row, strict=True):
            if not value:
                continue
            cells.append(
                f'<c r="{column}{index}" t="inlineStr"><is><t>{_escape(value)}</t></is></c>'
            )
        parts.append(f'<row r="{index}">{"".join(cells)}</row>')
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def _shared_string_sheet(
    rows: list[tuple[str, str, str]],
) -> tuple[str, str]:
    """A worksheet using t="s" cells, plus the sharedStrings part it points into."""
    table: list[str] = []
    index_of: dict[str, int] = {}
    parts = [_SHEET_OPEN]
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column, value in zip("ABC", row, strict=True):
            if not value:
                continue
            if value not in index_of:
                index_of[value] = len(table)
                table.append(value)
            cells.append(f'<c r="{column}{row_number}" t="s"><v>{index_of[value]}</v></c>')
        parts.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    parts.append("</sheetData></worksheet>")

    sst_items = "".join(f"<si><t>{_escape(value)}</t></si>" for value in table)
    sst = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS_MAIN}" count="{len(table)}" uniqueCount="{len(table)}">{sst_items}</sst>'
    )
    return "".join(parts), sst


def _workbook(sheets: list[tuple[str, str]]) -> tuple[str, str]:
    """Return (workbook.xml, workbook.xml.rels) for (sheet name, worksheet part) pairs.

    The order of `sheets` is display order; the part each one points at need not
    match. §2.7: sheet numbering does not follow display order, so the reader has
    to resolve through the rels.
    """
    sheet_tags = "".join(
        f'<sheet name="{_escape(name)}" sheetId="{position}" r:id="rId{position}"/>'
        for position, (name, _part) in enumerate(sheets, start=1)
    )
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><sheets>{sheet_tags}</sheets></workbook>'
    )
    rel_tags = "".join(
        f'<Relationship Id="rId{position}" Type="{_NS_REL}/worksheet" Target="{part}"/>'
        for position, (_name, part) in enumerate(sheets, start=1)
    )
    rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG_REL}">{rel_tags}</Relationships>'
    )
    return workbook, rels


def _content_types(parts: list[str], with_shared_strings: bool) -> str:
    overrides = "".join(
        f'<Override PartName="/xl/{part}" ContentType="{_CT_SHEET}"/>' for part in parts
    )
    if with_shared_strings:
        overrides += f'<Override PartName="/xl/sharedStrings.xml" ContentType="{_CT_SST}"/>'
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_NS_CT}">'
        f'<Default Extension="rels" '
        f'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        f'<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{_CT_BOOK}"/>'
        f"{overrides}</Types>"
    )


_ROOT_RELS = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_NS_PKG_REL}">'
    f'<Relationship Id="rId1" Type="{_NS_REL}/officeDocument" Target="xl/workbook.xml"/>'
    f"</Relationships>"
)


def _write_xlsx(path: Path, members: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)


def _rows(values: dict[str, str], *, indonesian_column_a: bool) -> list[tuple[str, str, str]]:
    """Build the `1000000` sheet rows for one taxonomy (§2.7)."""
    rows: list[tuple[str, str, str]] = []
    if indonesian_column_a:
        rows.append(("Informasi umum", "", "General information"))
    else:
        rows.append(("", "", "General information"))
    for label in REQUIRED_LABELS:
        value = values.get(label, "")
        column_a = INDONESIAN_LABELS[label] if indonesian_column_a else ""
        rows.append((column_a, value, label))
    return rows


def build_old_taxonomy(path: Path) -> None:
    """2021-2022 shape: column A EMPTY, value in B, English label in C.

    The `1000000` sheet is second in display order and lives in
    `xl/worksheets/sheet3.xml`, so a reader that guesses the part name from the
    sheet name, or takes the first worksheet, reads the wrong sheet.
    Shared-string cells (t="s") throughout.
    """
    info_sheet, sst = _shared_string_sheet(_rows(OLD_TAXONOMY_VALUES, indonesian_column_a=False))
    cover = _inline_sheet([("", "", "Cover - synthetic replica")])
    other = _inline_sheet([("", "", "Statement of financial position - synthetic replica")])
    sheets = [
        ("0000000", "worksheets/sheet2.xml"),
        ("1000000", "worksheets/sheet3.xml"),
        ("1210000", "worksheets/sheet1.xml"),
    ]
    workbook, rels = _workbook(sheets)
    _write_xlsx(
        path,
        {
            "[Content_Types].xml": _content_types(
                ["worksheets/sheet1.xml", "worksheets/sheet2.xml", "worksheets/sheet3.xml"],
                with_shared_strings=True,
            ),
            "_rels/.rels": _ROOT_RELS,
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/sharedStrings.xml": sst,
            "xl/worksheets/sheet1.xml": other,
            "xl/worksheets/sheet2.xml": cover,
            "xl/worksheets/sheet3.xml": info_sheet,
        },
    )


def build_new_taxonomy(path: Path, values: dict[str, str] | None = None) -> None:
    """2023-2025 shape: Indonesian label in A, value in B, English label in C.

    `1000000` is first in display order but lives in `xl/worksheets/sheet2.xml`.
    Inline-string cells (t="inlineStr") throughout, so both cell encodings are
    covered by the fixture pair.
    """
    info_sheet = _inline_sheet(_rows(values or NEW_TAXONOMY_VALUES, indonesian_column_a=True))
    cover = _inline_sheet([("Sampul", "", "Cover - synthetic replica")])
    sheets = [
        ("1000000", "worksheets/sheet2.xml"),
        ("0000000", "worksheets/sheet1.xml"),
    ]
    workbook, rels = _workbook(sheets)
    _write_xlsx(
        path,
        {
            "[Content_Types].xml": _content_types(
                ["worksheets/sheet1.xml", "worksheets/sheet2.xml"], with_shared_strings=False
            ),
            "_rels/.rels": _ROOT_RELS,
            "xl/workbook.xml": workbook,
            "xl/_rels/workbook.xml.rels": rels,
            "xl/worksheets/sheet1.xml": cover,
            "xl/worksheets/sheet2.xml": info_sheet,
        },
    )


def build_corrupt_body() -> bytes:
    """§2.8: HTTP 200, spreadsheet content-type, 165 bytes, not a ZIP.

    First eight bytes are `0d 41 64 6d 69 6e 69 73`, as observed for LUCK 2024
    and MSTI 2024.
    """
    head = b"\rAdministrator"
    body = head + b"\r\n" + b" " * (165 - len(head) - 2)
    assert len(body) == 165, len(body)
    assert body[:8] == bytes.fromhex("0d41646d696e6973")
    return body


def build_announcement_json() -> dict[str, object]:
    """Announcement JSON bodies, reproducing the shapes in §2.2 and §2.3."""
    truncated_rows = [
        {
            "pengumuman": {
                "TglPengumuman": f"2026-10-0{(index % 9) + 1}T00:00:00",
                "NoPengumuman": f"Peng-SPT-{index:05d}/BEI.PP3/10-2026",
                "JudulPengumuman": "Pemantauan Khusus - replika sintetis",
                "Kode_Emiten": "TECH  " if index % 50 == 0 else "    ",
            },
            "attachments": [],
        }
        for index in range(250)
    ]
    collective = {
        "pengumuman": {
            "TglPengumuman": "2025-07-01T00:00:00",
            "NoPengumuman": "Peng-S-00016/BEI.PLP/07-2025",
            "JudulPengumuman": "Penghentian Sementara Perdagangan Efek - replika sintetis",
            "Kode_Emiten": "   ",
        },
        "attachments": [
            {
                "File_Name": "Peng-S-00016.pdf",
                "File_Path": (
                    "/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/replica.pdf"
                ),
                "File_Size": 123456,
                "File_Modified": "2025-07-01T09:00:00",
            }
        ],
    }
    search_params = {
        "DateFrom": "2023-11-09T00:00:00",
        "DateTo": "2026-11-09T00:00:00",
        "Emiten": "",
        "Keyword": "Pemantauan Khusus",
    }
    return {
        "probe": {"ResultCount": 745, "SearchParams": search_params, "Replies": []},
        "truncated": {
            "ResultCount": 745,
            "SearchParams": search_params,
            "Replies": truncated_rows,
        },
        "collective": {
            "ResultCount": 1,
            "SearchParams": search_params,
            "Replies": [collective],
        },
        "varnish_503": {"error": "Service Unavailable", "server": "Varnish"},
        "not_found_404": {"error": "Not Found"},
    }


# -- PDF fixtures (§2.10, §1.2) -------------------------------------------------

_OPINION_PARAGRAPH = (
    "{phrase}, laporan keuangan terlampir menyajikan secara wajar, dalam semua "
    "hal yang material, posisi keuangan PT Replika Sintetis Tbk tanggal "
    "31 Desember 2024, sesuai dengan Standar Akuntansi Keuangan di Indonesia."
)


def build_text_layer_pdf(path: Path, phrase: str | None, opinion_page: int = 6) -> None:
    """An eight-page report with a text layer, opinion phrase deep inside it.

    §2.10: in MTEL 2022 the auditor's report was page 6 of 191, after the table
    of contents and the directors' statement, so a front-page-only search misses
    it.
    """
    import pymupdf

    document = pymupdf.open()
    for number in range(1, 9):
        page = document.new_page()
        lines = [f"Halaman {number} - replika sintetis", "Daftar isi / Surat pernyataan direksi"]
        if phrase is not None and number == opinion_page:
            lines += ["LAPORAN AUDITOR INDEPENDEN", "", _OPINION_PARAGRAPH.format(phrase=phrase)]
        page.insert_textbox(pymupdf.Rect(56, 56, 540, 760), chr(10).join(lines), fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()


def build_scanned_pdf(path: Path) -> None:
    """A single page that is an image with no text layer, as 2 of 7 real reports were."""
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 420, 560))
    pixmap.set_rect(pixmap.irect, (235, 235, 235))
    pixmap.set_rect(pymupdf.IRect(40, 60, 380, 90), (20, 20, 20))
    page.insert_image(pymupdf.Rect(56, 56, 540, 760), pixmap=pixmap)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()


# -- Replay fixtures (ReplayTransport triples) ----------------------------------

#: Issuer-years the CLI tests walk.
REPLAY_YEARS = (2021, 2024)
REPLAY_KODE = "TLKM"
ANNOUNCEMENT_KODE = "TECH"
ATTACHMENT_KODE = "LUCK"

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
XLSX_HEADERS = {"content-type": CONTENT_TYPE_XLSX}


def build_suspensi_body() -> dict[str, object]:
    """Two rows: one real suspension, one warrant delisting that must be excluded (§6.2)."""
    search_params = {
        "DateFrom": "2023-11-09T00:00:00",
        "DateTo": "2026-11-09T00:00:00",
        "Emiten": "",
        "Keyword": "",
    }
    return {
        "ResultCount": 2,
        "SearchParams": search_params,
        "Replies": [
            {
                "pengumuman": {
                    "TglPengumuman": "2025-04-14T00:00:00",
                    "NoPengumuman": "Peng-SPT-00123/BEI.PP3/04-2025",
                    "JudulPengumuman": (
                        "Penghentian Sementara Perdagangan Efek PT Replika Sintetis Tbk"
                    ),
                    "Kode_Emiten": "TECH  ",
                },
                "attachments": [],
            },
            {
                "pengumuman": {
                    "TglPengumuman": "2025-05-02T00:00:00",
                    "NoPengumuman": "Peng-LDEL-00007/BEI.PP1/05-2025",
                    "JudulPengumuman": (
                        "Mengingatkan Jadwal Penghapusan (Delisting) Efek Waran Seri I "
                        "PT Replika Sintetis Tbk"
                    ),
                    "Kode_Emiten": "TECH  ",
                },
                "attachments": [],
            },
        ],
    }


def build_profiles_body() -> dict[str, object]:
    return {
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
            },
            {
                "KodeEmiten": "TECH",
                "NamaEmiten": "PT Replika Kedua Tbk",
                "Sektor": "Technology",
                "SubSektor": "Software",
                "Industri": "Software",
                "SubIndustri": "Software",
                "PapanPencatatan": "Pengembangan",
                "TanggalPencatatan": "2021-07-07T00:00:00",
            },
        ]
    }


def build_attachments_body() -> dict[str, object]:
    return {
        "Results": [
            {
                "Attachments": [
                    {
                        "File_Name": "FinancialStatement-2024-Tahunan-LUCK.pdf",
                        "File_Path": "/StaticData/x/replica-audit-report.pdf",
                        "File_Size": 204800,
                        "File_Modified": "2025-03-06T00:00:00",
                    }
                ]
            }
        ]
    }


def _write_json_fixture(url: str, status: int, body: object) -> None:
    write_fixture(
        REPLAY_DIR,
        url=url,
        status=status,
        headers=JSON_HEADERS,
        body=json.dumps(body).encode("utf-8"),
        fetched_at=FIXED_STAMP,
    )


def _announcement_pair(params: dict[str, object], body: dict[str, object]) -> None:
    """Write the probe fixture and the full-page fixture for one announcement query."""
    # Key order matters: ReplayTransport keys on the full URL, so this must be
    # the order IdxClient builds its query in -- indexFrom, pageSize, then any
    # keyword, then any kodeEmiten.
    probe: dict[str, object] = {"indexFrom": 0, "pageSize": 1}
    for key in ("keyword", "kodeEmiten"):
        if key in params:
            probe[key] = params[key]
    count = int(body["ResultCount"])
    _write_json_fixture(
        build_url(ANNOUNCEMENTS_PATH, probe),
        200,
        {**body, "Replies": []} if count != 1 else body,
    )
    _write_json_fixture(
        build_url(ANNOUNCEMENTS_PATH, {**probe, "pageSize": max(count, 1)}), 200, body
    )


def build_replay_fixtures() -> None:
    """Fixture triples for the CLI tests, keyed by the URLs IdxClient really builds."""
    for stale in REPLAY_DIR.glob("*"):
        if stale.is_file():
            stale.unlink()

    _write_json_fixture(
        build_url(PROFILES_PATH, {"emitenType": "s", "start": 0, "length": 9999}),
        200,
        build_profiles_body(),
    )

    _write_json_fixture(
        build_url(
            ATTACHMENTS_PATH,
            {
                "indexFrom": 1,
                "pageSize": 12,
                "year": 2024,
                "reportType": "rdf",
                "EmitenType": "s",
                "periode": "audit",
                "kodeEmiten": ATTACHMENT_KODE,
                "SortColumn": "KodeEmiten",
                "SortOrder": "asc",
            },
        ),
        200,
        build_attachments_body(),
    )

    # The two taxonomies, served as the static XBRL path really serves them.
    for year, name in ((2021, "old_taxonomy_2021.xlsx"), (2024, "new_taxonomy_2024.xlsx")):
        write_fixture(
            REPLAY_DIR,
            url=static_statement_url(REPLAY_KODE, year),
            status=200,
            headers=XLSX_HEADERS,
            body=(XBRL_DIR / name).read_bytes(),
            fetched_at=FIXED_STAMP,
        )
    # §2.8: HTTP 200, spreadsheet content-type, 165 bytes that are not a ZIP.
    write_fixture(
        REPLAY_DIR,
        url=static_statement_url(ATTACHMENT_KODE, 2024),
        status=200,
        headers=XLSX_HEADERS,
        body=build_corrupt_body(),
        fetched_at=FIXED_STAMP,
    )
    # §3: 404 means NOT_LISTED.
    write_fixture(
        REPLAY_DIR,
        url=static_statement_url("GONE", 2024),
        status=404,
        headers=XLSX_HEADERS,
        body=b"",
        fetched_at=FIXED_STAMP,
    )

    suspensi = build_suspensi_body()
    _announcement_pair({"kodeEmiten": ANNOUNCEMENT_KODE}, suspensi)
    for keyword in SUSPENSION_KEYWORDS:
        _announcement_pair({"keyword": keyword}, suspensi)
        _announcement_pair({"keyword": keyword, "kodeEmiten": ANNOUNCEMENT_KODE}, suspensi)

    # A PDF to fetch, so `idx-reach fetch` has something to write.
    write_fixture(
        REPLAY_DIR,
        url=f"{BASE_URL}/StaticData/x/replica-audit-report.pdf",
        status=200,
        headers={"content-type": "application/pdf"},
        body=(PDF_DIR / "audit_report_text_layer.pdf").read_bytes(),
        fetched_at=FIXED_STAMP,
    )

    (REPLAY_DIR / "INDEX.md").write_text(_replay_index(), encoding="utf-8")


def _replay_index() -> str:
    lines = [
        "# Replay fixture index",
        "",
        "SYNTHETIC REPLICAS, not captures. See ../PROVENANCE.md.",
        "",
        "Each key is the SHA-256 of the full request URL, which is what",
        "`ReplayTransport` looks up. Query strings stay out of filenames on purpose",
        "(record §7.4).",
        "",
        "| key | URL |",
        "|---|---|",
    ]
    for meta in sorted(REPLAY_DIR.glob("*.meta.json")):
        payload = json.loads(meta.read_text(encoding="utf-8"))
        lines.append(f"| `{meta.name.removesuffix('.meta.json')}` | `{payload['url']}` |")
    return "\n".join(lines) + "\n"


def main() -> None:
    XBRL_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    REPLAY_DIR.mkdir(parents=True, exist_ok=True)

    build_old_taxonomy(XBRL_DIR / "old_taxonomy_2021.xlsx")
    build_new_taxonomy(XBRL_DIR / "new_taxonomy_2024.xlsx")
    build_new_taxonomy(
        XBRL_DIR / "new_taxonomy_2024_empty_opinion.xlsx",
        {**NEW_TAXONOMY_VALUES, "Type of auditor's opinion": ""},
    )
    build_new_taxonomy(
        XBRL_DIR / "new_taxonomy_2024_conflicting.xlsx",
        {
            **NEW_TAXONOMY_VALUES,
            "Type of report on financial statements": "Tidak Diaudit / Unaudit",
        },
    )
    (XBRL_DIR / "corrupt_165_bytes.bin").write_bytes(build_corrupt_body())

    build_text_layer_pdf(PDF_DIR / "audit_report_text_layer.pdf", "Menurut opini kami")
    build_text_layer_pdf(PDF_DIR / "audit_report_text_layer_old.pdf", "Menurut pendapat kami")
    build_text_layer_pdf(PDF_DIR / "audit_report_text_layer_english.pdf", "In our opinion")
    build_text_layer_pdf(PDF_DIR / "no_opinion.pdf", None)
    build_scanned_pdf(PDF_DIR / "audit_report_scanned.pdf")

    bodies = build_announcement_json()
    for name, body in bodies.items():
        (JSON_DIR / f"announcement_{name}.json").write_text(
            json.dumps(body, indent=2), encoding="utf-8"
        )

    build_replay_fixtures()

    print(f"wrote synthetic replica fixtures under {FIXTURES}")


if __name__ == "__main__":
    main()
