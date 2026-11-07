#!/usr/bin/env python3
"""
image-to-layers-server.py — zero-dependency HTTP wrapper around
tools/image-to-layers.py.

Serves:
  GET  /image-to-layers.html                 the UI
  GET  /_.css                                brand styles (if in root)
  GET  /data/projects/...                    project assets shared with Studio
  GET  /api/sample                           a sample reference if one exists
  GET  /api/vision-models                    configured providers + installed Ollama vision models
  POST /api/run                              run the CLI, stream NDJSON progress
  POST /api/cancel                           terminate the active run

The POST body can also recover missing layers: pass `mode: "recover"`, the
full reference, and an aligned studio `export` whose holes mark the missing
regions. The CLI cuts those regions from the reference and writes a composition
with the export as its visible base layer.

The Python CLI writes directly into <studio-root>/data/projects/, so once it
completes, the running studio's project picker shows the new composition — no
copying required. Set STUDIO_ROOT if the Studio checkout is not a sibling.

Usage:
  python3 image-to-layers-server.py
  PORT=8009 HOST=0.0.0.0 python3 image-to-layers-server.py

Scene description (vision LLM):
  POST /api/describe   { reference, referenceName, visionModel, detail, hint }
                       → { scene: { description, objects: [...] } }
  DeepSeek uses DEEPSEEK_API_KEY; Ollama uses its local chat API at
  http://127.0.0.1:11434 by default. ITL_OLLAMA_BASE_URL and
  ITL_OLLAMA_API_KEY configure another Ollama-compatible endpoint.
  ITL_VISION_MODEL overrides the CLI default. Secrets remain server-side.

Changed since the original:

  * /api/describe + `sam-scene` mode + `scene` in the run payload.
  * Granularity levers (`auto*`, `scene*`, `folders`) are validated and clamped
    here before they reach the CLI.
  * The HTML file is found under either name (image-to-layers.html or
    image-to-layers-server.html) instead of a hard-coded one.

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
import urllib.parse
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STUDIO_ROOT = Path(os.environ.get("STUDIO_ROOT", ROOT.parent / "studio")).resolve()
TOOL = ROOT / "tools" / "image-to-layers.py"
DATA_DIR = ROOT / "data"
PROJECTS_DIR = STUDIO_ROOT / "data" / "projects"
DOWNLOADS_DIR = DATA_DIR / "downloads"
SAMPLES_DIR = DATA_DIR / "imports"

PORT = int(os.environ.get("PORT", "8009"))
HOST = os.environ.get("HOST", "0.0.0.0")
MAX_BODY = 200 * 1024 * 1024  # 200 MB — generous for 4K references + masks
VISION_SUBPROCESS_TIMEOUT = 660

RUN_LOCK = threading.Lock()
RUNNING_PROC: subprocess.Popen | None = None


def load_dotenv(folder: Path) -> None:
    """Load KEY=VALUE pairs from the nearest .env; real env vars win."""
    for base in [folder, *folder.parents]:
        env_file = base / ".env"
        if not env_file.is_file():
            continue
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if name.startswith("export "):
                name = name[len("export "):].strip()
            if name and name not in os.environ:
                os.environ[name] = value.strip().strip('"').strip("'")
        return


load_dotenv(ROOT)


def _is_ollama_vision_model(model: dict) -> bool:
    capabilities = model.get("capabilities")
    if isinstance(capabilities, list) and capabilities:
        return "vision" in {str(value).lower() for value in capabilities}
    details = model.get("details") or {}
    families = details.get("families") or []
    if isinstance(families, list) and "clip" in {str(value).lower() for value in families}:
        return True
    name = str(model.get("name") or "").lower()
    return bool(re.search(r"(vision|llava|moondream|minicpm-v|qwen[^ ]*vl|gemma[34])", name))


def get_vision_model_catalog() -> dict:
    """Return configured DeepSeek status and installed Ollama vision models."""
    deepseek_available = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    base_url = os.environ.get(
        "ITL_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
    ).strip()
    if "://" not in base_url:
        base_url = "http://" + base_url
    parsed = urllib.parse.urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        ollama = {
            "available": False,
            "models": [],
            "error": "ITL_OLLAMA_BASE_URL must be a valid HTTP(S) URL.",
        }
    else:
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        endpoint = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, path + "/api/tags", "", "")
        )
        request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
        api_key = os.environ.get("ITL_OLLAMA_API_KEY", "").strip()
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            installed = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(installed, list):
                raise ValueError("Ollama returned an invalid model list.")
            models = []
            for model in installed:
                if not isinstance(model, dict) or not _is_ollama_vision_model(model):
                    continue
                name = str(model.get("name") or "").strip()
                if not name:
                    continue
                models.append({
                    "id": name,
                    "parameterSize": str((model.get("details") or {}).get("parameter_size") or ""),
                })
            ollama = {
                "available": bool(models),
                "models": models,
                "error": None if models else "No installed Ollama vision models were detected.",
            }
        except (OSError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            ollama = {
                "available": False,
                "models": [],
                "error": "Could not load installed vision models from the configured Ollama server.",
            }

    choices = (["deepseek-flash"] if deepseek_available else []) + [
        f"ollama:{model['id']}" for model in ollama["models"]
    ]
    configured = os.environ.get("ITL_VISION_MODEL", "").strip()
    default_model = configured if configured in choices else (
        "deepseek-flash" if deepseek_available
        else (f"ollama:{ollama['models'][0]['id']}" if ollama["models"] else "")
    )
    return {
        "deepseek": {
            "available": deepseek_available,
            "error": None if deepseek_available else "Set DEEPSEEK_API_KEY to enable DeepSeek Flash.",
        },
        "ollama": ollama,
        "defaultModel": default_model,
    }


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


def clamp_number(payload: dict, key: str, default, low, high, cast=float):
    """Read a numeric lever from the payload, falling back to ``default`` and clamping."""
    try:
        value = cast(payload.get(key, default))
    except (TypeError, ValueError):
        value = default
    if value != value:   # NaN
        value = default
    return max(low, min(high, value))


def ui_page() -> str:
    for name in ("image-to-layers-server.html", "image-to-layers.html"):
        if (ROOT / name).is_file():
            return "/" + name
    return "/image-to-layers.html"


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


def list_projects() -> list[dict]:
    projects = []
    if not PROJECTS_DIR.is_dir():
        return projects
    for folder in sorted(PROJECTS_DIR.iterdir(), key=lambda entry: entry.name.casefold()):
        if not folder.is_dir() or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", folder.name):
            continue
        composition_path = folder / "compositions" / f"{folder.name}.composition.json"
        manifest_path = folder / "layers.json"
        if not composition_path.is_file() or not manifest_path.is_file():
            continue
        try:
            composition = json.loads(composition_path.read_text(encoding="utf-8"))
            if not isinstance(composition, dict) or not isinstance(composition.get("layers"), list):
                continue
            width, height = composition.get("width"), composition.get("height")
            if not isinstance(width, int) or not isinstance(height, int):
                continue
            projects.append({
                "id": folder.name,
                "name": str(composition.get("name") or folder.name),
                "width": width,
                "height": height,
            })
        except (OSError, json.JSONDecodeError):
            continue
    return projects


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
        # The UI promises "vertical position, no depth model is downloaded"; without
        # this flag the CLI defaulted to loading Depth Anything.
        argv += ["--depth", "--depth-model", "none"]
    if payload.get("depthRole"):
        argv.append("--depth-role")

    folders = str(payload.get("folders") or "group").lower()
    argv += ["--folders", folders if folders in {"group", "flat"} else "group"]

    mode = payload.get("mode") or "sam-points"

    # Scene description from the vision LLM (optional except for sam-scene).
    scene = payload.get("scene")
    scene_objects = scene.get("objects") if isinstance(scene, dict) else None
    if isinstance(scene_objects, list) and scene_objects:
        scene_path = tmpdir / "scene.json"
        scene_path.write_text(json.dumps(scene), encoding="utf-8")
        argv += ["--scene-json", str(scene_path)]
    elif mode == "sam-scene":
        raise ValueError("Scene-guided SAM needs a scene description. Click “Describe image” first.")

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
        recover_project_id = payload.get("recoverProjectId")
        if recover_project_id:
            if not isinstance(recover_project_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,80}", recover_project_id
            ):
                raise ValueError("Choose a valid existing project for recovery.")
            if str(payload.get("outputMode") or "project").lower() == "zip":
                raise ValueError("Merging recovered layers requires Project output.")
            project_dir = PROJECTS_DIR / recover_project_id
            composition_path = (
                project_dir / "compositions" / f"{recover_project_id}.composition.json"
            )
            manifest_path = project_dir / "layers.json"
            if not composition_path.is_file() or not manifest_path.is_file():
                raise ValueError("The selected project is no longer available for recovery.")
            argv += ["--recover-into", recover_project_id]
    elif mode == "knockout":
        cutout_value = payload.get("cutout")
        if not cutout_value:
            raise ValueError("Knockout mode requires the cut-out image")
        cutout_name = payload.get("cutoutName") or "cutout.png"
        cutout_path = tmpdir / f"cutout{guess_extension(cutout_name)}"
        cutout_path.write_bytes(decode_data_url(cutout_value, "cutout"))
        argv += [
            "--knockout", str(cutout_path),
            "--knockout-underlap", str(max(0, int(payload.get("knockoutUnderlap", 1)))),
        ]
        if payload.get("knockoutKeepCutout"):
            argv.append("--knockout-keep-cutout")
        seeds = []
        for item in payload.get("knockoutExclude") or []:
            try:
                x, y = float(item.get("x")), float(item.get("y"))
            except (AttributeError, TypeError, ValueError):
                continue
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                seeds.append(f"{x:.6f},{y:.6f}")
        if seeds:
            argv += ["--knockout-exclude", ";".join(seeds)]
    elif mode == "sam-auto":
        argv += [
            "--sam-auto",
            "--auto-points-per-side", str(clamp_number(payload, "autoPointsPerSide", 16, 4, 64, int)),
            "--auto-min-area", str(clamp_number(payload, "autoMinArea", 0.1, 0.001, 5.0)),
            "--auto-min-stability", str(clamp_number(payload, "autoMinStability", 0.85, 0.0, 0.99)),
            "--auto-pred-iou", str(clamp_number(payload, "autoPredIou", 0.8, 0.0, 0.99)),
            "--auto-nms-iou", str(clamp_number(payload, "autoNmsIou", 0.55, 0.1, 0.95)),
            "--auto-containment", str(clamp_number(payload, "autoContainment", 0.85, 0.3, 1.0)),
            "--auto-max-layers", str(clamp_number(payload, "autoMaxLayers", 0, 0, 500, int)),
            "--scene-match", str(clamp_number(payload, "sceneMatch", 0.35, 0.05, 0.95)),
        ]
    elif mode == "sam-scene":
        argv += [
            "--sam-scene",
            "--scene-box-pad", str(clamp_number(payload, "sceneBoxPad", 0.02, 0.0, 0.2)),
        ]
        if payload.get("sceneUsePoints") is False:
            argv.append("--scene-no-points")
        if payload.get("sceneRemainder", True):
            argv.append("--scene-remainder")
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
                    pass
    # FIX: filesystem fallback — find the most recently modified composition
    # under PROJECTS_DIR. The CLI prints the path, but a small format change
    # would otherwise break the server silently.
    try:
        candidates = sorted(
            PROJECTS_DIR.glob("*/compositions/*.composition.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            # Only accept if it was written after the run started — we don't
            # have that time here, so be conservative and accept only if the
            # stdout explicitly mentions the project slug.
            for line in stdout_lines:
                if "wrote " in line and "composition.json" in line:
                    slug = Path(line.split("wrote ", 1)[1].strip()).stem.split(".")[0]
                    for candidate in candidates:
                        if candidate.stem.startswith(slug):
                            try:
                                return json.loads(candidate.read_text(encoding="utf-8"))
                            except json.JSONDecodeError:
                                pass
    except Exception:
        pass
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
        request_path = urllib.parse.urlsplit(self.path).path
        if any(part.startswith(".") for part in request_path.split("/") if part not in ("", ".", "..")):
            self.send_error(404)  # never serve .env, .git, .venv, ...
            return
        if self.path in ("", "/", "/image-to-layers.html", "/image-to-layers-server.html"):
            self.path = ui_page()
        elif self.path.startswith("/api/sample"):
            self.send_json(200, find_sample_reference() or {})
            return
        elif request_path == "/api/projects":
            self.send_json(200, {"projects": list_projects()})
            return
        elif request_path == "/api/vision-models":
            self.send_json(200, get_vision_model_catalog())
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:
        request_path = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        prefix = "/data/projects/"
        if request_path.startswith(prefix):
            target = (PROJECTS_DIR / request_path[len(prefix):]).resolve()
            try:
                target.relative_to(PROJECTS_DIR.resolve())
            except ValueError:
                return str(PROJECTS_DIR / "__invalid_path__")
            return str(target)
        return super().translate_path(path)

    def do_POST(self):
        if self.path == "/api/run":
            self.handle_run()
        elif self.path == "/api/describe":
            self.handle_describe()
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

    # ── describe (vision LLM) ──────────────────────────────────────────

    def handle_describe(self):
        """Look at the whole image once and return a scene description + objects."""
        tmpdir = Path(tempfile.mkdtemp(prefix="itl-describe-"))
        try:
            if not TOOL.is_file():
                raise RuntimeError(f"Missing CLI: {TOOL}")
            payload = read_json_body(self)
            if "reference" not in payload:
                raise ValueError("Missing `reference` data URL")
            reference_name = payload.get("referenceName") or "reference.png"
            reference_path = tmpdir / f"reference{guess_extension(reference_name)}"
            reference_path.write_bytes(decode_data_url(payload["reference"], "reference"))
            scene_path = tmpdir / "scene.json"

            model = str(payload.get("visionModel") or "").strip()
            if model and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,80}", model):
                raise ValueError("Invalid vision model id")
            argv = [
                sys.executable, str(TOOL),
                "--reference", str(reference_path),
                "--describe-only", "--scene-out", str(scene_path),
                "--scene-detail", str(clamp_number(payload, "detail", 3, 1, 5, int)),
                "--scene-hint", str(payload.get("hint") or "")[:600],
            ]
            if model:
                argv += ["--vision-model", model]

            done = subprocess.run(
                argv, cwd=str(ROOT), capture_output=True, text=True,
                timeout=VISION_SUBPROCESS_TIMEOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            if done.returncode != 0 or not scene_path.is_file():
                lines = [l for l in (done.stderr or done.stdout).strip().splitlines() if l.strip()]
                message = lines[-1] if lines else f"CLI exited with status {done.returncode}"
                self.send_json(502, {"error": message})
                return
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            self.send_json(200, {"scene": scene})
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "The vision model took too long (over 11 minutes)."})
        except (ValueError, KeyError) as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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
                    result["projectDir"] = str(project_dir)
                candidate = project_dir / "compositions" / f"{slug}.composition.json"
                if candidate.is_file():
                    result["outputPath"] = f"/data/projects/{slug}/compositions/{candidate.name}"

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
    print(f"[itl] http://{HOST}:{PORT}{ui_page()}")
    print(f"[itl] project output → {PROJECTS_DIR}")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("[itl] note: DEEPSEEK_API_KEY is not set — DeepSeek is unavailable; local Ollama remains an option")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[itl] stopped")


if __name__ == "__main__":
    main()