"""Document parsing and the offset-to-page map.

The page map is what turns an evidence offset into a highlight, so an
off-by-one here silently points every citation at the wrong page.
"""

from __future__ import annotations

import pytest

from app.modules.ingestion.document import (
    PAGE_BREAK,
    SourceKind,
    UnsupportedDocument,
    from_text,
    parse,
)
from app.modules.ingestion.ocr import (
    NullEngine,
    OcrResult,
    OcrUnavailable,
    TesseractEngine,
)


class TestPlainText:
    def test_text_round_trips_as_a_single_page(self):
        document = from_text("Applications close on 18 September.")

        assert document.page_count == 1
        assert document.source_kind is SourceKind.PLAIN_TEXT
        assert document.text.startswith("Applications close")

    def test_utf8_bytes_are_decoded(self):
        document = parse("Résumé requis — avant le 18 septembre.".encode())

        assert "Résumé" in document.text

    def test_binary_content_is_rejected_rather_than_mangled(self):
        # A ZIP: binary, and not a format this engine claims to read.
        with pytest.raises(UnsupportedDocument, match="binary"):
            parse(b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(64))

    def test_binary_without_null_bytes_is_still_rejected(self):
        """Regression: speculative UTF-16 decoding turned binary into text.

        Almost any even-length byte string decodes as UTF-16 without error, so
        trying it unprompted made image uploads succeed as documents full of
        garbage glyphs -- a failure nothing downstream could detect.
        """
        with pytest.raises(UnsupportedDocument, match="binary"):
            parse(bytes(range(0x80, 0xFF)) * 8)

    def test_utf16_with_a_byte_order_mark_is_decoded(self):
        document = parse("Submit before 18 September.".encode("utf-16"))

        assert "Submit before" in document.text

    def test_empty_input_is_rejected(self):
        with pytest.raises(UnsupportedDocument, match="empty"):
            parse(b"   \n\t  ")


class TestPageMap:
    @pytest.fixture
    def document(self):
        return from_text(
            f"First page text.{PAGE_BREAK}Second page text.{PAGE_BREAK}Third page text.",
            page_break=PAGE_BREAK,
        )

    def test_pages_are_split_and_numbered_from_one(self, document):
        assert document.page_count == 3
        assert [page.number for page in document.pages] == [1, 2, 3]

    def test_page_offsets_slice_back_to_the_page_text(self, document):
        for page in document.pages:
            assert document.text[page.char_start : page.char_end] == page.text

    def test_offset_resolves_to_the_containing_page(self, document):
        second = document.pages[1]

        assert document.page_for_offset(second.char_start) == 2
        assert document.page_for_offset(second.char_end - 1) == 2

    def test_first_and_last_characters_resolve(self, document):
        assert document.page_for_offset(0) == 1
        assert document.page_for_offset(len(document.text) - 1) == 3

    def test_offset_in_the_page_separator_resolves_to_the_earlier_page(self, document):
        boundary = document.pages[0].char_end

        assert document.page_for_offset(boundary) == 1

    def test_offset_beyond_the_document_is_rejected(self, document):
        with pytest.raises(IndexError):
            document.page_for_offset(len(document.text))

    def test_concatenated_text_contains_every_page(self, document):
        for page in document.pages:
            assert page.text in document.text


class TestPdf:
    @staticmethod
    def build_pdf(pages: list[str]) -> bytes:
        pypdf = pytest.importorskip("pypdf")
        from io import BytesIO

        writer = pypdf.PdfWriter()
        for body in pages:
            page = writer.add_blank_page(width=612, height=792)
            _ = body, page
        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def test_a_scanned_pdf_is_rejected_when_no_ocr_is_available(self):
        """Better to refuse than to return an empty document as an analysis."""
        with pytest.raises(UnsupportedDocument, match="no readable text layer"):
            parse(self.build_pdf(["", ""]), ocr=NullEngine())

    def test_a_truncated_pdf_raises_a_clear_error(self):
        with pytest.raises(UnsupportedDocument):
            parse(b"%PDF-1.4\n truncated before the xref table")


PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FakeOcrEngine:
    """Stands in for Tesseract so the OCR paths are testable anywhere."""

    name = "fake"

    def __init__(self, text: str, confidence: float = 0.88) -> None:
        self._text = text
        self._confidence = confidence
        self.calls = 0

    def is_available(self) -> bool:
        return True

    def read(self, image: bytes) -> OcrResult:
        self.calls += 1
        _ = image
        return OcrResult(text=self._text, confidence=self._confidence, engine=self.name)


class TestImages:
    def test_a_png_is_read_by_ocr(self):
        engine = FakeOcrEngine("Submit the form before 18 September 2026.")

        document = parse(PNG_HEADER, ocr=engine)

        assert document.source_kind is SourceKind.IMAGE
        assert "18 September" in document.text
        assert engine.calls == 1

    @pytest.mark.parametrize(
        "header",
        [
            PNG_HEADER,
            b"\xff\xd8\xff\xe0\x00\x10JFIF",
            b"II*\x00\x08\x00\x00\x00",
            b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        ],
        ids=["png", "jpeg", "tiff", "webp"],
    )
    def test_image_formats_are_detected_by_magic_bytes(self, header):
        document = parse(header, ocr=FakeOcrEngine("Submit the form by Friday."))

        assert document.source_kind is SourceKind.IMAGE

    def test_ocr_confidence_is_carried_on_the_document(self):
        document = parse(
            PNG_HEADER, ocr=FakeOcrEngine("Submit the form by Friday.", confidence=0.61)
        )

        assert document.text_confidence == 0.61
        assert document.ocr_engine == "fake"
        assert document.is_ocr_derived

    def test_native_text_carries_full_confidence(self):
        document = from_text("Submit the form by Friday.")

        assert document.text_confidence == 1.0
        assert not document.is_ocr_derived

    def test_an_image_is_rejected_when_no_ocr_is_available(self):
        with pytest.raises(UnsupportedDocument, match="OCR"):
            parse(PNG_HEADER, ocr=NullEngine())

    def test_illegible_ocr_output_is_rejected_rather_than_analysed(self):
        """A 20%-confidence read is noise, and analysing noise invents findings."""
        engine = FakeOcrEngine("rn1 5ubm1t f0rrn", confidence=0.2)

        with pytest.raises(UnsupportedDocument, match="could not read"):
            parse(PNG_HEADER, ocr=engine)

    def test_empty_ocr_output_is_rejected(self):
        with pytest.raises(UnsupportedDocument, match="could not read"):
            parse(PNG_HEADER, ocr=FakeOcrEngine("   ", confidence=0.95))


class TestOcrEngines:
    def test_the_null_engine_refuses_rather_than_returning_nothing(self):
        with pytest.raises(OcrUnavailable):
            NullEngine().read(PNG_HEADER)

    def test_the_null_engine_reports_itself_unavailable(self):
        assert NullEngine().is_available() is False

    def test_tesseract_availability_tracks_the_binary_not_the_wrapper(self):
        """pytesseract imports fine without Tesseract and fails only at call time."""
        import shutil

        engine = TesseractEngine()

        assert engine.is_available() == (shutil.which("tesseract") is not None)

    def test_an_unavailable_tesseract_raises_rather_than_returning_empty_text(self):
        engine = TesseractEngine()
        if engine.is_available():
            pytest.skip("Tesseract is installed in this environment")

        with pytest.raises(OcrUnavailable, match="not installed"):
            engine.read(PNG_HEADER)

    def test_a_result_below_the_usable_threshold_is_not_usable(self):
        assert not OcrResult("some text", 0.3, "fake").is_usable
        assert OcrResult("some text", 0.9, "fake").is_usable


def test_document_must_have_at_least_one_page():
    with pytest.raises(UnsupportedDocument):
        from_text("")
