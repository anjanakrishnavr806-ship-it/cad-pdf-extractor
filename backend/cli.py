"""In-process extraction CLI used by the Next.js API route (single port).

The PDF bytes are read from stdin and the JSON response is written to stdout,
so no temporary file is ever created. This lets the frontend run everything on
http://localhost:3000 without starting a separate uvicorn server.

Options are passed through environment variables (never argv) to avoid
exposing the PDF password in the process list:

    CAD_FILENAME            original upload filename
    CAD_PASSWORD            optional PDF password
    CAD_PREVIEW_DPI         preview render DPI
    CAD_MAX_PREVIEW_PAGES   maximum pages to render
    CAD_COUNT_VECTORS       "true"/"false"

Usage:
    python backend/cli.py < drawing.pdf
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.service import ExtractionError, build_result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    data = sys.stdin.buffer.read()
    filename = os.environ.get("CAD_FILENAME") or "uploaded.pdf"

    try:
        result = build_result(
            data,
            filename,
            password=os.environ.get("CAD_PASSWORD", ""),
            preview_dpi=os.environ.get("CAD_PREVIEW_DPI"),
            max_preview_pages=os.environ.get("CAD_MAX_PREVIEW_PAGES"),
            count_vectors=os.environ.get("CAD_COUNT_VECTORS"),
        )
    except ExtractionError as exc:
        json.dump(
            {"error": exc.message, "code": exc.code, "status": exc.status},
            sys.stdout,
            ensure_ascii=False,
        )
        return 0
    except Exception as exc:  # unexpected failure
        traceback.print_exc()
        json.dump(
            {
                "error": f"Unexpected extraction failure: {exc}",
                "code": None,
                "status": 500,
            },
            sys.stdout,
            ensure_ascii=False,
        )
        return 0

    json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
