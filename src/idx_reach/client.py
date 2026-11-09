"""IdxClient: the deep module at the centre of the tool.

Four public methods -- `profiles`, `financial_statement`, `attachments`,
`announcements` -- and behind them every quirk in §2 of the plan:

- the static XBRL path with its real doubled slash, escaped as `encodeURI`
  would escape it, so the slashes survive (§2.1);
- `ResultCount` reconciliation, because `indexFrom` is ignored on keyword
  queries and pagination silently returns 250 of 745 rows (§2.2);
- the immovable three-year announcement window, reported as `OUT_OF_WINDOW`
  rows rather than an empty list (§2.3);
- 503 is a rejected parameter, not rate limiting, so it never feeds the 429
  breaker (§2.4);
- collective announcements with no issuer code at all (§2.5);
- the ZIP magic check, so a corrupt file is `FILE_CORRUPT` and not
  `NOT_LISTED` (§2.8).

Apply the deletion test: delete this module and all of §2 reappears at every
call site.

`announcement_window` is a read-only property, not a fifth method: it is
derived state, read off the `SearchParams` block that the last announcement
probe returned. The plan's Task 5 asks for `announcement_window()`; a property
keeps the method count at four, which §1.3 requires.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from .models import Announcement, Attachment, FetchResult, Profile, RowStatus
from .ratelimit import RateLimiter
from .transport import BASE_URL, JsonResponse, Transport
from .xbrl import ZIP_MAGIC

#: §2.1 -- the doubled slash after `02_Soft_Copy_Laporan_Keuangan` is real.
STATIC_STATEMENT_PATH = (
    "/Portals/0/StaticData/ListedCompanies/Corporate_Actions/New_Info_JSX/"
    "Jenis_Informasi/01_Laporan_Keuangan/02_Soft_Copy_Laporan_Keuangan//"
    "Laporan Keuangan Tahun {tahun}/Audit/{kode}/"
    "FinancialStatement-{tahun}-Tahunan-{kode}.xlsx"
)

PROFILES_PATH = "/primary/ListedCompany/GetCompanyProfiles"
ATTACHMENTS_PATH = "/primary/ListedCompany/GetFinancialReport"
ANNOUNCEMENTS_PATH = "/primary/ListedCompany/GetAnnouncement"

#: §2.6 -- searching `suspensi` alone found 5 of 21 relevant events.
SUSPENSION_KEYWORDS: tuple[str, ...] = (
    "Penghentian Sementara Perdagangan",
    "Pembukaan Suspensi",
    "Pencabutan",
    "Suspensi",
    "Pemantauan Khusus",
    "Sanksi Penyampaian Laporan Keuangan",
    "Free Float",
)

#: §6.2 -- warrant delistings are not share delistings.
WARRANT_EXCLUSION = "Waran Seri"

#: The escape set of JavaScript's `encodeURI`: slashes and sub-delimiters survive,
#: spaces become %20. `encodeURIComponent` would escape the path separators and
#: the request would 404.
_ENCODE_URI_SAFE = ";,/?:@&=+$-_.!~*'()#"

TICMI_NOTE = (
    "IDX serves only the last three years on this endpoint and points historical "
    "data at TICMI (ticmi.co.id, paid). This is not evidence that no event occurred."
)


class IncompleteResultError(RuntimeError):
    """Raised when IDX returns fewer rows than its own `ResultCount` (§2.2)."""


def static_statement_url(kode: str, tahun: int) -> str:
    """Build the static XBRL URL for one issuer-year (§2.1)."""
    path = STATIC_STATEMENT_PATH.format(kode=kode.strip().upper(), tahun=tahun)
    return f"{BASE_URL}{quote(path, safe=_ENCODE_URI_SAFE)}"


def excluded_as_warrant(judul: str) -> bool:
    """§6.2: a title about `Waran Seri` is a warrant event, not a share event."""
    return WARRANT_EXCLUSION.casefold() in (judul or "").casefold()


class IdxClient:
    """Small interface, large implementation. The leverage of this codebase."""

    def __init__(
        self,
        transport: Transport,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._transport = transport
        self.limiter = limiter if limiter is not None else RateLimiter()
        self._window: tuple[date, date] | None = None

    # -- the four public methods -------------------------------------------------

    def profiles(self) -> FetchResult[list[Profile]]:
        """Every currently listed company. Current status only; no history."""
        response = self._get_json(PROFILES_PATH, {"emitenType": "s", "start": 0, "length": 9999})
        failure = self._json_failure(response)
        if failure is not None:
            return failure

        rows = [
            Profile(
                kode=str(row.get("KodeEmiten", "")).strip(),
                nama=str(row.get("NamaEmiten", "")).strip(),
                sektor=_text(row.get("Sektor")),
                sub_sektor=_text(row.get("SubSektor")),
                industri=_text(row.get("Industri")),
                sub_industri=_text(row.get("SubIndustri")),
                papan_pencatatan=_text(row.get("PapanPencatatan")),
                tanggal_pencatatan=_text(row.get("TanggalPencatatan")),
            )
            for row in _rows_of(response.body, ("data", "Results", "results"))
        ]
        return FetchResult(
            status=RowStatus.OK,
            value=rows,
            source_url=response.url,
            fetched_at=response.fetched_at,
        )

    def financial_statement(self, kode: str, tahun: int) -> FetchResult[bytes]:
        """The annual audited XBRL workbook for one issuer-year.

        404 is NOT_LISTED; HTTP 200 that is not a ZIP is FILE_CORRUPT, never
        NOT_LISTED -- in both real cases the audit report existed and was
        reachable through the attachment list (§2.8).
        """
        url = static_statement_url(kode, tahun)
        self.limiter.acquire()
        response = self._transport.get_bytes(url)
        self.limiter.record(response.status)

        if response.status == 404:
            return FetchResult(
                status=RowStatus.NOT_LISTED,
                value=None,
                source_url=url,
                fetched_at=response.fetched_at,
                detail=f"HTTP 404: {kode.upper()} was not listed in {tahun}, or no longer is.",
            )
        if response.status != 200:
            return FetchResult(
                status=RowStatus.NETWORK_ERROR,
                value=None,
                source_url=url,
                fetched_at=response.fetched_at,
                detail=f"HTTP {response.status}",
            )
        if response.body[:4] != ZIP_MAGIC:
            return FetchResult(
                status=RowStatus.FILE_CORRUPT,
                value=None,
                source_url=url,
                fetched_at=response.fetched_at,
                detail=(
                    f"HTTP 200 with content-type {response.content_type!r} but "
                    f"{len(response.body)} bytes that are not a ZIP. Try the "
                    "attachment list: the audit report is usually there in full."
                ),
            )
        return FetchResult(
            status=RowStatus.OK,
            value=response.body,
            source_url=url,
            fetched_at=response.fetched_at,
        )

    def attachments(self, kode: str, tahun: int) -> FetchResult[list[Attachment]]:
        """The attachment list for one issuer-year: the fallback when XBRL is corrupt."""
        response = self._get_json(
            ATTACHMENTS_PATH,
            {
                "indexFrom": 1,
                "pageSize": 12,
                "year": tahun,
                "reportType": "rdf",
                "EmitenType": "s",
                "periode": "audit",
                "kodeEmiten": kode.strip().upper(),
                "SortColumn": "KodeEmiten",
                "SortOrder": "asc",
            },
        )
        failure = self._json_failure(response)
        if failure is not None:
            return failure

        files: list[Attachment] = []
        for result in _rows_of(response.body, ("Results", "results")):
            for item in result.get("Attachments") or []:
                files.append(
                    Attachment(
                        file_name=str(item.get("File_Name", "")).strip(),
                        file_path=str(item.get("File_Path", "")).strip(),
                        file_size=_int(item.get("File_Size")),
                        file_modified=_text(item.get("File_Modified")),
                    )
                )
        detail = (
            ""
            if files
            else (
                "no attachments listed. A delisted issuer returns an empty list here "
                "even though its static XBRL file may still be reachable."
            )
        )
        return FetchResult(
            status=RowStatus.OK,
            value=files,
            source_url=response.url,
            fetched_at=response.fetched_at,
            detail=detail,
        )

    def announcements(
        self,
        *,
        kode: str | None = None,
        keyword: str | None = None,
        tahun: tuple[int, int] | None = None,
    ) -> FetchResult[list[Announcement]]:
        """Announcements for one issuer, one keyword, or both.

        Two requests, always: a `pageSize=1` probe to read `ResultCount` and
        `SearchParams`, then one full request with `pageSize >= ResultCount`.
        `indexFrom` is ignored on keyword queries, so paginating would silently
        return 250 of 745 rows (§2.2). The row count is reconciled against
        `ResultCount` and a mismatch raises `IncompleteResultError`.

        `tahun` is the range the caller actually cares about. A year lying
        entirely outside the live window produces one `OUT_OF_WINDOW` row for
        that year, never an empty list (§2.3, §5). A year that merely overlaps
        the window edge counts as inside, and the detail says so.
        """
        base = {"indexFrom": 0, "pageSize": 1}
        if keyword:
            base["keyword"] = keyword
        if kode:
            base["kodeEmiten"] = kode.strip().upper()

        probe = self._get_json(ANNOUNCEMENTS_PATH, base)
        failure = self._json_failure(probe)
        if failure is not None:
            return failure

        payload = probe.body if isinstance(probe.body, dict) else {}
        self._window = _parse_window(payload.get("SearchParams"))
        expected = _int(payload.get("ResultCount")) or 0

        full = self._get_json(ANNOUNCEMENTS_PATH, {**base, "pageSize": max(expected, 1)})
        failure = self._json_failure(full)
        if failure is not None:
            return failure

        body = full.body if isinstance(full.body, dict) else {}
        replies = body.get("Replies") or []
        if len(replies) != expected:
            raise IncompleteResultError(
                f"IDX reported ResultCount {expected} but returned {len(replies)} rows for "
                f"{'keyword ' + keyword if keyword else 'issuer ' + (kode or '?')}. "
                "indexFrom is ignored on keyword queries, so this is silent truncation, "
                "not an empty tail. Refusing to hand back a partial dataset."
            )

        rows = [_announcement(reply) for reply in replies]
        missing = self._years_outside_window(tahun)
        rows = [_out_of_window_row(year, kode) for year in missing] + rows

        status = RowStatus.OUT_OF_WINDOW if missing else RowStatus.OK
        detail = ""
        if missing:
            window = self.announcement_window
            span = f"{window[0].isoformat()}..{window[1].isoformat()}" if window else "unknown"
            detail = (
                f"requested {tahun[0]}-{tahun[1]} but the live window is {span}; "
                f"{', '.join(str(year) for year in missing)} cannot be verified here. " + TICMI_NOTE
            )
        return FetchResult(
            status=status,
            value=rows,
            source_url=full.url,
            fetched_at=full.fetched_at,
            detail=detail,
        )

    # -- derived state -----------------------------------------------------------

    @property
    def announcement_window(self) -> tuple[date, date] | None:
        """The live three-year window, as last reported by `SearchParams` (§2.3).

        None until an announcement request has been made: the window is IDX's
        answer, not ours to compute.
        """
        return self._window

    # -- internals ---------------------------------------------------------------

    def _get_json(self, path: str, params: Mapping[str, str | int]) -> JsonResponse:
        self.limiter.acquire()
        response = self._transport.get_json(path, params)
        self.limiter.record(response.status)
        return response

    @staticmethod
    def _json_failure(response: JsonResponse) -> FetchResult[Any] | None:
        """Translate a non-200 into a row status.

        §2.4: a 503 is a network error, never a hit on the 429 breaker.
        """
        if response.status == 200:
            return None
        if response.status == 404:
            return FetchResult(
                status=RowStatus.NOT_LISTED,
                value=None,
                source_url=response.url,
                fetched_at=response.fetched_at,
                detail="HTTP 404",
            )
        detail = f"HTTP {response.status}"
        if response.status == 503:
            detail += (
                " from Varnish. This endpoint answers 503 when it is sent a parameter "
                "it does not recognise -- it is not rate limiting (§2.4)."
            )
        return FetchResult(
            status=RowStatus.NETWORK_ERROR,
            value=None,
            source_url=response.url,
            fetched_at=response.fetched_at,
            detail=detail,
        )

    def _years_outside_window(self, tahun: tuple[int, int] | None) -> list[int]:
        if tahun is None:
            return []
        window = self._window
        if window is None:
            return []
        start, end = window
        return [
            year
            for year in range(tahun[0], tahun[1] + 1)
            if date(year, 12, 31) < start or date(year, 1, 1) > end
        ]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rows_of(body: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    """IDX wraps list payloads inconsistently; accept a bare list or any of `keys`."""
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        for key in keys:
            found = body.get(key)
            if isinstance(found, list):
                return [row for row in found if isinstance(row, dict)]
    return []


def _announcement(reply: Mapping[str, Any]) -> Announcement:
    pengumuman = reply.get("pengumuman") or {}
    attachments = tuple(
        Attachment(
            file_name=str(item.get("File_Name", "")).strip(),
            file_path=str(item.get("File_Path", "")).strip(),
            file_size=_int(item.get("File_Size")),
            file_modified=_text(item.get("File_Modified")),
        )
        for item in (reply.get("attachments") or [])
    )
    return Announcement(
        tanggal=_text(pengumuman.get("TglPengumuman")),
        nomor=_text(pengumuman.get("NoPengumuman")),
        judul=str(pengumuman.get("JudulPengumuman", "")).strip(),
        kode=_text(pengumuman.get("Kode_Emiten")),
        attachments=attachments,
    )


def _out_of_window_row(year: int, kode: str | None) -> Announcement:
    return Announcement(
        tanggal=str(year),
        nomor=None,
        judul=f"OUT_OF_WINDOW: {year} is outside the live announcement window. {TICMI_NOTE}",
        kode=(kode.strip().upper() if kode else None),
        status=RowStatus.OUT_OF_WINDOW,
    )


def _parse_window(search_params: Any) -> tuple[date, date] | None:
    if not isinstance(search_params, Mapping):
        return None
    start = _parse_date(search_params.get("DateFrom"))
    end = _parse_date(search_params.get("DateTo"))
    if start is None or end is None:
        return None
    return start, end


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None
