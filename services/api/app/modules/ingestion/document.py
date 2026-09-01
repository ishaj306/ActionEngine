"""Parse an uploaded file into text with a page map.

Evidence spans are offsets into one continuous string, because that is what
matching needs. Highlighting needs a page number. This module holds both views
of the same text and the mapping between them, so no other module has to
reconstruct it.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO

#: Separator inserted between pages in the concatenated text. Two newlines so
#: sentence-level matching does not accidentally bridge a page boundary.
PAGE_BREAK = "\n\n"

#: Below this many extractable characters per page, a PDF page is presumed to
#: be a scan whose text layer is absent or decorative.
_SCAN_THRESHOLD = 24

#: Bytes examined when deciding whether a file is text. A file that reads as
#: text for its first kilobyte is text for our purposes.
_SNIFF_BYTES = 1024

#: Fraction of the sniffed bytes that must be printable ASCII for a non-UTF-8
#: file to be treated as legacy single-byte text rather than binary.
_ASCII_TEXT_RATIO = 0.7


class SourceKind(str, Enum):
    PLAIN_TEXT = "plain_text"
    NATIVE_PDF = "native_pdf"
    #: A PDF with no usable text layer. Requires OCR before it can be used.
    SCANNED_PDF = "scanned_pdf"


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

    def __post_init__(self) -> None:
        if not self.pages:
            raise ValueError("a parsed document must have at least one page")

    @property
    def needs_ocr(self) -> bool:
        return bool(self.pages_needing_ocr)

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
) -> ParsedDocument:
    """Parse bytes, a path, or an open file into a `ParsedDocument`.

    Dispatch is on content, not on the filename, because an uploaded file's
    extension is attacker-controlled and frequently just wrong.
    """
    data = _read(source)
    if not data.strip():
        raise UnsupportedDocument("file is empty")

    if data[:5] == b"%PDF-":
        return _parse_pdf(data)
    return _parse_text(data, filename)


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


def _parse_pdf(data: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UnsupportedDocument("PDF support requires pypdf") from exc

    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            # An empty password covers PDFs encrypted only to set permissions,
            # which is common for official notices.
            if reader.decrypt("") == 0:
                raise UnsupportedDocument("PDF is password protected")
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
    kind = (
        SourceKind.SCANNED_PDF
        if len(sparse) == len(cleaned)
        else SourceKind.NATIVE_PDF
    )
    return _assemble(cleaned, kind, pages_needing_ocr=sparse)


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
