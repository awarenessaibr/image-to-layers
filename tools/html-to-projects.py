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
SKIP_CLASSES = {
    "bar", "safe", "fade", "itl-tooltip",
    # r1/r2 reel animation overlays – not meaningful as static layers
    "vg",   # vignette gradient overlay
    "sp",   # spotlight gradient overlay
    "bgb",  # blurred background duplicate
    "mr",   # mini-map animated viewport rect
    "rg",   # spotlight animated ring (calc-based position)
}
CLASS_HINTS = {
    "hdr": ("background", "Header"),
    "win": ("background", "Photo window"),
    "cam": ("background", "Photo"),
    "card": ("foreground", "Card"),
    "rule": ("foreground", "Rule badge"),
    "cp": ("foreground", "Caption"),
    "dish": ("foreground", "Dish"),
    "bb": ("foreground", "Speech bubble"),
    "stp": ("foreground", "Stamp"),
    "ftr": ("foreground", "Footer"),
    "tt": ("foreground", "Title"),
    # r1 / r2 stage elements
    "mm": ("foreground", "Mini-map"),
    "hud": ("foreground", "HUD"),
    "nb": ("foreground", "Badge"),
    "cd": ("foreground", "Card"),
    "out": ("foreground", "Outro"),
    "bd": ("foreground", "Board"),
    "hh": ("foreground", "Board header"),
    "rw": ("foreground", "Board row"),
    "end": ("foreground", "End card"),
    "pn": ("foreground", "Pin"),
}
TAG_HINTS = {
    "b": "Headline",
    "strong": "Subhead",
    "h2": "Title",
    "h3": "Title",
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
    if m:
        return m.group(1).strip()
    m = re.match(r"(data:image/[^\s;]+(?:;[^,\s]*)*,[^\s]+)", value.strip())
    if m:
        return m.group(1)
    return None


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
    timed = "data-s" in node.attrs or "data-e" in node.attrs
    for selector, decl in rules:
        if selector_matches(selector, node):
            style.update(decl)
        elif timed and selector.endswith(".on") and selector_matches(selector[:-3].rstrip(), node):
            # the page reveals timed elements by adding `.on`; that is the state
            # the viewer sees, so it wins over the entry-state rule
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
    w = length_pct(style.get("width") or style.get("min-width"), "x", W, H)
    right_inset = None
    if style.get("right"):
        r = length_pct(style["right"], "x", W, H)
        if r is not None:
            right_inset = r
            if x is None and w is not None:
                # right + width known → derive x
                x = 100 - w - r
            elif x is None:
                # only right known, no left/width → x from right edge (width added later)
                x = 100 - r
            elif w is None:
                # left + right → derive width
                w = 100 - x - r

    y = length_pct(style.get("top"), "y", W, H)
    bottom_inset = None
    if style.get("bottom"):
        b = length_pct(style["bottom"], "y", W, H)
        if b is not None:
            bottom_inset = b
            if y is None:
                y = 100 - b  # corrected once the height is known
    h = length_pct(style.get("height") or style.get("min-height"), "y", W, H)
    if h is None and y is not None and bottom_inset is not None and style.get("top"):
        h = 100 - y - bottom_inset

    ar = _aspect_ratio_val(style.get("aspect-ratio"))
    if h is None and w is not None and ar and H:
        h = (w / 100 * W / ar) / H * 100
    elif w is None and h is not None and ar and W:
        w = (h / 100 * H * ar) / W * 100

    if h is not None and bottom_inset is not None and not style.get("top"):
        y = 100 - bottom_inset - h
    if w is not None and right_inset is not None and not style.get("left"):
        x = 100 - right_inset - w
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


def _estimate_container_height(node: Node, rules, W: float, H: float, box_w: float) -> float:
    """Estimate the visual height of a styled container from padding + stacked children.
    Used when the container has no explicit height in CSS but has visible content."""
    style = computed(node, rules)
    # Extract top+bottom padding
    pad_top = pad_bot = 0.0
    padding = (style.get("padding") or "").strip()
    if padding:
        parts = padding.split()
        pt = length_pct(parts[0], "y", W, H) or 0.0
        pb = length_pct(parts[2] if len(parts) > 2 else parts[0], "y", W, H) or 0.0
        pad_top, pad_bot = pt, pb
    content_h = 0.0
    for child in node.children:
        cstyle = computed(child, rules)
        if cstyle.get("position") == "absolute" or cstyle.get("display") == "none":
            continue
        _, _, _, ch = _partial_box(cstyle, W, H, fill_h=False)
        if ch is None:
            if child.children and not child.text.strip():
                # Recurse into childless-text containers (e.g. <ol>, <ul>)
                ch = _estimate_container_height(child, rules, W, H, box_w)
            else:
                ch = estimate_height(cstyle, W, H, child.text, box_w)
        content_h += ch
    return max(pad_top + content_h + pad_bot, 2.0)


def resolve_box(node: Node, rules, W: float, H: float, extra: dict | None = None) -> dict | None:
    """Absolute % box. Explicit left/top/width/height win per-axis; anything
    missing is inherited from the parent box / estimated from stacked siblings."""
    style = {**(extra or {}), **computed(node, rules)}
    x, y, w, h = _partial_box(style, W, H, fill_h=False)
    own_xy = x is not None and y is not None
    is_estimated = False
    if None in (x, y, w, h):
        # For white-space:nowrap elements, estimate width from text content before
        # falling back to the parent's width (which is usually far too wide).
        if w is None and ("nowrap" in (style.get("white-space") or "") or style.get("right")):
            fs_x = length_pct(style.get("font-size", ""), "x", W, H) or 3.0
            pad = (style.get("padding") or "").strip().split()
            pad_x = 0.0
            if pad:
                pr = length_pct(pad[1] if len(pad) > 1 else pad[0], "x", W, H) or 0.0
                pl = length_pct(pad[3] if len(pad) > 3 else (pad[1] if len(pad) > 1 else pad[0]), "x", W, H) or 0.0
                pad_x = pr + pl
            txt = node.text.strip()
            if txt:
                w = min(len(txt) * fs_x * 0.65 + pad_x + 2.0, 50.0)
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

    def positioned_parent(node: Node) -> Node:
        cur = node.parent
        while cur is not None and cur is not stage:
            if computed(cur, rules).get("position") in ("absolute", "relative", "fixed"):
                return cur
            cur = cur.parent
        return stage

    for node in walk(stage):
        if node is stage:
            continue
        style = computed(node, rules)
        for key in ("--img", "background-image", "background"):
            val = style.get(key, "")
            if val:
                url = extract_url(resolve_css_var(val, scope))
                if url:
                    # the photo has to be positioned against the stage itself;
                    # a photo inside a window (e.g. .ph .im) is a layer, not
                    # the stage background
                    if positioned_parent(node) is not stage:
                        continue
                    x, y, w, h = _partial_box(style, W, H, fill_h=False)
                    if (w is None or w >= 70) and (h is None or h >= 70):
                        return url

    for node in walk(stage):
        if node.tag == "img" and node.attrs.get("src"):
            if positioned_parent(node) is stage:
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
    op = layer.get("opacity", 1.0)
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

    # Re-estimate height for containers that have no explicit height in CSS
    # but DO have child content (e.g. .out outro card, .bd departures board)
    if box["h"] <= 2.0 and node.children and fill:
        h_est = _estimate_container_height(node, rules, W, H, box["w"])
        if h_est > box["h"]:
            box = {**box, "h": round(h_est, 3)}

    name = "Shape"
    for cls in node.classes:
        if cls in ("cd", "card"): name = "Card"
        elif cls in ("nb", "badge", "tag", "tg", "pill", "pn"): name = "Badge"
        elif cls in ("btn", "button"): name = "Button"
        elif cls in ("hd", "banner", "bk"): name = "Banner"
        elif cls in ("tk", "ticker", "bar"): name = "Bar"
        elif cls in ("tape",): name = "Tape"
        elif cls in ("fr", "fr2", "frame"): name = "Frame"
        elif cls in ("ci", "circle"): name = "Circle"
        elif cls in ("bd", "board"): name = "Board"
        elif cls in ("out", "outro"): name = "Outro"
        elif cls in ("end",): name = "End card"
        elif cls in ("mm",): name = "Mini-map"

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

    stage_dur = stage.attrs.get("data-d")
    stage_duration_ms = int(stage_dur) if (stage_dur and stage_dur.isdigit()) else DURATION_MS

    seen: set[str] = set()
    dropped = 0

    def add(layer: dict | None) -> None:
        nonlocal idx, dropped
        if layer is None:
            return
        meta = layer.get("meta", {})
        start = meta.get("start", 0)
        end = meta.get("end")
        timing_key = f"{start:.2f}-{end:.2f}" if end is not None else f"{start:.2f}-inf"
        pos_key = f"{layer.get('x', 0):.1f},{layer.get('y', 0):.1f}"
        content_key = layer.get("text", "") or layer.get("id", "") or layer.get("name", "")
        key = f"{layer['type']}|{layer['name']}|{timing_key}|{pos_key}|{content_key}"
        if key in seen:
            dropped += 1
            return
        seen.add(key)
        layers.append(layer)
        idx += 1

    all_nodes = list(walk(stage))
    card_node = next((n for n in all_nodes if "card" in n.classes), None)
    hdr_node = next((n for n in all_nodes if "hdr" in n.classes), None)
    r1_node = any("cam" in n.classes for n in all_nodes) and any("cd" in n.classes for n in all_nodes)
    r2_node = any("bd" in n.classes for n in all_nodes) and any("wr" in n.classes for n in all_nodes)

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
    elif r1_node:
        # Spotlight Tour template (stage r1)
        # 1. Spotlight animated ring
        rg_node = next((n for n in all_nodes if "rg" in n.classes), None)
        if rg_node:
            rg_kfs = [
                {"time": 0, "x": 37.0, "y": 20.812, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 3000, "x": 37.0, "y": 20.812, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 3800, "x": 37.0, "y": 34.875, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 5400, "x": 37.0, "y": 34.875, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 6200, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 7800, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 8600, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 10000, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 10300, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 0.01, "easing": "easeInOutSine"},
                {"time": stage_duration_ms, "x": 37.0, "y": 43.312, "w": 26.0, "h": 14.625, "rotation": 0, "opacity": 0.01, "easing": "easeInOutSine"},
            ]
            add(make_shape_layer(rg_node, rules, W, H, idx, {"x": 37.0, "y": 20.812, "w": 26.0, "h": 14.625},
                                 "Spotlight ring", role="foreground", is_circle=True, stroke="#ffffff",
                                 stroke_width=9.72, shadow_color="rgba(255,255,255,0.53)", shadow_dy=1.0,
                                 keyframes=rg_kfs))

        # 2. Minimap thumbnail
        mm_node = next((n for n in all_nodes if "mm" in n.classes), None)
        if mm_node and bg_url:
            mm_box = {"x": 65.0, "y": 9.562, "w": 30.0, "h": 12.602}
            add(make_shape_layer(mm_node, rules, W, H, idx, mm_box, "Minimap frame", role="foreground",
                                 rx=16.2, stroke="#ffffff", stroke_width=6.48, fill="transparent",
                                 shadow_color="rgba(0,0,0,0.53)", shadow_dy=4.0))
            add({
                "id": f"html-{idx:02d}-minimap-photo", "type": "image", "name": "Minimap photo",
                "role": "foreground", "z": idx, **mm_box, "rotation": 0, "opacity": 1.0,
                "url": bg_url, "frames": [bg_url], "fit": "cover", "visible": True, "locked": False,
                "meta": {"tag": "div", "cssClasses": ["mm"]}, "source": {"type": "html", "tag": "div"},
                "keyframes": [{"time": 0, **mm_box, "rotation": 0, "opacity": 1.0, "easing": "linear"}],
            })
            md_nodes = [n for n in all_nodes if "md" in n.classes]
            for mdi, md_node in enumerate(md_nodes):
                md_st = _parse_declarations(md_node.style)
                lp = float(md_st.get("left", "0").rstrip("%")) / 100.0
                tp = float(md_st.get("top", "0").rstrip("%")) / 100.0
                dot_x = mm_box["x"] + mm_box["w"] * lp - 1.2
                dot_y = mm_box["y"] + mm_box["h"] * tp - 0.675
                dot_box = {"x": round(dot_x, 3), "y": round(dot_y, 3), "w": 2.4, "h": 1.35}
                add(make_shape_layer(md_node, rules, W, H, idx, dot_box, f"Map dot {mdi+1}", role="foreground",
                                     is_circle=True, fill="#ffffff", stroke="#0b1840", stroke_width=4.32))

        # 3. HUD Header
        hud_node = next((n for n in all_nodes if "hud" in n.classes), None)
        if hud_node:
            hud_span = next((c for c in hud_node.children if c.tag == "span" and "ct" not in c.classes), None)
            hud_ct = next((c for c in hud_node.children if "ct" in c.classes), None)
            if hud_span:
                add(make_layer(hud_span, rules, W, H, idx, box={"x": 5.0, "y": 4.5, "w": 75.0, "h": 2.5},
                               name_override="HUD Tour title", color_override="#ffffff"))
            if hud_ct:
                add(make_layer(hud_ct, rules, W, H, idx, box={"x": 80.0, "y": 4.5, "w": 15.0, "h": 2.5},
                               name_override="HUD Counter", align_override="right", color_override="#ffffff"))

        # 4. Content cards
        cd_nodes = [n for n in all_nodes if "cd" in n.classes]
        for ci, cd_node in enumerate(cd_nodes):
            s, e = find_timing(cd_node)
            card_box = {"x": 6.0, "y": 60.75, "w": 88.0, "h": 18.5}
            add(make_shape_layer(cd_node, rules, W, H, idx, card_box, f"Card {ci+1} frame",
                                 role="foreground", fill="#fff7e8", rx=32.4, shadow_color="#0b184055",
                                 shadow_dy=13.0, start=s, end=e))
            nb_node = next((c for c in cd_node.children if "nb" in c.classes), None)
            if nb_node:
                nb_box = {"x": 10.0, "y": 63.0, "w": 9.0, "h": 5.062}
                add(make_shape_layer(nb_node, rules, W, H, idx, nb_box, f"Badge {ci+1}", role="foreground",
                                     fill="#e63b2e", is_circle=True, start=s, end=e))
                add(make_layer(nb_node, rules, W, H, idx, box=nb_box, align_override="center",
                               color_override="#ffffff", name_override=f"Badge {ci+1} number"))
            b_dish = next((c for c in cd_node.children if c.tag == "b"), None)
            strong_dish = next((c for c in cd_node.children if c.tag == "strong"), None)
            em_dish = next((c for c in cd_node.children if c.tag == "em"), None)
            small_dish = next((c for c in cd_node.children if c.tag == "small"), None)
            if b_dish:
                add(make_layer(b_dish, rules, W, H, idx, box={"x": 23.0, "y": 62.4, "w": 67.0, "h": 4.5},
                               name_override=f"Card {ci+1} Headline", color_override="#0b1840"))
            if strong_dish:
                add(make_layer(strong_dish, rules, W, H, idx, box={"x": 23.0, "y": 66.8, "w": 67.0, "h": 2.8},
                               name_override=f"Card {ci+1} Item", color_override="#0b1840"))
            if em_dish:
                add(make_layer(em_dish, rules, W, H, idx, box={"x": 23.0, "y": 69.8, "w": 67.0, "h": 2.5},
                               name_override=f"Card {ci+1} Price", color_override="#e63b2e"))
            if small_dish:
                add(make_layer(small_dish, rules, W, H, idx, box={"x": 23.0, "y": 72.5, "w": 67.0, "h": 2.2},
                               name_override=f"Card {ci+1} Small print", color_override="#0b1840"))

        # 5. Outro card
        out_node = next((n for n in all_nodes if "out" in n.classes), None)
        if out_node:
            s_out, e_out = find_timing(out_node)
            out_box = {"x": 6.0, "y": 28.125, "w": 88.0, "h": 36.0}
            add(make_shape_layer(out_node, rules, W, H, idx, out_box, "Outro card frame", role="foreground",
                                 fill="#fff7e8", rx=32.4, shadow_color="#0b184055", shadow_dy=13.0,
                                 start=s_out, end=e_out))
            h3_node = next((c for c in out_node.children if c.tag == "h3"), None)
            if h3_node:
                add(make_layer(h3_node, rules, W, H, idx, box={"x": 10.0, "y": 30.0, "w": 80.0, "h": 5.0},
                               name_override="Outro Title", color_override="#0b1840"))
            ol_node = next((c for c in out_node.children if c.tag == "ol"), None)
            if ol_node:
                for lii, li in enumerate(ol_node.children):
                    if li.tag != "li":
                        continue
                    li_y = 35.5 + lii * 5.2
                    i_badge = next((c for c in li.children if c.tag == "i"), None)
                    span_place = next((c for c in li.children if c.tag == "span"), None)
                    b_item = next((c for c in li.children if c.tag == "b"), None)
                    if i_badge:
                        badge_box = {"x": 11.0, "y": li_y, "w": 6.0, "h": 3.375}
                        add(make_shape_layer(i_badge, rules, W, H, idx, badge_box, f"Outro item {lii+1} badge",
                                             role="foreground", fill="#e63b2e", is_circle=True, start=s_out, end=e_out))
                        add(make_layer(i_badge, rules, W, H, idx, box=badge_box, align_override="center",
                                       color_override="#ffffff", name_override=f"Outro item {lii+1} number"))
                    if span_place:
                        add(make_layer(span_place, rules, W, H, idx, box={"x": 19.0, "y": li_y, "w": 38.0, "h": 3.0},
                                       name_override=f"Outro item {lii+1} Place", color_override="#0b1840"))
                    if b_item:
                        add(make_layer(b_item, rules, W, H, idx, box={"x": 58.0, "y": li_y, "w": 32.0, "h": 3.0},
                                       name_override=f"Outro item {lii+1} Dish", align_override="right", color_override="#0b1840"))
            small_out = next((c for c in out_node.children if c.tag == "small"), None)
            if small_out:
                add(make_layer(small_out, rules, W, H, idx, box={"x": 10.0, "y": 57.5, "w": 80.0, "h": 2.5},
                               align_override="center", name_override="Outro Handle", color_override="#0b1840"))

    elif r2_node:
        # Animated Route template (stage r2)
        # 1. Route SVG line overlay
        route_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 177.8" preserveAspectRatio="none"><path d="M41.0 152.9 C42.5 150.8 46.5 146.1 50.0 140.5 C53.5 134.8 61.0 126.2 62.0 119.1 C63.0 112.0 58.8 104.6 56.0 97.8 C53.2 91.0 48.2 86.2 45.0 78.2 C41.8 70.2 35.3 53.0 37.0 49.8 C38.7 46.5 48.2 55.1 55.0 58.7 C61.8 62.2 71.7 66.4 78.0 71.1 C84.3 75.9 90.5 84.5 93.0 87.1" fill="none" stroke="#0b1840" stroke-opacity=".55" stroke-width="2.2" stroke-linecap="round"/><path d="M41.0 152.9 C42.5 150.8 46.5 146.1 50.0 140.5 C53.5 134.8 61.0 126.2 62.0 119.1 C63.0 112.0 58.8 104.6 56.0 97.8 C53.2 91.0 48.2 86.2 45.0 78.2 C41.8 70.2 35.3 53.0 37.0 49.8 C38.7 46.5 48.2 55.1 55.0 58.7 C61.8 62.2 71.7 66.4 78.0 71.1 C84.3 75.9 90.5 84.5 93.0 87.1" fill="none" stroke="#fff7e8" stroke-width="1.1" stroke-linecap="round"/></svg>'
        route_url = "data:image/svg+xml;utf8," + urllib.parse.quote(route_svg)
        add({
            "id": f"html-{idx:02d}-route-path", "type": "image", "name": "Route path",
            "role": "foreground", "z": idx, "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0,
            "rotation": 0, "opacity": 1.0, "url": route_url, "frames": [route_url],
            "fit": "fill", "visible": True, "locked": False,
            "meta": {"tag": "svg", "cssClasses": ["wr-svg"]}, "source": {"type": "html-shape", "tag": "svg"},
            "keyframes": [{"time": 0, "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0, "rotation": 0, "opacity": 1.0, "easing": "linear"}],
        })

        # 2. Departures board
        bd_node = next((n for n in all_nodes if "bd" in n.classes), None)
        if bd_node:
            bd_box = {"x": 4.0, "y": 3.938, "w": 92.0, "h": 16.5}
            add(make_shape_layer(bd_node, rules, W, H, idx, bd_box, "Departures Board frame", role="foreground",
                                 fill="#0b1840ee", rx=27.0))
            hh_node = next((c for c in bd_node.children if "hh" in c.classes), None)
            if hh_node:
                hh_box = {"x": 4.0, "y": 3.938, "w": 92.0, "h": 3.2}
                add(make_shape_layer(hh_node, rules, W, H, idx, hh_box, "Board Header banner", role="foreground",
                                     fill="#ffd23f", rx=0.0))
                add(make_layer(hh_node, rules, W, H, idx, box=hh_box, align_override="left",
                               color_override="#0b1840", name_override="Board Header text"))
            rw_nodes = [c for c in bd_node.children if "rw" in c.classes]
            for rwi, rw in enumerate(rw_nodes):
                row_y = 7.5 + rwi * 3.1
                b_t = next((c for c in rw.children if c.tag == "b"), None)
                span_t = next((c for c in rw.children if c.tag == "span"), None)
                em_t = next((c for c in rw.children if c.tag == "em"), None)
                if b_t:
                    add(make_layer(b_t, rules, W, H, idx, box={"x": 7.0, "y": row_y, "w": 15.0, "h": 2.4},
                                   name_override=f"Stop {rwi+1} Time", color_override="#ffd23f"))
                if span_t:
                    add(make_layer(span_t, rules, W, H, idx, box={"x": 23.0, "y": row_y, "w": 53.0, "h": 2.4},
                                   name_override=f"Stop {rwi+1} Description", color_override="#f6ecd9"))
                if em_t:
                    add(make_layer(em_t, rules, W, H, idx, box={"x": 77.0, "y": row_y, "w": 15.0, "h": 2.4},
                                   align_override="right", name_override=f"Stop {rwi+1} Price", color_override="#f6ecd9"))

        # 3. 4 Pins
        pn_nodes = [n for n in all_nodes if "pn" in n.classes]
        pn_stops = [
            (37.5, 84.04, 1.6),
            (58.5, 65.04, 4.0),
            (33.5, 26.04, 6.4),
            (89.5, 47.04, 8.6),
        ]
        for pni, pn in enumerate(pn_nodes):
            px, py, pstart = pn_stops[pni] if pni < len(pn_stops) else (50.0, 50.0, 0.0)
            pbox = {"x": px, "y": py, "w": 7.0, "h": 3.938}
            add(make_shape_layer(pn, rules, W, H, idx, pbox, f"Pin {pni+1} badge", role="foreground",
                                 fill="#ffffff", stroke="#e63b2e", stroke_width=8.64, is_circle=True,
                                 start=pstart, end=None))
            add(make_layer(pn, rules, W, H, idx, box=pbox, align_override="center", color_override="#0b1840",
                           name_override=f"Pin {pni+1} number", keyframes=_timing_keyframes(pbox, pstart, None)))

        # 4. 4 Caption Pills
        cp_nodes = [n for n in all_nodes if "cp" in n.classes]
        cp_coords = [
            (46.0, 84.8, 24.0, 1.6),
            (67.0, 65.8, 27.0, 4.0),
            (42.0, 26.8, 16.0, 6.4),
            (62.0, 47.8, 26.0, 8.6),
        ]
        for cpi, cp in enumerate(cp_nodes):
            cx, cy, cw, cstart = cp_coords[cpi] if cpi < len(cp_coords) else (50.0, 50.0, 20.0, 0.0)
            cbox = {"x": cx, "y": cy, "w": cw, "h": 2.4}
            add(make_shape_layer(cp, rules, W, H, idx, cbox, f"Caption {cpi+1} pill", role="foreground",
                                 fill="#0b1840", rx=43.2, start=cstart, end=None))
            add(make_layer(cp, rules, W, H, idx, box=cbox, align_override="center", color_override="#ffffff",
                           name_override=f"Caption {cpi+1} text", keyframes=_timing_keyframes(cbox, cstart, None)))

        # 5. Mover Scooter
        mv_node = next((n for n in all_nodes if "mv" in n.classes), None)
        if mv_node:
            mv_kfs = [
                {"time": 0, "x": 37.5, "y": 82.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 0.01, "easing": "easeInOutSine"},
                {"time": 400, "x": 37.5, "y": 82.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 1600, "x": 37.5, "y": 82.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 2400, "x": 37.5, "y": 82.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 4000, "x": 58.5, "y": 63.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 4900, "x": 58.5, "y": 63.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 6400, "x": 33.5, "y": 24.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 7200, "x": 33.5, "y": 24.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 8600, "x": 89.5, "y": 45.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
                {"time": 10000, "x": 89.5, "y": 45.0, "w": 8.0, "h": 4.5, "rotation": 0, "opacity": 1.0, "easing": "easeInOutSine"},
            ]
            add(make_layer(mv_node, rules, W, H, idx, box={"x": 37.5, "y": 82.0, "w": 8.0, "h": 4.5},
                           align_override="center", name_override="Mover Scooter", keyframes=mv_kfs))

        # 6. End card
        end_node = next((n for n in all_nodes if "end" in n.classes), None)
        if end_node:
            s_end, e_end = find_timing(end_node)
            end_box = {"x": 6.0, "y": 66.375, "w": 88.0, "h": 14.5}
            add(make_shape_layer(end_node, rules, W, H, idx, end_box, "End card frame", role="foreground",
                                 fill="#fff7e8", rx=32.4, shadow_color="#0b184055", shadow_dy=13.0,
                                 start=s_end, end=e_end))
            b_end = next((c for c in end_node.children if c.tag == "b"), None)
            small_end = next((c for c in end_node.children if c.tag == "small"), None)
            if b_end:
                add(make_layer(b_end, rules, W, H, idx, box={"x": 8.0, "y": 67.5, "w": 84.0, "h": 5.5},
                               name_override="End card Headline", align_override="center", color_override="#0b1840"))
            if small_end:
                add(make_layer(small_end, rules, W, H, idx, box={"x": 8.0, "y": 74.0, "w": 84.0, "h": 2.5},
                               name_override="End card Small print", align_override="center", color_override="#0b1840"))
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
        "duration": stage_duration_ms,
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


# ── measured layout backend (headless Chrome) ────────────────────────────
#
# The CSS walk above cannot see layout: shrink-to-fit boxes, bottom/right
# anchoring, padding, content heights, line wrapping, clipping or the `.on`
# resting state. When Chrome is available we let the browser measure the page
# (tools/measure-html-chrome.py) and build layers from those numbers instead.
# The CSS walk stays as a fallback.

MEASURE_SCRIPT = Path(__file__).resolve().with_name("measure-html-chrome.py")
MEASURE_TIMEOUT_S = 180
# UI chrome / cinematic overlays that should never become layers
MEASURE_SKIP = {"safe", "bar", "fade", "scan", "crt", "noise", "grain", "ctl", "itl-tooltip", "bgb"}
# gradient overlays that used to be skipped are now rendered as SVG gradients
_MATRIX_RE = re.compile(r"matrix(?:3d)?\(([^)]+)\)")


def detect_progress_vars(css: str) -> list[str]:
    """Custom properties the page drives with JS (e.g. --p, --u, --s).

    These start at 0 and reach 1 as the reel plays; the resting layout uses 1.
    Only numeric/calc values declared directly on the stage are considered, so
    `--pr: url(...)`-style asset variables are left alone.
    """
    names: set[str] = set()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css, re.S):
        selector, body = m.group(1).strip(), m.group(2)
        if not re.fullmatch(r"\.(?:stage|post|reel|story)[\w.#:-]*", selector):
            continue
        for key, value in _parse_declarations(body).items():
            if not key.startswith("--"):
                continue
            value = value.strip()
            if re.fullmatch(r"-?\d*\.?\d+(?:px|%|em|rem|s|deg)?", value) or value.startswith("calc("):
                names.add(key[2:])
    return sorted(names)


PROGRESS_VARS: list[str] = []


def measure_page(html_path: Path) -> dict | None:
    """Measure a page three times: progress 0, progress 1, and transforms off."""
    if not MEASURE_SCRIPT.is_file():
        say("warn: tools/measure-html-chrome.py not found; using CSS geometry")
        return None
    start_vars = [x for v in PROGRESS_VARS for x in ("--var", f"{v}=0")]
    end_vars = [x for v in PROGRESS_VARS for x in ("--var", f"{v}=1")]
    passes: dict[str, dict] = {}
    for key, extra in (("start", start_vars),
                       ("end", end_vars),
                       ("layout", [*end_vars, "--no-transform"])):
        with tempfile.TemporaryDirectory(prefix="itl-measure-") as td:
            out = Path(td) / "measure.json"
            argv = [sys.executable, str(MEASURE_SCRIPT), "--html", str(html_path),
                    "--state", "on", "--stage-width", str(DEFAULT_STAGE_W),
                    "--width", "1200", "--height", "1000", "--out", str(out), *extra]
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=MEASURE_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                say(f"warn: Chrome measurement failed ({exc}); using CSS geometry")
                return None
            if done.returncode != 0 or not out.is_file():
                tail = " | ".join((done.stderr or done.stdout or "").strip().splitlines()[-2:])
                say(f"warn: Chrome measurement failed ({tail or 'no output'}); using CSS geometry")
                return None
            try:
                passes[key] = json.loads(out.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                say("warn: Chrome measurement returned invalid JSON; using CSS geometry")
                return None
    if not passes["layout"].get("stages"):
        return None
    # One extra pass samples the page's own animation at 16 points in time.
    sample_ms = detect_page_duration_ms(html_path.read_text(encoding="utf-8", errors="replace")) or 10000
    with tempfile.TemporaryDirectory(prefix="itl-samples-") as td:
        out = Path(td) / "samples.json"
        argv = [sys.executable, str(MEASURE_SCRIPT), "--html", str(html_path),
                "--state", "on", "--stage-width", str(DEFAULT_STAGE_W),
                "--width", "1200", "--height", "1000", "--samples", "16",
                "--time", f"{sample_ms / 1000:.3f}",
                "--budget", "12000", "--out", str(out), *end_vars]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=MEASURE_TIMEOUT_S)
            if done.returncode == 0 and out.is_file():
                passes["samples"] = json.loads(out.read_text(encoding="utf-8")).get("samples")
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            passes["samples"] = None
    return passes


def detect_page_duration_ms(html: str) -> int | None:
    """Find the reel length the page itself declares (`const D = 10000` etc.)."""
    patterns = (
        r"\bconst\s+D\s*=\s*(\d{3,6})\b",
        r"\b(?:let|var)\s+D\s*=\s*(\d{3,6})\b",
        r"\bD\s*=\s*(\d{3,6})\s*[,;]",
        r"\bduration\s*[:=]\s*(\d{4,6})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, html)
        if m:
            value = int(m.group(1))
            if 500 <= value <= 600_000:
                return value
    return None


def _z_index(node: dict) -> int:
    try:
        return int(str(node.get("z") or "0").strip())
    except (TypeError, ValueError):
        return 0


def _colour_alpha(value: str | None) -> float:
    if not value:
        return 0.0
    if value.strip().lower() in ("transparent", "none"):
        return 0.0
    parsed = parse_colour(value, default="")
    if not parsed:
        return 0.0
    return 1.0 if len(parsed) == 7 else int(parsed[7:9], 16) / 255.0


def _rect_intersect(a: dict, b: dict) -> dict:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1 = min(a["x"] + a["w"], b["x"] + b["w"])
    y1 = min(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": round(x0, 3), "y": round(y0, 3),
            "w": round(max(0.0, x1 - x0), 3), "h": round(max(0.0, y1 - y0), 3)}


def _clip_cuts(box: dict, clip: dict, tol: float = 0.2) -> bool:
    return (clip["x"] > box["x"] + tol or clip["y"] > box["y"] + tol
            or clip["x"] + clip["w"] < box["x"] + box["w"] - tol
            or clip["y"] + clip["h"] < box["y"] + box["h"] - tol)


def _transform_parts(value: str | None) -> tuple[float, float]:
    """(rotation in degrees, uniform scale) from a computed transform matrix."""
    m = _MATRIX_RE.search(value or "")
    if not m:
        return 0.0, 1.0
    nums = [float(n) for n in re.split(r"[,\s]+", m.group(1).strip()) if n]
    if len(nums) < 4:
        return 0.0, 1.0
    a, b = nums[0], nums[1]
    rotation = math.degrees(math.atan2(b, a))
    scale = math.hypot(a, b)
    if abs(rotation) < 0.05:
        rotation = 0.0
    return round(rotation, 2), (scale if 0.05 < scale < 20 else 1.0)


def _matrix_values(value: str | None) -> tuple[float, ...] | None:
    m = _MATRIX_RE.search(value or "")
    if not m:
        return None
    nums = [float(n) for n in re.split(r"[,\s]+", m.group(1).strip()) if n]
    return tuple(nums) if len(nums) >= 4 else None


def _is_pure_translation(value: str | None) -> bool:
    nums = _matrix_values(value)
    if not nums:
        return False
    a, b, c, d = nums[:4]
    return abs(a - 1) < 0.002 and abs(b) < 0.002 and abs(c) < 0.002 and abs(d - 1) < 0.002


def _stage_area(box: dict) -> float:
    visible = _rect_intersect(box, {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0})
    return visible["w"] * visible["h"]


def _bg_position(value: str | None) -> tuple[float, float]:
    parts = (value or "0% 0%").split()

    def frac(token: str) -> float:
        token = token.strip().lower()
        if token in ("center",):
            return 0.5
        if token in ("left", "top"):
            return 0.0
        if token in ("right", "bottom"):
            return 1.0
        m = _NUM.match(token)
        if not m:
            return 0.5
        n, unit = float(m.group(1)), (m.group(2) or "px").lower()
        return max(0.0, min(1.0, n / 100.0)) if unit == "%" else 0.5

    if len(parts) == 1:
        return frac(parts[0]), 0.5
    if len(parts) >= 2:
        return frac(parts[0]), frac(parts[1])
    return 0.5, 0.5


def _image_bytes(url: str, base_dir: Path) -> bytes | None:
    if not url:
        return None
    if url.startswith("data:"):
        decoded = decode_data_url(url)
        return decoded[1] if decoded else None
    if re.match(r"https?://", url, re.I):
        return None
    local = urllib.parse.unquote(url.split("?", 1)[0].split("#", 1)[0])
    p = Path(local[7:] if local.startswith("file://") else local)
    if not p.is_absolute():
        p = base_dir / p
    return p.read_bytes() if p.is_file() else None


def _image_size(url: str, base_dir: Path) -> tuple[int, int] | None:
    if Image is None:
        return None
    data = _image_bytes(url, base_dir)
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _size_token(token: str, base: float) -> float | None:
    token = token.strip().lower()
    if token in ("auto", "cover", "contain"):
        return None
    m = re.match(r"(-?[\d.]+)\s*(%|px)?$", token)
    if not m:
        return None
    value = float(m.group(1))
    return base * value / 100.0 if (m.group(2) or "%") == "%" else value


def _paint_box(node: dict, url: str, W: int, H: int, base_dir: Path,
               fit_override: str | None = None) -> dict:
    """Where the image actually paints, in % of the stage (background-size aware)."""
    box = node["box"]
    size = _image_size(url, base_dir)
    if not size:
        return dict(box)
    iw, ih = size
    ew, eh = box["w"] / 100.0 * W, box["h"] / 100.0 * H
    style = (fit_override or node.get("bgSize") or "cover").lower()
    if "fill" in style:
        scale = 1.0
        pw, ph = ew, eh
    elif "contain" in style:
        scale = min(ew / iw, eh / ih)
        pw, ph = iw * scale, ih * scale
    elif style.startswith("none") or style.startswith("scale-down"):
        scale = min(1.0, min(ew / iw, eh / ih)) if "scale-down" in style else 1.0
        pw, ph = iw * scale, ih * scale
    elif "cover" not in style and re.search(r"\d", style):
        pw = _size_token(style.split()[0], ew) if style.split() else None
        ph = _size_token(style.split()[1], eh) if len(style.split()) > 1 else None
        if pw is not None and ph is None:
            ph = pw * ih / iw
        elif ph is not None and pw is None:
            pw = ph * iw / ih
        elif pw is None and ph is None:
            pw, ph = ew, eh
    else:
        scale = max(ew / iw, eh / ih)
        pw, ph = iw * scale, ih * scale
    fx, fy = _bg_position(None if fit_override else node.get("bgPos"))
    ox, oy = (ew - pw) * fx, (eh - ph) * fy
    return {"x": round(box["x"] + ox / W * 100, 3), "y": round(box["y"] + oy / H * 100, 3),
            "w": round(pw / W * 100, 3), "h": round(ph / H * 100, 3)}


def _filtered_data_url(url: str, value: str | None, base_dir: Path,
                      pixel_scale: float = 1.0) -> str | None:
    if not value or value == "none" or Image is None:
        return None
    data = _image_bytes(url, base_dir)
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as im:
            out = _apply_css_filter(im.convert("RGB"), value, pixel_scale)
            buf = BytesIO()
            out.save(buf, "PNG")
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _rounded_mask(size: tuple[int, int], radius: list[float] | None):
    """Per-corner rounded rectangle mask (radius in % of the crop box)."""
    if not radius or all(r <= 0.2 for r in radius):
        return None
    from PIL import ImageDraw
    w, h = size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([0, 0, w, h], fill=255)
    corners = (
        (0, 0, 1, 1, radius[0], radius[1]),
        (w, 0, -1, 1, radius[2], radius[3]),
        (w, h, -1, -1, radius[4], radius[5]),
        (0, h, 1, -1, radius[6], radius[7]),
    )
    for cx, cy, sx, sy, rx_pct, ry_pct in corners:
        rx, ry = rx_pct / 100 * w, ry_pct / 100 * h
        if rx <= 0.5 or ry <= 0.5:
            continue
        qx0, qx1 = (cx, cx + rx) if sx > 0 else (cx - rx, cx)
        qy0, qy1 = (cy, cy + ry) if sy > 0 else (cy - ry, cy)
        ex0, ex1 = (cx, cx + 2 * rx) if sx > 0 else (cx - 2 * rx, cx)
        ey0, ey1 = (cy, cy + 2 * ry) if sy > 0 else (cy - 2 * ry, cy)
        draw.rectangle([qx0, qy0, qx1, qy1], fill=0)
        draw.ellipse([ex0, ey0, ex1, ey1], fill=255)
    return mask


def _linear_mask(size: tuple[int, int], value: str | None):
    """A vertical linear-gradient mask (the common `mask-image` fade)."""
    if not value or "linear-gradient" not in value:
        return None
    stops = re.findall(r"(rgba?\([^)]*\)|#[0-9a-fA-F]{3,8})\s+([\d.]+)%", value)
    if len(stops) < 2:
        return None
    try:
        from PIL import Image as PILImage
        w, h = size
        mask = PILImage.new("L", (w, h), 255)
        px = mask.load()
        parsed = []
        for colour, pos in stops:
            alpha = _colour_alpha(colour)
            parsed.append((float(pos) / 100.0, alpha))
        parsed.sort()
        for y in range(h):
            f = y / max(1, h - 1)
            alpha = 1.0
            if f <= parsed[0][0]:
                alpha = parsed[0][1]
            elif f >= parsed[-1][0]:
                alpha = parsed[-1][1]
            else:
                for (p0, a0), (p1, a1) in zip(parsed, parsed[1:]):
                    if p0 <= f <= p1:
                        k = (f - p0) / max(1e-6, p1 - p0)
                        alpha = a0 + (a1 - a0) * k
                        break
            v = int(round(alpha * 255))
            for x in range(w):
                px[x, y] = v
        return mask
    except Exception:
        return None


def _crop_to_data_url(url: str, paint: dict, clip: dict, W: int, H: int,
                      base_dir: Path, filter_value: str | None = None,
                      radius: list[float] | None = None,
                      mask_image: str | None = None) -> str | None:
    """Crop the visible part of a clipped background/object-fit image."""
    if Image is None or paint["w"] <= 0 or paint["h"] <= 0:
        return None
    data = _image_bytes(url, base_dir)
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as im:
            iw, ih = im.size
            sx = (clip["x"] - paint["x"]) / paint["w"] * iw
            sy = (clip["y"] - paint["y"]) / paint["h"] * ih
            sw = clip["w"] / paint["w"] * iw
            sh = clip["h"] / paint["h"] * ih
            sx, sy = max(0.0, sx), max(0.0, sy)
            sw, sh = min(sw, iw - sx), min(sh, ih - sy)
            if sw < 2 or sh < 2:
                return None
            crop = im.convert("RGB").crop((round(sx), round(sy), round(sx + sw), round(sy + sh)))
            crop = _apply_css_filter(crop, filter_value,
                                     pixel_scale=crop.width / max(1.0, clip["w"] / 100 * W))
            buf = BytesIO()
            mask = _rounded_mask(crop.size, radius)
            if mask is None:
                mask = _linear_mask(crop.size, mask_image)
            if mask is not None:
                out = crop.convert("RGBA")
                out.putalpha(mask)
                out.save(buf, "PNG")
            else:
                crop.save(buf, "PNG")
    except Exception:
        return None
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _apply_css_filter(im, value: str | None, pixel_scale: float = 1.0):
    """Apply the CSS filter functions we can reproduce with Pillow."""
    if not value or value == "none" or Image is None:
        return im
    from PIL import ImageEnhance, ImageFilter, ImageOps
    out = im
    for name, arg in re.findall(r"([a-z-]+)\(([^)]*)\)", value):
        try:
            amount = float(re.sub(r"[^0-9.%-]", "", arg) or 1)
        except ValueError:
            continue
        if "%" in arg:
            amount /= 100.0
        if name == "grayscale":
            out = ImageOps.grayscale(out).convert("RGB") if amount >= 0.99 else                 Image.blend(out.convert("RGB"), ImageOps.grayscale(out).convert("RGB"), amount)
        elif name == "sepia":
            gray = ImageOps.grayscale(out).convert("RGB")
            tone = Image.new("RGB", out.size)
            px = gray.load()
            dst = tone.load()
            for y in range(out.size[1]):
                for x in range(out.size[0]):
                    v = px[x, y][0]
                    dst[x, y] = (min(255, int(v * 1.07)), min(255, int(v * 0.94)), min(255, int(v * 0.74)))
            out = Image.blend(out.convert("RGB"), tone, amount)
        elif name == "contrast":
            out = ImageEnhance.Contrast(out).enhance(amount)
        elif name == "brightness":
            out = ImageEnhance.Brightness(out).enhance(amount)
        elif name == "saturate":
            out = ImageEnhance.Color(out).enhance(amount)
        elif name == "blur":
            out = out.filter(ImageFilter.GaussianBlur(radius=amount * pixel_scale))
    return out


def _dedupe_repeat(text: str) -> str:
    """Marquee copy is repeated for the CSS animation; keep one run."""
    head = text[:24]
    if len(text) > 60 and head:
        nxt = text.find(head, 24)
        if 20 < nxt < len(text) - 5:
            return text[:nxt].rstrip(" ·-")
    return text


def _kf(time: int, box: dict, rotation: float, opacity: float) -> dict:
    return {"time": int(time), "x": round(box["x"], 3), "y": round(box["y"], 3),
            "w": round(box["w"], 3), "h": round(box["h"], 3),
            "rotation": rotation, "opacity": round(opacity, 3), "easing": "linear"}


def _slice_keyframes(box: dict, start: float, end: float, duration: int,
                     first: bool, last: bool) -> list[dict]:
    """Hard-cut slice: only one frame is opaque at any moment."""
    kfs: list[dict] = []
    if first:
        kfs.append(_kf(0, box, 0.0, 1.0))
    else:
        kfs.append(_kf(0, box, 0.0, 0.0))
        kfs.append(_kf(max(0.0, start - 1), box, 0.0, 0.0))
        kfs.append(_kf(start, box, 0.0, 1.0))
    if last:
        kfs.append(_kf(duration, box, 0.0, 1.0))
    else:
        kfs.append(_kf(max(start, end - 1), box, 0.0, 1.0))
        kfs.append(_kf(end, box, 0.0, 0.0))
    return kfs


def _window_keyframes(box: dict, rotation: float, opacity: float,
                      start: float, end: float | None, duration: int) -> list[dict]:
    """Hold hidden outside [start, end]; the page switches state instantly."""
    d = max(1, duration)
    t_in = max(0, int(round(start * 1000)))
    t_end = None if end is None else max(t_in + 1, int(round(end * 1000)))
    if t_in <= 0 and (t_end is None or t_end >= d):
        return [_kf(0, box, rotation, opacity), _kf(d, box, rotation, opacity)]
    pts: list[tuple[int, float]] = []
    if t_in > 0:
        pts.append((0, 0.0))
    pts.append((t_in, 0.0 if t_in > 0 else opacity))
    pts.append((t_in + 1, opacity))
    if t_end is None:
        pts.append((max(t_in + 1, d), opacity))
    else:
        pts.append((max(t_in + 1, t_end - 1), opacity))
        pts.append((t_end, 0.0))
        if t_end < d:
            pts.append((d, 0.0))
    out, last = [], -1
    for t, o in pts:
        if t > last:
            out.append(_kf(t, box, rotation, o))
            last = t
    return out


def _pan_keyframes(box0: dict, box1: dict, duration: int) -> list[dict]:
    """Animate the photo envelope when the page pans/zooms it with --p."""
    d = max(1, duration)
    same = all(abs(box0[k] - box1[k]) < 0.05 for k in ("x", "y", "w", "h"))
    if same:
        return [_kf(0, box0, 0.0, 1.0), _kf(d, box0, 0.0, 1.0)]
    return [_kf(0, box0, 0.0, 1.0), _kf(d, box1, 0.0, 1.0)]


def _measured_stage_duration(stage_attrs: dict, page_ms: int | None,
                             nodes: list[dict], fallback_ms: int) -> int:
    data_d = (stage_attrs.get("data-d") or "").strip()
    if data_d.isdigit():
        return max(500, int(data_d))
    if page_ms:
        return page_ms
    ends = sorted({n["timing"].get("end") for n in nodes
                   if n["timing"].get("end") is not None and n["timing"]["end"] < 90})
    if ends:
        return max(1000, int(math.ceil(ends[-1] * 1000)))
    return max(1000, fallback_ms)


def _is_open_end(end: float | None, duration_ms: int) -> bool:
    return end is None or end * 1000 >= duration_ms * 1.2


def _pick_stage_background(nodes: list[dict]) -> dict | None:
    """The full-bleed photo, if the stage has one (never a photo inside a window)."""
    best, best_area = None, 0.0
    for n in nodes:
        if n["disp"] == "none" or n["vis"] != "visible" or n["op"] < 0.05:
            continue
        if any(c in MEASURE_SKIP for c in n["cls"].split()):
            continue  # blurred duplicate / vignette / scanlines are not the photo
        if n["bgKind"] != "url" and n["tag"] != "img":
            continue
        visible = _rect_intersect(n["box"], n["clip"])
        area = visible["w"] * visible["h"]
        if area >= 0.88 * 100 * 100 and area > best_area:
            best, best_area = n, area
    return best


def _measured_name(node: dict, kind: str, text: str) -> str:
    classes = node["cls"].split()
    if kind == "text":
        words = " ".join(text.split())
        return (words[:38] or "Text")
    if kind == "image":
        for cls in classes:
            if cls in ("im", "photo", "cam", "win"):
                return "Photo"
        return "Photo"
    if node.get("bgKind") == "gradient":
        if "vg" in classes:
            return "Vignette"
        if "sp" in classes:
            return "Spotlight"
        return "Gradient"
    fill = _colour_alpha(node.get("bg")) > 0.05
    width, height = node["box"]["w"], node["box"]["h"]
    if not fill:
        return "Frame"
    if height <= 4:
        return "Bar"
    if height <= 7 and width <= 50:
        return "Badge"
    if height >= 12:
        return "Banner"
    return "Panel"


def _measured_text_layer(node: dict, W: int, H: int, idx: int, duration: int,
                         stage_w: float, warnings: list[str],
                         transform: str | None = None,
                         placement: dict | None = None) -> dict | None:
    # inline runs (e.g. `<div>SINGL-END<br>REC <span class=tc>08:14</span></div>`)
    # only merge into an element that already has its own direct text; a pure
    # wrapper must not steal its child's text.
    raw = node.get("flowText") if node.get("text") else node.get("text")
    text = _dedupe_repeat(_clean_text(raw or ""))
    before = node.get("before") or ""
    marker = re.match(r"""^["'](.*)["']$""", before.strip(), re.S)
    if marker and marker.group(1).strip() and text:
        text = marker.group(1) + text
    if not text:
        return None
    src = placement or node
    box = dict(src.get("content") or src["box"])
    text_box = src.get("textBox") or node.get("textBox")
    if box["w"] < 0.2 or box["h"] < 0.2:
        if not text_box:
            return None
        box = dict(text_box)
    clipped = _clip_cuts(box, node["clip"]) or (text_box and _clip_cuts(text_box, node["clip"]))
    if clipped:
        wide = text_box and text_box["w"] > node["clip"]["w"] * 1.8
        if wide:
            box = dict(text_box)  # marquee: keep one line and let the stage clip it
        else:
            box = _rect_intersect(box, node["clip"])
            if box["w"] < 0.2 or box["h"] < 0.2:
                return None
    font = node.get("font") or {}
    fs = float(font.get("size") or 16.0)
    size_pct = fs / (stage_w or 1080.0) * 100.0
    lh_px = float(font.get("lineHeight") or fs * 1.2)
    start = float(node["timing"].get("start") or 0.0)
    end = node["timing"].get("end")
    if _is_open_end(end, duration):
        end = None
    rotation, scale = _transform_parts(transform if transform is not None else node.get("tf"))
    if abs(scale - 1.0) > 0.02:
        size_pct *= scale
        for key in ("w", "h"):
            box[key] = round(box[key] * scale, 3)
        warnings.append(f"Text {text[:24]!r} is scaled × {scale:.2f}; the font size was scaled to match.")
    align = {"start": "left", "end": "right"}.get(font.get("align", "left"), font.get("align", "left"))
    # a little width slack so the renderer does not re-wrap a line the browser
    # already fitted (shrink-to-fit boxes sit exactly on the wrap boundary)
    slack = 1.0
    if align == "right":
        box["x"] = round(box["x"] - slack, 3)
    elif align == "center":
        box["x"] = round(box["x"] - slack / 2, 3)
    box["w"] = round(box["w"] + slack, 3)
    layer = {
        "id": f"m{idx:02d}-text-{_slug(text, 20)}",
        "type": "text",
        "name": _measured_name(node, "text", text),
        "role": "foreground",
        "z": idx,
        "x": round(box["x"], 3), "y": round(box["y"], 3),
        "w": round(box["w"], 3), "h": round(box["h"], 3),
        "rotation": rotation,
        "opacity": node["op"],
        "fit": "contain",
        "visible": True,
        "locked": False,
        "text": text,
        "meta": {
            "tag": node["tag"], "cssClasses": node["cls"].split(),
            "roleHint": "foreground", "textLength": len(text),
            "estimatedBox": False, "measured": True,
            "start": start, "end": end,
        },
        "source": {
            "type": "html-measured", "tag": node["tag"],
            "classList": node["cls"].split(),
            "roleHint": "foreground",
            "selector": "." + ".".join(node["cls"].split()) if node["cls"] else node["tag"],
        },
        "font": {
            "family": font.get("family") or "Helvetica Neue",
            "weight": parse_weight(font.get("weight")),
            "style": "italic" if "italic" in (font.get("style") or "") else "normal",
            "sizePct": round(size_pct, 2),
            "lineHeight": round(lh_px / fs, 3) if fs else 1.2,
            "letterSpacingEm": round(float(font.get("letterSpacing") or 0.0) / fs, 4) if fs else 0.0,
            "transform": "uppercase" if "uppercase" in (font.get("transform") or "") else "none",
        },
        "color": parse_colour(font.get("color")),
        "align": align,
        "verticalAlign": "middle",
        "overflow": "clip",
    }
    layer["keyframes"] = _window_keyframes(
        {"x": layer["x"], "y": layer["y"], "w": layer["w"], "h": layer["h"]},
        rotation, layer["opacity"], start, end, duration)
    return layer



# ── CSS gradients as SVG ─────────────────────────────────────────────────

_COLOUR_TOKEN = re.compile(r"^(rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}|[a-zA-Z]+)")


def _is_colour_token(token: str) -> bool:
    token = token.strip().lower()
    if token.startswith(("#", "rgb(", "rgba(", "hsl(", "hsla(")):
        return True
    return token in NAMED_COLOURS or token in ("transparent", "currentcolor")


def _starts_with_colour(text: str) -> bool:
    m = _COLOUR_TOKEN.match(text.strip())
    return bool(m and _is_colour_token(m.group(1)))


def _split_top_level(value: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == sep and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p]


def _gradient_angle(prelude: str) -> float:
    """CSS gradient angle in degrees (0 = up, 90 = right)."""
    text = prelude.strip().lower()
    if text.startswith("to "):
        side = text[3:].strip()
        return {"top": 0.0, "right": 90.0, "bottom": 180.0, "left": 270.0,
                "top right": 45.0, "right top": 45.0, "bottom right": 135.0,
                "right bottom": 135.0, "bottom left": 225.0, "left bottom": 225.0,
                "top left": 315.0, "left top": 315.0}.get(side, 180.0)
    m = re.match(r"(-?[\d.]+)\s*(deg|turn|rad|grad)?", text)
    if not m:
        return 180.0
    value, unit = float(m.group(1)), (m.group(2) or "deg")
    return value * {"deg": 1.0, "turn": 360.0, "rad": 57.2958, "grad": 0.9}[unit]


def _gradient_stops(text: str, axis_px: float) -> list[tuple[str, float]] | None:
    stops: list[tuple[str, float | None]] = []
    for part in _split_top_level(text):
        m = _COLOUR_TOKEN.match(part.strip())
        if not m or not _is_colour_token(m.group(1)):
            continue
        colour = parse_colour(m.group(1))
        rest = part[m.end():].strip()
        pos = None
        pm = re.search(r"(-?[\d.]+)\s*(%|px|em)?", rest)
        if pm:
            n = float(pm.group(1))
            unit = pm.group(2) or "px"
            pos = n / 100.0 if unit == "%" else (n / axis_px if axis_px else 0.0)
            pos = max(0.0, min(1.0, pos))
        stops.append((colour, pos))
    if not stops:
        return None
    for i, (_, pos) in enumerate(stops):
        if pos is None:
            stops[i] = (stops[i][0], i / max(1, len(stops) - 1))
    return [(c, p) for c, p in stops]


def _stop_xml(colour: str, offset: float) -> str:
    if len(colour) == 9:
        opacity = int(colour[7:9], 16) / 255.0
        return (f'<stop offset="{round(offset, 4)}" stop-color="{colour[:7]}" '
                f'stop-opacity="{round(opacity, 3)}"/>')
    return f'<stop offset="{round(offset, 4)}" stop-color="{colour}"/>'


def _gradient_layer_xml(value: str, index: int, tile_w: float, tile_h: float) -> tuple[str, str] | None:
    """Return (defs, fill) for one gradient layer, or None if unsupported."""
    text = value.strip()
    m = re.match(r"(repeating-)?(linear|radial)-gradient\((.*)\)$", text, re.S | re.I)
    if not m:
        return None
    kind = m.group(2).lower()
    parts = _split_top_level(m.group(3))
    if not parts:
        return None
    has_prelude = not _starts_with_colour(parts[0])
    prelude = parts[0] if has_prelude else ""
    stop_text = ", ".join(parts[1:] if has_prelude else parts)
    gid = f"g{index}"
    if kind == "linear":
        angle = _gradient_angle(prelude) if prelude else 180.0
        dx, dy = math.sin(math.radians(angle)), -math.cos(math.radians(angle))
        axis = abs(tile_w * dx) + abs(tile_h * dy) or tile_w or 1
        stops = _gradient_stops(stop_text, axis)
        if stops is None:
            return None
        stop_xml = "".join(_stop_xml(c, p) for c, p in stops)
        defs = (f'<linearGradient id="{gid}" x1="{round(0.5 - dx / 2, 4)}" '
                f'y1="{round(0.5 - dy / 2, 4)}" x2="{round(0.5 + dx / 2, 4)}" '
                f'y2="{round(0.5 + dy / 2, 4)}">{stop_xml}</linearGradient>')
        return defs, f"url(#{gid})"
    cx = cy = 0.5
    at = re.search(r"at\s+(-?[\d.]+)(%|px)?\s+(-?[\d.]+)(%|px)?", prelude)
    if at:
        cx = float(at.group(1)) / (100.0 if (at.group(2) or "%") == "%" else (tile_w or 1))
        cy = float(at.group(3)) / (100.0 if (at.group(4) or "%") == "%" else (tile_h or 1))
        cx, cy = max(0.0, min(1.0, cx)), max(0.0, min(1.0, cy))
    stops = _gradient_stops(stop_text, max(tile_w, tile_h))
    if stops is None:
        return None
    stop_xml = "".join(_stop_xml(c, p) for c, p in stops)
    defs = (f'<radialGradient id="{gid}" cx="{round(cx, 4)}" cy="{round(cy, 4)}" r="0.7071">'
            f'{stop_xml}</radialGradient>')
    return defs, f"url(#{gid})"


def gradient_data_url(value: str, bg_size: str | None, w_px: float, h_px: float,
                      stroke: str | None = None, stroke_width: float = 0.0,
                      rx: float = 0.0, is_circle: bool = False,
                      base: str | None = None) -> str | None:
    """Render CSS linear/radial (and tiled) gradients as an SVG data URL."""
    if not value:
        return None
    layers = [v for v in _split_top_level(value) if v.strip().lower() != "none"]
    if not layers:
        return None
    sizes = _split_top_level(bg_size or "auto")
    defs: list[str] = []
    fills: list[str] = []
    for i, layer_value in enumerate(layers):
        size = sizes[i] if i < len(sizes) else "auto"
        tw, th = w_px, h_px
        sm = re.match(r"([\d.]+)px\s+([\d.]+)px", size.replace(" ", " "))
        if sm:
            tw, th = float(sm.group(1)), float(sm.group(2))
        elif "%" in size:
            pcts = re.findall(r"([\d.]+)%", size)
            if len(pcts) >= 2:
                tw, th = w_px * float(pcts[0]) / 100, h_px * float(pcts[1]) / 100
        layer = _gradient_layer_xml(layer_value, i, tw, th)
        if layer is None:
            return None
        layer_defs, fill = layer
        defs.append(layer_defs)
        if (tw, th) != (w_px, h_px) and tw >= 1 and th >= 1:
            pid = f"p{i}"
            defs.append(f'<pattern id="{pid}" width="{round(tw, 2)}" height="{round(th, 2)}" '
                        f'patternUnits="userSpaceOnUse">'
                        f'<rect width="{round(tw, 2)}" height="{round(th, 2)}" fill="{fill}"/>'
                        f'</pattern>')
            fills.append(f"url(#{pid})")
        else:
            fills.append(fill)
    w = max(2.0, round(w_px, 1))
    h = max(2.0, round(h_px, 1))
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    if defs:
        parts.append("<defs>" + "".join(defs) + "</defs>")
    hw = round(stroke_width / 2.0, 2) if stroke_width else 0.0
    rw, rh = max(1.0, round(w - stroke_width, 1)), max(1.0, round(h - stroke_width, 1))
    rx_attr = f' rx="{rx}" ry="{rx}"' if rx > 0 else ""
    if base:
        parts.append(f'<rect x="{hw}" y="{hw}" width="{rw}" height="{rh}"{rx_attr} fill="{base}"/>')
    for fill in reversed(fills):  # CSS lists the top layer first
        parts.append(f'<rect x="{hw}" y="{hw}" width="{rw}" height="{rh}"{rx_attr} fill="{fill}"/>')
    if stroke and stroke_width > 0:
        parts.append(f'<rect x="{hw}" y="{hw}" width="{rw}" height="{rh}"{rx_attr} fill="none" '
                     f'stroke="{stroke}" stroke-width="{stroke_width}"/>')
    parts.append("</svg>")
    return "data:image/svg+xml;utf8," + urllib.parse.quote("".join(parts))

def _measured_shape_layers(node: dict, W: int, H: int, idx: int, duration: int,
                           stage_w: float, warnings: list[str],
                           transform: str | None = None,
                           placement: dict | None = None) -> list[dict]:
    """Background box + per-side border bars for one element."""
    box = (placement or node)["box"]
    fill_alpha = _colour_alpha(node.get("bg"))
    border = node.get("border") or {}
    widths = border.get("widths") or {}
    out: list[dict] = []
    scale = W / stage_w if stage_w else 1.0
    start = float(node["timing"].get("start") or 0.0)
    end = node["timing"].get("end")
    if _is_open_end(end, duration):
        end = None
    rotation, tscale = _transform_parts(transform if transform is not None else node.get("tf"))
    if placement is not None:
        rotation, tscale = 0.0, 1.0
    if abs(tscale - 1.0) > 0.02:
        cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
        w, h = box["w"] * tscale, box["h"] * tscale
        box = {"x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}
    if node["bgKind"] == "url":
        return out  # handled as an image layer

    w_px = box["w"] / 100.0 * W
    h_px = box["h"] / 100.0 * H
    top = float(widths.get("t") or 0.0)
    left = float(widths.get("l") or 0.0)
    right = float(widths.get("r") or 0.0)
    bottom = float(widths.get("b") or 0.0)
    fill = parse_colour(node.get("bg")) if fill_alpha > 0.02 else None
    uniform = max(top, left, right, bottom)
    has_border = uniform > 0.1
    has_gradient = node.get("bgKind") == "gradient"
    if not fill and not has_border and not has_gradient:
        return out
    if fill and box["w"] >= 95 and box["h"] >= 95 and start <= 0 and end is None:
        return out  # a permanent full-bleed colour is the stage background

    radius_px = float(node.get("radius") or 0.0) * scale
    is_circle = min(w_px, h_px) > 0 and radius_px >= min(w_px, h_px) / 2 - 0.6
    stroke_w = 0.0
    stroke = None
    uniform_sides = max(top, left, right, bottom) - min(top, left, right, bottom) < 0.6
    if has_border and uniform_sides:
        stroke_w = uniform * scale
        stroke = parse_colour(border.get("topColor") or "#000000")
    name = _measured_name(node, "shape", "")
    rx = 0.0 if is_circle else radius_px
    gradient_url = None
    if node.get("bgKind") == "gradient":
        gradient_url = gradient_data_url(node.get("bgGradient") or "", node.get("bgSize"),
                                         w_px, h_px, stroke=stroke, stroke_width=stroke_w,
                                         rx=rx, is_circle=is_circle)
        if gradient_url is None:
            warnings.append(f"Gradient on <{node['tag']} .{node['cls']}> is not supported; skipped.")
            return _measured_side_rules(node, name, W, H, idx, duration, stage_w,
                                       rotation, box, start, end)
    paints_box = bool(fill) or bool(gradient_url) or (has_border and uniform_sides)
    if not paints_box:
        return _measured_side_rules(node, name, W, H, idx, duration, stage_w, rotation, box, start, end)
    url = gradient_url or make_svg_data_url(max(2.0, w_px), max(2.0, h_px), fill=fill or "transparent",
                                            stroke=stroke, stroke_width=stroke_w, rx=rx,
                                            is_circle=is_circle)
    out.append({
        "id": f"m{idx:02d}-shape-{_slug(name, 20)}",
        "type": "image", "name": name, "role": "foreground", "z": idx,
        "x": round(box["x"], 3), "y": round(box["y"], 3),
        "w": round(box["w"], 3), "h": round(box["h"], 3),
        "rotation": rotation, "opacity": node["op"], "url": url, "frames": [url],
        "fit": "fill", "visible": True, "locked": False,
        "meta": {"tag": node["tag"], "cssClasses": node["cls"].split(),
                 "start": start, "end": end, "isShape": True, "estimatedBox": False,
                 "measured": True},
        "source": {"type": "html-measured", "tag": node["tag"], "classList": node["cls"].split()},
        "keyframes": _window_keyframes(
            {"x": box["x"], "y": box["y"], "w": box["w"], "h": box["h"]},
            rotation, node["op"], start, end, duration),
    })
    # per-side borders the shorthand path misses (e.g. `.hd`'s pink left rule)
    if has_border and not uniform_sides:
        out.extend(_measured_side_rules(node, name, W, H, idx, duration, stage_w,
                                       rotation, box, start, end))
    return out


def _measured_side_rules(node: dict, name: str, W: int, H: int, idx: int, duration: int,
                         stage_w: float, rotation: float, box: dict,
                         start: float, end: float | None) -> list[dict]:
    border = node.get("border") or {}
    widths = border.get("widths") or {}
    scale = W / stage_w if stage_w else 1.0
    px = {side: float(widths.get(side) or 0.0) * scale for side in ("t", "r", "b", "l")}
    out: list[dict] = []
    sides = (
        ("top", px["t"], border.get("topColor"),
         {"x": box["x"], "y": box["y"], "w": box["w"], "h": px["t"] / H * 100}),
        ("left", px["l"], border.get("leftColor"),
         {"x": box["x"], "y": box["y"], "w": px["l"] / W * 100, "h": box["h"]}),
        ("right", px["r"], border.get("topColor"),
         {"x": box["x"] + box["w"] - px["r"] / W * 100, "y": box["y"],
          "w": px["r"] / W * 100, "h": box["h"]}),
        ("bottom", px["b"], border.get("topColor"),
         {"x": box["x"], "y": box["y"] + box["h"] - px["b"] / H * 100,
          "w": box["w"], "h": px["b"] / H * 100}),
    )
    for side, _width, color, sbox in sides:
        if sbox["w"] < 0.01 or sbox["h"] < 0.01 or not color:
            continue
        side_url = make_svg_data_url(max(1.0, sbox["w"] / 100.0 * W),
                                     max(1.0, sbox["h"] / 100.0 * H),
                                     fill=parse_colour(color))
        out.append({
            "id": f"m{idx:02d}-border-{side}",
            "type": "image", "name": f"{name} {side} rule", "role": "foreground", "z": idx,
            "x": round(sbox["x"], 3), "y": round(sbox["y"], 3),
            "w": round(sbox["w"], 3), "h": round(sbox["h"], 3),
            "rotation": rotation, "opacity": node["op"], "url": side_url, "frames": [side_url],
            "fit": "fill", "visible": True, "locked": False,
            "meta": {"tag": node["tag"], "cssClasses": node["cls"].split(), "start": start,
                     "end": end, "isShape": True, "estimatedBox": False, "measured": True},
            "source": {"type": "html-measured", "tag": node["tag"]},
            "keyframes": _window_keyframes(sbox, rotation, node["op"], start, end, duration),
        })
    return out


def _measured_image_layer(node: dict, url: str, W: int, H: int, idx: int, duration: int,
                          base_dir: Path, warnings: list[str],
                          tf_node: dict | None = None) -> dict | None:
    start = float(node["timing"].get("start") or 0.0)
    end = node["timing"].get("end")
    if _is_open_end(end, duration):
        end = None
    fit = node.get("objFit") if node.get("tag") == "img" else None
    # pick the placement that is actually visible on stage: an exit transform can
    # park the element off-canvas, an entry transform can start it off-canvas
    candidates = [c for c in (tf_node, node)
                  if c and c["box"]["w"] > 0.2 and c["box"]["h"] > 0.2]
    if tf_node and _stage_area(tf_node["box"]) > 0.5:
        source_node = tf_node
    elif node and _stage_area(node["box"]) > 0.5:
        source_node = node
    else:
        source_node = max(candidates, key=lambda c: _stage_area(c["box"])) if candidates else node
    paint = _paint_box(source_node, url, W, H, base_dir, fit_override=fit)
    if paint["w"] <= 0.2 or paint["h"] <= 0.2:
        paint = dict(node["box"])
    # an image never paints outside its own (transformed) element box;
    # background-size:cover / object-fit overflow is clipped there first,
    # then by the nearest clipping ancestor
    visible = _rect_intersect(paint, source_node["box"])
    visible = _rect_intersect(visible, source_node.get("clip") or node["clip"])
    if visible["w"] * visible["h"] < 0.05:
        return None  # fully off-canvas in every state
    clip_radius = source_node.get("clipRadius") or node.get("clipRadius") or []
    box, crop_url = paint, None
    if (abs(visible["w"] - paint["w"]) > 0.2 or abs(visible["h"] - paint["h"]) > 0.2
            or abs(visible["x"] - paint["x"]) > 0.2 or abs(visible["y"] - paint["y"]) > 0.2):
        if visible["w"] > 0.2 and visible["h"] > 0.2:
            crop_url = _crop_to_data_url(url, paint, visible, W, H, base_dir,
                                         node.get("filter"), radius=clip_radius,
                                         mask_image=node.get("maskImage"))
            box = visible
        if crop_url is None:
            warnings.append(f"Clipped image <{node['tag']} .{node['cls'][:20]}> kept uncropped.")
            box = visible or paint
    elif clip_radius and any(r > 0.2 for r in clip_radius) and visible["w"] > 0.2:
        # a rounded clip window with an image exactly filling it still needs the mask
        crop_url = _crop_to_data_url(url, paint, visible, W, H, base_dir,
                                     node.get("filter"), radius=clip_radius,
                                     mask_image=node.get("maskImage"))
        if crop_url:
            box = visible
    elif node.get("maskImage") and visible["w"] > 0.2 and visible["h"] > 0.2:
        # a mask-image fade needs the image re-encoded even without a crop
        crop_url = _crop_to_data_url(url, paint, visible, W, H, base_dir,
                                     node.get("filter"), radius=clip_radius,
                                     mask_image=node.get("maskImage"))
        if crop_url:
            box = visible
    rotation, _ = _transform_parts(node.get("tf"))
    if crop_url is None and node.get("filter") and node["filter"] != "none":
        filtered = _filtered_data_url(url, node["filter"], base_dir)
        if filtered:
            url = filtered
        else:
            warnings.append(f"CSS filter on {node['tag']}.{node['cls'][:20]} not applied.")
    url = crop_url or url
    layer = {
        "id": f"m{idx:02d}-photo-{_slug(node['cls'] or node['tag'], 18)}",
        "type": "image", "name": _measured_name(node, "image", ""),
        "role": "foreground", "z": idx,
        "x": round(box["x"], 3), "y": round(box["y"], 3),
        "w": round(box["w"], 3), "h": round(box["h"], 3),
        "rotation": rotation, "opacity": node["op"],
        "url": url, "frames": [url], "fit": "fill", "visible": True, "locked": False,
        "meta": {"tag": node["tag"], "cssClasses": node["cls"].split(),
                 "start": start, "end": end, "measured": True, "estimatedBox": False},
        "source": {"type": "html-measured", "tag": node["tag"], "classList": node["cls"].split()},
    }
    layer["keyframes"] = _window_keyframes(box, rotation, node["op"], start, end, duration)
    return layer


RASTER_SCRIPT = Path(__file__).resolve().with_name("rasterize-html-chrome.py")
_RASTER_CACHE: dict[tuple[str, int, str], str] = {}


def rasterize_element(html_path: Path, stage_index: int, path: str, width_px: float,
                      height_px: float, stage_width: int, warnings: list[str],
                      time_s: float = 0.0) -> str | None:
    """Flatten complex artwork (inline SVG, canvas) into a transparent PNG layer."""
    key = (str(html_path), stage_index, path, round(time_s, 3))
    if key in _RASTER_CACHE:
        return _RASTER_CACHE[key]
    if not RASTER_SCRIPT.is_file():
        warnings.append("Rasterizer not found; artwork skipped.")
        return None
    with tempfile.TemporaryDirectory(prefix="itl-art-") as td:
        out = Path(td) / "art.png"
        argv = [sys.executable, str(RASTER_SCRIPT), "--html", str(html_path),
                "--stage-index", str(stage_index), "--path", path,
                "--width", str(max(1, int(round(width_px)))),
                "--height", str(max(1, int(round(height_px)))),
                "--stage-width", str(stage_width), "--out", str(out)]
        if time_s:
            argv += ["--time", f"{time_s:.3f}"]
        for var in PROGRESS_VARS:
            argv += ["--var", f"{var}=1"]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"Artwork rasterization failed ({exc}).")
            return None
        if done.returncode != 0 or not out.is_file():
            tail = " | ".join((done.stderr or done.stdout or "").strip().splitlines()[-2:])
            warnings.append(f"Artwork rasterization failed ({tail or 'no output'}).")
            return None
        data = out.read_bytes()
    # opaque artwork compresses far better as JPEG; keep PNG only when the
    # element really needs transparency
    mime = "image/png"
    if Image is not None:
        try:
            with Image.open(BytesIO(data)) as im:
                rgba = im.convert("RGBA")
                if rgba.getextrema()[3][0] == 255:
                    buf = BytesIO()
                    rgba.convert("RGB").save(buf, "JPEG", quality=92)
                    data, mime = buf.getvalue(), "image/jpeg"
        except Exception:
            pass
    url = f"data:{mime};base64," + base64.b64encode(data).decode()
    _RASTER_CACHE[key] = url
    return url


def build_stage_measured(passes: dict, index: int, name: str, page_ms: int | None,
                         fallback_ms: int, base_dir: Path, stage_attrs: dict,
                         html_path: Path | None = None) -> dict:
    layout = passes["layout"]["stages"][index]
    start_pass = passes["start"]["stages"][index]
    end_pass = passes["end"]["stages"][index]
    url_table = passes["layout"].get("urls") or []
    nodes = layout["nodes"]
    stage_samples = next((st for st in (passes.get("samples") or [])
                          if st.get("stage") == index), None)
    rows_by_path: dict[str, list[tuple[float, dict]]] = {}
    if stage_samples:
        for row in stage_samples.get("rows") or []:
            for sample_node in row.get("nodes") or []:
                rows_by_path.setdefault(sample_node["path"], []).append(
                    (float(row.get("t") or 0), sample_node))

    def _eff(node: dict) -> float:
        return float(node.get("effOp", node.get("op", 0.0)) or 0.0)

    def _path_max_eff(path: str) -> float | None:
        rows = rows_by_path.get(path)
        if not rows:
            return None
        return max(_eff(n) for _, n in rows)

    def _sample_rep(path: str):
        rows = rows_by_path.get(path) or []
        good = [(t, n) for t, n in rows
                if _eff(n) > 0.02 and n["box"]["w"] > 0.15 and n["box"]["h"] > 0.15]
        if not good:
            return None
        # prefer the state where the element is actually on stage; an entrance
        # transform can be visible but still outside the stage at early samples
        on_stage = [(t, n) for t, n in good if _stage_area(n["box"]) > 0.2]
        return max(on_stage or good,
                   key=lambda r: _stage_area(r[1]["box"]) * _eff(r[1]))
    sw, sh = float(layout["widthPx"] or 9), float(layout["heightPx"] or 16)
    W = DEFAULT_STAGE_W
    H = int(round(W * sh / sw)) or DEFAULT_STAGE_H
    measured_d = str(layout.get("dataD") or "")
    if measured_d.isdigit():
        duration = max(500, int(measured_d))
    else:
        duration = _measured_stage_duration(stage_attrs, page_ms, nodes, fallback_ms)
    warnings: list[str] = []
    layers: list[dict] = []
    idx = 0

    def add(layer: dict | None, path: str | None = None) -> None:
        nonlocal idx
        if layer is None:
            return
        if path is not None:
            layer.setdefault("meta", {})["path"] = path
        layer["z"] = idx
        layers.append(layer)
        idx += 1

    stage_colour = layout.get("background") or "rgba(0,0,0,0)"
    stage_bg_image = layout.get("backgroundImage") or ""
    base_path: str | None = None
    bg_node = _pick_stage_background(nodes)
    stage_gradient_url = None
    if "gradient" in stage_bg_image:
        stage_gradient_url = gradient_data_url(
            stage_bg_image, layout.get("backgroundSize"), W, H,
            base=parse_colour(stage_colour) if _colour_alpha(stage_colour) > 0.02 else None)
    if stage_gradient_url:
        # the stage's own gradient is the backdrop; full-bleed children are content
        add({
            "id": "html-bg", "type": "image", "name": "Background",
            "role": "background", "z": 0, "url": stage_gradient_url,
            "frames": [stage_gradient_url],
            "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0,
            "rotation": 0.0, "opacity": 1.0, "fit": "fill", "visible": True,
            "locked": False,
            "meta": {"tag": "stage", "cssClasses": [], "start": 0.0, "end": None,
                     "measured": True, "isBackground": True, "isShape": True,
                     "estimatedBox": False},
            "source": {"type": "html-measured", "tag": "stage"},
            "keyframes": [_kf(0, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0),
                          _kf(duration, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0)],
        })
        bg_node = None
    elif bg_node is not None:
        url = (url_table[bg_node["imgSrc"]] if bg_node["tag"] == "img"
               else url_table[bg_node["bgImage"]])
        bg_path = bg_node["path"]
        fit = bg_node.get("objFit") if bg_node["tag"] == "img" else None
        box0 = _paint_box(next((n for n in start_pass["nodes"] if n["path"] == bg_path), bg_node),
                          url, W, H, base_dir, fit_override=fit)
        box1 = _paint_box(next((n for n in end_pass["nodes"] if n["path"] == bg_path), bg_node),
                          url, W, H, base_dir, fit_override=fit)
        bg_filter = bg_node.get("filter") or "none"
        if bg_filter and bg_filter != "none":
            paint0 = _paint_box(bg_node, url, W, H, base_dir, fit_override=fit)
            size = _image_size(url, base_dir)
            if size:
                filtered = _filtered_data_url(
                    url, bg_filter, base_dir,
                    pixel_scale=size[0] / max(1.0, paint0["w"] / 100 * W))
                if filtered:
                    url = filtered
        bg_layer = {
            "id": "html-bg", "type": "image", "name": "Background photo",
            "role": "background", "z": 0, "url": url, "frames": [url],
            "x": box0["x"], "y": box0["y"], "w": box0["w"], "h": box0["h"],
            "rotation": 0.0, "opacity": bg_node.get("op", 1.0), "fit": "fill",
            "visible": True, "locked": False,
            "meta": {"tag": bg_node["tag"], "cssClasses": bg_node["cls"].split(),
                     "start": 0.0, "end": None, "measured": True, "isBackground": True,
                     "estimatedBox": False},
            "source": {"type": "html-measured", "tag": bg_node["tag"],
                       "classList": bg_node["cls"].split()},
            "keyframes": _pan_keyframes(box0, box1, duration),
        }
        add(bg_layer, bg_node["path"])
    elif "gradient" in stage_bg_image and not stage_gradient_url:
        url = gradient_data_url(stage_bg_image, layout.get("backgroundSize"), W, H,
                                base=parse_colour(stage_colour)
                                if _colour_alpha(stage_colour) > 0.02 else None)
        if url:
            add({
                "id": "html-bg", "type": "image", "name": "Background",
                "role": "background", "z": 0, "url": url, "frames": [url],
                "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0,
                "rotation": 0.0, "opacity": 1.0, "fit": "fill", "visible": True,
                "locked": False,
                "meta": {"tag": "stage", "cssClasses": [], "start": 0.0, "end": None,
                         "measured": True, "isBackground": True, "isShape": True,
                         "estimatedBox": False},
                "source": {"type": "html-measured", "tag": "stage"},
                "keyframes": [_kf(0, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0),
                              _kf(duration, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0)],
            })
        else:
            warnings.append("The stage gradient background could not be rendered.")
    else:
        base = next((n for n in nodes
                     if n["bgKind"] != "url" and n["tag"] != "img"
                     and n["box"]["w"] >= 95 and n["box"]["h"] >= 95
                     and n["op"] >= 0.05 and n["disp"] != "none"
                     and not any(c in MEASURE_SKIP for c in n["cls"].split())
                     and (_colour_alpha(n.get("bg")) > 0.9 or n.get("bgKind") == "gradient")
                     and _z_index(n) <= 0
                     and not n["timing"].get("end")), None)
        base_url = None
        base_path = None
        if base is not None:
            base_path = base["path"]
            base_url = gradient_data_url(base.get("bgGradient") or "", base.get("bgSize"),
                                         W, H, base=parse_colour(base.get("bg"))
                                         if _colour_alpha(base.get("bg")) > 0.02 else None)
            if base_url is None and _colour_alpha(base.get("bg")) > 0.02:
                base_url = make_svg_data_url(W, H, fill=parse_colour(base.get("bg")))
        if base_url:
            add({
                "id": "html-bg", "type": "image", "name": "Background",
                "role": "background", "z": 0, "url": base_url, "frames": [base_url],
                "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0,
                "rotation": 0.0, "opacity": 1.0, "fit": "fill", "visible": True,
                "locked": False,
                "meta": {"tag": base["tag"], "cssClasses": base["cls"].split(), "start": 0.0,
                         "end": None, "measured": True, "isBackground": True, "isShape": True,
                         "estimatedBox": False},
                "source": {"type": "html-measured", "tag": base["tag"]},
                "keyframes": [_kf(0, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0),
                              _kf(duration, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0)],
            })
        elif _colour_alpha(stage_colour) > 0.02:
            fill = parse_colour(stage_colour)
            url = make_svg_data_url(W, H, fill=fill)
            add({
                "id": "html-bg", "type": "image", "name": "Background",
                "role": "background", "z": 0, "url": url, "frames": [url],
                "x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0,
                "rotation": 0.0, "opacity": 1.0, "fit": "fill", "visible": True,
                "locked": False,
                "meta": {"tag": "stage", "cssClasses": [], "start": 0.0, "end": None,
                         "measured": True, "isBackground": True, "isShape": True,
                         "estimatedBox": False},
                "source": {"type": "html-measured", "tag": "stage"},
                "keyframes": [_kf(0, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0),
                              _kf(duration, {"x": 0, "y": 0, "w": 100, "h": 100}, 0.0, 1.0)],
            })
        else:
            warnings.append("No background colour or photo found on this stage.")

    skip_prefixes: list[str] = []
    skip_exact: set[str] = {base_path} if base_path else set()
    text_skip: set[str] = set()
    svg_roots = [n for n in nodes if n["tag"] == "svg"]
    animated_svgs: list[tuple[float, str]] = []
    if rows_by_path:
        for root in svg_roots:
            rpath = root["path"]
            for spath, srows in rows_by_path.items():
                if spath == rpath or spath.startswith(rpath + "."):
                    if len({round(float(sn.get("so") or 0), 2) for _, sn in srows}) > 1:
                        animated_svgs.append((_stage_area(root["box"]), rpath))
                        break
    animated_svgs.sort(reverse=True)
    route_paths = {rpath for _, rpath in animated_svgs[:2]}  # at most two per stage
    for root in svg_roots:
        rpath = root["path"]
        if any(rpath == p or rpath.startswith(p + ".") for p in skip_prefixes):
            continue
        if any(rpath.startswith(o["path"] + ".") for o in svg_roots if o is not root):
            continue  # nested <svg> inside another artwork
        rbox = root["box"]
        if rbox["w"] < 0.3 or rbox["h"] < 0.3 or root["op"] < 0.02:
            continue
        # an SVG whose stroke-dashoffset changes over time is a drawing animation:
        # sample-render it into frames instead of freezing one state
        route_rows = []
        if rpath in route_paths:
            for spath, srows in rows_by_path.items():
                if spath == rpath or spath.startswith(rpath + "."):
                    offsets = {round(float(sn.get("so") or 0), 2) for _, sn in srows}
                    if len(offsets) > 1:
                        route_rows = srows
                        break
        if route_rows and html_path:
            frames = max(8, min(12, int(round(duration / 900))))
            slice_ms = duration / frames
            made = 0
            for fi in range(frames):
                t = duration * fi / frames / 1000
                art = rasterize_element(html_path, index, rpath, rbox["w"] / 100 * W,
                                        rbox["h"] / 100 * H, W, warnings, time_s=t)
                if art is None:
                    break
                start = fi * slice_ms
                end = duration if fi == frames - 1 else (fi + 1) * slice_ms
                kfs = _slice_keyframes(rbox, start, end, duration,
                                       fi == 0, fi == frames - 1)
                add({
                    "id": f"m{idx:02d}-artwork-{fi:02d}",
                    "type": "image", "name": "Drawing (animated)",
                    "role": "foreground", "z": idx,
                    "x": round(rbox["x"], 3), "y": round(rbox["y"], 3),
                    "w": round(rbox["w"], 3), "h": round(rbox["h"], 3),
                    "rotation": 0.0, "opacity": 1.0, "url": art, "frames": [art],
                    "fit": "fill", "visible": True, "locked": False,
                    "meta": {"tag": "svg", "cssClasses": root["cls"].split(), "start": 0.0,
                             "end": None, "measured": True, "rasterized": True,
                             "animated": True, "frame": fi, "estimatedBox": False},
                    "source": {"type": "html-raster", "tag": "svg",
                               "classList": root["cls"].split()},
                    "keyframes": kfs,
                }, rpath)
                made += 1
            if made:
                skip_prefixes.append(rpath)
                continue
        art = rasterize_element(html_path, index, rpath, rbox["w"] / 100 * W,
                                rbox["h"] / 100 * H, W, warnings) if html_path else None
        if art is None:
            skip_prefixes.append(rpath)
            continue
        full_bleed = rbox["w"] >= 95 and rbox["h"] >= 95
        layer = {
            "id": f"m{idx:02d}-artwork",
            "type": "image", "name": "Artwork (flattened)",
            "role": "foreground", "z": idx,
            "x": round(rbox["x"], 3), "y": round(rbox["y"], 3),
            "w": round(rbox["w"], 3), "h": round(rbox["h"], 3),
            "rotation": 0.0, "opacity": root["op"], "url": art, "frames": [art],
            "fit": "fill", "visible": True, "locked": False,
            "meta": {"tag": "svg", "cssClasses": root["cls"].split(), "start": 0.0,
                     "end": None, "measured": True, "rasterized": True,
                     "estimatedBox": False},
            "source": {"type": "html-raster", "tag": "svg", "classList": root["cls"].split()},
            "keyframes": _window_keyframes(
                {"x": rbox["x"], "y": rbox["y"], "w": rbox["w"], "h": rbox["h"]},
                0.0, root["op"], 0.0, None, duration),
        }
        add(layer, rpath)
        skip_prefixes.append(rpath)

    for node in nodes:
        path = node["path"]
        if any(path == p or path.startswith(p + ".") for p in skip_prefixes):
            continue
        if path in skip_exact:
            continue
        if node is bg_node or (bg_node is not None and path == bg_node["path"]):
            continue
        if node["disp"] in ("none", "contents") or node["vis"] != "visible" or node["tag"] == "br":
            skip_prefixes.append(path)
            continue
        if any(c in MEASURE_SKIP for c in node["cls"].split()):
            skip_prefixes.append(path)
            continue
        max_eff = _path_max_eff(path) if rows_by_path else None
        rep_sample = None
        n2 = node
        if rows_by_path:
            if node["op"] < 0.02:
                rep = _sample_rep(path)
                if rep is None:
                    skip_prefixes.append(path)
                    continue
                rep_sample = rep[1]
                n2 = {**node,
                      "box": rep_sample["box"],
                      "content": rep_sample.get("content") or rep_sample["box"],
                      "text": rep_sample.get("text") or node.get("text"),
                      "op": max(0.02, rep_sample.get("op", 1.0)),
                      "clip": node.get("clip")}
        elif node["op"] < 0.02:
            skip_prefixes.append(path)
            continue
        if n2["box"]["w"] <= 0.15 or n2["box"]["h"] <= 0.15:
            # a border-only hairline (e.g. a 1px rule) has one zero dimension
            if n2["box"]["w"] > 0.5 or n2["box"]["h"] > 0.5:
                n2 = {**n2, "box": {**n2["box"],
                                    "w": max(n2["box"]["w"], 0.15),
                                    "h": max(n2["box"]["h"], 0.15)}}
            else:
                continue

        tf_node = None if rep_sample else next(
            (n for n in end_pass["nodes"] if n["path"] == path), None)
        tf_value = (tf_node or {}).get("tf")
        placement = None
        if tf_node and _is_pure_translation(tf_value) \
                and (_stage_area(tf_node["box"]) > 0.5 or _stage_area(node["box"]) <= 0.5):
            placement = tf_node
        emitted_image = False
        image_url = None
        if node["tag"] == "img" and node["imgSrc"] >= 0:
            image_url = url_table[node["imgSrc"]]
        elif node["tag"] == "video" and node.get("videoSrc", -1) >= 0:
            image_url = url_table[node["videoSrc"]]
        elif node["bgKind"] == "url" and node["bgImage"] >= 0:
            image_url = url_table[node["bgImage"]]
        if image_url:
            layer = _measured_image_layer(n2, image_url, W, H, idx, duration, base_dir,
                                          warnings, tf_node=tf_node)
            if layer is not None:
                add(layer, path)
                emitted_image = True

        if not emitted_image:
            for shape in _measured_shape_layers(n2, W, H, idx, duration, sw, warnings,
                                                transform=tf_value, placement=placement):
                add(shape, path)

        if path not in text_skip:
            text_layer = _measured_text_layer(n2, W, H, idx, duration, sw, warnings,
                                              transform=tf_value)
            if text_layer is not None:
                add(text_layer, path)
        if node.get("text"):
            text_skip.update(node.get("inlineKids") or [])


    if rows_by_path:
        # sampled box/opacity keyframes (camera flight, minimap window, cards, pins)
        for layer in layers:
            path = (layer.get("meta") or {}).get("path")
            rows = rows_by_path.get(path) or []
            if not rows:
                continue
            kind = "bg" if layer["id"] == "html-bg" else ("text" if layer["type"] == "text" else "box")
            kfs = []
            for t, sn in rows:
                if kind == "bg":
                    box = _paint_box(sn, layer["url"], W, H, base_dir, fit_override=None)
                elif kind == "text":
                    box = sn.get("content") or sn.get("box")
                else:
                    box = sn.get("box")
                if not box:
                    continue
                kfs.append(_kf(t * 1000, box, layer.get("rotation", 0.0), _eff(sn)))
            if len(kfs) < 2:
                continue
            xs = [k["x"] for k in kfs]
            ys = [k["y"] for k in kfs]
            ws = [k["w"] for k in kfs]
            hs = [k["h"] for k in kfs]
            ops = [k["opacity"] for k in kfs]
            start = float((layer.get("meta") or {}).get("start") or 0.0)
            end = (layer.get("meta") or {}).get("end")
            windowed = start > 1e-3 or end is not None
            if windowed:
                # data-s/data-e are instant cuts; sampled opacity would crossfade
                for i, (t, sn) in enumerate(rows):
                    kfs[i]["opacity"] = 1.0 if (t + 1e-6 >= start
                                                and (end is None or t < end - 1e-6)) else 0.0
                first_box = rows[0][1].get("content") or rows[0][1]["box"]
                last_box = rows[-1][1].get("content") or rows[-1][1]["box"]
                if start > 1e-3:
                    kfs += [_kf(max(0.0, start * 1000 - 1), first_box, layer.get("rotation", 0.0), 0.0),
                            _kf(start * 1000, first_box, layer.get("rotation", 0.0), 1.0)]
                if end is not None:
                    kfs += [_kf(max(0.0, end * 1000 - 1), last_box, layer.get("rotation", 0.0), 1.0),
                            _kf(end * 1000, last_box, layer.get("rotation", 0.0), 0.0)]
                kfs.sort(key=lambda k: k["time"])
            if (max(xs) - min(xs) > 0.25 or max(ys) - min(ys) > 0.25
                    or max(ws) - min(ws) > 0.25 or max(hs) - min(hs) > 0.25
                    or max(ops) - min(ops) > 0.02 or windowed):
                layer["keyframes"] = kfs

        # text that changes over the reel (counters): one layer per value
        variants = []
        for layer in list(layers):
            if layer["type"] != "text":
                continue
            path = (layer.get("meta") or {}).get("path")
            rows = rows_by_path.get(path) or []
            pairs: list[tuple[str, dict]] = []
            for t, sn in rows:
                text = (sn.get("text") or "").strip()
                if not text or _eff(sn) <= 0.02:
                    continue
                if all(text != p[0] for p in pairs):
                    pairs.append((text, sn))
            if len(pairs) < 2:
                continue
            base_node = next((n for n in nodes if n["path"] == path), None)
            if base_node is None:
                continue
            base_text = (layer.get("text") or "").strip()
            base_box = {k: layer[k] for k in ("x", "y", "w", "h")}
            base_content = base_node.get("content") or base_box
            if base_text and all(base_text != p[0] for p in pairs):
                # the resting text can predate the first time sample (a counter's 00)
                pairs.insert(0, (base_text, {"box": base_box, "content": base_content,
                                             "text": base_text, "op": 1.0}))
            layers.remove(layer)
            for text, sn in pairs:
                same_as_base = text == base_text
                synth = {**base_node, "text": text, "flowText": text,
                         "box": base_box if same_as_base else sn["box"],
                         "content": base_content if same_as_base
                                    else (sn.get("content") or sn["box"]),
                         "op": 1.0}
                tl = _measured_text_layer(synth, W, H, idx, duration, sw, warnings)
                if tl is None:
                    continue
                tl.setdefault("meta", {})["path"] = path
                tl["meta"]["textVariant"] = text
                kfs = []
                in_samples = any((s2.get("text") or "").strip() == text for _, s2 in rows)
                if same_as_base and not in_samples:
                    # visible before the first sampled state, then gone
                    end_t = next((t for t, s2 in rows
                                  if (s2.get("text") or "").strip() not in ("", text)), None)
                    kfs = [_kf(0, base_content, 0.0, 1.0),
                           _kf(max(0, (end_t or duration / 1000) * 1000 - 1),
                               base_content, 0.0, 1.0),
                           _kf((end_t or duration / 1000) * 1000, base_content, 0.0, 0.0)]
                else:
                    for t, s2 in rows:
                        same = (s2.get("text") or "").strip() == text
                        box = base_box if (same_as_base and tl.get("x") == base_box["x"]
                                           and tl.get("w") == base_box["w"]) else (
                            s2.get("content") or s2["box"])
                        kfs.append(_kf(t * 1000, box, 0.0, _eff(s2) if same else 0.0))
                tl["keyframes"] = kfs
                add(tl)

    if rows_by_path:
        # the active map dot changes colour and scale (`.md.on`)
        for layer in list(layers):
            classes = (layer.get("meta") or {}).get("cssClasses") or []
            if "md" not in classes:
                continue
            path = (layer.get("meta") or {}).get("path")
            rows = rows_by_path.get(path) or []
            on_rows = [(t, sn) for t, sn in rows
                       if "on" in (sn.get("cls") or "").split() and _eff(sn) > 0.02]
            if not on_rows:
                continue
            base_node = next((n for n in nodes if n["path"] == path), None)
            if base_node is None:
                continue
            t0, sn0 = on_rows[0]
            synth = {**base_node, "box": sn0["box"], "op": 1.0,
                     "bg": sn0.get("bg") or base_node.get("bg"),
                     "cls": sn0.get("cls") or base_node.get("cls")}
            for shape in _measured_shape_layers(synth, W, H, idx, duration, sw, warnings):
                shape["id"] = shape["id"] + "-active"
                shape["name"] = shape["name"] + " (active)"
                shape["keyframes"] = [
                    _kf(t * 1000, sn["box"], 0.0,
                        _eff(sn) if "on" in (sn.get("cls") or "").split() else 0.0)
                    for t, sn in rows]
                add(shape, path)

    if rows_by_path:
        # gradients whose centre moves with the camera (spotlight) become frames
        for layer in list(layers):
            if layer["type"] != "image" or not (layer.get("meta") or {}).get("isShape"):
                continue
            path = (layer.get("meta") or {}).get("path")
            rows = rows_by_path.get(path) or []
            if not rows:
                continue
            frames = []
            for t, sn in rows:
                value = sn.get("bgImage") or ""
                if "gradient" not in value:
                    continue
                url = gradient_data_url(value, sn.get("bgSize"), W, H,
                                        base=parse_colour(sn.get("bg"))
                                        if _colour_alpha(sn.get("bg")) > 0.02 else None)
                if url:
                    frames.append((t, sn, url))
            if len({u for _, _, u in frames}) < 2:
                continue
            pos = layers.index(layer)
            layers.remove(layer)
            total = len(frames)
            frame_layers = []
            for i, (t, sn, url) in enumerate(frames):
                start = t * 1000
                end = frames[i + 1][0] * 1000 if i + 1 < total else duration
                kfs = _slice_keyframes(sn["box"], start, end, duration,
                                       i == 0, i == total - 1)
                frame_layers.append({
                    "id": f"{layer['id']}-f{i:02d}",
                    "type": "image", "name": layer["name"] + " (animated)",
                    "role": layer.get("role", "foreground"), "z": idx,
                    "x": round(sn["box"]["x"], 3), "y": round(sn["box"]["y"], 3),
                    "w": round(sn["box"]["w"], 3), "h": round(sn["box"]["h"], 3),
                    "rotation": 0.0, "opacity": 1.0, "url": url, "frames": [url],
                    "fit": "fill", "visible": True, "locked": False,
                    "meta": {**layer.get("meta", {}), "isBackground": False,
                             "isShape": True, "animated": True, "frame": i},
                    "source": layer.get("source", {"type": "html-measured"}),
                    "keyframes": kfs,
                })
            for fl in frame_layers:
                fl["z"] = idx
                idx += 1
            layers[pos:pos] = frame_layers


    if rows_by_path and html_path:
        # a mirrored quote (Leonardo-style) that flips readable: the mirrored
        # state is captured as a raster, the live text takes over at the flip
        for node in nodes:
            if "mr" not in node["cls"].split():
                continue
            path = node["path"]
            rows = rows_by_path.get(path) or []
            flip_t = None
            for t, sn in rows:
                if "fl" in (sn.get("cls") or "").split():
                    flip_t = t
                    break
            if flip_t is None or flip_t <= 0:
                continue
            children = [l for l in layers if l["type"] == "text"
                        and (l.get("meta") or {}).get("path", "").startswith(path + ".")]
            if not children:
                continue
            box = node["box"]
            t_raster = max(0.0, min(flip_t - 0.2, duration / 1000 * 0.45))
            art = rasterize_element(html_path, index, path, box["w"] / 100 * W,
                                    box["h"] / 100 * H, W, warnings, time_s=t_raster)
            if art is None:
                continue
            start = min((l.get("meta") or {}).get("start") or 0.0 for l in children)
            kfs = [_kf(0, box, 0.0, 0.0),
                   _kf(start * 1000, box, 0.0, 1.0),
                   _kf(max(start * 1000, flip_t * 1000 - 1), box, 0.0, 1.0),
                   _kf(flip_t * 1000, box, 0.0, 0.0)]
            add({
                "id": f"m{idx:02d}-artwork-mirrored",
                "type": "image", "name": "Mirrored quote (flattened)",
                "role": "foreground", "z": idx,
                "x": round(box["x"], 3), "y": round(box["y"], 3),
                "w": round(box["w"], 3), "h": round(box["h"], 3),
                "rotation": 0.0, "opacity": 1.0, "url": art, "frames": [art],
                "fit": "fill", "visible": True, "locked": False,
                "meta": {"tag": "div", "cssClasses": node["cls"].split(), "start": 0.0,
                         "end": None, "measured": True, "rasterized": True,
                         "mirrored": True, "estimatedBox": False},
                "source": {"type": "html-raster", "tag": "div",
                           "classList": node["cls"].split()},
                "keyframes": kfs,
            }, path)
            for layer in children:
                kfs2 = []
                for kf in layer.get("keyframes") or []:
                    kf = dict(kf)
                    if kf["time"] < flip_t * 1000:
                        kf["opacity"] = 0.0
                    kfs2.append(kf)
                kfs2.append(_kf(flip_t * 1000,
                                {"x": layer["x"], "y": layer["y"],
                                 "w": layer["w"], "h": layer["h"]},
                                layer.get("rotation", 0.0), 1.0))
                kfs2.sort(key=lambda k: k["time"])
                layer["keyframes"] = kfs2

    # Attach time-varying CSS clip paths and filters to each layer. The
    # generic visibility/keyframe pass captures boxes and opacity but these
    # two effects are what make split-screen and filtered reels look wrong.
    def _clip_rect(value, stage_w, stage_h):
        if not value or value == "none":
            return None
        m = re.match(r"inset\(([^)]*)\)", value)
        if not m:
            return None
        parts = [p.strip() for p in m.group(1).split() if p.strip()]
        if not parts:
            return None
        vals = []
        for part in (parts * 4)[:4]:
            num = re.match(r"(-?[\d.]+)\s*(%|px)?", part)
            if not num:
                return None
            n = float(num.group(1))
            unit = num.group(2) or "px"
            vals.append(n / 100 if unit == "%" else n / max(1, stage_w if len(vals) % 2 == 0 else stage_h))
        top, right, bottom, left = (vals + [0, 0, 0, 0])[:4]
        return {"shape": "rect", "x": round(left * 100, 3), "y": round(top * 100, 3),
                "w": round(max(0, 1 - left - right) * 100, 3), "h": round(max(0, 1 - top - bottom) * 100, 3)}

    for layer in layers:
        path = (layer.get("meta") or {}).get("path")
        rows = rows_by_path.get(path) if path is not None else None
        if not rows:
            continue
        clips = []
        filters = []
        for t, sn in rows:
            cp = sn.get("clipPath")
            if cp:
                rect = _clip_rect(cp, W, H)
                if rect:
                    clips.append((t, rect))
            flt = sn.get("filter")
            if flt and flt != "none":
                filters.append((t, flt))
        if len(clips) >= 2 and len({json.dumps(c[1], sort_keys=True) for c in clips}) > 1:
            for k in layer.get("keyframes") or []:
                t = float(k.get("time") or 0) / 1000
                nearest = min(clips, key=lambda c: abs(c[0] - t))
                k["clip"] = nearest[1]
        if filters and len({f[1] for f in filters}) > 1:
            for k in layer.get("keyframes") or []:
                t = float(k.get("time") or 0) / 1000
                nearest = min(filters, key=lambda f: abs(f[0] - t))
                k["filter"] = nearest[1]

    text_layers = [l for l in layers if l["type"] == "text"]
    baked_text = any(n.get("text") and any(
        n["path"].startswith(r["path"] + ".") for r in svg_roots) for n in nodes)
    if not text_layers and not baked_text:
        warnings.append("No visible text found in this stage.")
    if baked_text and not text_layers:
        warnings.append("Text is baked into the flattened artwork layer (inline SVG).")
    if warnings:
        warnings = list(dict.fromkeys(warnings))
    return {
        "slug": f"{re.sub(r'[^a-zA-Z0-9_-]+', '-', name).strip('-').lower() or 'stage'}-stage",
        "name": name,
        "width": W, "height": H, "duration": duration,
        "layers": layers,
        "background": None,
        "warnings": warnings,
        "metadata": {
            "aspectRatio": f"{W}/{H}",
            "backgroundResolved": bg_node is not None or _colour_alpha(stage_colour) > 0.02,
            "textLayers": len(text_layers),
            "imageLayers": sum(1 for l in layers if l["type"] == "image"),
            "stageClasses": stage_attrs.get("classes") or [],
            "layout": "measured",
            "fonts": passes["layout"].get("fontsStatus"),
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
        "duration": int(stage.get("duration") or DURATION_MS),
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
    # The background is a normal layer (with its own pan keyframes), so the
    # preview renders exactly what the composition will render.
    return {
        "index": i, "name": s["name"], "width": s["width"], "height": s["height"],
        "background": None, "metadata": s.get("metadata", {}),
        "warnings": s.get("warnings", []), "duration": s.get("duration", DURATION_MS),
        "layers": [{
            "id": l["id"],
            "type": l["type"], "name": l["name"], "text": l.get("text", ""),
            "x": l["x"], "y": l["y"], "w": l["w"], "h": l["h"], "role": l["role"],
            "url": l.get("url") if l["type"] == "image" else None,
            "font": l.get("font"), "color": l.get("color"), "align": l.get("align"),
            "rotation": l.get("rotation", 0), "opacity": l.get("opacity", 1),
            "meta": l.get("meta"), "source": l.get("source"),
            "keyframes": l.get("keyframes"),
        } for l in s["layers"]],
    }


def main(argv: list[str] | None = None) -> int:
    global DURATION_MS, ROOT_VARS, FETCH_REMOTE, PREVIEW, PROGRESS_VARS
    p = argparse.ArgumentParser()
    p.add_argument("--html", required=True, help="Path to the HTML page")
    p.add_argument("--out", default="data/projects")
    p.add_argument("--name", default="")
    p.add_argument("--max-stages", type=int, default=10)
    p.add_argument("--duration-ms", type=int, default=DEFAULT_DURATION_MS,
                   help="fallback reel length when the page does not declare one")
    p.add_argument("--layout", choices=("auto", "measure", "css"), default="auto",
                   help="auto = measure with headless Chrome when available, else CSS; "
                        "measure = require Chrome; css = force the CSS fallback")
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
    PROGRESS_VARS = detect_progress_vars(css)
    if PROGRESS_VARS:
        say(f"progress vars: {', '.join('--' + v for v in PROGRESS_VARS)}")
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
    page_ms = detect_page_duration_ms(html)
    if page_ms:
        say(f"page declares a {page_ms} ms reel")
    else:
        say(f"warn: page declares no reel length; using {DURATION_MS} ms (or the page's last data-e)")

    scene = None
    if args.scene_json and args.scene_json.is_file():
        try:
            scene = json.loads(args.scene_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            say("warn: --scene-json is not valid JSON; ignored")

    measured = None
    if args.layout != "css":
        measured = measure_page(html_path)
        if measured is not None:
            fonts = measured["layout"].get("fontsStatus")
            say(f"measured {len(measured['layout'].get('stages', []))} stage(s) in Chrome "
                f"(fonts: {fonts})")
            if fonts != "loaded":
                say("warn: the page's webfonts were not ready; text boxes may re-wrap in Studio")
        elif args.layout == "measure":
            sys.exit("Chrome measurement is unavailable; rerun with --layout css to use the fallback")

    measured_count = len(measured["layout"].get("stages", [])) if measured else 0
    if not stages_nodes and measured_count == 0:
        sys.exit("no stage elements found in the HTML or in the rendered page")
    total = max(len(stages_nodes), measured_count)
    total = min(total, args.max_stages)
    stages = []
    for i in range(total):
        node = stages_nodes[i] if i < len(stages_nodes) else None
        stage_name = f"{page_name} — {i + 1:02d}"
        attrs = {"data-d": (node.attrs.get("data-d") if node else "") or "",
                 "classes": node.classes if node else []}
        if measured is not None and i < measured_count:
            meta_stage = measured["layout"]["stages"][i]
            attrs = {"data-d": meta_stage.get("dataD") or "",
                     "classes": (meta_stage.get("classes") or "").split()}
            stage = build_stage_measured(measured, i, stage_name, page_ms, DURATION_MS,
                                         html_path.resolve().parent, attrs,
                                         html_path=html_path.resolve())
            say(f"  stage {i + 1}: measured {stage['width']}×{stage['height']}, "
                f"{stage['duration']} ms, {len(stage['layers'])} layers")
        elif node is not None:
            stage_duration = page_ms or DURATION_MS
            if attrs["data-d"].isdigit():
                stage_duration = max(500, int(attrs["data-d"]))
            DURATION_MS = max(1000, stage_duration)
            stage = extract_stage(node, rules, ROOT_VARS, stage_name)
            stage["warnings"].append(
                "Chrome measurement unavailable — layout is estimated from CSS and may be inaccurate.")
            stage["metadata"]["layout"] = "css-estimated"
        else:
            continue
        stages.append(stage)

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