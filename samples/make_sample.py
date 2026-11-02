"""Generate samples/sample_floorplan.pdf - the test fixture floor plan.

A minimal two-bedroom plan with wall rectangles drawn as vector paths and
room labels placed as selectable text:

    LIVING ROOM, KITCHEN, BEDROOM 1, BEDROOM 2

The drawing also carries an *outer* dimension note ("OVERALL ...") but
deliberately no SCALE statement: without an explicit scale the analyzer
must NOT compute per-room dimensions or areas, and the UI note about
outer dimensions not establishing a room's area must appear.

Run from the project root:

    .\\.venv\\Scripts\\python.exe samples\\make_sample.py
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

BLACK = (0, 0, 0)
WALL_WIDTH = 6.0
PAGE_W, PAGE_H = 1000.0, 700.0

# Room cells: (name, x0, y0, x1, y1)
CELLS = [
    ("living", 50.0, 50.0, 550.0, 650.0),
    ("kitchen", 550.0, 50.0, 950.0, 300.0),
    ("bedroom1", 550.0, 300.0, 950.0, 475.0),
    ("bedroom2", 550.0, 475.0, 950.0, 650.0),
]

LABELS = [
    ("LIVING ROOM", 150.0, 330.0, 450.0, 370.0),
    ("KITCHEN", 650.0, 150.0, 850.0, 190.0),
    ("BEDROOM 1", 650.0, 360.0, 850.0, 400.0),
    ("BEDROOM 2", 650.0, 535.0, 850.0, 575.0),
]


def build() -> pymupdf.Document:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)

    for _name, x0, y0, x1, y1 in CELLS:
        page.draw_rect(
            pymupdf.Rect(x0, y0, x1, y1),
            color=BLACK,
            width=WALL_WIDTH,
        )

    for text, x0, y0, x1, y1 in LABELS:
        page.insert_textbox(
            pymupdf.Rect(x0, y0, x1, y1),
            text,
            fontsize=18,
            fontname="helv",
            color=BLACK,
            align=pymupdf.TEXT_ALIGN_CENTER,
        )

    # Outer / overall dimensions only - intentionally no SCALE note, so
    # per-room areas must stay uncalculated.
    page.insert_textbox(
        pymupdf.Rect(300.0, 662.0, 700.0, 692.0),
        "OVERALL 10000 x 8000 MM",
        fontsize=13,
        fontname="helv",
        color=BLACK,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    return doc


def main() -> None:
    target = Path(__file__).resolve().parent / "sample_floorplan.pdf"
    doc = build()
    doc.save(target)
    doc.close()
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
