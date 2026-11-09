#!/usr/bin/env python3
"""
render-html-example.py — browser-vs-extracted QA image for one example page.

Renders every stage twice at the same display width and timecode:

  left  = the page in headless Chrome (the design itself)
  right = the layers html-to-projects.py extracted, drawn from the composition data

    python3 tools/render-html-example.py \
        --html "html-examples/Singl-end · news bulletin reels.html" \
        --out docs/examples/img/news-bulletin.png

Used by the per-example docs; requires Chrome/Chromium.
"""

from __future__ import annotations

import argparse
import html as html_mod
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "tools" / "html-to-projects.py"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def load_extractor():
    spec = importlib.util.spec_from_file_location("h2p_render", EXTRACTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    sys.exit("Chrome/Chromium not found")


def run_extractor(page: Path) -> dict:
    done = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--html", str(page), "--preview-only",
         "--max-stages", "10", "--layout", "auto"],
        capture_output=True, text=True, timeout=300,
    )
    if done.returncode != 0:
        sys.exit((done.stderr or done.stdout).strip()[-600:])
    return json.loads(done.stdout)


def active(layer: dict, t: float) -> dict:
    kfs = layer.get("keyframes") or []
    if not kfs:
        return dict(layer)
    prev = kfs[0]
    nxt = kfs[-1]
    for i, kf in enumerate(kfs):
        if t >= kf["time"]:
            prev = kf
            nxt = kfs[i + 1] if i + 1 < len(kfs) else kf
    out = dict(layer)
    fraction = ((t - prev["time"]) / (nxt["time"] - prev["time"])
                if nxt["time"] > prev["time"] else 0.0)
    for key in ("x", "y", "w", "h", "rotation", "opacity"):
        a, b = prev.get(key), nxt.get(key)
        if a is not None and b is not None:
            out[key] = a + (b - a) * fraction
    return out


def extracted_stage_html(stage: dict, t: float, width: int) -> str:
    height = round(width * stage["height"] / stage["width"])
    boxes = []
    for layer in stage["layers"]:
        state = active(layer, t)
        if (state.get("opacity") or 0) < 0.02:
            continue
        style = (f"position:absolute;left:{state['x']}%;top:{state['y']}%;"
                 f"width:{state['w']}%;height:{state['h']}%;"
                 f"opacity:{state.get('opacity', 1)};")
        if state.get("rotation"):
            style += f"transform:rotate({state['rotation']}deg);"
        if layer["type"] == "image":
            is_shape = bool(layer.get("meta", {}).get("isShape")
                            or "shape" in layer["id"] or "border" in layer["id"])
            fit = "fill" if is_shape else (layer.get("fit") or "contain")
            boxes.append(f'<div style="{style}overflow:hidden">'
                         f'<img src="{html_mod.escape(layer["url"])}" '
                         f'style="width:100%;height:100%;display:block;object-fit:{fit}"></div>')
        else:
            font = layer.get("font") or {}
            justify = {"center": "center", "right": "flex-end"}.get(layer.get("align"), "flex-start")
            style += ("display:flex;align-items:center;white-space:pre-wrap;"
                      f"font-size:{font.get('sizePct', 3)}cqw;"
                      f"font-family:'{font.get('family')}',sans-serif;"
                      f"font-weight:{font.get('weight', 400)};"
                      f"font-style:{font.get('style', 'normal')};"
                      f"line-height:{font.get('lineHeight', 1.2)};"
                      f"letter-spacing:{font.get('letterSpacingEm', 0)}em;"
                      f"color:{layer.get('color') or '#fff'};"
                      f"text-align:{layer.get('align', 'left')};"
                      f"justify-content:{justify};"
                      + ("text-transform:uppercase;" if font.get("transform") == "uppercase" else ""))
            boxes.append(f'<div style="{style}">{html_mod.escape(layer.get("text") or "")}</div>')
    return (f'<div style="width:{width}px;flex:none;position:relative;'
            f'aspect-ratio:{stage["width"]}/{stage["height"]};overflow:hidden;'
            f'background:#000;container-type:inline-size">{ "".join(boxes) }</div>')


def truth_page(source: str, stages: list[dict], width: int, ratio: float, time_ms: float,
               base_dir: Path, vars_: list[str]) -> str:
    # keep the page JS (some pages build their stages in it) but stop the
    # animation loop so the state below is what gets rendered
    head = "<script>window.requestAnimationFrame=function(){return 0};</script>"
    source = re.sub(r"(</head>)", head + r"\1", source, count=1, flags=re.I)
    css = f"""
    <style>
      header,.notes,.ctl,.col h2,.bar,.safe{{display:none!important}}
      html,body{{margin:0!important;padding:0!important;background:#111!important}}
      .grid{{display:flex!important;gap:16px;padding:0!important;margin:0!important;
             max-width:none!important;align-items:flex-start!important}}
      .col{{width:{width}px!important;max-width:none!important;margin:0!important}}
      .stage,.post,.reel,.story{{width:{width}px!important;max-width:none!important;
             border-radius:0!important;box-shadow:none!important;margin:0!important}}
    </style>"""
    source = source.replace("</head>", css + "</head>", 1)
    start, end = "__TRUTH_" + uuid.uuid4().hex, "END_" + uuid.uuid4().hex
    script = f"""
    <script>
    (() => {{
      const TIME = {time_ms}, VARS = {json.dumps(vars_)};
      const stages = [...document.querySelectorAll('.stage, .post, .reel, .story')]
        .filter(el => !(el.parentElement && el.parentElement.closest('.stage, .post, .reel, .story')));
      stages.forEach((stage, i) => {{
        const d = {json.dumps([s.get('duration', 10000) for s in stages])}[i] || 10000;
        const t = Math.min(TIME, d) / 1000;
        VARS.forEach(v => stage.style.setProperty('--' + v, v === 'p' ? String(t * 1000 / d) : '1'));
        stage.querySelectorAll('[data-s]').forEach(el => {{
          const a = parseFloat(el.dataset.s) || 0;
          const b = el.dataset.e !== undefined ? parseFloat(el.dataset.e) : Infinity;
          el.classList.toggle('on', t >= a && t < b);
        }});
        const fade = stage.querySelector('.fade');
        if (fade) fade.classList.remove('on');
      }});
      try {{
        if (typeof f === 'function' && typeof o !== 'undefined' && stages.length) {{
          const base = o[stages[0].id] || performance.now();
          f(base + TIME);
        }}
      }} catch (err) {{}}
    }})();
    </script>"""
    return source.replace("</body>", script + "</body>", 1)


def extracted_page(stages_html: str, width: int, fonts: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
{fonts}
<style>html,body{{margin:0;background:#111}}#row{{display:flex;gap:16px;padding:0;align-items:flex-start}}</style>
</head><body><div id="row">{stages_html}</div></body></html>"""


def screenshot(chrome: str, page: Path, out: Path, width: int, height: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="itl-shot-"))
    try:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--disable-crash-reporter", "--mute-audio",
               f"--user-data-dir={tmp / 'profile'}",
               f"--window-size={width},{height}", "--virtual-time-budget=6000",
               f"--screenshot={out}", page.as_uri()]
        for attempt in (1, 2):
            subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if out.is_file() and out.stat().st_size:
                return
            if attempt == 1 and "--no-sandbox" not in cmd:
                cmd.insert(2, "--no-sandbox")
        sys.exit("Chrome produced no screenshot")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--width", type=int, default=336)
    ap.add_argument("--ratio", type=float, default=0.35, help="which moment to render (0-1)")
    ap.add_argument("--chrome")
    args = ap.parse_args(argv)

    from PIL import Image, ImageDraw

    h2p = load_extractor()
    source = args.html.read_text(encoding="utf-8", errors="replace")
    preview = run_extractor(args.html)
    stages = preview["stages"]
    vars_ = h2p.detect_progress_vars(
        re.search(r"<style[^>]*>(.*?)</style>", source, re.S).group(1)
        if "<style" in source else "")

    # one real moment in the reel: the same absolute time on both sides
    time_ms = args.ratio * min(s.get("duration", 10000) for s in stages)
    gap = 16
    tmp = Path(tempfile.mkdtemp(prefix="itl-render-"))
    try:
        truth = tmp / "truth.html"
        truth.write_text(truth_page(source, stages, args.width, args.ratio, time_ms,
                                    args.html.parent, vars_), encoding="utf-8")
        fonts = "\n".join(re.findall(r'<link[^>]+fonts\.googleapis\.com[^>]*>', source, re.I))
        extracted = tmp / "extracted.html"
        extracted.write_text(extracted_page(
            "".join(extracted_stage_html(s, time_ms, args.width)
                    for s in stages), args.width, fonts), encoding="utf-8")

        heights = [round(args.width * s["height"] / s["width"]) for s in stages]
        canvas_w = len(stages) * args.width + (len(stages) - 1) * gap + 2
        canvas_h = max(heights) + 2
        truth_png, layer_png = tmp / "truth.png", tmp / "layers.png"
        chrome = find_chrome(args.chrome)
        screenshot(chrome, truth, truth_png, canvas_w, canvas_h)
        screenshot(chrome, extracted, layer_png, canvas_w, canvas_h)

        rows = []
        for i, stage in enumerate(stages):
            x = i * (args.width + gap)
            top = Image.open(truth_png).crop((x, 0, x + args.width, heights[i])).convert("RGB")
            bottom = Image.open(layer_png).crop((x, 0, x + args.width, heights[i])).convert("RGB")
            row = Image.new("RGB", (args.width * 2 + gap, heights[i] + 26), (17, 17, 17))
            row.paste(top, (0, 26))
            row.paste(bottom, (args.width + gap, 26))
            draw = ImageDraw.Draw(row)
            draw.text((6, 7), f"browser · {stage['name']}", fill=(210, 210, 210))
            draw.text((args.width + gap + 6, 7), "extracted layers", fill=(210, 210, 210))
            rows.append(row)
        out = Image.new("RGB", (max(r.width for r in rows), sum(r.height for r in rows) + 12),
                        (17, 17, 17))
        y = 0
        for row in rows:
            out.paste(row, (0, y))
            y += row.height + 12
        args.out.parent.mkdir(parents=True, exist_ok=True)
        out.save(args.out)
        print(args.out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
