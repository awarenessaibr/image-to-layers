#!/usr/bin/env python3
"""
audit-html-examples.py — coverage audit for the measured HTML extractor.

For one page (or every page in html-examples/):

  1. records a Chrome ground-truth fixture,
  2. runs the extractor,
  3. matches every visible text run and every painted box to an emitted layer,
  4. reports unmatched items, layer counts, warnings and timing.

    python3 tools/audit-html-examples.py                 # all examples
    python3 tools/audit-html-examples.py --only news     # substring filter
    python3 tools/audit-html-examples.py --no-measure -o /tmp/audit.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "html-examples"
FIXTURES = ROOT / "tests" / "fixtures" / "examples"
MEASURE = ROOT / "tools" / "measure-html-chrome.py"
EXTRACTOR = ROOT / "tools" / "html-to-projects.py"
CHECKER = ROOT / "tools" / "check-html-parse.py"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


h2p = load("h2p_audit", EXTRACTOR)
checker = load("check_audit", CHECKER)


def slug(name: str, n: int = 60) -> str:
    base = unicodedata.normalize("NFKD", name)
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:n] or "example"


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.replace("&amp;", "&").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def measure(page: Path, out: Path) -> dict:
    text = page.read_text(encoding="utf-8", errors="replace")
    style = re.search(r"<style[^>]*>(.*?)</style>", text, re.S)
    progress = h2p.detect_progress_vars(style.group(1) if style else text)
    cmd = [sys.executable, str(MEASURE), "--html", str(page), "--state", "on",
           "--stage-width", "1080", "--no-transform", "--out", str(out)]
    for var in progress:
        cmd += ["--var", f"{var}=1"]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "").strip()[-400:])
    data = json.loads(out.read_text(encoding="utf-8"))
    # a second pass keeps the transformed resting box, so rotated/translated
    # elements (a postcard rack) can be matched at either position
    end_out = out.with_suffix(".end.json")
    end_cmd = [c for c in cmd if c != "--no-transform"]
    end_cmd[end_cmd.index(str(out))] = str(end_out)
    done_end = subprocess.run(end_cmd, capture_output=True, text=True, timeout=300)
    if done_end.returncode == 0 and end_out.is_file():
        end_data = json.loads(end_out.read_text(encoding="utf-8"))
        end_by_path = {}
        for si, st in enumerate(end_data.get("stages") or []):
            for n in st.get("nodes") or []:
                end_by_path[(si, n["path"])] = n
        for si, st in enumerate(data.get("stages") or []):
            for n in st.get("nodes") or []:
                e = end_by_path.get((si, n["path"]))
                if e:
                    n["endBox"] = e["box"]
                    n["endContent"] = e.get("content")
        end_out.unlink(missing_ok=True)
    # fixtures are for geometry/text matching; the inline data: URLs are bulk
    data["urls"] = []
    out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return data


def preview(page: Path, max_stages: int) -> dict:
    done = subprocess.run(
        [sys.executable, str(EXTRACTOR), "--html", str(page), "--max-stages",
         str(max_stages), "--preview-only", "--layout", "auto"],
        capture_output=True, text=True, timeout=300,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "").strip()[-400:])
    return json.loads(done.stdout)


def painted_kind(node: dict) -> str | None:
    if node["tag"] == "img":
        return "image"
    if node.get("bgKind") == "url":
        return "image"
    alpha = h2p._colour_alpha(node.get("bg"))
    borders = (node.get("border") or {}).get("widths") or {}
    if alpha > 0.02 or max(borders.values() or [0]) > 0.1:
        return "shape"
    return None


def skipped_paths(nodes: list[dict]) -> set[str]:
    """Mirror the extractor's subtree skips (classes, display, opacity)."""
    skipped: list[str] = []
    out: set[str] = set()
    for node in nodes:
        path = node["path"]
        if any(path == p or path.startswith(p + ".") for p in skipped):
            out.add(path)
            continue
        if node["disp"] in ("none", "contents") or node["vis"] != "visible":
            skipped.append(path)
            out.add(path)
            continue
        if any(c in h2p.MEASURE_SKIP for c in node["cls"].split()):
            skipped.append(path)
            out.add(path)
            continue
        if node["op"] < 0.02:
            skipped.append(path)
            out.add(path)
    return out


def audit_stage(truth: dict, stage: dict, tolerance: float) -> dict:
    nodes = truth.get("nodes", [])
    skipped = skipped_paths(nodes)
    bg_node = h2p._pick_stage_background(nodes)
    bg_path = bg_node["path"] if bg_node else None
    layers = stage.get("layers", [])
    text_layers = [l for l in layers if l["type"] == "text"]
    image_layers = [l for l in layers if l["type"] == "image"]

    # the extractor emits a text layer for elements with their own direct text;
    # inline children are merged into that layer (or own one themselves)
    expected_text = [n for n in nodes
                     if n.get("text")
                     and n["path"] not in skipped and n["path"] != bg_path
                     and n["box"]["x"] < 100 and n["box"]["x"] + n["box"]["w"] > 0]
    expected_paint = []
    def on_stage(n: dict) -> bool:
        b = n["box"]
        return b["x"] < 100 and b["x"] + b["w"] > 0 and b["y"] < 100 and b["y"] + b["h"] > 0

    for n in nodes:
        if n["path"] in skipped or n["path"] == bg_path or not on_stage(n):
            continue
        kind = painted_kind(n)
        if not kind:
            continue
        alpha = h2p._colour_alpha(n.get("bg"))
        if (kind == "shape" and alpha > 0.02 and n["box"]["w"] >= 95 and n["box"]["h"] >= 95
                and not (n.get("timing") or {}).get("end")):
            continue  # permanent full-bleed colour is the stage background
        expected_paint.append((n, kind))

    stats = checker.measure(truth, stage, tolerance)
    matched_text_ids = {d[3] for d in stats["deltas"]}

    paint_matched, paint_missing = 0, []
    for node, kind in expected_paint:
        want = (node["tag"], tuple(node["cls"].split()))
        found = None
        for layer in image_layers:
            meta = layer.get("meta") or {}
            if (meta.get("tag"), tuple(meta.get("cssClasses") or [])) != want:
                continue
            boxes = [{"x": layer["x"], "y": layer["y"],
                      "w": layer["w"], "h": layer["h"]}]
            for kf in layer.get("keyframes") or []:
                boxes.append({"x": kf["x"], "y": kf["y"], "w": kf["w"], "h": kf["h"]})
            targets = [node["box"]]
            if node.get("endBox"):
                targets.append(node["endBox"])
            for box in boxes:
                for target in targets:
                    overlap = h2p._rect_intersect(box, target)
                    overlap_ratio = (overlap["w"] * overlap["h"]
                                     / max(1e-6, target["w"] * target["h"]))
                    if (max(abs(box[k] - target[k]) for k in "xywh") <= tolerance
                            or checker.covers(box, target, 0.8)
                            or checker.covers(target, box, 0.8)
                            or checker.covers(box, node["clip"], 0.8)
                            or (kind == "image" and overlap_ratio >= 0.5)):
                        found = layer
                        break
                if found:
                    break
            if not found and layer.get("keyframes"):
                found = layer  # an animated element has no single static box
                break
            if found:
                break
        if found:
            paint_matched += 1
        else:
            paint_missing.append({
                "tag": node["tag"], "cls": node["cls"],
                "box": node["box"], "kind": kind,
                "text": (node.get("text") or "")[:40],
            })

    svg_paths = [n["path"] for n in nodes if n["tag"] == "svg"]

    def baked(node: dict) -> bool:
        return any(node["path"] == p or node["path"].startswith(p + ".") for p in svg_paths)

    def layer_for_text(node: dict) -> dict | None:
        direct = norm(node.get("text"))
        if not direct:
            return None
        wants = [w for w in (direct, norm(node.get("flowText"))) if w]
        for layer in text_layers:
            got = norm(layer.get("text"))
            if got and any(got in w or w in got
                           or (len(got) >= 12 and got[:12] == w[:12]) for w in wants):
                return layer
        return None

    text_missing = []
    for node in expected_text:
        if baked(node):
            continue  # SVG text is part of the flattened artwork layer
        if layer_for_text(node) is None:
            text_missing.append({"tag": node["tag"], "cls": node["cls"],
                                 "box": node["box"], "text": node.get("text", "")[:40]})

    kinds = {"text": len(text_layers),
             "shape": sum(1 for l in image_layers if (l.get("meta") or {}).get("isShape")),
             "photo": sum(1 for l in image_layers if not (l.get("meta") or {}).get("isShape"))}
    return {
        "index": stage["index"],
        "width": stage["width"], "height": stage["height"],
        "duration": stage["duration"],
        "layout": (stage.get("metadata") or {}).get("layout"),
        "layers": kinds,
        "text": {"expected": len(expected_text), "within": stats["within"],
                 "matched": stats["matched"], "missing": text_missing},
        "paint": {"expected": len(expected_paint), "matched": paint_matched,
                  "missing": paint_missing},
        "warnings": stage.get("warnings", []),
    }


def audit_page(page: Path, out_dir: Path, tolerance: float, measure_fixture: bool) -> dict:
    name = page.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = out_dir / f"{slug(name)}.json"
    fixture = measure(page, fixture_path) if measure_fixture else json.loads(
        fixture_path.read_text(encoding="utf-8"))

    # count stages cheaply from the fixture, then parse
    parsed = preview(page, max(1, len(fixture["stages"])))
    stages = [audit_stage(f, p, tolerance)
              for f, p in zip(fixture["stages"], parsed["stages"])]
    return {
        "page": page.name,
        "file": str(page.relative_to(ROOT)),
        "slug": slug(name),
        "fixture": str(fixture_path.relative_to(ROOT)),
        "fonts": fixture.get("fontsStatus"),
        "stageCount": len(stages),
        "stages": stages,
        "totals": {
            "layers": sum(sum(s["layers"].values()) for s in stages),
            "textExpected": sum(s["text"]["expected"] for s in stages),
            "textWithin": sum(s["text"]["within"] for s in stages),
            "paintExpected": sum(s["paint"]["expected"] for s in stages),
            "paintMatched": sum(s["paint"]["matched"] for s in stages),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=Path, default=EXAMPLES)
    ap.add_argument("--fixtures", type=Path, default=FIXTURES)
    ap.add_argument("--only", default="", help="substring of the page name")
    ap.add_argument("--tolerance", type=float, default=2.0)
    ap.add_argument("--no-measure", action="store_true", help="reuse existing fixtures")
    ap.add_argument("-o", "--out", type=Path, help="write the audit JSON here")
    args = ap.parse_args(argv)

    pages = sorted(p for p in args.examples.glob("*.html") if args.only.lower() in p.name.lower())
    if not pages:
        sys.exit("no matching example pages")
    results = []
    for page in pages:
        print(f"· {page.name}", file=sys.stderr, flush=True)
        try:
            result = audit_page(page, args.fixtures, args.tolerance, not args.no_measure)
        except Exception as exc:  # keep going: one broken page must not hide the others
            result = {"page": page.name, "file": str(page.relative_to(ROOT)),
                      "slug": slug(page.stem), "error": str(exc)}
        results.append(result)
        if "error" in result:
            print(f"  ! {result['error']}", file=sys.stderr)
        else:
            t = result["totals"]
            print(f"  stages {result['stageCount']} · layers {t['layers']} · "
                  f"text {t['textWithin']}/{t['textExpected']} · "
                  f"paint {t['paintMatched']}/{t['paintExpected']}", file=sys.stderr)

    report = {"pages": results, "tolerance": args.tolerance}
    text = json.dumps(report, indent=1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
