# Singl-end · 9:16 reel concepts

> Two 10-second looping reels, one per scene. Use Safe zones to see where Instagram’s interface will cover the frame.

![browser vs extracted layers](img/singl-end-9-16-reel-concepts.png)

**Source:** [`html-examples/Singl-end · 9_16 reel concepts.html`](../../html-examples/Singl-end · 9_16 reel concepts.html) · **Stages:** 2 · **Duration:** 10 s  
**Fixture:** [`tests/fixtures/examples/singl-end-9-16-reel-concepts.json`](../../tests/fixtures/examples/singl-end-9-16-reel-concepts.json) — Chrome at 1080 px, webfonts loaded  
**Layout:** measured in headless Chrome (`--layout measure`)

## Stages

| # | size | duration | layers | text | shapes | photos | text ≤2 % | paint |
|---|---|---|---|---|---|---|---|---|
| 1 | 1080×1920 | 10 s | 15 | 7 | 6 | 2 | 7/7 | 3/3 |
| 2 | 1080×1920 | 10 s | 15 | 7 | 7 | 1 | 7/7 | 1/1 |

## How it was extracted

- 2 background layer(s) across all stages — a full-bleed photo with its pan/zoom keyframes, or the stage's own colour/gradient.
- 14 text layer(s) from measured content boxes; font, weight, colour, alignment and letter-spacing come from the computed style, and inline runs are merged into one layer.
- 13 shape layer(s) from background colour, borders, radius and per-side rules, including 6 CSS gradient(s) rendered as SVG (linear, radial or tiled).
- 3 photo layer(s) placed at their cover/contain paint box; clipped ones are cropped to the visible window and CSS filters are baked in.
- JS-driven progress variables are set to their resting value before measuring: `--p`.
- Reel length comes from the page's own `D = 10000` constant.
- Stage duration: 10 s; visibility uses the measured `data-s`/`data-e` windows (intersected with ancestors) with instant cuts.

## Design notes

Reel 1 keeps the whole scene visible (blurred fill above and below), Reel 2 pans across the facade. Motion is built from your scenes’ timelines (light dim, sparkle, shine) as overlays; the cyclist can only truly ride in once the separate layer images referenced in the JSON are available. Still to confirm: the Scene 1 sign says “Mon–Sun 9–5”.

## Known gaps

- Entry/exit motion (slide, scale, spin) is represented by visibility windows and the background pan, not by the authored transitions.
- Decorative pseudo-elements (dots, halos, halftone overlays) are not measured; textual `::before` content is included.
- Text that the page injects from JS (live clocks, typed lines) is a snapshot from measurement time.

## Warnings

_None._

## Verify

```bash
python3 tools/audit-html-examples.py --only "9_16 reel concepts" --no-measure
python3 tools/check-html-parse.py --fixture tests/fixtures/examples/singl-end-9-16-reel-concepts.json \
  --html "html-examples/Singl-end · 9_16 reel concepts.html" --verbose
```

