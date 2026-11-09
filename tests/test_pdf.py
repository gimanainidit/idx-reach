"""Tests for seam #2, PdfReader (Task 6)."""

from pathlib import Path

import pytest

from idx_reach.models import OpinionKind
from idx_reach.pdf import (
    OPINION_PHRASES,
    OcrPdfReader,
    OcrUnavailable,
    PdfReader,
    TextPdfReader,
    find_audit_opinion,
    is_scanned_page,
    read_pdf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def test_both_adapters_satisfy_the_pdf_reader_interface():
    assert isinstance(TextPdfReader(), PdfReader)
    assert isinstance(OcrPdfReader(), PdfReader)


def test_all_three_phrasings_are_searched():
    """§2.10: wording differs by era, and the English form appears too."""
    assert OPINION_PHRASES == (
        "Menurut opini kami",
        "Menurut pendapat kami",
        "In our opinion",
    )


def test_a_text_layer_pdf_yields_pages_marked_text_layer():
    pages = TextPdfReader().extract_text(FIXTURES / "audit_report_text_layer.pdf")

    assert len(pages) == 8
    assert {page.confidence for page in pages} == {"text_layer"}
    assert pages[0].page_number == 1


def test_the_opinion_is_found_with_its_page_number_even_deep_in_the_file():
    """§2.10: in MTEL 2022 the auditor's report was page 6 of 191."""
    pages = TextPdfReader().extract_text(FIXTURES / "audit_report_text_layer.pdf")

    evidence = find_audit_opinion(pages)

    assert evidence is not None
    assert evidence.page_number == 6
    assert evidence.phrase == "Menurut opini kami"
    assert evidence.confidence == "text_layer"
    assert evidence.opinion is OpinionKind.UNMODIFIED
    assert "Menurut opini kami" in evidence.excerpt


@pytest.mark.parametrize(
    ("name", "phrase"),
    [
        ("audit_report_text_layer_old.pdf", "Menurut pendapat kami"),
        ("audit_report_text_layer_english.pdf", "In our opinion"),
    ],
)
def test_the_older_and_english_phrasings_are_found_too(name, phrase):
    pages = TextPdfReader().extract_text(FIXTURES / name)

    evidence = find_audit_opinion(pages)

    assert evidence is not None
    assert evidence.phrase == phrase


def test_find_audit_opinion_returns_none_when_no_phrasing_appears():
    pages = TextPdfReader().extract_text(FIXTURES / "no_opinion.pdf")

    assert find_audit_opinion(pages) is None


def test_a_scanned_page_is_detected_by_the_documented_rule():
    """§1.2: under 50 characters of text while the page carries images."""
    assert is_scanned_page(text="", image_count=1) is True
    assert is_scanned_page(text="x" * 49, image_count=2) is True
    assert is_scanned_page(text="x" * 50, image_count=2) is False
    assert is_scanned_page(text="", image_count=0) is False


def test_read_pdf_routes_a_scanned_page_to_ocr_and_says_so_when_ocr_is_missing():
    """A scanned page must never read as an empty page: that would look like no opinion."""
    with pytest.raises(OcrUnavailable) as excinfo:
        read_pdf(FIXTURES / "audit_report_scanned.pdf", ocr=OcrPdfReader(tesseract=None))

    message = str(excinfo.value)
    assert "tesseract" in message
    assert "page 1" in message


def test_read_pdf_uses_the_text_layer_when_there_is_one():
    pages = read_pdf(FIXTURES / "audit_report_text_layer.pdf")

    assert [page.confidence for page in pages] == ["text_layer"] * 8


def test_the_ocr_adapter_refuses_clearly_rather_than_returning_none():
    with pytest.raises(OcrUnavailable):
        OcrPdfReader(tesseract=None).extract_text(FIXTURES / "audit_report_scanned.pdf")
