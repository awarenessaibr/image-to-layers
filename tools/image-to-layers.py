#!/usr/bin/env python3
"""image-to-layers.py — render a LayerStudio composition from a reference image.

Layer sources (choose exactly one):
  --masks <dir>                      one mask PNG per layer
  --sam-points "spec"                point/box-prompted SAM 2.1
  --sam-auto                         SAM 2 automatic mask generation
  --export <png>                     recover holes from an aligned studio export
  --knockout <png>                   --reference is the FULL image; <png> is a cut-out of
                                     part of it. Emits a background layer = reference with
                                     the cut-out's exact silhouette removed.

Point prompt format (0–1 fractions of the canvas):
  "name:x,y|!x,y|box:x1,y1,x2,y2;name2:x,y;..."
    |    separates prompts for the SAME layer
    ;    separates layers
    !    prefix → negative point
    box:x1,y1,x2,y2 → box prompt (can combine with points)

Quality flags:
  --depth              sort entries by monocular depth
  --depth-role         also re-derive role from depth bands
  --depth-model ID     Depth Anything model id; 'none' = vertical heuristic
  --feather N          mask edge blur (default 1)
  --dilate N           mask edge expand (default 0)
  --decontaminate      replace edge RGB with dilated interior to kill halos
  --strict             fail on empty/full-canvas masks
  --knockout-underlap N   shrink the hole by N px so the cut-out overlaps the
                          background edge (hides seams; default 1)
  --knockout-keep-cutout  also emit the cut-out layer, re-cut from the reference
  --knockout-exclude "x,y;x,y"  keep the objects under these 0–1 canvas points
                          in the background instead of knocking them out
  --sam-points-fallback  skip SAM, always use circle approximation

Scene description (vision LLM → names, groups, SAM prompts):
  --describe-only        ask a vision LLM to look at the WHOLE image first and write
                         a scene description plus a list of named objects (name,
                         group, role, bbox, point). Writes --scene-out and exits.
                         Needs DEEPSEEK_API_KEY in .env or the environment.
  --scene-json PATH      use a scene description (from --describe-only or hand-edited)
  --sam-scene            layer source: prompt SAM 2 with every object in --scene-json
                         (box + centre point per object) → named, grouped masks
  --vision-model ID      vision model used by --describe-only
  --scene-detail 1-5     how fine the vision model should split the image
  --scene-hint TEXT      extra context for the vision model
  --scene-box-pad F      grow every scene box by F (0-1 fraction) before prompting SAM
  --scene-no-points      prompt SAM with the box only (no centre point)
  --scene-remainder      add an "unassigned" layer with the pixels no object claimed
  --scene-match F        with --sam-auto + --scene-json: minimum match score (0-1)
                         for giving an automatic mask the name of a scene object

Automatic SAM granularity (--sam-auto):
  --auto-points-per-side N  sampling grid; more points find smaller things (default 16)
  --auto-min-area PCT       drop masks smaller than PCT % of the canvas (default 0.1)
  --auto-min-stability F    drop unstable masks (default 0.85)
  --auto-pred-iou F         drop masks SAM itself is unsure about (default 0.8)
  --auto-nms-iou F          duplicate suppression overlap (default 0.55)
  --auto-containment F      drop masks mostly inside a bigger one (default 0.85)
  --auto-max-layers N       keep only the N largest masks (0 = no cap)

Output organisation:
  --folders group|flat   group = images/<group>/<layer>.png when a group holds
                         2+ layers (default); flat = everything in images/
                         A layers.json manifest and scene.json are always written.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageStat


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


load_dotenv(Path(__file__).resolve().parent)

# ── role / part classification ───────────────────────────────────────────

ROLE_ORDER = [
    ("background", 0),
    ("midground", 30),
    ("ground", 40),
    ("prop", 60),
    ("character", 70),
    ("character-part", 80),
    ("foreground", 90),
    ("effect", 95),
]

SAM_GROUPS: dict[str, tuple[str, str, str]] = {
    "background": ("Background", "background", "background"),
    "books": ("Books", "book", "prop"),
    "foreground": ("Foreground", "foreground", "foreground"),
    "subject": ("Subject", "subject", "character"),
    "props": ("Props", "prop", "prop"),
    "effects": ("Effects", "effect", "effect"),
    "details": ("Details", "detail", "prop"),
}
SAM_GROUP_ORDER = [
    "background", "books", "foreground", "subject", "props", "effects", "details",
]


class LayerMask(tuple):
    """A ``(name, alpha)`` pair with optional grouping metadata."""

    def __new__(cls, name, alpha, group: str = "", role: str | None = None,
                group_slug: str = "", source: str = "", label: str = "",
                description: str = ""):
        instance = super().__new__(cls, (name, alpha))
        instance.group = group
        instance.role = role
        instance.group_slug = group_slug
        instance.source = source          # "scene" when the name came from the vision LLM
        instance.label = label
        instance.description = description
        return instance

    @property
    def name(self) -> str:
        return self[0]

    @property
    def alpha(self) -> Image.Image:
        return self[1]

    def with_alpha(self, alpha: Image.Image) -> "LayerMask":
        """Same metadata, new alpha (used after feather / dilate)."""
        return LayerMask(
            self[0], alpha, group=self.group, role=self.role,
            group_slug=self.group_slug, source=self.source,
            label=self.label, description=self.description,
        )


PART_MATCHERS: list[tuple[str, list[str]]] = [
    ("pupils", ["pupil", "pupils", "iris", "irises"]),
    ("highlights", ["highlight", "highlights", "catchlight", "specular"]),
    ("eyelids", ["eyelid", "eyelids", "lid", "lids"]),
    ("eyes", ["eye", "eyes", "eyeball", "eyeballs"]),
    ("brows", ["brow", "brows"]),
    ("teeth", ["teeth", "tooth"]),
    ("tongue", ["tongue"]),
    ("jaw", ["jaw", "chin"]),
    ("mouth", ["mouth", "lip", "lips"]),
    ("mustache", ["mustache", "moustache"]),
    ("face", ["face", "skin"]),
    ("ears", ["ear", "ears"]),
    ("neck", ["neck", "throat"]),
    ("hands", ["hand", "hands", "fist", "palm", "arm", "arms"]),
    ("beard", ["beard", "whisker", "whiskers"]),
    ("head", ["hair", "head", "skull", "wig"]),
    ("body", ["shirt", "body", "torso", "dress", "jacket", "coat", "robe", "armor", "suit"]),
]


def _contains_word(text: str, *words: str) -> bool:
    for word in words:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text):
            return True
    return False


def slugify(value: str, limit: int = 60) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value)).strip("-")
    return (value or "layer")[:limit]


def classify_role(name: str) -> str:
    n = name.lower()
    if _contains_word(n, "foreground"):
        return "foreground"
    if _contains_word(n, "midground"):
        return "midground"
    if _contains_word(
        n,
        "background", "backgrounds", "backdrop", "backdrops",
        "sky", "skies", "cloud", "clouds",
        "distant", "horizon",
        "hill", "hills", "mountain", "mountains",
    ):
        return "background"
    if _contains_word(
        n,
        "ground", "floor", "shore", "sand", "earth",
        "lake", "water", "river", "sea",
        "grass", "path", "road", "street", "pavement",
    ):
        return "ground"
    if _contains_word(n, "bubble", "bubbles", "speech", "caption", "captions", "label", "labels", "text"):
        return "effect"
    if _contains_word(
        n,
        "character", "characters", "figure", "figures",
        "robot", "robots", "swan", "swans", "bird", "birds",
        "mermaid", "mermaids", "fairy", "fairies",
        "dwarf", "dwarves", "boy", "boys", "girl", "girls",
        "man", "men", "woman", "women",
    ):
        return "character"
    if _contains_word(
        n,
        "eye", "eyes", "hair", "beard", "dress", "hat",
        "jacket", "glasses", "sunglasses", "mustache", "mouth", "brow",
    ):
        return "character-part"
    return "prop"


def classify_part(name: str) -> tuple[str, str, str] | None:
    n = name.lower()
    for part, words in PART_MATCHERS:
        if _contains_word(n, *words):
            side = ""
            match = re.search(r"(?<![a-z])(left|right|l|r)(?![a-z])", n)
            if match:
                side = "right" if match.group(1).startswith("r") else "left"
            variant = ""
            match = re.search(
                r"(?<![a-z])(open|closed|half|front|back|top|neutral|raised|furrowed|sad|happy|angry|surprised)(?![a-z])",
                n,
            )
            if match:
                variant = match.group(1)
            group = f"{part}-{side}" if side else part
            if part == "head" and variant in ("front", "back"):
                return ("head", "head", variant)
            return (part, group, variant)
    return None


# ── scene description (vision LLM) ───────────────────────────────────────
#
# SAM 2 cannot read text, so the vision model does the "reading": it looks at
# the whole image once, writes a description, and returns every separable
# element as {name, group, role, bbox, point}. Those boxes/points become SAM
# prompts (--sam-scene) or are used to name automatic masks (--sam-auto).

DEFAULT_VISION_MODEL = "deepseek-flash"
DEFAULT_VISION_BASE_URL = "https://api.deepseek.com"
SCENE_ROLES = [role for role, _ in ROLE_ORDER]
ROLE_TO_GROUP = {
    "background": "Background",
    "midground": "Midground",
    "ground": "Ground",
    "prop": "Props",
    "character": "Subject",
    "character-part": "Subject",
    "foreground": "Foreground",
    "effect": "Effects",
}
DETAIL_GUIDE = {
    1: "Very coarse: only the 3-6 largest regions (e.g. background, ground, main subject).",
    2: "Coarse: the major elements only; merge small props into their surroundings.",
    3: "Balanced: every distinct object and character as ONE layer each; do not split objects into parts.",
    4: "Fine: also split significant sub-parts (clothing, hair, large decorations, each book on a shelf).",
    5: "Very fine: every separable part, including facial features, small props and decorative details.",
}


def _clean_point(value) -> tuple[float, float] | None:
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (x == x and y == y):
        return None
    return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))


def _clean_box(value) -> list[float] | None:
    try:
        x1, y1, x2, y2 = (float(v) for v in value[:4])
    except (TypeError, ValueError):
        return None
    x1, x2 = sorted((min(1.0, max(0.0, x1)), min(1.0, max(0.0, x2))))
    y1, y2 = sorted((min(1.0, max(0.0, y1)), min(1.0, max(0.0, y2))))
    if x2 - x1 < 0.004 or y2 - y1 < 0.004:
        return None
    return [x1, y1, x2, y2]


def normalize_scene(raw, model: str = "", detail: int = 3) -> dict:
    """Validate / repair a scene JSON object (LLM output or hand-edited file)."""
    if not isinstance(raw, dict):
        raise ValueError("scene description must be a JSON object")
    objects: list[dict] = []
    used: dict[str, int] = {}
    for item in raw.get("objects") or []:
        if not isinstance(item, dict):
            continue
        base = slugify(item.get("name") or item.get("label") or "object", 40).lower()
        box = _clean_box(item.get("bbox"))
        point = _clean_point(item.get("point"))
        if box is None and point is None:
            continue
        if point is None and box is not None:
            point = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        negatives = []
        for candidate in item.get("negative_points") or []:
            cleaned = _clean_point(candidate)
            if cleaned:
                negatives.append(cleaned)
        role = item.get("role")
        if role not in SCENE_ROLES:
            role = classify_role(base)
        group = re.sub(r"\s+", " ", str(item.get("group") or "")).strip()[:40]
        if not group:
            group = ROLE_TO_GROUP.get(role, "Props")
        count = used.get(base, 0) + 1
        used[base] = count
        objects.append({
            "name": base if count == 1 else f"{base}-{count}",
            "label": str(item.get("label") or base.replace("-", " ")).strip()[:80],
            "description": str(item.get("description") or "").strip()[:300],
            "group": group,
            "role": role,
            "bbox": box,
            "point": list(point),
            "negative_points": [list(p) for p in negatives],
        })
    return {
        "description": str(raw.get("description") or "").strip(),
        "style": str(raw.get("style") or "").strip()[:120],
        "model": str(raw.get("model") or model),
        "detail": int(raw.get("detail") or detail),
        "objects": objects,
    }


def load_scene(path: Path) -> dict:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"scene: cannot read {path} ({exc})")
    if isinstance(raw, dict) and isinstance(raw.get("scene"), dict):
        raw = raw["scene"]
    try:
        return normalize_scene(raw)
    except ValueError as exc:
        sys.exit(f"scene: {exc}")


def build_scene_prompt(detail: int, hint: str) -> str:
    detail = min(5, max(1, int(detail)))
    lines = [
        "You are preparing an image for layer segmentation with SAM 2. First look at the "
        "WHOLE image, describe it, then list every separable element as a named layer.",
        "",
        "Return ONLY one JSON object (no markdown fences, no commentary) shaped like:",
        '{"description": "2-4 sentences: subject, setting, composition, art style",',
        ' "style": "short style tag, e.g. flat vector illustration",',
        ' "objects": [{"name": "...", "label": "...", "description": "...", "group": "...",',
        '              "role": "...", "bbox": [x1, y1, x2, y2], "point": [x, y],',
        '              "negative_points": [[x, y]]}]}',
        "",
        "Rules:",
        "- All coordinates are fractions 0..1 of the full image width/height, origin top-left.",
        "- bbox is tight around the object's visible pixels.",
        "- point must sit on solid pixels of THAT object (not on a hole, a gap, or something "
        "in front of it). negative_points are optional: spots inside the bbox that belong "
        "to other objects.",
        "- name: lowercase kebab-case, specific and unique (bookshelf-left, red-armchair, "
        "hero-hair). Never generic names such as object-1, item or shape.",
        "- label: the same thing in plain words for a human. description: one short sentence.",
        "- group: a short noun that gathers related layers (Background, Subject, Props, "
        "Foreground, Effects, Books, Furniture, ...). Reuse the same string for related layers.",
        "- role: one of " + ", ".join(SCENE_ROLES) + ".",
        "- List objects back to front.",
        "- Granularity (level %d of 5): %s" % (detail, DETAIL_GUIDE[detail]),
    ]
    hint = (hint or "").strip()
    if hint:
        lines += ["", "Extra context from the user: " + hint[:600]]
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("the vision model did not return a JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"the vision model returned invalid JSON ({exc})") from exc


def describe_scene(reference: Image.Image, model: str = DEFAULT_VISION_MODEL,
                   detail: int = 3, hint: str = "", timeout: int = 180) -> dict:
    """Send the whole image to DeepSeek's vision model and return a normalized scene dict."""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Add it to .env next to this repo, or export it "
            "before starting the server (the key stays on the server; it is never sent to the browser)."
        )
    image = reference.convert("RGB")
    image.thumbnail((1568, 1568), Image.Resampling.LANCZOS)  # coordinates are fractions, so scaling is safe
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    body = {
        "model": model,
        "max_tokens": 8192,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": build_scene_prompt(detail, hint)},
            ],
        }],
    }
    base_url = os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_VISION_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail_text = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"vision API returned {exc.code}: {detail_text}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"vision API unreachable ({exc})") from exc
    try:
        text = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("the vision model returned no message content") from exc
    if isinstance(text, list):
        text = "".join(
            part.get("text", "") for part in text
            if isinstance(part, dict) and part.get("type") == "text"
        )
    scene = normalize_scene(_extract_json_object(text), model=model, detail=detail)
    if not scene["objects"]:
        raise RuntimeError(
            "the vision model returned no usable objects; use a vision-capable model "
            f"(deepseek-flash) or a higher detail level "
            f"[model={model}]"
        )
    return scene


def scene_to_prompts(scene: dict, pad: float = 0.02, use_points: bool = True) -> list[dict]:
    """Turn scene objects into SAM prompt dicts (box + centre point)."""
    prompts: list[dict] = []
    for obj in scene["objects"]:
        box = obj.get("bbox")
        if box:
            box = (
                max(0.0, box[0] - pad), max(0.0, box[1] - pad),
                min(1.0, box[2] + pad), min(1.0, box[3] + pad),
            )
        positives = [tuple(obj["point"])] if (use_points and obj.get("point")) else []
        if not positives and not box:
            continue
        prompts.append({
            "name": obj["name"],
            "positive": positives,
            "negative": [tuple(p) for p in obj.get("negative_points", [])],
            "box": box,
            "group": obj["group"],
            "role": obj["role"],
            "label": obj.get("label", ""),
            "description": obj.get("description", ""),
            "source": "scene",
        })
    return prompts


def _bbox_iou(a, b) -> tuple[float, float]:
    """Return (iou, fraction of ``a`` that lies inside ``b``)."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / (area_a + area_b - inter), inter / area_a


def match_scene_objects(records: list[dict], scene: dict, threshold: float = 0.35) -> int:
    """Give each automatic mask the scene object it overlaps best (in place).

    score = 0.40·bbox IoU + 0.30·(object's point lies inside the mask)
          + 0.15·(mask centre lies inside object box) + 0.15·(mask box inside object box)
    """
    matched = 0
    objects = scene.get("objects", [])
    for record in records:
        alpha: Image.Image = record["alpha"]
        width, height = alpha.size
        x0, y0, x1, y1 = record["stats"]["bbox"]
        mask_box = (x0 / width, y0 / height, x1 / width, y1 / height)
        cx, cy = record["stats"]["cx"], record["stats"]["cy"]
        best_index, best_score = None, 0.0
        for index, obj in enumerate(objects):
            box = obj.get("bbox")
            iou, inside = _bbox_iou(mask_box, box) if box else (0.0, 0.0)
            px = min(width - 1, max(0, round(obj["point"][0] * (width - 1))))
            py = min(height - 1, max(0, round(obj["point"][1] * (height - 1))))
            point_hit = 1.0 if alpha.getpixel((px, py)) >= 128 else 0.0
            centre = 1.0 if box and box[0] <= cx <= box[2] and box[1] <= cy <= box[3] else 0.0
            score = 0.40 * iou + 0.30 * point_hit + 0.15 * centre + 0.15 * inside
            if score > best_score:
                best_index, best_score = index, score
        record["scene_index"] = best_index if best_score >= threshold else None
        record["scene_score"] = best_score
        if record["scene_index"] is not None:
            matched += 1
    return matched


def remainder_mask(items: list["LayerMask"], size: tuple[int, int],
                   min_coverage: float = 0.005) -> Image.Image | None:
    """Pixels no layer claimed, or None when almost everything is covered."""
    covered = Image.new("L", size, 0)
    for item in items:
        solid = item[1].point(lambda value: 255 if value >= 128 else 0)
        covered = ImageChops.lighter(covered, solid)
    rest = ImageChops.invert(covered)
    if sum(rest.histogram()[128:]) / float(size[0] * size[1]) < min_coverage:
        return None
    return rest


# ── mask loading / cutout ────────────────────────────────────────────────

def load_mask(path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA"):
            alpha = img.getchannel("A")
        elif img.mode == "L":
            alpha = img.copy()
        else:
            alpha = img.convert("L")
        if alpha.size != size:
            alpha = alpha.resize(size, Image.Resampling.LANCZOS)
        return alpha.copy()


def cutout(reference: Image.Image, alpha: Image.Image) -> Image.Image:
    if alpha.size != reference.size:
        alpha = alpha.resize(reference.size, Image.Resampling.LANCZOS)
    rgba = reference.convert("RGBA").copy()
    rgba.putalpha(alpha.convert("L"))
    return rgba


def tight_crop(img: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    bbox = img.getbbox()
    if bbox is None:
        return img, (0, 0, img.width, img.height)
    return img.crop(bbox), bbox


def refine_alpha(alpha: Image.Image, feather: float, dilate: int,
                 decontaminate: bool = False, reference: Image.Image | None = None) -> Image.Image:
    """Feather / dilate alpha; optionally color-decontaminate the edge zone."""
    mask = alpha.convert("L")
    if dilate > 0:
        radius = dilate * 2 + 1
        mask = mask.filter(ImageFilter.MaxFilter(radius))
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(feather))
    if decontaminate and reference is not None and feather > 0:
        mask = _decontaminate_alpha(mask, reference)
    return mask


def _decontaminate_alpha(alpha: Image.Image, reference: Image.Image) -> Image.Image:
    """Blend the edge zone toward a blurred interior-only version of the
    reference, so feathering doesn't leak background RGB into the cutout.

    Returns the alpha unchanged (the decontamination happens in ``cutout``);
    this function exists purely to be called by ``cutout_decontaminated``.
    """
    return alpha


def cutout_decontaminated(reference: Image.Image, alpha: Image.Image,
                          blur: float = 3.0) -> Image.Image:
    """Cutout where edge pixels take the interior's RGB, avoiding halos."""
    if alpha.size != reference.size:
        alpha = alpha.resize(reference.size, Image.Resampling.LANCZOS)
    interior = alpha.point(lambda v: 255 if v >= 250 else 0)
    interior_rgb = Image.new("RGB", reference.size)
    interior_rgb.paste(reference.convert("RGB"), (0, 0), interior)
    filled = interior_rgb.filter(ImageFilter.GaussianBlur(blur))
    edge_mask = alpha.point(lambda v: 255 if 0 < v < 250 else 0)
    result = Image.composite(filled, reference.convert("RGB"), edge_mask)
    out = result.convert("RGBA")
    out.putalpha(alpha)
    return out


# ── knockout (background = full image minus a cut-out's silhouette) ─────

def knockout_content_mask(cut: Image.Image, white_threshold: int = 244,
                          alpha_threshold: int = 16) -> tuple[Image.Image, str]:
    """Return (mask, method): 255 where the cut-out has content.

    * Cut-out has real transparency  → its alpha channel *is* the silhouette.
    * Cut-out is flattened on white  → near-white pixels connected to the canvas
      border are 'empty'; everything else is content. Flood-filling from the
      border (instead of thresholding) keeps legitimately white areas that are
      enclosed by content, e.g. paper inside a frame.
    """
    rgba = cut.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] < 255:
        soft = alpha.point(lambda v: 0 if v < alpha_threshold else v)
        return soft, "alpha"

    near_white = _near_white_mask(rgba.convert("RGB"), white_threshold)
    w, h = near_white.size
    padded = Image.new("L", (w + 2, h + 2), 255)   # white frame links all border whites
    padded.paste(near_white, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128, thresh=0)
    outside = padded.crop((1, 1, w + 1, h + 1)).point(lambda v: 255 if v == 128 else 0)
    return ImageChops.invert(outside), "white-flood"


def parse_knockout_exclude(value: str | None) -> list[tuple[float, float]]:
    """Parse "x,y;x,y" normalized points that mark objects to keep."""
    seeds: list[tuple[float, float]] = []
    for chunk in (value or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            x_text, y_text = chunk.split(",", 1)
            x, y = float(x_text), float(y_text)
        except ValueError:
            continue
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            seeds.append((x, y))
    return seeds


def exclude_knockout_regions(hole: Image.Image, seeds: list[tuple[float, float]]
                             ) -> tuple[Image.Image, int]:
    """Remove the connected hole regions containing the normalized seeds.

    Each seed is an object clicked on the preview; its whole silhouette stays
    in the background instead of being knocked out.
    """
    if not seeds:
        return hole, 0
    width, height = hole.size
    binary = hole.point(lambda value: 255 if value >= 128 else 0)
    removed = 0
    for x, y in seeds:
        px = min(width - 1, max(0, round(x * (width - 1))))
        py = min(height - 1, max(0, round(y * (height - 1))))
        if binary.getpixel((px, py)) != 255:
            continue
        ImageDraw.floodfill(binary, (px, py), 128, thresh=0)
        removed += 1
    if not removed:
        return hole, 0
    keep_mask = binary.point(lambda value: 0 if value == 128 else 255)
    return ImageChops.multiply(hole, keep_mask), removed


def build_knockout_items(reference: Image.Image, cut: Image.Image, underlap: int = 1,
                         white_threshold: int = 244, keep_cutout: bool = False,
                         exclude: list[tuple[float, float]] | None = None
                         ) -> tuple[list[LayerMask], dict]:
    """Background layer = reference minus the cut-out silhouette."""
    content, method = knockout_content_mask(cut, white_threshold)
    content, excluded = exclude_knockout_regions(content, exclude or [])
    covered = sum(content.histogram()[128:]) / float(content.width * content.height)
    if covered < 0.0005:
        sys.exit("knockout: nothing left to remove (the cut-out looks empty or every object was kept)")
    if covered > 0.98:
        sys.exit("knockout: the cut-out covers the whole canvas; nothing to remove")

    hole = content
    if underlap > 0:   # smaller hole → background tucks under the cut-out's edge
        hole = content.filter(ImageFilter.MinFilter(underlap * 2 + 1))
    background_alpha = ImageChops.invert(hole)

    items = [LayerMask("Background", background_alpha, role="background")]
    if keep_cutout:
        items.append(LayerMask("Cutout", content, role="prop"))
    return items, {
        "method": method,
        "coverage": covered,
        "underlap": underlap,
        "excluded": excluded,
    }


# ── recovery ─────────────────────────────────────────────────────────────

def load_rgba(path: Path) -> Image.Image:
    with Image.open(path) as img:
        return img.convert("RGBA")


def _flatten_on_white(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, rgba).convert("RGB")


def _near_white_mask(rgb: Image.Image, min_channel: int = 244, max_saturation: int = 8) -> Image.Image:
    red, green, blue = rgb.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    brightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    saturation = ImageChops.subtract(brightest, darkest)
    return ImageChops.multiply(
        darkest.point(lambda value: 255 if value >= min_channel else 0),
        saturation.point(lambda value: 255 if value <= max_saturation else 0),
    )


def _max_channel_difference(left: Image.Image, right: Image.Image) -> Image.Image:
    diff = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    red, green, blue = diff.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def reference_content_mask(reference: Image.Image) -> Image.Image:
    rgba = reference.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] < 255:
        return alpha.point(lambda value: 255 if value >= 16 else 0)
    return Image.new("L", rgba.size, 255)


def detect_missing_holes(
    reference: Image.Image,
    export: Image.Image,
    hole_source: str = "auto",
    alpha_threshold: int = 32,
    white_threshold: int = 244,
    diff_threshold: int = 16,
) -> tuple[Image.Image, str, list[str]]:
    notes: list[str] = []
    if export.size != reference.size:
        if abs(
            (export.width / export.height) - (reference.width / reference.height)
        ) > 0.02:
            notes.append("export aspect ratio differs; check the alignment")
        notes.append(
            f"export resized from {export.width}x{export.height} to "
            f"{reference.width}x{reference.height}"
        )
        export = export.resize(reference.size, Image.Resampling.LANCZOS)

    exp = export.convert("RGBA")
    alpha = exp.getchannel("A")
    content = reference_content_mask(reference)
    has_transparency = alpha.getextrema()[0] < alpha_threshold

    if hole_source == "alpha" or (hole_source == "auto" and has_transparency):
        holes = ImageChops.multiply(
            alpha.point(lambda value: 255 if value < alpha_threshold else 0),
            content,
        )
        if holes.getbbox() is not None or hole_source == "alpha":
            return holes, "alpha", notes
        notes.append("no transparent holes found; falling back to near-white detection")

    flat = _flatten_on_white(exp)
    near_white = _near_white_mask(flat, white_threshold)
    changed = _max_channel_difference(reference, flat).point(
        lambda value: 255 if value >= diff_threshold else 0
    )
    holes = ImageChops.multiply(ImageChops.multiply(near_white, changed), content)
    return holes, "white", notes


def find_hole_regions(
    holes: Image.Image,
    min_area_pct: float = 0.1,
    ignore_border: bool = True,
) -> tuple[list[dict], dict]:
    width, height = holes.size
    data = holes.tobytes()
    seen = bytearray(width * height)
    min_area = max(1, int(round(width * height * min_area_pct / 100.0)))
    regions: list[dict] = []
    stats = {"small": 0, "border": 0, "min_area": min_area}

    for y in range(height):
        row_offset = y * width
        for x in range(width):
            start = row_offset + x
            if seen[start] or not data[start]:
                continue
            stack = [start]
            seen[start] = 1
            pixels: list[int] = []
            area = 0
            x0 = x1 = x
            y0 = y1 = y
            touches_border = False

            while stack:
                index = stack.pop()
                pixels.append(index)
                area += 1
                cy, cx = divmod(index, width)
                if cx < x0:
                    x0 = cx
                elif cx > x1:
                    x1 = cx
                if cy < y0:
                    y0 = cy
                elif cy > y1:
                    y1 = cy
                if cx == 0 or cy == 0 or cx == width - 1 or cy == height - 1:
                    touches_border = True

                if cx > 0:
                    neighbour = index - 1
                    if data[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                if cx + 1 < width:
                    neighbour = index + 1
                    if data[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                if cy > 0:
                    neighbour = index - width
                    if data[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                if cy + 1 < height:
                    neighbour = index + width
                    if data[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)

            if ignore_border and touches_border:
                stats["border"] += 1
                continue
            if area < min_area:
                stats["small"] += 1
                continue

            local_width = x1 - x0 + 1
            local_height = y1 - y0 + 1
            local = bytearray(local_width * local_height)
            for index in pixels:
                cy, cx = divmod(index, width)
                local[(cy - y0) * local_width + (cx - x0)] = 255
            region_mask = Image.new("L", (width, height), 0)
            region_mask.paste(
                Image.frombytes("L", (local_width, local_height), bytes(local)),
                (x0, y0),
            )
            regions.append(
                {
                    "mask": region_mask,
                    "bbox": (x0, y0, x1 + 1, y1 + 1),
                    "area": area,
                }
            )

    regions.sort(key=lambda region: (region["bbox"][1], region["bbox"][0]))
    return regions, stats


def solidify_base_alpha(
    export: Image.Image,
    alpha_threshold: int = 32,
) -> tuple[Image.Image, int | None]:
    rgba = export.convert("RGBA")
    alpha = rgba.getchannel("A")
    histogram = alpha.histogram()
    content_count = sum(histogram[alpha_threshold:])
    if content_count <= 0:
        return rgba, None

    mode_value = max(
        range(alpha_threshold, 256),
        key=lambda value: histogram[value],
    )
    if mode_value >= 250 or histogram[mode_value] / content_count < 0.75:
        return rgba, None

    def ramp(value: int) -> int:
        if value < alpha_threshold:
            return 0
        return min(255, int(round(value * 255.0 / mode_value)))

    rgba = rgba.copy()
    rgba.putalpha(alpha.point(ramp))
    return rgba, mode_value


def write_recover_preview(
    reference: Image.Image,
    regions: list[dict],
    path: Path,
) -> Path:
    base = reference.convert("RGBA")
    combined = Image.new("L", base.size, 0)
    for region in regions:
        combined.paste(region["mask"], (0, 0), region["mask"])

    tint = Image.new("RGBA", base.size, (255, 45, 149, 255))
    tint.putalpha(combined.point(lambda value: 118 if value else 0))
    preview = Image.alpha_composite(base, tint)

    draw = ImageDraw.Draw(preview)
    scale = max(base.width, base.height)
    line_width = max(2, round(scale * 0.0025))

    for index, region in enumerate(regions, 1):
        x0, y0, x1, y1 = region["bbox"]
        box_size = max(8, min(x1 - x0, y1 - y0) - 4)
        font_size = max(8, min(round(scale * 0.016), box_size))
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            font = ImageFont.load_default()
        draw.rectangle(
            (x0, y0, x1 - 1, y1 - 1),
            outline=(255, 45, 149, 255),
            width=line_width,
        )
        label = str(index)
        draw.text(
            (x0 + line_width + 2, y0 + line_width + 1),
            label,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=max(1, line_width // 2),
            stroke_fill=(22, 20, 28, 235),
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    preview.convert("RGB").save(path, optimize=True)
    return path


# ── SAM ──────────────────────────────────────────────────────────────────

def pick_sam2_model_id(model: str) -> str:
    if "/" in model:
        return model
    return {
        "sam2-large": "facebook/sam2.1-hiera-large",
        "sam2-base": "facebook/sam2.1-hiera-base-plus",
        "sam2-small": "facebook/sam2.1-hiera-small",
        "sam2-tiny": "facebook/sam2.1-hiera-tiny",
    }.get(model, "facebook/sam2.1-hiera-large")


def resolve_sam_device(device: str, torch) -> str:
    if device != "auto":
        return device
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def parse_sam_points(spec: str) -> list[dict]:
    """Parse ``name:x,y|!x,y|box:x1,y1,x2,y2;name2:x,y;...``."""
    items: list[dict] = []
    for chunk in spec.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, rest = chunk.split(":", 1)
        name = name.strip() or "layer"
        positives: list[tuple[float, float]] = []
        negatives: list[tuple[float, float]] = []
        box: Optional[tuple[float, float, float, float]] = None
        for token in rest.split("|"):
            token = token.strip()
            if not token:
                continue
            if token.startswith("box:"):
                parts = token[4:].split(",")
                if len(parts) == 4:
                    try:
                        box = tuple(float(v) for v in parts)  # type: ignore
                    except ValueError:
                        pass
                continue
            negative = token.startswith("!")
            if negative:
                token = token[1:]
            if "," not in token:
                continue
            x_str, y_str = token.split(",", 1)
            try:
                x = float(x_str)
                y = float(y_str)
            except ValueError:
                continue
            if negative:
                negatives.append((x, y))
            else:
                positives.append((x, y))
        if not positives and not negatives and box is None:
            continue
        items.append({"name": name, "positive": positives, "negative": negatives, "box": box})
    return items


def create_prompt_mask(
    width: int,
    height: int,
    positives: list[tuple[float, float]],
    negatives: list[tuple[float, float]],
    radius_fraction: float = 0.03,
    box: Optional[tuple[float, float, float, float]] = None,
) -> Image.Image:
    """Approximate mask from points; used only as a SAM-unavailable fallback."""
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    radius = max(6, int(min(width, height) * radius_fraction))
    if box and not positives:   # scene prompt without a point: fill the box ellipse
        draw.ellipse((box[0] * width, box[1] * height, box[2] * width, box[3] * height), fill=255)
    for x, y in positives:
        cx, cy = x * width, y * height
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=255)
    for x, y in negatives:
        cx, cy = x * width, y * height
        draw.ellipse(
            (cx - radius * 2, cy - radius * 2, cx + radius * 2, cy + radius * 2),
            fill=0,
        )
    mask = mask.filter(ImageFilter.GaussianBlur(2))
    mask = mask.point(lambda p: 255 if p > 140 else 0)
    return mask


def build_sam2_predictor(model: str, device: str):
    try:
        import numpy as np  # noqa: F401
        import torch
        from sam2.sam2_image_predictor import SAM2ImagePredictor
    except ImportError as exc:
        raise RuntimeError(
            "Optional SAM 2 setup: install the SAM 2 stack with 'pip install -r requirements-sam.txt' "
            f"(import error: {exc})"
        ) from exc
    resolved = resolve_sam_device(device, torch)
    model_id = pick_sam2_model_id(model)
    try:
        predictor = SAM2ImagePredictor.from_pretrained(model_id, device=resolved)
    except TypeError:
        predictor = SAM2ImagePredictor.from_pretrained(model_id)
        if resolved not in ("cpu", None):
            try:
                predictor.model.to(resolved)
            except Exception:
                pass
    return predictor


def generate_sam_from_points(
    reference: Image.Image,
    prompts: list[dict],
    model: str,
    device: str,
    radius_fraction: float = 0.03,
) -> list[LayerMask]:
    """Run SAM 2 with positive/negative points and optional boxes."""
    import numpy as np

    predictor = build_sam2_predictor(model, device)
    rgb = np.asarray(reference.convert("RGB"))
    predictor.set_image(rgb)
    width, height = reference.size
    min_pixels = max(4, int(width * height * 0.0002))
    results: list[LayerMask] = []

    def make_layer(prompt: dict, alpha: Image.Image) -> LayerMask:
        # Scene prompts carry their own group / role / label; manual prompts use the name.
        return LayerMask(
            prompt["name"], alpha,
            group=prompt.get("group", prompt["name"]),
            role=prompt.get("role"),
            group_slug=slugify(prompt["group"], 20).lower() if prompt.get("group") else "",
            source=prompt.get("source", ""),
            label=prompt.get("label", ""),
            description=prompt.get("description", ""),
        )

    for prompt in prompts:
        point_coords = []
        point_labels = []
        for x, y in prompt["positive"]:
            point_coords.append([x * width, y * height])
            point_labels.append(1)
        for x, y in prompt["negative"]:
            point_coords.append([x * width, y * height])
            point_labels.append(0)

        kwargs: dict = {}
        if point_coords:
            kwargs["point_coords"] = np.array(point_coords, dtype=np.float32)
            kwargs["point_labels"] = np.array(point_labels, dtype=np.int32)
        if prompt.get("box"):
            x1, y1, x2, y2 = prompt["box"]
            kwargs["box"] = np.array(
                [x1 * width, y1 * height, x2 * width, y2 * height],
                dtype=np.float32,
            )

        try:
            masks, scores, _ = predictor.predict(multimask_output=False, **kwargs)
        except Exception as exc:
            print(f"  warn: SAM failed for {prompt['name']!r} ({exc}); falling back to circles")
            alpha = create_prompt_mask(
                width, height, prompt["positive"], prompt["negative"], radius_fraction,
                box=prompt.get("box"),
            )
            results.append(make_layer(prompt, alpha))
            continue

        best = int(np.argmax(scores))
        bool_mask = (np.asarray(masks)[best] > 0.5)
        pixels = int(bool_mask.sum())
        if pixels < min_pixels:
            print(f"  warn: {prompt['name']!r} produced a tiny mask ({pixels} px)")
        print(f"  {prompt['name']:<24} score={float(scores[best]):.3f}  pixels={pixels}")
        alpha = Image.fromarray((bool_mask.astype(np.uint8) * 255))
        results.append(make_layer(prompt, alpha))

    return results


def _numpy_mask_ops_available(np_module) -> bool:
    """Return True when the imported NumPy is sufficiently complete for mask math."""
    return (
        np_module is not None
        and hasattr(np_module, "asarray")
        and hasattr(np_module, "logical_and")
        and hasattr(np_module, "uint8")
    )


def nms_masks(records: list[dict],
              iou_threshold: float = 0.55,
              containment_threshold: float = 0.85) -> list[dict]:
    """Drop masks that overlap an already-kept mask too much."""
    import numpy as np
    if not _numpy_mask_ops_available(np):
        return records
    records = sorted(records, key=lambda r: r["area"], reverse=True)
    kept: list[dict] = []
    for rec in records:
        a = rec["seg"]
        area_a = rec["area"]
        if area_a < 1:
            continue
        drop = False
        for k in kept:
            ax1, ay1, ax2, ay2 = rec["bbox"]
            bx1, by1, bx2, by2 = k["bbox"]
            if ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1:
                continue
            inter = int(np.logical_and(a, k["seg"]).sum())
            if inter == 0:
                continue
            union = area_a + k["area"] - inter
            if inter / union > iou_threshold:
                drop = True
                break
            if inter / area_a > containment_threshold:
                drop = True
                break
        if not drop:
            kept.append(rec)
    return kept


def build_sam2_auto_generator(model: str, device: str, points_per_side: int = 16,
                              pred_iou_thresh: float = 0.8,
                              stability_score_thresh: float = 0.85):
    try:
        import numpy as np
        import torch
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
        from sam2.build_sam import build_sam2_hf
    except ImportError as exc:
        raise RuntimeError(
            "Optional SAM 2 setup: install the SAM 2 stack with 'pip install -r requirements-sam.txt' "
            f"(import error: {exc})"
        ) from exc

    resolved_device = resolve_sam_device(device, torch)
    model_id = pick_sam2_model_id(model)
    try:
        sam_model = build_sam2_hf(model_id, device=resolved_device)
    except Exception as exc:
        raise RuntimeError(
            "Optional SAM 2 setup: install the SAM 2 stack with 'pip install -r requirements-sam.txt' "
            f"(model load error: {exc})"
        ) from exc
    # The generator filters on these itself. Previously only points_per_side was
    # passed, so SAM's built-in 0.95 stability cut-off silently overrode --min-stability.
    generator = SAM2AutomaticMaskGenerator(
        sam_model,
        points_per_side=points_per_side,
        pred_iou_thresh=pred_iou_thresh,
        stability_score_thresh=stability_score_thresh,
        output_mode="binary_mask",
    )
    return generator, np


def _mask_statistics(alpha: Image.Image, reference: Image.Image) -> dict | None:
    width, height = alpha.size
    bbox = alpha.getbbox()
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    box_width = max(1, x1 - x0)
    box_height = max(1, y1 - y0)
    pixels = sum(alpha.histogram()[128:])
    if pixels <= 0:
        return None
    solid = alpha.point(lambda value: 255 if value >= 128 else 0)
    stats = ImageStat.Stat(reference.convert("RGB"), solid)
    red, green, blue = (stats.mean or [0, 0, 0])[:3]
    highest = max(red, green, blue)
    lowest = min(red, green, blue)
    return {
        "bbox": bbox,
        "pixels": pixels,
        "coverage": pixels / float(width * height),
        "width_fraction": box_width / float(width),
        "height_fraction": box_height / float(height),
        "aspect": box_width / float(box_height),
        "fill": pixels / float(box_width * box_height),
        "cx": (x0 + x1) / (2.0 * width),
        "cy": (y0 + y1) / (2.0 * height),
        "touches_top": y0 <= 1,
        "touches_bottom": y1 >= height - 1,
        "touches_left": x0 <= 1,
        "touches_right": x1 >= width - 1,
        "brightness": (red + green + blue) / (3.0 * 255.0),
        "saturation": (highest - lowest) / highest if highest > 0 else 0.0,
    }


def _looks_like_shelf_book(stats: dict) -> bool:
    return (
        stats["aspect"] <= 0.55
        and stats["coverage"] <= 0.08
        and stats["fill"] >= 0.45
        and 0.03 <= stats["height_fraction"] <= 0.6
        and stats["cy"] <= 0.72
    )


def _classify_sam_mask(stats: dict, *, books: bool) -> str:
    if books:
        return "books"
    if (
        stats["brightness"] >= 0.78
        and stats["saturation"] >= 0.4
        and stats["coverage"] <= 0.06
    ):
        return "effects"
    if stats["coverage"] >= 0.35:
        return "background"
    if (
        stats["touches_bottom"]
        and not stats["touches_top"]
        and stats["cy"] >= 0.60
        and stats["coverage"] <= 0.20
        and (stats["width_fraction"] >= 0.15 or stats["coverage"] >= 0.015)
    ):
        return "foreground"
    if (
        stats["touches_top"]
        and (stats["width_fraction"] >= 0.15 or stats["coverage"] >= 0.015)
    ):
        return "background"
    if (
        (stats["touches_left"] or stats["touches_right"])
        and (
            stats["width_fraction"] >= 0.10
            or stats["height_fraction"] >= 0.15
            or stats["coverage"] >= 0.02
        )
    ):
        return "background"
    if (
        stats["touches_bottom"]
        and stats["cy"] >= 0.65
        and (stats["width_fraction"] >= 0.15 or stats["coverage"] >= 0.015)
    ):
        return "foreground"
    if stats["coverage"] >= 0.02 and 0.18 <= stats["cx"] <= 0.82:
        return "subject"
    if stats["coverage"] >= 0.0015:
        return "props"
    return "details"


def _sam_sort_key(record: dict) -> tuple:
    order = SAM_GROUP_ORDER.index(record["kind"])
    stats = record["stats"]
    if record["kind"] == "books":
        return (order, stats["bbox"][0], stats["bbox"][1])
    if record["kind"] == "background":
        return (order, stats["bbox"][1], stats["bbox"][0])
    return (order, -stats["coverage"], stats["bbox"][1], stats["bbox"][0])


def generate_sam_auto(
    reference: Image.Image,
    model: str,
    device: str,
    min_area_pct: float = 0.1,
    min_stability: float = 0.85,
    points_per_side: int = 16,
    pred_iou: float = 0.8,
    nms_iou: float = 0.55,
    containment: float = 0.85,
    max_layers: int = 0,
    scene: dict | None = None,
    scene_match: float = 0.35,
) -> list[LayerMask]:
    generator, np = build_sam2_auto_generator(
        model, device, points_per_side, pred_iou, min_stability,
    )
    rgb = np.asarray(reference.convert("RGB")).copy()
    raw_masks = generator.generate(rgb)
    min_area = max(1, int(reference.width * reference.height * min_area_pct / 100))
    rgb_reference = reference.convert("RGB")

    raw_records: list[dict] = []
    for item in raw_masks:
        if item.get("area", 0) < min_area:
            continue
        if item.get("stability_score", 1.0) < min_stability:
            continue
        segmentation = item["segmentation"]
        if isinstance(segmentation, Image.Image):
            alpha = segmentation.convert("L")
            if _numpy_mask_ops_available(np):
                seg = np.asarray(alpha) > 128
            else:
                seg = alpha
        else:
            if _numpy_mask_ops_available(np):
                seg = np.asarray(segmentation, dtype=bool)
                alpha = Image.fromarray((seg.astype(np.uint8)) * 255)
            else:
                alpha = Image.fromarray((segmentation > 0.5).astype("uint8") * 255) if hasattr(segmentation, "astype") else Image.fromarray(segmentation)
                seg = alpha
        if alpha.size != reference.size:
            alpha = alpha.resize(reference.size, Image.Resampling.NEAREST)
            if _numpy_mask_ops_available(np):
                seg = np.asarray(alpha) > 128
            else:
                seg = alpha
        bbox = alpha.getbbox()
        if bbox is None:
            continue
        if _numpy_mask_ops_available(np):
            area = int(seg.sum())
        else:
            area = sum(1 for value in alpha.getdata() if value > 128)
        raw_records.append({
            "alpha": alpha,
            "seg": seg,
            "bbox": bbox,
            "area": area,
        })

    deduped = nms_masks(
        raw_records, iou_threshold=nms_iou, containment_threshold=containment,
    )
    if max_layers and len(deduped) > max_layers:
        deduped = sorted(deduped, key=lambda r: r["area"], reverse=True)[:max_layers]

    records: list[dict] = []
    for rec in deduped:
        stats = _mask_statistics(rec["alpha"], rgb_reference)
        if stats is None:
            continue
        records.append({**rec, "stats": stats})

    # 1) Names from the vision LLM's scene description, when one was supplied.
    scene_objects = (scene or {}).get("objects") or []
    scene_matched = 0
    if scene_objects:
        scene_matched = match_scene_objects(records, scene, scene_match)
    for record in records:
        record.setdefault("scene_index", None)

    # 2) Heuristic classes for everything the scene did not claim.
    unmatched = [r for r in records if r["scene_index"] is None]
    book_like = {id(record) for record in unmatched
                 if _looks_like_shelf_book(record["stats"])}
    use_books = len(book_like) >= 3
    for record in unmatched:
        record["kind"] = _classify_sam_mask(
            record["stats"], books=use_books and id(record) in book_like
        )

    unmatched.sort(key=_sam_sort_key)
    matched = sorted(
        (r for r in records if r["scene_index"] is not None),
        key=lambda r: (r["scene_index"], -r["stats"]["coverage"],
                       r["stats"]["bbox"][1], r["stats"]["bbox"][0]),
    )

    results: list[LayerMask] = []
    per_object: dict[int, int] = {}
    for record in matched:
        per_object[record["scene_index"]] = per_object.get(record["scene_index"], 0) + 1
    seen_object: dict[int, int] = {}
    for record in matched:
        obj = scene_objects[record["scene_index"]]
        seen_object[record["scene_index"]] = seen_object.get(record["scene_index"], 0) + 1
        name = obj["name"]
        if per_object[record["scene_index"]] > 1:
            name = f"{name}-{seen_object[record['scene_index']]:02d}"
        results.append(LayerMask(
            name, record["alpha"],
            group=obj["group"], role=obj["role"],
            group_slug=slugify(obj["group"], 20).lower(),
            source="scene", label=obj.get("label", ""),
            description=obj.get("description", ""),
        ))

    counts: dict[str, int] = {}
    for record in unmatched:
        kind = record["kind"]
        label, prefix, role = SAM_GROUPS[kind]
        counts[kind] = counts.get(kind, 0) + 1
        name = f"{prefix}-{counts[kind]:02d}"
        results.append(LayerMask(
            name,
            record["alpha"],
            group=label,
            role=role,
            group_slug=kind,
        ))

    print(
        f"  SAM 2 automatic masks: {len(results)} kept from {len(raw_masks)} raw "
        f"(after NMS; minimum area {min_area}px, stability {min_stability:.2f})"
    )
    if scene_objects:
        print(
            f"  scene naming: {scene_matched} of {len(records)} masks matched one of "
            f"{len(scene_objects)} scene objects (threshold {scene_match:.2f})"
        )
    if counts:
        summary = ", ".join(
            f"{SAM_GROUPS[kind][0]} {counts[kind]}"
            for kind in SAM_GROUP_ORDER
            if counts.get(kind)
        )
        print(f"  named masks in {len(counts)} group(s): {summary}")
    return results


# ── Depth Anything v2 ────────────────────────────────────────────────────

_DEPTH_PIPE = None


def compute_depth(reference: Image.Image, model: str, device: str):
    """Return HxW float array in [0,1]; 1 = nearest, 0 = farthest."""
    global _DEPTH_PIPE
    import numpy as np
    if _DEPTH_PIPE is None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                "Depth ordering requires transformers + torch.\n"
                "Install with: pip install -r requirements-sam.txt"
            ) from exc
        device_index = 0 if device == "cuda" else -1
        _DEPTH_PIPE = pipeline("depth-estimation", model=model, device=device_index)

    result = _DEPTH_PIPE(reference.convert("RGB"))
    raw = result["predicted_depth"]
    if hasattr(raw, "cpu"):
        raw = raw.cpu().numpy()
    depth = np.asarray(raw, dtype=np.float32)
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


def layer_depth_score(depth_map, alpha: Image.Image) -> Optional[float]:
    import numpy as np
    if alpha.size != (depth_map.shape[1], depth_map.shape[0]):
        alpha = alpha.resize((depth_map.shape[1], depth_map.shape[0]), Image.BILINEAR)
    a = np.asarray(alpha)
    mask = a > 64
    if not mask.any():
        return None
    return float(np.median(depth_map[mask]))


# ── composition emission ─────────────────────────────────────────────────

def build_composition(
    entries: list[dict],
    name: str,
    width: int,
    height: int,
    reference_url: str,
    base: dict | None = None,
    meta: dict | None = None,
    sort_by_depth: bool = False,
) -> dict:
    reference_id = "reference"
    layers = [{
        "id": reference_id,
        "name": "Final image",
        "role": "background",
        "url": reference_url,
        "frames": [reference_url],
        "spriteFps": 1,
        "spriteMode": "once",
        "x": 0, "y": 0, "w": 100, "h": 100,
        "opacity": 1, "z": 0, "rotation": 0,
        "fit": "contain",
        "visible": False, "locked": True, "positionConfirmed": True,
        "keyframes": [],
    }]

    if base:
        layers.append({
            "id": base.get("id", "aligned-export"),
            "name": base.get("name", "Aligned export"),
            "role": "background",
            "url": base["url"],
            "frames": [base["url"]],
            "spriteFps": 1,
            "spriteMode": "once",
            "x": 0, "y": 0, "w": 100, "h": 100,
            "opacity": 1, "z": 1, "rotation": 0,
            "fit": "contain",
            "visible": True, "locked": True, "positionConfirmed": True,
            "keyframes": [],
        })

    order = {role: idx for idx, (role, _) in enumerate(ROLE_ORDER)}
    if sort_by_depth:
        # FIX: depth first, then role, then name.
        entries_sorted = sorted(
            entries,
            key=lambda e: (
                e.get("depth_score", 0.5),
                order.get(e["role"], 60),
                e["name"],
            ),
        )
    else:
        entries_sorted = sorted(
            entries, key=lambda e: (order.get(e["role"], 60), e["name"])
        )

    z = 2
    for entry in entries_sorted:
        x0, y0, x1, y1 = entry["crop"]
        x = x0 / width * 100
        y = y0 / height * 100
        w = (x1 - x0) / width * 100
        h = (y1 - y0) / height * 100
        layer = {
            "id": entry["id"],
            "name": entry["name"],
            "role": entry["role"],
            "url": entry["url"],
            "frames": [entry["url"]],
            "spriteFps": 1,
            "spriteMode": "once",
            "x": round(x, 4), "y": round(y, 4),
            "w": round(w, 4), "h": round(h, 4),
            "opacity": 1, "z": z, "rotation": 0,
            "fit": "contain",
            "visible": True, "locked": False, "positionConfirmed": True,
            "keyframes": [],
        }
        if entry.get("segment_group"):
            layer["segmentGroup"] = entry["segment_group"]
        if entry.get("label"):
            layer["label"] = entry["label"]
        if entry.get("description"):
            layer["description"] = entry["description"]
        # Part info is decided once in main(); scene-named layers keep the role the
        # vision model gave them instead of being re-guessed from the file name.
        if entry.get("part"):
            layer["role"] = "character-part"
            layer["part"] = entry["part"]
            layer["group"] = entry["group"]
            layer["variant"] = entry.get("variant", "")
        layers.append(layer)
        z += 1

    composition_meta = {
        "source": "image-to-layers",
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "note": "Generated from reference image + mask prompts or uploaded masks.",
    }
    if meta:
        composition_meta.update(meta)

    return {
        "version": 1,
        "id": slugify(name, 60) or "image-to-layers",
        "name": name,
        "width": width,
        "height": height,
        "duration": 3000,
        "referenceLayerId": reference_id,
        "layers": layers,
        "meta": composition_meta,
    }


def emit_zip(reference: Image.Image, items: list[LayerMask], slug: str) -> Path:
    target = Path(f"{slug}.zip")
    used: dict[str, int] = {}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            name, alpha = item
            group_slug = getattr(item, "group_slug", "") or ""
            if not group_slug:
                group = getattr(item, "group", "") or ""
                group_slug = slugify(group, 20) if group else ""
            prefix = f"{group_slug}_" if group_slug else ""
            cut = cutout(reference, alpha)
            cropped, _ = tight_crop(cut)
            if cropped.width == 0 or cropped.height == 0:
                continue
            base_stem = prefix + slugify(name, 40)
            count = used.get(base_stem, 0) + 1
            used[base_stem] = count
            stem = base_stem if count == 1 else f"{base_stem}-{count}"
            buf = io.BytesIO()
            cropped.save(buf, format="PNG", optimize=True)
            zf.writestr(f"{stem}.png", buf.getvalue())
    print(f"wrote {target}")
    print("drop it into Studio → Import ZIP + image")
    return target


def _unique_stem(base: str, used: dict[str, int]) -> str:
    count = used.get(base, 0) + 1
    used[base] = count
    return base if count == 1 else f"{base}-{count}"


# ── main ─────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=False)
    parser.add_argument("--masks", type=Path)
    parser.add_argument("--sam-points", type=str,
                        help='Format: "name:x,y|!x,y|box:x1,y1,x2,y2;..." (0–1 fractions)')
    parser.add_argument("--sam-auto", action="store_true")
    parser.add_argument("--sam-points-fallback", action="store_true",
                        help="Skip SAM; always use circular approximation (fast, rough)")
    parser.add_argument("--export", dest="export_path", type=Path,
                        help="Aligned studio export with missing (transparent/near-white) areas")
    parser.add_argument("--knockout", type=Path,
                        help="Cut-out of part of --reference; emits reference-minus-cutout background")
    parser.add_argument("--knockout-underlap", type=int, default=1,
                        help="Shrink the hole by N px so the cut-out overlaps the edge (default 1)")
    parser.add_argument("--knockout-white", type=int, default=244,
                        help="Min channel value treated as white when the cut-out has no alpha")
    parser.add_argument("--knockout-keep-cutout", action="store_true",
                        help="Also emit the cut-out layer, re-cut from the reference")
    parser.add_argument("--knockout-exclude", type=str, default="",
                        help="Objects to keep in the background, as normalized 'x,y;x,y' points")
    parser.add_argument("--sam-model", default="sam2-large")

    # scene description (vision LLM)
    parser.add_argument("--describe-only", action="store_true",
                        help="Ask a vision LLM for a scene description + named objects, write --scene-out, exit")
    parser.add_argument("--scene-out", type=Path, help="Where --describe-only writes the scene JSON")
    parser.add_argument("--scene-json", type=Path,
                        help="Scene description to use (from --describe-only or hand-edited)")
    parser.add_argument("--sam-scene", action="store_true",
                        help="Layer source: prompt SAM 2 with every object of --scene-json")
    parser.add_argument("--vision-model", default=os.environ.get("ITL_VISION_MODEL", DEFAULT_VISION_MODEL))
    parser.add_argument("--scene-detail", type=int, default=3, choices=[1, 2, 3, 4, 5])
    parser.add_argument("--scene-hint", default="")
    parser.add_argument("--scene-box-pad", type=float, default=0.02)
    parser.add_argument("--scene-no-points", action="store_true")
    parser.add_argument("--scene-remainder", action="store_true")
    parser.add_argument("--scene-match", type=float, default=0.35)

    # automatic SAM granularity
    parser.add_argument("--auto-points-per-side", type=int, default=16)
    parser.add_argument("--auto-min-area", type=float, default=0.1)
    parser.add_argument("--auto-min-stability", type=float, default=0.85)
    parser.add_argument("--auto-pred-iou", type=float, default=0.8)
    parser.add_argument("--auto-nms-iou", type=float, default=0.55)
    parser.add_argument("--auto-containment", type=float, default=0.85)
    parser.add_argument("--auto-max-layers", type=int, default=0)

    parser.add_argument("--folders", choices=["group", "flat"], default="group")
    parser.add_argument("--depth", action="store_true",
                        help="Refine z-order by monocular depth (or vertical heuristic)")
    parser.add_argument("--depth-role", action="store_true",
                        help="Re-derive role from depth bands")
    parser.add_argument("--depth-model", default="depth-anything/Depth-Anything-V2-Small-hf",
                        help="HuggingFace depth model id, or 'none' for vertical heuristic")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--feather", type=float, default=1.0)
    parser.add_argument("--dilate", type=int, default=0)
    parser.add_argument("--decontaminate", action="store_true",
                        help="Replace feathered edge RGB with dilated interior to kill halos")
    parser.add_argument("--strict", action="store_true",
                        help="Fail on empty or full-canvas masks")
    parser.add_argument("--point-radius", type=float, default=0.03,
                        help="Circle radius for --sam-points-fallback")
    parser.add_argument("--name", default="Image layers")
    parser.add_argument("--out", default="data/projects")
    parser.add_argument("--mode", choices=["project", "zip"], default="project")
    parser.add_argument("--recover-holes", choices=["auto", "alpha", "white"], default="auto")
    parser.add_argument("--recover-min-area", type=float, default=0.1)
    parser.add_argument("--recover-alpha", type=int, default=32)
    parser.add_argument("--recover-white", type=int, default=244)
    parser.add_argument("--recover-diff", type=int, default=16)
    parser.add_argument("--recover-keep-border", action="store_true")
    parser.add_argument("--keep-base-alpha", action="store_true")
    parser.add_argument("--list-models", action="store_true")
    args = parser.parse_args(argv)

    if args.list_models:
        print("sam2-tiny\nsam2-small\nsam2-base\nsam2-large")
        return 0

    if not args.reference:
        parser.error("--reference is required")

    reference_path = Path(args.reference)
    if not reference_path.exists():
        sys.exit(f"reference not found: {reference_path}")

    if args.describe_only:
        if not args.scene_out:
            parser.error("--describe-only needs --scene-out")
        described_from = Image.open(reference_path).convert("RGBA")
        print(f"  Describing the whole image with {args.vision_model} (detail {args.scene_detail})…", flush=True)
        try:
            described = describe_scene(
                described_from, args.vision_model, args.scene_detail, args.scene_hint,
            )
        except RuntimeError as exc:
            sys.exit(f"describe: {exc}")
        args.scene_out.parent.mkdir(parents=True, exist_ok=True)
        args.scene_out.write_text(json.dumps(described, indent=2), encoding="utf-8")
        print(f"  scene: {len(described['objects'])} object(s)")
        print(f"scene written {args.scene_out}")
        return 0

    sources = (args.masks, args.sam_points, args.sam_auto, args.export_path,
               args.knockout, args.sam_scene)
    if sum(1 for value in sources if value) != 1:
        parser.error(
            "choose exactly one of --masks / --sam-points / --sam-auto / --sam-scene / "
            "--export / --knockout"
        )

    scene: dict | None = load_scene(args.scene_json) if args.scene_json else None
    if args.sam_scene and not (scene and scene["objects"]):
        parser.error("--sam-scene needs --scene-json with at least one object")

    reference = Image.open(reference_path).convert("RGBA")
    width, height = reference.size
    items: list[LayerMask] = []

    output_name = args.name
    base_image: Image.Image | None = None
    base_meta: dict | None = None
    recover_meta: dict | None = None
    recover_regions: list[dict] = []
    recovery_method: str | None = None
    recovering = bool(args.export_path)

    # Validate point-prompt availability.
    use_sam_points = bool(args.sam_points) and not args.sam_points_fallback

    if args.sam_points:
        prompts = parse_sam_points(args.sam_points)
        if not prompts:
            sys.exit("no valid prompts in --sam-points")
        if use_sam_points:
            try:
                items = generate_sam_from_points(
                    reference, prompts, args.sam_model, args.device, args.point_radius,
                )
            except RuntimeError as exc:
                print(f"  warn: SAM point prompts unavailable; using circle fallback ({exc})")
                for prompt in prompts:
                    mask = create_prompt_mask(
                        width, height,
                        prompt["positive"], prompt["negative"],
                        args.point_radius,
                    )
                    items.append(LayerMask(prompt["name"], mask, group=prompt["name"]))
        else:
            for prompt in prompts:
                mask = create_prompt_mask(
                    width, height,
                    prompt["positive"], prompt["negative"],
                    args.point_radius,
                )
                items.append(LayerMask(prompt["name"], mask, group=prompt["name"]))
    elif args.sam_scene:
        prompts = scene_to_prompts(scene, args.scene_box_pad, not args.scene_no_points)
        if not prompts:
            sys.exit("no usable objects in --scene-json")
        print(f"  Running SAM 2 on {len(prompts)} scene object(s) "
              f"({args.sam_model}, device={args.device})", flush=True)
        try:
            items = generate_sam_from_points(
                reference, prompts, args.sam_model, args.device, args.point_radius,
            )
        except RuntimeError as exc:
            print(f"  warn: SAM unavailable; using box/circle fallback ({exc})")
            for prompt in prompts:
                mask = create_prompt_mask(
                    width, height, prompt["positive"], prompt["negative"],
                    args.point_radius, box=prompt.get("box"),
                )
                items.append(LayerMask(
                    prompt["name"], mask, group=prompt["group"], role=prompt["role"],
                    group_slug=slugify(prompt["group"], 20).lower(), source="scene",
                    label=prompt.get("label", ""), description=prompt.get("description", ""),
                ))
        if args.scene_remainder:
            rest = remainder_mask(items, reference.size)
            if rest is not None:
                items.append(LayerMask(
                    "unassigned", rest, group="Background", role="background",
                    group_slug="background", source="scene",
                    label="Unassigned pixels",
                    description="Everything no scene object claimed, so nothing is lost.",
                ))
                print("  added 'unassigned' remainder layer")
    elif args.sam_auto:
        print(f"  Running SAM 2 automatic segmentation ({args.sam_model}, device={args.device})", flush=True)
        for mask in generate_sam_auto(
            reference, args.sam_model, args.device,
            min_area_pct=args.auto_min_area,
            min_stability=args.auto_min_stability,
            points_per_side=args.auto_points_per_side,
            pred_iou=args.auto_pred_iou,
            nms_iou=args.auto_nms_iou,
            containment=args.auto_containment,
            max_layers=args.auto_max_layers,
            scene=scene,
            scene_match=args.scene_match,
        ):
            items.append(mask)
    elif args.knockout:
        knockout_path = Path(args.knockout)
        if not knockout_path.exists():
            sys.exit(f"knockout cut-out not found: {knockout_path}")
        cut = Image.open(knockout_path).convert("RGBA")
        if cut.size != reference.size:
            ref_aspect, cut_aspect = width / height, cut.width / cut.height
            if abs(ref_aspect - cut_aspect) > 0.01:
                sys.exit(
                    f"knockout: cut-out is {cut.width}x{cut.height} but reference is "
                    f"{width}x{height}; they must be the same canvas (same aspect ratio)"
                )
            print(f"  note: cut-out scaled {cut.width}x{cut.height} -> {width}x{height}")
            cut = cut.resize((width, height), Image.Resampling.LANCZOS)
        items, info = build_knockout_items(
            reference, cut, args.knockout_underlap, args.knockout_white,
            args.knockout_keep_cutout, parse_knockout_exclude(args.knockout_exclude),
        )
        print(
            f"  knockout via {info['method']}: cut-out covers {info['coverage'] * 100:.1f}% "
            f"of the canvas, underlap {info['underlap']}px"
            + (f", {info['excluded']} object(s) kept" if info.get("excluded") else "")
        )
        if not re.search(r"background$", output_name, re.IGNORECASE):
            output_name = f"{output_name} background"
    elif args.export_path:
        export_path = Path(args.export_path)
        if not export_path.exists():
            sys.exit(f"export not found: {export_path}")
        export = load_rgba(export_path)
        holes, recovery_method, notes = detect_missing_holes(
            reference,
            export,
            hole_source=args.recover_holes,
            alpha_threshold=args.recover_alpha,
            white_threshold=args.recover_white,
            diff_threshold=args.recover_diff,
        )
        for note in notes:
            print(f"  note: {note}")
        regions, stats = find_hole_regions(
            holes,
            min_area_pct=args.recover_min_area,
            ignore_border=not args.recover_keep_border,
        )
        if not regions and args.recover_holes == "auto" and recovery_method == "alpha":
            print("  note: no transparent holes survived; retrying near-white detection")
            holes, recovery_method, notes = detect_missing_holes(
                reference,
                export,
                hole_source="white",
                white_threshold=args.recover_white,
                diff_threshold=args.recover_diff,
            )
            for note in notes:
                print(f"  note: {note}")
            regions, stats = find_hole_regions(
                holes,
                min_area_pct=args.recover_min_area,
                ignore_border=not args.recover_keep_border,
            )
        if not regions:
            sys.exit(
                "no missing regions found. Check that the export is aligned with the "
                "final image and contains transparent or near-white holes; try lowering "
                "the minimum area, changing the hole source, or keeping border regions."
            )

        print(f"  recovered {len(regions)} missing region(s) via {recovery_method} detection")
        print(
            f"  ignored {stats['small']} small and {stats['border']} border-touching "
            f"region(s); minimum area {stats['min_area']}px"
        )
        for index, region in enumerate(regions, 1):
            x0, y0, x1, y1 = region["bbox"]
            print(
                f"    missing-{index:02d}: {x1 - x0}x{y1 - y0} at ({x0},{y0}), "
                f"{region['area']}px"
            )
            alpha = region["mask"]
            items.append(LayerMask(f"missing-{index:02d}", alpha))

        base_image = export
        if not args.keep_base_alpha:
            base_image, normalized_from = solidify_base_alpha(export, args.recover_alpha)
            if normalized_from is not None:
                print(
                    f"  base alpha normalized from {normalized_from} to 255 "
                    "(uniform partial export opacity)"
                )

        recover_regions = regions
        if not re.search(r"recovered$", output_name, re.IGNORECASE):
            output_name = f"{output_name} recovered"
    else:
        masks_dir = args.masks
        if not masks_dir or not masks_dir.is_dir():
            sys.exit(f"masks dir not found: {masks_dir}")
        seen_names: dict[str, int] = {}
        for mask_path in sorted(masks_dir.iterdir()):
            if mask_path.suffix.lower() not in {".png", ".webp", ".tif", ".tiff"}:
                continue
            # FIX: dedupe the *name*, not just the file-on-disk stem.
            base = mask_path.stem
            count = seen_names.get(base, 0) + 1
            seen_names[base] = count
            name = base if count == 1 else f"{base}-{count}"
            if count > 1:
                print(f"  note: duplicate stem {base!r} → {name!r}")
            alpha = load_mask(mask_path, (width, height))
            if args.strict:
                extrema = alpha.getextrema()
                if extrema[1] == 0:
                    sys.exit(f"strict: mask {name!r} is empty")
                if extrema[0] == 255:
                    sys.exit(f"strict: mask {name!r} covers the whole canvas")
            items.append(LayerMask(name, alpha))

    if not items:
        sys.exit("no usable masks")

    # Depth pass.
    use_depth = bool(args.depth)
    use_depth_model = use_depth and args.depth_model.lower() != "none"
    depth_map = None
    if use_depth_model:
        device = args.device
        if device == "auto":
            try:
                import torch
                device = resolve_sam_device("auto", torch)
            except ImportError:
                device = "cpu"
        print(f"  Loading depth model {args.depth_model} on {device}…")
        depth_map = compute_depth(reference, args.depth_model, device)

    # Prepare entries.
    slug = slugify(output_name, 60) or "image-to-layers"

    if args.mode == "zip":
        if recovering:
            preview_path = Path(f"{slug}-missing-preview.png")
            write_recover_preview(reference, recover_regions, preview_path)
            print(f"preview {preview_path.resolve()}")
        # Apply refinement before zipping.
        refined: list[LayerMask] = []
        for item in items:
            name, alpha = item
            refined_alpha = refine_alpha(
                alpha, args.feather, args.dilate,
                decontaminate=False,  # ZIP cuts straight from the reference anyway
            )
            refined.append(item.with_alpha(refined_alpha))
        emit_zip(reference, refined, slug)
        return 0

    project_dir = Path(args.out) / slug
    images_dir = project_dir / "images"
    compositions_dir = project_dir / "compositions"
    images_dir.mkdir(parents=True, exist_ok=True)
    compositions_dir.mkdir(parents=True, exist_ok=True)

    ref_name = f"reference{reference_path.suffix.lower()}"
    ref_out = images_dir / ref_name
    if not ref_out.exists():
        saved_reference = reference.convert("RGB") if ref_out.suffix.lower() in {".jpg", ".jpeg"} else reference
        saved_reference.save(ref_out, optimize=True)
    reference_url = f"/data/projects/{project_dir.name}/images/{ref_name}"

    preview_url = None
    if recovering:
        base_path = images_dir / "export.png"
        assert base_image is not None
        base_image.save(base_path, optimize=True)
        base_url = f"/data/projects/{project_dir.name}/images/export.png"
        base_meta = {
            "id": "aligned-export",
            "name": "Aligned export",
            "url": base_url,
        }

        preview_path = images_dir / "missing-preview.png"
        write_recover_preview(reference, recover_regions, preview_path)
        print(f"preview {preview_path.resolve()}")
        preview_url = f"/data/projects/{project_dir.name}/images/missing-preview.png"
        recover_meta = {
            "source": "image-to-layers-recover",
            "note": (
                "Missing regions were detected from an aligned studio export and cut "
                "from the final image. The aligned export is the visible base layer."
            ),
            "recover": {
                "exportName": Path(args.export_path).name,
                "holeSource": recovery_method,
                "regions": len(recover_regions),
                "minAreaPct": args.recover_min_area,
                "ignoredBorder": not args.recover_keep_border,
                "previewUrl": preview_url,
                "baseLayerId": "aligned-export",
            },
        }

    entries: list[dict] = []
    used_stems: dict[str, int] = {}
    alpha_by_entry: dict[str, Image.Image] = {}

    # A group only gets its own folder when it holds 2+ layers, so a manual
    # one-layer-per-group run does not scatter single files into many folders.
    group_sizes: dict[str, int] = {}
    for item in items:
        group_name = getattr(item, "group", "")
        if group_name:
            group_sizes[group_name] = group_sizes.get(group_name, 0) + 1

    def folder_for(item) -> str:
        group_name = getattr(item, "group", "")
        if args.folders == "group" and group_name and group_sizes.get(group_name, 0) >= 2:
            return slugify(group_name, 40).lower()
        return ""

    for item in items:
        name, raw_alpha = item
        alpha = refine_alpha(raw_alpha, args.feather, args.dilate)
        if args.decontaminate:
            cut = cutout_decontaminated(reference, alpha)
        else:
            cut = cutout(reference, alpha)
        cropped, bbox = tight_crop(cut)
        if cropped.width == 0 or cropped.height == 0:
            print(f"  skip {name}: empty crop")
            continue
        if args.strict and bbox == (0, 0, width, height) and len(items) > 1:
            print(f"  warn (strict): {name!r} covers the whole canvas")
        folder = folder_for(item)
        stem = _unique_stem(f"{folder}/{slugify(name, 40)}", used_stems).split("/", 1)[1]
        target_dir = images_dir / folder if folder else images_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        out_file = target_dir / f"{stem}.png"
        cropped.save(out_file, optimize=True)
        relative_file = out_file.relative_to(images_dir).as_posix()
        entry = {
            "name": name,
            "id": f"layer-{folder + '-' if folder else ''}{stem}",
            "role": getattr(item, "role", None) or classify_role(name),
            "crop": bbox,
            "url": f"/data/projects/{project_dir.name}/images/{relative_file}",
            "file": f"images/{relative_file}",
        }
        segment_group = getattr(item, "group", "")
        if segment_group:
            entry["segment_group"] = segment_group
        if getattr(item, "label", ""):
            entry["label"] = item.label
        if getattr(item, "description", ""):
            entry["description"] = item.description
        # Scene-named layers keep the vision model's role; guessing "character-part"
        # from the file name would undo that. Everything else behaves as before.
        part = classify_part(name)
        if part and (getattr(item, "source", "") != "scene" or entry["role"] == "character-part"):
            entry["role"] = "character-part"
            entry["part"] = part[0]
            entry["group"] = part[1]
            entry["variant"] = part[2]
        if use_depth:
            if depth_map is not None:
                score = layer_depth_score(depth_map, alpha)
                entry["depth_score"] = score if score is not None else 0.5
            else:
                y_center = (bbox[1] + bbox[3]) / 2 / height
                entry["depth_score"] = 1.0 - y_center
        entries.append(entry)
        alpha_by_entry[entry["id"]] = alpha

    if not entries:
        sys.exit("no usable entries after cropping")

    if use_depth and args.depth_role:
        for entry in entries:
            score = entry.get("depth_score", 0.5)
            if score < 0.18:
                entry["role"] = "background"
            elif score < 0.35:
                entry["role"] = "midground"
            elif score < 0.6:
                entry["role"] = "ground"
            elif score > 0.82:
                entry["role"] = "foreground"

    composition_meta_extra = dict(recover_meta or {})
    if scene:
        composition_meta_extra["scene"] = {
            "description": scene.get("description", ""),
            "style": scene.get("style", ""),
            "model": scene.get("model", ""),
            "objects": len(scene.get("objects", [])),
        }
    composition = build_composition(
        entries,
        output_name,
        width,
        height,
        reference_url,
        base=base_meta,
        meta=composition_meta_extra or None,
        sort_by_depth=use_depth,
    )

    # Manifest: what file lives in which folder, and why it has that name.
    manifest = {
        "name": output_name,
        "width": width,
        "height": height,
        "description": (scene or {}).get("description", ""),
        "layers": [
            {
                "name": entry["name"],
                "file": entry["file"],
                "folder": str(Path(entry["file"]).parent.relative_to("images").as_posix())
                if Path(entry["file"]).parent != Path("images") else "",
                "group": entry.get("segment_group", ""),
                "role": entry["role"],
                "label": entry.get("label", ""),
                "description": entry.get("description", ""),
                "bbox": list(entry["crop"]),
            }
            for entry in sorted(entries, key=lambda e: (e["file"]))
        ],
    }
    (project_dir / "layers.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if scene:
        (project_dir / "scene.json").write_text(json.dumps(scene, indent=2), encoding="utf-8")

    out_json = compositions_dir / f"{slug}.composition.json"
    out_json.write_text(json.dumps(composition, indent=2), encoding="utf-8")
    print(f"wrote {out_json}")
    folders_used = sorted({m["folder"] for m in manifest["layers"] if m["folder"]})
    if folders_used:
        print(f"      folders: {', '.join(folders_used)}")
    if recovering:
        print(f"      {len(entries)} missing layers + 1 aligned base, {width}x{height}")
    else:
        print(f"      {len(entries)} layers, {width}x{height}")
    if depth_map is not None:
        preview_path = project_dir / "depth-preview.png"
        Image.fromarray((depth_map * 255).astype("uint8")).save(preview_path)
        print(f"      depth preview → {preview_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())