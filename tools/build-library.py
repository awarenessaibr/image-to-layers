#!/usr/bin/env python3
"""
build-library.py — turn extracted projects into a structured, agent-ready library.

Step 1 (html-to-projects.py) writes one project per stage:
    data/projects/<slug>/compositions/<slug>.composition.json  (+ images/)
This tool reads every composition and writes data/library/ :

    index.json              small catalog the agent loads first: designs, health,
                            tags, facets, which animations are safe to show
    designs/<id>.json       per-design detail: classified layers, groups, entities
    business.json           brands: name, contacts, hours, offers, hashtags, palette
    menu.json               menu items (name, description, price) with sources
    assets.json             de-duplicated images/shapes and where each is used
    clips.json              small reusable motions (extracted + built-in presets)
    animations/<id>/*.json  agent-studio/animation/2 files, linked to one design

    python3 tools/build-library.py build   --projects data/projects --library data/library
    python3 tools/build-library.py compose --library data/library --design ID --steps steps.json
    python3 tools/build-library.py validate --library data/library --design ID --animation file.json

Status rules (what the Studio may show):
    ready    measured layout, every image present, original motion valid
    review   usable, but something deserves a look (estimated layout, warnings, off-stage layers)
    blocked  not usable (missing images, broken layers, unusable motion)
Only `ready` designs, and only animations whose validation status is `ok`, should be offered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCHEMA = "agent-studio/library/1"
ANIM_SCHEMA = "agent-studio/animation/2"
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(msg: str) -> None:
    print(msg, flush=True)


def slug(text: str, n: int = 60) -> str:
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()[:n] or "item"


def norm(text: str | None) -> str:
    t = unicodedata.normalize("NFKD", text or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t).strip().lower()


def r3(v: float) -> float:
    return round(float(v), 4)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ── layer helpers ───────────────────────────────────────────────────────

EPS = {"x": .05, "y": .05, "w": .05, "h": .05, "rotation": .2, "opacity": .005}


def kfs_of(layer: dict) -> list[dict]:
    """Absolute keyframes, from the extractor (`keyframes`) or Studio (`animation.keyframes`)."""
    raw = layer.get("keyframes") or (layer.get("animation") or {}).get("keyframes") or []
    out = [{"time": int(k.get("time", 0)), "x": k.get("x", layer.get("x", 0)),
            "y": k.get("y", layer.get("y", 0)), "w": k.get("w", layer.get("w", 0)),
            "h": k.get("h", layer.get("h", 0)), "rotation": k.get("rotation", 0) or 0,
            "opacity": k.get("opacity", 1) if k.get("opacity") is not None else 1} for k in raw]
    return sorted(out, key=lambda k: k["time"])


def box_of(layer: dict) -> dict:
    return {k: r3(layer.get(k, 0) or 0) for k in "xywh"}


def windows(layer: dict) -> list[list]:
    """Time ranges (ms) in which the layer is visible; end None = until the end."""
    ks = kfs_of(layer)
    if not ks:
        return [[0, None]] if layer.get("visible", True) and (layer.get("opacity", 1) or 0) > .05 else []
    out, start = [], None
    for k in ks:
        on = k["opacity"] > .05
        if on and start is None:
            start = k["time"]
        elif not on and start is not None:
            out.append([start, k["time"]])
            start = None
    if start is not None:
        out.append([start, None])
    return out


def union(boxes: list[dict]) -> dict:
    x0 = min(b["x"] for b in boxes); y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["w"] for b in boxes); y1 = max(b["y"] + b["h"] for b in boxes)
    return {"x": r3(x0), "y": r3(y0), "w": r3(x1 - x0), "h": r3(y1 - y0)}


def iou(a: dict, b: dict) -> float:
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    inter = ix * iy
    return inter / max(1e-6, a["w"] * a["h"] + b["w"] * b["h"] - inter)


def inside(inner: dict, outer: dict, slack: float = 1.0) -> bool:
    return (inner["x"] >= outer["x"] - slack and inner["y"] >= outer["y"] - slack
            and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + slack
            and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + slack)


def is_full_bleed(l: dict) -> bool:
    return (l.get("w", 0) or 0) >= 95 and (l.get("h", 0) or 0) >= 95


def layer_kind(l: dict) -> str:
    if l.get("type") == "text":
        return "text"
    url = str(l.get("url") or "")
    meta = l.get("meta") or {}
    if l.get("role") == "background" or (is_full_bleed(l) and not meta.get("isShape")):
        return "photo"
    if meta.get("isShape") or "shape" in str(l.get("id")) or url.split("?")[0].endswith(".svg"):
        return "shape"
    return "image"


# ── text classification (business info, menu) ──────────────────────────

CUR = r"(?:£|€|\$|USD|EUR|GBP)"
PRICE = re.compile(rf"(?:{CUR}\s?\d{{1,4}}(?:[.,]\d{{1,2}})?|\d{{1,4}}(?:[.,]\d{{2}})\s?{CUR})", re.I)
PHONE = re.compile(r"(?<![\d.])(?:\+?\d[\d\s().-]{7,}\d)(?![\d.])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"(?:https?://|www\.)\S+|\b[a-z0-9-]+\.(?:com|co\.uk|uk|net|org|io|it|es|fr|de|app)\b\S*", re.I)
HANDLE = re.compile(r"(?<![\w.])@\w{2,}")
HASH = re.compile(r"#\w{2,}")
HOURS = re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b.*\d|\b\d{1,2}(?:[:.]\d{2})?\s?(?:am|pm)\b|"
                   r"\b\d{1,2}[:.]\d{2}\s?[-–]\s?\d{1,2}[:.]\d{2}\b|\bopen(?:s|ing)?\b.*\d", re.I)
ADDRESS = re.compile(r"\b\d{1,4}\s+[A-Z][\w' ]+\s(?:st|street|road|rd|avenue|ave|lane|ln|way|place|square|via|piazza)\b|"
                     r"\b[A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2}\b", re.I)
OFFER = re.compile(r"\b(?:\d+\s?%|off|free|offer|deal|2\s?for\s?1|special|save|discount|happy hour|combo)\b", re.I)
CTA = re.compile(r"\b(?:order|book|reserve|visit|call|tap|swipe|follow|subscribe|join|shop|try|link in bio|find us)\b", re.I)
EMOJI = re.compile(r"^[\W\d_]*[\U0001F000-\U0001FAFF\u2600-\u27BF]+[\W\d_]*$")


def parse_price(text: str) -> dict | None:
    m = PRICE.search(text or "")
    if not m:
        return None
    raw = m.group(0)
    num = re.search(r"\d{1,4}(?:[.,]\d{1,2})?", raw).group(0).replace(",", ".")
    sym = next((c for c in "£€$" if c in raw), None)
    cur = {"£": "GBP", "€": "EUR", "$": "USD"}.get(sym) or (re.search(r"USD|EUR|GBP", raw, re.I) or [None])[0]
    return {"raw": raw, "amount": float(num), "currency": cur.upper() if cur else None}


def classify_text(l: dict, top_size: float, brand: str) -> str:
    t = (l.get("text") or "").strip()
    if not t:
        return "text"
    size = (l.get("font") or {}).get("sizePct") or 0
    m = PRICE.search(t)
    if m and len(m.group(0)) / max(1, len(t)) >= .6:
        return "price"
    if EMAIL.search(t): return "email"
    if URL.search(t) and " " not in t.strip(): return "url"
    if HANDLE.fullmatch(t) or (HANDLE.search(t) and len(t) < 40): return "handle"
    if HASH.search(t): return "hashtag"
    if ADDRESS.search(t): return "address"
    if HOURS.search(t): return "hours"
    if PHONE.search(t) and len(re.sub(r"\D", "", t)) >= 9: return "phone"
    if brand and norm(brand) and (norm(t) == norm(brand) or norm(brand) in norm(t) and len(t) < len(brand) + 12):
        return "business-name"
    if EMOJI.match(t): return "emoji"
    if OFFER.search(t) and len(t) < 80: return "offer"
    if CTA.search(t) and len(t) < 60: return "cta"
    if len(t) <= 14 and t.upper() == t and size < 3.4: return "label"
    if size and size >= top_size * .85: return "headline"
    if len(t) > 45 or (size and size < 2.5 and len(t) > 25): return "description"
    return "title"


# ── design analysis ─────────────────────────────────────────────────────

def brand_of(comp: dict) -> dict:
    meta = comp.get("meta") or {}
    name = meta.get("brandName") or ((comp.get("brandKit") or {}).get("name") or "").replace(" kit", "")
    if not name:
        src = (comp.get("sourceFacts") or {}).get("htmlSource") or comp.get("name", "")
        name = re.sub(r"^\d+[-_ ]*", "", re.split(r"\s[·|–—]\s|\s-\s", src)[0])
    name = name.strip() or "Unknown"
    return {"id": slug(name, 40), "name": name}


def detect_menu_items(layers: list[dict]) -> list[dict]:
    items = []
    texts = [l for l in layers if l["type"] == "text"]
    for p in [l for l in texts if l["entity"] == "price"]:
        pw = p["window"][0] if p["window"] else [0, None]
        cands = []
        for t in texts:
            if t is p or t["entity"] in ("price", "label", "emoji", "cta"):
                continue
            tw = t["window"][0] if t["window"] else [0, None]
            dy = p["box"]["y"] - t["box"]["y"]
            xo = min(t["box"]["x"] + t["box"]["w"], p["box"]["x"] + p["box"]["w"]) - max(t["box"]["x"], p["box"]["x"])
            near_time = abs(tw[0] - pw[0]) <= 2500 or (tw[1] is None or tw[1] > pw[0]) and (pw[1] is None or pw[1] > tw[0])
            if 0 < dy <= 25 and xo > 0 and near_time and abs(tw[0] - pw[0]) <= 2500:
                cands.append(t)
        if not cands:
            continue
        name = max((c for c in cands if len(c["text"]) <= 60), key=lambda c: c["size"], default=None)
        if not name:
            continue
        rest = [c for c in cands if c is not name]
        desc = max(rest, key=lambda c: len(c["text"]), default=None)
        price = parse_price(p["text"])
        items.append({"name": name["text"].strip(), "description": desc["text"].strip() if desc else "",
                      "price": price, "layerIds": {"name": name["id"], "description": desc["id"] if desc else None,
                                                  "price": p["id"]},
                      "confidence": round(min(1.0, .5 + (.25 if desc else 0) + (.25 if price else 0)), 2)})
    return items


def build_groups(layers: list[dict], menu_items: list[dict]) -> list[dict]:
    by_id = {l["id"]: l for l in layers}
    groups: list[dict] = []
    used: set[str] = set()
    n = [0]

    def add(kind, label, ids, children=None, extra=None):
        n[0] += 1
        members = [by_id[i] for i in ids]
        ws = [w for m in members for w in m["window"][:1]]
        end = None if any(w[1] is None for w in ws) else max((w[1] for w in ws), default=None)
        g = {"id": f"g-{kind}-{slug(ids[0], 28)}", "kind": kind, "label": label, "layerIds": list(ids),
             "children": children or [], "bbox": union([m["box"] for m in members]),
             "window": {"start": min((w[0] for w in ws), default=0), "end": end},
             "slots": [{"layerId": m["id"], "type": m["type"], "label": m["name"],
                        "current": m.get("text") or ""} for m in members if m["kind"] in ("text", "image", "photo")]}
        g.update(extra or {})
        groups.append(g)
        for m in members:
            m.setdefault("groups", []).append(g["id"])
        return g

    # full-bleed photos → photo / before-after
    photos = [l for l in layers if l["kind"] == "photo"]
    reveal = [l for l in layers if l["kind"] == "shape" and l["box"]["w"] < 2 and l["box"]["h"] >= 90]
    def fades_out(l: dict) -> bool:
        ks = kfs_of(l["_raw"])
        return bool(ks) and ks[0]["opacity"] > .5 and min(k["opacity"] for k in ks) < .05

    fading = [p for p in photos if fades_out(p)]
    if len(photos) >= 2 and fading:
        add("before-after", "Before / after photos", [p["id"] for p in photos] + [r["id"] for r in reveal])
        used.update(p["id"] for p in photos); used.update(r["id"] for r in reveal)
    for p in photos:
        if p["id"] not in used:
            add("photo", p["name"], [p["id"]]); used.add(p["id"])

    # text swaps: same box, disjoint visibility windows
    texts = [l for l in layers if l["type"] == "text" and l["id"] not in used]
    for i, a in enumerate(texts):
        if a["id"] in used: continue
        mates = [b for b in texts[i + 1:] if b["id"] not in used and iou(a["box"], b["box"]) > .85
                 and a["window"] and b["window"] and (a["window"][0][1] or 1e9) <= b["window"][0][0] + 50]
        if mates:
            add("text-swap", " → ".join(m["text"][:18] for m in [a] + mates), [a["id"]] + [m["id"] for m in mates])
            used.update([a["id"]] + [m["id"] for m in mates])

    # shape + text pairs → badges
    shapes = [l for l in layers if l["kind"] == "shape" and l["id"] not in used and not is_full_bleed(l["_raw"])]
    for s in shapes:
        mates = [t for t in layers if t["type"] == "text" and t["id"] not in used and iou(s["box"], t["box"]) > .7]
        if mates:
            t = mates[0]
            kind = "price-badge" if t["entity"] == "price" else "badge"
            add(kind, t["text"][:30], [s["id"], t["id"]])
            used.update([s["id"], t["id"]])

    # cards: a large shape holding two or more units
    unit_groups = list(groups)
    for s in [l for l in layers if l["kind"] == "shape" and l["id"] not in used
              and l["box"]["w"] * l["box"]["h"] >= 300 and l["box"]["h"] >= 8]:
        inner = [g for g in unit_groups if inside(g["bbox"], s["box"], 1.0) and g["kind"] not in ("photo", "before-after")]
        loose = [l for l in layers if l["id"] not in used and l is not s and l["kind"] != "photo"
                 and inside(l["box"], s["box"], 1.0)]
        if len(inner) + len(loose) >= 2:
            ids = [s["id"]] + [i for g in inner for i in g["layerIds"]] + [l["id"] for l in loose]
            items = [m for m in menu_items if m["layerIds"]["name"] in ids]
            g = add("menu-item" if items else "card", (items[0]["name"] if items else s["name"]), ids,
                    children=[c["id"] for c in inner], extra={"menuItem": items[0]["name"]} if items else None)
            for c in inner: c["parent"] = g["id"]
            used.update(ids)

    # remaining single layers
    for l in layers:
        if l["id"] in used:
            continue
        kind = l["entity"] if l["type"] == "text" else l["kind"]
        add(kind, (l.get("text") or l["name"])[:30], [l["id"]])
        used.add(l["id"])
    return groups


def infer_kind(comp: dict, groups: list[dict], menu_items: list[dict], layers: list[dict]) -> tuple[str, list[str]]:
    name = norm((comp.get("sourceFacts") or {}).get("htmlSource") or comp.get("name"))
    gk = {g["kind"] for g in groups}
    tags = sorted(gk | ({"menu"} if menu_items else set()))
    ents = {l.get("entity") for l in layers if l["type"] == "text"}
    for e in ("offer", "cta", "hours", "address", "phone", "url", "handle", "hashtag"):
        if e in ents: tags.append(e)
    if "before-after" in gk or ("before" in name and "after" in name): kind = "before-after"
    elif "news" in name or "bulletin" in name: kind = "news-bulletin"
    elif "stop-motion" in name or "stop motion" in name: kind = "stop-motion"
    elif menu_items: kind = "menu-spotlight"
    elif "offer" in ents: kind = "promo"
    else: kind = "social-post"
    return kind, sorted(set(tags))


def aspect_name(w: int, h: int) -> str:
    r = w / max(1, h)
    return min({"9:16": 9 / 16, "4:5": .8, "1:1": 1.0, "5:4": 1.25, "4:3": 4 / 3, "16:9": 16 / 9, "3:4": .75},
               key=lambda k: abs({"9:16": 9 / 16, "4:5": .8, "1:1": 1.0, "5:4": 1.25, "4:3": 4 / 3, "16:9": 16 / 9, "3:4": .75}[k] - r))


def resolve_asset(url: str, projects_dir: Path) -> tuple[str, Path | None]:
    if not url: return "none", None
    if url.startswith("data:"): return "inline", None
    p = urlsplit(url).path
    if p.startswith("/data/projects/"):
        f = projects_dir / unquote(p[len("/data/projects/"):])
        return ("local" if f.is_file() else "missing"), f
    if url.startswith(("http://", "https://")): return "remote", None
    return "unknown", None


# ── motion: original animation, clips, presets ─────────────────────────

def original_animation(comp: dict, design_id: str) -> dict:
    W = {"x": 0}
    tracks = []
    for l in comp["layers"]:
        ks = kfs_of(l)
        if len(ks) < 1:
            continue
        frames = [{"t": k["time"], "dx": r3((k["x"] - l["x"]) / 100), "dy": r3((k["y"] - l["y"]) / 100),
                   "sx": r3(k["w"] / l["w"]) if l.get("w") else 1, "sy": r3(k["h"] / l["h"]) if l.get("h") else 1,
                   "a": r3(k["opacity"])} for k in ks]
        tracks.append(track_for(l, [{"preset": "keyframes", "label": "Keyframes", "start": 0,
                                     "duration": int(comp.get("duration") or 10000), "loop": False, "frames": frames}]))
    return {"schema": ANIM_SCHEMA, "kind": "studio-animation", "id": f"{design_id}-original",
            "name": "Original motion", "compositionId": design_id, "origin": "original",
            "createdAt": NOW(), "duration": int(comp.get("duration") or 10000), "fps": 25,
            "source": {"app": "build-library", "design": comp.get("name"), "layerCount": len(comp["layers"])},
            "tracks": tracks}


def track_for(l: dict, segments: list[dict]) -> dict:
    return {"layerId": l["id"], "layerName": l.get("name", ""), "layerType": l.get("type"), "role": l.get("role", ""),
            "text": l.get("text", "") or "", "box": {k: r3((l.get(k) or 0) / 100) for k in "xywh"}, "segments": segments}


def thin(frames: list[dict], n: int = 24) -> list[dict]:
    if len(frames) <= n: return frames
    idx = sorted({round(i * (len(frames) - 1) / (n - 1)) for i in range(n)})
    return [frames[i] for i in idx]


def clip_events(l: dict) -> list[list[dict]]:
    ks, events, cur = kfs_of(l), [], None
    for a, b in zip(ks, ks[1:]):
        if any(abs(a[k] - b[k]) > EPS[k] for k in EPS):
            cur = cur or [a]
            cur.append(b)
        elif cur:
            events.append(cur); cur = None
    if cur: events.append(cur)
    return events


def clip_from_event(ev: list[dict]) -> dict | None:
    a0, a1 = ev[0]["opacity"], ev[-1]["opacity"]
    mode = "in" if a1 - a0 > .5 else "out" if a0 - a1 > .5 else "move"
    rest = ev[-1] if mode == "in" else ev[0]
    t0 = ev[0]["time"]
    frames = []
    for k in ev:
        f = {"t": k["time"] - t0, "dx": r3((k["x"] - rest["x"]) / 100), "dy": r3((k["y"] - rest["y"]) / 100),
             "sx": r3(k["w"] / rest["w"]) if rest["w"] else 1, "sy": r3(k["h"] / rest["h"]) if rest["h"] else 1,
             "a": r3(k["opacity"])}
        if abs(k["rotation"] - rest["rotation"]) > .2: f["r"] = r3(k["rotation"] - rest["rotation"])
        frames.append(f)
    frames = thin(frames)
    dur = frames[-1]["t"]
    if dur < 40: return None
    f0, f1 = frames[0], frames[-1]
    ex = max(abs(f["dx"]) for f in frames); ey = max(abs(f["dy"]) for f in frames)
    sc = max(abs(f["sx"] - 1) for f in frames)
    if mode == "in":
        vx, vy = f0["dx"], f0["dy"]
        if max(f["sx"] for f in frames) > 1.04 and sc > .1: effect, d = "pop-in", ""
        elif max(abs(vx), abs(vy)) > .02:
            d = ("from-left" if vx < 0 else "from-right") if abs(vx) >= abs(vy) else ("from-below" if vy > 0 else "from-above")
            effect = "slide-in"
        elif sc > .08: effect, d = "scale-in", ""
        else: effect, d = "fade-in", ""
    elif mode == "out":
        vx, vy = f1["dx"], f1["dy"]
        if max(abs(vx), abs(vy)) > .02:
            d = ("to-left" if vx < 0 else "to-right") if abs(vx) >= abs(vy) else ("to-below" if vy > 0 else "to-above")
            effect = "slide-out"
        elif sc > .08: effect, d = "scale-out", ""
        else: effect, d = "fade-out", ""
    else:
        if ex >= .25 and ey < .05: effect, d = "sweep", ("rightward" if f1["dx"] > f0["dx"] else "leftward")
        elif sc > .03 and dur >= 2500: effect, d = ("ken-burns-in" if f1["sx"] > f0["sx"] else "ken-burns-out"), ""
        elif sc > .01 or ex > .01 or ey > .01: effect, d = "drift", ""
        else: return None
    cid = "-".join(x for x in (effect, d, str(int(round(dur, -2)))) if x)
    return {"id": f"clip-{cid}", "name": " ".join(x for x in (effect, d) if x).replace("-", " "), "effect": effect,
            "direction": d, "mode": mode, "duration": dur, "loop": False, "frames": frames, "origin": "extracted",
            "tags": sorted({effect.split("-")[0], mode})}


def preset(name: str, duration: int = 600, distance: float = .06) -> dict | None:
    d = duration
    F = lambda t, dx=0, dy=0, s=1, a=1, e=None: {"t": int(t), "dx": dx, "dy": dy, "sx": s, "sy": s, "a": a, **({"e": e} if e else {})}
    P = {
        "fade-in": [F(0, a=0), F(d, e="out")], "fade-out": [F(0), F(d, a=0, e="in")],
        "slide-in-from-left": [F(0, dx=-distance, a=0), F(d, e="out")], "slide-in-from-right": [F(0, dx=distance, a=0), F(d, e="out")],
        "slide-in-from-below": [F(0, dy=distance, a=0), F(d, e="out")], "slide-in-from-above": [F(0, dy=-distance, a=0), F(d, e="out")],
        "slide-out-to-left": [F(0), F(d, dx=-distance, a=0, e="in")], "slide-out-to-right": [F(0), F(d, dx=distance, a=0, e="in")],
        "slide-out-to-below": [F(0), F(d, dy=distance, a=0, e="in")], "slide-out-to-above": [F(0), F(d, dy=-distance, a=0, e="in")],
        "pop-in": [F(0, s=.6, a=0), F(d * .6, s=1.08, e="out"), F(d, e="inOut")],
        "scale-in": [F(0, s=.85, a=0), F(d, e="out")],
        "ken-burns-in": [F(0), F(d, dx=-.06, dy=-.06, s=1.12, e="linear")],
        "ken-burns-out": [F(0, dx=-.06, dy=-.06, s=1.12), F(d, e="linear")],
        "pulse": [F(0), F(d / 2, s=1.06, e="inOut"), F(d, e="inOut")],
    }
    if name not in P: return None
    effect = re.sub(r"-(from|to)-\w+$", "", name)
    direction = name[len(effect) + 1:]
    mode = "move" if effect.startswith(("ken", "pulse")) else "in" if effect.endswith("-in") else "out" if effect.endswith("-out") else "move"
    return {"id": f"preset-{name}", "name": name.replace("-", " "), "effect": effect, "direction": direction, "mode": mode,
            "duration": d, "loop": name == "pulse", "frames": P[name], "origin": "preset",
            "tags": sorted({effect.split("-")[0], mode}), "appliesTo": ["*"], "params": {"distance": distance}}


PRESET_NAMES = ["fade-in", "fade-out", "slide-in-from-left", "slide-in-from-right", "slide-in-from-below",
                "slide-in-from-above", "slide-out-to-left", "slide-out-to-right", "slide-out-to-below",
                "slide-out-to-above", "pop-in", "scale-in", "ken-burns-in", "ken-burns-out", "pulse"]


# ── animation validation + composition ─────────────────────────────────

def validate_animation(comp: dict, anim: dict, design_id: str) -> dict:
    issues, broken = [], False
    ids = {l["id"] for l in comp["layers"]}
    link = anim.get("compositionId")
    legacy = (anim.get("source") or {}).get("design") == comp.get("name")
    if link and link != design_id:
        issues.append(f"linked to another design ({link})"); broken = True
    elif not link and not legacy:
        issues.append("not linked to this design"); broken = True
    dur = anim.get("duration") or 0
    if not (1000 <= dur <= 60000): issues.append(f"duration {dur} ms is out of range"); broken = True
    tracks = anim.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return {"status": "broken", "issues": issues + ["no tracks"]}
    for tr in tracks:
        lid = tr.get("layerId")
        if lid not in ids:
            issues.append(f"unknown layer {lid}"); broken = True; continue
        segs = tr.get("segments") or []
        if not segs: issues.append(f"{lid}: no segments"); broken = True; continue
        hidden = True
        for s in segs:
            fr = s.get("frames") or []
            if not fr: issues.append(f"{lid}: empty segment"); broken = True; continue
            ts = [f.get("t") for f in fr]
            if any(not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 for t in ts) or ts != sorted(ts):
                issues.append(f"{lid}: frame times must be ascending numbers"); broken = True
            for f in fr:
                for k in ("dx", "dy", "sx", "sy", "a"):
                    v = f.get(k)
                    if v is not None and (not isinstance(v, (int, float)) or not math.isfinite(v)):
                        issues.append(f"{lid}: {k} is not a number"); broken = True
                if (f.get("a", 1) or 0) > .05: hidden = False
            if (s.get("start") or 0) > dur: issues.append(f"{lid}: segment starts after the end"); broken = True
        if hidden: issues.append(f"{lid}: never visible")
    if anim.get("source", {}).get("layerCount") not in (None, len(comp["layers"])):
        issues.append("layer count differs from the design")
    status = "broken" if broken else "warn" if issues else "ok"
    return {"status": status, "issues": issues}


def resolve_targets(detail: dict, target) -> list[str]:
    ids: list[str] = []
    groups = {g["id"]: g for g in detail["groups"]}
    for t in ([target] if isinstance(target, str) else target or []):
        t = t.split(":")[-1]
        if t in groups: ids += groups[t]["layerIds"]
        elif any(l["id"] == t for l in detail["layers"]): ids.append(t)
        else: raise ValueError(f"unknown target: {t}")
    return list(dict.fromkeys(ids))


def compose(detail: dict, comp: dict, steps: list[dict], clips: dict, name: str = "Composed animation",
            duration: int | None = None, design_id: str | None = None) -> dict:
    """Assemble an animation from clips: steps = [{target, clip, start, duration?, stagger?, params?}]."""
    design_id = design_id or detail["id"]
    layers = {l["id"]: l for l in comp["layers"]}
    segs: dict[str, list[dict]] = {}
    end_all = 0
    for i, st in enumerate(steps):
        cid = st.get("clip", "")
        clip = clips.get(cid) or clips.get("preset-" + cid) or clips.get("clip-" + cid)
        if not clip: raise ValueError(f"step {i + 1}: unknown clip {cid!r}")
        if clip["origin"] == "preset":
            p = st.get("params") or {}
            clip = preset(clip["id"][7:], int(st.get("duration") or clip["duration"]), float(p.get("distance", clip["params"]["distance"])))
        scale = (st.get("duration") / clip["duration"]) if st.get("duration") and clip["duration"] else 1
        dur = int(round(clip["duration"] * scale))
        for j, lid in enumerate(resolve_targets(detail, st.get("target"))):
            start = int(st.get("start", 0)) + j * int(st.get("stagger", 0))
            frames = [{**f, "t": int(round(f["t"] * scale))} for f in clip["frames"]]
            f0 = frames[0]
            rest = f0["dx"] == 0 and f0["dy"] == 0 and f0["sx"] == 1 and f0["sy"] == 1 and f0.get("a", 1) == 1
            lst = segs.setdefault(lid, [])
            if start > 0 and not rest and not lst:
                lst.append({"preset": "keyframes", "label": "Hold", "start": 0, "duration": start, "loop": False, "frames": [dict(f0, t=0)]})
            lst.append({"preset": "keyframes", "label": clip["name"], "start": start, "duration": dur,
                        "loop": bool(clip.get("loop")), "frames": frames})
            end_all = max(end_all, start + dur)
    total = int(duration or max(int(comp.get("duration") or 10000), end_all + 500))
    tracks = [track_for(layers[lid], sorted(s, key=lambda x: x["start"])) for lid, s in segs.items()]
    return {"schema": ANIM_SCHEMA, "kind": "studio-animation", "id": f"{design_id}-{slug(name, 30)}-{int(datetime.now().timestamp())}",
            "name": name, "compositionId": design_id, "origin": "composed", "createdAt": NOW(), "duration": total, "fps": 25,
            "source": {"app": "build-library", "design": comp.get("name"), "layerCount": len(comp["layers"]),
                       "steps": steps}, "tracks": tracks}


# ── build ───────────────────────────────────────────────────────────────

def analyse(comp: dict, design_id: str, project: str, comp_file: Path, projects_dir: Path, check_files: bool) -> tuple[dict, list]:
    brand = brand_of(comp)
    raw_layers = [l for l in comp.get("layers", []) if isinstance(l, dict) and l.get("id")]
    sizes = [((l.get("font") or {}).get("sizePct") or 0) for l in raw_layers if l.get("type") == "text"]
    top = max(sizes, default=0)
    layers = []
    for l in sorted(raw_layers, key=lambda x: x.get("z", 0)):
        row = {"id": l["id"], "name": l.get("name", ""), "type": l.get("type", "image"), "kind": layer_kind(l),
               "z": l.get("z", 0), "box": box_of(l), "window": windows(l), "_raw": l,
               "size": (l.get("font") or {}).get("sizePct") or 0}
        if row["type"] == "text":
            row["text"] = (l.get("text") or "").strip()
            row["entity"] = classify_text(l, top, brand["name"])
            row["font"] = {"family": (l.get("font") or {}).get("family"), "sizePct": row["size"], "color": l.get("color")}
        else:
            row["url"] = l.get("url", "")
        layers.append(row)
    menu = detect_menu_items(layers)
    groups = build_groups(layers, menu)
    kind, tags = infer_kind(comp, groups, menu, layers)

    # health
    issues, level = [], 0  # 0 ready, 1 review, 2 blocked
    def flag(sev, msg):
        nonlocal level
        level = max(level, sev); issues.append({"severity": ["info", "review", "blocked"][sev], "message": msg})
    facts = comp.get("sourceFacts") or {}
    layout = (facts.get("stageMetadata") or {}).get("layout")
    if layout == "css-estimated": flag(1, "Layout was estimated from CSS, not measured in Chrome.")
    for w in facts.get("warnings") or []:
        if re.search(r"could not|missing|failed|skipped|unavailable", w, re.I): flag(1, w[:160])
    if not layers: flag(2, "The composition has no layers.")
    assets_out = []
    for l in layers:
        raw = l["_raw"]
        if (raw.get("w") or 0) <= 0 or (raw.get("h") or 0) <= 0: flag(2, f"{l['id']} has no size.")
        b = l["box"]
        if l["kind"] != "photo" and not (l["type"] == "text" and b["w"] > 150) and (
                b["x"] < -.5 or b["y"] < -.5 or b["x"] + b["w"] > 100.5 or b["y"] + b["h"] > 100.5):
            flag(1, f"{l['id']} extends past the stage.")
        if l["type"] == "image":
            state, f = resolve_asset(l["url"], projects_dir)
            if state == "missing" and check_files: flag(2, f"Image file missing for {l['id']}.")
            assets_out.append((l, state, f))
    comp_view = {"id": design_id, "name": comp.get("name"), "layers": raw_layers, "duration": comp.get("duration", 10000)}
    orig = original_animation(comp_view, design_id)
    ov = validate_animation(comp_view, orig, design_id)
    if ov["status"] == "broken": flag(2, "Original motion is not usable: " + "; ".join(ov["issues"][:2]))
    elif ov["status"] == "warn": flag(1, "Original motion has notes: " + "; ".join(ov["issues"][:2]))
    status = ["ready", "review", "blocked"][level]

    for l in layers:
        l.pop("_raw"); l.pop("size", None)
    ents = {}
    for l in layers:
        if l["type"] == "text" and l["entity"] not in ("text", "title", "headline", "description", "label", "emoji"):
            ents.setdefault(l["entity"], []).append(l["text"])
    detail = {"schema": SCHEMA, "id": design_id, "project": project, "name": comp.get("name"), "brand": brand,
              "kind": kind, "tags": tags, "width": comp.get("width"), "height": comp.get("height"),
              "aspect": aspect_name(comp.get("width") or 1, comp.get("height") or 1), "duration": comp.get("duration", 10000),
              "composition": f"/data/projects/{project}/compositions/{comp_file.name}", "status": status, "issues": issues,
              "layers": layers, "groups": groups, "menuItems": menu, "entities": ents, "builtAt": NOW()}
    return detail, [(orig, ov), assets_out, comp_view]


def build(projects_dir: Path, library_dir: Path, only: list[str] | None = None, check_files: bool = True) -> dict:
    designs, menu_all, brands, assets, clips = [], {}, {}, {}, {}
    for name in PRESET_NAMES:
        p = preset(name); clips[p["id"]] = {**p, "uses": 0}
    comp_files = sorted(projects_dir.glob("*/compositions/*.composition.json"))
    if only:
        say(f"library: indexing {len(comp_files)} composition(s); {len(only)} just updated")
    for cf in comp_files:
        comp = read_json(cf)
        if not isinstance(comp, dict) or not isinstance(comp.get("layers"), list):
            continue
        project = cf.parent.parent.name
        stem = cf.name[: -len(".composition.json")]
        design_id = stem if stem == project else f"{project}--{stem}"
        try:
            detail, (orig, ov), assets_out, comp_view = None, (None, None), [], None
            detail, extra = analyse(comp, design_id, project, cf, projects_dir, check_files)
            (orig, ov), assets_out, comp_view = extra
        except Exception as exc:  # one bad composition must not hide the rest
            say(f"warn: library skipped {cf.name}: {exc}")
            continue
        adir = library_dir / "animations" / design_id
        write_json(adir / "original.animation.json", orig)
        anims = [{"id": orig["id"], "name": orig["name"], "origin": "original", "file": f"/data/library/animations/{design_id}/original.animation.json",
                  "duration": orig["duration"], "tracks": len(orig["tracks"]), **ov}]
        extra_files = sorted(adir.glob("*.json")) + sorted((projects_dir / project / "animations").glob("*.json") if (projects_dir / project / "animations").is_dir() else [])
        for af in extra_files:
            if af.name == "original.animation.json": continue
            a = read_json(af)
            if not isinstance(a, dict): continue
            v = validate_animation(comp_view, a, design_id)
            anims.append({"id": a.get("id") or af.stem, "name": a.get("name") or af.stem, "origin": a.get("origin") or "studio",
                          "file": f"/data/library/animations/{design_id}/{af.name}" if af.parent == adir else f"/data/projects/{project}/animations/{af.name}",
                          "duration": a.get("duration"), "tracks": len(a.get("tracks") or []), **v})
        # clips from every original track
        by_layer = {l["id"]: l for l in comp_view["layers"]}
        gkind = {lid: g["kind"] for g in detail["groups"] for lid in g["layerIds"]}
        lkind = {l["id"]: l["kind"] for l in detail["layers"]}
        for l in comp_view["layers"]:
            for ev in clip_events(l):
                c = clip_from_event(ev)
                if not c: continue
                e = clips.setdefault(c["id"], {**c, "uses": 0, "appliesTo": [], "examples": []})
                e["uses"] += 1
                for k in (lkind.get(l["id"]), gkind.get(l["id"])):
                    if k and k not in e["appliesTo"]: e["appliesTo"].append(k)
                if len(e["examples"]) < 6:
                    e["examples"].append({"design": design_id, "layer": l["id"], "start": ev[0]["time"]})
        write_json(library_dir / "designs" / f"{design_id}.json", {**detail, "animations": anims})
        thumb = next((l["url"] for l in detail["layers"] if l["kind"] == "photo" and l.get("url")), "")
        designs.append({"id": design_id, "name": detail["name"], "brandId": detail["brand"]["id"], "kind": detail["kind"],
                        "tags": detail["tags"], "aspect": detail["aspect"], "width": detail["width"], "height": detail["height"],
                        "duration": detail["duration"], "layers": len(detail["layers"]), "groups": len(detail["groups"]),
                        "status": detail["status"], "issues": len(detail["issues"]), "thumbnail": thumb,
                        "composition": detail["composition"], "detail": f"/data/library/designs/{design_id}.json",
                        "menuItems": len(detail["menuItems"]),
                        "animations": [{k: a[k] for k in ("id", "name", "origin", "file", "duration", "status")} for a in anims]})
        b = brands.setdefault(detail["brand"]["id"], {"id": detail["brand"]["id"], "name": detail["brand"]["name"], "designs": [],
                                                      "contacts": {}, "offers": [], "hours": [], "hashtags": [], "taglines": [],
                                                      "palette": [], "fonts": [], "logos": []})
        b["designs"].append(design_id)
        for ent, vals in detail["entities"].items():
            key = {"phone": "phones", "url": "urls", "email": "emails", "handle": "handles", "address": "addresses"}.get(ent)
            tgt = b["contacts"].setdefault(key, []) if key else b.setdefault({"offer": "offers", "hours": "hours", "hashtag": "hashtags"}.get(ent, "other"), [])
            for v in vals:
                if v not in tgt: tgt.append(v)
        for l in detail["layers"]:
            if l["type"] == "text" and l["entity"] == "headline" and l["text"] not in b["taglines"] and len(b["taglines"]) < 12:
                b["taglines"].append(l["text"])
        kit = comp.get("brandKit") or {}
        for p in kit.get("palette") or []:
            if p.get("hex") and p["hex"] not in b["palette"]: b["palette"].append(p["hex"])
        for f in kit.get("fonts") or []:
            if f.get("family") and f["family"] not in b["fonts"]: b["fonts"].append(f["family"])
        for lg in kit.get("logos") or []:
            if lg.get("url") and lg["url"] not in b["logos"]: b["logos"].append(lg["url"])
        for it in detail["menuItems"]:
            key = f"{detail['brand']['id']}:{norm(it['name'])}"
            m = menu_all.setdefault(key, {"id": slug(key, 70), "brandId": detail["brand"]["id"], "name": it["name"],
                                          "description": it["description"], "price": it["price"], "sources": [],
                                          "category": None, "confidence": it["confidence"]})
            m["sources"].append({"design": design_id, **it["layerIds"]})
        for l, state, f in assets_out:
            aid, size, dims = None, None, None
            if f is not None and f.is_file():
                data = f.read_bytes(); aid = hashlib.sha1(data).hexdigest()[:16]; size = len(data)
                try:
                    from PIL import Image  # type: ignore
                    with Image.open(f) as im: dims = list(im.size)
                except Exception:
                    pass
            aid = aid or "u-" + hashlib.sha1(l["url"].encode()).hexdigest()[:14]
            a = assets.setdefault(aid, {"id": aid, "kind": l["kind"], "url": l["url"], "state": state, "bytes": size,
                                        "size": dims, "brandId": detail["brand"]["id"], "uses": []})
            a["uses"].append({"design": design_id, "layer": l["id"], "group": (l.get("groups") or [None])[0]})

    for c in clips.values():
        c.setdefault("appliesTo", ["*"])
    facets = {"kinds": {}, "aspects": {}, "status": {}, "brands": {}}
    for d in designs:
        for k, v in (("kinds", d["kind"]), ("aspects", d["aspect"]), ("status", d["status"]), ("brands", d["brandId"])):
            facets[k][v] = facets[k].get(v, 0) + 1
    ready = [d for d in designs if d["status"] == "ready"]
    index = {"schema": SCHEMA, "generatedAt": NOW(), "counts": {"designs": len(designs), "ready": len(ready),
             "brands": len(brands), "menuItems": len(menu_all), "assets": len(assets), "clips": len(clips)},
             "facets": facets, "files": {"business": "/data/library/business.json", "menu": "/data/library/menu.json",
             "assets": "/data/library/assets.json", "clips": "/data/library/clips.json"},
             "rules": "Show only designs with status 'ready' and animations with status 'ok'.", "designs": designs}
    write_json(library_dir / "business.json", {"schema": SCHEMA, "brands": list(brands.values())})
    write_json(library_dir / "menu.json", {"schema": SCHEMA, "items": list(menu_all.values())})
    write_json(library_dir / "assets.json", {"schema": SCHEMA, "assets": list(assets.values())})
    write_json(library_dir / "clips.json", {"schema": SCHEMA, "clips": sorted(clips.values(), key=lambda c: (-c["uses"], c["id"]))})
    write_json(library_dir / "index.json", index)
    return index


def save_animation(library_dir: Path, projects_dir: Path, design_id: str, anim: dict) -> dict:
    """Validate and store an animation (Studio / LLM / composed) against one design."""
    detail = read_json(library_dir / "designs" / f"{design_id}.json")
    if not detail: raise ValueError(f"unknown design {design_id}")
    comp_path = projects_dir / detail["project"] / "compositions" / Path(detail["composition"]).name
    comp = read_json(comp_path)
    comp_view = {"id": design_id, "name": comp.get("name"), "layers": comp["layers"], "duration": comp.get("duration", 10000)}
    anim = {**anim, "schema": ANIM_SCHEMA, "compositionId": design_id}
    anim.setdefault("origin", "studio"); anim.setdefault("createdAt", NOW())
    anim["id"] = anim.get("id") or f"{design_id}-{slug(anim.get('name', 'animation'), 30)}"
    v = validate_animation(comp_view, anim, design_id)
    if v["status"] == "broken": raise ValueError("; ".join(v["issues"][:4]))
    path = library_dir / "animations" / design_id / f"{slug(anim['id'], 80)}.animation.json"
    write_json(path, anim)
    return {"file": f"/data/library/animations/{design_id}/{path.name}", "id": anim["id"], **v}


def compose_design(library_dir: Path, projects_dir: Path, design_id: str, steps: list[dict],
                   name: str = "Composed animation", save: bool = False) -> dict:
    detail = read_json(library_dir / "designs" / f"{design_id}.json")
    if not detail: raise ValueError(f"unknown design {design_id}")
    comp = read_json(projects_dir / detail["project"] / "compositions" / Path(detail["composition"]).name)
    comp_view = {"id": design_id, "name": comp.get("name"), "layers": comp["layers"], "duration": comp.get("duration", 10000)}
    clips = {c["id"]: c for c in read_json(library_dir / "clips.json", {"clips": []})["clips"]}
    anim = compose(detail, comp_view, steps, clips, name, design_id=design_id)
    check = validate_animation(comp_view, anim, design_id)
    if save and check["status"] != "broken":
        return {"animation": anim, **save_animation(library_dir, projects_dir, design_id, anim)}
    return {"animation": anim, **check}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build"); b.add_argument("--projects", default="data/projects"); b.add_argument("--library", default="data/library")
    b.add_argument("--skip-file-checks", action="store_true")
    c = sub.add_parser("compose"); c.add_argument("--library", default="data/library"); c.add_argument("--projects", default="data/projects")
    c.add_argument("--design", required=True); c.add_argument("--steps", required=True, type=Path); c.add_argument("--name", default="Composed animation")
    c.add_argument("--save", action="store_true")
    v = sub.add_parser("validate"); v.add_argument("--library", default="data/library"); v.add_argument("--projects", default="data/projects")
    v.add_argument("--design", required=True); v.add_argument("--animation", required=True, type=Path)
    args = ap.parse_args(argv)
    if args.cmd in (None, "build"):
        args = args if args.cmd else ap.parse_args(["build"])
        idx = build(Path(args.projects), Path(args.library), check_files=not args.skip_file_checks)
        n = idx["counts"]
        say(f"library {Path(args.library)}")
        say(f"library: {n['designs']} design(s) ({n['ready']} ready), {n['menuItems']} menu item(s), {n['assets']} asset(s), {n['clips']} clip(s)")
        return 0
    lib, proj = Path(args.library), Path(args.projects)
    if args.cmd == "validate":
        detail = read_json(lib / "designs" / f"{args.design}.json") or sys.exit(f"unknown design {args.design}")
        comp = read_json(proj / detail["project"] / "compositions" / Path(detail["composition"]).name)
        comp_view = {"id": args.design, "name": comp.get("name"), "layers": comp["layers"], "duration": comp.get("duration", 10000)}
        print(json.dumps(validate_animation(comp_view, read_json(args.animation), args.design), indent=1)); return 0
    res = compose_design(lib, proj, args.design, json.loads(args.steps.read_text(encoding="utf-8")), args.name, args.save)
    print(json.dumps(res if args.save else res["animation"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())