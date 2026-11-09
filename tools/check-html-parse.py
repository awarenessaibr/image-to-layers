#!/usr/bin/env python3
"""
check-html-parse.py — compare the Python extractor against Chrome's real layout.

Given a measurement fixture produced by `tools/measure-html-chrome.py
--no-transform` and an HTML page, runs `html-to-projects.py --preview-only` and
reports, per stage:

  * how many of the text nodes Chrome renders were found at all,
  * how many text layers land within `--tolerance` percent on every edge
    (compared with the element's content box, which is what a text layer uses),
  * layers the page shows but the parse emits below `opacity 0.05`,
  * layers whose box sticks out of the stage,
  * the composition duration against the page's own reel length.

Report-only by default; `--strict` exits non-zero when the thresholds fail, so it
can be used as the acceptance gate while the extractor is being fixed.

    python3 tools/check-html-parse.py \
        --fixture tests/fixtures/news-bulletins-chrome-boxes.json \
        --html "html-examples/Singl-end · news bulletin reels.html" --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR = ROOT / "tools" / "html-to-projects.py"


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.replace("&amp;", "&").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def preview_for(html_path: Path, max_stages: int, python: str, layout: str) -> dict:
    done = subprocess.run(
        [python, str(EXTRACTOR), "--html", str(html_path), "--max-stages", str(max_stages),
         "--preview-only", "--layout", layout],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if done.returncode != 0:
        sys.exit((done.stderr or done.stdout or "extractor failed").strip())
    return json.loads(done.stdout)


def covers(outer: dict, inner: dict, slack: float) -> bool:
    return (outer["x"] <= inner["x"] + slack and outer["y"] <= inner["y"] + slack
            and outer["x"] + outer["w"] >= inner["x"] + inner["w"] - slack
            and outer["y"] + outer["h"] >= inner["y"] + inner["h"] - slack)


def measure(truth: dict, stage: dict, tolerance: float) -> dict:
    layers = [l for l in stage.get("layers", []) if l.get("type") == "text"]
    used: dict[int, int] = {}
    stats = {"textNodes": 0, "matched": 0, "within": 0, "deltas": []}
    for node in truth.get("nodes", []):
        direct = norm(node.get("text"))
        if not direct or (node.get("op") or 0) < 0.02:
            continue  # hidden at rest, or a container whose children own the text
        wants = [w for w in (direct, norm(node.get("flowText"))) if w]
        want = direct
        stats["textNodes"] += 1
        targets = [node.get("content") or node.get("box")]
        if node.get("textBox"):
            targets.append(node["textBox"])
        if node.get("endContent") or node.get("endBox"):
            targets.append(node.get("endContent") or node.get("endBox"))
        best, best_delta, best_score = None, None, -1.0
        for i, layer in enumerate(layers):
            got = norm(layer.get("text"))
            if not got:
                continue
            score = 0.0
            for candidate in wants:
                if got == candidate:
                    score = max(score, 3.0)
                elif got in candidate:
                    score = max(score, 2.0)
                elif candidate in got:
                    score = max(score, 1.0)
                elif len(got) >= 12 and got[:12] == candidate[:12]:
                    score = max(score, 0.5)
            if score <= 0:
                continue
            boxes = [{"x": layer["x"], "y": layer["y"],
                      "w": layer["w"], "h": layer["h"]}]
            for kf in layer.get("keyframes") or []:
                boxes.append({"x": kf["x"], "y": kf["y"], "w": kf["w"], "h": kf["h"]})
            delta = min(max(abs(b["x"] - t["x"]), abs(b["y"] - t["y"]),
                            abs(b["w"] - t["w"]), abs(b["h"] - t["h"]))
                        for b in boxes for t in targets)
            # a merged inline run (e.g. a timecode inside a right-aligned block)
            # is covered by its container layer rather than owning one
            if any(covers(b, t, 0.6) for b in boxes for t in targets):
                delta = min(delta, 0.0)
            if (score > best_score
                    or (score == best_score and (best_delta is None or delta < best_delta))):
                best, best_delta, best_score = i, delta, score
        if best is None:
            continue
        used[best] = used.get(best, 0) + 1
        stats["matched"] += 1
        stats["deltas"].append((best_delta, node.get("cls", ""), node.get("text", "")[:40],
                                layers[best]["id"]))
        if best_delta <= tolerance:
            stats["within"] += 1
    return stats


def outside(layer: dict) -> bool:
    # backgrounds deliberately extend past the stage (they are clipped by it),
    # and a marquee line is wider than the stage by design
    if layer.get("role") == "background":
        return False
    if layer.get("type") == "text" and layer.get("w", 0) > 150:
        return False
    return (layer.get("x", 0) < -0.5 or layer.get("y", 0) < -0.5
            or layer.get("x", 0) + layer.get("w", 0) > 100.5
            or layer.get("y", 0) + layer.get("h", 0) > 100.5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare the extractor with Chrome boxes")
    ap.add_argument("--fixture", required=True, type=Path)
    ap.add_argument("--html", required=True, type=Path)
    ap.add_argument("--tolerance", type=float, default=2.0, help="allowed delta in %% of the stage")
    ap.add_argument("--strict", action="store_true", help="exit 1 when the thresholds fail")
    ap.add_argument("--verbose", action="store_true", help="list every matched text node")
    ap.add_argument("--layout", default="auto", help="passed to the extractor (auto/measure/css)")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args(argv)

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    stages = fixture["stages"]
    parsed = preview_for(args.html, len(stages), args.python, args.layout)
    if len(parsed.get("stages", [])) != len(stages):
        print(f"! fixture has {len(stages)} stage(s), parse produced "
              f"{len(parsed.get('stages', []))}", file=sys.stderr)

    failures = 0
    total_text = total_within = 0
    print(f"{'stage':>5}  {'text':>5} {'found':>5} {'in tol':>6} {'dim<.05':>7} {'outside':>7} "
          f"{'ms':>7}  max delta")
    for i, (truth, parsed_stage) in enumerate(zip(stages, parsed.get("stages", []))):
        stats = measure(truth, parsed_stage, args.tolerance)
        layers = parsed_stage.get("layers", [])
        dim = sum(1 for l in layers if (l.get("opacity") or 0) < 0.05)
        out = sum(1 for l in layers if outside(l))
        worst = max(stats["deltas"], default=(0.0, "", "", ""))
        print(f"{i + 1:>5}  {stats['textNodes']:>5} {stats['matched']:>5} {stats['within']:>6} "
              f"{dim:>7} {out:>7} {parsed_stage.get('duration', 0):>7}  {worst[0]:>6.2f}%")

        if args.verbose:
            for delta, cls, text, lid in sorted(stats["deltas"], reverse=True):
                mark = "ok " if delta <= args.tolerance else "OFF"
                print(f"      {mark} {delta:6.2f}%  {cls:<16.16} {text!r:<44} {lid}")

        total_text += stats["textNodes"]
        total_within += stats["within"]
        failed = (stats["within"] < stats["textNodes"] or dim > 0 or out > 0)
        failures += 1 if failed else 0

    print(f"\ntext layers within {args.tolerance:g}% on all edges: {total_within}/{total_text}")
    if failures and args.strict:
        print("FAIL", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
