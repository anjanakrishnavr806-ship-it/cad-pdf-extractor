"""FastAPI backend for CAD PDF Extractor.

Reuses the shared extraction logic in backend/service.py. The Next.js
frontend (frontend/) can talk to this server, or run the same pipeline
in-process on port 3000 (the default). Everything runs locally and in
memory - files are never saved to disk.

Start (from the project root, with the virtual environment active):

    uvicorn backend.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow "import extractor" regardless of the working directory.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.service import MAX_UPLOAD_BYTES, ExtractionError, build_result
from extractor import APP_TITLE

READ_CHUNK_SIZE = 1024 * 1024


app = FastAPI(title=APP_TITLE, version="1.0.0")

# The backend is local-only; CORS is limited to the Next.js dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


async def read_upload_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the upload in chunks, rejecting anything over max_bytes."""
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. "
                    "Please use a smaller PDF, or split the drawing into "
                    "several files."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {"status": "ok", "app": APP_TITLE}


@app.post("/api/extract")
async def extract(
    file: UploadFile = File(...),
    password: str = Form(""),
    preview_dpi: Optional[str] = Form(None),
    max_preview_pages: Optional[str] = Form(None),
    count_vectors: Optional[str] = Form(None),
) -> Dict[str, Any]:
    filename = file.filename or "uploaded.pdf"
    data = await read_upload_capped(file, MAX_UPLOAD_BYTES)

    try:
        return build_result(
            data,
            filename,
            password=password,
            preview_dpi=preview_dpi,
            max_preview_pages=max_preview_pages,
            count_vectors=count_vectors,
        )
    except ExtractionError as exc:
        detail: Any = exc.message
        if exc.code:
            detail = {"code": exc.code, "message": exc.message}
        raise HTTPException(status_code=exc.status, detail=detail)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    detail: Any = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message", "Request failed.")
    else:
        message = str(detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": message, "code": (detail.get("code") if isinstance(detail, dict) else None)},
    )
