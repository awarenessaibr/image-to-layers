# Example pages — extraction reports

Every page in `html-examples/` is rendered in headless Chrome, measured, turned into LayerStudio layers by `tools/html-to-projects.py`, and scored against the browser layout. Each report below shows the browser and the extracted layers side by side at the same timecode, the per-stage inventory, and what is still approximate.

The shared pipeline is described in [HTML → LayerStudio pipeline](../html-to-layers-pipeline.md).

| page | stages | layers | text ≤2 % | paint | report |
|---|---|---|---|---|---|
| [02-brutal](02-brutal.md) | 3 | 40 | 16/16 | 15/15 | [image](img/02-brutal.png) |
| [03-immersive](03-immersive.md) | 1 | 7 | 6/6 | 1/1 | [image](img/03-immersive.png) |
| [Alternatives · Heritage · Italian places · Pinocchio](alternatives-heritage-italian-places-pinocchio.md) | 3 | 127 | 50/50 | 67/67 | [image](img/alternatives-heritage-italian-places-pinocchio.png) |
| [butterfly-pig-cinematic-posts](butterfly-pig-cinematic-posts.md) | 4 | 218 | 72/72 | 31/31 | [image](img/butterfly-pig-cinematic-posts.png) |
| [Deep Thinkers · cinematic reels](deep-thinkers-cinematic-reels.md) | 3 | 93 | 21/21 | 4/4 | [image](img/deep-thinkers-cinematic-reels.png) |
| [Heritage Collection · ALT places reels](heritage-collection-alt-places-reels.md) | 3 | 100 | 48/49 | 24/24 | [image](img/heritage-collection-alt-places-reels.png) |
| [Heritage Collection · places reels](heritage-collection-places-reels.md) | 3 | 168 | 70/70 | 44/44 | [image](img/heritage-collection-places-reels.png) |
| [Heritage Collection · story reels](heritage-collection-story-reels.md) | 6 | 217 | 44/44 | 21/21 | [image](img/heritage-collection-story-reels.png) |
| [Heritage Collection · video posts](heritage-collection-video-posts.md) | 3 | 83 | 17/17 | 10/10 | [image](img/heritage-collection-video-posts.png) |
| [Melano · stop-motion style posts](melano-stop-motion-style-posts.md) | 3 | 12 | 0/16 (baked) | 3/3 | [image](img/melano-stop-motion-style-posts.png) |
| [Singl-end Pinocchio · ALT reels](singl-end-pinocchio-alt-reels.md) | 3 | 69 | 22/22 | 38/38 | [image](img/singl-end-pinocchio-alt-reels.png) |
| [Singl-end · 3 minimalist reels](singl-end-3-minimalist-reels.md) | 3 | 17 | 9/9 | 4/4 | [image](img/singl-end-3-minimalist-reels.png) |
| [Singl-end · 3 more reel styles](singl-end-3-more-reel-styles.md) | 3 | 32 | 14/14 | 15/15 | [image](img/singl-end-3-more-reel-styles.png) |
| [Singl-end · 9_16 reel concepts](singl-end-9-16-reel-concepts.md) | 2 | 30 | 14/14 | 4/4 | [image](img/singl-end-9-16-reel-concepts.png) |
| [Singl-end · before & after reels](singl-end-before-after-reels.md) | 3 | 42 | 22/23 | 13/13 | [image](img/singl-end-before-after-reels.png) |
| [Singl-end · Italian places reels](singl-end-italian-places-reels.md) | 2 | 132 | 52/52 | 27/27 | [image](img/singl-end-italian-places-reels.png) |
| [Singl-end · Le Regole della Nonna (3-episode series)](singl-end-le-regole-della-nonna-3-episode-series.md) | 3 | 102 | 57/57 | 36/36 | [image](img/singl-end-le-regole-della-nonna-3-episode-series.png) |
| [Singl-end · news bulletin reels](singl-end-news-bulletin-reels.md) | 3 | 66 | 34/34 | 23/23 | [image](img/singl-end-news-bulletin-reels.png) |
| [Singl-end · Tarantella posts](singl-end-tarantella-posts.md) | 3 | 41 | 17/18 | 20/20 | [image](img/singl-end-tarantella-posts.png) |
| [Singl-end · two social post concepts](singl-end-two-social-post-concepts.md) | 2 | 31 | 12/12 | 8/8 | [image](img/singl-end-two-social-post-concepts.png) |
| [Singl-end × Giovannin Senza Paura · reels](singl-end-giovannin-senza-paura-reels.md) | 3 | 95 | 41/41 | 31/31 | [image](img/singl-end-giovannin-senza-paura-reels.png) |
| [Singl-end × Pinocchio · storytelling reels](singl-end-pinocchio-storytelling-reels.md) | 3 | 55 | 25/25 | 19/19 | [image](img/singl-end-pinocchio-storytelling-reels.png) |

## Regenerating

```bash
# 1. fixtures + coverage audit (headless Chrome)
python3 tools/audit-html-examples.py -o docs/examples/_audit.json

# 2. browser-vs-extracted images
python3 - <<'PY'
import json, subprocess, sys
for page in json.load(open('docs/examples/_audit.json'))['pages']:
    subprocess.run([sys.executable, 'tools/render-html-example.py', '--html', page['file'],
                    '--out', f"docs/examples/img/{page['slug']}.png"], check=True)
PY

# 3. these reports
python3 tools/build-example-docs.py
```

Fixtures live in `tests/fixtures/examples/`; `tests/test_html_layout.py` checks them.
