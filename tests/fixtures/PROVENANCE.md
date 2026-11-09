# Fixture provenance

**Every fixture in this directory is a SYNTHETIC REPLICA. None of it was captured
from idx.co.id.**

They were hand-built on 2026-11-09 by `tests/fixtures/make_fixtures.py`, in an
environment with no network access, to reproduce the shapes recorded in
shapes measured against the live site in November 2026. They are good
enough to exercise the parsing paths the source session found dangerous, and they
are *not* evidence about what IDX serves today.

Issuer names and values inside them are invented (`PT Replika Sintetis Tbk`). No
real issuer's filing is reproduced here.

Rebuild them with:

    uv run python tests/fixtures/make_fixtures.py

The output is deterministic, so a rebuild produces identical bytes.

## Replace these with real captures

A human with a browser should replace each file below with a real capture via
`idx-reach record <url>`, which writes a `ReplayTransport` fixture triple
(`<sha256>.bin|.json` plus `<sha256>.meta.json`) under `tests/fixtures/replay/`.
Then update this file with the real source URL and capture date per fixture, and
delete the synthetic replica it replaced. Until that happens, a green test suite
proves the code handles the *recorded shapes*, not that the shapes are still
current.

## XBRL workbooks — `xbrl/`

| File | Reproduces | Shape |
|---|---|---|
| `old_taxonomy_2021.xlsx` | plan §2.7, record §5.3 (2021–2022 column layout) | Column A **empty**, value in B, English label in C. Sheet `1000000` is second in display order and lives in `xl/worksheets/sheet3.xml`, so the workbook-rels resolution path must be exercised; a reader that guesses the part from the sheet name or takes the first worksheet reads the wrong sheet. Cells are shared strings (`t="s"`). |
| `new_taxonomy_2024.xlsx` | plan §2.7, record §5.3 (2023–2025 column layout) | Indonesian label in A, value in B, English label in C. Sheet `1000000` is first in display order but lives in `xl/worksheets/sheet2.xml`. Cells are inline strings (`t="inlineStr"`), so the pair covers both cell encodings. |
| `new_taxonomy_2024_empty_opinion.xlsx` | plan §3, record §6.3 | As above with `Type of auditor's opinion` genuinely blank at source, for the `FIELD_EMPTY` test. |
| `new_taxonomy_2024_conflicting.xlsx` | plan §3, record §6.4 (the PGJO 2024 shape) | `Type of report on financial statements` = `Tidak Diaudit / Unaudit` beside a populated unmodified opinion, firm, partner and date, for the `CONFLICTING` test. |
| `corrupt_165_bytes.bin` | plan §2.8, record §6.1 (LUCK 2024, MSTI 2024) | Exactly 165 bytes; first eight bytes `0d 41 64 6d 69 6e 69 73` (`\rAdministrator`); no ZIP magic. Served in tests with HTTP 200 and `content-type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, which is what makes it dangerous. |

The twelve English labels in these workbooks are the list from plan §2.7,
verbatim. The values exercise plan §2.9: `28 maret 2022` (lowercase Indonesian
month), `Wajar Tanpa Pengecualian` versus `Wajar Tanpa Modifikasian`, and the two
spellings of one audit firm (`… Mawar & Rekan` versus `KAP … Mawar  Rekan`).

## PDF reports — `pdf/`

Built with PyMuPDF. Invented text throughout; no real audit report is reproduced.

| File | Reproduces |
|---|---|
| `audit_report_text_layer.pdf` | plan §2.10: eight pages with a text layer, the phrase `Menurut opini kami` on **page 6**, after a table of contents and a directors' statement. In MTEL 2022 the auditor's report was page 6 of 191, so a front-page-only search misses it. |
| `audit_report_text_layer_old.pdf` | The older wording, `Menurut pendapat kami`. |
| `audit_report_text_layer_english.pdf` | The English wording, `In our opinion`. |
| `no_opinion.pdf` | None of the three phrasings, so `find_audit_opinion` returning None is tested too. |
| `audit_report_scanned.pdf` | record §5.5: a page that is an image with no text layer, as 2 of the 7 audit PDFs tested were. Exercises the §1.2 auto-selection rule and the `OcrUnavailable` refusal. |

## Announcement JSON — `json/`

Bodies for the `/primary/ListedCompany/GetAnnouncement` endpoint, in the response
shape recorded in record §2.4: `{ ResultCount, SearchParams, Replies[] }`.

| File | Reproduces |
|---|---|
| `announcement_probe.json` | The `pageSize=1` probe: `ResultCount` 745 with an empty `Replies`, plus a `SearchParams` block whose `DateFrom` is exactly three years before `DateTo` (plan §2.3). |
| `announcement_truncated.json` | plan §2.2: `ResultCount` 745 with only 250 rows returned. This is the silent truncation that `IncompleteResultError` exists to catch. |
| `announcement_collective.json` | plan §2.5, record §3.3: a mass announcement with an empty `Kode_Emiten`, no code in the title, and a PDF attachment. Modelled on `Peng-S-00016/BEI.PLP/07-2025`, whose attachment listed 41 issuers. |
| `announcement_varnish_503.json` | plan §2.4: the Varnish 503 served when an unrecognised date parameter is sent. A rejected parameter, not rate limiting. |
| `announcement_not_found_404.json` | plan §3: HTTP 404, which means `NOT_LISTED`. |

## Replay fixtures — `replay/`

`ReplayTransport` fixture triples, generated from the JSON bodies above, the
XBRL workbooks and one PDF, keyed by the SHA-256 of the full request URL. Hashing keeps
query strings out of filenames and logs, which record §7.4 found were filtered as
cookie/query-string data. `replay/INDEX.md` lists which URL each key stands for.

They cover: the profiles endpoint; the attachment list for one issuer-year;
the two taxonomies served over the static XBRL path; the 165-byte corrupt body
at HTTP 200; a 404 for an unlisted issuer-year; one announcement query per
issuer and per §2.6 keyword, each as a probe plus a full page, carrying one
real suspension title and one `Waran Seri` title that must be excluded; and one
PDF for `idx-reach fetch`.

These are synthetic too: the URLs are the real URL shapes that `IdxClient` builds
(including the doubled slash of plan §2.1), but no request was ever sent to them.
