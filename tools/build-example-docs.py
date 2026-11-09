#!/usr/bin/env python3
"""
build-example-docs.py — write the per-example extraction reports.

Reads docs/examples/_audit.json (produced by tools/audit-html-examples.py) and
the preview for each page, then writes docs/examples/<slug>.md plus the index.

    python3 tools/audit-html-examples.py -o docs/examples/_audit.json
    python3 tools/render-html-example.py --html ... --out docs/examples/img/<slug>.png
    python3 tools/build-example-docs.py
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACTOR = ROOT / "tools" / "html-to-projects.py"

AUDIT = ROOT / "docs" / "examples" / "_audit.json"
DEEP_REVIEWS = {
    "singl-end-italian-places-reels": "../review-italian-places-animation.md",
}

# Page-specific approximations observed in the browser-vs-extracted comparison.
SPECIFIC_GAPS = {
    "melano-stop-motion-style-posts": [
        "The SVG scene is one flattened layer; the HTML product photo (`.pd`) can sit a few pixels "
        "off the drawn package, and the felt/clay stop-motion jitter is frozen.",
    ],
    "singl-end-tarantella-posts": [
        "The outlined display type (`FESTA`), the halftone dot texture and the circle backdrop are "
        "not reproduced as layers, so those areas fall back to the plain background.",
        "The price caption wraps into more lines than the browser's, because the text layer gets a "
        "width slack the original shrink-to-fit box did not have.",
    ],
    "singl-end-italian-places-reels": [
        "The camera flight, minimap viewport, active location dot, counter, route and spotlight are "
        "sampled from the page's own `f(t)` and emitted as keyframes or flipbook frames; the route "
        "(12 frames) and spotlight (16 frames) step rather than move continuously.",
        "The second pulsing ring and the soft shadows are a `::after` pseudo-element and "
        "`box-shadow`, so they are not separate layers.",
        "The departures board's active row (`.rw.on`) swaps to a gold background; the row container "
        "is transparent at rest, so only its text is emitted and the highlight is not.",
    ],
    "singl-end-le-regole-della-nonna-3-episode-series": [
        "The `Regola` badge overlaps the card; stacking is DOM order, so on this page the badge can "
        "end up behind the card instead of above it.",
        "Box shadows (card, badge) are not reproduced.",
    ],
    "singl-end-3-minimalist-reels": [
        "The arch window is masked correctly, but its `clip-path` wipe-in and the card's box shadow "
        "are not animated/reproduced.",
    ],
    "singl-end-news-bulletin-reels": [
        "The LIVE dot (a pseudo-element) and the scanline/vignette texture are not separate layers; "
        "the timecode is a snapshot taken while measuring.",
    ],
    "heritage-collection-places-reels": [
        "The map flight and the quiz reveal are SVG drawings; the two largest animated SVGs per stage "
        "are sample-rendered into 12-frame flipbooks, so those drawings step every ~0.8 s rather than "
        "moving continuously.",
        "The passport stamps are rotated containers with a separate country raster; their frame and "
        "silhouette are placed at the measured angle.",
    ],
    "heritage-collection-story-reels": [
        "The book covers float and tilt with a long CSS animation; the layers keep the resting transform "
        "measured at the end of the reel.",
        "The blurred backdrop art (`filter: blur(25.92px) saturate(1.2)` at 32 % over `mix-blend-mode:screen`) "
        "is baked into the image; the blend mode itself is approximated.",
    ],
    "singl-end-pinocchio-storytelling-reels": [
        "The curtain halves are gradient layers and the progress-driven overlays sit at their resting "
        "position; their entrance timing is not keyframed.",
        "The theatre-window photo can overpaint its gold frame when it is transformed inside a window "
        "that does not clip its content.",
    ],
}

SPECIFIC_GAPS.update({
    "deep-thinkers-cinematic-reels": [
        "The Leonardo quote is mirrored until 7 s; the mirrored state is a flattened raster and "
        "the live text takes over at the flip, so the 1.4 s transform is a cut rather than a tween.",
        "The red/amber glows use `mix-blend-mode: screen`; they are emitted as alpha gradients, so "
        "the light is approximated rather than blended.",
        "The floating dust motes are CSS animations sampled at 16 points; their motion is not "
        "keyframed.",
        "Portraits use `mask-image: linear-gradient(...)` fades; the linear fade is baked into the "
        "image, more complex masks are not.",
    ],
    "butterfly-pig-cinematic-posts": [
        "The repeating-conic-gradient sunburst behind the Door 13 trailer is skipped: the CSS→SVG "
        "converter supports linear and radial gradients, not conic ones.",
        "The gossip reel is a stack of time-windowed quote cards; each card is a separate text + gradient "
        "layer with an instant cut at its `data-s`/`data-e`.",
        "The belle-époque poster is a flattened raster, so its fine print is not editable type.",
    ],
    "02-brutal": [
        "The stacked bathroom photo frames are 2D-transformed layers; the photo is placed at its "
        "cover paint box, so the extracted frame can show a different crop/stack than the browser.",
        "The counter and frame transitions are sampled at 16 points; the CRT/scanline overlay is "
        "skipped as a texture.",
    ],
    "03-immersive": [
        "This is one stage with three auto-advancing chapters; the chapter DOM is rebuilt by the page's "
        "JS, so the composition reflects the sampled chapters and chapter-specific images can be "
        "placed at a different scale than the browser.",
    ],
    "alternatives-heritage-italian-places-pinocchio": [
        "The first reel is a CRT-styled postcard rack: five full-bleed cover cards are clipped to the "
        "card window, so the extracted cover crop and paper card can differ from the browser; the "
        "CRT/scanline overlay is skipped as a texture.",
        "The second reel's letter and photo postcards are independent cards; the photo postcard can "
        "sit behind the letter in the flattened order.",
    ],
    "heritage-collection-alt-places-reels": [
        "The first reel's postcard rack uses 3D transforms on five cards; the composition keeps the "
        "2D card frames (the layer model has no perspective), so the card art can be blank and the "
        "stamps are placed at the measured 2D angle.",
        "The second reel fills a passport with stamps; the stamps' dashed borders and inner country "
        "art are separate rasters and can be scaled slightly differently.",
    ],
    "heritage-collection-video-posts": [
        "The four videos are embedded as a captured first frame (a poster), not as video layers; "
        "video motion, audio and blend modes are not part of the composition. The overlays are "
        "keyframed from the video's own timeline.",
        "The poster is captured at the measurement moment, so the browser-vs-extracted image can "
        "show a different video frame.",
    ],
    "singl-end-pinocchio-alt-reels": [
        "The shadow-puppet and comic-strip reels rely on hard shadows and clip wipes; box shadows "
        "and `clip-path` wipes are not separate layers, so the wipe is a visibility window.",
    ],
})

OUT = ROOT / "docs" / "examples"
IMG = "img"


def load_extractor():
    import importlib.util
    spec = importlib.util.spec_from_file_location("h2p_docs", EXTRACTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def preview(page: Path) -> dict:
    done = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--html", str(page), "--preview-only",
         "--max-stages", "10"],
        capture_output=True, text=True, timeout=300,
    )
    return json.loads(done.stdout) if done.returncode == 0 else {"stages": []}


def text_of(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " ", fragment, flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def page_copy(source: str) -> dict:
    title = re.search(r"<h1[^>]*>(.*?)</h1>", source, re.S | re.I)
    intro = re.search(r"<header[^>]*>.*?<p[^>]*>(.*?)</p>", source, re.S | re.I)
    notes = re.search(r'<div class="notes"[^>]*>(.*?)</div>', source, re.S | re.I)
    return {
        "title": text_of(title.group(1)) if title else "",
        "intro": text_of(intro.group(1)) if intro else "",
        "notes": text_of(notes.group(1)) if notes else "",
    }


def features(parsed: dict) -> dict:
    layers = [l for s in parsed.get("stages", []) for l in s.get("layers", [])]
    images = [l for l in layers if l["type"] == "image" and not l["meta"].get("isShape")]
    shapes = [l for l in layers if l["type"] == "image" and l["meta"].get("isShape")]
    texts = [l for l in layers if l["type"] == "text"]
    return {
        "layers": len(layers),
        "text": len(texts),
        "shape": len(shapes),
        "photo": len(images),
        "gradient": sum(1 for l in shapes if "Gradient" in l.get("url", "") or "linearGradient" in l.get("url", "")
                        or "radialGradient" in l.get("url", "") or "pattern" in l.get("url", "")),
        "raster": sum(1 for l in layers if l["meta"].get("rasterized")),
        "glyphs": sum(1 for l in texts if "✔" in (l.get("text") or "") or "✓" in (l.get("text") or "")),
        "borders": sum(1 for l in layers if "-border-" in l["id"]),
        "background": len([l for l in layers if l["meta"].get("isBackground")]),
        "animated": sum(1 for l in layers if len(l.get("keyframes") or []) > 4),
    }


def stage_table(stages: list[dict], parsed: dict) -> str:
    rows = ["| # | size | duration | layers | text | shapes | photos | text ≤2 % | paint |",
            "|---|---|---|---|---|---|---|---|---|"]
    for stage in stages:
        p = parsed["stages"][stage["index"]] if stage["index"] < len(parsed.get("stages", [])) else {}
        kinds = stage["layers"]
        text = stage["text"]
        text_cell = f"{text['within']}/{text['expected']}"
        if text["expected"] and text["within"] == 0:
            text_cell += " (baked)"
        rows.append(
            f"| {stage['index'] + 1} | {stage['width']}×{stage['height']} | "
            f"{stage['duration'] / 1000:g} s | {sum(kinds.values())} | {kinds['text']} | "
            f"{kinds['shape']} | {kinds['photo']} | {text_cell} | "
            f"{stage['paint']['matched']}/{stage['paint']['expected']} |")
    return "\n".join(rows)


def build(page: dict, audit: dict) -> str:
    source = (ROOT / page["file"]).read_text(encoding="utf-8", errors="replace")
    copy = page_copy(source)
    parsed = preview(ROOT / page["file"])
    feat = features(parsed)
    durations = sorted({s["duration"] for s in page["stages"]})
    duration_text = ", ".join(f"{d / 1000:g} s" for d in durations)
    h2p = load_extractor()
    page_ms = h2p.detect_page_duration_ms(source)
    style = re.search(r"<style[^>]*>(.*?)</style>", source, re.S)
    progress = h2p.detect_progress_vars(style.group(1) if style else "")

    bullets: list[str] = []
    if feat["background"]:
        bullets.append(f"{feat['background']} background layer(s) across all stages — a full-bleed photo "
                       "with its pan/zoom keyframes, or the stage's own colour/gradient.")
    if feat["text"]:
        bullets.append(f"{feat['text']} text layer(s) from measured content boxes; font, weight, "
                       "colour, alignment and letter-spacing come from the computed style, and "
                       "inline runs are merged into one layer.")
    if feat["shape"]:
        detail = f"{feat['shape']} shape layer(s) from background colour, borders, radius and per-side rules"
        if feat["gradient"]:
            detail += f", including {feat['gradient']} CSS gradient(s) rendered as SVG (linear, radial or tiled)"
        bullets.append(detail + ".")
    if feat["borders"]:
        bullets.append(f"{feat['borders']} separate border rule(s) for borders that only exist on one side.")
    if feat["photo"]:
        bullets.append(f"{feat['photo']} photo layer(s) placed at their cover/contain paint box; clipped ones "
                       "are cropped to the visible window and CSS filters are baked in.")
    if feat["raster"]:
        bullets.append(f"{feat['raster']} inline-SVG artwork layer(s), rasterized in Chrome because the drawing "
                       "cannot be expressed as editable boxes or type.")
    if feat["glyphs"]:
        bullets.append(f"{feat['glyphs']} text layer(s) include a `::before` glyph (the menu ticks).")
    if progress:
        bullets.append("JS-driven progress variables are set to their resting value before measuring: "
                       + ", ".join("`--" + v + "`" for v in progress) + ".")
    if page_ms:
        bullets.append(f"Reel length comes from the page's own `D = {page_ms}` constant.")
    if duration_text:
        bullets.append(f"Stage duration: {duration_text}; visibility uses the measured `data-s`/`data-e` "
                       "windows (intersected with ancestors) with instant cuts.")

    gaps: list[str] = list(SPECIFIC_GAPS.get(page["slug"], []))
    if feat["raster"]:
        gaps.append("The SVG artwork is flattened: its type and illustration are not editable layers, and the "
                    "stop-motion filters are frozen at the measured frame.")
    if any("gradient" in " ".join(s.get("warnings") or []).lower() for s in page["stages"]):
        gaps.append("Some gradient overlays could not be reproduced and were skipped (see the warnings below).")
    if not feat.get("animated"):
        gaps.append("Entry/exit motion (slide, scale, spin) is represented by visibility windows and the background "
                    "pan, not by the authored transitions.")
    if any("No background colour" in " ".join(s.get("warnings") or []) for s in page["stages"]):
        gaps.append("At least one stage has no opaque backdrop, so it stays transparent in the composition.")
    gaps.append("Decorative pseudo-elements (dots, halos, halftone overlays) are not measured; textual `::before` "
                "content is included.")
    gaps.append("Text that the page injects from JS (live clocks, typed lines) is a snapshot from measurement time.")

    warnings = [f"Stage {s['index'] + 1}: {w}" for s in page["stages"] for w in s.get("warnings") or []]
    wall = "\n".join(f"> {w}" for w in warnings) if warnings else "_None._"

    slug = page["slug"]
    title = copy["title"] or Path(page["file"]).stem
    parts = [
        f"# {title}",
        "",
        f"> {copy['intro']}" if copy["intro"] else "",
        "",
        f"![browser vs extracted layers]({IMG}/{slug}.png)",
        "",
        f"**Source:** [`{page['file']}`](../../{page['file']}) · **Stages:** {page['stageCount']} · "
        f"**Duration:** {duration_text}  ",
        f"**Fixture:** [`{page['fixture']}`](../../{page['fixture']}) — Chrome at 1080 px, webfonts loaded  ",
        "**Layout:** measured in headless Chrome (`--layout measure`)",
        *([f"**Deep review:** [animation fidelity]({DEEP_REVIEWS[slug]})  "] if slug in DEEP_REVIEWS else []),
        "",
        "## Stages",
        "",
        stage_table(page["stages"], parsed),
        "",
        "## How it was extracted",
        "",
        *[f"- {b}" for b in bullets],
        "",
        "## Design notes",
        "",
        copy["notes"].removeprefix("Notes:").strip() or "_The page carries no notes block._",
        "",
        "## Known gaps",
        "",
        *[f"- {g}" for g in gaps],
        "",
        "## Warnings",
        "",
        wall,
        "",
        "## Verify",
        "",
        "```bash",
        "python3 tools/audit-html-examples.py --only "
        + json.dumps(Path(page["file"]).stem.split(" · ")[-1][:36]) + " --no-measure",
        f"python3 tools/check-html-parse.py --fixture {page['fixture']} \\",
        f"  --html \"{page['file']}\" --verbose",
        "```",
        "",
    ]
    return "\n".join(part for part in parts if part is not None)


def main() -> int:
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    index_only = "--index-only" in sys.argv[1:]
    only = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
    pages = [p for p in audit["pages"]
             if not only or only.lower() in p.get("page", "").lower()]
    index_rows = ["| page | stages | layers | text ≤2 % | paint | report |",
                  "|---|---|---|---|---|---|"]
    for page in pages:
        if "error" in page:
            continue
        totals = page["totals"]
        text_within = totals["textWithin"]
        text_expected = totals["textExpected"]
        paint_matched = totals["paintMatched"]
        paint_expected = totals["paintExpected"]
        label = Path(page["file"]).stem
        if index_only:
            index_rows.append(
                f"| [{label}]({page['slug']}.md) | {page['stageCount']} | {totals['layers']} | "
                f"{text_within}/{text_expected}"
                + (" (baked)" if text_expected and text_within == 0 else "")
                + f" | {paint_matched}/{paint_expected} | "
                f"[image]({IMG}/{page['slug']}.png) |")
            continue
        md = build(page, audit)
        (OUT / f"{page['slug']}.md").write_text(md + "\n", encoding="utf-8")
        index_rows.append(
            f"| [{label}]({page['slug']}.md) | {page['stageCount']} | {totals['layers']} | "
            f"{text_within}/{text_expected}"
            + (" (baked)" if text_expected and text_within == 0 else "")
            + f" | {paint_matched}/{paint_expected} | "
            f"[image]({IMG}/{page['slug']}.png) |")
        print(f"wrote {page['slug']}.md", file=sys.stderr)
    if only and not index_only:
        print("(partial run: index not rewritten)", file=sys.stderr)
        return 0

    index = [
        "# Example pages — extraction reports",
        "",
        "Every page in `html-examples/` is rendered in headless Chrome, measured, turned into "
        "LayerStudio layers by `tools/html-to-projects.py`, and scored against the browser layout. "
        "Each report below shows the browser and the extracted layers side by side at the same "
        "timecode, the per-stage inventory, and what is still approximate.",
        "",
        "The shared pipeline is described in [HTML → LayerStudio pipeline](../html-to-layers-pipeline.md).",
        "",
        *index_rows,
        "",
        "## Regenerating",
        "",
        "```bash",
        "# 1. fixtures + coverage audit (headless Chrome)",
        "python3 tools/audit-html-examples.py -o docs/examples/_audit.json",
        "",
        "# 2. browser-vs-extracted images",
        "python3 - <<'PY'",
        "import json, subprocess, sys",
        "for page in json.load(open('docs/examples/_audit.json'))['pages']:",
        "    subprocess.run([sys.executable, 'tools/render-html-example.py', '--html', page['file'],",
        "                    '--out', f\"docs/examples/img/{page['slug']}.png\"], check=True)",
        "PY",
        "",
        "# 3. these reports",
        "python3 tools/build-example-docs.py",
        "```",
        "",
        "Fixtures live in `tests/fixtures/examples/`; `tests/test_html_layout.py` checks them.",
    ]
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    print(f"wrote README.md ({len(pages)} row(s))"
          + ("" if index_only else f" and {len(pages)} reports"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
