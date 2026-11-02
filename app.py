"""CAD PDF Extractor - a lightweight, local Streamlit application.

Extracts selectable text, page information, page previews and vector
drawing statistics from PDF engineering drawings, floor plans and CAD
exports. Everything runs locally, in memory.

The PDF extraction logic lives in extractor.py and is shared with the
FastAPI backend (backend/main.py) used by the Next.js frontend.
"""

from __future__ import annotations

import hashlib
import traceback
from typing import Any, Dict, Tuple

import streamlit as st

from extractor import (
    APP_TITLE,
    DEFAULT_MAX_PREVIEW_PAGES,
    DEFAULT_PREVIEW_DPI,
    MAX_UPLOAD_MB,
    build_json_payload,
    document_requires_password,
    extract_pdf,
    friendly_error,
    human_size,
    try_authenticate,
    validate_pdf_bytes,
)

SCOPE_POSITIVE = [
    "Extract selectable text from every page of a PDF.",
    "Show filename, page count, page dimensions (mm and points).",
    "Count vector drawing paths and drawing segments per page.",
    "Show a rendered preview of each page.",
    "Download all extracted results as a JSON file.",
]

SCOPE_NEGATIVE = [
    "It does **not** identify room names, room areas, wall lengths, "
    "dimensions or construction quantities.",
    "It does **not** run OCR (OCR is not supported), so scanned drawings "
    "that contain no selectable text will report zero text.",
    "Semantic dimension and quantity extraction is a planned future "
    "feature, not something this version can do.",
]


@st.cache_data(show_spinner="Reading PDF and extracting data...", max_entries=3)
def process_pdf(
    data: bytes,
    password: str,
    preview_dpi: int,
    max_preview_pages: int,
    count_vectors: bool,
) -> Tuple[Dict[str, Any], Dict[int, bytes]]:
    """Cached wrapper so widget interactions do not re-parse the whole PDF."""
    return extract_pdf(data, password, preview_dpi, max_preview_pages, count_vectors)


# ---------------------------------------------------------------------------
# User interface
# ---------------------------------------------------------------------------

def show_scope() -> None:
    st.markdown("#### What this version does")
    for item in SCOPE_POSITIVE:
        st.markdown(f"- {item}")
    st.markdown("#### What this version does not do yet")
    for item in SCOPE_NEGATIVE:
        st.markdown(f"- {item}")


def show_password_prompt(data: bytes, file_hash: str) -> None:
    st.warning("This PDF is password protected. Enter the password to continue.")
    with st.form("pdf_password_form"):
        entered = st.text_input("PDF password", type="password")
        submitted = st.form_submit_button("Unlock PDF")
        if submitted:
            if try_authenticate(data, entered):
                st.session_state["unlock"] = {
                    "hash": file_hash,
                    "password": entered,
                }
                st.rerun()
            else:
                st.error(
                    "Incorrect password, or the file could not be opened. "
                    "Please try again."
                )
    st.stop()


def render_results(
    info: Dict[str, Any],
    previews: Dict[int, bytes],
    uploaded_name: str,
    file_size: int,
    preview_dpi: int,
    max_preview_pages: int,
    count_vectors: bool,
) -> None:
    doc_info = info.get("document", {})
    pages = info.get("pages", [])

    st.subheader(uploaded_name)
    st.caption(
        f"Format: {doc_info.get('pdf_format', 'unknown')} | "
        f"File size: {human_size(file_size)} | "
        "Processed in memory - the file is not saved to disk."
    )

    first = pages[0] if pages else None
    total_paths = sum(p.get("vector_paths") or 0 for p in pages)
    total_segments = sum(p.get("vector_segments") or 0 for p in pages)
    total_text = sum(p.get("text_characters", 0) for p in pages)
    pages_with_text = sum(1 for p in pages if p.get("text_characters", 0) > 0)

    if not pages:
        st.warning("This PDF contains no pages.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Pages", len(pages))
    c2.metric(
        "Page dimensions (page 1)",
        f"{first['width_mm']} x {first['height_mm']} mm" if first else "n/a",
    )
    c3.metric("Vector paths", f"{total_paths:,}" if count_vectors else "n/a")
    c4.metric("Drawing segments", f"{total_segments:,}" if count_vectors else "n/a")

    c5, c6 = st.columns(2)
    c5.metric("Pages with selectable text", f"{pages_with_text} / {len(pages)}")
    c6.metric("Total text characters", f"{total_text:,}")

    payload = build_json_payload(info, uploaded_name, file_size)
    file_stem = uploaded_name.rsplit(".", 1)[0] or "extracted"
    st.download_button(
        "Download extracted results as JSON",
        data=payload,
        file_name=f"{file_stem}_extracted.json",
        mime="application/json",
        type="primary",
        key="download_json",
    )

    if pages_with_text == 0:
        st.warning(
            "No selectable text was found on any page. This usually means the "
            "drawing is a scanned image or contains only vector geometry. "
            "OCR is currently unavailable in this version - please try a "
            "text-based CAD PDF with selectable text."
        )

    st.markdown("#### Page overview")
    table_rows = []
    for page in pages:
        table_rows.append(
            {
                "Page": page["page_number"],
                "Width (mm)": page["width_mm"],
                "Height (mm)": page["height_mm"],
                "Width (pt)": page["width_points"],
                "Height (pt)": page["height_points"],
                "Rotation (deg)": page["rotation_degrees"],
                "Text characters": page["text_characters"],
                "Vector paths": (
                    page["vector_paths"] if page["vector_paths"] is not None else "n/a"
                ),
                "Segments": (
                    page["vector_segments"]
                    if page["vector_segments"] is not None
                    else "n/a"
                ),
            }
        )
    st.dataframe(table_rows, use_container_width=True, hide_index=True)

    rendered = len(previews)
    if max_preview_pages != 0 and rendered < len(pages):
        st.caption(
            f"Previews are shown for the first {rendered} of {len(pages)} pages "
            "(limit set in the sidebar)."
        )

    st.markdown("#### Pages")
    for position, page in enumerate(pages):
        number = page["page_number"]
        label = (
            f"Page {number} - {page['width_mm']} x {page['height_mm']} mm - "
            f"{page['text_characters']} text characters"
        )
        with st.expander(label, expanded=(position == 0)):
            left, right = st.columns(2)
            with left:
                png = previews.get(position)
                if png:
                    st.image(png, caption=f"Page {number} preview ({preview_dpi} DPI)")
                else:
                    st.caption(
                        "No preview for this page. Increase the preview page "
                        "limit in the sidebar to render it."
                    )
            with right:
                if page["text_characters"] == 0:
                    st.info(
                        f"No selectable text found on page {number}. The page is "
                        "probably a scan or pure vector geometry. OCR is "
                        "currently unavailable in this version - please try a "
                        "text-based CAD PDF."
                    )
                else:
                    st.caption("Selectable text")
                    st.text_area(
                        f"Text from page {number}",
                        value=page["text"],
                        height=320,
                        key=f"page_text_{number}",
                        label_visibility="collapsed",
                    )
                    st.caption(f"{page['text_characters']} characters extracted")
                if count_vectors:
                    st.caption(
                        f"Vector paths: {page['vector_paths']:,} | "
                        f"Drawing segments: {page['vector_segments']:,}"
                    )


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Local tool for PDF engineering drawings, floor plans and CAD exports. "
        "Extracts text, page information, previews and vector drawing "
        "statistics. Files are processed in memory and never uploaded anywhere."
    )

    with st.sidebar:
        st.header("Settings")
        preview_dpi = st.slider(
            "Preview quality (DPI)",
            min_value=72,
            max_value=200,
            value=DEFAULT_PREVIEW_DPI,
            step=8,
            help="Higher values look sharper but use more memory. 100 is a good default.",
        )
        max_preview_pages = st.number_input(
            "Maximum pages to preview (0 = all)",
            min_value=0,
            max_value=2000,
            value=DEFAULT_MAX_PREVIEW_PAGES,
            step=1,
            help="Limits memory use on large drawings.",
        )
        count_vectors = st.checkbox(
            "Compute vector drawing statistics",
            value=True,
            help="Turn this off for very large PDFs if processing feels slow.",
        )
        st.divider()
        st.markdown("**Version 1 scope**")
        st.markdown(
            "- Text, page sizes, previews and vector counts only.\n"
            "- No room names, areas, wall lengths, dimensions or quantities.\n"
            "- Scanned drawings need OCR (not supported in this version)."
        )

    uploaded = st.file_uploader(
        "Upload a PDF engineering drawing, floor plan or CAD drawing",
        type=["pdf"],
        help=(
            f"Maximum size: {MAX_UPLOAD_MB} MB. The file is processed in "
            "memory and is not saved to disk."
        ),
    )

    if uploaded is None:
        st.info("No file selected yet. Use the box above to choose a PDF.")
        show_scope()
        return

    data = uploaded.getvalue()
    validation_error = validate_pdf_bytes(data)
    if validation_error:
        st.error(validation_error)
        return

    file_hash = hashlib.sha256(data).hexdigest()
    password = ""
    unlock = st.session_state.get("unlock")
    if isinstance(unlock, dict) and unlock.get("hash") == file_hash:
        password = unlock.get("password", "")

    if document_requires_password(data) and not try_authenticate(data, password):
        st.session_state.pop("unlock", None)
        show_password_prompt(data, file_hash)

    try:
        info, previews = process_pdf(
            data, password, preview_dpi, int(max_preview_pages), count_vectors
        )
    except Exception as exc:  # graceful failure for broken or odd files
        st.error(friendly_error(exc))
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
        return

    render_results(
        info=info,
        previews=previews,
        uploaded_name=uploaded.name,
        file_size=len(data),
        preview_dpi=preview_dpi,
        max_preview_pages=int(max_preview_pages),
        count_vectors=count_vectors,
    )


if __name__ == "__main__":
    main()
