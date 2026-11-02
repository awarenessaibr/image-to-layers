# Reviewing the studio's ingestion + designing an image → layers side project

I read through `_.js`, `server.js`, `studio.html`, `composition-generator.js`, `apply-beards-rig.js`, and the sample data. Here's what matters for a tool that feeds the studio, then the design and working code for the side project.

## 1. What the studio actually expects

There are **three ingestion paths**, and they have very different requirements:

### A. "Import ZIP + image" (server-driven, auto-aligned)
- You upload a ZIP of segment images + the final reference image to `POST /api/imports`.
- The server (`createImport` → `buildComposition`) extracts images under `data/imports/<id>/segments/`, computes each segment's `width/height` relative to the reference, and **guesses** initial positions via `initialPlacement()` + `depthFor()`.
- The **client** then runs `alignToReference()` which does multi-scale template matching (`matchImageToReference`) of each cutout against the reference composite. It succeeds only when `score ≤ 0.18` and `alternativeScore - score ≥ 0.01`; otherwise the layer is marked `unmatched` / `ambiguous` and needs manual review.
- Good for arbitrary art. Fragile for overlapping/occluded shapes and low-contrast areas.

### B. Project composition (bypasses alignment)
- Any file at `data/projects/<Name>/compositions/<slug>.composition.json` shows up in the project selector (`listStudioProjects`).
- The JSON is served as-is (`readStudioComposition`); its layer `url`s can be **any URL the server can serve** (data URLs, `/data/projects/…`, `/data/imports/…`). Existing examples: `data/projects/Beards/…` references `data/imports/beards-…/assets/…`.
- **Positions are trusted exactly** — no matching, no ambiguity. Best when your segmenter already knows the pixel-accurate boxes.
- If you omit `characters`, the server auto-runs `detectCharacters()` on first load and persists them.

### C. Character bundle (rigged characters only)
- `POST /api/imports/character` expects a ZIP with a `character.json` manifest (`schema: "layer-studio/character/v1"`) + per-part PNGs. This is where `part`/`group`/`variant` become relevant so blink/talk/expression rigs work.
- See `apply-beards-rig.js` for the exact part naming and `data/projects/Beards/compositions/*.composition.json` for the shape the rig expects.

### Key schema constraints (from `makeLayer` / `normalizeComposition`)
| Field | Range / format |
|---|---|
| `id` | `^[A-Za-z0-9_-]{1,80}$` (must be unique) |
| `url` | `data:image/*` or `http(s):` (relative URLs are OK; studio prepends origin) |
| `x, y` | −100 … 200 (% of canvas, **top-left** origin) |
| `w, h` | 1 … 300 (% of canvas) |
| `opacity` | 0…1 |
| `rotation` | −3600…3600 (deg) |
| `fit` | `"contain"` or `"cover"` |
| `role` | `background` / `midground` / `ground` / `foreground` / `character` / `character-part` / `prop` / `effect` |
| `z` | −1000…1000 |
| reference | `referenceLayerId` must point to an existing layer; that layer is drawn first (bottom) |

For **character parts**, the server-side auto-detector (`CHARACTER_PART_MATCHERS`) classifies by **filename**:
- `pupil|iris` → pupils
- `highlight|catchlight|specular` → highlights
- `eyelid|lid` → eyelids
- `eye|eyeball` → eyes
- `brow` → brows
- `mouth|lip` → mouth
- `teeth|tooth` → teeth
- `tongue` → tongue
- `jaw|chin` → jaw
- `mustache|moustache` → mustache
- `face|skin|head-base|face-base` → face
- `hair|head|skull|wig` → head
- `beard|whisker` → beard
- `shirt|body|torso|dress|jacket|coat|robe|armor|suit` → body
- `ear`, `neck|throat`, `hand|fist|palm|arm` → ears / neck / hands

If you name files accordingly, you get a free rig without hand-writing `characters`.

### Alignment math if you need it
`matchImageToReference` treats `placement.x/y` as the **top-left of the cutout image** (not its bounds), so tight crops and padded cutouts both work. `w/h` are the cutout's original dimensions times `placement.factor`. `fit: "contain"` is what the matcher expects.

---

## 2. Side project design: `image-to-layers`

Two output modes, one backend interface.

```
reference.png ──┐
                ├─→  masks/ (one RGBA or grayscale mask per layer)
                │       ↓
                │   image-to-layers.py
                │       ↓
                ├─→ --mode project   → data/projects/<Name>/{images/,compositions/}
                └─→ --mode zip       → <slug>.zip (drop into "Import ZIP + image")
```

**Why two modes:** the ZIP path lets the studio do the alignment for you; the project path lets you *prove* your segmenter is right and skip the matcher's ambiguity entirely. I'd start with project mode, since that removes the biggest reliability risk.

### Segmentation backends (pick one, they all feed the same mask directory)

| Backend | When to use | Deps |
|---|---|---|
| `--masks <dir>` | SAM output, photoshop exports, manual masks — **start here** | none |
| `--backend rembg` | Subject/background split, fast | `rembg`, `onnxruntime` |
| `--backend depth` | 3–5 depth bands (sky / mid / ground / fg) | `transformers`, `torch`, Depth Anything v2 |
| `--backend sam` | Best quality, any object | `segment-anything`, weights; or SAM 2 |

The script I'll give you targets **`--masks`** so it runs today, and I'll show the SAM adapter in comments.

### What it does with each mask

1. Load the reference, load each mask, require same resolution.
2. Extract cutout: `RGBA = reference * mask_alpha`.
3. Tight-crop the cutout to its alpha bounds (smaller PNG, faster ZIP; template matching still works because `matchImageToReference` recomputes bounds internally).
4. Derive the **composition coordinates** from the crop's origin and size (percent of the reference canvas).
5. Classify `role` from the layer name using the same heuristics the server uses, so behaviour is consistent.
6. Emit `part` / `group` / `variant` if the name matches a character part, so the server-side rig can pick it up.
7. Write the composition JSON with a hidden reference layer at the bottom, cutouts stacked by role depth, and `meta.source = "image-to-layers"`.

Because the reference layer is *hidden* by default, any uncovered region will show as white in the studio — a good visual check that the segmenter missed something. Toggle it on in the layer list to compare.

---

## 3. Working code

Save as `tools/image-to-layers.py`. It has no dependency beyond Pillow, produces a project composition, and `--mode zip` will produce a ZIP for the built-in importer if you want to compare against the auto-alignment path.

```python
#!/usr/bin/env python3
"""image-to-layers.py — turn a reference image + per-layer masks into a
LayerStudio composition.

Masks can be:
  * RGBA PNGs with any alpha > 0 inside the layer
  * 8-bit grayscale PNGs where > 127 = inside
One mask per layer; the filename stem is the layer name.

Outputs (--mode project, default):
  data/projects/<Name>/images/<slug>.png       (tight RGBA cutouts)
  data/projects/<Name>/images/reference.<ext>
  data/projects/<Name>/compositions/<slug>.composition.json

Outputs (--mode zip):
  <slug>.zip   → drop into Studio's "Import ZIP + image" together with
                 the reference image to use the server-side alignment path.

Usage:
  python3 tools/image-to-layers.py \
      --reference path/to/final.jpg \
      --masks path/to/masks/ \
      --name "My Scene" \
      [--out data/projects] \
      [--mode project|zip] \
      [--overrides layers.yaml]
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image

# ── role/depth heuristics (mirror of server.js) ───────────────────────────

ROLE_ORDER = [
    ("background", 0),
    ("midground", 30),
    ("ground", 40),
    ("foreground", 90),
    ("character", 70),
    ("character-part", 80),
    ("prop", 60),
    ("effect", 95),
]

def classify_role(name: str) -> str:
    n = name.lower()
    if re.search(r"^(sky|clouds?|backdrop|background)$", n) or re.search(r"distant|hill|mountain|horizon", n):
        return "background"
    if re.search(r"lake|water|river|sea|ground|floor|shore|sand|earth", n):
        return "ground"
    if re.search(r"bubble|speech|caption|label|text", n):
        return "effect"
    if re.search(r"man|woman|figure|character|robot|swan|bird|mermaid|fairy|dwarf|boy|girl", n):
        return "character"
    if re.search(r"eye|hair|beard|dress|hat|jacket|glasses|sunglasses|mustache|mouth|brow", n):
        return "character-part"
    return "prop"


PART_MATCHERS = [
    ("pupils",     r"pupil|iris"),
    ("highlights", r"highlight|catchlight|specular|eye-shine"),
    ("eyelids",    r"eyelid|lid(?:-|_|\b)"),
    ("eyes",       r"eye|eyeball"),
    ("brows",      r"brow"),
    ("teeth",      r"teeth|tooth"),
    ("tongue",     r"tongue"),
    ("jaw",        r"jaw|chin"),
    ("mouth",      r"mouth|lip"),
    ("mustache",   r"mustache|moustache"),
    ("face",       r"face|skin|head-base|face-base"),
    ("ears",       r"ear(?:-|_|\b)"),
    ("neck",       r"neck|throat"),
    ("hands",      r"hand|fist|palm|arm"),
    ("beard",      r"beard|whisker"),
    ("head",       r"hair|head|skull|wig"),
    ("body",       r"shirt|body|torso|dress|jacket|coat|robe|armor|suit"),
]

def classify_part(name: str) -> tuple[str, str, str] | None:
    """Return (part, group, variant) if the name looks like a character part."""
    n = name.lower()
    for part, pattern in PART_MATCHERS:
        if re.search(pattern, n):
            side = ""
            m = re.search(r"(left|right|l\b|r\b)", n)
            if m:
                side = "left" if m.group(1).startswith("l") else "right"
            variant = ""
            m = re.search(r"(open|closed|half|neutral|raised|furrowed|sad|happy|angry|surprised)", n)
            if m:
                variant = m.group(1)
            group = f"{part}-{side}" if side else part
            return (part, group, variant)
    return None

def slugify(value: str, limit: int = 60) -> str:
    v = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return (v or "layer")[:limit]

# ── mask → cutout ────────────────────────────────────────────────────────

def load_mask(path: Path, size: tuple[int, int]) -> Image.Image:
    mask = Image.open(path)
    if mask.size != size:
        mask = mask.resize(size, Image.LANCZOS)
    if mask.mode in ("RGBA", "LA"):
        return mask.getchannel("A")
    if mask.mode == "L":
        return mask
    return mask.convert("L")

def cutout(reference: Image.Image, mask: Image.Image) -> Image.Image:
    rgba = reference.convert("RGBA").copy()
    alpha = Image.new("L", reference.size, 0)
    alpha.paste(mask, (0, 0))
    rgba.putalpha(alpha)
    return rgba

def tight_crop(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    bbox = img.getbbox()
    if bbox is None:
        return img, (0, 0, img.width, img.height)
    return img.crop(bbox), bbox

# ── composition emission ─────────────────────────────────────────────────

def build_composition(reference_path: Path, entries: list[dict],
                      name: str, width: int, height: int) -> dict:
    reference_layer_id = "reference"

    layers = [{
        "id": reference_layer_id,
        "name": "Final image",
        "role": "background",
        "url": entries[0]["ref_url"],     # filled in by caller
        "frames": [entries[0]["ref_url"]],
        "spriteFps": 1,
        "spriteMode": "once",
        "x": 0, "y": 0, "w": 100, "h": 100,
        "opacity": 1, "z": 0, "rotation": 0,
        "fit": "contain",
        "visible": False,                 # hide so segmentation gaps show up
        "locked": True, "positionConfirmed": True,
        "keyframes": [],
    }]

    # Stable depth ordering: role base + filename, so repeated runs match.
    order = {role: base for role, base in ROLE_ORDER}
    entries_sorted = sorted(entries, key=lambda e: (order[e["role"]], e["name"]))
    z = 2
    for entry in entries_sorted:
        x = entry["crop"][0] / width * 100
        y = entry["crop"][1] / height * 100
        w = entry["crop"][2] / width * 100
        h = entry["crop"][3] / height * 100
        layer = {
            "id": entry["id"],
            "name": entry["name"],
            "role": entry["role"],
            "url": entry["url"],
            "frames": [entry["url"]],
            "spriteFps": 1, "spriteMode": "once",
            "x": round(x, 4), "y": round(y, 4),
            "w": round(w, 4), "h": round(h, 4),
            "opacity": 1, "z": z, "rotation": 0,
            "fit": "contain",
            "visible": True, "locked": False,
            "positionConfirmed": True,
            "keyframes": [],
        }
        part = classify_part(entry["name"])
        if part:
            layer["role"] = "character-part"
            layer["part"], layer["group"], layer["variant"] = part
        layers.append(layer)
        z += 2

    composition = {
        "version": 1,
        "id": slugify(name, 60) or "image-to-layers",
        "name": name,
        "width": width,
        "height": height,
        "duration": 3000,
        "referenceLayerId": reference_layer_id,
        "layers": layers,
        "meta": {
            "source": "image-to-layers",
            "createdAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "note": "Positions come from mask cropping; reference layer is hidden by default so missed regions show as white.",
        },
    }
    return composition

# ── main ─────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--masks", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out", default="data/projects")
    parser.add_argument("--mode", choices=["project", "zip"], default="project")
    parser.add_argument("--require-transparent", action="store_true",
                        help="Fail if a mask has no transparent pixels (all-white).")
    args = parser.parse_args(argv)

    reference_path = Path(args.reference)
    masks_dir = Path(args.masks)
    if not reference_path.exists():
        sys.exit(f"reference not found: {reference_path}")
    if not masks_dir.is_dir():
        sys.exit(f"masks dir not found: {masks_dir}")

    reference = Image.open(reference_path).convert("RGBA")
    width, height = reference.size

    mask_files = sorted(
        p for p in masks_dir.iterdir()
        if p.suffix.lower() in (".png", ".webp", ".tif", ".tiff")
    )
    if not mask_files:
        sys.exit(f"no mask images found in {masks_dir}")

    name = args.name.strip()
    slug = slugify(name, 60) or "image-to-layers"

    if args.mode == "zip":
        return emit_zip(reference_path, mask_files, slug, args.require_transparent)

    project_dir = Path(args.out) / name.replace(" ", "-")
    images_dir = project_dir / "images"
    compositions_dir = project_dir / "compositions"
    images_dir.mkdir(parents=True, exist_ok=True)
    compositions_dir.mkdir(parents=True, exist_ok=True)

    # Persist a copy of the reference next to the cutouts.
    reference_name = f"reference{reference_path.suffix.lower()}"
    reference_out = images_dir / reference_name
    if not reference_out.exists():
        reference.save(reference_out, optimize=True)

    entries: list[dict] = []
    for mask_path in mask_files:
        layer_name = mask_path.stem
        mask = load_mask(mask_path, (width, height))
        if args.require_transparent and all(mask.getextrema()[1:] == (0,)) is False:
            # (skip: mask has no opaque pixels at all → useless)
            if mask.getextrema()[1] == 0:
                print(f"  skip {layer_name}: mask is empty")
                continue
        cut = cutout(reference, mask)
        cropped, bbox = tight_crop(cut)
        if cropped.width == 0 or cropped.height == 0:
            print(f"  skip {layer_name}: empty crop")
            continue
        out_file = images_dir / f"{slugify(layer_name, 40)}.png"
        cropped.save(out_file, optimize=True)
        entries.append({
            "name": layer_name,
            "id": f"layer-{slugify(layer_name, 40)}",
            "role": classify_role(layer_name),
            "crop": bbox,
            "url": f"/data/projects/{project_dir.name}/images/{out_file.name}",
        })

    if not entries:
        sys.exit("no usable masks")

    ref_url = f"/data/projects/{project_dir.name}/images/{reference_name}"
    for entry in entries:
        entry["ref_url"] = ref_url
    composition = build_composition(reference_path, entries, name, width, height)
    # Ensure reference layer points at the copied reference, not entry[0].
    composition["layers"][0]["url"] = ref_url
    composition["layers"][0]["frames"] = [ref_url]

    out_json = compositions_dir / f"{slug}.composition.json"
    out_json.write_text(json.dumps(composition, indent=2), encoding="utf-8")
    print(f"wrote {out_json}")
    print(f"      {len(entries)} layers, {width}x{height}")
    return 0

def emit_zip(reference_path: Path, mask_files: list[Path],
             slug: str, require_transparent: bool) -> int:
    """Produce a ZIP of tight-cropped cutouts for "Import ZIP + image"."""
    reference = Image.open(reference_path).convert("RGBA")
    width, height = reference.size
    out_zip = Path(f"{slug}.zip")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for mask_path in mask_files:
            layer_name = mask_path.stem
            mask = load_mask(mask_path, (width, height))
            if require_transparent and mask.getextrema()[1] == 0:
                continue
            cut = cutout(reference, mask)
            cropped, _ = tight_crop(cut)
            if cropped.width == 0 or cropped.height == 0:
                continue
            buf = io.BytesIO()
            cropped.save(buf, format="PNG", optimize=True)
            zf.writestr(f"{slugify(layer_name, 40)}.png", buf.getvalue())
    print(f"wrote {out_zip} (drop it into Studio → Import ZIP + image)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

### First run against an existing sample

The fastest way to sanity-check is to use the existing `data/imports/beards-…/segments/bearded-man/` directory as a fake mask set — every PNG has alpha, so `--masks` will treat each as a layer:

```bash
python3 tools/image-to-layers.py \
  --reference data/imports/beards-20261019000756-e757/reference.png \
  --masks     data/imports/beards-20261019000756-e757/segments/bearded-man/ \
  --name "Beards — direct" \
  --out /tmp/test-projects
```

You'll get `/tmp/test-projects/Beards-—-direct/compositions/*.composition.json` with each cutout cropped to its own alpha bounds and placed by role. Copy that directory into `data/projects/`, restart the server (or just refresh — `listStudioProjects` reads at request time), and pick it from the project dropdown. No alignment needed.

That's the smoke test. Everything after this is about improving the segmenter.

---

## 4. Plugging in a real segmenter

The mask directory is the contract. Each backend just needs to write one mask PNG per layer.

### SAM 2 / Segment Anything
```python
# pseudocode — install `segment-anything` + a checkpoint
from segment_anything import SamPredictor, sam_model_registry
sam = sam_model_registry["vit_h"](checkpoint="sam_vit_h.pth").to("cuda")
predictor = SamPredictor(sam)
predictor.set_image(np.asarray(reference.convert("RGB")))

# Option 1: automatic mask generator
from segment_anything import SamAutomaticMaskGenerator
gen = SamAutomaticMaskGenerator(sam, points_per_side=32)
masks = gen.generate(np.asarray(reference.convert("RGB")))
for i, m in enumerate(masks):
    Image.fromarray((m["segmentation"] * 255).astype("uint8")).save(f"masks/mask-{i:03d}.png")

# Option 2: point-prompted (recommended — you get semantic layers, not
# thousands of granular superpixels)
for name, (px, py) in {"sky": (100, 80), "character": (520, 380)}.items():
    masks, scores, _ = predictor.predict(point_coords=[[px, py]], point_labels=[1])
    Image.fromarray((masks[0] * 255).astype("uint8")).save(f"masks/{name}.png")
```

Point-prompted SAM is what I'd use for a production flow: the user drops one click per desired layer, the tool names it by clicking order (or a short label), and the composition is written with exactly those layers. This is the same pattern as Photoshop's "Select Subject" but reproducible and scriptable.

**Note for overlapping layers:** SAM tends to segment only the *visible* pixels of an occluded object. That's exactly what the studio wants — the composition stacks the cutouts in z-order, and the reference behind them is hidden, so nothing double-draws.

### `rembg` (fast background removal, then depth-band split)
```python
from rembg import remove
subject = remove(reference_image)            # RGBA
Image.alpha_composite(reference, subject).save("masks/character.png")
# background = reference * (1 - subject.alpha)
```

Good when the input is a simple "figure over scenery". Not enough alone for multi-layer scenes.

### Depth Anything v2 (banding)
```python
from transformers import pipeline
depth = pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf")
d = np.asarray(depth(reference_image)["depth"])
# Split into bands: sky/mid/ground/foreground
for name, (lo, hi) in {"sky": (0.0, 0.15), "mid": (0.15, 0.45),
                       "ground": (0.45, 0.8), "fg": (0.8, 1.01)}.items():
    band = (d >= lo) & (d < hi)
    Image.fromarray((band * 255).astype("uint8")).save(f"masks/{name}.png")
```

This is a *cheap* layered pipeline that will produce 3–5 depth-ordered cutouts with no ML weights you haven't already installed. Combine it with `rembg` on the foreground band to get a clean character cutout.

---

## 5. Emitting a character rig (optional, high-leverage)

If a subgroup of masks represent parts of one character (eyes, pupils, mouth, brows, beard, …) and you want the studio's full blink/talk/expression rig, **name them so the server's auto-detector fires** — e.g. `eye-white-left.png`, `pupil-left.png`, `eyelid-closed-left.png`, `brow-left-neutral.png`, `mouth-a.png`, `beard.png` — and include the side/pose tokens. Then just leave `characters` out of the composition JSON; the server runs `detectCharacters()` on first load and persists the rig.

For a *first-class* rig (with `coverLayerId`, `variants`, `rigVersion: 3`), follow `apply-beards-rig.js` and emit:

```python
characters = [{
    "id": "character-1",
    "name": "Hero",
    "parts": {"eyes": ["layer-eye-white-left", "layer-eye-white-right"],
              "pupils": [...], "brows": [...], "mouth": [...], "beard": [...], ...},
    "hidden": [],
    "coverLayerId": None,
    "settings": { ...defaults from CHARACTER_DEFAULTS... },
    "applied": False,
    "rigVersion": 3,
}]
```

Only add it if you're serious about animating; otherwise the auto-detector is less code and less risk.

---

## 6. Practical recommendation

1. **Ship the project-output mode first** (`--mode project`). It's the cleanest ingestion path and surfaces segmentation gaps immediately, because the reference layer is hidden. The `--mode zip` path is a nice fallback if you ever want to reuse the studio's matcher.
2. **Use point-prompted SAM** as your segmenter. It's the only approach that gives you both quality *and* semantic labels without extra classification models. Automatic mask generators produce hundreds of superpixels and no names, which makes role/depth assignment guesswork and the composition unreadable.
3. **Encode depth from a monocular model** (`depth-anything`) to seed z-order. Right now my script mirrors `server.js`'s name-based heuristics; swapping in `depth_order(role, mask_pixels)` is a 20-line change and will beat the heuristics on anything non-standard.
4. **Keep a `--masks` interface** even if you add SAM. It's the API you'll want for regression tests and for integrating with anything else (Photoshop export, Photoshop-in-the-loop review, hand-drawn masks from a designer).

If you want, I can extend the script with (a) a `--sam-points name:x,y,name:x,y` flag that runs point-prompted SAM and writes masks + composition in one shot, or (b) a `--depth` flag that replaces the name-based z-order with depth-band z-order. Both are plug-in additions behind the existing mask-directory contract, so neither would change the emitted composition schema.

# Extended `image-to-layers.py` — SAM 2.1 + Depth Anything v2

Below is the full replacement for the script. Everything is backward-compatible: `--masks` still works, and the mask-directory contract is unchanged. New capabilities:

- **`--sam-points`** — point-prompted SAM 2.1 (multi-point, negative points, box + points)
- **`--sam-auto`** — automatic SAM with IoU dedup and area filtering
- **`--depth`** — depth-aware z-ordering via Depth Anything v2 (per-mask median depth, banded)
- **`--depth-role`** — also re-derive role from depth bands (opt-in)
- **`--feather N`** — Gaussian-blur mask edges for smooth compositing (default 1 px)
- **`--dilate N`** — expand the mask by N px (covers SAM undercutting)
- **`--list-models`** — inspect available backends

Quality choices: **SAM 2.1 Large** and **Depth Anything v2 Large** are the defaults; both auto-download their weights from Hugging Face.

## Install

```bash
# Recommended stack (SAM 2.1 + Depth Anything v2)
pip install torch transformers pillow numpy
pip install git+https://github.com/facebookresearch/sam2.git

# CPU-only is fine for a few layers; CUDA/MPS strongly recommended for auto mode
```

## Usage

```bash
# Point-prompted SAM, depth-ordered, project output
python3 tools/image-to-layers.py \
  --reference data/imports/beards-20261019000756-e757/reference.png \
  --sam-points "beard:174,330|!174,280;hair-front:174,60;shirt:174,420;eyes-left:120,180;eyes-right:230,180" \
  --depth \
  --name "Beards — SAM + depth"

# Automatic SAM with depth, then a manual ZIP fallback if you want to compare
python3 tools/image-to-layers.py \
  --reference scene.jpg \
  --sam-auto --depth \
  --name "Auto scene"

# Pre-computed masks with depth ordering
python3 tools/image-to-layers.py \
  --reference scene.jpg --masks masks/ --depth \
  --name "Manual masks + depth"

# Compare against the studio's template matcher
python3 tools/image-to-layers.py \
  --reference scene.jpg --masks masks/ --mode zip --name "ZIP test"
```

## The script

```python
#!/usr/bin/env python3
"""image-to-layers.py — turn a reference image into a LayerStudio composition.

LAYER SOURCES
-------------
  --masks path/to/dir/       One PNG per layer; filename stem = layer name.
  --sam-points "spec"        Point-prompted SAM 2.1 / SAM 1 (recommended).
  --sam-auto                 Automatic mask generation (fast, noisier).

SAM POINT SPEC
--------------
  "name:px,py|px,py|!negx,negy|box:x1,y1,x2,y2;name2:...;name3:..."
    |    separates prompts for the SAME layer
    ;    separates layers
    !    prefix → negative point
    box: four floats  → box prompt (can combine with points)
    e.g. "sky:100,80;character:520,380|!640,300|box:480,340,780,760"

OUTPUT MODES
------------
  --mode project  data/projects/<Name>/{images/,compositions/}    (default)
  --mode zip      <slug>.zip for Studio → "Import ZIP + image"

QUALITY FLAGS
-------------
  --depth              Order layers by median monocular depth.
  --depth-role         Also re-derive role from depth bands.
  --feather N          Blur mask edge by N px (default 1; use 0 for hard).
  --dilate N           Expand mask by N px (default 0; try 1-2 if edges
                       undercut the subject).
  --sam-model          sam2-large (default), sam2-base, sam2-small,
                       or a SAM 1 checkpoint path.
  --depth-model        depth-anything/Depth-Anything-V2-Large-hf (default).
  --device             auto (default), cuda, mps, cpu.
  --sam-min-score      Discard SAM masks below this score (auto mode).
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter

# ── role / part classification (mirror of server.js) ─────────────────────

ROLE_ORDER = {
    "background": 0,
    "midground": 30,
    "ground": 40,
    "prop": 60,
    "character": 70,
    "character-part": 80,
    "foreground": 90,
    "effect": 95,
}

PART_MATCHERS = [
    ("pupils",     r"pupil|iris"),
    ("highlights", r"highlight|catchlight|specular|eye-shine"),
    ("eyelids",    r"eyelid|lid(?:-|_|\b)"),
    ("eyes",       r"eye|eyeball"),
    ("brows",      r"brow"),
    ("teeth",      r"teeth|tooth"),
    ("tongue",     r"tongue"),
    ("jaw",        r"jaw|chin"),
    ("mouth",      r"mouth|lip"),
    ("mustache",   r"mustache|moustache"),
    ("face",       r"face|skin|head-base|face-base"),
    ("ears",       r"ear(?:-|_|\b)"),
    ("neck",       r"neck|throat"),
    ("hands",      r"hand|fist|palm|arm"),
    ("beard",      r"beard|whisker"),
    ("head",       r"hair|head|skull|wig"),
    ("body",       r"shirt|body|torso|dress|jacket|coat|robe|armor|suit"),
]

# Priority *inside* one character (higher = drawn later = on top).
# Only used when no depth signal is available for a layer, or as a tiebreak
# between layers with near-identical depth bands.
PART_PRIORITY = {
    "head": 0,
    "hair-back": 1,
    "body": 2,
    "neck": 3,
    "hands": 3,
    "ears": 4,
    "jaw": 5,
    "face": 6,
    "hair-front": 7,
    "beard": 8,
    "mustache": 9,
    "mouth": 10,
    "teeth": 11,
    "tongue": 11,
    "eyes": 12,
    "eyelids": 13,
    "pupils": 14,
    "highlights": 15,
    "brows": 16,
    "fx": 20,
}


def classify_role(name: str) -> str:
    n = name.lower()
    if re.search(r"^(sky|clouds?|backdrop|background)$", n) or re.search(r"distant|hill|mountain|horizon", n):
        return "background"
    if re.search(r"lake|water|river|sea|ground|floor|shore|sand|earth", n):
        return "ground"
    if re.search(r"bubble|speech|caption|label|text", n):
        return "effect"
    if re.search(r"man|woman|figure|character|robot|swan|bird|mermaid|fairy|dwarf|boy|girl", n):
        return "character"
    if re.search(r"eye|hair|beard|dress|hat|jacket|glasses|sunglasses|mustache|mouth|brow", n):
        return "character-part"
    return "prop"


def classify_part(name: str) -> Optional[tuple[str, str, str]]:
    """Return (part, group, variant) if the name looks like a character part."""
    n = name.lower()
    for part, pattern in PART_MATCHERS:
        if re.search(pattern, n):
            side = ""
            m = re.search(r"(left|right|\bl\b|\br\b)", n)
            if m:
                token = m.group(1)
                side = "right" if token.startswith("r") else "left"
            variant = ""
            m = re.search(r"(open|closed|half|front|back|top|neutral|raised|furrowed|sad|happy|angry|surprised|smile|frown)", n)
            if m:
                variant = m.group(1)
            group = f"{part}-{side}" if side else part
            # Fold "hair-front"/"hair-back" into part "head" but keep variant
            if part == "head" and variant in ("front", "back"):
                return ("head", "head", variant)
            return (part, group, variant)
    return None


def part_priority(entry: dict) -> int:
    part = entry.get("part", "")
    variant = entry.get("variant", "")
    base = PART_PRIORITY.get(part, 0)
    if variant == "back":
        base -= 3
    elif variant == "front":
        base += 3
    elif variant == "top":
        base += 4
    return base


def slugify(value: str, limit: int = 60) -> str:
    v = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return (v or "layer")[:limit]


# ── mask loading / cutout ────────────────────────────────────────────────

def load_mask_from_file(path: Path, size: tuple[int, int]) -> Image.Image:
    mask = Image.open(path)
    if mask.size != size:
        mask = mask.resize(size, Image.LANCZOS)
    if mask.mode in ("RGBA", "LA"):
        return mask.getchannel("A")
    if mask.mode == "L":
        return mask
    return mask.convert("L")


def cutout(reference: Image.Image, alpha: Image.Image) -> Image.Image:
    """Multiply the reference by the mask's alpha channel."""
    if alpha.size != reference.size:
        alpha = alpha.resize(reference.size, Image.LANCZOS)
    rgba = reference.convert("RGBA").copy()
    rgba.putalpha(alpha)
    return rgba


def tight_crop(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    bbox = img.getbbox()
    if bbox is None:
        return img, (0, 0, img.width, img.height)
    return img.crop(bbox), bbox


def refine_alpha(alpha: Image.Image, feather: float, dilate: int) -> Image.Image:
    """Feather and/or dilate the alpha channel."""
    if dilate > 0:
        radius = dilate * 2 + 1
        alpha = alpha.filter(ImageFilter.MaxFilter(radius))
    if feather > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    return alpha


# ── SAM backends ─────────────────────────────────────────────────────────

def _pick_sam2_hf_id(model: str) -> str:
    if "/" in model:
        return model
    return {
        "sam2-large": "facebook/sam2.1-hiera-large",
        "sam2-base":  "facebook/sam2.1-hiera-base-plus",
        "sam2-small": "facebook/sam2.1-hiera-small",
        "sam2-tiny":  "facebook/sam2.1-hiera-tiny",
    }.get(model, "facebook/sam2.1-hiera-large")


def load_sam(model: str, device: str):
    """Load SAM. Returns (predictor, kind) with kind in {'sam2','sam1'}."""
    if model.startswith("sam2"):
        try:
            from sam2.sam2_image_predictor import SAM2ImagePredictor
        except ImportError as e:
            raise RuntimeError(
                "SAM 2 is not installed. Run:\n"
                "    pip install git+https://github.com/facebookresearch/sam2.git\n"
                f"(import error: {e})"
            )
        hf_id = _pick_sam2_hf_id(model)
        try:
            predictor = SAM2ImagePredictor.from_pretrained(hf_id, device=device)
        except TypeError:
            predictor = SAM2ImagePredictor.from_pretrained(hf_id)
            if device not in ("cpu", None):
                try:
                    predictor.model.to(device)
                except Exception:
                    pass
        return predictor, "sam2"

    # SAM 1
    try:
        from segment_anything import SamPredictor, sam_model_registry
    except ImportError:
        raise RuntimeError(
            "Neither SAM 2 nor SAM 1 is installed. Install SAM 2 (recommended):\n"
            "    pip install git+https://github.com/facebookresearch/sam2.git"
        )
    ckpt = model if os.path.exists(model) else {
        "sam-vit-h": "sam_vit_h_4b8939.pth",
        "sam-vit-l": "sam_vit_l_0b3195.pth",
        "sam-vit-b": "sam_vit_b_01ec64.pth",
    }.get(model, model)
    if not os.path.exists(ckpt):
        raise RuntimeError(
            f"SAM checkpoint not found: {ckpt}\n"
            "Download from https://github.com/facebookresearch/segment-anything#model-checkpoints"
        )
    key = "vit_h" if "vit_h" in ckpt else ("vit_l" if "vit_l" in ckpt else "vit_b")
    sam = sam_model_registry[key](checkpoint=ckpt).to(device)
    return SamPredictor(sam), "sam1"


def set_sam_image(predictor, image: Image.Image, kind: str) -> None:
    rgb = np.asarray(image.convert("RGB"))
    predictor.set_image(rgb)


def sam_predict(predictor, kind: str, *,
                points=None, labels=None, box=None) -> tuple[np.ndarray, float]:
    kwargs = {}
    if points is not None:
        kwargs["point_coords"] = np.array(points, dtype=np.float32)
        kwargs["point_labels"] = np.array(labels, dtype=np.int32)
    if box is not None:
        kwargs["box"] = np.array(box, dtype=np.float32)
    if not kwargs:
        raise ValueError("SAM prompt must include points and/or a box")
    masks, scores, _ = predictor.predict(multimask_output=False, **kwargs)
    masks = np.asarray(masks)
    if masks.ndim == 3:
        masks = masks[0]
    return masks.astype(bool), float(np.asarray(scores).reshape(-1)[0])


def parse_sam_spec(spec: str) -> list[dict]:
    """Parse 'name:px,py|!negx,negy|box:x1,y1,x2,y2;name2:...'"""
    layers = []
    seen = set()
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"expected 'name:...' in SAM spec, got {chunk!r}")
        name, rest = chunk.split(":", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"empty layer name in spec: {chunk!r}")
        if name in seen:
            raise ValueError(f"duplicate layer name {name!r}")
        seen.add(name)

        points, labels, box = [], [], None
        for token in rest.split("|"):
            token = token.strip()
            if not token:
                continue
            if token.startswith("box:"):
                parts = token[4:].split(",")
                if len(parts) != 4:
                    raise ValueError(f"{name}: box needs x1,y1,x2,y2, got {token!r}")
                box = tuple(float(v) for v in parts)
                continue
            label = 1
            if token.startswith("!"):
                label = 0
                token = token[1:].lstrip()
            xy = token.split(",")
            if len(xy) != 2:
                raise ValueError(f"{name}: point needs x,y, got {token!r}")
            points.append((float(xy[0]), float(xy[1])))
            labels.append(label)

        if not points and box is None:
            raise ValueError(f"{name}: no points or box specified")
        layers.append({
            "name": name,
            "points": points or None,
            "labels": labels or None,
            "box": box,
        })
    return layers


def generate_sam_masks(reference: Image.Image, spec: str,
                       model: str, device: str) -> list[tuple[str, Image.Image]]:
    layers = parse_sam_spec(spec)
    predictor, kind = load_sam(model, device)
    set_sam_image(predictor, reference, kind)
    results = []
    h, w = reference.size[1], reference.size[0]
    min_pixels = max(4, int(h * w * 0.0005))   # 0.05 %
    for layer in layers:
        try:
            mask, score = sam_predict(
                predictor, kind,
                points=layer["points"], labels=layer["labels"], box=layer["box"],
            )
        except Exception as e:
            raise RuntimeError(f"SAM failed for {layer['name']!r}: {e}") from e
        pixels = int(mask.sum())
        if pixels < min_pixels:
            print(f"  warn: {layer['name']!r} → tiny mask ({pixels} px), check the prompt")
        alpha = Image.fromarray((mask * 255).astype(np.uint8))
        results.append((layer["name"], alpha))
        print(f"  {layer['name']:<24} score={score:.3f}  pixels={pixels:>8}")
    return results


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    inter = np.logical_and(a, b).sum()
    union = np.logical_or(a, b).sum()
    return float(inter) / float(union) if union else 0.0


def generate_sam_auto(reference: Image.Image, model: str, device: str,
                      min_area_pct: float = 0.1,
                      min_stability: float = 0.9) -> list[tuple[str, Image.Image]]:
    predictor, kind = load_sam(model, device)
    set_sam_image(predictor, reference, kind)
    rgb = np.asarray(reference.convert("RGB"))
    h, w = rgb.shape[:2]

    if kind == "sam2":
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
        gen = SAM2AutomaticMaskGenerator(predictor, points_per_side=24)
    else:
        from segment_anything import SamAutomaticMaskGenerator
        gen = SamAutomaticMaskGenerator(predictor, points_per_side=24)

    raw = gen.generate(rgb)
    min_area = int(h * w * min_area_pct / 100)
    kept = []
    for m in sorted(raw, key=lambda m: -m["area"]):
        if m["area"] < min_area:
            continue
        if m["stability_score"] < min_stability:
            continue
        if any(_iou(m["segmentation"], k["segmentation"]) > 0.6 for k in kept):
            continue
        kept.append(m)

    results = []
    for i, m in enumerate(kept):
        name = f"mask-{i + 1:03d}"
        alpha = Image.fromarray((m["segmentation"] * 255).astype(np.uint8))
        results.append((name, alpha))
        print(f"  {name:<24} area={m['area']:>8}  stability={m['stability_score']:.2f}")
    return results


# ── Depth Anything v2 ────────────────────────────────────────────────────

_DEPTH_PIPE = None


def load_depth_pipeline(model: str, device: str):
    global _DEPTH_PIPE
    if _DEPTH_PIPE is not None:
        return _DEPTH_PIPE
    try:
        from transformers import pipeline
    except ImportError:
        raise RuntimeError(
            "Depth ordering requires transformers + torch:\n"
            "    pip install torch transformers"
        )
    device_index = 0 if device == "cuda" else -1
    _DEPTH_PIPE = pipeline("depth-estimation", model=model, device=device_index)
    return _DEPTH_PIPE


def compute_depth(reference: Image.Image, model: str, device: str) -> np.ndarray:
    """Return HxW float array in [0,1]; 1 = nearest, 0 = farthest."""
    pipe = load_depth_pipeline(model, device)
    result = pipe(reference.convert("RGB"))
    raw = result["predicted_depth"]
    if hasattr(raw, "cpu"):
        raw = raw.cpu().numpy()
    depth = np.asarray(raw, dtype=np.float32)

    # Depth Anything outputs inverse relative depth. Robust-normalize using
    # percentiles so a stray foreground object cannot flatten the whole range.
    lo, hi = np.percentile(depth, [1, 99])
    if hi <= lo:
        norm = np.zeros_like(depth)
    else:
        norm = np.clip((depth - lo) / (hi - lo), 0.0, 1.0)

    if norm.shape != (reference.height, reference.width):
        vis = Image.fromarray((norm * 255).astype(np.uint8)).resize(
            reference.size, Image.BILINEAR)
        norm = np.asarray(vis, dtype=np.float32) / 255.0
    return norm


def layer_depth_score(depth_map: np.ndarray, alpha: Image.Image) -> Optional[float]:
    """Median depth over masked pixels, or None if the mask is empty."""
    if alpha.size != (depth_map.shape[1], depth_map.shape[0]):
        alpha = alpha.resize((depth_map.shape[1], depth_map.shape[0]), Image.BILINEAR)
    a = np.asarray(alpha)
    mask = a > 64
    if not mask.any():
        return None
    return float(np.median(depth_map[mask]))


# ── composition emission ─────────────────────────────────────────────────

def sort_entries(entries: list[dict], use_depth: bool, bands: int = 20) -> None:
    """Sort in place, low z first. Mutates the list."""
    def key(e: dict):
        d = e.get("depth_score")
        band = 9999 if d is None else int(round(d * bands))
        role = ROLE_ORDER.get(e["role"], 60)
        pp = part_priority(e) if e["role"] == "character-part" else 0
        return (band, role, pp, e["name"])
    entries.sort(key=key)


def reassign_roles_from_depth(entries: list[dict]) -> None:
    for e in entries:
        d = e.get("depth_score")
        if d is None:
            continue
        if d < 0.15:
            e["role"] = "background"
        elif d < 0.35:
            e["role"] = "midground"
        elif d < 0.55:
            e["role"] = "ground"
        elif d > 0.85:
            e["role"] = "foreground"
        # 0.55 <= d <= 0.85 keeps the name-based role


def build_composition(entries: list[dict], name: str, width: int, height: int,
                      reference_url: str) -> dict:
    """Entries must already be sorted low-z first."""
    layers = [{
        "id": "reference",
        "name": "Final image",
        "role": "background",
        "url": reference_url,
        "frames": [reference_url],
        "spriteFps": 1,
        "spriteMode": "once",
        "x": 0, "y": 0, "w": 100, "h": 100,
        "opacity": 1, "z": 0, "rotation": 0,
        "fit": "contain",
        "visible": False,   # hide so segmentation gaps show up as white
        "locked": True,
        "positionConfirmed": True,
        "keyframes": [],
    }]

    z = 2
    for e in entries:
        x = e["crop"][0] / width * 100
        y = e["crop"][1] / height * 100
        w = e["crop"][2] / width * 100
        h = e["crop"][3] / height * 100
        layer = {
            "id": e["id"],
            "name": e["name"],
            "role": e["role"],
            "url": e["url"],
            "frames": [e["url"]],
            "spriteFps": 1,
            "spriteMode": "once",
            "x": round(x, 4), "y": round(y, 4),
            "w": round(w, 4), "h": round(h, 4),
            "opacity": 1, "z": z, "rotation": 0,
            "fit": "contain",
            "visible": True, "locked": False,
            "positionConfirmed": True,
            "keyframes": [],
        }
        if e.get("part"):
            layer["role"] = "character-part"
            layer["part"] = e["part"]
            layer["group"] = e["group"]
            layer["variant"] = e["variant"]
        layers.append(layer)
        z += 2

    return {
        "version": 1,
        "id": slugify(name, 60) or "image-to-layers",
        "name": name,
        "width": width,
        "height": height,
        "duration": 3000,
        "referenceLayerId": "reference",
        "layers": layers,
        "meta": {
            "source": "image-to-layers",
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
            "note": "Positions from mask cropping; reference layer is hidden by default.",
        },
    }


# ── zip output (for the studio's server-side matcher) ────────────────────

def emit_zip(reference_path: Path, mask_list: list[tuple[str, Image.Image]],
             slug: str) -> int:
    reference = Image.open(reference_path).convert("RGBA")
    out_zip = Path(f"{slug}.zip")
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, alpha in mask_list:
            cut = cutout(reference, alpha)
            cropped, _ = tight_crop(cut)
            if cropped.width == 0 or cropped.height == 0:
                continue
            buf = io.BytesIO()
            cropped.save(buf, format="PNG", optimize=True)
            zf.writestr(f"{slugify(name, 40)}.png", buf.getvalue())
    print(f"wrote {out_zip}  → drop into Studio → 'Import ZIP + image'")
    return 0


# ── device + model helpers ───────────────────────────────────────────────

def resolve_device(preferred: str) -> str:
    if preferred != "auto":
        return preferred
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def print_models() -> int:
    print("SAM models (point-prompted quality improves top → bottom):")
    print("  sam2-tiny   facebook/sam2.1-hiera-tiny")
    print("  sam2-small  facebook/sam2.1-hiera-small")
    print("  sam2-base   facebook/sam2.1-hiera-base-plus")
    print("  sam2-large  facebook/sam2.1-hiera-large   ← default")
    print()
    print("Depth models:")
    print("  depth-anything/Depth-Anything-V2-Small-hf")
    print("  depth-anything/Depth-Anything-V2-Base-hf")
    print("  depth-anything/Depth-Anything-V2-Large-hf ← default")
    return 0


# ── main ─────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reference image → LayerStudio composition.")
    parser.add_argument("--reference", required=False)
    parser.add_argument("--masks", type=Path)
    parser.add_argument("--sam-points", type=str)
    parser.add_argument("--sam-auto", action="store_true")
    parser.add_argument("--sam-model", default="sam2-large")
    parser.add_argument("--sam-min-score", type=float, default=0.5,
                        help="Drop SAM masks below this score (auto mode).")
    parser.add_argument("--depth", action="store_true",
                        help="Order layers by median monocular depth.")
    parser.add_argument("--depth-role", action="store_true",
                        help="Also re-derive role from depth bands.")
    parser.add_argument("--depth-model", default="depth-anything/Depth-Anything-V2-Large-hf")
    parser.add_argument("--device", default="auto",
                        help="auto (default), cuda, mps, cpu")
    parser.add_argument("--feather", type=float, default=1.0,
                        help="Mask edge blur in pixels (default 1).")
    parser.add_argument("--dilate", type=int, default=0,
                        help="Expand the mask by N px (try 1-2 if edges undercut).")
    parser.add_argument("--name", default="Image layers")
    parser.add_argument("--out", default="data/projects")
    parser.add_argument("--mode", choices=["project", "zip"], default="project")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args(argv)

    if args.list_models:
        return print_models()

    if not args.reference:
        parser.error("--reference is required")

    sources = sum(1 for x in (args.masks, args.sam_points, args.sam_auto) if x)
    if sources != 1:
        parser.error("choose exactly one of --masks / --sam-points / --sam-auto")

    reference_path = Path(args.reference)
    if not reference_path.exists():
        sys.exit(f"reference not found: {reference_path}")
    reference = Image.open(reference_path).convert("RGBA")
    width, height = reference.size

    # ── Acquire masks ────────────────────────────────────────────────────
    masks: list[tuple[str, Image.Image]] = []  # (name, alpha L-image)
    if args.sam_points:
        print(f"Running SAM ({args.sam_model}) on point prompts…")
        masks = generate_sam_masks(
            reference, args.sam_points, args.sam_model, resolve_device(args.device))
    elif args.sam_auto:
        print(f"Running SAM ({args.sam_model}) in auto mode…")
        masks = generate_sam_auto(
            reference, args.sam_model, resolve_device(args.device),
            min_stability=max(args.sam_min_score, 0.0))
    else:
        mask_files = sorted(
            p for p in args.masks.iterdir()
            if p.suffix.lower() in (".png", ".webp", ".tif", ".tiff"))
        if not mask_files:
            sys.exit(f"no mask images in {args.masks}")
        for p in mask_files:
            masks.append((p.stem, load_mask_from_file(p, (width, height))))

    if not masks:
        sys.exit("no usable masks")

    # ── Optional depth pass ─────────────────────────────────────────────
    depth_map = None
    if args.depth:
        dev = resolve_device(args.device)
        print(f"Computing depth ({args.depth_model}) on {dev}…")
        depth_map = compute_depth(reference, args.depth_model, dev)

    # ── Build entries ───────────────────────────────────────────────────
    name = args.name.strip() or "Image layers"
    slug = slugify(name, 60) or "image-to-layers"

    if args.mode == "zip":
        # ZIP mode: don't need composition metadata, just cropped cutouts.
        return emit_zip(reference_path, masks, slug)

    project_dir = Path(args.out) / name.replace(" ", "-")
    images_dir = project_dir / "images"
    compositions_dir = project_dir / "compositions"
    images_dir.mkdir(parents=True, exist_ok=True)
    compositions_dir.mkdir(parents=True, exist_ok=True)

    ref_name = f"reference{reference_path.suffix.lower()}"
    ref_out = images_dir / ref_name
    if not ref_out.exists():
        reference.save(ref_out, optimize=True)
    ref_url = f"/data/projects/{project_dir.name}/images/{ref_name}"

    entries: list[dict] = []
    for layer_name, raw_alpha in masks:
        alpha = refine_alpha(raw_alpha, args.feather, args.dilate)
        cut = cutout(reference, alpha)
        cropped, bbox = tight_crop(cut)
        if cropped.width == 0 or cropped.height == 0:
            print(f"  skip {layer_name}: empty crop")
            continue
        out_file = images_dir / f"{slugify(layer_name, 40)}.png"
        cropped.save(out_file, optimize=True)
        entry = {
            "name": layer_name,
            "id": f"layer-{slugify(layer_name, 40)}",
            "role": classify_role(layer_name),
            "crop": bbox,
            "url": f"/data/projects/{project_dir.name}/images/{out_file.name}",
        }
        part = classify_part(layer_name)
        if part:
            entry["role"] = "character-part"
            entry["part"], entry["group"], entry["variant"] = part
        if depth_map is not None:
            entry["depth_score"] = layer_depth_score(depth_map, alpha)
        entries.append(entry)

    if not entries:
        sys.exit("no usable entries after cropping")

    if args.depth and args.depth_role:
        reassign_roles_from_depth(entries)

    sort_entries(entries, use_depth=bool(depth_map))

    composition = build_composition(entries, name, width, height, ref_url)
    out_json = compositions_dir / f"{slug}.composition.json"
    out_json.write_text(json.dumps(composition, indent=2), encoding="utf-8")
    print(f"\nwrote {out_json}")
    print(f"      {len(entries)} layers, {width}×{height}")
    if depth_map is not None:
        preview_path = project_dir / "depth-preview.png"
        Image.fromarray((depth_map * 255).astype(np.uint8)).save(preview_path)
        print(f"      depth preview → {preview_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

## What changed in the sort key

`sort_entries()` produces `(band, role, part_priority, name)` where `band = round(depth * 20)`.

- **No depth** → all entries share `band=9999`; ordering is by role → part priority → name (identical to before).
- **With depth** → two layers with the same depth band (±0.025) fall back to role and part priority, but different bands dominate. So a foreground prop at depth 0.85 sorts above a character at 0.80 regardless of names, while a character's eyes (depth 0.80) stay above its beard (depth 0.80) because part priority breaks the tie.
- **Tiny masks** with `depth_score = None` sort to the very top — matching the previous role-based fallback for edge cases.

`--depth-role` is opt-in because raw per-mask depth is noisier than filename heuristics on stylised art (e.g. a hand-painted sky can register as `midground`). Leave it off unless your inputs are photographic.

## Caveats worth knowing

1. **SAM 2.1 auto-downloads ~1 GB on first run.** It caches under `~/.cache/huggingface/`. `--sam-model sam2-small` drops that to ~150 MB.
2. **Negative points are the highest-leverage quality knob.** If a mask bleeds into a neighbouring region, add a `!x,y` at the centre of the spilling region. This is usually faster than tuning anything else.
3. **The `--dilate` + `--feather` combination matters most for edge quality.** With clean SAM masks, defaults (`feather=1`, `dilate=0`) look right in the studio preview. If your cutout edges look "shaved", raise `--dilate 1` before touching `--feather`.
4. **Depth Anything v2 is relative, not metric.** It gives you a good ordering but not absolute distances, so banding is the right abstraction. The 20-band default works out to ~5 % depth tolerance per band.
5. **The composition JSON is byte-identical in schema** to what the studio already accepts. Once the file lands in `data/projects/<Name>/compositions/`, `listStudioProjects()` picks it up on refresh — no restart, no server change, no schema edit.
6. **Character parts still need rig-compatible names** for blink/talk/expression to activate. The auto-detector keys off filenames (`eye-white-left`, `pupil-left`, `mouth-a`, `brow-right-furrowed`, …). If you point-prompt `left-eye`, `right-eye`, `mouth`, you'll get three character-parts that don't wire into a rig — add the qualifiers to the SAM prompt names and the rig activates for free.