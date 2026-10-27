"""
report_documents.py
====================================================================
Read a maintenance report file into ordered sections and source references.

This module extracts native text, embedded images, and optional OCR text.
It does not interpret defects, score severity, call a language-model API,
or change the maintenance backlog.
====================================================================
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Protocol

_PDF_HEADER = b"%PDF-"
_ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_JPEG_HEADER = b"\xff\xd8\xff"
_GIF_HEADERS = (b"GIF87a", b"GIF89a")
_TIFF_HEADERS = (b"II*\x00", b"MM\x00*")
_WEBP_RIFF = b"RIFF"
_WEBP_MARK = b"WEBP"
_JP2_HEADER = b"\x00\x00\x00\x0cjP"

_SUFFIX_KIND: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".gif": "gif",
}

_MEDIA_TYPE: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "tiff": "image/tiff",
    "gif": "image/gif",
}

_UNREAD_WARNING_CODES = frozenset(
    {
        "OCR_UNAVAILABLE",
        "OCR_FAILED",
        "OCR_EMPTY",
        "INSUFFICIENT_TEXT",
        "TEXT_EXTRACT_FAILED",
        "IMAGE_EXTRACT_FAILED",
    }
)

ORIGIN_EMBEDDED_STREAM = "embedded_stream"
ORIGIN_EMBEDDED_PART = "embedded_part"
ORIGIN_EMBEDDED_FILE = "embedded_file"
ORIGIN_RAW_STREAM = "raw_stream"


class DocumentReadError(Exception):
    """The file could not be read as a report document."""


class UnsupportedFormatError(DocumentReadError):
    """The extension or the file contents are not a supported report format."""


class DocumentLimitError(DocumentReadError):
    """The file is larger or longer than the configured limit."""


class ReadStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class ExtractionMethod(str, Enum):
    NATIVE_TEXT = "native_text"
    OCR = "ocr"
    IMAGE = "image"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    """Limits applied before a file is parsed and while its pages are read."""

    max_bytes: int = 15 * 1024 * 1024
    max_pdf_pages: int = 25
    max_docx_blocks: int = 5000
    max_images: int = 15
    min_native_text_chars: int = 8

    def __post_init__(self) -> None:
        if min(self.max_bytes, self.max_pdf_pages, self.max_docx_blocks, self.max_images) < 1:
            raise ValueError("size, page, block, and image limits must be positive")
        if self.min_native_text_chars < 1:
            raise ValueError("min_native_text_chars must be at least 1")


@dataclass(frozen=True, slots=True)
class TextBlock:
    """One exact text extract and the reference that locates it in the source."""

    block_id: str
    text: str
    method: ExtractionMethod
    reference: str
    page_number: int | None
    rows: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True, slots=True)
class ImageReference:
    """Bytes of one embedded or uploaded image, with a stable source reference."""

    image_id: str
    reference: str
    page_number: int | None
    media_type: str
    byte_size: int
    sha256: str
    data: bytes
    origin: str
    source_name: str


@dataclass(frozen=True, slots=True)
class DocumentWarning:
    """A page or section that was skipped, unread, or only partly read."""

    code: str
    message: str
    page_number: int | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentSection:
    """One PDF page, one DOCX block, or one standalone image."""

    reference: str
    page_number: int | None
    native_text: str
    ocr_text: str
    blocks: tuple[TextBlock, ...]
    images: tuple[ImageReference, ...]
    methods: tuple[ExtractionMethod, ...]


@dataclass(frozen=True, slots=True)
class DocumentBundle:
    """Ordered read of one report file. Sections keep their own source text."""

    filename: str
    media_kind: str
    sha256: str
    byte_size: int
    status: ReadStatus
    sections: tuple[DocumentSection, ...]
    warnings: tuple[DocumentWarning, ...]
    methods_used: tuple[ExtractionMethod, ...]


class OcrAdapter(Protocol):
    """Recognises text from an extracted image. Implementations must not invent images."""

    def available(self) -> bool:
        """True when this adapter can be called on this machine."""

    def read_image(self, image: ImageReference) -> str:
        """Return the text recognised in `image`, or raise if recognition fails."""


class TesseractOcrAdapter:
    """Use a local Tesseract executable when it and its Python bindings are installed."""

    def available(self) -> bool:
        if shutil.which("tesseract") is None:
            return False
        return (
            importlib.util.find_spec("pytesseract") is not None
            and importlib.util.find_spec("PIL") is not None
        )

    def read_image(self, image: ImageReference) -> str:
        if not self.available():
            raise DocumentReadError("OCR is not available.")
        import pytesseract
        from PIL import Image

        with Image.open(BytesIO(image.data)) as picture:
            recognised = pytesseract.image_to_string(picture)
        return recognised or ""


def read_report_document(
    file_bytes: bytes,
    filename: str,
    *,
    limits: DocumentLimits | None = None,
    ocr: OcrAdapter | None = None,
) -> DocumentBundle:
    """Read `file_bytes` named `filename` into sections, text blocks, and image references.

    PDF pages keep 1-based page numbers. DOCX blocks use paragraph and table
    references because a DOCX package does not store printed page numbers.
    Pages that already contain enough native text are not sent to OCR.
    A scanned page stays in the result when OCR cannot read it, with a warning
    that names that page. The returned status is partial rather than complete.
    """
    if not isinstance(file_bytes, bytes):
        raise TypeError("file_bytes must be bytes")
    display_name = _display_name(filename)
    active_limits = limits if limits is not None else DocumentLimits()
    if not isinstance(active_limits, DocumentLimits):
        raise TypeError("limits must be a DocumentLimits")
    if len(file_bytes) == 0:
        raise DocumentReadError(f"{display_name} is empty.")
    if len(file_bytes) > active_limits.max_bytes:
        raise DocumentLimitError(
            f"{display_name} is {len(file_bytes)} bytes, above the limit of {active_limits.max_bytes} bytes."
        )
    media_kind = _validated_kind(display_name, file_bytes)
    reader = ocr if ocr is not None else TesseractOcrAdapter()
    if media_kind == "pdf":
        return _read_pdf(file_bytes, display_name, active_limits, reader)
    if media_kind == "docx":
        return _read_docx(file_bytes, display_name, active_limits, reader)
    return _read_standalone_image(file_bytes, display_name, media_kind, active_limits, reader)


def _display_name(filename: str) -> str:
    if not isinstance(filename, str):
        raise DocumentReadError("A file name is required.")
    try:
        name = Path(filename).name
    except (TypeError, ValueError) as exc:
        raise DocumentReadError("The file name is not valid.") from exc
    if not name or name in {".", ".."}:
        raise DocumentReadError("A file name is required.")
    return name


def _validated_kind(filename: str, file_bytes: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUFFIX_KIND:
        supported = "PDF, DOCX, PNG, JPEG, WEBP, TIFF, GIF"
        raise UnsupportedFormatError(f"Unsupported file type {suffix or '(none)'}. Supported types: {supported}.")
    expected = _SUFFIX_KIND[suffix]
    if expected == "docx":
        _require_docx_package(filename, file_bytes)
        return "docx"
    sniffed = _sniff_kind(file_bytes)
    if sniffed != expected:
        raise UnsupportedFormatError(f"{filename} content does not match its {suffix} extension.")
    return expected


def _sniff_kind(file_bytes: bytes) -> str | None:
    if file_bytes.startswith(_PDF_HEADER):
        return "pdf"
    if file_bytes.startswith(_PNG_HEADER):
        return "png"
    if file_bytes.startswith(_JPEG_HEADER):
        return "jpeg"
    if file_bytes.startswith(_GIF_HEADERS):
        return "gif"
    if file_bytes.startswith(_TIFF_HEADERS):
        return "tiff"
    if len(file_bytes) >= 12 and file_bytes.startswith(_WEBP_RIFF) and file_bytes[8:12] == _WEBP_MARK:
        return "webp"
    if file_bytes.startswith(_ZIP_HEADERS):
        return "zip"
    return None


def _require_docx_package(filename: str, file_bytes: bytes) -> None:
    if not file_bytes.startswith(_ZIP_HEADERS):
        raise UnsupportedFormatError(f"{filename} content does not match its .docx extension.")
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise DocumentReadError(f"Could not read {filename}: the DOCX package is corrupt.") from exc
    if "word/document.xml" not in names:
        raise UnsupportedFormatError(f"{filename} is not a DOCX document.")


def _read_pdf(
    file_bytes: bytes,
    filename: str,
    limits: DocumentLimits,
    ocr: OcrAdapter,
) -> DocumentBundle:
    directory = tempfile.TemporaryDirectory(prefix="ai1-report-")
    reader = None
    try:
        path = Path(directory.name) / "source.pdf"
        path.write_bytes(file_bytes)
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise DocumentReadError("PDF support is unavailable because pypdf is not installed.") from exc
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                raise DocumentReadError(f"{filename} is encrypted and cannot be read.")
            page_count = len(reader.pages)
            if page_count < 1:
                raise DocumentReadError(f"{filename} does not contain any pages.")
            if page_count > limits.max_pdf_pages:
                counted = "page" if page_count == 1 else "pages"
                allowed = "page" if limits.max_pdf_pages == 1 else "pages"
                raise DocumentLimitError(
                    f"{filename} has {page_count} {counted}, above the limit of {limits.max_pdf_pages} {allowed}."
                )
            return _pdf_bundle(reader, file_bytes, filename, limits, ocr)
        except (DocumentReadError, DocumentLimitError, UnsupportedFormatError):
            raise
        except Exception as exc:
            raise DocumentReadError(f"Could not read {filename}: {exc}") from exc
    finally:
        if reader is not None:
            reader.close()
        directory.cleanup()


def _pdf_bundle(reader: object, file_bytes: bytes, filename: str, limits: DocumentLimits, ocr: OcrAdapter) -> DocumentBundle:
    sections: list[DocumentSection] = []
    warnings: list[DocumentWarning] = []
    image_count = 0
    for page_number, page in enumerate(reader.pages, start=1):  # type: ignore[attr-defined]
        native_text, extract_error = _pdf_page_text(page)
        images, image_count, image_warnings = _pdf_page_images(page, page_number, image_count, limits, filename)
        warnings.extend(image_warnings)
        section, section_warnings = _compose_section(
            reference=f"page:{page_number}",
            page_number=page_number,
            native_text=native_text,
            native_rows=None,
            images=tuple(images),
            limits=limits,
            ocr=ocr,
        )
        if extract_error is not None:
            section_warnings = (
                _unread_warning(
                    "TEXT_EXTRACT_FAILED",
                    page_number,
                    f"page:{page_number}",
                    extract_error,
                ),
            ) + section_warnings
        sections.append(section)
        warnings.extend(section_warnings)
    return _bundle(filename, "pdf", file_bytes, tuple(sections), tuple(warnings))


def _pdf_page_text(page: object) -> tuple[str, str | None]:
    try:
        extracted = page.extract_text()  # type: ignore[attr-defined]
    except Exception as exc:
        return "", str(exc)
    if extracted is None:
        return "", None
    return str(extracted), None


def _pdf_page_images(
    page: object,
    page_number: int,
    image_count: int,
    limits: DocumentLimits,
    filename: str,
) -> tuple[list[ImageReference], int, tuple[DocumentWarning, ...]]:
    warnings: list[DocumentWarning] = []
    images: list[ImageReference] = []
    try:
        resources = page["/Resources"]  # type: ignore[index]
    except KeyError:
        return images, image_count, ()
    except Exception as exc:
        warning = _unread_warning(
            "IMAGE_EXTRACT_FAILED",
            page_number,
            f"page:{page_number}",
            f"embedded images could not be listed ({exc})",
        )
        return images, image_count, (warning,)
    resources = _resolved(resources)
    if not hasattr(resources, "get"):
        return images, image_count, ()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return images, image_count, ()
    xobjects = _resolved(xobjects)
    try:
        names = sorted(xobjects.keys(), key=str)
    except Exception as exc:
        warning = _unread_warning(
            "IMAGE_EXTRACT_FAILED",
            page_number,
            f"page:{page_number}",
            f"embedded images could not be listed ({exc})",
        )
        return images, image_count, (warning,)
    for name in names:
        try:
            image_obj = _resolved(xobjects[name])
        except Exception as exc:
            warnings.append(
                _unread_warning(
                    "IMAGE_EXTRACT_FAILED",
                    page_number,
                    f"page:{page_number}",
                    f"embedded image {name} could not be opened ({exc})",
                )
            )
            continue
        if not hasattr(image_obj, "get") or str(image_obj.get("/Subtype", "")) != "/Image":
            continue
        image_count += 1
        if image_count > limits.max_images:
            raise DocumentLimitError(
                f"{filename} contains more than {limits.max_images} embedded images."
            )
        try:
            payload = image_obj.get_data()
        except Exception as exc:
            warnings.append(
                _unread_warning(
                    "IMAGE_EXTRACT_FAILED",
                    page_number,
                    f"page:{page_number}",
                    f"embedded image {name} could not be extracted ({exc})",
                )
            )
            image_count -= 1
            continue
        data = payload.encode("latin-1") if isinstance(payload, str) else bytes(payload)
        media_type = _media_type_of(data)
        origin = ORIGIN_EMBEDDED_STREAM if media_type is not None else ORIGIN_RAW_STREAM
        if media_type is None:
            media_type = "application/octet-stream"
            warnings.append(
                DocumentWarning(
                    code="IMAGE_RAW_STREAM",
                    message=(
                        f"Page {page_number} image {image_count} is stored as a raw PDF image stream "
                        "and was not repackaged."
                    ),
                    page_number=page_number,
                    reference=f"page:{page_number}/image:{len(images) + 1}",
                )
            )
        image_id = f"page:{page_number}/image:{len(images) + 1}"
        images.append(_image_reference(image_id, page_number, media_type, data, origin, str(name).lstrip("/")))
    return images, image_count, tuple(warnings)


def _read_docx(
    file_bytes: bytes,
    filename: str,
    limits: DocumentLimits,
    ocr: OcrAdapter,
) -> DocumentBundle:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocumentReadError("DOCX support is unavailable because python-docx is not installed.") from exc
    try:
        document = Document(BytesIO(file_bytes))
    except Exception as exc:
        raise DocumentReadError(f"Could not read {filename}: {exc}") from exc

    sections: list[DocumentSection] = []
    warnings: list[DocumentWarning] = []
    paragraph_number = 0
    table_number = 0
    block_count = 0
    image_count = 0
    paragraph_tag = qn("w:p")
    table_tag = qn("w:tbl")
    for child in document.element.body.iterchildren():
        if child.tag == paragraph_tag:
            block_count += 1
            _reject_too_many_blocks(filename, block_count, limits)
            paragraph_number += 1
            reference = f"paragraph:{paragraph_number}"
            paragraph = Paragraph(child, document)
            drafts, image_warnings, image_count = _images_in_element(
                child, document, reference, None, image_count, limits, filename
            )
            warnings.extend(image_warnings)
            if paragraph.text == "" and not drafts and not image_warnings:
                continue
            section, section_warnings = _compose_section(
                reference=reference,
                page_number=None,
                native_text=paragraph.text,
                native_rows=None,
                images=tuple(drafts),
                limits=limits,
                ocr=ocr,
            )
            sections.append(section)
            warnings.extend(section_warnings)
        elif child.tag == table_tag:
            block_count += 1
            _reject_too_many_blocks(filename, block_count, limits)
            table_number += 1
            reference = f"table:{table_number}"
            table = Table(child, document)
            rows = tuple(tuple(cell.text for cell in row.cells) for row in table.rows)
            drafts, image_warnings, image_count = _images_in_element(
                child, document, reference, None, image_count, limits, filename
            )
            warnings.extend(image_warnings)
            section, section_warnings = _compose_section(
                reference=reference,
                page_number=None,
                native_text=_table_text(rows),
                native_rows=rows,
                images=tuple(drafts),
                limits=limits,
                ocr=ocr,
            )
            sections.append(section)
            warnings.extend(section_warnings)
    return _bundle(filename, "docx", file_bytes, tuple(sections), tuple(warnings))


def _images_in_element(
    element: object,
    document: object,
    parent_reference: str,
    page_number: int | None,
    image_count: int,
    limits: DocumentLimits,
    filename: str,
) -> tuple[list[ImageReference], tuple[DocumentWarning, ...], int]:
    warnings: list[DocumentWarning] = []
    images: list[ImageReference] = []
    seen: set[str] = set()
    blips, blip_error = _xpath_local(element, "blip")
    vml_images, vml_error = _xpath_local(element, "imagedata")
    if blip_error is not None or vml_error is not None:
        reason = blip_error or vml_error
        warning = DocumentWarning(
            code="IMAGE_EXTRACT_FAILED",
            message=f"{parent_reference} embedded images could not be listed ({reason}).",
            page_number=page_number,
            reference=parent_reference,
        )
        return images, (warning,), image_count
    from docx.oxml.ns import qn

    relation_ids: list[str] = []
    for blip in blips:
        relation_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if relation_id:
            relation_ids.append(str(relation_id))
    for image_data in vml_images:
        relation_id = image_data.get(qn("r:id"))
        if relation_id:
            relation_ids.append(str(relation_id))
    related = document.part.related_parts  # type: ignore[attr-defined]
    for relation_id in relation_ids:
        if relation_id in seen:
            continue
        seen.add(relation_id)
        part = related.get(relation_id) if hasattr(related, "get") else None
        if part is None:
            warnings.append(
                DocumentWarning(
                    code="IMAGE_EXTRACT_FAILED",
                    message=f"{parent_reference} is missing embedded image {relation_id}.",
                    page_number=page_number,
                    reference=parent_reference,
                )
            )
            continue
        image_count += 1
        if image_count > limits.max_images:
            raise DocumentLimitError(f"{filename} contains more than {limits.max_images} embedded images.")
        data = bytes(part.blob)
        media_type = str(getattr(part, "content_type", "") or "") or _media_type_of(data) or "application/octet-stream"
        image_id = f"{parent_reference}/image:{len(images) + 1}"
        source_name = str(getattr(part, "partname", relation_id))
        images.append(_image_reference(image_id, page_number, media_type, data, ORIGIN_EMBEDDED_PART, source_name))
    return images, tuple(warnings), image_count


def _read_standalone_image(
    file_bytes: bytes,
    filename: str,
    media_kind: str,
    limits: DocumentLimits,
    ocr: OcrAdapter,
) -> DocumentBundle:
    if limits.max_images < 1:
        raise DocumentLimitError(f"{filename} exceeds the embedded image limit.")
    image = _image_reference(
        "image:1",
        None,
        _MEDIA_TYPE[media_kind],
        file_bytes,
        ORIGIN_EMBEDDED_FILE,
        filename,
    )
    section, warnings = _compose_section(
        reference="image:1",
        page_number=None,
        native_text="",
        native_rows=None,
        images=(image,),
        limits=limits,
        ocr=ocr,
    )
    return _bundle(filename, media_kind, file_bytes, (section,), warnings)


def _compose_section(
    *,
    reference: str,
    page_number: int | None,
    native_text: str,
    native_rows: tuple[tuple[str, ...], ...] | None,
    images: tuple[ImageReference, ...],
    limits: DocumentLimits,
    ocr: OcrAdapter,
) -> tuple[DocumentSection, tuple[DocumentWarning, ...]]:
    blocks: list[TextBlock] = []
    warnings: list[DocumentWarning] = []
    if native_text != "" or native_rows is not None:
        blocks.append(
            TextBlock(
                block_id=f"{reference}/text:1",
                text=native_text,
                method=ExtractionMethod.NATIVE_TEXT,
                reference=reference,
                page_number=page_number,
                rows=native_rows,
            )
        )
    ocr_parts: list[str] = []
    if not _has_usable_text(native_text, limits.min_native_text_chars):
        if images:
            ready, reason = _ocr_ready(ocr)
            if not ready:
                warnings.append(_unread_warning("OCR_UNAVAILABLE", page_number, reference, reason or "OCR is not available."))
            else:
                for image in images:
                    try:
                        recognised = ocr.read_image(image)
                    except Exception as exc:
                        warnings.append(_unread_warning("OCR_FAILED", page_number, image.image_id, str(exc)))
                        continue
                    if not _has_usable_text(recognised, 1):
                        warnings.append(_unread_warning("OCR_EMPTY", page_number, image.image_id, "OCR returned no text."))
                        continue
                    ocr_parts.append(recognised)
                    blocks.append(
                        TextBlock(
                            block_id=f"{image.image_id}/ocr",
                            text=recognised,
                            method=ExtractionMethod.OCR,
                            reference=image.image_id,
                            page_number=page_number,
                        )
                    )
        elif _alphanumeric_count(native_text) > 0:
            warnings.append(
                _unread_warning(
                    "INSUFFICIENT_TEXT",
                    page_number,
                    reference,
                    "native text is below the readable minimum and there is no embedded image to recognise.",
                )
            )
    section = DocumentSection(
        reference=reference,
        page_number=page_number,
        native_text=native_text,
        ocr_text="\n".join(ocr_parts),
        blocks=tuple(blocks),
        images=images,
        methods=_section_methods(blocks, images),
    )
    return section, tuple(warnings)


def _bundle(
    filename: str,
    media_kind: str,
    file_bytes: bytes,
    sections: tuple[DocumentSection, ...],
    warnings: tuple[DocumentWarning, ...],
) -> DocumentBundle:
    return DocumentBundle(
        filename=filename,
        media_kind=media_kind,
        sha256=hashlib.sha256(file_bytes).hexdigest(),
        byte_size=len(file_bytes),
        status=_status(warnings),
        sections=sections,
        warnings=warnings,
        methods_used=_bundle_methods(sections),
    )


def _status(warnings: tuple[DocumentWarning, ...]) -> ReadStatus:
    if any(warning.code in _UNREAD_WARNING_CODES for warning in warnings):
        return ReadStatus.PARTIAL
    return ReadStatus.COMPLETE


def _section_methods(blocks: list[TextBlock], images: tuple[ImageReference, ...]) -> tuple[ExtractionMethod, ...]:
    methods: list[ExtractionMethod] = []
    if any(block.method is ExtractionMethod.NATIVE_TEXT for block in blocks):
        methods.append(ExtractionMethod.NATIVE_TEXT)
    if images:
        methods.append(ExtractionMethod.IMAGE)
    if any(block.method is ExtractionMethod.OCR for block in blocks):
        methods.append(ExtractionMethod.OCR)
    if not methods:
        methods.append(ExtractionMethod.NONE)
    return tuple(methods)


def _bundle_methods(sections: tuple[DocumentSection, ...]) -> tuple[ExtractionMethod, ...]:
    present = {method for section in sections for method in section.methods}
    if len(present) > 1:
        present.discard(ExtractionMethod.NONE)
    ordered = (
        ExtractionMethod.NATIVE_TEXT,
        ExtractionMethod.OCR,
        ExtractionMethod.IMAGE,
        ExtractionMethod.NONE,
    )
    return tuple(method for method in ordered if method in present)


def _image_reference(
    image_id: str,
    page_number: int | None,
    media_type: str,
    data: bytes,
    origin: str,
    source_name: str,
) -> ImageReference:
    return ImageReference(
        image_id=image_id,
        reference=image_id,
        page_number=page_number,
        media_type=media_type,
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        origin=origin,
        source_name=source_name,
    )


def _unread_warning(code: str, page_number: int | None, reference: str, reason: str) -> DocumentWarning:
    if page_number is None:
        message = f"{reference} could not be read: {reason}"
    else:
        message = f"Page {page_number} could not be read: {reason}"
    return DocumentWarning(code=code, message=message, page_number=page_number, reference=reference)


def _ocr_ready(ocr: OcrAdapter) -> tuple[bool, str | None]:
    try:
        ready = bool(ocr.available())
    except Exception as exc:
        return False, str(exc)
    if not ready:
        return False, "OCR is not available."
    return True, None


def _has_usable_text(text: str, minimum: int) -> bool:
    return _alphanumeric_count(text) >= minimum


def _alphanumeric_count(text: str) -> int:
    return sum(1 for character in text if character.isalnum())


def _table_text(rows: tuple[tuple[str, ...], ...]) -> str:
    return "\n".join("\t".join(row) for row in rows)


def _reject_too_many_blocks(filename: str, block_count: int, limits: DocumentLimits) -> None:
    if block_count > limits.max_docx_blocks:
        raise DocumentLimitError(
            f"{filename} has more than {limits.max_docx_blocks} paragraphs and tables."
        )


def _xpath_local(element: object, local_name: str) -> tuple[list[object], str | None]:
    try:
        found = element.xpath(f".//*[local-name()='{local_name}']")  # type: ignore[attr-defined]
    except Exception as exc:
        return [], str(exc)
    return list(found), None


def _resolved(value: object) -> object:
    getter = getattr(value, "get_object", None)
    if callable(getter):
        return getter()
    return value


def _media_type_of(data: bytes) -> str | None:
    if data.startswith(_JPEG_HEADER):
        return "image/jpeg"
    if data.startswith(_PNG_HEADER):
        return "image/png"
    if data.startswith(_GIF_HEADERS):
        return "image/gif"
    if data.startswith(_TIFF_HEADERS):
        return "image/tiff"
    if data.startswith(_JP2_HEADER):
        return "image/jp2"
    if len(data) >= 12 and data.startswith(_WEBP_RIFF) and data[8:12] == _WEBP_MARK:
        return "image/webp"
    return None


__all__ = [
    "ORIGIN_EMBEDDED_FILE",
    "ORIGIN_EMBEDDED_PART",
    "ORIGIN_EMBEDDED_STREAM",
    "ORIGIN_RAW_STREAM",
    "DocumentBundle",
    "DocumentLimitError",
    "DocumentLimits",
    "DocumentReadError",
    "DocumentSection",
    "DocumentWarning",
    "ExtractionMethod",
    "ImageReference",
    "OcrAdapter",
    "ReadStatus",
    "TesseractOcrAdapter",
    "TextBlock",
    "UnsupportedFormatError",
    "read_report_document",
]
