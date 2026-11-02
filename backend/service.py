"""Shared extraction pipeline used by the HTTP API and the local CLI.

``backend/main.py`` calls :func:`build_result` for HTTP requests and
``backend/cli.py`` calls it for the in-process (single-port) mode used by the
Next.js API route. Keeping the logic here means both entry points behave
identically.
"""

from __future__ import annotations

import base64
import traceback
from typing import Any, Dict, Optional

from analyzer import analyze_rooms
from extractor import (
    DEFAULT_MAX_PREVIEW_PAGES,
    DEFAULT_PREVIEW_DPI,
    MAX_UPLOAD_MB,
    build_json_payload,
    document_requires_password,
    extract_pdf,
    friendly_error,
    try_authenticate,
    validate_pdf_bytes,
)

MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_PREVIEW_DPI = 200
MIN_PREVIEW_DPI = 36
MAX_PREVIEW_PAGES_LIMIT = 500

PASSWORD_ERROR = (
    "This PDF is password protected. Enter the correct password to continue."
)


class ExtractionError(Exception):
    """A request-level failure with an HTTP status and optional error code."""

    def __init__(self, message: str, status: int = 422, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def clamp_int(value: Optional[str], default: int, low: int, high: int) -> int:
    try:
        parsed = int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def build_result(
    data: bytes,
    filename: str,
    password: str = "",
    preview_dpi: Optional[str] = None,
    max_preview_pages: Optional[str] = None,
    count_vectors: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and analyse a PDF, returning the API response payload."""
    validation_error = validate_pdf_bytes(data)
    if validation_error:
        raise ExtractionError(validation_error, status=400)

    if document_requires_password(data) and not try_authenticate(data, password):
        raise ExtractionError(PASSWORD_ERROR, status=401, code="password_required")

    dpi = clamp_int(preview_dpi, DEFAULT_PREVIEW_DPI, MIN_PREVIEW_DPI, MAX_PREVIEW_DPI)
    pages_limit = clamp_int(
        max_preview_pages, DEFAULT_MAX_PREVIEW_PAGES, 0, MAX_PREVIEW_PAGES_LIMIT
    )
    vectors = (count_vectors or "true").lower() not in {"false", "0", "no"}

    try:
        info, previews = extract_pdf(
            data,
            password=password,
            preview_dpi=dpi,
            max_preview_pages=pages_limit,
            count_vectors=vectors,
        )
        analysis = analyze_rooms(data, password=password)
    except PermissionError as exc:
        raise ExtractionError(friendly_error(exc), status=401, code="password_required")
    except Exception as exc:
        traceback.print_exc()
        raise ExtractionError(friendly_error(exc), status=422)

    preview_payload = [
        {
            "page_number": index + 1,
            "png_base64": base64.b64encode(png).decode("ascii"),
        }
        for index, png in sorted(previews.items())
    ]

    json_payload = build_json_payload(info, filename, len(data), analysis=analysis)
    document = info.get("document", {})
    pages = info.get("pages", [])
    total_paths = sum(p.get("vector_paths") or 0 for p in pages)
    total_segments = sum(p.get("vector_segments") or 0 for p in pages)
    total_text = sum(p.get("text_characters", 0) for p in pages)
    pages_with_text = sum(1 for p in pages if p.get("text_characters", 0) > 0)

    return {
        "filename": filename,
        "file_size_bytes": len(data),
        "preview_dpi": dpi,
        "count_vectors": vectors,
        "document": document,
        "pages": pages,
        "previews": preview_payload,
        "analysis": analysis,
        "summary": {
            "page_count": len(pages),
            "pages_with_selectable_text": pages_with_text,
            "pages_without_selectable_text": len(pages) - pages_with_text,
            "total_text_characters": total_text,
            "total_vector_paths": total_paths if vectors else None,
            "total_vector_segments": total_segments if vectors else None,
        },
        "json_payload": json_payload,
    }
