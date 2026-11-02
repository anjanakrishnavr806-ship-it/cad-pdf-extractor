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
  bedrooms, kitchens, living rooms, bathrooms, and other rooms (dining,
  study, store, corridor). Explicit terms (BEDROOM, KITCHEN, LIVING ROOM,
  BATHROOM...) and abbreviations (BED RM, KIT...) are distinguished;
  abbreviations are reported as *inferred*.
- **Merges duplicate nearby labels** so one room is not counted twice
  (e.g. MASTER + BEDROOM on separate lines). Original label text and page
  number are always preserved.
- **Results dashboard**: summary cards (total rooms, bedrooms, kitchens,
  living rooms, bathrooms, other, doors/windows when labelled), plus a
  room-details table (name, type, page, dimensions, area, status).
- **Geometry association** (vector rectangles and experimental image
  segmentation) attaches an enclosing drawing region to an already
  detected label. Geometry alone never creates a room, and vector paths
  are never counted as rooms.
- **Dimensions and areas are only calculated** when an explicit drawing
  scale (e.g. "SCALE 1:100") is found. Outer/overall dimensions on a
  drawing never establish each room's individual area.
- Drawing preview, raw extracted text and technical diagnostics are in
  separate collapsible sections.
- Download all structured results (including the room analysis) as JSON.

## What it does NOT do yet

- It does **not** invent room counts, dimensions or areas. If a PDF has
  no readable labels, the app says room classification could not be
  reliably determined.
- It does **not** run OCR (**OCR is not supported** in this version).
  Drawings without selectable text (scans or image-only exports) get a
  message telling you that OCR is currently unavailable and to try a
  text-based CAD PDF.
- Image-based region detection is **experimental**; it is only used to
  help associate labels with regions and is never claimed to be reliable.
- Wall lengths, construction quantities and takeoffs are out of scope.

## Requirements

- Windows, macOS or Linux
- Python 3.9 or newer (tested with Python 3.14)
- Node.js 18.18 or newer (tested with Node.js 24) and npm
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
├── requirements.txt     # Python packages (streamlit, pymupdf, fastapi, uvicorn, python-multipart, numpy)
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
| `requirements.txt`    | Python packages (`streamlit`, `pymupdf`, `fastapi`, `uvicorn`, `python-multipart`) |
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
- **Zero text on every page** - the drawing is a scan or pure vector
  geometry. OCR is not supported in this version; re-export the plan as a
  CAD PDF with selectable text labels and try again.
- **Processing is slow on a huge PDF** - lower the preview DPI, reduce the
  maximum preview pages, or untick *Compute vector statistics*.

## Limitations

- Room detection is **text-label based**. Plans whose room names exist only
  as scanned pixels (no selectable text) cannot be classified; OCR is not
  supported in this version (the app asks you to try a text-based CAD PDF).
- Room areas are only calculated when an explicit drawing scale is present
  in the PDF text. Outer/overall dimensions do not establish each room's
  area.
- Geometry (vector rectangles and experimental image segmentation) is only
  used to associate an already detected label with a drawing region; it
  never creates rooms by itself and is not claimed to be reliable.
- Doors and windows are only counted when readable "DOOR"/"WINDOW" text
  labels exist; unlabeled openings are not detected.
- Vector statistics are counts of paths and segments as reported by PyMuPDF,
  not an interpretation of the drawing's geometry.
- Very large drawings use more memory while rendering previews; the preview
  limits (DPI, maximum preview pages) exist to keep memory use reasonable.
- Text is extracted exactly as stored in the PDF, including line breaks and
  spacing, so complex tables may look messy.
- The backend binds to `127.0.0.1` only and CORS is restricted to
  `http://localhost:3000`; it is not meant to be exposed to a network.
