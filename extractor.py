"""Shared PDF extraction logic for CAD PDF Extractor.

Used by both the Streamlit app (app.py) and the FastAPI backend
(backend/main.py). Extracts selectable text, page information, page
previews and vector drawing statistics from PDF engineering drawings,
floor plans and CAD exports. Everything runs locally, in memory.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymupdf as fitz  # PyMuPDF (current import name)
except ImportError:  # older PyMuPDF builds
    import fitz  # type: ignore[no-redef]

APP_TITLE = "CAD PDF Extractor"
MAX_UPLOAD_MB = 50
DEFAULT_PREVIEW_DPI = 100
DEFAULT_MAX_PREVIEW_PAGES = 10
POINTS_PER_INCH = 72.0


# ---------------------------------------------------------------------------
# Validation and PDF helpers
# ---------------------------------------------------------------------------

def validate_pdf_bytes(data: Optional[bytes]) -> Optional[str]:
    """Return a human readable error message, or None when the data looks ok."""
    if data is None or len(data) == 0:
        return "The uploaded file is empty. Please choose a PDF file."
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        return (
            f"The file is larger than {MAX_UPLOAD_MB} MB. "
            "Please use a smaller PDF, or split the drawing into several files."
        )
    if b"%PDF" not in data[:1024]:
        return (
            "Unsupported file: this does not appear to be a PDF document. "
            "Please upload a standard PDF drawing (engineering drawing, "
            "floor plan or CAD export)."
        )
    return None


def document_requires_password(data: bytes) -> bool:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return False
    try:
        return bool(doc.needs_pass)
    finally:
        doc.close()


def try_authenticate(data: bytes, password: str) -> bool:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return False
    try:
        if not doc.needs_pass:
            return True
        return bool(doc.authenticate(password or ""))
    except Exception:
        return False
    finally:
        doc.close()


def open_document(data: bytes, password: str = "") -> Any:
    doc = fitz.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        if not doc.authenticate(password or ""):
            doc.close()
            raise PermissionError("A password is required to open this PDF.")
    return doc


def count_vector_geometry(page: Any) -> Tuple[int, int]:
    """Return (number of vector paths, number of drawing segments)."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return 0, 0
    paths = len(drawings)
    segments = sum(len(entry.get("items") or []) for entry in drawings)
    return paths, segments


def page_to_png(page: Any, dpi: int) -> bytes:
    zoom = dpi / POINTS_PER_INCH
    matrix = fitz.Matrix(zoom, zoom)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    return pixmap.tobytes("png")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_pdf(
    data: bytes,
    password: str = "",
    preview_dpi: int = DEFAULT_PREVIEW_DPI,
    max_preview_pages: int = DEFAULT_MAX_PREVIEW_PAGES,
    count_vectors: bool = True,
) -> Tuple[Dict[str, Any], Dict[int, bytes]]:
    """Extract text, page info and vector stats.

    Returns (info, previews) where previews maps a zero based page index to
    PNG bytes. Previews are kept out of the JSON payload on purpose.
    """
    doc = open_document(data, password)
    try:
        pages: List[Dict[str, Any]] = []
        previews: Dict[int, bytes] = {}
        meta = doc.metadata or {}
        total_pages = doc.page_count

        for index in range(total_pages):
            page = doc.load_page(index)

            try:
                text = page.get_text("text") or ""
            except Exception:
                text = ""

            vector_paths: Optional[int] = None
            vector_segments: Optional[int] = None
            if count_vectors:
                vector_paths, vector_segments = count_vector_geometry(page)

            rect = page.rect
            entry: Dict[str, Any] = {
                "page_number": index + 1,
                "width_points": round(rect.width, 2),
                "height_points": round(rect.height, 2),
                "width_mm": round(rect.width * 25.4 / POINTS_PER_INCH, 1),
                "height_mm": round(rect.height * 25.4 / POINTS_PER_INCH, 1),
                "rotation_degrees": int(page.rotation),
                "text_characters": len(text.strip()),
                "vector_paths": vector_paths,
                "vector_segments": vector_segments,
                "text": text,
            }
            pages.append(entry)

            if max_preview_pages == 0 or index < max_preview_pages:
                try:
                    previews[index] = page_to_png(page, preview_dpi)
                except Exception:
                    previews.pop(index, None)

        info: Dict[str, Any] = {
            "document": {
                "page_count": total_pages,
                "pdf_format": meta.get("format") or "unknown",
                "title": meta.get("title") or "",
                "author": meta.get("author") or "",
                "producer": meta.get("producer") or "",
                "creation_date": meta.get("creationDate") or "",
            },
            "pages": pages,
        }
        return info, previews
    finally:
        doc.close()


def build_json_payload(
    info: Dict[str, Any],
    filename: str,
    size_bytes: int,
    analysis: Optional[Dict[str, Any]] = None,
) -> str:
    pages = info.get("pages", [])
    total_text = sum(p.get("text_characters", 0) for p in pages)
    pages_with_text = sum(1 for p in pages if p.get("text_characters", 0) > 0)
    paths = [p.get("vector_paths") for p in pages]
    segments = [p.get("vector_segments") for p in pages]
    vector_stats_available = any(v is not None for v in paths)

    payload = {
        "source_file": filename,
        "file_size_bytes": size_bytes,
        "extracted_by": APP_TITLE,
        "document": info.get("document", {}),
        "summary": {
            "page_count": len(pages),
            "pages_with_selectable_text": pages_with_text,
            "pages_without_selectable_text": len(pages) - pages_with_text,
            "total_text_characters": total_text,
            "total_vector_paths": (
                sum(paths) if vector_stats_available else None
            ),
            "total_vector_segments": (
                sum(segments) if vector_stats_available else None
            ),
        },
        "pages": pages,
    }
    if analysis is not None:
        payload["analysis"] = analysis
    return json.dumps(payload, indent=2, ensure_ascii=False)


def friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    text = str(exc).lower()
    if isinstance(exc, PermissionError) or "password" in text:
        return "This PDF is password protected. Enter the correct password to continue."
    if name in {"FileDataError", "EmptyFileError"} or "cannot open" in text:
        return (
            "The file could not be opened as a PDF. It may be damaged, "
            "incomplete, or not really a PDF."
        )
    if "not a pdf" in text or "syntax" in text:
        return (
            "The file could not be read as a PDF. It may be corrupted or "
            "produced by an unsupported tool."
        )
    return f"Could not process the PDF: {exc}"


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
