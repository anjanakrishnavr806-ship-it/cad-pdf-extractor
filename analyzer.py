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
  message says OCR is currently unavailable and suggests trying a
  text-based CAD PDF.
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

from extractor import POINTS_PER_INCH, open_document

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
    "other": "Other",
}

# Types that roll up into the "Other rooms" dashboard bucket.
OTHER_TYPES = {"dining_room", "study", "store", "corridor", "other"}

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
]

# Abbreviations / partial terms.  Matched rooms are reported as
# "inferred" detections (lower confidence), never as explicit ones.
INFERRED_RULES: List[Tuple[str, str]] = [
    ("bedroom", r"\bBED\s*RM\b|\bBDRM\b|\bBED\b"),
    ("kitchen", r"\bKIT\b|\bKITTN\b"),
    ("living_room", r"\bLIV\s*RM\b|\bLIV\b"),
    ("bathroom", r"\bBATH\b|\bPWDR\b|\bW\.C\.\b"),
    ("dining_room", r"\bDIN\b"),
    ("study", r"\bSTY\b"),
    ("store", r"\bSTOR\b"),
    ("corridor", r"\bCORR\b|\bHALL\b"),
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
    )

    # Table rows commonly follow a pattern like:
    #   1 LIVING ROOM 4.0 x 3.5 14.0
    #   LIVING ROOM 4.0 x 3.5 14.0
    #   Bedroom 2 3.0 x 3.0 9.0
    # We only suppress rows that combine a room keyword with a dimension/area
    # pattern and a summary value, because that is schedule metadata rather than
    # the actual room label itself.
    has_room_keyword = any(keyword in norm for keyword in room_keywords)
    has_dimension = bool(re.search(r"\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?", norm))
    has_area_value = bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:M2|M\^2|SQM|SQFT|M)|\b\d+(?:\.\d+)?\b", norm))

    if has_room_keyword and has_dimension and has_area_value:
        return True

    # A row that starts with a numeric ID and room name is also schedule data.
    if re.match(r"^\d+\s+(?:MASTER\s+)?[A-Z0-9\s/.-]+$", norm):
        if any(keyword in norm for keyword in room_keywords):
            return True

    # Table headers and summary rows are not room labels.
    if re.search(r"\b(?:NO\.|ROOM|SIZE|AREA|TOTAL)\b", norm) and re.search(r"\d", norm):
        return True

    return False

DOOR_RULE = r"\bDOORS?\b"
WINDOW_RULE = r"\bWINDOWS?\b"
NON_OPENING_KEYWORDS = ("SCHEDULE", "LEGEND", "INDEX", "NOTES")

SCALE_RULE = re.compile(r"\bSCALE\b[^0-9]{0,12}1\s*[:/]\s*(\d+)", re.IGNORECASE)

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
    return len(types) >= 2


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
        if not any(keyword in norm for keyword in SCHEDULE_HEADER_KEYWORDS):
            continue
        hx0, hy0, hx1, hy1 = header["bbox"]
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

def _collect_lines(page: Any) -> List[Dict[str, Any]]:
    """Return every text line on the page with its bounding box."""
    lines: List[Dict[str, Any]] = []
    try:
        payload = page.get_text("dict")
    except Exception:
        return lines
    for block in payload.get("blocks") or []:
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines") or []:
            spans = line.get("spans") or []
            text = "".join(span.get("text", "") for span in spans).strip()
            if not text:
                continue
            bbox = line.get("bbox") or (0, 0, 0, 0)
            lines.append({"text": text, "bbox": [float(v) for v in bbox]})
    return lines


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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_rooms(
    data: bytes,
    password: str = "",
    use_image_regions: bool = True,
) -> Dict[str, Any]:
    """Detect and classify room labels in a PDF floor plan.

    Returns a JSON-serialisable analysis dictionary.  Never invents
    rooms, dimensions or areas: every number comes from readable text
    or from an explicitly scaled geometry calculation.
    """
    started = time.perf_counter()
    doc = open_document(data, password)
    try:
        return _analyze_document(doc, use_image_regions=use_image_regions, started=started)
    finally:
        doc.close()


def _analyze_document(
    doc: Any, use_image_regions: bool, started: float
) -> Dict[str, Any]:
    page_count = doc.page_count
    page_lines: Dict[int, List[Dict[str, Any]]] = {}
    page_texts: Dict[int, str] = {}
    total_text_chars = 0
    pages_with_images = 0
    room_candidates: List[Dict[str, Any]] = []
    opening_candidates: List[Dict[str, Any]] = []
    text_lines_scanned = 0

    for index in range(page_count):
        page = doc.load_page(index)
        try:
            raw_text = page.get_text("text") or ""
        except Exception:
            raw_text = ""
        page_texts[index] = raw_text
        total_text_chars += len(raw_text.strip())
        try:
            if page.get_images(full=True):
                pages_with_images += 1
        except Exception:
            pass

        lines = _collect_lines(page)
        page_lines[index] = lines
        text_lines_scanned += len(lines)
        schedule_regions = _schedule_regions(lines, float(page.rect.width))

        for line in lines:
            if any(_center_inside(line["bbox"], region) for region in schedule_regions):
                # Room names inside a schedule/legend table are metadata,
                # not labels on the drawing itself.
                continue
            classified = classify_label(line["text"])
            if classified:
                room_candidates.append(
                    {
                        "label": normalize_label(line["text"]),
                        "page": index + 1,
                        "bbox": list(line["bbox"]),
                        "room_type": classified["room_type"],
                        "detection": classified["detection"],
                        "all_types": classified["all_types"],
                        "_line": line,
                    }
                )
            opening = classify_opening(line["text"])
            if opening:
                opening_candidates.append(
                    {
                        "kind": opening,
                        "label": normalize_label(line["text"]),
                        "page": index + 1,
                        "bbox": list(line["bbox"]),
                    }
                )

    scale_ratio = _find_scale(page_texts)
    no_text = total_text_chars == 0

    # Expand wrapped labels (MASTER / BEDROOM) before merging.
    for candidate in room_candidates:
        lines = page_lines.get(candidate["page"] - 1, [])
        _expand_label(candidate, lines)
    rooms_raw, merged_count = _merge_duplicates(room_candidates)

    # Opening dedup (same simple proximity rule, per kind).
    openings: Dict[str, List[Dict[str, Any]]] = {"door": [], "window": []}
    opening_merged = 0
    for kind in ("door", "window"):
        kept: List[Dict[str, Any]] = []
        for candidate in [c for c in opening_candidates if c["kind"] == kind]:
            hit = None
            for existing in kept:
                if existing["page"] != candidate["page"]:
                    continue
                gap_x, gap_y = _bbox_gap(existing["bbox"], candidate["bbox"])
                if gap_x <= MERGE_GAP_X and gap_y <= MERGE_GAP_Y:
                    hit = existing
                    break
            if hit is None:
                kept.append(candidate)
            else:
                opening_merged += 1
                hit["merged_labels"] = list(
                    dict.fromkeys(hit.get("merged_labels", [hit["label"]]) + [candidate["label"]])
                )
        openings[kind] = kept

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
        if region is None:
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

        if room["detection"] == "explicit":
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
                "dimension_status": dimension_status,
                "combined_label": len(room.get("all_types", [])) > 1,
                "also_matches": [
                    t for t in room.get("all_types", []) if t != room["room_type"]
                ],
            }
        )

    summary = _build_summary(rooms, openings)
    status, message, notes = _build_messages(
        no_text=no_text,
        room_count=len(rooms),
        explicit_count=summary["explicit_labels"],
        scale_ratio=scale_ratio,
        summary=summary,
        pages_with_images=pages_with_images,
    )

    diagnostics = {
        "pages_analyzed": page_count,
        "pages_with_room_labels": len(label_pages),
        "pages_with_embedded_images": pages_with_images,
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
    rooms: List[Dict[str, Any]], openings: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    def count_types(types: Any) -> int:
        return sum(1 for room in rooms if room["room_type"] in types)

    door_count = openings["door"]
    window_count = openings["window"]
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
        "doors": {
            "count": len(door_count) if door_count else None,
            "status": "detected_from_text_labels" if door_count else "not_detected",
        },
        "windows": {
            "count": len(window_count) if window_count else None,
            "status": "detected_from_text_labels" if window_count else "not_detected",
        },
    }


def _build_messages(
    no_text: bool,
    room_count: int,
    explicit_count: int,
    scale_ratio: Optional[int],
    summary: Dict[str, Any],
    pages_with_images: int = 0,
) -> Tuple[str, str, List[str]]:
    notes: List[str] = []
    if no_text:
        if pages_with_images > 0:
            return (
                "no_text",
                "No selectable text was found in this PDF. The pages contain "
                "embedded images (a scanned drawing or a rasterized CAD "
                "export), so room labels cannot be read. OCR is currently "
                "unavailable in this app - please try a text-based CAD PDF.",
                [
                    "OCR is not supported in this version; floor plans "
                    "stored only as images cannot be classified. Try a "
                    "text-based CAD PDF with selectable room labels.",
                ],
            )
        return (
            "no_text",
            "No selectable text was found in this PDF, so room classification "
            "could not be attempted. OCR is currently unavailable in this app "
            "- please try a text-based CAD PDF.",
            [
                "OCR is not supported in this version; scanned drawings or "
                "pure vector geometry cannot be classified. Try a text-based "
                "CAD PDF with selectable room labels.",
            ],
        )
    if room_count == 0:
        return (
            "no_labels",
            "Room classification could not be reliably determined: no readable "
            "room labels were found in the PDF text.",
            [
                "No rooms are reported when no label matches the known room "
                "vocabulary - counts are never guessed from geometry alone.",
            ],
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
    if summary["doors"]["status"] == "not_detected":
        notes.append(
            "No door labels were detected (doors are only counted when a "
            "readable 'DOOR' text label exists)."
        )
    if summary["windows"]["status"] == "not_detected":
        notes.append(
            "No window labels were detected (windows are only counted when a "
            "readable 'WINDOW' text label exists)."
        )
    notes.append(
        "Vector paths are never counted as rooms; geometry is only used to "
        "attach an enclosing region to a detected label."
    )
    return "ok", message, notes
