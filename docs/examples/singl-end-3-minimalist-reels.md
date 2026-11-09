# Singl-end · three minimalist reels

> Quiet, text-light 10-second 9:16 loops: one line, one price, lots of space.

![browser vs extracted layers](img/singl-end-3-minimalist-reels.png)

**Source:** [`html-examples/Singl-end · 3 minimalist reels.html`](../../html-examples/Singl-end · 3 minimalist reels.html) · **Stages:** 3 · **Duration:** 10 s  
**Fixture:** [`tests/fixtures/examples/singl-end-3-minimalist-reels.json`](../../tests/fixtures/examples/singl-end-3-minimalist-reels.json) — Chrome at 1080 px, webfonts loaded  
**Layout:** measured in headless Chrome (`--layout measure`)

## Stages

| # | size | duration | layers | text | shapes | photos | text ≤2 % | paint |
|---|---|---|---|---|---|---|---|---|
| 1 | 1080×1920 | 10 s | 6 | 3 | 2 | 1 | 3/3 | 1/1 |
| 2 | 1080×1920 | 10 s | 6 | 3 | 2 | 1 | 3/3 | 2/2 |
| 3 | 1080×1920 | 10 s | 5 | 3 | 1 | 1 | 3/3 | 1/1 |

## How it was extracted

- 3 background layer(s) across all stages — a full-bleed photo with its pan/zoom keyframes, or the stage's own colour/gradient.
- 9 text layer(s) from measured content boxes; font, weight, colour, alignment and letter-spacing come from the computed style, and inline runs are merged into one layer.
- 5 shape layer(s) from background colour, borders, radius and per-side rules, including 1 CSS gradient(s) rendered as SVG (linear, radial or tiled).
- 3 photo layer(s) placed at their cover/contain paint box; clipped ones are cropped to the visible window and CSS filters are baked in.
- JS-driven progress variables are set to their resting value before measuring: `--p`.
- Reel length comes from the page's own `D = 10000` constant.
- Stage duration: 10 s; visibility uses the measured `data-s`/`data-e` windows (intersected with ancestors) with instant cuts.

## Design notes

A pans slowly across the cyclist scene, B frames the Merchant City arched doorways in an arch-shaped window (echoing the building), C opens a thin line into a widening band of the street. Prices are from your menu PDF. The Scene 1 sign still reads “Mon–Sun 9–5”, so check hours before publishing.

## Known gaps

- The arch window is masked correctly, but its `clip-path` wipe-in and the card's box shadow are not animated/reproduced.
- Entry/exit motion (slide, scale, spin) is represented by visibility windows and the background pan, not by the authored transitions.
- Decorative pseudo-elements (dots, halos, halftone overlays) are not measured; textual `::before` content is included.
- Text that the page injects from JS (live clocks, typed lines) is a snapshot from measurement time.

## Warnings

_None._

## Verify

```bash
python3 tools/audit-html-examples.py --only "3 minimalist reels" --no-measure
python3 tools/check-html-parse.py --fixture tests/fixtures/examples/singl-end-3-minimalist-reels.json \
  --html "html-examples/Singl-end · 3 minimalist reels.html" --verbose
```

