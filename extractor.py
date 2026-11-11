"""Shared PDF extraction logic for CAD PDF Extractor.

Used by both the Streamlit app (app.py) and the FastAPI backend
(backend/main.py). Extracts selectable or OCR-recognized text, page
information, page previews and vector drawing statistics from PDF
engineering drawings, floor plans and CAD exports. Everything runs
locally, in memory.
"""

from __future__ import annotations

import csv
import io
import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageEnhance, ImageOps

try:
    import pymupdf as fitz  # PyMuPDF (current import name)
except ImportError:  # older PyMuPDF builds
    import fitz  # type: ignore[no-redef]

APP_TITLE = "CAD PDF Extractor"
MAX_UPLOAD_MB = 50
DEFAULT_PREVIEW_DPI = 100
DEFAULT_MAX_PREVIEW_PAGES = 10
POINTS_PER_INCH = 72.0
OCR_DPI = 200
OCR_UPSCALE = 4.0
OCR_MAX_DIMENSION_PX = 2000
OCR_MIN_CONFIDENCE = 15.0
OCR_TIMEOUT_SECONDS = 60
OCR_TILE_TRIGGER_SCORE = 8
OCR_EMBEDDED_COVERAGE = 0.5
OCR_ROOM_TEXT_HINT = re.compile(
    r"\b(?:BED|BEDROOM|BATH|BATHROOM|TOILET|KITCHEN|KIT|LIVING|DINING|"
    r"STUDY|STORE|CORRIDOR|HALL|BALCONY|PORCH|TERRACE)\b",
    re.IGNORECASE,
)


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


def is_ocr_available() -> bool:
    """Return whether the Tesseract executable needed for OCR is on PATH."""
    return shutil.which("tesseract") is not None


def _embedded_page_image(page: Any) -> Optional[Tuple[Image.Image, Any]]:
    """Use a full-page raster at its native resolution instead of rerendering it."""
    try:
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        best: Optional[Tuple[int, Image.Image, Any]] = None
        for image_info in page.get_images(full=True):
            xref = image_info[0]
            image_data = page.parent.extract_image(xref)["image"]
            image = Image.open(io.BytesIO(image_data)).convert("L")
            for rect in page.get_image_rects(xref, transform=False):
                rect_area = float(rect.width) * float(rect.height)
                if rect_area < page_area * OCR_EMBEDDED_COVERAGE:
                    continue
                image_aspect = image.width / max(1, image.height)
                rect_aspect = float(rect.width) / max(1.0, float(rect.height))
                if abs(image_aspect - rect_aspect) > image_aspect * 0.03:
                    continue
                if best is None or image.width * image.height > best[0]:
                    best = (image.width * image.height, image.copy(), rect)
        if best is not None:
            return best[1], best[2]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return None


def _offset_ocr_lines(
    lines: List[Dict[str, Any]], offset_x: float, offset_y: float
) -> None:
    for line in lines:
        line["bbox"][0] += offset_x
        line["bbox"][2] += offset_x
        line["bbox"][1] += offset_y
        line["bbox"][3] += offset_y
        for word in line.get("words", []):
            word["bbox"][0] += offset_x
            word["bbox"][2] += offset_x
            word["bbox"][1] += offset_y
            word["bbox"][3] += offset_y


def read_page_text(page: Any) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Return page text, its source, and page-space text lines."""
    text = page.get_text("text") or ""
    selectable_lines = extract_page_lines(page) if text.strip() else []
    embedded_image = None
    try:
        has_embedded_images = bool(page.get_images(full=True))
    except AttributeError:
        has_embedded_images = False
    if text.strip():
        if has_embedded_images:
            embedded_image = _embedded_page_image(page)
        selectable_hint_score = sum(
            len(OCR_ROOM_TEXT_HINT.findall(line["text"]))
            for line in selectable_lines
        )
        if not has_embedded_images or selectable_hint_score >= OCR_TILE_TRIGGER_SCORE:
            return text, "selectable", selectable_lines
    if not is_ocr_available():
        return (text, "selectable", selectable_lines) if text.strip() else ("", "none", [])

    try:
        if embedded_image is None:
            # Image-only pages still benefit from OCR-ing the native
            # embedded raster instead of a re-rendered copy.
            embedded_image = _embedded_page_image(page)
        if embedded_image:
            image, source_rect = embedded_image
        else:
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(
                    OCR_DPI / POINTS_PER_INCH, OCR_DPI / POINTS_PER_INCH
                ),
                alpha=False,
            )
            image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("L")
            source_rect = page.rect
        image = ImageOps.autocontrast(image, cutoff=1)
        image = ImageEnhance.Contrast(image).enhance(1.5)
        image = ImageEnhance.Sharpness(image).enhance(1.5)
        resize_factor = min(
            OCR_UPSCALE,
            OCR_MAX_DIMENSION_PX / max(image.width, image.height),
        )
        image = image.resize(
            (
                max(1, int(image.width * resize_factor)),
                max(1, int(image.height * resize_factor)),
            ),
            Image.Resampling.BICUBIC,
        )
        image_bytes = io.BytesIO()
        image.save(image_bytes, format="PNG")

        executable = shutil.which("tesseract")
        if executable is None:
            return "", "none", []
        command = [executable, "stdin", "stdout", "-l", "eng"]
        image_data = image_bytes.getvalue()
        result = subprocess.run(
            command + ["--psm", "11", "tsv"],
            input=image_data,
            capture_output=True,
            check=True,
            timeout=OCR_TIMEOUT_SECONDS,
        )
        tsv = result.stdout.decode("utf-8", errors="replace")
        lines = _parse_tesseract_tsv(
            tsv,
            image.width,
            image.height,
            float(source_rect.width),
            float(source_rect.height),
        )
        for line in lines:
            line["ocr_pass"] = 11
        _offset_ocr_lines(lines, float(source_rect.x0), float(source_rect.y0))
        block_result = subprocess.run(
            command + ["--psm", "6", "tsv"],
            input=image_data,
            capture_output=True,
            check=True,
            timeout=OCR_TIMEOUT_SECONDS,
        )
        block_lines = _parse_tesseract_tsv(
            block_result.stdout.decode("utf-8", errors="replace"),
            image.width,
            image.height,
            float(source_rect.width),
            float(source_rect.height),
        )
        for line in block_lines:
            line["ocr_pass"] = 6
        _offset_ocr_lines(block_lines, float(source_rect.x0), float(source_rect.y0))
        room_hint_score = max(
            sum(
                len(OCR_ROOM_TEXT_HINT.findall(line["text"])) for line in lines
            ),
            sum(
                len(OCR_ROOM_TEXT_HINT.findall(line["text"]))
                for line in block_lines
            ),
        )
        lines.extend(block_lines)
        if room_hint_score < OCR_TILE_TRIGGER_SCORE:
            tiled_lines: List[Dict[str, Any]] = []
            x0, x1 = 0.12, 0.84
            for y0, y1 in ((0.12, 0.55), (0.42, 0.84)):
                crop_box = (
                    int(image.width * x0),
                    int(image.height * y0),
                    int(image.width * x1),
                    int(image.height * y1),
                )
                crop = image.crop(crop_box)
                crop_buffer = io.BytesIO()
                crop.save(crop_buffer, format="PNG")
                tile_result = subprocess.run(
                    command + ["--psm", "6", "tsv"],
                    input=crop_buffer.getvalue(),
                    capture_output=True,
                    check=True,
                    timeout=OCR_TIMEOUT_SECONDS,
                )
                tile_lines = _parse_tesseract_tsv(
                    tile_result.stdout.decode("utf-8", errors="replace"),
                    crop.width,
                    crop.height,
                    float(source_rect.width) * (x1 - x0),
                    float(source_rect.height) * (y1 - y0),
                )
                for line in tile_lines:
                    line["ocr_pass"] = 6
                _offset_ocr_lines(
                    tile_lines,
                    float(source_rect.x0) + float(source_rect.width) * x0,
                    float(source_rect.y0) + float(source_rect.height) * y0,
                )
                tiled_lines.extend(tile_lines)
            tile_score = sum(
                len(OCR_ROOM_TEXT_HINT.findall(line["text"]))
                for line in tiled_lines
            )
            if tile_score:
                lines.extend(tiled_lines)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Tesseract OCR timed out after {OCR_TIMEOUT_SECONDS} seconds."
        ) from exc
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "Tesseract OCR failed. Verify that its English ('eng') language "
            f"data is installed. {details}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start Tesseract OCR: {exc}") from exc

    ocr_text = "\n".join(line["text"] for line in lines)
    if selectable_lines and ocr_text:
        for line in selectable_lines:
            line["text_source"] = "selectable"
        for line in lines:
            line["text_source"] = "ocr"
        return f"{text.rstrip()}\n{ocr_text}", "mixed", selectable_lines + lines
    if selectable_lines:
        return text, "selectable", selectable_lines
    return ocr_text, "ocr" if ocr_text else "ocr_empty", lines


def _parse_tesseract_tsv(
    tsv: str, image_width: float, image_height: float, page_width: float, page_height: float
) -> List[Dict[str, Any]]:
    """Convert Tesseract word results into page-space text lines."""
    grouped: Dict[Tuple[str, str, str, str], List[Dict[str, Any]]] = {}
    try:
        rows = csv.DictReader(io.StringIO(tsv), delimiter="\t")
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            confidence = float(row.get("conf") or -1)
            # Tesseract often reports a zero confidence for a correctly
            # recognized room name in sparse-text mode.  Keep words that
            # match the room vocabulary even at low confidence; other
            # low-confidence words are treated as OCR noise.
            if confidence < OCR_MIN_CONFIDENCE and not OCR_ROOM_TEXT_HINT.search(
                text
            ):
                continue
            key = (
                row["page_num"],
                row["block_num"],
                row["par_num"],
                row["line_num"],
            )
            grouped.setdefault(key, []).append(
                {
                    "text": text,
                    "left": int(row["left"]),
                    "top": int(row["top"]),
                    "width": int(row["width"]),
                    "height": int(row["height"]),
                }
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not parse Tesseract OCR output: {exc}") from exc

    scale_x = page_width / max(1.0, image_width)
    scale_y = page_height / max(1.0, image_height)
    lines: List[Dict[str, Any]] = []
    for words in grouped.values():
        words.sort(key=lambda word: word["left"])
        scale_x = page_width / max(1.0, image_width)
        scale_y = page_height / max(1.0, image_height)
        word_boxes = [
            {
                "text": word["text"],
                "bbox": [
                    round(word["left"] * scale_x, 2),
                    round(word["top"] * scale_y, 2),
                    round((word["left"] + word["width"]) * scale_x, 2),
                    round((word["top"] + word["height"]) * scale_y, 2),
                ],
            }
            for word in words
        ]
        lines.append(
            {
                "text": " ".join(word["text"] for word in words),
                "bbox": [
                    round(min(word["left"] for word in words) * scale_x, 2),
                    round(min(word["top"] for word in words) * scale_y, 2),
                    round(
                        max(word["left"] + word["width"] for word in words)
                        * scale_x,
                        2,
                    ),
                    round(
                        max(word["top"] + word["height"] for word in words)
                        * scale_y,
                        2,
                    ),
                ],
                "words": word_boxes,
            }
        )
    return sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))


def extract_page_lines(page: Any) -> List[Dict[str, Any]]:
    """Return selectable text lines and their page-space bounding boxes."""
    try:
        payload = page.get_text("dict")
    except Exception:
        return []

    lines: List[Dict[str, Any]] = []
    for block in payload.get("blocks") or []:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            line_text = "".join(span.get("text", "") for span in spans).strip()
            if not line_text:
                continue
            bbox = line.get("bbox") or (0, 0, 0, 0)
            lines.append({"text": line_text, "bbox": [float(v) for v in bbox]})
    return lines


def _format_ocr_text(lines: List[Dict[str, Any]]) -> str:
    """Display recognized OCR lines without misleading spatial padding."""
    return "\n".join(str(line["text"]).strip() for line in lines)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_pdf(
    data: bytes,
    password: str = "",
    preview_dpi: int = DEFAULT_PREVIEW_DPI,
    max_preview_pages: int = DEFAULT_MAX_PREVIEW_PAGES,
    count_vectors: bool = True,
    include_analysis_data: bool = False,
) -> Tuple[Dict[str, Any], Dict[int, bytes]]:
    """Extract text, page info and vector stats.

    Returns (info, previews) where previews maps a zero based page index to
    PNG bytes. Previews are kept out of the JSON payload on purpose.
    """
    doc = open_document(data, password)
    try:
        pages: List[Dict[str, Any]] = []
        analysis_pages: List[Dict[str, Any]] = []
        previews: Dict[int, bytes] = {}
        meta = doc.metadata or {}
        total_pages = doc.page_count

        for index in range(total_pages):
            page = doc.load_page(index)

            text, text_source, text_lines = read_page_text(page)

            vector_paths: Optional[int] = None
            vector_segments: Optional[int] = None
            if count_vectors:
                vector_paths, vector_segments = count_vector_geometry(page)

            rect = page.rect
            display_text = _format_ocr_text(text_lines) if text_source == "ocr" else text
            entry: Dict[str, Any] = {
                "page_number": index + 1,
                "width_points": round(rect.width, 2),
                "height_points": round(rect.height, 2),
                "width_mm": round(rect.width * 25.4 / POINTS_PER_INCH, 1),
                "height_mm": round(rect.height * 25.4 / POINTS_PER_INCH, 1),
                "rotation_degrees": int(page.rotation),
                "text_characters": len(text.strip()),
                "text_source": text_source,
                "vector_paths": vector_paths,
                "vector_segments": vector_segments,
                "text": display_text,
            }
            pages.append(entry)
            if include_analysis_data:
                analysis_pages.append(
                    {
                        "text": text,
                        "text_source": text_source,
                        "lines": text_lines,
                    }
                )

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
        if include_analysis_data:
            info["_analysis_pages"] = analysis_pages
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
    pages_with_text = sum(
        1
        for p in pages
        if p.get("text_source") in {"selectable", "mixed"}
        or (
            "text_source" not in p
            and p.get("text_characters", 0) > 0
        )
    )
    pages_with_ocr = sum(
        1 for p in pages if p.get("text_source") in {"ocr", "mixed"}
    )
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
            "pages_with_ocr_text": pages_with_ocr,
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
