import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

const MAX_UPLOAD_MB = 50;
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;
const EXTRACT_TIMEOUT_MS = 120_000;

function errorResponse(error: string, status: number, code?: string) {
  return NextResponse.json({ error, code: code ?? null }, { status });
}

function projectRoot(): string {
  return path.resolve(process.cwd(), "..");
}

function pythonExecutable(root: string): string {
  if (process.env.PYTHON_EXECUTABLE) return process.env.PYTHON_EXECUTABLE;
  const candidates =
    process.platform === "win32"
      ? [path.join(root, ".venv", "Scripts", "python.exe")]
      : [path.join(root, ".venv", "bin", "python")];
  return candidates.find(existsSync) ?? (process.platform === "win32" ? "python" : "python3");
}

interface ExtractionResult {
  status: number;
  body: unknown;
}

function runLocalExtraction(
  file: File,
  options: {
    password: string;
    previewDpi: string;
    maxPreviewPages: string;
    countVectors: string;
  }
): Promise<ExtractionResult> {
  const root = projectRoot();
  const cliPath = path.join(root, "backend", "cli.py");
  if (!existsSync(cliPath)) {
    return Promise.resolve({
      status: 500,
      body: { error: "Backend script not found (backend/cli.py).", code: "backend_missing" },
    });
  }

  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: ExtractionResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const child = spawn(pythonExecutable(root), [cliPath], {
      cwd: root,
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        CAD_FILENAME: file.name,
        CAD_PASSWORD: options.password,
        CAD_PREVIEW_DPI: options.previewDpi,
        CAD_MAX_PREVIEW_PAGES: options.maxPreviewPages,
        CAD_COUNT_VECTORS: options.countVectors,
      },
    });

    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => (stdout += chunk.toString("utf8")));
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString("utf8")));
    child.stdin.on("error", () => {
      // The child may exit before we finish writing; the close handler reports it.
    });

    const timer = setTimeout(() => {
      child.kill();
      finish({
        status: 504,
        body: {
          error: "The extraction timed out. Try a smaller PDF or lower the preview settings.",
          code: "timeout",
        },
      });
    }, EXTRACT_TIMEOUT_MS);

    child.on("error", (err) => {
      finish({
        status: 502,
        body: {
          error:
            "Could not start Python. Make sure the .venv exists and the backend dependencies are installed " +
            `(${err.message}).`,
          code: "backend_unreachable",
        },
      });
    });

    child.on("close", () => {
      let parsed: unknown = null;
      try {
        parsed = stdout ? JSON.parse(stdout) : null;
      } catch {
        parsed = null;
      }
      if (!parsed || typeof parsed !== "object") {
        finish({
          status: 502,
          body: {
            error:
              "The Python extractor returned an unreadable response. Check the terminal running npm run dev." +
              (stderr ? `\n${stderr.trim().split("\n").slice(-3).join("\n")}` : ""),
            code: "backend_error",
          },
        });
        return;
      }
      const data = parsed as { status?: number };
      finish({ status: typeof data.status === "number" ? data.status : 200, body: parsed });
    });

    file
      .arrayBuffer()
      .then((buffer) => {
        child.stdin.write(Buffer.from(buffer));
        child.stdin.end();
      })
      .catch((err: Error) => {
        child.kill();
        finish({
          status: 400,
          body: { error: `Could not read the uploaded file: ${err.message}`, code: "read_error" },
        });
      });
  });
}

async function proxyToBackend(file: File, form: FormData, backendUrl: string): Promise<ExtractionResult> {
  const forward = new FormData();
  forward.append("file", file, file.name);
  const password = form.get("password");
  if (typeof password === "string") forward.append("password", password);
  const previewDpi = form.get("preview_dpi");
  if (typeof previewDpi === "string" && previewDpi) forward.append("preview_dpi", previewDpi);
  const maxPreviewPages = form.get("max_preview_pages");
  if (typeof maxPreviewPages === "string" && maxPreviewPages !== "")
    forward.append("max_preview_pages", maxPreviewPages);
  const countVectors = form.get("count_vectors");
  if (typeof countVectors === "string") forward.append("count_vectors", countVectors);

  try {
    const response = await fetch(`${backendUrl}/api/extract`, {
      method: "POST",
      body: forward,
      signal: AbortSignal.timeout(EXTRACT_TIMEOUT_MS),
    });
    const body = await response.json().catch(() => null);
    return { status: response.status, body };
  } catch (err) {
    const message =
      err instanceof Error && err.name === "TimeoutError"
        ? "The extraction timed out. Try a smaller PDF or lower the preview settings."
        : `Could not reach the Python backend at ${backendUrl}.`;
    return { status: 502, body: { error: message, code: "backend_unreachable" } };
  }
}

export async function POST(request: NextRequest) {
  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return errorResponse("Invalid upload. Please send the PDF as multipart form data.", 400);
  }

  const file = form.get("file");
  if (!(file instanceof File)) {
    return errorResponse("No PDF file was provided.", 400);
  }
  if (file.size === 0) {
    return errorResponse("The uploaded file is empty. Please choose a PDF file.", 400);
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    return errorResponse(
      `The file is larger than ${MAX_UPLOAD_MB} MB. Please use a smaller PDF.`,
      413
    );
  }

  const name = file.name.toLowerCase();
  const type = file.type;
  if (!name.endsWith(".pdf") && type !== "application/pdf") {
    return errorResponse(
      "Unsupported file: this does not appear to be a PDF document.",
      400
    );
  }

  // Cheap magic-byte check before handing the file to Python.
  const head = new Uint8Array(await file.slice(0, 1024).arrayBuffer());
  let sawPdfHeader = false;
  for (let i = 0; i + 3 < head.length; i++) {
    if (head[i] === 0x25 && head[i + 1] === 0x50 && head[i + 2] === 0x44 && head[i + 3] === 0x46) {
      sawPdfHeader = true;
      break;
    }
  }
  if (!sawPdfHeader) {
    return errorResponse(
      "Unsupported file: this does not appear to be a PDF document (missing %PDF header).",
      400
    );
  }

  const backendUrl = process.env.PYTHON_API_URL;
  const result = backendUrl
    ? await proxyToBackend(file, form, backendUrl)
    : await runLocalExtraction(file, {
        password: typeof form.get("password") === "string" ? String(form.get("password")) : "",
        previewDpi: String(form.get("preview_dpi") ?? ""),
        maxPreviewPages: String(form.get("max_preview_pages") ?? ""),
        countVectors: String(form.get("count_vectors") ?? ""),
      });

  if (!result.body) {
    return errorResponse("The Python extractor returned an unreadable response.", 502);
  }
  return NextResponse.json(result.body, { status: result.status });
}
