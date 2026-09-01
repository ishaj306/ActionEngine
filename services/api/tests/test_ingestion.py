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
        with pytest.raises(UnsupportedDocument, match="binary"):
            parse(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")

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

    def test_a_pdf_without_a_text_layer_is_flagged_for_ocr(self):
        document = parse(self.build_pdf(["", ""]))

        assert document.source_kind is SourceKind.SCANNED_PDF
        assert document.needs_ocr
        assert document.pages_needing_ocr == (1, 2)

    def test_a_truncated_pdf_raises_a_clear_error(self):
        with pytest.raises(UnsupportedDocument):
            parse(b"%PDF-1.4\n truncated before the xref table")


def test_document_must_have_at_least_one_page():
    with pytest.raises(UnsupportedDocument):
        from_text("")
