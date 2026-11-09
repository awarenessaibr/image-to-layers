#!/usr/bin/env python3
"""
rasterize-html-chrome.py — render one element of a template page to a PNG.

Complex artwork (inline SVG with gradients/filters, canvas, filtered HTML) cannot
be rebuilt as editable layers. For those nodes the extractor takes a flattened
raster of the element in its `.on` state, with a transparent background, and uses
it as one image layer.

    python3 tools/rasterize-html-chrome.py \
        --html "html-examples/Melano · stop-motion style posts.html" \
        --stage-index 0 --path 0 --width 1080 --height 1350 --out /tmp/art.png

`--path` is the child-index path from the stage (the same `path` the measurement
harness records). The element is isolated: every other node is hidden, the page
background is cleared, and the stage is shifted so the element sits at (0,0).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

JS = r"""
<script>
(() => {
  window.requestAnimationFrame = function () { return 0; };
  const STAGE_INDEX = __STAGE__, PATH = "__PATH__", STAGE_W = __SW__, VARS = __VARS__;
  const TIME = __TIME__;
  const SELECTOR = __SELECTOR__;
  const capture = () => {
    const stages = [...document.querySelectorAll(SELECTOR)]
      .filter(el => !(el.parentElement && el.parentElement.closest(SELECTOR)));
    const stage = stages[STAGE_INDEX];
    if (!stage) { document.title = 'RASTERIZE: no stage'; return; }
    stage.style.width = STAGE_W + 'px';
    stage.style.maxWidth = 'none';
    Object.entries(VARS).forEach(([k, v]) => stage.style.setProperty('--' + k, v));
    if (TIME) {
      try {
        if (typeof f === 'function' && typeof o !== 'undefined' && o[stage.id] !== undefined) {
          f(o[stage.id] + TIME * 1000);
        }
      } catch (err) {}
    }
    stage.querySelectorAll('*').forEach(el => {
      el.style.transition = 'none';
      el.style.animation = 'none';
      if (!TIME && el.dataset && el.dataset.s !== undefined && el.dataset.s !== '')
        el.classList.add('on');
    });
    document.body.offsetHeight;

    let el = stage;
    for (const part of PATH.split('.')) el = el && el.children[+part];
    if (!el) { document.title = 'RASTERIZE: no element'; return; }
    const rect = el.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    const dx = rect.left - stageRect.left, dy = rect.top - stageRect.top;

    // hide every body descendant, then reveal only the element and its chain;
    // clear ancestor backgrounds so the PNG is transparent around the element
    document.querySelectorAll('body *').forEach(e => { e.style.visibility = 'hidden'; });
    for (let p = el.parentElement; p; p = p.parentElement) {
      p.style.visibility = 'visible';
      p.style.background = 'transparent';
      p.style.boxShadow = 'none';
    }
    el.style.visibility = 'visible';
    el.querySelectorAll('*').forEach(e => { e.style.visibility = 'visible'; });

    document.documentElement.style.background = 'transparent';
    document.body.style.background = 'transparent';
    document.body.style.margin = '0';
    stage.style.background = 'transparent';
    stage.style.boxShadow = 'none';
    stage.style.borderRadius = '0';
    stage.style.position = 'fixed';
    stage.style.left = (-dx) + 'px';
    stage.style.top = (-dy) + 'px';
    stage.style.margin = '0';
    document.title = 'RASTERIZE:' + Math.round(rect.width) + 'x' + Math.round(rect.height);
  };
  // isolate immediately: the screenshot itself is taken after the virtual time
  // budget, by which point webfonts have swapped in (paint, not layout, changes)
  capture();
})();
</script>
</body>"""


def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if found:
        return found
    sys.exit("Chrome/Chromium not found; pass --chrome /path/to/binary")


def build_page(source: str, stage_index: int, path: str, stage_width: int, time_s: float,
               selector: str, css_vars: dict[str, str], base_dir: Path) -> str:
    # keep the page's scripts (some pages build their stages in JS), but stop the
    # animation loop before it starts so the isolation below is not overwritten
    stub = "<script>window.requestAnimationFrame=function(){return 0};</script>"
    if re.search(r"<head[^>]*>", source, re.I):
        source = re.sub(r"(<head[^>]*>)", r"\1" + stub, source, count=1, flags=re.I)
    js = (JS.replace("__STAGE__", str(stage_index))
            .replace("__PATH__", path)
            .replace("__SW__", str(stage_width))
            .replace("__VARS__", json.dumps(css_vars))
            .replace("__TIME__", json.dumps(time_s))
            .replace("__SELECTOR__", json.dumps(selector)))
    page = source.replace("</body>", js, 1) if "</body>" in source else source + js
    base = f'<base href="{base_dir.as_uri().rstrip("/")}/">'
    if re.search(r"<head[^>]*>", page, re.I):
        page = re.sub(r"(<head[^>]*>)", r"\1" + base, page, count=1, flags=re.I)
    else:
        page = base + page
    return page


def rasterize(html_path: Path, stage_index: int, path: str, width: int, height: int,
              out: Path, stage_width: int, selector: str, scale: float,
              css_vars: dict[str, str], chrome: str | None, no_sandbox: bool,
              time_s: float = 0.0) -> Path:
    source = html_path.read_text(encoding="utf-8", errors="replace")
    tmpdir = Path(tempfile.mkdtemp(prefix="itl-raster-"))
    try:
        page = build_page(source, stage_index, path, stage_width, time_s, selector,
                          css_vars, html_path.parent)
        page_path = tmpdir / "raster.html"
        page_path.write_text(page, encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        cmd = [find_chrome(chrome), "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--disable-crash-reporter", "--mute-audio",
               f"--user-data-dir={tmpdir / 'profile'}",
               f"--window-size={max(1, int(round(width * scale)))},"
               f"{max(1, int(round(height * scale)))}",
               f"--force-device-scale-factor={scale}",
               "--default-background-color=00000000",
               "--virtual-time-budget=5000",
               f"--screenshot={out}", page_path.as_uri()]
        if no_sandbox:
            cmd.insert(2, "--no-sandbox")
        for attempt in (1, 2, 3):
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if out.is_file() and out.stat().st_size > 0:
                return out
            if attempt == 1 and "--no-sandbox" not in cmd:
                cmd.insert(2, "--no-sandbox")
                continue
            if attempt == 2 and "--headless=new" in cmd:
                cmd[cmd.index("--headless=new")] = "--headless"
                continue
            sys.exit("Chrome produced no screenshot")
        return out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rasterize one element with headless Chrome")
    ap.add_argument("--html", required=True)
    ap.add_argument("--stage-index", type=int, required=True)
    ap.add_argument("--path", required=True, help="child-index path from the stage")
    ap.add_argument("--width", type=int, required=True, help="element width in stage px")
    ap.add_argument("--height", type=int, required=True, help="element height in stage px")
    ap.add_argument("--stage-width", type=int, default=1080)
    ap.add_argument("--selector", default=".stage, .post, .reel, .story")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--time", type=float, default=0.0,
                    help="advance the page's own animation to this reel time (s)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--chrome")
    ap.add_argument("--no-sandbox", action="store_true")
    args = ap.parse_args(argv)

    css_vars = {}
    for item in args.var:
        k, v = item.split("=", 1)
        css_vars[k.strip().lstrip("-")] = v.strip()
    out = rasterize(Path(args.html).expanduser().resolve(), args.stage_index, args.path,
                    args.width, args.height, args.out, args.stage_width, args.selector,
                    args.scale, css_vars, args.chrome, args.no_sandbox, args.time)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
