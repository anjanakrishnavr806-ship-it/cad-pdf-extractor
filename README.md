# CAD PDF Extractor

A small, beginner-friendly web app that extracts information from PDF
engineering drawings, floor plans and CAD exports.

It runs fully **locally**. No Docker, no cloud, no API keys, no paid
services, no database. Uploaded PDFs are processed **in memory** and are not
saved to disk.

The project has two parts:

| Part | Technology | Command |
| ---- | ---------- | ------- |
| Frontend + local extraction | Next.js + TypeScript + Tailwind CSS | `npm run dev` (in `frontend/`) |
| Optional HTTP backend | Python + FastAPI + PyMuPDF | `uvicorn backend.main:app` (project root) |

By default the Next.js API route runs the Python extractor directly, so the
whole app lives on **one port: <http://localhost:3000>**. You do not need to
start a separate backend server. The FastAPI server in `backend/main.py`
still works and is only needed if you prefer to run Python as a service.

The original **Streamlit** single-file app (`app.py`) still works and shares
the same extraction code.

## What it does (version 2)

- Web interface titled **CAD PDF Extractor** (Next.js frontend).
- Upload a PDF drawing and validate it (invalid, empty, unsupported,
  oversized and password-protected files are reported clearly).
- **Detects room labels** from readable PDF text and classifies them:
  bedrooms, kitchens, living rooms, bathrooms, balconies, and other rooms
  (dining, study, store, corridor). Explicit terms (BEDROOM, KITCHEN, LIVING ROOM,
  BATHROOM...) and abbreviations (BED RM, KIT...) are distinguished;
  abbreviations are reported as *inferred*.
- **Merges duplicate nearby labels** so one room is not counted twice
  (e.g. MASTER + BEDROOM on separate lines). Original label text and page
  number are always preserved.
- **Results dashboard**: summary cards (total rooms, bedrooms, kitchens,
  living rooms, bathrooms, other, doors/windows when labelled), plus a
  room-details table (name, type, page, dimensions, area, status).
- **Geometry association** attaches a vector rectangle, or an experimental
  region on non-raster pages, to an already detected label. Geometry alone
  never creates a room, and vector paths are never counted as rooms.
- **Tesseract OCR fallback** reads scanned and embedded-image floor plans,
  including pages that also have a small amount of selectable title text.
  Full-page images are read at their embedded resolution; weak OCR results
  trigger alternate layout and focused-crop passes. OCR-recognized labels are
  marked for review and assigned medium confidence.
- **Printed room dimensions** with explicit metric or feet-and-inches units
  (for example "4.50 × 2.10 m" or `11'6" × 10'6"`) are read from drawing
  text/OCR and associated with nearby room labels. The original printed
  values are shown in the room table.
- **Dimensions calculated from drawing geometry** require an explicit scale
  (e.g. "SCALE 1:100") and a matched room region. Outer/overall dimensions
  never establish each room's individual area.
- Drawing preview and technical diagnostics are in separate collapsible
  sections.
- Download all structured results (including the room analysis) as JSON.

## What it does NOT do yet

- It does **not** invent room counts, dimensions or areas. If a PDF has
  no readable labels, the app says room classification could not be
  reliably determined.
- OCR requires the separate **Tesseract OCR** application with English
  (`eng`) language data installed and its executable available on `PATH`.
  Without it, scans without selectable text cannot be analyzed.
- OCR may misread small, blurry, rotated, or low-contrast labels; verify
  recognized text against the drawing.
- Image-based region detection is **experimental** and is not used to invent
  rooms or estimate dimensions from raster scans. Verify OCR labels and
  dimensions against the drawing, especially for low-resolution images.
- Wall lengths, construction quantities and takeoffs are out of scope.

## Requirements

- Windows, macOS or Linux
- Python 3.9 or newer (tested with Python 3.14)
- Node.js 18.18 or newer (tested with Node.js 24) and npm
- Tesseract OCR with English (`eng`) language data for scanned PDFs
- About 400 MB of free disk space (Python deps + Next.js `node_modules`)

## Project layout

```
cad-pdf-extractor/
├── app.py               # Original Streamlit UI (optional, still works)
├── extractor.py         # Shared PyMuPDF extraction logic (single source)
├── analyzer.py          # Room label detection & classification
├── backend/
│   ├── cli.py           # In-process extractor used by the Next.js API route
│   ├── service.py       # Shared extraction pipeline
│   ├── main.py          # Optional FastAPI server (port 8000)
│   └── test_backend.py  # Backend tests (unittest)
├── samples/
│   ├── make_sample.py   # Regenerates the sample floor-plan PDF
│   └── sample_floorplan.pdf
├── frontend/            # Next.js + TypeScript + Tailwind CSS app
│   ├── app/
│   │   ├── page.tsx     # Main UI (analysis dashboard)
│   │   └── api/extract/route.ts  # Upload → runs backend/cli.py locally
│   └── package.json
├── requirements.txt     # Python packages (streamlit, pymupdf, fastapi, uvicorn, python-multipart, numpy, Pillow)
└── README.md
```

## Setup (Windows PowerShell)

### 1. Python backend dependencies

Open PowerShell and run:

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

To enable scanned-PDF OCR, install Tesseract OCR separately, include the
English (`eng`) language data, and ensure `tesseract.exe` is on `PATH`.
Verify the installation in a new PowerShell window:

```powershell
tesseract --version
```

Restart the app after installing Tesseract so it can find the executable.

If PowerShell blocks the activation script, run this once in the same window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Or skip activation entirely and always call the virtual environment Python
directly (used in the run commands below).

### 2. Frontend dependencies

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor\frontend"
npm install
```

## Run the application

Everything runs on **one port (3000)**. You need only one terminal.

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor\frontend"
npm run dev
```

Open <http://localhost:3000> in your browser.

The upload goes to the Next.js API route
`frontend/app/api/extract/route.ts`, which validates the file and runs
`backend/cli.py` with the project's virtual-environment Python. The PDF is
piped through stdin and the JSON result comes back on stdout, so no temporary
files are created and no second web server is needed.

The `.venv` must exist with the Python dependencies installed (see below). If
it lives somewhere else, set `PYTHON_EXECUTABLE` before `npm run dev`.

### Optional — run Python as a separate FastAPI server (port 8000)

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor"
.\.venv\Scripts\Activate.ps1
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Health check: <http://127.0.0.1:8000/api/health> returns
`{"status":"ok","app":"CAD PDF Extractor"}`.

To make the frontend use that server instead of the in-process extractor, set
`PYTHON_API_URL` before `npm run dev`:

```powershell
$env:PYTHON_API_URL = "http://127.0.0.1:8000"
npm run dev
```

### Optional — original Streamlit UI (port 8501)

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor"
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

Both UIs use the same extraction code in `extractor.py`.

## How to use it

1. Click the upload box and choose a PDF floor plan (or drag a file onto it).
2. Optionally set the password, preview DPI, maximum preview pages and whether
   to compute vector statistics.
3. Click **Analyze floor plan** and wait for the status indicator to finish.
4. Read the analysis dashboard: status message, summary cards (total rooms,
   bedrooms, kitchens, living rooms, bathrooms, other, doors/windows) and the
   room-details table (name, type, page, dimensions, area, confidence/status).
5. Expand **Drawing preview**, **Raw extracted text** or **Technical
   diagnostics** as needed.
6. Click **Download JSON** to save everything, including the structured room
   analysis.

Try the bundled sample first:
`samples/sample_floorplan.pdf` contains LIVING ROOM, KITCHEN, BEDROOM 1 and
BEDROOM 2. All four are classified as explicit detections. The sample has an
outer "OVERALL" dimension note but **no** scale statement, so per-room
dimensions and areas stay empty — the outer dimensions do not establish each
room's individual area.

## Run the backend tests

```powershell
cd "C:\Users\Anjana Krishna\cad-pdf-extractor"
.\.venv\Scripts\python.exe -m unittest backend.test_backend -v
```

The tests verify room classification against the sample floor plan, the
no-text / no-label honesty rules, and that dimensions are not calculated
without a scale.

## Stop the application

In each terminal, press:

```text
Ctrl + C
```

Press it a second time if it asks for confirmation, then close the window.

## Files in this project

| File / folder         | Purpose                                                        |
| --------------------- | -------------------------------------------------------------- |
| `extractor.py`        | Shared PyMuPDF extraction logic (text, pages, previews, vectors) |
| `backend/main.py`     | FastAPI server: `/api/health`, `/api/extract`                  |
| `frontend/`           | Next.js app: UI + `/api/extract` local extraction route        |
| `app.py`              | Original Streamlit UI (optional)                               |
| `requirements.txt`    | Python packages (`streamlit`, `pymupdf`, `fastapi`, `uvicorn`, `python-multipart`, `Pillow`) |
| `README.md`           | This file                                                      |
| `.gitignore`          | Ignores virtual environments, caches, `node_modules`, `.next`  |

## Troubleshooting

- **"Could not start Python"** or **"Could not reach the Python backend"** in
  the browser - the project's `.venv` is missing or its dependencies are not
  installed (see Setup above), or you set `PYTHON_API_URL` but the FastAPI
  server is not running. By default no separate backend is needed.
- **`uvicorn` is not recognized** - only relevant for the optional HTTP
  backend. Activate the virtual environment, or use
  `.\.venv\Scripts\uvicorn.exe backend.main:app ...`.
- **`npm run dev` fails** - run `npm install` inside `frontend/` first.
- **Port 3000 or 8000 already in use** - stop the other program, or run
  `uvicorn backend.main:app --port 8001` and set
  `$env:PYTHON_API_URL = "http://127.0.0.1:8001"` before `npm run dev`.
- **"This does not appear to be a PDF"** - the file is not a PDF or its header
  is missing. Re-export the drawing as PDF from your CAD tool.
- **"The file could not be opened as a PDF"** - the file is damaged or
  incomplete. Try re-exporting it.
- **"This PDF is password protected"** - type the password in the password
  field and extract again.
- **Zero text on every page** - install Tesseract OCR with English (`eng`)
  language data, ensure it is on `PATH`, and restart the app. If OCR still
  finds no text, use a clearer scan or re-export with selectable labels.
- **Processing is slow on a huge PDF** - lower the preview DPI, reduce the
  maximum preview pages, or untick *Compute vector statistics*.

## Limitations

- Room detection is **text-label based**. Scanned labels are read with
  Tesseract OCR when available; verify OCR output because recognition can
  be imperfect.
- Room areas are only calculated when an explicit drawing scale is present
  in the PDF text. Outer/overall dimensions do not establish each room's
  area.
- Geometry (vector rectangles and experimental image segmentation) is only
  used to associate an already detected label with a drawing region; it
  never creates rooms by itself and is not claimed to be reliable.
- Doors and windows are counted from an opening schedule (e.g. a
  "SCHEDULE OF OPENINGS" table with D1/W1 IDs), opening tags placed on the
  drawing (each D1/W1 occurrence is one opening), or readable
  "DOOR"/"WINDOW" text labels, in that priority order. Door arcs, window
  glyphs and completely unlabeled openings are not detected.
- Vector statistics are counts of paths and segments as reported by PyMuPDF,
  not an interpretation of the drawing's geometry.
- Very large drawings use more memory while rendering previews; the preview
  limits (DPI, maximum preview pages) exist to keep memory use reasonable.
- Text is extracted exactly as stored in the PDF, including line breaks and
  spacing, so complex tables may look messy.
- The backend binds to `127.0.0.1` only and CORS is restricted to
  `http://localhost:3000`; it is not meant to be exposed to a network.
