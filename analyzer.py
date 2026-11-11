"""Floor-plan room analysis for CAD PDF Extractor.

Used by the FastAPI backend (backend/main.py). Builds on the shared
PyMuPDF extraction helpers in extractor.py.

How it works
------------
1. Every text line on every page is read with PyMuPDF and classified
   against a fixed vocabulary of room terms (BEDROOM, KITCHEN, LIVING
   ROOM, LOUNGE, BATHROOM, TOILET, WC, DINING ROOM, STUDY, STORE,
   CORRIDOR, ...).  Abbreviations such as "BED RM" or "KIT" are marked
   as *inferred* rather than *explicit* detections.
2. Nearby related labels on the same page are merged so one room is not
   counted twice (e.g. "MASTER" + "BEDROOM", or "BEDROOM" sitting next
   to "BEDROOM 1").  The original label text and page number are always
   preserved.
3. Geometry is only used to *associate* an already detected label with
   the enclosed drawing region around it (smallest containing vector
   path rectangle first, experimental image segmentation as a
   fallback).  Geometry alone NEVER creates a room, and raw vector path
   counts are never treated as rooms.
4. Dimensions and areas are only computed when BOTH an explicit drawing
   scale (e.g. "SCALE 1:100") is found in the PDF text AND a room region
   could be associated with the label.  Otherwise they stay ``None``
   with a human readable ``dimension_status``.  Outer/overall drawing
   dimensions never establish an individual room's area.

Honesty rules
-------------
* If a PDF has no selectable text, ``status`` is ``"no_text"`` and the
  message explains whether Tesseract OCR is missing or could not read
  the page.
* If text exists but no room label matches, ``status`` is ``"no_labels"``
  and the message says room classification could not be reliably
  determined.  No rooms are invented.
* Image-based region detection is experimental; it is reported in
  diagnostics and is never claimed to be reliable.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a declared dependency
    np = None  # type: ignore[assignment]

try:
    import pymupdf as fitz  # PyMuPDF (current import name)
except ImportError:  # older PyMuPDF builds
    import fitz  # type: ignore[no-redef]

from extractor import (
    OCR_ROOM_TEXT_HINT,
    POINTS_PER_INCH,
    is_ocr_available,
    open_document,
    read_page_text,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ROOM_TYPE_LABELS: Dict[str, str] = {
    "bedroom": "Bedroom",
    "kitchen": "Kitchen",
    "living_room": "Living room",
    "bathroom": "Bathroom",
    "dining_room": "Dining room",
    "study": "Study",
    "store": "Store",
    "corridor": "Corridor",
    "balcony": "Balcony",
    "other": "Other",
}

# Types that roll up into the "Other rooms" dashboard bucket.
OTHER_TYPES = {"dining_room", "study", "store", "corridor", "balcony", "other"}

# Explicit, full-word room terms, ordered by priority (first match wins
# the primary type; every match is recorded so combined labels such as
# "LIVING / DINING" can be flagged instead of double counted).
EXPLICIT_RULES: List[Tuple[str, str]] = [
    ("bedroom", r"\bMASTER\s*BED\s*ROOM|\bBED\s*ROOMS?\b|\bBEDROOMS?\b"),
    ("kitchen", r"\bKITCHENETTES?\b|\bKITCHENS?\b"),
    ("living_room", r"\bLIVING\s*ROOMS?\b|\bLOUNGE\b|\bLOUNGING\s*ROOM\b|\bFAMILY\s*ROOM\b|\bLIVING\b"),
    ("bathroom", r"\bBATH\s*ROOMS?\b|\bTOILETS?\b|\bWC\b|\bWASHROOMS?\b|\bPOWDER\s*ROOM\b|\bSHOWER\s*ROOM\b"),
    ("dining_room", r"\bDINING\s*(?:ROOM|AREA|HALL)?\b"),
    ("study", r"\bSTUD(?:Y|Y\s*ROOM)\b"),
    ("store", r"\bSTOR(?:E|E\s*ROOM|AGE)\b"),
    ("corridor", r"\bCORRIDORS?\b|\bHALLWAYS?\b|\bPASSAGES?\b"),
    ("balcony", r"\bBALCON(?:Y|IES)\b|\bTERRACE\b|\bPORCH\b"),
]

# Abbreviations / partial terms.  Matched rooms are reported as
# "inferred" detections (lower confidence), never as explicit ones.
INFERRED_RULES: List[Tuple[str, str]] = [
    ("bedroom", r"\bBED\s*RM\b|\bBDRM\b|\bB\.?\s*R\.?\b|\bBED\b"),
    ("kitchen", r"\bKIT\b|\bKITTN\b"),
    ("living_room", r"\bLIV\s*RM\b|\bLIV\b"),
    ("bathroom", r"\bBATH\b|\bPWDR\b|\bW\.C\.\b"),
    ("dining_room", r"\bDIN\b"),
    ("study", r"\bSTY\b"),
    ("store", r"\bSTOR\b"),
    ("corridor", r"\bCORR\b|\bHALL\b"),
    ("balcony", r"\bBALC\b"),
]

# Lines that mention rooms but are not room labels (schedules, legends...).
NON_LABEL_KEYWORDS = (
    "SCHEDULE",
    "LEGEND",
    "INDEX",
    "NOTES",
    "NOTE:",
    "TYPICAL",
    "TITLE",
    "DRAWN BY",
    "CHECKED",
    "TOTAL INDOOR AREA",
    "TOTAL AREA",
    "PORCH AREA",
    "AREA (M",
    "SIZE (M)",
    "NO.",
)


def _looks_like_schedule_row(text: str) -> bool:
    """Reject room-schedule rows (e.g. '1 Living Room 4.0 x 3.5 14.0')."""
    norm = normalize_label(text)
    if not norm:
        return False

    room_keywords = (
        "BEDROOM",
        "KITCHEN",
        "LIVING",
        "DINING",
        "TOILET",
        "BATHROOM",
        "PORCH",
        "STUDY",
        "STORE",
        "CORRIDOR",
        "BALCONY",
        "TERRACE",
    )

    # Table rows commonly follow a pattern like:
    #   1 LIVING ROOM 4.0 x 3.5 14.0
    #   LIVING ROOM 4.0 x 3.5 14.0
    #   Bedroom 2 3.0 x 3.0 9.0
    # A room label with a printed dimension inside it (e.g. "BED ROOM
    # 3.50 x 3.00") is NOT a schedule row - it is the drawing label this
    # tool is meant to read.  Only suppress rows that add an extra numeric
    # column (an area/count) or start with a numeric row ID.
    has_room_keyword = any(keyword in norm for keyword in room_keywords)
    dimension_match = re.search(
        r"\d+(?:[.,]\d+)?\s*[xX×*k]\s*\d+(?:[.,]\d+)?", norm
    )
    has_area_value = bool(
        re.search(r"\d+(?:[.,]\d+)?\s*(?:M2|M\^2|M²|SQM|SQ\.?\s*M|SQFT|FT2)\b", norm)
    )

    if has_room_keyword and dimension_match:
        remainder = norm[: dimension_match.start()] + " " + norm[dimension_match.end() :]
        extra_numbers = re.findall(r"\d+(?:[.,]\d+)?", remainder)
        if has_area_value or len(extra_numbers) >= 2:
            return True

    # A row that starts with a numeric ID and room name is also schedule data.
    if re.match(r"^\d+\s+(?:MASTER\s+)?[A-Z0-9\s/.-]+$", norm):
        if has_room_keyword and dimension_match:
            return True

    # Table headers and summary rows are not room labels.  "ROOM" is
    # deliberately excluded here because genuine labels such as
    # "BED ROOM 3.50 x 3.00" also contain that word.
    if re.search(r"\b(?:NO\.|SIZE|AREA|TOTAL)\b", norm) and re.search(r"\d", norm):
        return True

    return False

DOOR_RULE = r"\bDOORS?\b"
WINDOW_RULE = r"\bWINDOWS?\b"
NON_OPENING_KEYWORDS = ("SCHEDULE", "LEGEND", "INDEX", "NOTES")

# CAD plans commonly tag openings as D1/D2/W1/W2 and list them in a
# schedule of openings instead of writing "DOOR"/"WINDOW" on the plan.
OPENING_TAG_RULE = re.compile(
    r"(?<![\w.])([DW])\s*[-#]?\s*([0-9OoIl|]{1,2})(?![\w])", re.IGNORECASE
)
# A standalone tag line such as "D2" or "W1".  Schedule rows and room
# labels are longer, so this keeps plan-tag counting precise.
OPENING_TAG_LINE_RULE = re.compile(
    r"[DW][-#]?\s*[0-9OoIl|]{1,2}", re.IGNORECASE
)
# OCR frequently confuses 1/0 with these characters inside small tags.
_OPENING_DIGIT_TRANSLATION = str.maketrans(
    {"O": "0", "o": "0", "I": "1", "i": "1", "l": "1", "|": "1"}
)


def _canonical_opening_tag(letter: str, number: str) -> Optional[str]:
    digits = number.strip().translate(_OPENING_DIGIT_TRANSLATION)
    if not digits.isdigit():
        return None
    return f"{letter.upper()}{int(digits)}"

SCALE_RULE = re.compile(r"\bSCALE\b[^0-9]{0,12}1\s*[:/]\s*(\d+)", re.IGNORECASE)
PRINTED_DIMENSION_RULE = re.compile(
    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*[x×k*]\s*"
    r"(\d+(?:[.,]\d+)?)\s*(mm|cm|m)\b",
    re.IGNORECASE,
)
# Many CAD exports print room sizes without a unit ("3.50 x 3.00") because
# the drawing uses metres implicitly.  This pattern is only used after the
# unit-bearing patterns fail.
PRINTED_BARE_DIMENSION_RULE = re.compile(
    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*[x×k*]\s*(\d+(?:[.,]\d+)?)(?![\d.])",
    re.IGNORECASE,
)
IMPERIAL_MEASUREMENT = (
    r"(\d+(?:[.,]\d+)?)\s*(?:['′’]|FT|FEET)\s*[-–]?\s*"
    r"(?:(\d+(?:[.,]\d+)?)\s*(?:[\"″”]|IN(?:CH(?:ES)?)?))?"
)
PRINTED_IMPERIAL_DIMENSION_RULE = re.compile(
    rf"(?<![\d.]){IMPERIAL_MEASUREMENT}\s*[x×]\s*"
    rf"{IMPERIAL_MEASUREMENT}",
    re.IGNORECASE,
)
# Low-resolution scans often lose the prime/double-prime marks, leaving
# AutoCAD feet-inches as "8-7 x 7-3".  Only used when the marked patterns
# above fail, and only when both inch parts are plausible (< 12).
PRINTED_HYPHEN_IMPERIAL_RULE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*[-–]\s*(\d{1,2})\s*[\"″”]?\s*[x×]\s*"
    r"(\d{1,2})\s*[-–]\s*(\d{1,2})\s*[\"″”]?",
    re.IGNORECASE,
)

# Label-to-label merge thresholds in PDF points.  Two labels of the same
# type on the same page are considered the same room when their
# bounding boxes are this close together.
MERGE_GAP_X = 30.0
MERGE_GAP_Y = 16.0

# Page rendering budget for the experimental image region detector.
IMAGE_REGION_MAX_DIM_PX = 800
IMAGE_REGION_MIN_AREA_FRAC = 0.0008
MAX_IMAGE_ANALYSIS_PAGES = 10
INK_THRESHOLD = 200


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def normalize_label(text: str) -> str:
    """Uppercase, collapse whitespace and tidy punctuation."""
    cleaned = text.upper()
    cleaned = re.sub(r"[–—]", "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" \t\n\r-–—·•|")


def _is_non_label(norm: str, keywords: Sequence[str]) -> bool:
    return any(keyword in norm for keyword in keywords)


def classify_label(text: str) -> Optional[Dict[str, Any]]:
    """Classify a single text line as a room label.

    Returns None when the line is not a room label (this is what keeps
    arbitrary lines / vector paths from being counted as rooms).
    """
    norm = normalize_label(text)
    if _looks_like_schedule_row(text) or _looks_like_room_summary(text):
        return None
    if not norm or _is_non_label(norm, NON_LABEL_KEYWORDS):
        return None

    explicit_matches = [
        room_type
        for room_type, pattern in EXPLICIT_RULES
        if re.search(pattern, norm)
    ]
    if explicit_matches:
        return {
            "room_type": explicit_matches[0],
            "detection": "explicit",
            "all_types": explicit_matches,
            "normalized": norm,
        }

    inferred_matches = [
        room_type
        for room_type, pattern in INFERRED_RULES
        if re.search(pattern, norm)
    ]
    if inferred_matches:
        return {
            "room_type": inferred_matches[0],
            "detection": "inferred",
            "all_types": inferred_matches,
            "normalized": norm,
        }
    return None


DIMENSION_VALUE_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:[.,]\d+)?\s*(?:MM|CM|M|FT|IN)?\s*[xX×]\s*"
    r"\d+(?:[.,]\d+)?\s*(?:MM|CM|M|FT|IN)?"
    r"|\d+(?:[.,]\d+)?\s*(?:M2|M²|SQM|SQ\.?\s*M|SQFT|FT2)\b"
    r"|\d+(?:[.,]\d+)?"
    r")\s*$",
    re.IGNORECASE,
)

SCHEDULE_HEADER_KEYWORDS = ("SCHEDULE", "LEGEND", "INDEX")
SCHEDULE_ROW_MAX_GAP = 34.0


def _looks_like_dimension_value(text: str) -> bool:
    """True for standalone dimension/area cells such as '3.0 x 3.0 m'."""
    return bool(DIMENSION_VALUE_RE.match(normalize_label(text)))


def _looks_like_room_summary(text: str) -> bool:
    """Reject summary lines that list several room types with counts.

    Example: 'TEST TARGET: 3 BEDROOMS, 1 KITCHEN, 1 LIVING, 2 BATHROOMS'.
    A genuine combined label (e.g. 'LIVING / DINING') has no counts.
    """
    norm = normalize_label(text)
    if not re.search(r"\d", norm):
        return False
    types = {
        room_type
        for room_type, pattern in EXPLICIT_RULES + INFERRED_RULES
        if re.search(pattern, norm)
    }
    if len(types) < 2:
        return False
    # A combined label that only carries a printed size (e.g.
    # "LIVING / DINING 6.00 x 3.00") is a real label, not a summary.
    dimension_match = re.search(
        r"\d+(?:[.,]\d+)?\s*[xX×*k]\s*\d+(?:[.,]\d+)?", norm
    )
    if dimension_match:
        remainder = norm[: dimension_match.start()] + " " + norm[dimension_match.end() :]
        if not re.search(r"\d", remainder):
            return False
    return True


def _bbox_center(bbox: Sequence[float]) -> Tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _center_inside(
    bbox: Sequence[float], region: Sequence[float], margin: float = 2.0
) -> bool:
    cx, cy = _bbox_center(bbox)
    return (
        region[0] - margin <= cx <= region[2] + margin
        and region[1] - margin <= cy <= region[3] + margin
    )


def _schedule_regions(
    lines: List[Dict[str, Any]], page_width: float
) -> List[Tuple[float, float, float, float]]:
    """Find table/legend regions so schedule rows are not read as rooms.

    A region starts at a 'SCHEDULE' / 'LEGEND' / 'INDEX' header and grows
    downwards through the contiguous rows in the same column band.  Room
    names inside that region are schedule metadata, not drawing labels.
    """
    regions: List[Tuple[float, float, float, float]] = []
    for header in lines:
        norm = normalize_label(header["text"])
        keywords = [
            keyword
            for keyword in SCHEDULE_HEADER_KEYWORDS
            if keyword in norm
        ]
        if not keywords:
            continue
        header_bbox = list(header["bbox"])
        words = header.get("words") or []
        if words:
            header_word_indexes = [
                index
                for index, word in enumerate(words)
                if any(
                    keyword in normalize_label(word["text"])
                    for keyword in keywords
                )
            ]
            if not header_word_indexes:
                continue
            first_header_word = min(header_word_indexes)
            header_words = words[first_header_word:]
            header_bbox = [
                min(word["bbox"][0] for word in header_words),
                min(word["bbox"][1] for word in header_words),
                max(word["bbox"][2] for word in header_words),
                max(word["bbox"][3] for word in header_words),
            ]
        hx0, hy0, hx1, hy1 = header_bbox
        band = [
            line
            for line in lines
            if line["bbox"][0] >= hx0 - 12.0
            and line["bbox"][2] <= page_width + 1.0
        ]
        band.sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))
        region = [hx0, hy0, hx1, hy1]
        bottom = hy1
        for line in band:
            lx0, ly0, lx1, ly1 = line["bbox"]
            if ly0 < hy0 - 1.0:
                continue
            if ly0 - bottom > SCHEDULE_ROW_MAX_GAP:
                break
            region[0] = min(region[0], lx0)
            region[1] = min(region[1], ly0)
            region[2] = max(region[2], lx1)
            region[3] = max(region[3], ly1)
            bottom = max(bottom, ly1)
        regions.append((region[0], region[1], region[2], region[3]))
    return regions


def classify_opening(text: str) -> Optional[str]:
    """Return 'door', 'window' or None for a text line."""
    norm = normalize_label(text)
    if not norm or _is_non_label(norm, NON_OPENING_KEYWORDS):
        return None
    if re.search(DOOR_RULE, norm):
        return "door"
    if re.search(WINDOW_RULE, norm):
        return "window"
    return None


def _bbox_gap(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float]:
    """Horizontal / vertical gap between two bounding boxes (0 = touching)."""
    gap_x = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    gap_y = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return gap_x, gap_y


def _opening_kind_from_tag(letter: str) -> str:
    return "door" if letter.upper() == "D" else "window"


def _dedupe_openings(
    candidates: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], int]:
    """Merge openings of the same kind whose boxes nearly overlap."""
    kept: List[Dict[str, Any]] = []
    merged = 0
    for candidate in candidates:
        hit = None
        for existing in kept:
            if existing["kind"] != candidate["kind"]:
                continue
            if existing["page"] != candidate["page"]:
                continue
            gap_x, gap_y = _bbox_gap(existing["bbox"], candidate["bbox"])
            if gap_x <= MERGE_GAP_X and gap_y <= MERGE_GAP_Y:
                hit = existing
                break
        if hit is None:
            kept.append(candidate)
            continue
        merged += 1
        hit["merged_labels"] = list(
            dict.fromkeys(
                hit.get("merged_labels", [hit["label"]]) + [candidate["label"]]
            )
        )
    return kept, merged


def _standalone_opening_tags(
    lines: List[Dict[str, Any]], page_number: int
) -> List[Dict[str, Any]]:
    """Find plan tags such as 'D2' or 'W1' that sit on the drawing."""
    tags: List[Dict[str, Any]] = []
    for line in lines:
        norm = normalize_label(line["text"]).strip(" .,:;")
        if not OPENING_TAG_LINE_RULE.fullmatch(norm):
            continue
        number = norm[1:].lstrip("-#").strip()
        canonical = _canonical_opening_tag(norm[0], number)
        if canonical is None:
            continue
        tags.append(
            {
                "kind": _opening_kind_from_tag(canonical[0]),
                "label": canonical,
                "page": page_number,
                "bbox": list(line["bbox"]),
            }
        )
    return tags


def _schedule_opening_ids(
    lines: List[Dict[str, Any]],
    schedule_regions: List[Tuple[float, float, float, float]],
    page_number: int,
) -> List[Tuple[int, str, str]]:
    """Collect Dn/Wn IDs listed in a schedule-of-openings table."""
    found: List[Tuple[int, str, str]] = []
    for line in lines:
        if not any(_center_inside(line["bbox"], region) for region in schedule_regions):
            continue
        norm = normalize_label(line["text"])
        if not (re.search(DOOR_RULE, norm) or re.search(WINDOW_RULE, norm)
                or OPENING_TAG_RULE.search(norm)):
            continue
        for letter, number in OPENING_TAG_RULE.findall(norm):
            canonical = _canonical_opening_tag(letter, number)
            if canonical is not None:
                found.append((page_number, canonical[0], canonical[1:]))
    return found


def _join_labels(first: str, second: str) -> str:
    """Join two related label strings without repetition."""
    a = normalize_label(first)
    b = normalize_label(second)
    if not a:
        return second
    if not b:
        return first
    if a == b:
        return first
    if a in b:
        return second
    if b in a:
        return first
    return f"{first} {second}"


# ---------------------------------------------------------------------------
# Page reading
# ---------------------------------------------------------------------------

def _expand_label(
    candidate: Dict[str, Any], lines: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Append adjacent text lines (wrapped labels like MASTER / BEDROOM)."""
    best_text = candidate["label"]
    best_bbox = list(candidate["bbox"])
    for line in lines:
        if line is candidate.get("_line"):
            continue
        gap_x, gap_y = _bbox_gap(candidate["bbox"], line["bbox"])
        if gap_x > 24.0 or gap_y > 14.0:
            continue
        # Dimensions and area cells sit right next to a label but are not
        # part of its name.
        if _looks_like_dimension_value(line["text"]):
            continue
        joined = _join_labels(best_text, line["text"])
        reclassified = classify_label(joined)
        # Only absorb the neighbour when the combined text still means
        # the same room type - never merge two different rooms.
        if reclassified and reclassified["room_type"] == candidate["room_type"]:
            best_text = joined
            x0 = min(best_bbox[0], line["bbox"][0])
            y0 = min(best_bbox[1], line["bbox"][1])
            x1 = max(best_bbox[2], line["bbox"][2])
            y1 = max(best_bbox[3], line["bbox"][3])
            best_bbox = [x0, y0, x1, y1]
    candidate["label"] = normalize_label(best_text)
    candidate["bbox"] = best_bbox
    return candidate


def _merge_duplicates(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Merge nearby same-type labels so a room is not double counted."""
    merged: List[Dict[str, Any]] = []
    merged_count = 0
    for candidate in candidates:
        target = None
        for existing in merged:
            if existing["room_type"] != candidate["room_type"]:
                continue
            if existing["page"] != candidate["page"]:
                continue
            gap_x, gap_y = _bbox_gap(existing["bbox"], candidate["bbox"])
            if gap_x <= MERGE_GAP_X and gap_y <= MERGE_GAP_Y:
                target = existing
                break
        if target is None:
            candidate["merged_labels"] = [candidate["label"]]
            merged.append(candidate)
            continue
        merged_count += 1
        joined = _join_labels(target["label"], candidate["label"])
        if joined != target["label"]:
            target["merged_labels"] = list(
                dict.fromkeys(target.get("merged_labels", [target["label"]])
                              + [candidate["label"]])
            )
            x0 = min(target["bbox"][0], candidate["bbox"][0])
            y0 = min(target["bbox"][1], candidate["bbox"][1])
            x1 = max(target["bbox"][2], candidate["bbox"][2])
            y1 = max(target["bbox"][3], candidate["bbox"][3])
            target["bbox"] = [x0, y0, x1, y1]
            target["label"] = normalize_label(joined)
        else:
            target["merged_labels"] = list(
                dict.fromkeys(
                    target.get("merged_labels", [target["label"]])
                    + candidate.get("merged_labels", [candidate["label"]])
                )
            )
        # An explicit detection next to an inferred one keeps "explicit".
        if candidate["detection"] == "explicit":
            target["detection"] = "explicit"
        if candidate["all_types"] != target["all_types"]:
            target["all_types"] = list(
                dict.fromkeys(target["all_types"] + candidate["all_types"])
            )
    return merged, merged_count


# ---------------------------------------------------------------------------
# Geometry: vector regions
# ---------------------------------------------------------------------------

def _page_drawings(page: Any, cache: Dict[int, List[Any]]) -> List[Any]:
    key = int(page.number)
    if key not in cache:
        try:
            cache[key] = page.get_drawings()
        except Exception:
            cache[key] = []
    return cache[key]


def _vector_region(
    page: Any, bbox: Sequence[float], drawings_cache: Dict[int, List[Any]]
) -> Optional[Dict[str, Any]]:
    """Smallest vector path rectangle that encloses the label."""
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    best: Optional[Any] = None
    best_area = 0.0
    for drawing in _page_drawings(page, drawings_cache):
        rect = drawing.get("rect")
        if rect is None:
            continue
        try:
            contains = (
                rect.x0 <= cx <= rect.x1
                and rect.y0 <= cy <= rect.y1
                and rect.x0 <= bbox[0] and rect.y0 <= bbox[1]
                and rect.x1 >= bbox[2] and rect.y1 >= bbox[3]
            )
        except Exception:
            continue
        if not contains:
            continue
        area = max(1.0, float(rect.width) * float(rect.height))
        # Ignore the page border / full-page frame.
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        if area >= 0.9 * page_area:
            continue
        if area < 400.0:
            continue
        if best is None or area < best_area:
            best = rect
            best_area = area
    if best is None:
        return None
    return {
        "source": "vector_rect",
        "bbox": [
            round(float(best.x0), 2),
            round(float(best.y0), 2),
            round(float(best.x1), 2),
            round(float(best.y1), 2),
        ],
    }


# ---------------------------------------------------------------------------
# Geometry: experimental image regions (numpy only)
# ---------------------------------------------------------------------------

def _shift(mask: Any, dy: int, dx: int, fill: bool = False) -> Any:
    height, width = mask.shape
    out = np.full_like(mask, fill)
    ys0, ys1 = max(0, dy), min(height, height + dy)
    xs0, xs1 = max(0, dx), min(width, width + dx)
    if ys0 < ys1 and xs0 < xs1:
        out[ys0:ys1, xs0:xs1] = mask[ys0 - dy : ys1 - dy, xs0 - dx : xs1 - dx]
    return out


def _dilate(mask: Any) -> Any:
    result = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            result |= _shift(mask, dy, dx)
    return result


def _erode(mask: Any) -> Any:
    result = mask.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            result &= _shift(mask, dy, dx, fill=True)
    return result


def _flood_from_border(open_mask: Any) -> Any:
    """Mark every open (white) pixel reachable from the image border."""
    height, _width = open_mask.shape
    reachable = np.zeros_like(open_mask)
    reachable[0, :] = open_mask[0, :]
    reachable[-1, :] = open_mask[-1, :]
    reachable[:, 0] |= open_mask[:, 0]
    reachable[:, -1] |= open_mask[:, -1]
    max_iterations = open_mask.shape[0] + open_mask.shape[1] + 4
    for _ in range(max_iterations):
        grown = (
            reachable
            | _shift(reachable, 1, 0)
            | _shift(reachable, -1, 0)
            | _shift(reachable, 0, 1)
            | _shift(reachable, 0, -1)
        ) & open_mask
        if np.array_equal(grown, reachable):
            return reachable
        reachable = grown
    return reachable


def _connected_components(mask: Any, min_area: int) -> Tuple[Any, List[Dict[str, Any]]]:
    """Label enclosed open regions; returns (label_map, regions)."""
    labels = np.zeros(mask.shape, dtype=np.int32)
    regions: List[Dict[str, Any]] = []
    ys, xs = np.nonzero(mask)
    current = 0
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if labels[y0, x0]:
            continue
        current += 1
        queue = deque([(y0, x0)])
        labels[y0, x0] = current
        area = 0
        miny = maxy = y0
        minx = maxx = x0
        while queue:
            y, x = queue.popleft()
            area += 1
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if ny < 0 or nx < 0 or ny >= labels.shape[0] or nx >= labels.shape[1]:
                    continue
                if mask[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = current
                    queue.append((ny, nx))
        if area < min_area:
            labels[labels == current] = 0
            continue
        regions.append(
            {
                "id": current,
                "pixel_bbox": [minx, miny, maxx + 1, maxy + 1],
                "pixel_area": area,
            }
        )
    # Compact remaining ids.
    kept = [region["id"] for region in regions]
    remap = {old: index + 1 for index, old in enumerate(kept)}
    if kept:
        compact = np.zeros_like(labels)
        for old, new in remap.items():
            compact[labels == old] = new
        labels = compact
        for region in regions:
            region["id"] = remap[region["id"]]
    else:
        labels = np.zeros_like(labels)
    return labels, regions


def _render_gray(page: Any) -> Optional[Tuple[Any, int, int]]:
    if np is None:
        return None
    rect = page.rect
    longest = max(float(rect.width), float(rect.height))
    if longest <= 0:
        return None
    zoom = IMAGE_REGION_MAX_DIM_PX / longest
    zoom = max(0.1, min(4.0, zoom))
    try:
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False
        )
    except Exception:
        return None
    arr = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width
    )
    return arr, pixmap.width, pixmap.height


def _image_regions(page: Any) -> Tuple[List[Dict[str, Any]], Any, int, int, Dict[str, int]]:
    """Experimental: segment enclosed room regions from a rendered page."""
    stats = {"regions": 0, "pixels": 0}
    rendered = _render_gray(page)
    if rendered is None:
        return [], None, 0, 0, stats
    gray, width, height = rendered
    stats["pixels"] = int(width * height)
    ink = gray < INK_THRESHOLD
    # Morphological closing seals anti-alias gaps so walls read as solid.
    closed = _dilate(ink)
    closed = _erode(closed)
    open_mask = ~closed
    reachable = _flood_from_border(open_mask)
    enclosed = open_mask & ~reachable
    min_area = max(64, int(IMAGE_REGION_MIN_AREA_FRAC * width * height))
    labels, regions = _connected_components(enclosed, min_area)
    stats["regions"] = len(regions)
    return regions, labels, width, height, stats


def _pixel_to_points(
    pixel_bbox: Sequence[int], page_rect: Any, width: int, height: int
) -> List[float]:
    sx = float(page_rect.width) / max(1, width)
    sy = float(page_rect.height) / max(1, height)
    return [
        round(float(page_rect.x0) + pixel_bbox[0] * sx, 2),
        round(float(page_rect.y0) + pixel_bbox[1] * sy, 2),
        round(float(page_rect.x0) + pixel_bbox[2] * sx, 2),
        round(float(page_rect.y0) + pixel_bbox[3] * sy, 2),
    ]


def _image_region_for_label(
    page: Any,
    bbox: Sequence[float],
    labels_map: Any,
    width: int,
    height: int,
    page_rect: Any,
) -> Optional[Dict[str, Any]]:
    if labels_map is None or width <= 0 or height <= 0:
        return None
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    px = int((cx - page_rect.x0) / max(1e-6, page_rect.width) * width)
    py = int((cy - page_rect.y0) / max(1e-6, page_rect.height) * height)
    if px < 0 or py < 0 or py >= labels_map.shape[0] or px >= labels_map.shape[1]:
        return None
    region_id = int(labels_map[py, px])
    if region_id == 0:
        return None
    ys, xs = np.nonzero(labels_map == region_id)
    if len(xs) == 0:
        return None
    pixel_bbox = [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]
    return {
        "source": "image_region_experimental",
        "bbox": _pixel_to_points(pixel_bbox, page_rect, width, height),
    }


# ---------------------------------------------------------------------------
# Scale detection
# ---------------------------------------------------------------------------

def _find_scale(page_texts: Dict[int, str]) -> Optional[int]:
    """Return the denominator of an explicit "SCALE 1:N" note, if any."""
    for text in page_texts.values():
        match = SCALE_RULE.search(text or "")
        if match:
            try:
                ratio = int(match.group(1))
            except ValueError:
                continue
            if 1 <= ratio <= 10000:
                return ratio
    return None


def _parse_printed_dimension(text: str) -> Optional[Dict[str, Any]]:
    """Parse an explicit dimension pair from a drawing text line.

    Supports unit-suffixed metric ("4.50 x 2.10 m"), imperial
    ("11'6\" x 10'6\"") and unit-less CAD room sizes ("3.50 x 3.00"),
    which are interpreted as metres (small values) or millimetres
    (large values).
    """
    match = PRINTED_DIMENSION_RULE.search(text)
    if match:
        first, second, unit = match.groups()
        unit = unit.lower()
        millimeters_per_unit = {"mm": 1.0, "cm": 10.0, "m": 1000.0}[unit]
        width_mm = float(first.replace(",", ".")) * millimeters_per_unit
        height_mm = float(second.replace(",", ".")) * millimeters_per_unit
        display_text = f"{first} × {second} {unit}"
    else:
        match = PRINTED_IMPERIAL_DIMENSION_RULE.search(text)
        if match:
            first_feet, first_inches, second_feet, second_inches = match.groups()

            def inches(feet: str, inch_part: Optional[str]) -> float:
                whole_inches = float(feet.replace(",", ".")) * 12
                if inch_part:
                    whole_inches += float(inch_part.replace(",", "."))
                return whole_inches

            width_mm = inches(first_feet, first_inches) * 25.4
            height_mm = inches(second_feet, second_inches) * 25.4

            def format_measurement(feet: str, inch_part: Optional[str]) -> str:
                if inch_part is None:
                    return f"{feet}'"
                return f'{feet}\'{inch_part}"'

            display_text = (
                f"{format_measurement(first_feet, first_inches)} × "
                f"{format_measurement(second_feet, second_inches)}"
            )
        else:
            match = PRINTED_HYPHEN_IMPERIAL_RULE.search(text)
            if match:
                f1, i1, f2, i2 = match.groups()
                if float(i1) >= 12.0 or float(i2) >= 12.0:
                    return None
                width_mm = (float(f1) * 12.0 + float(i1)) * 25.4
                height_mm = (float(f2) * 12.0 + float(i2)) * 25.4
                display_text = f"{f1}'{i1}\" × {f2}'{i2}\""
                smallest = min(width_mm, height_mm)
                largest_mm = max(width_mm, height_mm)
                if (
                    smallest < 400.0
                    or largest_mm > 30000.0
                    or largest_mm / max(1.0, smallest) > 15.0
                ):
                    return None
            else:
                match = PRINTED_BARE_DIMENSION_RULE.search(text)
                if not match:
                    return None
                first, second = match.groups()
                width = float(first.replace(",", "."))
                height = float(second.replace(",", "."))
                largest = max(width, height)
                # Choose the unit that keeps the size plausible for a room:
                # small decimals are metres (3.50), three-digit values are
                # centimetres (350), and large values are millimetres (3000).
                if largest < 100.0:
                    width_mm = width * 1000.0
                    height_mm = height * 1000.0
                    display_text = f"{first} × {second} m"
                elif largest < 1000.0:
                    width_mm = width * 10.0
                    height_mm = height * 10.0
                    display_text = f"{first} × {second} cm"
                else:
                    width_mm = width
                    height_mm = height
                    display_text = f"{first} × {second} mm"

                # Guard against OCR-degraded numbers (for example "80 x 3.00")
                # becoming implausible room sizes.
                smallest = min(width_mm, height_mm)
                largest_mm = max(width_mm, height_mm)
                if (
                    smallest < 400.0
                    or largest_mm > 30000.0
                    or largest_mm / max(1.0, smallest) > 15.0
                ):
                    return None

    width_mm = round(width_mm, 1)
    height_mm = round(height_mm, 1)
    return {
        "text": display_text,
        "width_mm": width_mm,
        "height_mm": height_mm,
        "area_m2": round((width_mm / 1000.0) * (height_mm / 1000.0), 2),
    }


def _clean_ocr_room_label(
    text: str, room_type: str, all_types: Sequence[str]
) -> str:
    """Keep the recognized room name, not nearby OCR noise or dimensions."""
    patterns = {
        "bedroom": r"\b(?:MASTER\s*)?BED\s*ROOMS?\b|\bBEDROOMS?\b",
        "kitchen": r"\bKITCHENETTES?\b|\bKITCHENS?\b",
        "living_room": (
            r"\bLIVING\s*ROOMS?\b|\bLOUNGE\b|\bFAMILY\s*ROOM\b|\bLIVING\b"
        ),
        "bathroom": (
            r"\bBATH\s*ROOMS?\b|\bTOILETS?\b|\bWC\b|\bWASHROOMS?\b|"
            r"\bPOWDER\s*ROOM\b|\bSHOWER\s*ROOM\b"
        ),
        "dining_room": r"\bDINING\s*(?:ROOM|AREA|HALL)?\b",
        "study": r"\bSTUDY(?:\s*ROOM)?\b",
        "store": r"\bSTOR(?:E|AGE)(?:\s*ROOM)?\b",
        "corridor": r"\bCORRIDORS?\b|\bHALLWAYS?\b|\bPASSAGES?\b|\bHALL\b",
        "balcony": r"\bBALCON(?:Y|IES)\b|\bTERRACE\b|\bPORCH\b",
    }
    if len(all_types) > 1:
        return normalize_label(text)

    match = re.search(patterns.get(room_type, r"$^"), text, re.IGNORECASE)
    if not match:
        return ROOM_TYPE_LABELS.get(room_type, normalize_label(text)).upper()
    label = normalize_label(match.group(0))
    suffix = re.match(r"\s*[#-]?\s*(\d+)\b", text[match.end() :])
    return f"{label} {suffix.group(1)}" if suffix else label


def _split_ocr_room_line(line: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Split OCR lines that contain separate labels placed side by side."""
    words = line.get("words")
    if not words or _parse_printed_dimension(line["text"]):
        return [line]

    groups: List[List[Dict[str, Any]]] = [[]]
    for word in sorted(words, key=lambda item: item["bbox"][0]):
        if groups[-1]:
            previous = groups[-1][-1]
            gap = word["bbox"][0] - previous["bbox"][2]
            previous_height = previous["bbox"][3] - previous["bbox"][1]
            word_height = word["bbox"][3] - word["bbox"][1]
            if gap > max(12.0, max(previous_height, word_height) * 1.5):
                groups.append([])
        groups[-1].append(word)

    segments = []
    for group in groups:
        starts = [
            index
            for index, word in enumerate(group)
            if re.match(
                r"^(?:BED|BEDROOM|BATH|BATHROOM|KITCHEN|KIT|LIVING|LOUNGE|"
                r"FAMILY|DINING|DIN|STUDY|STUD|STORE|STOR|CORRIDOR|CORR|"
                r"HALL|BALCONY|BALC|TERRACE|PORCH|TOILET|WC)\b",
                word["text"].upper().strip(".,:;|"),
            )
        ]
        if len(starts) > 1 and "/" not in line["text"] and "&" not in line["text"]:
            group_segments = [
                group[start : (starts[index + 1] if index + 1 < len(starts) else len(group))]
                for index, start in enumerate(starts)
            ]
        else:
            group_segments = [group]

        for word_group in group_segments:
            segment = {
                "text": " ".join(word["text"] for word in word_group),
                "bbox": [
                    min(word["bbox"][0] for word in word_group),
                    min(word["bbox"][1] for word in word_group),
                    max(word["bbox"][2] for word in word_group),
                    max(word["bbox"][3] for word in word_group),
                ],
            }
            if classify_label(segment["text"]):
                segments.append(segment)
    return segments or [line]


def _attach_printed_dimensions(
    rooms: List[Dict[str, Any]], dimensions: List[Dict[str, Any]]
) -> None:
    """Attach each printed dimension to its nearest room label below it."""
    assigned_rooms = set()
    for dimension in sorted(
        dimensions,
        key=lambda item: (item["page"], item["bbox"][1], item["bbox"][0]),
    ):
        best_match = None
        best_score = float("inf")
        dimension_bbox = dimension["bbox"]
        dimension_center_x = (dimension_bbox[0] + dimension_bbox[2]) / 2.0
        for index, room in enumerate(rooms):
            if index in assigned_rooms or room["page"] != dimension["page"]:
                continue
            room_bbox = room["bbox"]
            if dimension_bbox[1] < room_bbox[1]:
                continue
            gap_x, gap_y = _bbox_gap(room_bbox, dimension_bbox)
            center_delta_x = abs(
                (room_bbox[0] + room_bbox[2]) / 2.0 - dimension_center_x
            )
            if gap_x > 45.0 or gap_y > 50.0:
                continue
            score = gap_y + gap_x * 0.25 + center_delta_x * 0.1
            if score < best_score:
                best_match = index
                best_score = score
        if best_match is not None:
            rooms[best_match]["_printed_dimension"] = dimension["value"]
            assigned_rooms.add(best_match)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_rooms(
    data: bytes,
    password: str = "",
    use_image_regions: bool = True,
    extracted_pages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Detect and classify room labels in a PDF floor plan.

    Returns a JSON-serialisable analysis dictionary.  Never invents
    rooms, dimensions or areas: every number comes from readable text
    or from an explicitly scaled geometry calculation.
    """
    started = time.perf_counter()
    doc = open_document(data, password)
    try:
        return _analyze_document(
            doc,
            use_image_regions=use_image_regions,
            started=started,
            extracted_pages=extracted_pages,
        )
    finally:
        doc.close()


def _analyze_document(
    doc: Any,
    use_image_regions: bool,
    started: float,
    extracted_pages: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    page_count = doc.page_count
    page_lines: Dict[int, List[Dict[str, Any]]] = {}
    page_texts: Dict[int, str] = {}
    total_text_chars = 0
    pages_with_images = 0
    image_page_numbers = set()
    ocr_available = is_ocr_available()
    pages_with_ocr_text = 0
    pages_ocr_attempted = 0
    room_candidates: List[Dict[str, Any]] = []
    dimension_candidates: List[Dict[str, Any]] = []
    opening_candidates: List[Dict[str, Any]] = []
    opening_tag_candidates: List[Dict[str, Any]] = []
    schedule_opening_ids: List[Tuple[int, str, str]] = []
    text_lines_scanned = 0

    for index in range(page_count):
        page = doc.load_page(index)
        prepared_page = (
            extracted_pages[index]
            if extracted_pages is not None and index < len(extracted_pages)
            else None
        )
        if prepared_page is None:
            raw_text, text_source, lines = read_page_text(page)
        else:
            raw_text = prepared_page["text"]
            text_source = prepared_page["text_source"]
            lines = prepared_page["lines"]
        if text_source in {"ocr", "mixed"}:
            pages_with_ocr_text += 1
        if text_source in {"ocr", "ocr_empty", "mixed"}:
            pages_ocr_attempted += 1
        page_texts[index] = raw_text
        total_text_chars += len(raw_text.strip())
        try:
            if page.get_images(full=True):
                pages_with_images += 1
                image_page_numbers.add(index + 1)
        except Exception:
            pass

        page_lines[index] = lines
        text_lines_scanned += len(lines)
        schedule_regions = _schedule_regions(lines, float(page.rect.width))
        effective_lines: List[Dict[str, Any]] = []

        for line in lines:
            if (
                line.get("ocr_pass") == 6
                and line["bbox"][2] - line["bbox"][0]
                > float(page.rect.width) * 0.35
                and not OCR_ROOM_TEXT_HINT.search(line["text"])
            ):
                # Wide sparse-text lines are usually stray artwork or
                # table noise, but a wide line can still carry real room
                # names placed side by side.  Keep those for splitting.
                continue
            if any(_center_inside(line["bbox"], region) for region in schedule_regions):
                # Room names inside a schedule/legend table are metadata,
                # not labels on the drawing itself.
                continue
            line_source = line.get("text_source", text_source)
            candidate_lines = (
                _split_ocr_room_line(line) if line_source == "ocr" else [line]
            )
            effective_lines.extend(candidate_lines)
            for candidate_line in candidate_lines:
                candidate_source = candidate_line.get("text_source", line_source)
                printed_dimension = _parse_printed_dimension(candidate_line["text"])
                if printed_dimension:
                    dimension_candidates.append(
                        {
                            "page": index + 1,
                            "bbox": list(candidate_line["bbox"]),
                            "value": printed_dimension,
                        }
                    )
                classified = classify_label(candidate_line["text"])
                if classified:
                    room_candidates.append(
                        {
                            "label": (
                                _clean_ocr_room_label(
                                    candidate_line["text"],
                                    classified["room_type"],
                                    classified["all_types"],
                                )
                                if candidate_source == "ocr"
                                else normalize_label(candidate_line["text"])
                            ),
                            "page": index + 1,
                            "bbox": list(candidate_line["bbox"]),
                            "room_type": classified["room_type"],
                            "detection": classified["detection"],
                            "all_types": classified["all_types"],
                            "text_source": candidate_source,
                            "_line": candidate_line,
                        }
                    )
                opening = classify_opening(candidate_line["text"])
                if opening:
                    opening_candidates.append(
                        {
                            "kind": opening,
                            "label": normalize_label(candidate_line["text"]),
                            "page": index + 1,
                            "bbox": list(candidate_line["bbox"]),
                        }
                    )
        page_lines[index] = effective_lines
        opening_tag_candidates.extend(
            _standalone_opening_tags(effective_lines, index + 1)
        )
        schedule_opening_ids.extend(
            _schedule_opening_ids(lines, schedule_regions, index + 1)
        )

    scale_ratio = _find_scale(page_texts)
    no_text = total_text_chars == 0

    # Expand wrapped labels (MASTER / BEDROOM) before merging.
    for candidate in room_candidates:
        if candidate["text_source"] == "ocr":
            continue
        lines = page_lines.get(candidate["page"] - 1, [])
        _expand_label(candidate, lines)
    rooms_raw, merged_count = _merge_duplicates(room_candidates)
    unique_dimensions: List[Dict[str, Any]] = []
    for candidate in dimension_candidates:
        x0, y0, x1, y1 = candidate["bbox"]
        duplicate = False
        for existing in unique_dimensions:
            if candidate["page"] != existing["page"]:
                continue
            ex0, ey0, ex1, ey1 = existing["bbox"]
            intersection = max(0.0, min(x1, ex1) - max(x0, ex0)) * max(
                0.0, min(y1, ey1) - max(y0, ey0)
            )
            smaller_area = min(
                max(0.0, (x1 - x0) * (y1 - y0)),
                max(0.0, (ex1 - ex0) * (ey1 - ey0)),
            )
            if smaller_area and intersection / smaller_area >= 0.5:
                duplicate = True
                break
        if not duplicate:
            unique_dimensions.append(candidate)
    _attach_printed_dimensions(rooms_raw, unique_dimensions)

    # Opening counts, in priority order: a schedule of openings (distinct
    # IDs) is the most reliable explicit source, then tags placed on the
    # drawing (D1/W1 -> one physical opening each), then plain
    # "DOOR"/"WINDOW" text labels.
    tag_openings, tag_merged = _dedupe_openings(opening_tag_candidates)
    word_openings, word_merged = _dedupe_openings(opening_candidates)
    opening_merged = tag_merged + word_merged

    schedule_ids = {
        (page, letter, number) for page, letter, number in schedule_opening_ids
    }
    opening_summary: Dict[str, Dict[str, Any]] = {}
    for kind in ("door", "window"):
        scheduled = sorted(
            {
                f"{letter}{number}"
                for _page, letter, number in schedule_ids
                if _opening_kind_from_tag(letter) == kind
            }
        )
        if scheduled:
            opening_summary[kind] = {
                "count": len(scheduled),
                "status": "detected_from_schedule",
                "tags": scheduled,
            }
            continue
        tags = [c for c in tag_openings if c["kind"] == kind]
        if tags:
            opening_summary[kind] = {
                "count": len(tags),
                "status": "detected_from_plan_tags",
                "tags": sorted({tag["label"] for tag in tags}),
            }
            continue
        words = [c for c in word_openings if c["kind"] == kind]
        if words:
            opening_summary[kind] = {
                "count": len(words),
                "status": "detected_from_text_labels",
                "tags": sorted({word["label"] for word in words}),
            }
            continue
        opening_summary[kind] = {
            "count": None,
            "status": "not_detected",
            "tags": [],
        }

    # Geometry association, phase 1: cheap vector rectangles for every
    # detected label (geometry never creates a room, it only attaches a
    # region to an already detected label).
    label_pages = sorted({room["page"] for room in rooms_raw})
    drawings_cache: Dict[int, List[Any]] = {}
    vector_matches = 0
    built: List[Dict[str, Any]] = []
    for position, room in enumerate(rooms_raw, start=1):
        page_number = room["page"]
        page = doc.load_page(page_number - 1)
        region = _vector_region(page, room["bbox"], drawings_cache)
        if region is not None:
            vector_matches += 1
        built.append({"room": room, "page_obj": page, "position": position, "region": region})

    # Phase 2: experimental image segmentation, only on pages where at
    # least one label could not be tied to a vector rectangle, and only
    # up to MAX_IMAGE_ANALYSIS_PAGES pages.
    image_pages_used = 0
    image_region_stats = {"regions": 0, "pixels": 0}
    image_matches = 0
    if use_image_regions:
        pending_pages = sorted(
            {
                entry["room"]["page"]
                for entry in built
                if entry["region"] is None
                and entry["room"]["page"] not in image_page_numbers
            }
        )
        image_ready: Dict[int, Tuple[Any, int, int]] = {}
        for page_number in pending_pages:
            if image_pages_used >= MAX_IMAGE_ANALYSIS_PAGES:
                break
            regions, labels_map, width, height, stats = _image_regions(doc.load_page(page_number - 1))
            image_region_stats["regions"] += stats["regions"]
            image_region_stats["pixels"] += stats["pixels"]
            if regions:
                image_pages_used += 1
            image_ready[page_number] = (labels_map, width, height)
        for entry in built:
            if entry["region"] is not None:
                continue
            ready = image_ready.get(entry["room"]["page"])
            if not ready:
                continue
            labels_map, width, height = ready
            region = _image_region_for_label(
                entry["page_obj"], entry["room"]["bbox"], labels_map,
                width, height, entry["page_obj"].rect,
            )
            if region is not None:
                entry["region"] = region
                image_matches += 1

    rooms: List[Dict[str, Any]] = []
    for entry in built:
        room = entry["room"]
        region = entry["region"]
        page_number = room["page"]

        width_mm: Optional[float] = None
        height_mm: Optional[float] = None
        area_m2: Optional[float] = None
        printed_dimension = room.get("_printed_dimension")
        if printed_dimension is not None:
            width_mm = printed_dimension["width_mm"]
            height_mm = printed_dimension["height_mm"]
            area_m2 = printed_dimension["area_m2"]
            dimension_status = "read_from_drawing"
        elif region is None:
            dimension_status = "not_calculated_no_region"
        elif scale_ratio is None:
            dimension_status = "not_calculated_no_scale"
        else:
            x0, y0, x1, y1 = region["bbox"]
            width_pt = max(0.0, x1 - x0)
            height_pt = max(0.0, y1 - y0)
            width_mm = round(width_pt * 25.4 / POINTS_PER_INCH * scale_ratio, 1)
            height_mm = round(height_pt * 25.4 / POINTS_PER_INCH * scale_ratio, 1)
            area_m2 = round((width_mm / 1000.0) * (height_mm / 1000.0), 2)
            dimension_status = "calculated"

        if room["text_source"] == "ocr":
            confidence = "medium"
        elif room["detection"] == "explicit":
            confidence = "high" if region is not None else "medium"
        else:
            confidence = "low"

        rooms.append(
            {
                "id": f"p{page_number}-r{entry['position']}",
                "label": room["label"],
                "merged_labels": room.get("merged_labels", [room["label"]]),
                "room_type": room["room_type"],
                "type_label": ROOM_TYPE_LABELS.get(room["room_type"], "Other"),
                "page": page_number,
                "detection": room["detection"],
                "confidence": confidence,
                "bbox": [round(v, 2) for v in room["bbox"]],
                "region": region,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "area_m2": area_m2,
                "printed_dimensions": (
                    printed_dimension["text"] if printed_dimension else None
                ),
                "dimension_status": dimension_status,
                "combined_label": len(room.get("all_types", [])) > 1,
                "also_matches": [
                    t for t in room.get("all_types", []) if t != room["room_type"]
                ],
            }
        )

    summary = _build_summary(rooms, opening_summary)
    status, message, notes = _build_messages(
        no_text=no_text,
        room_count=len(rooms),
        explicit_count=summary["explicit_labels"],
        scale_ratio=scale_ratio,
        summary=summary,
        pages_with_images=pages_with_images,
        ocr_available=ocr_available,
        pages_with_ocr_text=pages_with_ocr_text,
        pages_ocr_attempted=pages_ocr_attempted,
    )

    diagnostics = {
        "pages_analyzed": page_count,
        "pages_with_room_labels": len(label_pages),
        "pages_with_embedded_images": pages_with_images,
        "pages_with_ocr_text": pages_with_ocr_text,
        "pages_ocr_attempted": pages_ocr_attempted,
        "ocr_available": ocr_available,
        "text_lines_scanned": text_lines_scanned,
        "room_label_candidates": len(room_candidates),
        "duplicate_labels_merged": merged_count,
        "opening_duplicates_merged": opening_merged,
        "vector_regions_matched": vector_matches,
        "image_regions_matched": image_matches,
        "image_regions_detected": image_region_stats["regions"],
        "image_pages_segmented": image_pages_used,
        "image_region_detection": "experimental",
        "region_method": (
            "smallest containing vector path rectangle, with experimental "
            "rendered-image segmentation as a fallback"
        ),
        "analysis_ms": int((time.perf_counter() - started) * 1000),
    }

    return {
        "status": status,
        "classification_reliable": status == "ok" and summary["explicit_labels"] > 0,
        "message": message,
        "summary": summary,
        "rooms": rooms,
        "notes": notes,
        "scale": {
            "found": scale_ratio is not None,
            "ratio": scale_ratio,
            "text": f"SCALE 1:{scale_ratio}" if scale_ratio else None,
        },
        "diagnostics": diagnostics,
    }


def _build_summary(
    rooms: List[Dict[str, Any]], openings: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    def count_types(types: Any) -> int:
        return sum(1 for room in rooms if room["room_type"] in types)

    return {
        "total_rooms": len(rooms),
        "bedrooms": count_types({"bedroom"}),
        "kitchens": count_types({"kitchen"}),
        "living_rooms": count_types({"living_room"}),
        "bathrooms": count_types({"bathroom"}),
        "other_rooms": count_types(OTHER_TYPES),
        "explicit_labels": sum(1 for r in rooms if r["detection"] == "explicit"),
        "inferred_labels": sum(1 for r in rooms if r["detection"] == "inferred"),
        "dimensions_available": any(r["width_mm"] is not None for r in rooms),
        "doors": openings["door"],
        "windows": openings["window"],
    }


def _build_messages(
    no_text: bool,
    room_count: int,
    explicit_count: int,
    scale_ratio: Optional[int],
    summary: Dict[str, Any],
    pages_with_images: int = 0,
    ocr_available: bool = False,
    pages_with_ocr_text: int = 0,
    pages_ocr_attempted: int = 0,
) -> Tuple[str, str, List[str]]:
    notes: List[str] = []
    if no_text:
        if pages_ocr_attempted:
            page_description = (
                "The pages contain scanned images. "
                if pages_with_images > 0
                else ""
            )
            return (
                "no_text",
                f"No readable text was found, even after OCR. {page_description}"
                "The scan may be too blurry, low-resolution, or difficult to read.",
                [
                    "OCR ran but did not recognize readable text. Check the "
                    "scan quality and verify that Tesseract's English "
                    "language data is installed.",
                ],
            )
        page_description = (
            "on image-based pages" if pages_with_images > 0 else "in this PDF"
        )
        return (
            "no_text",
            f"No selectable text was found {page_description}. Install "
            "Tesseract OCR with English language data and make sure it is on "
            "PATH, then restart the app to read scanned or image-only drawings."
            if not ocr_available
            else "No readable text was found in the PDF.",
            [
                (
                    "Tesseract OCR is not available on PATH. Install it with "
                    "English ('eng') language data and restart the app."
                    if not ocr_available
                    else "No readable text could be extracted from the PDF."
                )
            ],
        )
    if pages_with_ocr_text:
        notes.append(
            "Some text was recognized by OCR. Verify OCR-read labels against "
            "the drawing before relying on the room counts."
        )
    if room_count == 0:
        no_labels_notes = [
            "No rooms are reported when no label matches the known room "
            "vocabulary - counts are never guessed from geometry alone."
        ]
        if pages_with_ocr_text:
            no_labels_notes.extend(notes)
        return (
            "no_labels",
            "Room classification could not be reliably determined: no readable "
            "room labels were found in the extracted text.",
            no_labels_notes,
        )

    message = (
        f"{room_count} room label{'s' if room_count != 1 else ''} detected from "
        "readable text."
    )
    if explicit_count == 0:
        message = (
            "Only abbreviated or inferred labels were found, so room "
            "classification may be incomplete or incorrect."
        )
        notes.append(
            "All detections are *inferred* from abbreviations (for example "
            "'BED RM' or 'KIT'); treat counts as a starting point only."
        )
    else:
        notes.append(
            "Rooms are counted from explicit text labels only; every label "
            "keeps its original wording and page number."
        )

    if scale_ratio is None:
        notes.append(
            "Room dimensions and areas were not calculated: no explicit "
            "drawing scale (for example 'SCALE 1:100') was found in the PDF. "
            "Outer/overall dimensions shown on a drawing do NOT establish "
            "each room's individual area."
        )
    if (
        summary["doors"]["status"] == "detected_from_plan_tags"
        or summary["windows"]["status"] == "detected_from_plan_tags"
    ):
        notes.append(
            "Door and window counts include opening tags placed on the "
            "drawing (for example D1, D2, W1, W2); each tag occurrence is "
            "counted as one opening."
        )
    if (
        summary["doors"]["status"] == "detected_from_schedule"
        or summary["windows"]["status"] == "detected_from_schedule"
    ):
        notes.append(
            "Door and window counts include the schedule-of-openings table; "
            "these are distinct opening types, not necessarily the number of "
            "physical openings."
        )
    if summary["doors"]["status"] == "not_detected":
        notes.append(
            "No door labels were detected (doors are counted from plan tags "
            "such as D1, a schedule of openings, or a readable 'DOOR' label)."
        )
    if summary["windows"]["status"] == "not_detected":
        notes.append(
            "No window labels were detected (windows are counted from plan "
            "tags such as W1, a schedule of openings, or a readable 'WINDOW' "
            "label)."
        )
    notes.append(
        "Vector paths are never counted as rooms; geometry is only used to "
        "attach an enclosing region to a detected label."
    )
    return "ok", message, notes
