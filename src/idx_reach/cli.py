"""The CLI: thin on purpose.

Each command parses arguments, calls one module, writes a file, and prints a
small JSON summary -- row count, error count, output path. A dataset never goes
to stdout (Global Constraint 5): the source session found agent message channels
truncate at about 1,100 characters, which cut CSV rows in half (record §7.3).

`transport_factory` and `limiter_factory` are the seams the tests inject
through. Nothing else in this module knows how a request is made.
"""

from __future__ import annotations

import csv
import functools
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from .client import SUSPENSION_KEYWORDS, IdxClient, excluded_as_warrant
from .models import Announcement, FetchResult, GeneralInfo, RowStatus
from .ratelimit import DEFAULT_DELAY_SECONDS, RateLimiter
from .store import Store, column_name
from .transport import MissingFixtureError, Transport, write_fixture
from .xbrl import GENERAL_INFO_LABELS, XbrlReader


def loud(command: Callable[..., None]) -> Callable[..., None]:
    """Turn a missing fixture into a clean, loud CLI error on stderr.

    A `ReplayTransport` miss must never look like an empty result: it is the
    difference between "IDX has no such row" and "nobody ever recorded one".
    """

    @functools.wraps(command)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            command(*args, **kwargs)
        except MissingFixtureError as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(code=2) from error

    return wrapper


app = typer.Typer(
    add_completion=False,
    help="Fetch Indonesia Stock Exchange (IDX/BEI) data for researchers, through a real browser.",
)

DEFAULT_FIXTURE_DIR = Path("tests/fixtures/replay")


def default_transport() -> Transport:
    """The live adapter. Imported lazily so the offline suite never loads Playwright."""
    from .playwright_transport import PlaywrightTransport

    return PlaywrightTransport()


#: Seams. Tests replace these with ReplayTransport and a manual clock.
transport_factory: Callable[[], Transport] = default_transport


def default_limiter(delay: float) -> RateLimiter:
    """The real limiter, on the real clock."""
    return RateLimiter(delay)


limiter_factory: Callable[[float], RateLimiter] = default_limiter


def _client(delay: float = DEFAULT_DELAY_SECONDS) -> IdxClient:
    return IdxClient(transport=transport_factory(), limiter=limiter_factory(delay))


def _emit(summary: dict[str, Any]) -> None:
    typer.echo(json.dumps(summary, indent=2, default=str))


def _parse_years(tahun: str) -> list[int]:
    """`2021-2025` or `2024`. Inclusive."""
    text = tahun.strip()
    if "-" in text:
        first, _, last = text.partition("-")
        start, end = int(first), int(last)
    else:
        start = end = int(text)
    if end < start:
        raise typer.BadParameter(f"{tahun!r}: the range ends before it starts")
    return list(range(start, end + 1))


def _read_codes(path: Path) -> list[str]:
    """One issuer code per line; `#` comments and blank lines ignored."""
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0].strip().upper()
        if code:
            codes.append(code)
    return codes


def _write_jsonl(out: Path, rows: Iterable[dict[str, Any]]) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            written += 1
    return written


def _counts(statuses: Iterable[str]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for status in statuses:
        tally[status] = tally.get(status, 0) + 1
    return tally


def _general_info_row(kode: str, tahun: int, result: FetchResult[GeneralInfo]) -> dict[str, Any]:
    """One JSONL row: status, detail, raw beside normalised, source URL, timestamp."""
    row: dict[str, Any] = {
        "kode": kode,
        "tahun": tahun,
        "status": str(result.status),
        "detail": result.detail,
        "source_url": result.source_url,
        "fetched_at": result.fetched_at.isoformat(),
    }
    info = result.value
    for label in GENERAL_INFO_LABELS:
        column = column_name(label)
        row[column] = info.normalised(label) if info is not None else None
        row[f"raw_{column}"] = info.raw(label) if info is not None else None
    return row


def _announcement_row(row: Announcement) -> dict[str, Any]:
    payload = asdict(row)
    payload["status"] = str(row.status)
    payload["is_collective"] = row.is_collective
    return payload


def _fetch_one(client: IdxClient, reader: XbrlReader, kode: str, tahun: int):  # noqa: ANN202
    """Fetch and parse one issuer-year. The XBRL statuses win over the fetch status."""
    fetched = client.financial_statement(kode, tahun)
    if not fetched.ok or fetched.value is None:
        return FetchResult(
            status=fetched.status,
            value=None,
            source_url=fetched.source_url,
            fetched_at=fetched.fetched_at,
            detail=fetched.detail,
        )
    return reader.read_general_info(
        fetched.value, source_url=fetched.source_url, fetched_at=fetched.fetched_at
    )


# -- commands ------------------------------------------------------------------


@app.command()
@loud
def doctor(
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Report which backend is ready right now."""
    report: dict[str, Any] = {
        "playwright": _probe_import("playwright"),
        "pymupdf": _probe_import("pymupdf"),
        "tesseract": _probe_tesseract(),
        "browser_context": {"loaded": False, "detail": "not attempted by doctor"},
        "cloudflare": {"passed": False, "detail": "not attempted by doctor"},
        "announcement_window": None,
        "profiles_probe": {"status": None, "rows": 0, "detail": ""},
    }

    # One transport, built once: for the live adapter, building it opens a browser.
    client = _client()
    report["transport"] = type(client._transport).__name__  # noqa: SLF001 - reported, not used
    if report["transport"] == "PlaywrightTransport":
        loaded, detail = _probe_browser(client)
        report["browser_context"] = {"loaded": loaded, "detail": detail}
        report["cloudflare"] = {"passed": loaded, "detail": detail}

    profiles = client.profiles()
    report["profiles_probe"] = {
        "status": str(profiles.status),
        "rows": len(profiles.value or []),
        "detail": profiles.detail,
    }

    announcements = client.announcements(keyword=SUSPENSION_KEYWORDS[4])
    window = client.announcement_window
    report["announcement_window"] = (
        [window[0].isoformat(), window[1].isoformat()] if window else None
    )
    report["announcement_probe"] = {
        "status": str(announcements.status),
        "rows": len(announcements.value or []),
    }

    if json_output:
        _emit(report)
        return
    for key, value in report.items():
        typer.echo(f"{key}: {json.dumps(value, default=str)}")


def _probe_import(module: str) -> dict[str, Any]:
    try:
        imported = __import__(module)
    except ImportError as error:
        return {"installed": False, "version": None, "detail": str(error)}
    return {
        "installed": True,
        "version": getattr(imported, "__version__", None),
        "detail": "",
    }


def _probe_tesseract() -> dict[str, Any]:
    import shutil

    found = shutil.which("tesseract")
    return {
        "installed": bool(found),
        "path": found,
        "detail": "" if found else "OCR for scanned audit reports is unavailable without it",
    }


def _probe_browser(client: IdxClient) -> tuple[bool, str]:
    try:
        return client._transport.probe()  # noqa: SLF001 - adapter-specific probe
    except Exception as error:  # noqa: BLE001 - doctor reports, never raises
        return False, f"{type(error).__name__}: {error}"


@app.command()
@loud
def profil(
    all_: Annotated[bool, typer.Option("--all", help="Every currently listed company.")] = False,
    out: Annotated[Path, typer.Option("--out")] = Path("profil.csv"),
) -> None:
    """Write the listed-company profiles to CSV."""
    if not all_:
        raise typer.BadParameter("pass --all: IDX serves the profile list whole or not at all")
    result = _client().profiles()
    rows = result.value or []
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "kode",
                "nama",
                "sektor",
                "sub_sektor",
                "industri",
                "sub_industri",
                "papan_pencatatan",
                "tanggal_pencatatan",
                "source_url",
                "fetched_at",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.kode,
                    row.nama,
                    row.sektor,
                    row.sub_sektor,
                    row.industri,
                    row.sub_industri,
                    row.papan_pencatatan,
                    row.tanggal_pencatatan,
                    result.source_url,
                    result.fetched_at.isoformat(),
                ]
            )
    _emit(
        {
            "rows": len(rows),
            "errors": 0 if result.ok else 1,
            "status": str(result.status),
            "out": str(out.resolve()),
        }
    )


@app.command()
@loud
def lk(
    kode: Annotated[str, typer.Option("--kode")],
    tahun: Annotated[str, typer.Option("--tahun")],
    out: Annotated[Path, typer.Option("--out")] = Path("lk.jsonl"),
    delay: Annotated[float, typer.Option("--delay")] = DEFAULT_DELAY_SECONDS,
) -> None:
    """Fetch and parse the annual XBRL general information for one issuer."""
    years = _parse_years(tahun)
    client, reader = _client(delay), XbrlReader()
    rows = [
        _general_info_row(kode.upper(), year, _fetch_one(client, reader, kode, year))
        for year in years
    ]
    written = _write_jsonl(out, rows)
    tally = _counts(row["status"] for row in rows)
    _emit(
        {
            "rows": written,
            "errors": sum(count for status, count in tally.items() if status != RowStatus.OK),
            "by_status": tally,
            "out": str(out.resolve()),
        }
    )


@app.command("lk-batch")
@loud
def lk_batch(
    kode_file: Annotated[Path, typer.Option("--kode-file")],
    tahun: Annotated[str, typer.Option("--tahun")],
    out: Annotated[Path, typer.Option("--out")] = Path("lk.jsonl"),
    db: Annotated[Path, typer.Option("--db")] = Path("data/idx-reach.sqlite"),
    delay: Annotated[float, typer.Option("--delay")] = DEFAULT_DELAY_SECONDS,
) -> None:
    """Fetch many issuer-years, resuming from the store.

    A full run is about 36 minutes per 330 files at the mandatory spacing, so it
    will be interrupted; `pending()` decides what is left (record §7.2).
    """
    years = _parse_years(tahun)
    codes = _read_codes(kode_file)
    client, reader = _client(delay), XbrlReader()

    with Store(db) as store:
        wanted = store.pending(codes, years)
        rows: list[dict[str, Any]] = []
        for kode, year in wanted:
            result = _fetch_one(client, reader, kode, year)
            store.save_general_info(kode, year, result)
            rows.append(_general_info_row(kode, year, result))
        written = _write_jsonl(out, rows)
        tally = _counts(row["status"] for row in rows)
        _emit(
            {
                "rows": written,
                "fetched": len(wanted),
                "skipped": len(codes) * len(years) - len(wanted),
                "errors": sum(count for status, count in tally.items() if status != RowStatus.OK),
                "by_status": tally,
                "db": str(db.resolve()),
                "out": str(out.resolve()),
            }
        )


@app.command()
@loud
def lampiran(
    kode: Annotated[str, typer.Option("--kode")],
    tahun: Annotated[int, typer.Option("--tahun")],
    out: Annotated[Path | None, typer.Option("--out")] = None,
) -> None:
    """List the attachment files for one issuer-year: the fallback when XBRL is corrupt."""
    destination = out or Path(f"lampiran-{kode.upper()}-{tahun}.jsonl")
    result = _client().attachments(kode, tahun)
    rows = [
        {
            **asdict(attachment),
            "source_url": result.source_url,
            "fetched_at": result.fetched_at.isoformat(),
        }
        for attachment in (result.value or [])
    ]
    written = _write_jsonl(destination, rows)
    _emit(
        {
            "rows": written,
            "errors": 0 if result.ok else 1,
            "status": str(result.status),
            "detail": result.detail,
            "out": str(destination.resolve()),
        }
    )


@app.command()
@loud
def pengumuman(
    kode: Annotated[str | None, typer.Option("--kode")] = None,
    keyword: Annotated[str | None, typer.Option("--keyword")] = None,
    tahun: Annotated[str | None, typer.Option("--tahun")] = None,
    out: Annotated[Path, typer.Option("--out")] = Path("pengumuman.jsonl"),
) -> None:
    """Announcements for one issuer, one keyword, or both."""
    if not kode and not keyword:
        raise typer.BadParameter("pass --kode, --keyword, or both")
    years = _parse_years(tahun) if tahun else None
    span = (years[0], years[-1]) if years else None

    result = _client().announcements(kode=kode, keyword=keyword, tahun=span)
    rows = [_announcement_row(row) for row in (result.value or [])]
    written = _write_jsonl(out, rows)
    _emit(
        {
            "rows": written,
            "errors": 0 if result.ok else 1,
            "status": str(result.status),
            "detail": result.detail,
            "by_status": _counts(row["status"] for row in rows),
            "out": str(out.resolve()),
        }
    )


@app.command()
@loud
def suspensi(
    kode_file: Annotated[Path, typer.Option("--kode-file")],
    tahun: Annotated[str, typer.Option("--tahun")],
    out: Annotated[Path, typer.Option("--out")] = Path("suspensi.jsonl"),
    delay: Annotated[float, typer.Option("--delay")] = DEFAULT_DELAY_SECONDS,
) -> None:
    """Suspension events, both passes (§2.5), with warrant titles excluded (§6.2).

    Pass one is per issuer. Pass two is by keyword, because mass suspensions
    carry an empty `Kode_Emiten` and no code in the title -- only 6 of 745
    announcements under `Pemantauan Khusus` carried one, so a per-issuer query
    alone misses them entirely.
    """
    years = _parse_years(tahun)
    span = (years[0], years[-1])
    codes = _read_codes(kode_file)
    client = _client(delay)

    rows: list[dict[str, Any]] = []
    excluded = 0
    seen: set[tuple[str | None, str | None, str]] = set()
    statuses: list[str] = []

    def collect(result: FetchResult[list[Announcement]], pass_name: str, query: str) -> None:
        nonlocal excluded
        statuses.append(str(result.status))
        for row in result.value or []:
            if row.status is RowStatus.OK and excluded_as_warrant(row.judul):
                excluded += 1
                continue
            key = (row.nomor, row.kode, row.judul)
            if key in seen:
                continue
            seen.add(key)
            payload = _announcement_row(row)
            payload["pass"] = pass_name
            payload["query"] = query
            payload["source_url"] = result.source_url
            payload["fetched_at"] = result.fetched_at.isoformat()
            rows.append(payload)

    for kode in codes:
        collect(client.announcements(kode=kode, tahun=span), "per-issuer", kode)
    for keyword in SUSPENSION_KEYWORDS:
        collect(client.announcements(keyword=keyword, tahun=span), "keyword", keyword)

    written = _write_jsonl(out, rows)
    tally = _counts(row["status"] for row in rows)
    _emit(
        {
            "rows": written,
            "errors": sum(1 for status in statuses if status != RowStatus.OK),
            "by_status": tally,
            "excluded_waran_seri": excluded,
            "passes": ["per-issuer", "keyword"],
            "keywords": list(SUSPENSION_KEYWORDS),
            "issuers": len(codes),
            "out": str(out.resolve()),
        }
    )


@app.command()
@loud
def fetch(
    url: Annotated[str, typer.Argument()],
    out: Annotated[Path, typer.Option("--out")] = Path("berkas.pdf"),
) -> None:
    """Download one URL through the browser transport."""
    client = _client()
    client.limiter.acquire()
    response = client._transport.get_bytes(url)  # noqa: SLF001 - fetch is a raw passthrough
    client.limiter.record(response.status)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(response.body)
    _emit(
        {
            "http_status": response.status,
            "bytes": len(response.body),
            "content_type": response.content_type,
            "errors": 0 if response.status == 200 else 1,
            "out": str(out.resolve()),
            "fetched_at": response.fetched_at.isoformat(),
        }
    )


@app.command()
@loud
def record(
    url: Annotated[str, typer.Argument()],
    fixture_dir: Annotated[Path, typer.Option("--fixture-dir")] = DEFAULT_FIXTURE_DIR,
) -> None:
    """Capture one URL as a test fixture, replacing a synthetic replica."""
    client = _client()
    client.limiter.acquire()
    response = client._transport.get_bytes(url)  # noqa: SLF001 - record is a raw passthrough
    client.limiter.record(response.status)
    body_path = write_fixture(
        fixture_dir,
        url=url,
        status=response.status,
        headers={"content-type": response.content_type},
        body=response.body,
        fetched_at=response.fetched_at,
    )
    _emit(
        {
            "http_status": response.status,
            "bytes": len(response.body),
            "key": body_path.stem,
            "out": str(body_path.resolve()),
            "fetched_at": response.fetched_at.isoformat(),
        }
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
