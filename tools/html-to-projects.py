#!/usr/bin/env python3
"""
html-to-projects.py — ingest an HTML page containing up to 3 social-post
templates (`<div class="stage">` blocks) and emit one LayerStudio project
folder per stage.

Each stage is inspected in three passes:

  1. CSS + DOM walk
     Resolve `--img`/`background-image`/`<img src>` to get the background
     photo. Walk the subtree to pick out visible text nodes and image
     leaves, compute each one's absolute % box by adding the offsets of
     every positioned ancestor (`position:absolute|relative|fixed`).
     `data-s`/`data-e` on any ancestor becomes a fade in/out keyframe.

  2. (Optional) background segmentation
     When `--segment-background` is given, the extracted background photo
     is written to a temp file and passed to `image-to-layers.py --sam-auto`
     (optionally `--scene-json`). The resulting cut-out layers are inserted
     between the flat background and the HTML text layers.

  3. Composition emission
     Writes `<out>/<slug>/images/*` and `<out>/<slug>/compositions/<slug>.composition.json`
     in the LayerStudio schema (see the Singl-end example).

Preview only:
    python3 html-to-projects.py --html page.html --preview-only
    → prints a single JSON line to stdout: {"stages":[{...},...]}

Write projects:
    python3 html-to-projects.py --html page.html --out data/projects --name "My Page"
    → emits `project <path>` lines, one per generated project.
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import ipaddress
import math
import socket
import urllib.parse
import urllib.request
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image  # type: ignore
except ImportError:
    Image = None  # type: ignore

# ── constants ────────────────────────────────────────────────────────────

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
RAW_TEXT_TAGS = {"script", "style"}
SKIP_CLASSES = {"bar", "safe", "fade", "itl-tooltip"}
CLASS_HINTS = {
    "hdr": ("background", "Header"),
    "win": ("background", "Photo window"),
    "cam": ("background", "Photo"),
    "card": ("foreground", "Card"),
    "rule": ("foreground", "Rule badge"),
    "cp": ("foreground", "Phase"),
    "dish": ("foreground", "Dish"),
    "bb": ("foreground", "Speech bubble"),
    "stp": ("foreground", "Stamp"),
    "ftr": ("foreground", "Footer"),
    "tt": ("foreground", "Title"),
}
TAG_HINTS = {
    "b": "Headline",
    "small": "Small print",
    "em": "Badge",
    "u": "Next rule",
    "i": "Kicker",
    "p": "Body",
}
DEFAULT_STAGE_W = 1080
DEFAULT_STAGE_H = 1920
DEFAULT_DURATION_MS = 10000
FADE_MS = 300
SEG_TIMEOUT_S = 1800

# Run-wide state, set once in main().
DURATION_MS = DEFAULT_DURATION_MS
ROOT_VARS: dict[str, str] = {}
FETCH_REMOTE = False
PREVIEW = False
_COMPUTED_CACHE: dict[int, dict] = {}
INHERITED_PROPS = ("color", "font-family", "font-size", "font-weight", "font-style",
                   "line-height", "letter-spacing", "text-align", "text-transform")


def say(msg: str) -> None:
    """Progress/warning output. Preview mode keeps stdout clean for the JSON line."""
    print(msg, file=sys.stderr if PREVIEW else sys.stdout, flush=True)

# ── tiny CSS parser ──────────────────────────────────────────────────────

def _parse_declarations(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    depth = 0
    chunk = ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            _add_decl(out, chunk)
            chunk = ""
        else:
            chunk += ch
    _add_decl(out, chunk)
    return out


def _add_decl(out: dict[str, str], chunk: str) -> None:
    chunk = chunk.strip()
    if not chunk or ":" not in chunk:
        return
    k, _, v = chunk.partition(":")
    v = re.sub(r"\s*!important\s*$", "", v.strip(), flags=re.I)
    out[k.strip()] = v


def extract_root_vars(css: str) -> dict[str, str]:
    """Collect `--name: value` from every `:root[...]? { ... }` block."""
    vars: dict[str, str] = {}
    for m in re.finditer(r":root\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", css):
        vars.update(_parse_declarations(m.group(1)))
    return vars


def resolve_css_var(value: str, vars: dict[str, str], depth: int = 0) -> str:
    """Resolve var(--x) and var(--x, fallback), including nested references."""
    pattern = re.compile(r"var\(\s*--([\w-]+)\s*(?:,\s*([^()]*))?\)")
    for _ in range(20):
        if "var(" not in value:
            break
        new = pattern.sub(lambda m: vars.get(m.group(1), vars.get("--" + m.group(1), (m.group(2) or "").strip())), value)
        if new == value:
            break
        value = new
    return value


def extract_url(value: str) -> str | None:
    m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", value)
    return m.group(1).strip() if m else None


def _split_selector_list(selector: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in selector:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p]


def _specificity(selector: str) -> tuple[int, int, int]:
    ids = selector.count("#")
    classes = selector.count(".") + selector.count("[") + len(re.findall(r":(?!:)", selector))
    tags = len(re.findall(r"(?:^|[\s>+~])([a-zA-Z][\w-]*)", selector))
    return (ids, classes, tags)


def split_rules(css: str) -> list[tuple[str, dict[str, str]]]:
    """Split `selector { a:b; c:d }` pairs: comments stripped, selector lists
    expanded, result ordered by specificity (stable, so source order breaks ties).
    @media/@supports blocks are skipped: they depend on a viewport we don't have."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[tuple[str, dict[str, str]]] = []
    i, n = 0, len(css)
    while i < n:
        brace = css.find("{", i)
        if brace < 0:
            break
        selector = css[i:brace].strip()
        depth, j = 0, brace
        while j < n:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = css[brace + 1:j]
        if selector and not selector.startswith("@"):
            decl = _parse_declarations(body)
            for sel in _split_selector_list(selector):
                rules.append((sel, decl))
        i = j + 1
    rules.sort(key=lambda r: _specificity(r[0]))
    return rules


# ── selector matching (tag / .class / #id / descendant / :pseudo) ────────

def _compound_matches(compound: str, node: "Node") -> bool:
    if not compound or compound == "*":
        return True
    pseudo = ""
    if ":" in compound:
        compound, _, pseudo = compound.partition(":")
        if pseudo.startswith(":") or pseudo.split("(")[0] not in ("first-child", "last-child"):
            return False  # ::before, :hover, :root … never describe a plain element
        siblings = node.parent.children if node.parent else [node]
        if pseudo == "first-child" and siblings[0] is not node:
            return False
        if pseudo == "last-child" and siblings[-1] is not node:
            return False
    compound = compound.split("[", 1)[0]
    tag_m = re.match(r"^([a-zA-Z][\w-]*)", compound)
    if tag_m and node.tag != tag_m.group(1).lower():
        return False
    for idm in re.finditer(r"#([\w-]+)", compound):
        if node.id != idm.group(1):
            return False
    for clsm in re.finditer(r"\.([\w-]+)", compound):
        if clsm.group(1) not in node.classes:
            return False
    return True


def selector_matches(selector: str, node: "Node") -> bool:
    """Supports compound selectors with descendant (' ') and child ('>') combinators."""
    tokens = re.findall(r"[^\s>+~]+|[>+~]", selector.strip())
    if not tokens or any(t in ("+", "~") for t in tokens):
        return False
    parts: list[tuple[str, str]] = []  # (compound, combinator linking it to the previous compound)
    comb = " "
    for t in tokens:
        if t == ">":
            comb = ">"
            continue
        parts.append((t, comb))
        comb = " "
    if not parts or not _compound_matches(parts[-1][0], node):
        return False
    return _match_ancestors(parts, len(parts) - 1, node)


def _match_ancestors(parts, i: int, node: "Node") -> bool:
    if i == 0:
        return True
    comb, prev = parts[i][1], parts[i - 1][0]
    cur = node.parent
    while cur is not None and cur.tag not in ("body", "html", "root"):
        if _compound_matches(prev, cur) and _match_ancestors(parts, i - 1, cur):
            return True
        if comb == ">":
            return False
        cur = cur.parent
    return False


# ── DOM tree ─────────────────────────────────────────────────────────────

class Node:
    __slots__ = ("tag", "classes", "id", "style", "attrs",
                 "children", "text", "parent")

    def __init__(self, tag: str, attrs: dict, parent: "Node | None" = None):
        self.tag = tag
        self.classes = (attrs.get("class") or "").split()
        self.id = attrs.get("id") or ""
        self.style = attrs.get("style") or ""
        self.attrs = attrs
        self.children: list[Node] = []
        self.text = ""
        self.parent = parent

    def __repr__(self) -> str:
        return f"<{self.tag} .{'.'.join(self.classes)}>"


class Walker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.stack: list[Node] = [self.root]
        self.stages: list[Node] = []
        self._raw_tag: str | None = None
        self._css_chunks: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs = dict(attrs)
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if any(c in node.classes for c in ("stage", "post", "reel", "story")) and not any(any(c in anc.classes for c in ("stage", "post", "reel", "story")) for anc in self.stack[1:]):
            self.stages.append(node)
        if tag == "br":
            self.stack[-1].text += "\n"
        if tag in RAW_TEXT_TAGS:
            self._raw_tag = tag
            self._in_style = tag == "style"
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == self._raw_tag:
            self._raw_tag = None
            self._in_style = False
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_style:
            self._css_chunks.append(data)
            return
        if self._raw_tag:
            return
        text = data.strip()
        if text:
            self.stack[-1].text += (" " if self.stack[-1].text else "") + text


def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


# ── computed style ───────────────────────────────────────────────────────

def computed(node: Node, rules) -> dict[str, str]:
    """Cascade (by specificity) + inline style, with var() resolved against
    :root and every ancestor's custom properties. Cached per node."""
    hit = _COMPUTED_CACHE.get(id(node))
    if hit is not None:
        return hit
    style: dict[str, str] = {}
    for selector, decl in rules:
        if selector_matches(selector, node):
            style.update(decl)
    style.update(_parse_declarations(node.style))
    scope = {(k[2:] if k.startswith("--") else k): v for k, v in ROOT_VARS.items()}
    if node.parent is not None and node.parent.tag != "root":
        scope.update({k[2:]: v for k, v in computed(node.parent, rules).items() if k.startswith("--")})
    scope.update({k[2:]: v for k, v in style.items() if k.startswith("--")})
    for k, v in list(style.items()):
        if "var(" in v:
            style[k] = resolve_css_var(v, scope)
    _COMPUTED_CACHE[id(node)] = style
    return style


def inherited(node: Node, rules) -> dict[str, str]:
    """computed() plus the properties CSS inherits (colour, font, alignment …)."""
    style = dict(computed(node, rules))
    cur = node.parent
    while cur is not None and cur.tag not in ("html", "root"):
        pst = computed(cur, rules)
        for k in INHERITED_PROPS:
            if k not in style and k in pst:
                style[k] = pst[k]
        cur = cur.parent
    return style


# ── length conversion ────────────────────────────────────────────────────

_NUM = re.compile(r"^(-?\d*\.?\d+)\s*([a-z%]*)$", re.I)


def length_pct(value: str | None, axis: str, W: float, H: float) -> float | None:
    if not value:
        return None
    m = _NUM.match(value.strip())
    if not m:
        return None
    n, unit = float(m.group(1)), (m.group(2) or "px").lower()
    if unit == "%":
        return n
    if unit == "cqw":
        return n if axis == "x" else n * (W / H) if H else 0
    if unit == "cqh":
        return n if axis == "y" else n * (H / W) if W else 0
    if unit == "px":
        base = W if axis == "x" else H
        return n / base * 100 if base else 0
    if unit in ("em", "rem"):
        base = W if axis == "x" else H
        return n * 16 / base * 100 if base else 0
    if unit == "vw":
        return n if axis == "x" else n * W / H if H else 0
    if unit == "vh":
        return n if axis == "y" else n * H / W if W else 0
    return None


def _aspect_ratio_val(val: str | None) -> float | None:
    if not val:
        return None
    val = val.strip()
    if "/" in val:
        parts = val.split("/", 1)
        try:
            return float(parts[0].strip()) / float(parts[1].strip())
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(val)
    except ValueError:
        return None


def _partial_box(style: dict[str, str], W: float, H: float, fill_h: bool = True):
    inset = style.get("inset")
    if inset:
        iparts = inset.strip().split()
        if len(iparts) == 1:
            it = ir = ib = il = iparts[0]
        elif len(iparts) == 2:
            it, ib = iparts[0], iparts[0]
            ir, il = iparts[1], iparts[1]
        elif len(iparts) == 4:
            it, ir, ib, il = iparts[0], iparts[1], iparts[2], iparts[3]
        else:
            it = ir = ib = il = iparts[0]
        style = {"top": it, "right": ir, "bottom": ib, "left": il, **style}

    x = length_pct(style.get("left"), "x", W, H)
    if x is None and style.get("right"):
        r = length_pct(style["right"], "x", W, H)
        if r is not None:
            x = 100 - r
    w = length_pct(style.get("width") or style.get("min-width"), "x", W, H)
    if w is None and x is not None and style.get("right"):
        r = length_pct(style["right"], "x", W, H)
        if r is not None:
            w = 100 - x - r
    if x is not None and w is None and style.get("right"):
        w = 100 - x

    y = length_pct(style.get("top"), "y", W, H)
    if y is None and style.get("bottom"):
        b = length_pct(style["bottom"], "y", W, H)
        if b is not None:
            y = 100 - b
    h = length_pct(style.get("height") or style.get("min-height"), "y", W, H)
    if h is None and y is not None and style.get("bottom"):
        b = length_pct(style["bottom"], "y", W, H)
        if b is not None:
            h = 100 - y - b

    ar = _aspect_ratio_val(style.get("aspect-ratio"))
    if h is None and w is not None and ar and H:
        h = (w / 100 * W / ar) / H * 100
    elif w is None and h is not None and ar and W:
        w = (h / 100 * H * ar) / W * 100

    if fill_h and y is not None and h is None:
        h = 100 - y
    return x, y, w, h


def box_from_style(style: dict[str, str], W: float, H: float) -> dict | None:
    x, y, w, h = _partial_box(style, W, H)
    if None in (x, y, w, h):
        return None
    return {"x": round(x, 3), "y": round(y, 3), "w": round(w, 3), "h": round(h, 3)}


def _apply_translate(box: dict, style: dict[str, str], W: float, H: float) -> dict:
    """Honour translate(-50%, -50%) style centring, the usual companion of left:50%."""
    t = style.get("transform") or ""
    m = re.search(r"translate(X|Y)?\(\s*([^,)]+)(?:,\s*([^)]+))?\)", t)
    if not m:
        return box
    axis, a, b = m.groups()
    tx, ty = (a, None) if axis == "X" else (None, a) if axis == "Y" else (a, b)
    out = dict(box)
    for val, key, dim, ax in ((tx, "x", "w", "x"), (ty, "y", "h", "y")):
        if not val:
            continue
        val = val.strip()
        try:
            if val.endswith("%"):
                out[key] = round(out[key] + out[dim] * float(val[:-1]) / 100, 3)
            else:
                d = length_pct(val, ax, W, H)
                if d is not None:
                    out[key] = round(out[key] + d, 3)
        except ValueError:
            pass
    return out


def _rotation(style: dict[str, str]) -> float:
    m = re.search(r"rotate\(\s*(-?[\d.]+)deg", style.get("transform") or "")
    return float(m.group(1)) if m else 0.0


# ── position resolution ─────────────────────────────────────────────────

def positioned_offset(node: Node, rules, W: float, H: float) -> tuple[float, float]:
    """Sum `top`/`left` of every positioned ancestor, stopping at stage root."""
    ox = oy = 0.0
    cur = node.parent
    while cur is not None and cur.tag not in ("body", "html", "root"):
        if any(c in cur.classes for c in ("stage", "post", "reel", "story")):
            break
        style = computed(cur, rules)
        if style.get("position") in ("absolute", "relative", "fixed"):
            x, y, _, _ = _partial_box(style, W, H, fill_h=False)
            ox += x or 0.0
            oy += y or 0.0
        cur = cur.parent
    return ox, oy


def _line_height(style: dict[str, str]) -> float:
    m = _NUM.match((style.get("line-height") or "").strip())
    if m and not m.group(2):
        return float(m.group(1))
    if m and m.group(2) == "%":
        return float(m.group(1)) / 100
    if m and m.group(2) in ("em", "rem"):
        return float(m.group(1))
    return 1.2


def estimate_height(style: dict[str, str], W: float, H: float,
                    text: str = "", width_pct: float = 100.0) -> float:
    """Height in % of the stage: font size × line height × estimated wrapped lines."""
    fs = style.get("font-size", "")
    fs_y = (length_pct(fs, "y", W, H) if fs else None) or (16 / H * 100)
    lines = 1
    if text:
        fs_x = (length_pct(fs, "x", W, H) if fs else None) or (16 / W * 100)
        char_px = max(1.0, fs_x / 100 * W * 0.52)
        per_line = max(1, int((max(1.0, width_pct / 100 * W)) // char_px))
        lines = sum(max(1, math.ceil(len(part) / per_line)) for part in text.split("\n"))
    return fs_y * _line_height(style) * lines


def estimate_block_y(node: Node, rules, W: float, H: float, parent_box: dict) -> float:
    """Estimate a static child's y by stacking previous block siblings."""
    parent = node.parent
    if parent is None:
        return parent_box["y"]
    try:
        idx = parent.children.index(node)
    except ValueError:
        return parent_box["y"]
    y = parent_box["y"]
    for sib in parent.children[:idx]:
        sstyle = computed(sib, rules)
        if sstyle.get("position") == "absolute":
            continue
        if sstyle.get("display") in ("none", "inline"):
            continue
        y += estimate_height(sstyle, W, H, sib.text, parent_box["w"])
    return y


def resolve_box(node: Node, rules, W: float, H: float, extra: dict | None = None) -> dict | None:
    """Absolute % box. Explicit left/top/width/height win per-axis; anything
    missing is inherited from the parent box / estimated from stacked siblings."""
    style = {**(extra or {}), **computed(node, rules)}
    x, y, w, h = _partial_box(style, W, H, fill_h=False)
    own_xy = x is not None and y is not None
    is_estimated = False
    if None in (x, y, w, h):
        parent = node.parent
        pbox = resolve_box(parent, rules, W, H) if parent is not None else None
        if pbox is None and all(v is None for v in (x, y, w, h)):
            return None
        if pbox is None:
            is_estimated = True
            pbox = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0}
        if x is None:
            x = pbox["x"]
        if w is None:
            w = pbox["w"]
        if y is None:
            y = estimate_block_y(node, rules, W, H, pbox)
        if h is None:
            h = estimate_height(style, W, H, node.text, w)
    px, py = positioned_offset(node, rules, W, H)
    if own_xy:
        x, y = x + px, y + py
    box = {"x": round(x, 3), "y": round(y, 3), "w": round(w, 3), "h": round(h, 3), "_estimated": is_estimated}
    return _apply_translate(box, style, W, H)


# ── colours / fonts / timing ────────────────────────────────────────────

NAMED_COLOURS = {
    "white": "#ffffff", "black": "#000000", "red": "#ff0000", "transparent": "#00000000",
    "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0", "yellow": "#ffff00",
    "orange": "#ffa500", "blue": "#0000ff", "green": "#008000", "lime": "#00ff00",
    "purple": "#800080", "pink": "#ffc0cb", "navy": "#000080", "teal": "#008080",
    "maroon": "#800000", "gold": "#ffd700", "ivory": "#fffff0", "beige": "#f5f5dc",
}


def parse_colour(value: str | None, default: str = "#ffffff") -> str:
    """#rgb/#rrggbb(aa), rgb()/rgba(), hsl()/hsla() and common names → #rrggbb[aa]."""
    if not value:
        return default
    v = value.strip().lower()
    if v in NAMED_COLOURS:
        return NAMED_COLOURS[v]
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        return "#" + h if len(h) in (6, 8) and re.fullmatch(r"[0-9a-f]+", h) else default
    m = re.match(r"(rgba?|hsla?)\(\s*([^)]+)\)", v)
    if not m:
        return default
    parts = [p for p in re.split(r"[\s,/]+", m.group(2).strip()) if p]
    if len(parts) < 3:
        return default
    try:
        alpha = 1.0
        if len(parts) > 3:
            alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
        if m.group(1).startswith("rgb"):
            rgb = [round(float(p[:-1]) * 2.55) if p.endswith("%") else round(float(p)) for p in parts[:3]]
        else:
            hue = float(parts[0].replace("deg", "")) % 360 / 360
            sat, lig = (float(p.rstrip("%")) / 100 for p in parts[1:3])
            rgb = [round(c * 255) for c in colorsys.hls_to_rgb(hue, lig, sat)]
        r, g, b = (max(0, min(255, c)) for c in rgb)
    except ValueError:
        return default
    out = f"#{r:02x}{g:02x}{b:02x}"
    return out + f"{round(max(0.0, min(1.0, alpha)) * 255):02x}" if alpha < 1 else out


def parse_family(value: str | None) -> str:
    if not value:
        return "Helvetica Neue"
    first = value.split(",", 1)[0]
    return first.strip().strip("'\"")


def parse_weight(value: str | None) -> int:
    v = (value or "").strip().lower()
    if v == "bold":
        return 700
    try:
        return int(float(v)) if v else 400
    except (TypeError, ValueError):
        return 400


def parse_letter_spacing(value: str | None) -> float:
    if not value:
        return 0.0
    m = _NUM.match(value.strip())
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "em":
        return n
    if unit == "px":
        return n / 16
    return 0.0


def find_timing(node: Node) -> tuple[float, float | None]:
    cur = node
    while cur is not None:
        s, e = cur.attrs.get("data-s"), cur.attrs.get("data-e")
        if s or e:
            try:
                return (float(s) if s else 0.0), (float(e) if e else None)
            except ValueError:
                return 0.0, None
        cur = cur.parent
    return 0.0, None


# ── stage extraction ────────────────────────────────────────────────────

def extract_background(stage: Node, rules, root_vars, stage_style, W, H) -> str | None:
    inline = _parse_declarations(stage.style)
    scope = {**root_vars, **inline}
    img_var = inline.get("--img")
    if img_var:
        url = extract_url(resolve_css_var(img_var, scope))
        if url:
            return url

    own = computed(stage, rules)
    for key in ("--img", "background-image", "background"):
        val = own.get(key, "")
        if val:
            url = extract_url(resolve_css_var(val, scope))
            if url:
                return url

    for node in walk(stage):
        if node is stage:
            continue
        style = computed(node, rules)
        for key in ("--img", "background-image", "background"):
            val = style.get(key, "")
            if val:
                url = extract_url(resolve_css_var(val, scope))
                if url:
                    if any(c in node.classes for c in ("cam", "abs", "photo", "image", "im", "bgb", "world", "bgp", "nvi")):
                        return url
                    x, y, w, h = _partial_box(style, W, H, fill_h=False)
                    if (w is None or w >= 70) and (h is None or h >= 70):
                        return url

    for node in walk(stage):
        if node.tag == "img" and node.attrs.get("src"):
            return node.attrs["src"]

    for key in ("background", "background-color"):
        val = own.get(key, "") or stage_style.get(key, "")
        if val and val != "transparent" and not val.startswith("url"):
            resolved = resolve_css_var(val, root_vars)
            if resolved and resolved != "transparent":
                return make_svg_data_url(W, H, fill=resolved)

    return None


def stack_children(container: Node, rules, W, H, parent_box) -> list[tuple[Node, dict]]:
    """Approximate flex-column layout of direct children."""
    style = computed(container, rules)
    if "flex" not in (style.get("display") or ""):
        return []
    direction = style.get("flex-direction", "row")
    if "column" not in direction:
        return []
    gap = style.get("gap", "0")
    gap_px = 0.0
    gm = _NUM.match(gap.strip())
    if gm:
        n, unit = float(gm.group(1)), (gm.group(2) or "px").lower()
        if unit == "cqw":
            gap_px = n * W / 100
        elif unit == "cqh":
            gap_px = n * H / 100
        else:
            gap_px = n
    out: list[tuple[Node, dict]] = []
    y = parent_box["y"]
    for child in container.children:
        cstyle = computed(child, rules)
        h = estimate_height(cstyle, W, H, child.text, parent_box["w"])
        out.append((child, {"x": parent_box["x"], "y": round(y, 3),
                            "w": parent_box["w"], "h": round(h, 3)}))
        y += h + gap_px / H * 100
    return out


def _slug(text: str, n: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "layer"


def _clean_text(text: str) -> str:
    return re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip()


def make_layer(node: Node, rules, W, H, idx: int, box: dict | None = None,
               align_override: str | None = None, color_override: str | None = None,
               rotation_override: float | None = None, name_override: str | None = None,
               keyframes: list[dict] | None = None) -> dict | None:
    style = inherited(node, rules)
    if style.get("display") == "none" or style.get("visibility") == "hidden":
        return None
    if any(c in SKIP_CLASSES for c in node.classes):
        return None
    text = _clean_text(node.text)
    if not text:
        return None

    _x, _y, _w, _h = _partial_box(computed(node, rules), W, H, fill_h=False)
    explicit = (_x is not None and _y is not None) or (box is not None)
    if box is None:
        box = resolve_box(node, rules, W, H)
    if box is None:
        return None
    if not explicit:
        explicit = not box.get("_estimated", False)

    role, name = "foreground", "Text"
    for cls in node.classes:
        if cls in CLASS_HINTS:
            role, name = CLASS_HINTS[cls]
            break
    name = TAG_HINTS.get(node.tag, name)
    if name == "Text":
        name = " ".join(text.split()[:3])[:30]
    if name_override:
        name = name_override

    size_pct = length_pct(style.get("font-size", ""), "x", W, H) or 3.0
    start, end = find_timing(node)
    try:
        opacity = float(style.get("opacity", "1") or 1)
    except ValueError:
        opacity = 1.0
    align = align_override or {"start": "left", "end": "right"}.get(style.get("text-align", "left"), style.get("text-align", "left"))
    rotation = rotation_override if rotation_override is not None else _rotation(style)
    color = color_override or parse_colour(style.get("color"))

    layer = {
        "id": f"html-{idx:02d}-" + _slug(name),
        "type": "text",
        "name": name,
        "role": role,
        "z": idx,
        **box,
        "rotation": rotation,
        "opacity": opacity,
        "fit": "contain",
        "visible": True,
        "locked": False,
        "text": text,
        "meta": {
            "tag": node.tag,
            "cssClasses": node.classes,
            "roleHint": role,
            "textLength": len(text),
            "estimatedBox": not explicit,
            "start": start,
            "end": end,
        },
        "source": {
            "type": "html",
            "tag": node.tag,
            "classList": node.classes,
            "roleHint": role,
            "selector": "." + ".".join(node.classes) if node.classes else node.tag,
        },
        "font": {
            "family": parse_family(style.get("font-family")),
            "weight": parse_weight(style.get("font-weight")),
            "style": "italic" if "italic" in (style.get("font-style") or "") else "normal",
            "sizePct": round(size_pct, 2),
            "lineHeight": round(_line_height(style), 3),
            "letterSpacingEm": parse_letter_spacing(style.get("letter-spacing")),
            "transform": "uppercase" if "uppercase" in (style.get("text-transform") or "") else "none",
        },
        "color": color,
        "align": align,
        "verticalAlign": "middle",
        "overflow": "clip",
        "keyframes": [],
    }
    layer["keyframes"] = keyframes if keyframes is not None else _timing_keyframes(layer, start, end)
    return layer


def make_svg_data_url(w_px: float, h_px: float, fill: str = "#ffffff", stroke: str | None = None,
                      stroke_width: float = 0.0, rx: float = 0.0, is_circle: bool = False,
                      is_gingham: bool = False, shadow_color: str | None = None, shadow_dy: float = 0.0) -> str:
    w_px = max(2.0, round(w_px, 1))
    h_px = max(2.0, round(h_px, 1))
    svg_parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_px}" height="{h_px}" viewBox="0 0 {w_px} {h_px}">']
    defs = []
    fill_ref = fill
    if is_gingham:
        pat_size = round(w_px * 0.04, 1) or 43.2
        half = round(pat_size / 2.0, 1)
        defs.append(f'''<pattern id="gingham" width="{pat_size}" height="{pat_size}" patternUnits="userSpaceOnUse">
  <rect width="{pat_size}" height="{pat_size}" fill="#ffffff" />
  <rect width="{half}" height="{pat_size}" fill="rgba(168,32,44,0.53)" />
  <rect width="{pat_size}" height="{half}" fill="rgba(168,32,44,0.53)" />
</pattern>''')
        fill_ref = "url(#gingham)"
    if shadow_color and shadow_dy > 0:
        defs.append(f'''<filter id="drop" x="-10%" y="-10%" width="120%" height="140%">
  <feDropShadow dx="0" dy="{shadow_dy}" stdDeviation="0" flood-color="{shadow_color}"/>
</filter>''')
    if defs:
        svg_parts.append("<defs>" + "".join(defs) + "</defs>")
    hw = round(stroke_width / 2.0, 2) if stroke_width else 0.0
    rw = max(1.0, round(w_px - stroke_width, 1))
    rh = max(1.0, round(h_px - stroke_width, 1))
    filter_attr = ' filter="url(#drop)"' if (shadow_color and shadow_dy > 0) else ""
    stroke_attr = f' stroke="{stroke}" stroke-width="{stroke_width}"' if (stroke and stroke_width > 0) else ""
    if is_circle:
        cx, cy = round(w_px / 2.0, 1), round(h_px / 2.0, 1)
        r = max(1.0, round(min(w_px, h_px) / 2.0 - hw, 1))
        svg_parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill_ref}"{stroke_attr}{filter_attr}/>')
    else:
        rx_attr = f' rx="{rx}" ry="{rx}"' if rx > 0 else ""
        svg_parts.append(f'<rect x="{hw}" y="{hw}" width="{rw}" height="{rh}"{rx_attr} fill="{fill_ref}"{stroke_attr}{filter_attr}/>')
    svg_parts.append("</svg>")
    return "data:image/svg+xml;utf8," + urllib.parse.quote("".join(svg_parts))


def make_shape_layer(node: Node | None, rules, W: float, H: float, idx: int,
                     box: dict, name: str, role: str = "foreground",
                     url: str | None = None, start: float = 0.0,
                     end: float | None = None, rotation: float = 0.0,
                     keyframes: list[dict] | None = None,
                     fill: str | None = None, stroke: str | None = None,
                     stroke_width: float = 0.0, rx: float = 0.0,
                     is_circle: bool = False, is_gingham: bool = False,
                     shadow_color: str | None = None, shadow_dy: float = 0.0) -> dict:
    style = computed(node, rules) if node else {}
    if not url:
        w_px = box["w"] / 100.0 * W
        h_px = box["h"] / 100.0 * H
        bg = style.get("background") or style.get("background-color") or "#ffffff"
        if not is_gingham:
            is_gingham = "linear-gradient" in bg and ("90deg" in bg or "#a8202c" in bg)
        if fill is None:
            fill = parse_colour(bg) if not is_gingham else "#ffffff"
        if stroke is None:
            border = style.get("border") or ""
            if border and border != "none":
                bm = re.search(r"(-?\d*\.?\d+)\s*([a-z%]*)\s+(?:solid|dashed)\s+([^;]+)", border, re.I)
                if bm:
                    unit = (bm.group(2) or "px").lower()
                    n = float(bm.group(1))
                    stroke_width = (n / 100.0 * W) if unit == "cqw" else n
                    stroke = parse_colour(bm.group(3).strip())
        radius = style.get("border-radius") or ""
        if "50%" in radius:
            is_circle = True
        elif rx == 0.0 and radius:
            rm = _NUM.match(radius.split()[0].strip())
            if rm:
                runit = (rm.group(2) or "px").lower()
                rn = float(rm.group(1))
                rx = (rn / 100.0 * W) if runit == "cqw" else rn
        if shadow_dy == 0.0:
            shadow = style.get("box-shadow") or ""
            if shadow and shadow != "none":
                sm = re.search(r"(?:-?\d*\.?\d+[a-z%]*\s+)(-?\d*\.?\d+)\s*([a-z%]*)\s+(?:-?\d*\.?\d+[a-z%]*\s+)?([^;]+)", shadow, re.I)
                if sm:
                    sunit = (sm.group(2) or "px").lower()
                    sn = float(sm.group(1))
                    shadow_dy = (sn / 100.0 * W) if sunit == "cqw" else sn
                    shadow_color = parse_colour(sm.group(3).strip())
        url = make_svg_data_url(w_px, h_px, fill=fill, stroke=stroke, stroke_width=stroke_width,
                                rx=rx, is_circle=is_circle, is_gingham=is_gingham,
                                shadow_color=shadow_color, shadow_dy=shadow_dy)

    try:
        opacity = float(style.get("opacity", "1") or 1)
    except ValueError:
        opacity = 1.0

    rot = rotation or _rotation(style)
    kfs = keyframes or _timing_keyframes({"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"], "rotation": rot, "opacity": opacity}, start, end)

    layer = {
        "id": f"html-{idx:02d}-shape-" + _slug(name),
        "type": "image",
        "name": name,
        "role": role,
        "z": idx,
        **box,
        "rotation": rot,
        "opacity": opacity,
        "url": url,
        "frames": [url],
        "fit": "fill",
        "visible": True,
        "locked": False,
        "meta": {
            "tag": node.tag if node else "div",
            "cssClasses": node.classes if node else [],
            "start": start,
            "end": end,
            "isShape": True,
            "estimatedBox": False,
        },
        "source": {
            "type": "html-shape",
            "tag": node.tag if node else "div",
            "classList": node.classes if node else [],
        },
        "keyframes": kfs,
    }
    return layer


def make_image_layer(node: Node, rules, W: float, H: float, idx: int, url: str, name: str,
                     keyframes: list[dict] | None = None) -> dict | None:
    extra = {}
    if node.tag == "img":
        for a in ("width", "height"):
            v = (node.attrs.get(a) or "").strip()
            if re.fullmatch(r"\d+(\.\d+)?", v):
                extra[a] = v + "px"
    box = resolve_box(node, rules, W, H, extra)
    if box is None:
        return None
    style = computed(node, rules)
    try:
        opacity = float(style.get("opacity", "1") or 1)
    except ValueError:
        opacity = 1.0
    fit = style.get("object-fit") or ("cover" if "cover" in style.get("background-size", "") else "contain")
    start, end = find_timing(node)
    layer = {
        "id": f"html-{idx:02d}-" + _slug(name),
        "type": "image", "name": name, "role": "foreground", "z": idx, **box,
        "rotation": _rotation(style), "opacity": opacity,
        "url": url, "frames": [url],
        "fit": fit if fit in ("cover", "contain") else "contain",
        "visible": True, "locked": False,
        "meta": {"tag": node.tag, "cssClasses": node.classes, "start": start, "end": end},
        "source": {"type": "html", "tag": node.tag, "classList": node.classes},
        "keyframes": [],
    }
    layer["keyframes"] = keyframes if keyframes is not None else _timing_keyframes(layer, start, end)
    return layer


def _timing_keyframes(layer: dict, start: float, end: float | None) -> list[dict]:
    """Hold hidden until `start`, fade in over FADE_MS, fade out ending at `end`.
    Layers with no data-s/data-e stay visible for the whole composition."""
    base = {k: layer[k] for k in ("x", "y", "w", "h")}
    base["rotation"] = layer.get("rotation", 0)
    op = layer["opacity"]
    d = DURATION_MS

    def kf(t: int, o: float) -> dict:
        return {"time": t, **base, "opacity": o, "easing": "easeInOutSine"}

    if start == 0 and end is None:
        return [kf(0, op), kf(d // 2, op), kf(d, op)]
    t_in = int(start * 1000)
    pts = [(0, 0.01)] if t_in > 0 else []
    pts.append((t_in, 0.01))
    if end is None:
        pts += [(t_in + FADE_MS, op), (max(d, t_in + FADE_MS + 1), op)]
    else:
        t_end = max(int(end * 1000), t_in + 2)
        fade = min(FADE_MS, max(1, (t_end - t_in) // 2))
        pts += [(t_in + fade, op), (t_end - fade, op), (t_end, 0.01)]
    out, last = [], -1
    for t, o in pts:
        if t > last:
            out.append(kf(t, o))
            last = t
    return out


def _skipped(node: Node, rules, stage: Node) -> bool:
    """True if the node or any ancestor below the stage is hidden / a guide overlay."""
    cur = node
    while cur is not None and cur is not stage:
        if any(c in SKIP_CLASSES for c in cur.classes):
            return True
        st = computed(cur, rules)
        if st.get("display") == "none" or st.get("visibility") == "hidden":
            return True
        cur = cur.parent
    return False


def _own_background_url(node: Node, rules) -> str | None:
    style = computed(node, rules)
    for key in ("--img", "background-image", "background"):
        val = resolve_css_var(style.get(key, ""), ROOT_VARS)
        url = extract_url(val)
        if url:
            return url
    return None


def _background_keyframes(static: bool) -> list[dict]:
    d = DURATION_MS
    if static:
        return [{"time": 0, "x": 0, "y": 0, "w": 100, "h": 100, "rotation": 0, "opacity": 1, "easing": "linear"}]
    ease = "easeInOutSine"
    return [
        {"time": 0, "x": -2, "y": -2, "w": 104, "h": 104, "rotation": 0, "opacity": 1, "easing": ease},
        {"time": d // 2, "x": -5, "y": -4, "w": 110, "h": 110, "rotation": 0, "opacity": 1, "easing": ease},
        {"time": d, "x": -2, "y": -2, "w": 104, "h": 104, "rotation": 0, "opacity": 1, "easing": ease},
    ]


def extract_container_shape(node: Node, rules, W: float, H: float, idx: int) -> dict | None:
    style = computed(node, rules)
    bg = style.get("background-color") or style.get("background") or ""
    bg_resolved = resolve_css_var(bg, ROOT_VARS)
    border = style.get("border") or ""
    border_resolved = resolve_css_var(border, ROOT_VARS)
    radius = style.get("border-radius") or ""
    shadow = style.get("box-shadow") or ""

    fill = None
    if bg_resolved and bg_resolved not in ("transparent", "inherit", "none") and not bg_resolved.startswith("url"):
        fill = parse_colour(bg_resolved)
        if not fill and "gradient" in bg_resolved:
            fill = bg_resolved

    stroke = None
    stroke_w = 0.0
    bm = re.search(r"([\d.]+(?:px|cqw|cqh|%))\s+solid\s+([^;]+)", border_resolved)
    if bm:
        stroke_w_pct = length_pct(bm.group(1), "x", W, H) or 0.0
        stroke_w = stroke_w_pct * W / 100
        stroke = parse_colour(bm.group(2).strip())

    rx_px = 0.0
    is_circle = False
    if radius:
        if "50%" in radius:
            is_circle = True
        else:
            rm = _NUM.match(radius.strip())
            if rm:
                rx_pct = length_pct(radius, "x", W, H) or 0.0
                rx_px = rx_pct * W / 100

    shadow_col = None
    shadow_dy = 0.0
    if shadow:
        sm = re.search(r"0\s+([\d.]+(?:px|cqw|cqh|%))\s+([^;]+)", shadow)
        if sm:
            dy_pct = length_pct(sm.group(1), "y", W, H) or 0.0
            shadow_dy = dy_pct * H / 100
            shadow_col = parse_colour(sm.group(2).strip().split()[-1])

    if not fill and not stroke and not is_circle:
        return None

    box = resolve_box(node, rules, W, H)
    if not box or box["w"] <= 0 or box["h"] <= 0:
        return None
    if box["w"] >= 95 and box["h"] >= 95:
        return None

    name = "Shape"
    for cls in node.classes:
        if cls in ("cd", "card"): name = "Card"
        elif cls in ("nb", "badge", "tag", "tg", "pill"): name = "Badge"
        elif cls in ("btn", "button"): name = "Button"
        elif cls in ("hd", "banner", "bk"): name = "Banner"
        elif cls in ("tk", "ticker", "bar"): name = "Bar"
        elif cls in ("tape",): name = "Tape"
        elif cls in ("fr", "fr2", "frame"): name = "Frame"
        elif cls in ("ci", "circle"): name = "Circle"

    start, end = find_timing(node)
    return make_shape_layer(node, rules, W, H, idx, box, name,
                            fill=fill or "transparent", stroke=stroke,
                            stroke_width=stroke_w, rx=rx_px, is_circle=is_circle,
                            shadow_color=shadow_col, shadow_dy=shadow_dy,
                            start=start, end=end)


def extract_stage(stage: Node, rules, root_vars, base_name: str) -> dict:
    stage_style = computed(stage, rules)
    aspect = stage_style.get("aspect-ratio", "9/16")
    W, H = DEFAULT_STAGE_W, DEFAULT_STAGE_H
    m = re.match(r"([\d.]+)\s*/\s*([\d.]+)", aspect)
    if m and float(m.group(1)) > 0 and float(m.group(2)) > 0:
        H = int(round(DEFAULT_STAGE_W * float(m.group(2)) / float(m.group(1))))
    else:
        pw = _NUM.match((stage_style.get("width") or "").strip())
        ph = _NUM.match((stage_style.get("height") or "").strip())
        if pw and ph and (pw.group(2) or "px") == "px" and (ph.group(2) or "px") == "px" and float(pw.group(1)) > 0:
            H = int(round(DEFAULT_STAGE_W * float(ph.group(1)) / float(pw.group(1))))

    warnings: list[str] = []
    bg_url = extract_background(stage, rules, root_vars, stage_style, W, H)
    if not bg_url:
        warnings.append("No background photo found (looked for --img, background-image and <img>).")

    layers: list[dict] = []
    idx = 0
    if bg_url:
        layers.append({
            "id": "html-bg", "type": "image", "url": bg_url, "frames": [bg_url],
            "role": "background", "name": "Background photo",
            "x": -2, "y": -2, "w": 104, "h": 104, "opacity": 1, "z": 0, "rotation": 0,
            "fit": "cover", "visible": True, "locked": False,
            "keyframes": _background_keyframes(False),
        })
        idx += 1

    seen: set[str] = set()
    dropped = 0

    def add(layer: dict | None) -> None:
        nonlocal idx, dropped
        if layer is None:
            return
        key = f"{layer['type']}|{layer['name']}|{layer.get('text', layer.get('url', ''))[:40]}"
        if key in seen:
            dropped += 1
            return
        seen.add(key)
        layers.append(layer)
        idx += 1

    all_nodes = list(walk(stage))
    card_node = next((n for n in all_nodes if "card" in n.classes), None)
    hdr_node = next((n for n in all_nodes if "hdr" in n.classes), None)

    if card_node or hdr_node:
        ftr_node = next((n for n in all_nodes if "ftr" in n.classes), None)
        tt_node = next((n for n in all_nodes if "tt" in n.classes), None)
        rule_node = next((n for n in all_nodes if "rule" in n.classes), None)
        dish_node = next((n for n in all_nodes if "dish" in n.classes), None)
        stp_node = next((n for n in all_nodes if "stp" in n.classes), None)
        bb_nodes = [n for n in all_nodes if "bb" in n.classes]
        cp_nodes = [n for n in all_nodes if "cp" in n.classes]

        # 1. Header gingham banner
        if hdr_node:
            hdr_box = {"x": 0.0, "y": 2.812, "w": 100.0, "h": 8.438}
            add(make_shape_layer(hdr_node, rules, W, H, idx, hdr_box, "Header pattern", role="background", is_gingham=True))

        # 2. Footer gingham banner
        if ftr_node:
            ftr_box = {"x": 0.0, "y": 81.562, "w": 100.0, "h": 18.438}
            add(make_shape_layer(ftr_node, rules, W, H, idx, ftr_box, "Footer pattern", role="background", is_gingham=True))

        # 3. Card frame container
        if card_node:
            card_box = {"x": 5.0, "y": 41.625, "w": 90.0, "h": 38.25}
            card_kfs = [
                {"time": 0, "x": 5.0, "y": 64.688, "w": 90.0, "h": 15.188, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": 7500, "x": 5.0, "y": 64.688, "w": 90.0, "h": 15.188, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": 7800, "x": 5.0, "y": 41.625, "w": 90.0, "h": 38.25, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": DURATION_MS, "x": 5.0, "y": 41.625, "w": 90.0, "h": 38.25, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
            ]
            add(make_shape_layer(card_node, rules, W, H, idx, card_box, "Card frame", role="background",
                                 keyframes=card_kfs, fill="#fff7e8", stroke="#a8202c",
                                 stroke_width=7.56, rx=32.4, shadow_color="rgba(168,32,44,0.33)", shadow_dy=13.0))

        # 4. Title badge & text
        if tt_node:
            tt_box = {"x": 16.0, "y": 3.0, "w": 68.0, "h": 8.0}
            add(make_shape_layer(tt_node, rules, W, H, idx, tt_box, "Title badge", role="foreground",
                                 fill="#fff7e8", stroke="#a8202c", stroke_width=5.4, rx=64.8))
            b_tt = next((c for c in tt_node.children if c.tag == "b"), None)
            small_tt = next((c for c in tt_node.children if c.tag == "small"), None)
            if b_tt:
                add(make_layer(b_tt, rules, W, H, idx, box={"x": 16.0, "y": 3.2, "w": 68.0, "h": 4.2}, align_override="center"))
            if small_tt:
                add(make_layer(small_tt, rules, W, H, idx, box={"x": 16.0, "y": 7.4, "w": 68.0, "h": 2.0}, align_override="center"))

        # 5. Rule badge & text
        if rule_node:
            rule_box = {"x": 9.0, "y": 39.712, "w": 26.0, "h": 3.0}
            rule_kfs = [
                {"time": 0, "x": 9.0, "y": 62.775, "w": 26.0, "h": 3.0, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": 7500, "x": 9.0, "y": 62.775, "w": 26.0, "h": 3.0, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": 7800, "x": 9.0, "y": 39.712, "w": 26.0, "h": 3.0, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
                {"time": DURATION_MS, "x": 9.0, "y": 39.712, "w": 26.0, "h": 3.0, "rotation": 0, "opacity": 1, "easing": "easeInOutSine"},
            ]
            add(make_shape_layer(rule_node, rules, W, H, idx, rule_box, "Rule badge", role="foreground",
                                 keyframes=rule_kfs, fill="#a8202c", rx=43.2))
            add(make_layer(rule_node, rules, W, H, idx, box=rule_box, align_override="center", color_override="#ffffff", keyframes=rule_kfs))

        # 6. Speech bubbles
        for bi, bb_node in enumerate(bb_nodes):
            s, e = find_timing(bb_node)
            txt = _clean_text(bb_node.text)
            st = computed(bb_node, rules)
            lx = length_pct(st.get("left"), "x", W, H) or (50.0 if bi == 0 else 42.0)
            bw = 28.0 if len(txt) < 12 else 36.0
            bb_box = {"x": round(lx, 2), "y": 16.875, "w": bw, "h": 5.2}
            add(make_shape_layer(bb_node, rules, W, H, idx, bb_box, f"Speech bubble {bi+1}", role="foreground",
                                 start=s, end=e, fill="#fff7e8", stroke="#0b1840", stroke_width=7.56,
                                 rx=43.2, shadow_color="rgba(0,0,0,0.25)", shadow_dy=10.8))
            add(make_layer(bb_node, rules, W, H, idx, box=bb_box, align_override="center"))

        # 7. Captions
        for cpi, cp_node in enumerate(cp_nodes):
            b_cp = next((c for c in cp_node.children if c.tag == "b"), None)
            span_cp = next((c for c in cp_node.children if c.tag == "span"), None)
            if b_cp:
                add(make_layer(b_cp, rules, W, H, idx, box={"x": 9.0, "y": 67.5, "w": 82.0, "h": 4.5}))
            if span_cp:
                add(make_layer(span_cp, rules, W, H, idx, box={"x": 9.0, "y": 72.8, "w": 82.0, "h": 3.0}))

        # 8. Dish details
        if dish_node:
            for child in dish_node.children:
                if child.tag == "i":
                    add(make_layer(child, rules, W, H, idx, box={"x": 9.0, "y": 44.438, "w": 82.0, "h": 2.2}, color_override="#a8202c"))
                elif child.tag == "b":
                    add(make_layer(child, rules, W, H, idx, box={"x": 9.0, "y": 47.0, "w": 82.0, "h": 5.8}))
                elif child.tag == "p":
                    add(make_layer(child, rules, W, H, idx, box={"x": 9.0, "y": 53.6, "w": 82.0, "h": 8.0}))
                elif child.tag == "em":
                    s_d, e_d = find_timing(dish_node)
                    em_box = {"x": 9.0, "y": 62.6, "w": 22.0, "h": 4.0}
                    add(make_shape_layer(child, rules, W, H, idx, em_box, "Price badge", role="foreground",
                                         start=s_d, end=e_d, fill="#a8202c", rx=64.8))
                    add(make_layer(child, rules, W, H, idx, box=em_box, align_override="center", color_override="#ffffff"))
                elif child.tag == "small":
                    add(make_layer(child, rules, W, H, idx, box={"x": 9.0, "y": 67.5, "w": 82.0, "h": 3.8}))
                elif child.tag == "u":
                    add(make_layer(child, rules, W, H, idx, box={"x": 9.0, "y": 72.5, "w": 82.0, "h": 3.0}))

        # 9. Stamp
        if stp_node:
            s_stp, e_stp = find_timing(stp_node)
            stp_box = {"x": 69.0, "y": 31.5, "w": 25.0, "h": 14.062}
            add(make_shape_layer(stp_node, rules, W, H, idx, stp_box, "Stamp badge", role="foreground",
                                 start=s_stp, end=e_stp, rotation=-12.0, fill="#fff7e8ee",
                                 stroke="#a8202c", stroke_width=10.8, is_circle=True))
            add(make_layer(stp_node, rules, W, H, idx, box=stp_box, rotation_override=-12.0, align_override="center", color_override="#a8202c"))

        # 10. Footer text
        if ftr_node:
            ftr_txt_box = {"x": 0.0, "y": 89.0, "w": 100.0, "h": 3.0}
            add(make_layer(ftr_node, rules, W, H, idx, box=ftr_txt_box, align_override="center"))
    else:
        done = set()
        for node in walk(stage):
            if node is stage or id(node) in done or _skipped(node, rules, stage):
                continue

            if node.tag == "img" and node.attrs.get("src"):
                if node.attrs["src"] != bg_url:
                    nm = (node.attrs.get("alt") or "Image").strip()[:30] or "Image"
                    layer = make_image_layer(node, rules, W, H, idx, node.attrs["src"], nm)
                    if layer is None:
                        warnings.append(f"<img> {node.attrs['src'][:40]!r} has no resolvable position; skipped.")
                    add(layer)
                continue

            own_bg = _own_background_url(node, rules)
            if own_bg and own_bg != bg_url:
                nm = next((CLASS_HINTS[c][1] for c in node.classes if c in CLASS_HINTS), "Image")
                add(make_image_layer(node, rules, W, H, idx, own_bg, nm))
                continue

            shape = extract_container_shape(node, rules, W, H, idx)
            if shape is not None:
                add(shape)

            pbox = resolve_box(node, rules, W, H) or {"x": 0, "y": 0, "w": 100, "h": 100}
            for child, cb in stack_children(node, rules, W, H, pbox):
                if not child.text.strip() or _skipped(child, rules, stage):
                    continue
                add(make_layer(child, rules, W, H, idx, box=cb))
                done.add(id(child))

            if node.text and node.text.strip():
                add(make_layer(node, rules, W, H, idx))

    text_layers = [l for l in layers if l["type"] == "text"]
    est = sum(1 for l in text_layers if l["meta"].get("estimatedBox"))
    if est:
        warnings.append(f"{est} of {len(text_layers)} text layer(s) have no explicit CSS box; "
                        "their position/height is estimated and may need nudging in Studio.")
    if not text_layers:
        warnings.append("No visible text found in this stage.")
    if dropped:
        warnings.append(f"{dropped} duplicate layer(s) were merged.")

    slug_base = re.sub(r"[^a-zA-Z0-9_-]+", "-", base_name).strip("-").lower() or "stage"
    return {
        "slug": f"{slug_base}-stage",
        "name": base_name,
        "width": W, "height": H,
        "layers": layers,
        "background": bg_url,
        "warnings": warnings,
        "metadata": {
            "aspectRatio": aspect,
            "backgroundResolved": bool(bg_url),
            "textLayers": len(text_layers),
            "imageLayers": sum(1 for l in layers if l["type"] == "image"),
            "stageClasses": stage.classes,
        },
    }


# ── project writing ──────────────────────────────────────────────────────

def decode_data_url(url: str) -> tuple[str, bytes] | None:
    m = re.match(r"data:([\w.+-]+/[\w.+-]+)?((?:;[^,;]*)*),(.*)$", url, re.S)
    if not m or not (m.group(1) or "").lower().startswith("image/"):
        return None
    mime, params, payload = m.group(1).lower(), m.group(2) or "", m.group(3)
    try:
        if ";base64" in params:
            payload = urllib.parse.unquote(payload)
            data = base64.b64decode(payload + "=" * (-len(payload) % 4))
        else:
            data = urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return None
    ext = {"image/jpeg": "jpg", "image/svg+xml": "svg", "image/x-icon": "ico"}.get(mime, mime.split("/", 1)[1])
    return ext, data


def _is_public_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def materialise_image(url: str, base_dir: Path, images_dir: Path, stem: str,
                      slug: str, warnings: list[str]) -> str | None:
    """Copy/decode/download an image into the project and return its /data/projects URL.
    Returns the original URL (with a warning) when it cannot be brought local."""
    if not url or url.startswith("/data/projects/"):
        return url or None
    if url.startswith("data:"):
        dec = decode_data_url(url)
        if not dec:
            warnings.append(f"{stem}: unreadable data: URL.")
            return None
        ext, data = dec
    elif re.match(r"https?://", url, re.I):
        host = urllib.parse.urlsplit(url).hostname or ""
        if not FETCH_REMOTE:
            warnings.append(f"{stem}: remote image kept as a link (enable 'Download remote images' to bundle it).")
            return url
        if not _is_public_host(host):
            warnings.append(f"{stem}: refused to fetch non-public host {host!r}; kept as a link.")
            return url
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "html-to-projects/2"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(40 * 1024 * 1024)
                ctype = resp.headers.get_content_type()
        except Exception as exc:
            warnings.append(f"{stem}: download failed ({exc}); kept as a link.")
            return url
        ext = Path(urllib.parse.urlsplit(url).path).suffix.lstrip(".").lower() or \
            (ctype.split("/")[-1] if ctype.startswith("image/") else "png")
    else:
        local = urllib.parse.unquote(url.split("?", 1)[0].split("#", 1)[0])
        p = Path(local[7:] if local.startswith("file://") else local)
        if not p.is_absolute():
            p = base_dir / p
        if not p.is_file():
            warnings.append(f"{stem}: file {url[:60]!r} not found next to the HTML; "
                            "embed it as a data: URL or use an absolute https URL.")
            return url
        data, ext = p.read_bytes(), (p.suffix.lstrip(".").lower() or "png")
    ext = re.sub(r"[^a-z0-9]", "", ext.replace("jpeg", "jpg")) or "png"
    filename = f"{stem}.{ext}"
    (images_dir / filename).write_bytes(data)
    return f"/data/projects/{slug}/images/{filename}"


def segment_background(bg_path: Path, slug: str, images_dir: Path, W: int, H: int,
                       sam_model: str, scene: dict | None, vision_model: str | None,
                       image_to_layers: Path, warnings: list[str]) -> list[dict]:
    """Run image-to-layers --sam-auto on the background and return cut-out layers,
    positioned in stage coordinates (accounting for object-fit: cover)."""
    if Image is None:
        warnings.append("Segmentation skipped: Pillow is not installed.")
        return []
    try:
        with Image.open(bg_path) as im:
            iw, ih = im.size
    except Exception as exc:
        warnings.append(f"Segmentation skipped: background is not a raster image ({exc}).")
        return []
    scale = max(W / iw, H / ih)
    dw, dh = iw * scale, ih * scale
    ox, oy = (W - dw) / 2, (H - dh) / 2

    seg_dir = Path(tempfile.mkdtemp(prefix="itl-html-seg-"))
    inserted: list[dict] = []
    try:
        argv = [sys.executable, str(image_to_layers), "--reference", str(bg_path),
                "--sam-auto", "--sam-model", sam_model, "--name", f"{slug}-background",
                "--out", str(seg_dir), "--feather", "1", "--dilate", "0", "--mode", "project"]
        if scene:
            scene_path = seg_dir / "scene.json"
            scene_path.write_text(json.dumps(scene), encoding="utf-8")
            argv += ["--scene-json", str(scene_path)]
        if vision_model:
            argv += ["--vision-model", vision_model]
        say("progress-note segmenting background (this can take a while)…")
        subprocess.run(argv, check=True, capture_output=True, text=True, timeout=SEG_TIMEOUT_S)
        comps = list(seg_dir.glob("*/compositions/*.composition.json"))
        if not comps:
            warnings.append("Segmentation produced no composition.")
            return []
        sub = json.loads(comps[0].read_text(encoding="utf-8"))
        for layer in sub.get("layers", []):
            if layer.get("id") == sub.get("referenceLayerId") or not layer.get("url"):
                continue
            # layer URLs look like /data/projects/<project>/images/<file>
            rel = layer["url"]
            rel = rel[len("/data/projects/"):] if rel.startswith("/data/projects/") else rel.lstrip("/")
            src_file = seg_dir / rel
            if not src_file.is_file():
                src_file = comps[0].parent.parent / rel.split("/", 1)[-1]
            if not src_file.is_file():
                continue
            name = f"seg-{layer['id']}.png"
            shutil.copy2(src_file, images_dir / name)
            new = dict(layer)
            new.update({
                "id": f"seg-{layer['id']}",
                "url": f"/data/projects/{slug}/images/{name}",
                "x": round((ox + layer["x"] / 100 * dw) / W * 100, 4),
                "y": round((oy + layer["y"] / 100 * dh) / H * 100, 4),
                "w": round(layer["w"] / 100 * dw / W * 100, 4),
                "h": round(layer["h"] / 100 * dh / H * 100, 4),
            })
            new["frames"] = [new["url"]]
            new.setdefault("type", "image")
            inserted.append(new)
        if not inserted:
            warnings.append("Segmentation ran but no cut-out files could be located.")
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "").strip().splitlines()[-3:]
        warnings.append(f"Background segmentation failed (exit {exc.returncode}): {' | '.join(tail)}")
    except subprocess.TimeoutExpired:
        warnings.append(f"Background segmentation timed out after {SEG_TIMEOUT_S}s.")
    finally:
        shutil.rmtree(seg_dir, ignore_errors=True)
    return inserted


def _unique_slug(root: Path, slug: str, overwrite: bool) -> str:
    if overwrite or not (root / slug).exists():
        return slug
    n = 2
    while (root / f"{slug}-{n}").exists():
        n += 1
    return f"{slug}-{n}"


def write_project(out_root: Path, stage: dict, index: int,
                  source_name: str, segment: bool, sam_model: str,
                  scene: dict | None, vision_model: str | None,
                  image_to_layers: Path | None, base_dir: Path | None = None,
                  overwrite: bool = False) -> Path:
    base_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", source_name).strip("-").lower() or "page"
    slug = _unique_slug(out_root, f"{base_slug}-{index + 1:02d}", overwrite)
    project_dir = out_root / slug
    images_dir = project_dir / "images"
    comp_dir = project_dir / "compositions"
    images_dir.mkdir(parents=True, exist_ok=True)
    comp_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = stage.setdefault("warnings", [])
    base_dir = base_dir or Path.cwd()
    layers: list[dict] = stage["layers"]

    # bring every image into the project (data: URLs, local files, optionally remote)
    for layer in layers:
        if layer.get("type") == "image":
            new = materialise_image(layer.get("url", ""), base_dir, images_dir, layer["id"], slug, warnings)
            if new:
                layer["url"], layer["frames"] = new, [new]

    bg = next((l for l in layers if l["id"] == "html-bg"), None)
    if segment:
        if not bg:
            warnings.append("Segmentation skipped: no background photo.")
        elif not image_to_layers:
            warnings.append("Segmentation skipped: image-to-layers.py not found next to this script.")
        elif not bg["url"].startswith("/data/projects/"):
            warnings.append("Segmentation skipped: the background could not be saved locally.")
        else:
            inserted = segment_background(
                out_root / bg["url"][len("/data/projects/"):], slug, images_dir,
                stage["width"], stage["height"], sam_model, scene, vision_model,
                image_to_layers, warnings)
            if inserted:
                # cut-outs sit at stage coordinates, so the backdrop must not drift
                bg.update({"x": 0, "y": 0, "w": 100, "h": 100, "keyframes": _background_keyframes(True)})
                layers = [bg] + inserted + [l for l in layers if l is not bg]
    for i, layer in enumerate(layers):
        layer["z"] = i
    stage["layers"] = layers

    composition = {
        "version": 1,
        "id": slug,
        "name": stage["name"],
        "width": stage["width"],
        "height": stage["height"],
        "duration": DURATION_MS,
        "defaultLocale": "en",
        "activeLocale": None,
        "referenceLayerId": None,
        "characters": [],
        "formats": [{
            "id": "master", "isMaster": True, "channel": "social",
            "label": "Master", "width": stage["width"], "height": stage["height"],
            "layerOverrides": {}, "variableValues": {},
        }],
        "activeFormatId": "master",
        "variables": [],
        "brandKit": None,
        "brandKitId": None,
        "guardrails": {"enabled": False, "block": False},
        "showSafeZones": True,
        "revision": 0,
        "sourceFacts": {
            "htmlSource": source_name,
            "stageMetadata": stage.get("metadata", {}),
            "layerCount": len(layers),
            "warnings": warnings,
            "kind": "html-stage",
        },
        "layers": layers,
    }
    (comp_dir / f"{slug}.composition.json").write_text(json.dumps(composition, indent=2), encoding="utf-8")

    manifest = {
        "name": stage["name"], "width": stage["width"], "height": stage["height"],
        "layers": [{
            "name": l.get("name", ""),
            "file": l.get("url", "").replace(f"/data/projects/{slug}/", ""),
            "role": l.get("role", ""),
            "z": l.get("z", 0),
        } for l in layers],
    }
    (project_dir / "layers.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for w in warnings:
        say(f"warn: {w}")
    return project_dir


# ── main ─────────────────────────────────────────────────────────────────

def _stage_preview(i: int, s: dict) -> dict:
    return {
        "index": i, "name": s["name"], "width": s["width"], "height": s["height"],
        "background": s["background"], "metadata": s.get("metadata", {}),
        "warnings": s.get("warnings", []), "duration": DURATION_MS,
        "layers": [{
            "id": l["id"],
            "type": l["type"], "name": l["name"], "text": l.get("text", ""),
            "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"], "role": l["role"],
            "url": l.get("url") if l["type"] == "image" and l["id"] != "html-bg" else None,
            "font": l.get("font"), "color": l.get("color"), "align": l.get("align"),
            "rotation": l.get("rotation", 0), "opacity": l.get("opacity", 1),
            "meta": l.get("meta"), "source": l.get("source"),
            "keyframes": l.get("keyframes"),
        } for l in s["layers"] if l["id"] != "html-bg"],
    }


def main(argv: list[str] | None = None) -> int:
    global DURATION_MS, ROOT_VARS, FETCH_REMOTE, PREVIEW
    p = argparse.ArgumentParser()
    p.add_argument("--html", required=True, help="Path to the HTML page")
    p.add_argument("--out", default="data/projects")
    p.add_argument("--name", default="")
    p.add_argument("--max-stages", type=int, default=3)
    p.add_argument("--duration-ms", type=int, default=DEFAULT_DURATION_MS)
    p.add_argument("--preview-only", action="store_true")
    p.add_argument("--segment-background", action="store_true")
    p.add_argument("--sam-model", default="sam2-base")
    p.add_argument("--scene-json", type=Path)
    p.add_argument("--vision-model", default=None)
    p.add_argument("--fetch-remote", action="store_true",
                   help="Download http(s) images into the project (public hosts only)")
    p.add_argument("--overwrite", action="store_true",
                   help="Reuse an existing project folder instead of numbering a new one")
    p.add_argument("--html-tool", default="image-to-layers.py",
                   help="Path to image-to-layers.py (defaults to sibling)")
    args = p.parse_args(argv)

    PREVIEW = args.preview_only
    FETCH_REMOTE = args.fetch_remote
    DURATION_MS = max(1000, args.duration_ms)

    html_path = Path(args.html)
    if not html_path.is_file():
        sys.exit(f"html not found: {html_path}")
    html = html_path.read_text(encoding="utf-8", errors="replace")

    walker = Walker()
    walker.feed(html)
    walker.close()
    css = "\n".join(walker._css_chunks)
    ROOT_VARS = extract_root_vars(re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    rules = split_rules(css)
    _COMPUTED_CACHE.clear()

    page_name = args.name
    if not page_name:
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        page_name = title_m.group(1).strip() if title_m else html_path.stem

    stages_nodes = walker.stages[: args.max_stages]
    if not stages_nodes:
        say("warn: no `<div class=\"stage\">` found — falling back to elements with an aspect-ratio")
        for node in walk(walker.root):
            style = computed(node, rules)
            if style.get("aspect-ratio") and "stage" not in node.classes:
                stages_nodes.append(node)
                if len(stages_nodes) >= args.max_stages:
                    break
    if not stages_nodes:
        sys.exit("no stage elements found in the HTML")

    # stretch the composition if any data-e runs past the requested duration
    latest = 0.0
    for node in walk(walker.root):
        for key in ("data-s", "data-e"):
            try:
                latest = max(latest, float(node.attrs.get(key) or 0))
            except ValueError:
                pass
    if latest * 1000 > DURATION_MS:
        DURATION_MS = int(math.ceil(latest * 1000))
        say(f"warn: data-e reaches {latest:g}s; duration extended to {DURATION_MS} ms")

    scene = None
    if args.scene_json and args.scene_json.is_file():
        try:
            scene = json.loads(args.scene_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            say("warn: --scene-json is not valid JSON; ignored")

    stages = [extract_stage(node, rules, ROOT_VARS, f"{page_name} — {i + 1:02d}")
              for i, node in enumerate(stages_nodes)]

    if args.preview_only:
        print(json.dumps({"stages": [_stage_preview(i, s) for i, s in enumerate(stages)]}))
        return 0

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    img_tool = Path(args.html_tool)
    if not img_tool.is_absolute():
        img_tool = Path(__file__).resolve().parent / img_tool
    if not img_tool.is_file():
        img_tool = None

    n = len(stages)
    for i, stage in enumerate(stages):
        say(f"progress {int(i / n * 100)} Writing stage {i + 1}/{n}")
        project_dir = write_project(
            out_root, stage, i, page_name,
            segment=args.segment_background, sam_model=args.sam_model, scene=scene,
            vision_model=args.vision_model, image_to_layers=img_tool,
            base_dir=html_path.resolve().parent, overwrite=args.overwrite,
        )
        say(f"  stage {i + 1}: {len(stage['layers'])} layers, {stage['width']}×{stage['height']}")
        say(f"project {project_dir}")
    say(f"progress 100 Done")
    say(f"done: {n} project(s) under {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())