"""Parse an uploaded file into text with a page map.

Evidence spans are offsets into one continuous string, because that is what
matching needs. Highlighting needs a page number. This module holds both views
of the same text and the mapping between them, so no other module has to
reconstruct it.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from app.modules.ingestion.ocr import OcrEngine, OcrUnavailable, default_engine

#: Separator inserted between pages in the concatenated text. Two newlines so
#: sentence-level matching does not accidentally bridge a page boundary.
PAGE_BREAK = "\n\n"

#: Refused above this many pages. The 20 MB upload cap does not bound work: a
#: PDF's page tree is compressed, so a small file can declare tens of thousands
#: of pages and turn one request into minutes of parsing. Notices are not
#: hundreds of pages long, so the limit costs nothing real.
MAX_PDF_PAGES = 200

#: Below this many extractable characters per page, a PDF page is presumed to
#: be a scan whose text layer is absent or decorative.
_SCAN_THRESHOLD = 24

#: Bytes examined when deciding whether a file is text. A file that reads as
#: text for its first kilobyte is text for our purposes.
_SNIFF_BYTES = 1024

#: Fraction of the sniffed bytes that must be printable ASCII for a non-UTF-8
#: file to be treated as legacy single-byte text rather than binary.
_ASCII_TEXT_RATIO = 0.7

#: Image formats recognised by magic bytes. WEBP is handled separately because
#: its signature is split across the header.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"BM", "BMP"),
)


class SourceKind(str, Enum):
    PLAIN_TEXT = "plain_text"
    NATIVE_PDF = "native_pdf"
    #: A PDF with no usable text layer, read by OCR.
    SCANNED_PDF = "scanned_pdf"
    #: A photograph or screenshot, read by OCR.
    IMAGE = "image"


class UnsupportedDocument(Exception):
    """Raised when a file cannot be turned into text at all."""


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    text: str
    #: Offsets of this page's text within `ParsedDocument.text`.
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Document text plus everything needed to locate a span inside it."""

    text: str
    pages: tuple[Page, ...]
    source_kind: SourceKind
    #: Pages whose text layer was too sparse to trust, 1-indexed.
    pages_needing_ocr: tuple[int, ...] = ()
    #: How much to trust the characters themselves. 1.0 for a native text
    #: layer; the engine's mean confidence when the text came from OCR. The
    #: pipeline caps every derived claim at this value -- a deadline read at
    #: 61% cannot be reported as a 97% fact.
    text_confidence: float = 1.0
    #: Set when a page needed OCR and none could be performed.
    ocr_engine: str | None = None
    ocr_error: str | None = None

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("a parsed document must have at least one page")

    @property
    def needs_ocr(self) -> bool:
        """True when text is missing and OCR did not supply it."""
        return bool(self.pages_needing_ocr) and self.ocr_error is not None

    @property
    def is_ocr_derived(self) -> bool:
        return self.source_kind in (SourceKind.SCANNED_PDF, SourceKind.IMAGE)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def page_for_offset(self, offset: int) -> int:
        """Return the 1-indexed page containing `offset`.

        Offsets landing in the separator between two pages resolve to the
        earlier page, which is where the preceding sentence ended.
        """
        if not 0 <= offset < len(self.text):
            raise IndexError(f"offset {offset} outside document of {len(self.text)} chars")
        starts = [page.char_start for page in self.pages]
        index = bisect_right(starts, offset) - 1
        return self.pages[max(index, 0)].number

    def slice(self, start: int, end: int) -> str:
        return self.text[start:end]


def parse(
    source: BinaryIO | bytes | str | Path,
    *,
    filename: str | None = None,
    ocr: OcrEngine | None = None,
) -> ParsedDocument:
    """Parse bytes, a path, or an open file into a `ParsedDocument`.

    Dispatch is on content, not on the filename, because an uploaded file's
    extension is attacker-controlled and frequently just wrong.
    """
    data = _read(source)
    if not data.strip():
        raise UnsupportedDocument("file is empty")

    engine = ocr if ocr is not None else default_engine()

    if data[:5] == b"%PDF-":
        return _parse_pdf(data, engine)
    if _image_format(data):
        return _parse_image(data, engine)
    return _parse_text(data, filename)


def _image_format(data: bytes) -> str | None:
    """Identify an image by its magic bytes."""
    for signature, label in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return label
    # WEBP is "RIFF____WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


def _parse_image(data: bytes, engine: OcrEngine) -> ParsedDocument:
    """Read a photograph or screenshot of a document."""
    try:
        result = engine.read(data)
    except OcrUnavailable as exc:
        raise UnsupportedDocument(
            f"This image needs OCR to be read, but {exc}"
        ) from exc

    if not result.is_usable:
        raise UnsupportedDocument(
            "OCR could not read enough text from this image to analyse it. "
            "A sharper or higher-contrast scan may work."
        )

    document = _assemble([_tidy(result.text)], SourceKind.IMAGE)
    return replace(
        document,
        text_confidence=result.confidence,
        ocr_engine=result.engine,
    )


def from_text(text: str, *, page_break: str | None = None) -> ParsedDocument:
    """Build a document directly from text, for tests and pasted input."""
    if not text.strip():
        raise UnsupportedDocument("text is empty")
    chunks = text.split(page_break) if page_break else [text]
    return _assemble(chunks, SourceKind.PLAIN_TEXT)


def _read(source: BinaryIO | bytes | str | Path) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (str, Path)):
        return Path(source).read_bytes()
    return source.read()


def _parse_text(data: bytes, filename: str | None) -> ParsedDocument:
    """Decode text, refusing binary rather than turning it into garbage glyphs.

    Encoding detection is deliberately conservative. UTF-16 is only attempted
    behind a byte-order mark: tried speculatively it succeeds on almost any
    even-length byte string, so an uploaded image becomes a document full of
    plausible-looking CJK characters that nothing downstream can recognise as
    wrong.
    """
    label = filename or "file"

    # UTF-16 legitimately contains null bytes, so its BOM has to be read before
    # the binary heuristic below rejects it for containing them.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return _assemble([data.decode("utf-16")], SourceKind.PLAIN_TEXT)
        except UnicodeDecodeError:
            pass

    if b"\x00" in data[:_SNIFF_BYTES]:
        raise UnsupportedDocument(
            f"{label} looks like a binary format this engine cannot read"
        )

    try:
        # UTF-8 is strongly self-validating; arbitrary binary rarely decodes.
        return _assemble([data.decode("utf-8-sig")], SourceKind.PLAIN_TEXT)
    except UnicodeDecodeError:
        pass

    # Not UTF-8. Either legacy single-byte text, or binary. Real single-byte
    # text is overwhelmingly ASCII with occasional accented characters; binary
    # is not.
    if not _mostly_ascii(data):
        raise UnsupportedDocument(
            f"{label} looks like a binary format this engine cannot read"
        )

    for encoding in ("cp1252", "latin-1"):
        try:
            return _assemble([data.decode(encoding)], SourceKind.PLAIN_TEXT)
        except UnicodeDecodeError:
            continue
    raise UnsupportedDocument(f"could not decode {label} as text")


def _mostly_ascii(data: bytes) -> bool:
    """True when the head of `data` reads as prose in a single-byte encoding."""
    head = data[:_SNIFF_BYTES]
    if not head:
        return False
    textual = bytes(range(0x20, 0x7F)) + b"\n\r\t\f"
    return sum(byte in textual for byte in head) / len(head) >= _ASCII_TEXT_RATIO


def _parse_pdf(data: bytes, engine: OcrEngine) -> ParsedDocument:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UnsupportedDocument("PDF support requires pypdf") from exc

    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(data))
        # An empty password covers PDFs encrypted only to set permissions,
        # which is common for official notices. `and` short-circuits, so
        # decrypt() is still only attempted on an encrypted file.
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise UnsupportedDocument("PDF is password protected")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise UnsupportedDocument(
                f"PDF has {len(reader.pages)} pages; the limit is {MAX_PDF_PAGES}"
            )
        raw_pages = [page.extract_text() or "" for page in reader.pages]
    except UnsupportedDocument:
        raise
    except (PyPdfError, ValueError, OSError) as exc:
        raise UnsupportedDocument(f"PDF could not be read: {exc}") from exc

    if not raw_pages:
        raise UnsupportedDocument("PDF contains no pages")

    cleaned = [_tidy(page) for page in raw_pages]
    sparse = tuple(
        number
        for number, text in enumerate(cleaned, start=1)
        if len(text.strip()) < _SCAN_THRESHOLD
    )
    if not sparse:
        return _assemble(cleaned, SourceKind.NATIVE_PDF)

    filled, confidence, error = _ocr_pages(reader, cleaned, sparse, engine)
    still_empty = tuple(
        number
        for number, text in enumerate(filled, start=1)
        if len(text.strip()) < _SCAN_THRESHOLD
    )
    kind = (
        SourceKind.SCANNED_PDF
        if len(sparse) == len(cleaned)
        else SourceKind.NATIVE_PDF
    )

    if not any(text.strip() for text in filled):
        raise UnsupportedDocument(
            f"This PDF has no readable text layer and {error or 'OCR produced nothing'}."
        )

    return replace(
        _assemble(filled, kind, pages_needing_ocr=still_empty),
        text_confidence=confidence,
        ocr_engine=engine.name if error is None else None,
        ocr_error=error,
    )


def _ocr_pages(
    reader: object,
    cleaned: list[str],
    sparse: tuple[int, ...],
    engine: OcrEngine,
) -> tuple[list[str], float, str | None]:
    """Fill in pages with no text layer by reading their embedded images.

    A scanned page is almost always a single full-page image, so the image can
    be pulled straight out of the PDF rather than re-rasterizing the page --
    which avoids a second PDF library purely to redraw what is already there.
    """
    if not engine.is_available():
        return cleaned, 1.0, "no OCR engine is configured"

    filled = list(cleaned)
    confidences: list[float] = []
    failure: str | None = None

    for number in sparse:
        try:
            images = list(reader.pages[number - 1].images)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - malformed embedded stream
            failure = f"the embedded image on page {number} could not be read ({exc})"
            continue

        page_text: list[str] = []
        for image in images:
            try:
                result = engine.read(image.data)
            except OcrUnavailable as exc:
                failure = str(exc)
                break
            if result.is_usable:
                page_text.append(result.text)
                confidences.append(result.confidence)

        if page_text:
            filled[number - 1] = _tidy("\n".join(page_text))

    mean = round(sum(confidences) / len(confidences), 4) if confidences else 1.0
    return filled, mean, failure


def _assemble(
    chunks: list[str],
    kind: SourceKind,
    *,
    pages_needing_ocr: tuple[int, ...] = (),
) -> ParsedDocument:
    pages: list[Page] = []
    cursor = 0
    parts: list[str] = []

    for number, chunk in enumerate(chunks, start=1):
        if number > 1:
            parts.append(PAGE_BREAK)
            cursor += len(PAGE_BREAK)
        parts.append(chunk)
        pages.append(
            Page(
                number=number,
                text=chunk,
                char_start=cursor,
                char_end=cursor + len(chunk),
            )
        )
        cursor += len(chunk)

    return ParsedDocument(
        text="".join(parts),
        pages=tuple(pages),
        source_kind=kind,
        pages_needing_ocr=pages_needing_ocr,
    )


def _tidy(text: str) -> str:
    """Repair the two artefacts pypdf reliably introduces.

    Extractors emit a space between every glyph pair on some documents and drop
    spaces entirely on others. Only the cheap, safe fixes belong here -- the
    normalizer in the evidence module handles the rest, and it must see text
    that still corresponds to the original offsets.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)
