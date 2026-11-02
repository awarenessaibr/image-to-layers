#!/usr/bin/env python3
"""
image-to-layers-server.py — zero-dependency HTTP wrapper around
tools/image-to-layers.py.

Serves:
  GET  /image-to-layers.html                 the UI
  GET  /_.css                                brand styles (if in root)
  GET  /data/...                             shared with the studio backend
  GET  /api/sample                           a sample reference if one exists
  POST /api/run                              run the CLI, stream NDJSON progress
  POST /api/cancel                           terminate the active run

The POST body can also recover missing layers: pass `mode: "recover"`, the
full reference, and an aligned studio `export` whose holes mark the missing
regions. The CLI cuts those regions from the reference and writes a composition
with the export as its visible base layer.

The Python CLI writes directly into <root>/data/projects/, so once it
completes, both this tool and the running studio's project picker show
the new composition — no copying required.

Usage:
  python3 image-to-layers-server.py
  PORT=8008 HOST=127.0.0.1 python3 image-to-layers-server.py

Changed since the original:

  * Pre-stream validation errors now return proper 400/500 JSON instead of
    writing raw NDJSON bytes without headers.
  * projectDir / outputPath are derived from the composition slug instead of
    a lossy "name.replace(' ')", so they actually point at the real files.
  * New POST /api/cancel endpoint; also terminates the child process on
    client disconnect.
  * MAX_BODY raised to 200 MB for large 4K references.
  * Data-URL MIME is validated against image/* before writing to disk.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "tools" / "image-to-layers.py"
DATA_DIR = ROOT / "data"
PROJECTS_DIR = DATA_DIR / "projects"
DOWNLOADS_DIR = DATA_DIR / "downloads"
SAMPLES_DIR = DATA_DIR / "imports"

PORT = int(os.environ.get("PORT", "8009"))
HOST = os.environ.get("HOST", "127.0.0.1")
MAX_BODY = 200 * 1024 * 1024  # 200 MB — generous for 4K references + masks

RUN_LOCK = threading.Lock()
RUNNING_PROC: subprocess.Popen | None = None


# ── decoding helpers ─────────────────────────────────────────────────────

ALLOWED_REFERENCE_MIMES = {
    "image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif",
}
ALLOWED_MASK_SUFFIXES = {".png", ".webp", ".tif", ".tiff"}


def decode_data_url(value: str, label: str) -> bytes:
    if not isinstance(value, str) or "," not in value:
        raise ValueError(f"{label}: missing data URL")
    header, payload = value.split(",", 1)
    if not header.startswith("data:"):
        raise ValueError(f"{label}: not a data URL")
    mime = header[5:].split(";", 1)[0].strip().lower()
    if mime and mime not in ALLOWED_REFERENCE_MIMES:
        raise ValueError(f"{label}: unsupported MIME {mime!r}")
    try:
        return base64.b64decode(payload)
    except Exception as exc:
        raise ValueError(f"{label}: invalid base64 ({exc})") from exc


def guess_extension(name: str, default: str = ".png") -> str:
    suffix = Path(name or "").suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return suffix
    return default


def find_sample_reference() -> dict | None:
    """Return the most recent reference.* under data/imports/ if any."""
    if not SAMPLES_DIR.is_dir():
        return None
    candidates = []
    for project in SAMPLES_DIR.iterdir():
        if not project.is_dir():
            continue
        for name in ("reference.png", "reference.jpg", "reference.jpeg", "reference.webp"):
            file = project / name
            if file.is_file():
                candidates.append(file)
                break
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    best = candidates[0]
    rel = best.relative_to(ROOT).as_posix()
    return {
        "url": f"/{rel}",
        "name": f"{best.parent.name}",
    }


# ── request body ────────────────────────────────────────────────────────

def read_json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        raise ValueError("Empty body")
    if length > MAX_BODY:
        raise ValueError(f"Body too large ({length} > {MAX_BODY})")
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Body is not valid JSON: {exc}") from exc


# ── CLI invocation ──────────────────────────────────────────────────────

def build_command(payload: dict, tmpdir: Path) -> tuple[list[str], Path]:
    """Write the input files, return (argv, expected_composition_dir)."""
    if "reference" not in payload:
        raise ValueError("Missing `reference` data URL")
    reference_bytes = decode_data_url(payload["reference"], "reference")
    reference_name = payload.get("referenceName") or "reference.png"
    reference_path = tmpdir / f"reference{guess_extension(reference_name)}"
    reference_path.write_bytes(reference_bytes)

    argv = [
        sys.executable,
        str(TOOL),
        "--reference", str(reference_path),
        "--name", str(payload.get("name") or "My Scene"),
        "--out", str(PROJECTS_DIR),
        "--feather", str(float(payload.get("feather", 1.0))),
        "--dilate", str(int(payload.get("dilate", 0))),
    ]
    if payload.get("samModel"):
        argv += ["--sam-model", str(payload["samModel"])]
    if payload.get("pointRadius"):
        argv += ["--point-radius", str(float(payload["pointRadius"]))]
    if payload.get("depth"):
        argv.append("--depth")
    if payload.get("depthRole"):
        argv.append("--depth-role")

    mode = payload.get("mode") or "sam-points"
    if mode == "sam-points":
        sam_points = str(payload.get("samPoints") or "").strip()
        if not sam_points:
            raise ValueError("Point-prompt mode requires at least one point per layer")
        argv += ["--sam-points", sam_points]
    elif mode == "masks":
        masks = payload.get("masks") or []
        if not masks:
            raise ValueError("Upload at least one mask")
        masks_dir = tmpdir / "masks"
        masks_dir.mkdir(parents=True, exist_ok=True)
        for i, item in enumerate(masks):
            name = (item.get("name") or f"mask-{i + 1}").strip()
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or f"mask-{i + 1}"
            data_url = item.get("dataUrl") or item.get("data")
            if not data_url:
                raise ValueError(f"Mask {i + 1} is missing image data.")
            data = decode_data_url(data_url, f"mask {i + 1}")
            (masks_dir / f"{safe}.png").write_bytes(data)
        argv += ["--masks", str(masks_dir)]
    elif mode == "recover":
        export_value = payload.get("export")
        if not export_value:
            raise ValueError("Recover mode requires the aligned studio export")
        export_name = payload.get("exportName") or "export.png"
        export_path = tmpdir / f"export{guess_extension(export_name)}"
        export_path.write_bytes(decode_data_url(export_value, "export"))
        hole_source = str(payload.get("recoverHoleSource") or "auto").lower()
        if hole_source not in {"auto", "alpha", "white"}:
            raise ValueError(f"Unknown hole source {hole_source!r}")
        argv += [
            "--export", str(export_path),
            "--recover-holes", hole_source,
            "--recover-min-area", str(float(payload.get("recoverMinArea", 0.1))),
        ]
        if payload.get("recoverIgnoreBorder") is False:
            argv.append("--recover-keep-border")
    elif mode == "sam-auto":
        argv.append("--sam-auto")
    else:
        raise ValueError(f"Unknown mode {mode!r}")

    output_mode = (payload.get("outputMode") or "project").lower()
    if output_mode == "zip":
        argv += ["--mode", "zip"]
    return argv, PROJECTS_DIR


def parse_composition_output(stdout_lines: list[str]) -> dict | None:
    for line in stdout_lines:
        if "wrote " in line and "composition.json" in line:
            candidate = line.split("wrote ", 1)[1].strip()
            path = Path(candidate)
            if not path.is_absolute():
                path = ROOT / path
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None
    return None


def find_zip_output(stdout_lines: list[str]) -> Path | None:
    for line in stdout_lines:
        if "wrote " in line and line.strip().endswith(".zip"):
            candidate = line.split("wrote ", 1)[1].strip()
            path = Path(candidate)
            if not path.is_absolute():
                path = ROOT / path
            if path.is_file():
                return path
    return None


def find_preview_output(stdout_lines: list[str]) -> Path | None:
    for line in stdout_lines:
        if line.startswith("preview "):
            path = Path(line.split(" ", 1)[1].strip())
            if not path.is_absolute():
                path = ROOT / path
            if path.is_file():
                return path
    return None


def publish_preview(path: Path) -> str | None:
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        relative = None
    if relative and relative.parts[:1] == ("data",):
        return "/" + relative.as_posix()

    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS_DIR / path.name
    try:
        shutil.move(str(path), str(target))
    except Exception:
        shutil.copy2(str(path), str(target))
    return "/" + target.relative_to(ROOT).as_posix()


# ── handler ─────────────────────────────────────────────────────────────

class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        message = args[0] if args else fmt
        if isinstance(message, str) and "/api/" in message:
            super().log_message(fmt, *args)

    def do_GET(self):
        if self.path in ("", "/"):
            self.path = "/image-to-layers.html"
        elif self.path in ("/image-to-layers.html", "/image-to-layers-server.html"):
            self.path = "/image-to-layers.html"
        elif self.path.startswith("/api/sample"):
            self.send_json(200, find_sample_reference() or {})
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/run":
            self.handle_run()
        elif self.path == "/api/cancel":
            self.handle_cancel()
        else:
            self.send_error(404)

    # ── helpers ────────────────────────────────────────────────────────

    def send_json(self, status: int, value: dict):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_ndjson_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

    def emit(self, payload: dict):
        line = json.dumps(payload) + "\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._broken = True

    # ── cancel ─────────────────────────────────────────────────────────

    def handle_cancel(self):
        proc = RUNNING_PROC
        if proc is None or proc.poll() is not None:
            self.send_json(404, {"error": "No run in progress."})
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
            return
        self.send_json(200, {"ok": True})

    # ── main run ───────────────────────────────────────────────────────

    def handle_run(self):
        global RUNNING_PROC
        if not RUN_LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "Another run is already in progress."})
            return

        proc: subprocess.Popen | None = None
        tmpdir = Path(tempfile.mkdtemp(prefix="itl-"))
        headers_sent = False
        self._broken = False
        try:
            if not TOOL.is_file():
                raise RuntimeError(f"Missing CLI: {TOOL}")
            payload = read_json_body(self)
            argv, _ = build_command(payload, tmpdir)

            self.send_ndjson_headers()
            headers_sent = True
            started = time.time()
            self.emit({"type": "log", "message": "$ " + " ".join(argv[:5]) + " …"})

            env = os.environ.copy()
            env.setdefault("PYTHONUNBUFFERED", "1")
            proc = subprocess.Popen(
                argv, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
            RUNNING_PROC = proc
            stdout_lines: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                stdout_lines.append(line.rstrip("\n"))
                self.emit({"type": "log", "message": line.rstrip()})
                if self._broken:
                    # Client went away; stop the child promptly.
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    return
            returncode = proc.wait()
            elapsed_ms = int((time.time() - started) * 1000)

            if returncode != 0:
                self.emit({
                    "type": "error",
                    "message": f"CLI exited with status {returncode}. See the log above.",
                    "elapsedMs": elapsed_ms,
                })
                return

            composition = parse_composition_output(stdout_lines)
            zip_path = find_zip_output(stdout_lines) if payload.get("outputMode") == "zip" else None

            result = {
                "type": "done",
                "elapsedMs": elapsed_ms,
                "outputMode": payload.get("outputMode") or "project",
                "composition": composition,
            }

            for line in stdout_lines:
                match = re.search(r"recovered (\d+) missing region", line)
                if match:
                    result["recoverRegions"] = int(match.group(1))
                    break

            if zip_path and zip_path.is_file():
                DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
                target = DOWNLOADS_DIR / zip_path.name
                try:
                    shutil.move(str(zip_path), str(target))
                except Exception:
                    shutil.copy2(str(zip_path), str(target))
                result["zipUrl"] = "/" + target.relative_to(ROOT).as_posix()

            if composition and composition.get("id"):
                slug = str(composition["id"])
                project_dir = PROJECTS_DIR / slug
                if project_dir.is_dir():
                    result["projectDir"] = project_dir.relative_to(ROOT).as_posix()
                candidate = project_dir / "compositions" / f"{slug}.composition.json"
                if candidate.is_file():
                    result["outputPath"] = "/" + candidate.relative_to(ROOT).as_posix()

            preview_path = find_preview_output(stdout_lines)
            if preview_path:
                preview_url = publish_preview(preview_path)
                if preview_url:
                    result["previewUrl"] = preview_url

            self.emit(result)
        except (ValueError, KeyError) as exc:
            if headers_sent:
                self.emit({"type": "error", "message": str(exc)})
            else:
                try:
                    self.send_json(400, {"error": str(exc)})
                except Exception:
                    pass
        except Exception as exc:
            if headers_sent:
                self.emit({"type": "error", "message": str(exc)})
            else:
                try:
                    self.send_json(500, {"error": str(exc)})
                except Exception:
                    pass
        finally:
            RUNNING_PROC = None
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            shutil.rmtree(tmpdir, ignore_errors=True)
            RUN_LOCK.release()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)  # SimpleHTTPRequestHandler serves from CWD
    server = ThreadingServer((HOST, PORT), Handler)
    print(f"[itl] http://{HOST}:{PORT}/image-to-layers-server.html")
    print(f"[itl] project output → {PROJECTS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[itl] stopped")


if __name__ == "__main__":
    main()