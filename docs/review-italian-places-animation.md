# Review — Italian places reels: animation fidelity

**Page:** `html-examples/Singl-end · Italian places reels.html` (2 stages, 1080×1920, 12 s / 10 s)
**Report:** [examples/singl-end-italian-places-reels.md](examples/singl-end-italian-places-reels.md)
**Reviewed:** after the first extraction pass, which reproduced the layout but froze the
camera, minimap, dots and route. This review covers the page's animation model and what is
now extracted.

## The page's animation model

Both stages are driven by the page's own `f(t)` loop, not by CSS transitions alone.

### Stage A — spotlight tour (`data-k="a"`, 12 s)

* **Camera.** A `data-cam` waypoint list of `[time, fx, fy, zoom, --ry]` tuples with
  smoothstep between waypoints. Each frame sets `background-position` from `fx`, a
  `translateY`/`scale` on `.cam`, `transform-origin: 50% fy`, and the stage's `--ry`
  (used by the spotlight gradient and the ring).
* **Spotlight.** `.sp` is a radial gradient centred at `50% calc(--ry * 1cqw)`; it follows
  the camera's vertical focus.
* **Ring.** `.rg` is a 26 cqw white ring at the same `--ry` centre; its `::after` is a
  pulsing second ring.
* **Minimap.** `.mm` shows the whole photo; `.mr` is the camera's viewport rectangle:
  `left/width` from `fx` and zoom, `top/height` from the camera's stage window.
* **Location dots.** Four `.md` dots; `.md.on` turns the current one yellow and scales it
  1.5×. The active index changes at **0.6, 3.0, 5.4 and 7.8 s** (T in the page).
* **Counter.** `.ct` reads `1/4` … `4/4` from the same table.
* **Cards.** Four `.cd` cards with `data-s`/`data-e` windows (0.8–3.1, 3.2–5.5, 5.6–7.9,
  8.0–10.1) and an end card `.out` (10.3–11.6).

### Stage B — animated route (`data-k="b"`, 10 s)

* **Route.** The SVG path length is measured with `getTotalLength()`; `stroke-dasharray`/
  `stroke-dashoffset` draw it between the four stops. The page's timeline holds each stop
  (1.6–2.4, 4.0–4.9, 6.4–7.2, 8.6–10 s) and moves between them with smoothstep.
* **Scooter.** `.mv` is positioned with `getPointAtLength(len)` each frame.
* **Pins/captions.** Each `.pn`/`.cp` becomes `.on` when the route passes its stop.
* **Board.** The departures board is always visible; `.rw.on` highlights the active row.
* **End card.** `.end` appears at the end.

## What is extracted now

The measurement harness takes a **16-sample pass** across the reel, calling the page's own
`f(t)` (falling back to `--p`/`.on` for pages without one) and recording every node's box,
content box, effective opacity, text, colour and resolved background. The extractor then
builds:

| channel | representation | verified |
|---|---|---|
| camera flight | `html-bg` box keyframes from the sampled `.cam` paint box | ✓ position/zoom follow the page |
| minimap viewport | `.mr` shape with 16 box keyframes | ✓ left/width/top/height |
| location dots | 4 base dots + 4 yellow `active` variants with on/off windows | ✓ 2/4 etc. |
| counter | one text layer per value (`1/4`…`4/4`) with windows | ✓ |
| cards / end card | box + opacity keyframes from the samples | ✓ windows, entrance offset |
| spotlight | 16 gradient frames at the sampled `--ry` centre | ✓ follows the camera |
| ring | `.rg` circle with 16 box keyframes | ✓ moves with `--ry` |
| route | 12 sample-rendered SVG frames with hard-cut windows | ✓ draws progressively |
| scooter | text layer with 16 box keyframes along the path | ✓ |
| pins / captions | shape+text layers with sampled opacity windows | ✓ appear as the route passes |
| end card (B) | opacity keyframes | ✓ |

The comparison renderer now advances the **truth** page to the same absolute time as the
extracted side, so both show the same frame (previously the truth stayed at t=0, which made
the camera/dots/route look wrong even when they were correct).

## Remaining approximations

1. **Step frames.** The route (12 frames / 10 s) and the spotlight (16 frames / 12 s) are
   flipbooks, not continuous strokes, so they advance in ~0.8 s steps.
2. **16-sample interpolation.** The camera keyframes are sampled every 0.75 s and
   interpolated linearly; the page uses smoothstep between waypoints, so the motion is
   slightly straighter between samples.
3. **Pulse ring.** `.rg::after` (the expanding second ring) is a pseudo-element and is not
   emitted; only the main ring is.
4. **Board highlight.** `.rw.on` swaps the row to a gold background and navy text; the row
   container is transparent at rest, so only the row text is emitted — the highlight is not.
5. **Shadows.** The ring and card glows are `box-shadow`/`filter: drop-shadow`; not
   reproduced.
6. **Route frames** are full-stage rasters of the SVG (transparent), so the line is not
   editable geometry.

## Reproduce

```bash
.venv/bin/python tools/measure-html-chrome.py \
  --html "html-examples/Singl-end · Italian places reels.html" \
  --state on --stage-width 1080 --samples 16 --out /tmp/italy.json
.venv/bin/python tools/html-to-projects.py \
  --html "html-examples/Singl-end · Italian places reels.html" --preview-only
.venv/bin/python tools/render-html-example.py \
  --html "html-examples/Singl-end · Italian places reels.html" \
  --out docs/examples/img/singl-end-italian-places-reels.png
```
