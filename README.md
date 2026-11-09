# idx-reach

A CLI that fetches Indonesia Stock Exchange (IDX / BEI) data for researchers,
through a real browser, one request at a time. One CLI, pluggable backends, and a
`doctor` command that reports which backend is ready right now.

**State of the tool: every layer is implemented. The offline suite passes
without network access, and the live browser adapter has been verified against
idx.co.id.** See [What is and is not proven](#what-is-and-is-not-proven).

---

## Install

```sh
uv tool install idx-reach         # from a release
uv sync                           # from a clone, for development
uv run playwright install chromium
```

Python 3.11 or newer. OCR for scanned audit reports additionally needs the
`tesseract` binary on `PATH`; everything else works without it, and `doctor`
tells you whether it is there.

## The first run, and the Cloudflare handshake

A plain HTTP client cannot reach IDX at all. `curl` with a Chrome User-Agent gets
HTTP 403 and a 6354-byte Cloudflare JavaScript challenge page. Only `fetch()`
executed inside a loaded `www.idx.co.id` page gets through.

So the browser is not an implementation detail, it is the product:

1. The first run opens a visible Chromium window on a persistent profile under
   `~/.idx-reach/browser`.
2. If Cloudflare shows an interstitial, clear it yourself, once, in that window.
3. Leave the window open. Every request after that runs inside that page.

The profile persists, so later runs normally start straight into the site.

**idx-reach will never copy your `cf_clearance` or `__cf_bm` cookies into another
HTTP client.** That is bot-check circumvention, and the tokens are bound to a
User-Agent and an IP address anyway, so it would be fragile as well as wrong.
Requests run inside the page or they do not run.

## Pacing

One request at a time, 6.5 seconds apart, never in parallel. That spacing is
measured, not guessed: 330 consecutive XBRL downloads at 6.5 s produced zero
HTTP 429. `--delay` can raise it. Do not lower it without a reason.

After three consecutive HTTP 429s the breaker trips, the run stops and reports.
It does not retry by itself. An HTTP 503 does **not** count toward the breaker: a
503 from Varnish on the announcement endpoint means IDX was sent a parameter it
does not recognise, and backing off would be retreating from a bug in the query.

A full population run takes time: about 36 minutes per 330 files. `lk-batch`
keeps progress in SQLite and resumes, so an interrupted run costs at most the row
in flight.

## Commands

Every command writes a file and prints a small JSON summary — row count, error
count, output path. A dataset never comes back on stdout.

```sh
idx-reach doctor --json
idx-reach profil --all --out profil.csv
idx-reach lk --kode TLKM --tahun 2021-2025 --out lk.jsonl
idx-reach lk-batch --kode-file populasi.txt --tahun 2021-2025 --out lk.jsonl
idx-reach lampiran --kode LUCK --tahun 2024
idx-reach pengumuman --kode TECH
idx-reach pengumuman --keyword "Penghentian Sementara Perdagangan"
idx-reach suspensi --kode-file populasi.txt --tahun 2021-2025
idx-reach fetch <url> --out berkas.pdf
idx-reach record <url>            # writes a fixture for the test suite
```

`doctor` reports the Playwright version, whether a browser context can load
idx.co.id, whether the Cloudflare challenge was passed, whether tesseract and
PyMuPDF are installed, the live announcement window, and the result of one probe
against the profiles endpoint. `--json` is the machine-readable form.

## Row status: what each value asks you to do

Every row carries a status, never a boolean "available". Three different facts
look identical once they are flattened into "no data", and they call for three
different actions.

| Status | What it means | What you do |
|---|---|---|
| `OK` | Fetched and parsed; every requested field is present. | Use it. |
| `NOT_LISTED` | HTTP 404. The issuer was not yet listed that year, or no longer is. | Check the listing date. This is a fact about the issuer, not a failure. |
| `FILE_CORRUPT` | HTTP 200 with a spreadsheet content-type, but the body is not a ZIP — 165 bytes beginning `\rAdministrator`. Broken at IDX's end. | Run `lampiran` for that issuer-year: in both observed cases the audit report was there in full as a PDF. |
| `FIELD_EMPTY` | Parsed fine; the field is blank at source. | Read the PDF, or report it as missing. Do not treat it as a read failure. |
| `CONFLICTING` | Fields inside one file contradict each other. | Read the PDF and decide. The tool will not pick a side for you. |
| `OUT_OF_WINDOW` | Outside the three-year announcement window, so it cannot be verified here. | See below. This is **not** "no events". |
| `NETWORK_ERROR` | 429, 503 or timeout. | Retry later. `lk-batch` will pick it up; the other statuses it treats as settled. |

`CONFLICTING` is not hypothetical. One 2024 filing tagged
`Type of report on financial statements` as `Tidak Diaudit / Unaudit` while the
opinion field held `Wajar Tanpa Modifikasian` with a named firm, a signing
partner, a date and a Key Audit Matters paragraph. The PDF showed it was fully
audited.

Values are stored raw **and** normalised, side by side, so you can trace any
number back. Audit dates arrive as `2024-03-06`, `28 Februari 2023`,
`February 23, 2022` and `28 maret 2022`; one audit firm's name is spelled two
ways across years; and the opinion wording changed with SA 700 from
`Wajar Tanpa Pengecualian` to `Wajar Tanpa Modifikasian`. Both normalise to
`UNMODIFIED`, and both raw strings survive. A field that is empty at source stays
empty: nothing is defaulted, inferred or guessed.

## The three-year window

The announcement endpoint serves a window of exactly three years, ending today.
`SearchParams.DateFrom` is always today minus three years, and it cannot be
moved: every attempt to pass a date range, in any spelling, answers HTTP 503 or
is silently ignored.

So a query reaching further back does not return an empty list — it returns
`OUT_OF_WINDOW` rows for the years it cannot see. IDX points historical
announcement data at **TICMI** (ticmi.co.id), which is a paid service. If your
study period starts before the window, that is where the data is.

Two more traps worth knowing if you work with announcements:

- **Mass announcements carry no issuer code.** A single suspension announcement
  can cover dozens of issuers with an empty `Kode_Emiten` and no code in the
  title; under one keyword, only 6 of 745 announcements carried a code. A
  per-issuer query misses them, so `suspensi` runs both passes: per issuer, and
  by keyword.
- **Titles are inconsistent.** Searching `suspensi` found 5 of 21 relevant
  events, because the official title is `Penghentian Sementara Perdagangan
  Efek`. `suspensi` searches a fixed set of seven keywords, and excludes titles
  containing `Waran Seri` — those are warrant delistings, not share delistings,
  and a naive `delisting` filter would wrongly bin healthy issuers.

## What this tool refuses to do

- Store or reuse `cf_clearance` outside the browser.
- Issue parallel requests to IDX.
- Fill a source-empty field with a guessed value.
- Report "no events" for a range outside the three-year window.
- Return a large dataset on stdout.

## Development

```sh
uv run pytest          # the whole suite, offline; `network` tests are deselected
uv run ruff check
uv run python tests/fixtures/make_fixtures.py   # rebuild the fixtures
```

The architecture is four seams, two of them real:

```
cli (typer)            thin: parse args, call one module, print a JSON summary
IdxClient              deep module: every IDX quirk hides here, behind 4 methods
Transport              SEAM: PlaywrightTransport | ReplayTransport | FakeTransport
XbrlReader             deep module: bytes -> GeneralInfo
PdfReader              SEAM: TextPdfReader | OcrPdfReader
Store                  deep module: SQLite, raw beside normalised, resumable
```

No test touches the network. `tests/fixtures/PROVENANCE.md` says where every
fixture came from — read it before you trust a green suite.

## What is and is not proven

`PlaywrightTransport` was verified against the live site on 9 November 2026. Four
network-marked tests passed from a real machine: `probe()` returned HTTP 200 with
`recordsTotal=962`, the profiles endpoint answered, the TLKM 2023 workbook came
back with its ZIP magic intact, and the body survived the chunked base64 copy-out
with every ZIP entry passing its own CRC-32 check. Run them yourself:

```sh
uv run pytest -m network -s
```

They are deselected by default, so the ordinary suite still passes with no
internet.

Two things remain unproven, and no test can settle either.

The fixtures are **synthetic replicas**, not captures. They reproduce shapes that
were measured against IDX, which proves the parsers handle those shapes and says
nothing about what IDX serves today. `tests/fixtures/PROVENANCE.md` labels each
one. Replace them with real captures via `idx-reach record <url>`.

Everything this tool knows about IDX is an **observation from November 2026**, not
a contract: the 6.5 s spacing, the three-year announcement window, the keyword
set, the two XBRL taxonomies. All of it can drift. The tool is built to report
drift rather than paper over it — hence `IncompleteResultError`, the
`OUT_OF_WINDOW` status, and `doctor --json` printing the live window it actually
saw.

## Background

Every endpoint, quirk, rate limit and parsing trap described here was measured in
a real 330-request session against IDX on 8-9 November 2026, while assembling a
purposive sample of 66 listed issuers for an economics thesis. The tool exists
because that session was slow, manual, and full of traps that cost hours each:
two XBRL taxonomies that silently return empty fields, an `indexFrom` parameter
that is ignored on some queries, collective announcements that carry no issuer
code, and files served as spreadsheets that are 165 bytes of plain text.
