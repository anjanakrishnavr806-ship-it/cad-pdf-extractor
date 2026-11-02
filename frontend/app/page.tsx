"use client";

import { useCallback, useRef, useState } from "react";
import type {
  ApiError,
  ExtractResponse,
  PageInfo,
  Room,
} from "@/lib/types";

type Status = "idle" | "uploading" | "processing" | "done" | "error";

const MAX_UPLOAD_MB = 50;

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function downloadJson(payload: string, filename: string) {
  const blob = new Blob([payload], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const stem = filename.replace(/\.pdf$/i, "") || "extracted";
  link.download = `${stem}_analysis.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function dimensionText(room: Room): string {
  if (room.width_mm === null || room.height_mm === null) return "—";
  return `${room.width_mm} × ${room.height_mm} mm`;
}

function statusText(room: Room): string {
  const parts: string[] = [room.confidence];
  if (room.detection === "inferred") parts.push("inferred");
  if (room.dimension_status === "calculated") parts.push("scaled");
  else parts.push("no scale");
  return parts.join(" · ");
}

function SummaryCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition duration-200 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-md">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-slate-500">
          {label}
        </p>
        <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500/80" />
      </div>
      <p className="mt-2 text-3xl font-semibold tracking-tight text-slate-900">
        {value}
      </p>
      {hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

function CollapsibleSection({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-6 py-4 text-left transition hover:bg-slate-50 sm:px-8"
      >
        <span className="font-semibold text-slate-900">{title}</span>
        <span className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-100 text-sm text-slate-600 transition hover:bg-slate-200">
          {open ? "−" : "+"}
        </span>
      </button>
      {open && (
        <div className="border-t border-slate-100 p-6 sm:p-8">{children}</div>
      )}
    </div>
  );
}

function PageCard({
  page,
  preview,
  dpi,
}: {
  page: PageInfo;
  preview?: string;
  dpi: number;
}) {
  const [open, setOpen] = useState(page.page_number === 1);
  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left hover:bg-slate-50 rounded-xl"
      >
        <span className="font-medium text-slate-800">
          Page {page.page_number}
        </span>
        <span className="text-sm text-slate-500">
          {page.width_mm} × {page.height_mm} mm · {page.text_characters} text
          chars
          {page.vector_paths !== null ? ` · ${page.vector_paths} paths` : ""}
        </span>
        <span className="text-slate-400">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="grid gap-4 border-t border-slate-100 p-4 md:grid-cols-2">
          <div>
            {preview ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={preview}
                alt={`Preview of page ${page.page_number}`}
                className="w-full rounded-lg border border-slate-200 bg-white"
              />
            ) : (
              <p className="rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-500">
                No preview for this page.
              </p>
            )}
            <p className="mt-2 text-xs text-slate-500">
              Rendered at {dpi} DPI · {page.width_points} × {page.height_points}{" "}
              pt · rotation {page.rotation_degrees}°
            </p>
          </div>
          <div>
            {page.text_characters === 0 ? (
              <p className="rounded-lg bg-slate-50 p-4 text-sm text-slate-600">
                No selectable text on this page.
              </p>
            ) : (
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 font-mono text-xs text-slate-700">
                {page.text}
              </pre>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [previewDpi, setPreviewDpi] = useState(100);
  const [maxPreviewPages, setMaxPreviewPages] = useState(10);
  const [countVectors, setCountVectors] = useState(true);
  const [status, setStatus] = useState<Status>("idle");
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<ExtractResponse | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [rawTextOpen, setRawTextOpen] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [selectionNote, setSelectionNote] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const selectFiles = useCallback((files: FileList | null) => {
    const list = files ? Array.from(files) : [];
    setResult(null);
    setError(null);
    setStatus("idle");
    setStatusMessage("");
    setSelectionNote("");

    if (list.length === 0) {
      setFile(null);
      return;
    }

    // PDFs are analysed one at a time; the first PDF in the selection is used.
    const chosen = list.find(
      (f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf"
    );
    if (!chosen) {
      setError({
        error: "Unsupported file: please choose a PDF document.",
        status: 400,
      });
      setFile(null);
      return;
    }
    if (chosen.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError({
        error: `The file is larger than ${MAX_UPLOAD_MB} MB. Please use a smaller PDF.`,
        status: 413,
      });
      setFile(null);
      return;
    }

    setFile(chosen);
    if (list.length > 1) {
      setSelectionNote(
        `You selected ${list.length} files. PDFs are analysed one at a time, so only "${chosen.name}" will be used.`
      );
    }
  }, []);

  const onFileChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      selectFiles(event.target.files);
      // Allow choosing the same file again later.
      event.target.value = "";
    },
    [selectFiles]
  );

  const onDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setIsDragging(false);
      selectFiles(event.dataTransfer.files);
    },
    [selectFiles]
  );

  const processFile = useCallback(async () => {
    if (!file) return;
    setError(null);
    setResult(null);
    setPreviewOpen(false);
    setRawTextOpen(false);
    setDiagnosticsOpen(false);
    setStatus("uploading");
    setStatusMessage("Uploading PDF to the local backend…");

    const form = new FormData();
    form.append("file", file, file.name);
    if (password) form.append("password", password);
    form.append("preview_dpi", String(previewDpi));
    form.append("max_preview_pages", String(maxPreviewPages));
    form.append("count_vectors", String(countVectors));

    try {
      setStatus("processing");
      setStatusMessage("Extracting text and analyzing rooms…");
      const response = await fetch("/api/extract", {
        method: "POST",
        body: form,
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body) {
        const apiError: ApiError = {
          error: body?.error ?? `Upload failed (HTTP ${response.status}).`,
          code: body?.code ?? null,
          status: response.status,
        };
        setError(apiError);
        setStatus("error");
        setStatusMessage("");
        return;
      }
      setResult(body as ExtractResponse);
      // With no selectable text the only useful output is the drawing image.
      setPreviewOpen((body as ExtractResponse).analysis?.status === "no_text");
      setStatus("done");
      setStatusMessage("");
    } catch {
      setError({
        error:
          "Network error while contacting the local API. Check that both servers are running.",
        status: 0,
      });
      setStatus("error");
      setStatusMessage("");
    }
  }, [file, password, previewDpi, maxPreviewPages, countVectors]);

  const reset = useCallback(() => {
    setFile(null);
    setResult(null);
    setError(null);
    setStatus("idle");
    setStatusMessage("");
    setSelectionNote("");
    setPassword("");
    if (inputRef.current) inputRef.current.value = "";
  }, []);

  const busy = status === "uploading" || status === "processing";
  const needsPassword = error?.status === 401;
  const analysis = result?.analysis;

  return (
    <main
      className="mx-auto max-w-6xl px-4 py-10 sm:px-6 lg:px-8"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => event.preventDefault()}
    >
      <header className="mb-8 border-b border-slate-200/80 pb-7">
        <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white/80 px-3 py-1 text-xs font-medium text-slate-600 shadow-sm backdrop-blur">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
          Local · private · files stay on this machine
        </span>
        <h1 className="mt-4 text-3xl font-bold tracking-tight text-slate-900 sm:text-4xl">
          CAD PDF Extractor
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-slate-600">
          Upload a CAD PDF floor plan to extract room labels, drawing previews
          and technical statistics.
        </p>
      </header>

      <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <div
          role="button"
          tabIndex={0}
          aria-label="Choose or drop a PDF drawing"
          onClick={() => inputRef.current?.click()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              inputRef.current?.click();
            }
          }}
          onDragEnter={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
            setIsDragging(true);
          }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setIsDragging(false);
            }
          }}
          onDrop={onDrop}
          className={`group flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed px-6 py-10 text-center outline-none transition focus-visible:ring-2 focus-visible:ring-slate-400 ${
            isDragging
              ? "border-slate-500 bg-slate-100"
              : "border-slate-300 bg-slate-50/60 hover:border-slate-400 hover:bg-slate-50"
          }`}
        >
          <span className="flex h-12 w-12 items-center justify-center rounded-full bg-slate-900 text-white shadow-sm transition duration-200 group-hover:scale-105">
            <svg
              className="h-5 w-5"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M12 16V4" />
              <path d="m7 9 5-5 5 5" />
              <path d="M20 16v3a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-3" />
            </svg>
          </span>
          <span className="mt-4 text-sm font-semibold text-slate-800">
            {file ? file.name : "Choose a PDF drawing"}
          </span>
          <span className="mt-1 text-xs text-slate-500">
            {file
              ? `${humanSize(file.size)} — click to change`
              : `PDF only · max ${MAX_UPLOAD_MB} MB · drag \u2019n\u2019 drop or click`}
          </span>
          <input
            id="pdf-upload"
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            multiple
            className="hidden"
            onChange={onFileChange}
          />
        </div>
        {selectionNote && (
          <p className="mt-2 text-xs text-amber-700">{selectionNote}</p>
        )}

        <div className="mt-5 grid gap-4 rounded-2xl border border-slate-200 bg-slate-50/60 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label
              htmlFor="password"
              className="block text-xs font-medium uppercase tracking-wide text-slate-500"
            >
              PDF password (optional)
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Only if protected"
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div>
            <label
              htmlFor="dpi"
              className="block text-xs font-medium uppercase tracking-wide text-slate-500"
            >
              Preview quality (DPI): {previewDpi}
            </label>
            <input
              id="dpi"
              type="range"
              min={72}
              max={200}
              step={8}
              value={previewDpi}
              onChange={(e) => setPreviewDpi(Number(e.target.value))}
              className="mt-3 w-full"
            />
          </div>
          <div>
            <label
              htmlFor="max-pages"
              className="block text-xs font-medium uppercase tracking-wide text-slate-500"
            >
              Max preview pages (0 = all)
            </label>
            <input
              id="max-pages"
              type="number"
              min={0}
              max={500}
              value={maxPreviewPages}
              onChange={(e) =>
                setMaxPreviewPages(
                  Math.max(0, Math.min(500, Number(e.target.value) || 0))
                )
              }
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-slate-500 focus:outline-none"
            />
          </div>
          <div className="flex items-end pb-1">
            <label className="flex items-center gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={countVectors}
                onChange={(e) => setCountVectors(e.target.checked)}
                className="h-4 w-4 rounded border-slate-300 accent-slate-900"
              />
              Compute vector statistics
            </label>
          </div>
        </div>

        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={processFile}
            disabled={!file || busy}
            className="rounded-xl bg-slate-900 px-6 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-slate-700 hover:shadow-md disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "Processing…" : "Analyze floor plan"}
          </button>
          {(file || result || error) && (
            <button
              type="button"
              onClick={reset}
              className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-medium text-slate-600 transition hover:bg-slate-50"
            >
              Reset
            </button>
          )}
          {busy && (
            <span className="flex items-center gap-2 text-sm text-slate-500">
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-700" />
              {statusMessage}
            </span>
          )}
        </div>

        {needsPassword && (
          <p className="mt-3 rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-800">
            This PDF is password protected. Enter the password above and try
            again.
          </p>
        )}
        {error && error.status !== 401 && (
          <p className="mt-3 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700">
            {error.error}
          </p>
        )}
      </section>

      {result && analysis && (
        <section className="mt-8 space-y-6">
          {/* ---------- File header + JSON download ---------- */}
          <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h2 className="text-xl font-semibold text-slate-900">
                  {result.filename}
                </h2>
                <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-600">
                  <span className="rounded-full bg-slate-100 px-3 py-1 font-medium">
                    {result.document.pdf_format}
                  </span>
                  <span className="rounded-full bg-slate-100 px-3 py-1 font-medium">
                    {humanSize(result.file_size_bytes)}
                  </span>
                  <span className="rounded-full bg-slate-100 px-3 py-1 font-medium">
                    {result.summary.page_count}{" "}
                    {result.summary.page_count === 1 ? "page" : "pages"}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => downloadJson(result.json_payload, result.filename)}
                className="rounded-xl bg-emerald-600 px-5 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-emerald-500 hover:shadow-md"
              >
                Download JSON
              </button>
            </div>

            {/* Analysis status banner */}
            <div
              className={`mt-6 rounded-xl border px-4 py-3 text-sm font-medium ${
                analysis.status === "ok"
                  ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                  : "border-amber-200 bg-amber-50 text-amber-800"
              }`}
            >
              {analysis.message}
              {!analysis.classification_reliable && analysis.status === "ok" && (
                <p className="mt-1">
                  Room classification could not be reliably determined from
                  explicit labels alone.
                </p>
              )}
            </div>
          </div>

          {/* ---------- Summary cards ---------- */}
          <div>
            <div className="mb-3 flex items-center gap-2">
              <h3 className="text-lg font-semibold text-slate-900">
                Room summary
              </h3>
              <span className="rounded-full bg-slate-900 px-2 py-0.5 text-xs font-semibold text-white">
                {analysis.summary.total_rooms}
              </span>
            </div>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <SummaryCard
                label="Total rooms"
                value={analysis.summary.total_rooms}
                hint={`${analysis.summary.explicit_labels} explicit · ${analysis.summary.inferred_labels} inferred`}
              />
              <SummaryCard
                label="Bedrooms"
                value={analysis.summary.bedrooms}
              />
              <SummaryCard label="Kitchens" value={analysis.summary.kitchens} />
              <SummaryCard
                label="Living rooms"
                value={analysis.summary.living_rooms}
              />
              <SummaryCard
                label="Bathrooms"
                value={analysis.summary.bathrooms}
              />
              <SummaryCard
                label="Other rooms"
                value={analysis.summary.other_rooms}
              />
              <SummaryCard
                label="Doors"
                value={
                  analysis.summary.doors.count !== null
                    ? analysis.summary.doors.count
                    : "—"
                }
                hint={
                  analysis.summary.doors.status === "not_detected"
                    ? "not reliably detectable"
                    : "from text labels"
                }
              />
              <SummaryCard
                label="Windows"
                value={
                  analysis.summary.windows.count !== null
                    ? analysis.summary.windows.count
                    : "—"
                }
                hint={
                  analysis.summary.windows.status === "not_detected"
                    ? "not reliably detectable"
                    : "from text labels"
                }
              />
            </div>
          </div>

          {/* ---------- Room details table ---------- */}
          <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
            <div className="flex items-center gap-2">
              <h3 className="text-lg font-semibold text-slate-900">
                Room details
              </h3>
              {analysis.rooms.length > 0 && (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600">
                  {analysis.rooms.length}
                </span>
              )}
            </div>
            {analysis.rooms.length === 0 ? (
              <p className="mt-3 text-sm text-slate-600">
                No rooms were detected. {analysis.message}
              </p>
            ) : (
              <div className="mt-4 overflow-x-auto rounded-xl border border-slate-200">
                <table className="w-full text-left text-sm">
                  <thead className="bg-slate-50">
                    <tr className="text-xs uppercase tracking-wide text-slate-500">
                      <th className="px-4 py-3 font-semibold">Room name</th>
                      <th className="px-4 py-3 font-semibold">Type</th>
                      <th className="px-4 py-3 font-semibold">Page</th>
                      <th className="px-4 py-3 font-semibold">Dimensions</th>
                      <th className="px-4 py-3 font-semibold">Area</th>
                      <th className="px-4 py-3 font-semibold">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {analysis.rooms.map((room, index) => (
                      <tr
                        key={room.id}
                        className={`transition hover:bg-slate-50 ${
                          index % 2 === 1 ? "bg-slate-50/40" : ""
                        }`}
                      >
                        <td className="px-4 py-3 font-medium text-slate-800">
                          {room.label}
                          {room.combined_label && (
                            <span className="ml-1 text-xs text-amber-600">
                              (combined)
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {room.type_label}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {room.page}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {dimensionText(room)}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {room.area_m2 !== null
                            ? `${room.area_m2} m²`
                            : "—"}
                        </td>
                        <td className="px-4 py-3 text-slate-600">
                          {statusText(room)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Notes / honesty rules */}
            {analysis.notes.length > 0 && (
              <ul className="mt-4 list-disc space-y-1 pl-5 text-xs text-slate-500">
                {analysis.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            )}
          </div>

          {/* ---------- Collapsible: drawing preview ---------- */}
          <CollapsibleSection
            title="Drawing preview"
            open={previewOpen}
            onToggle={() => setPreviewOpen((v) => !v)}
          >
            <div className="space-y-3">
              {result.pages.map((page) => {
                const previewEntry = result.previews.find(
                  (p) => p.page_number === page.page_number
                );
                const preview = previewEntry
                  ? `data:image/png;base64,${previewEntry.png_base64}`
                  : undefined;
                return (
                  <PageCard
                    key={page.page_number}
                    page={page}
                    preview={preview}
                    dpi={result.preview_dpi}
                  />
                );
              })}
            </div>
          </CollapsibleSection>

          {/* ---------- Collapsible: raw text ---------- */}
          <CollapsibleSection
            title="Raw extracted text"
            open={rawTextOpen}
            onToggle={() => setRawTextOpen((v) => !v)}
          >
            {result.summary.pages_with_selectable_text === 0 ? (
              <p className="rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-800">
                No selectable text was found on any page. OCR is currently
                unavailable in this app — please try a text-based CAD PDF with
                selectable room labels.
              </p>
            ) : (
              <div className="space-y-4">
                {result.pages.map((page) => (
                  <div key={page.page_number}>
                    <p className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">
                      Page {page.page_number} ({page.text_characters} characters)
                    </p>
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 font-mono text-xs text-slate-700">
                      {page.text || "(empty)"}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </CollapsibleSection>

          {/* ---------- Collapsible: technical diagnostics ---------- */}
          <CollapsibleSection
            title="Technical diagnostics"
            open={diagnosticsOpen}
            onToggle={() => setDiagnosticsOpen((v) => !v)}
          >
            <dl className="grid gap-x-8 gap-y-2 text-sm sm:grid-cols-2">
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Pages analyzed</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.pages_analyzed}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Pages with room labels</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.pages_with_room_labels}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Pages with embedded images</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.pages_with_embedded_images}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Text lines scanned</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.text_lines_scanned}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Room label candidates</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.room_label_candidates}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Duplicate labels merged</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.duplicate_labels_merged}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Vector regions matched</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.vector_regions_matched}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Image regions (experimental)</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.image_regions_detected} detected ·{" "}
                  {analysis.diagnostics.image_regions_matched} matched
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Drawing scale</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.scale.found ? analysis.scale.text : "not found"}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Vector paths (total)</dt>
                <dd className="font-medium text-slate-800">
                  {result.summary.total_vector_paths !== null
                    ? result.summary.total_vector_paths.toLocaleString()
                    : "n/a"}
                </dd>
              </div>
              <div className="flex justify-between border-b border-slate-100 py-1">
                <dt className="text-slate-500">Analysis time</dt>
                <dd className="font-medium text-slate-800">
                  {analysis.diagnostics.analysis_ms} ms
                </dd>
              </div>
            </dl>
            <p className="mt-4 text-xs text-slate-500">
              Region method: {analysis.diagnostics.region_method}. Image-based
              region detection is experimental and is never claimed to be
              reliable.
            </p>
          </CollapsibleSection>
        </section>
      )}

      {!result && !busy && (
        <section className="mt-8 grid gap-4 md:grid-cols-2">
          <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
            <h3 className="font-semibold text-slate-800">What it does</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-600">
              <li>Detects room labels from selectable PDF text.</li>
              <li>Classifies rooms: bedrooms, kitchens, living rooms, bathrooms, other.</li>
              <li>Merges duplicate nearby labels so one room is not counted twice.</li>
              <li>Associates labels with nearby drawing regions when possible.</li>
              <li>Downloads structured results as JSON.</li>
            </ul>
          </div>
          <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
            <h3 className="font-semibold text-slate-800">What it does not do</h3>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-slate-600">
              <li>
                Does not calculate room areas without an explicit drawing scale.
              </li>
              <li>
                Outer/overall drawing dimensions do not establish each room&apos;s
                area.
              </li>
              <li>
                No OCR support — image-only or scanned drawings cannot be
                classified; try a text-based CAD PDF.
              </li>
              <li>Vector paths are never counted as rooms.</li>
            </ul>
          </div>
        </section>
      )}
    </main>
  );
}
