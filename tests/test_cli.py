"""Tests for the CLI (Task 8).

Every command runs against ReplayTransport through the injected transport
factory. No command touches the network under test (Global Constraint 1), and
every command prints only a small JSON summary (Global Constraint 5).
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from idx_reach import cli
from idx_reach.models import RowStatus
from idx_reach.ratelimit import ManualClock, RateLimiter
from idx_reach.transport import ReplayTransport

REPLAY = Path(__file__).parent / "fixtures" / "replay"


@pytest.fixture(autouse=True)
def offline_transport(monkeypatch):
    """Inject ReplayTransport and a manual clock, so nothing dials out and nothing sleeps."""
    monkeypatch.setattr(cli, "transport_factory", lambda: ReplayTransport(fixture_dir=REPLAY))
    monkeypatch.setattr(cli, "limiter_factory", lambda delay: RateLimiter(delay, ManualClock()))
    return None


@pytest.fixture
def run(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    def invoke(*args: str):
        result = runner.invoke(cli.app, list(args))
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        return result

    return invoke


def summary(result) -> dict:
    """Every command prints exactly one JSON object and nothing else."""
    return json.loads(result.stdout.strip())


def test_doctor_json_reports_every_documented_check(run):
    result = run("doctor", "--json")

    report = summary(result)
    assert set(report) >= {
        "playwright",
        "browser_context",
        "cloudflare",
        "tesseract",
        "pymupdf",
        "announcement_window",
        "profiles_probe",
        "transport",
    }
    assert report["pymupdf"]["installed"] is True
    assert report["profiles_probe"]["status"] == RowStatus.OK
    assert report["profiles_probe"]["rows"] == 2


def test_doctor_reports_the_live_window_from_search_params(run):
    report = summary(run("doctor", "--json"))

    assert report["announcement_window"] == ["2023-11-09", "2026-11-09"]


def test_doctor_does_not_claim_a_browser_it_never_opened(run):
    report = summary(run("doctor", "--json"))

    assert report["browser_context"]["loaded"] is False
    assert report["cloudflare"]["passed"] is False
    assert "ReplayTransport" in report["transport"]


def test_profil_writes_a_csv_and_prints_a_summary(run):
    result = run("profil", "--all", "--out", "profil.csv")

    report = summary(result)
    assert report["rows"] == 2
    assert report["errors"] == 0
    assert report["out"].endswith("profil.csv")
    lines = Path(report["out"]).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("kode,nama,")
    assert "TLKM" in lines[1]


def test_lk_writes_one_jsonl_row_per_issuer_year(run):
    result = run("lk", "--kode", "TLKM", "--tahun", "2021-2021", "--out", "lk.jsonl")

    report = summary(result)
    assert report["rows"] == 1
    rows = [
        json.loads(line) for line in Path(report["out"]).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["kode"] == "TLKM"
    assert rows[0]["status"] == RowStatus.OK
    assert rows[0]["raw_type_of_auditors_opinion"] == "Wajar Tanpa Pengecualian"
    assert rows[0]["type_of_auditors_opinion"] == "UNMODIFIED"
    assert rows[0]["source_url"].endswith("FinancialStatement-2021-Tahunan-TLKM.xlsx")
    assert rows[0]["fetched_at"]


def test_lk_reports_the_corrupt_file_as_file_corrupt(run):
    result = run("lk", "--kode", "LUCK", "--tahun", "2024-2024", "--out", "luck.jsonl")

    report = summary(result)
    rows = [
        json.loads(line) for line in Path(report["out"]).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["status"] == RowStatus.FILE_CORRUPT
    assert report["errors"] == 1
    assert report["by_status"][RowStatus.FILE_CORRUPT] == 1


def test_lk_reports_a_404_as_not_listed(run):
    result = run("lk", "--kode", "GONE", "--tahun", "2024-2024", "--out", "gone.jsonl")

    report = summary(result)
    assert report["by_status"][RowStatus.NOT_LISTED] == 1


def test_lk_batch_reads_a_code_file_and_resumes_from_the_store(run, tmp_path):
    (tmp_path / "populasi.txt").write_text("TLKM\n# a comment\n\nLUCK\n", encoding="utf-8")

    first = summary(
        run(
            "lk-batch",
            "--kode-file",
            "populasi.txt",
            "--tahun",
            "2024-2024",
            "--out",
            "batch.jsonl",
            "--db",
            "batch.sqlite",
        )
    )
    second = summary(
        run(
            "lk-batch",
            "--kode-file",
            "populasi.txt",
            "--tahun",
            "2024-2024",
            "--out",
            "batch2.jsonl",
            "--db",
            "batch.sqlite",
        )
    )

    assert first["rows"] == 2
    assert first["fetched"] == 2
    assert second["fetched"] == 0
    assert second["skipped"] == 2


def test_lampiran_lists_the_attachments_for_one_issuer_year(run):
    result = run("lampiran", "--kode", "LUCK", "--tahun", "2024")

    report = summary(result)
    assert report["rows"] == 1
    rows = [
        json.loads(line) for line in Path(report["out"]).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["file_name"].endswith("LUCK.pdf")


def test_pengumuman_by_issuer_writes_rows(run):
    result = run("pengumuman", "--kode", "TECH", "--out", "peng.jsonl")

    report = summary(result)
    assert report["rows"] == 2
    assert report["status"] == RowStatus.OK


def test_pengumuman_by_keyword_writes_rows(run):
    result = run("pengumuman", "--keyword", "Penghentian Sementara Perdagangan", "--out", "k.jsonl")

    report = summary(result)
    assert report["rows"] == 2


def test_pengumuman_needs_a_kode_or_a_keyword(run):
    result = run("pengumuman")

    assert result.exit_code != 0


def test_suspensi_runs_both_passes_and_excludes_waran_seri(run, tmp_path):
    """§2.5 both passes; §6.2 the warrant exclusion that would have binned 13 issuers."""
    (tmp_path / "populasi.txt").write_text("TECH\n", encoding="utf-8")

    result = run(
        "suspensi", "--kode-file", "populasi.txt", "--tahun", "2024-2025", "--out", "susp.jsonl"
    )

    report = summary(result)
    rows = [
        json.loads(line) for line in Path(report["out"]).read_text(encoding="utf-8").splitlines()
    ]
    titles = {row["judul"] for row in rows}
    assert any("Penghentian Sementara Perdagangan" in title for title in titles)
    assert not any("Waran Seri" in title for title in titles)
    assert report["excluded_waran_seri"] >= 1
    assert report["passes"] == ["per-issuer", "keyword"]
    assert report["keywords"] == list(cli.SUSPENSION_KEYWORDS)


def test_suspensi_reports_out_of_window_years_rather_than_no_events(run, tmp_path):
    """§2.3 and §5: an unverifiable range must never look like an empty result."""
    (tmp_path / "populasi.txt").write_text("TECH\n", encoding="utf-8")

    result = run(
        "suspensi", "--kode-file", "populasi.txt", "--tahun", "2021-2022", "--out", "old.jsonl"
    )

    report = summary(result)
    rows = [
        json.loads(line) for line in Path(report["out"]).read_text(encoding="utf-8").splitlines()
    ]
    assert report["by_status"][RowStatus.OUT_OF_WINDOW] >= 2
    assert any("TICMI" in row["judul"] for row in rows)


def test_fetch_writes_the_bytes_to_a_file_and_prints_only_a_summary(run):
    result = run(
        "fetch",
        "https://www.idx.co.id/StaticData/x/replica-audit-report.pdf",
        "--out",
        "berkas.pdf",
    )

    report = summary(result)
    assert report["bytes"] > 1000
    assert Path(report["out"]).read_bytes()[:4] == b"%PDF"


def test_record_writes_a_fixture_triple(run, tmp_path):
    result = run(
        "record",
        "https://www.idx.co.id/StaticData/x/replica-audit-report.pdf",
        "--fixture-dir",
        "fixtures",
    )

    report = summary(result)
    key = report["key"]
    assert (tmp_path / "fixtures" / f"{key}.meta.json").is_file()
    assert (tmp_path / "fixtures" / f"{key}.bin").is_file()


def test_a_missing_fixture_fails_loudly_instead_of_writing_an_empty_file(run):
    result = run("lk", "--kode", "NOPE", "--tahun", "2024-2024", "--out", "nope.jsonl")

    assert result.exit_code != 0
    assert "no recorded fixture" in result.output


def test_a_bad_year_range_is_rejected(run):
    result = run("lk", "--kode", "TLKM", "--tahun", "2025-2021", "--out", "x.jsonl")

    assert result.exit_code != 0
