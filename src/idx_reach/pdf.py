"""Seam #2 -- PdfReader: two genuine adapters over one tiny interface.

`TextPdfReader` reads the PyMuPDF text layer. `OcrPdfReader` renders each page
and reads it with tesseract, and lives behind the optional `ocr` extra. Two of
the seven audit PDFs tested in the source session were pure images in exactly
the part that mattered, so the OCR path is not hypothetical.

Selection is automatic, per §1.2: a page with fewer than 50 characters of text
while carrying at least one image is scanned, and routes to OCR. `PageText`
carries `confidence` so a researcher can tell a transcription from a reading.

An unavailable OCR path raises `OcrUnavailable`. It must never degrade to an
empty page: an empty page looks exactly like a report with no opinion in it,
which is the quiet wrong answer this tool exists to avoid.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import OpinionEvidence, PageText, normalise_opinion

#: §2.10 -- all three phrasings, newest first.
OPINION_PHRASES: tuple[str, ...] = (
    "Menurut opini kami",
    "Menurut pendapat kami",
    "In our opinion",
)

#: §1.2 -- the scanned-page rule, measured in the source session.
MIN_TEXT_CHARACTERS = 50

OCR_RENDER_DPI = 140
_EXCERPT_CHARACTERS = 400


class OcrUnavailable(RuntimeError):
    """Raised when a page needs OCR and no OCR engine is available."""


@runtime_checkable
class PdfReader(Protocol):
    """The seam. One method."""

    def extract_text(self, pdf: Path) -> list[PageText]: ...


def is_scanned_page(*, text: str, image_count: int) -> bool:
    """§1.2: little or no text while the page carries images means a scan."""
    return len(text.strip()) < MIN_TEXT_CHARACTERS and image_count > 0


class TextPdfReader:
    """The PyMuPDF text layer, verbatim."""

    def extract_text(self, pdf: Path) -> list[PageText]:
        import pymupdf

        with pymupdf.open(pdf) as document:
            return [
                PageText(
                    page_number=number,
                    text=page.get_text("text"),
                    confidence="text_layer",
                )
                for number, page in enumerate(document, start=1)
            ]


class OcrPdfReader:
    """Render each page and read it with tesseract.

    `tesseract` is the binary name or path; None means "deliberately absent",
    which is how the tests exercise the refusal path.
    """

    def __init__(self, tesseract: str | None = "tesseract", language: str = "ind+eng") -> None:
        self._tesseract = tesseract
        self._language = language

    @property
    def available(self) -> bool:
        return bool(self._tesseract) and shutil.which(self._tesseract) is not None

    def extract_text(self, pdf: Path) -> list[PageText]:
        import pymupdf

        with pymupdf.open(pdf) as document:
            return [
                PageText(
                    page_number=number,
                    text=self.read_page(page, number),
                    confidence="ocr",
                )
                for number, page in enumerate(document, start=1)
            ]

    def read_page(self, page: object, page_number: int) -> str:
        """OCR one already-open PyMuPDF page."""
        if not self.available:
            raise OcrUnavailable(
                f"page {page_number} of this PDF is a scan with no text layer, and OCR is "
                f"unavailable: tesseract was not found"
                f"{'' if self._tesseract else ' (it was explicitly disabled)'}. "
                "Install tesseract and the 'ocr' extra (uv sync --extra ocr), or read this "
                "page by hand. Refusing to report an empty page, which would look like a "
                "report with no opinion in it."
            )
        pixmap = page.get_pixmap(dpi=OCR_RENDER_DPI)  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as workspace:
            image = Path(workspace) / f"page-{page_number}.png"
            pixmap.save(image)
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [str(self._tesseract), str(image), "stdout", "-l", self._language],
                capture_output=True,
                text=True,
                check=False,
            )
        if completed.returncode != 0:
            raise OcrUnavailable(
                f"tesseract failed on page {page_number}: "
                f"{completed.stderr.strip() or 'no error output'}"
            )
        return completed.stdout


def read_pdf(pdf: Path, ocr: OcrPdfReader | None = None) -> list[PageText]:
    """Read every page, routing scanned pages to OCR (§1.2).

    `ocr` defaults to a plain `OcrPdfReader`, which raises `OcrUnavailable` when
    tesseract is not installed.
    """
    import pymupdf

    engine = ocr if ocr is not None else OcrPdfReader()
    pages: list[PageText] = []
    with pymupdf.open(pdf) as document:
        for number, page in enumerate(document, start=1):
            text = page.get_text("text")
            if is_scanned_page(text=text, image_count=len(page.get_images(full=True))):
                pages.append(
                    PageText(
                        page_number=number,
                        text=engine.read_page(page, number),
                        confidence="ocr",
                    )
                )
            else:
                pages.append(PageText(page_number=number, text=text, confidence="text_layer"))
    return pages


def find_audit_opinion(pages: list[PageText]) -> OpinionEvidence | None:
    """Locate the auditor's opinion paragraph and say which page it was on.

    Returns None only when none of the three phrasings appears anywhere. The
    auditor's report is not always at the front: in MTEL 2022 it was page 6 of
    191 (§2.10).
    """
    for page in pages:
        for phrase in OPINION_PHRASES:
            position = page.text.casefold().find(phrase.casefold())
            if position < 0:
                continue
            excerpt = " ".join(page.text[position : position + _EXCERPT_CHARACTERS].split())
            return OpinionEvidence(
                page_number=page.page_number,
                phrase=phrase,
                excerpt=excerpt,
                confidence=page.confidence,
                opinion=normalise_opinion(_opinion_wording(excerpt)),
            )
    return None


def _opinion_wording(excerpt: str) -> str | None:
    """Pick the opinion wording out of the paragraph, so the enum is not guessed."""
    lowered = excerpt.casefold()
    for wording in (
        "wajar tanpa pengecualian",
        "wajar tanpa modifikasian",
        "wajar dengan pengecualian",
        "tidak menyatakan pendapat",
        "tidak wajar",
        "menyajikan secara wajar",
        "present fairly",
    ):
        if wording in lowered:
            # "menyajikan secara wajar" / "present fairly" is the body of an
            # unmodified opinion; the short forms are the field wording.
            if wording in ("menyajikan secara wajar", "present fairly"):
                return "Wajar Tanpa Modifikasian"
            return wording
    return None
