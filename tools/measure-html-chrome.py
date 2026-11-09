#!/usr/bin/env python3
"""
measure-html-chrome.py — browser ground truth for an HTML template page.

Renders a page in headless Chrome and records, for every element inside each
stage: its layout box, text box, content box, clipping ancestor, computed
typography / colours / borders / background, transforms, and the effective
`data-s` / `data-e` window. This is what `html-to-projects.py` builds layers
from (and what its CSS-only fallback is scored against).

    python3 tools/measure-html-chrome.py \
        --html "html-examples/Singl-end · news bulletin reels.html" \
        --state on --var p=1 --var s=1 \
        --out tests/fixtures/news-bulletins-chrome-boxes.json

Options that matter:

  --state on        add `.on` to every `[data-s]` element, i.e. measure the state
                    the viewer actually sees (CSS defaults are the entry state).
  --state at-time   only elements inside their `data-s`/`data-e` window.
  --var p=1         set the `--p` custom property on each stage (JS-driven reels).
  --no-transform    disable transforms (measures unrotated layout boxes; the
                    transform is still reported so callers can re-apply it).

Needs only a Chrome/Chromium binary — no Python packages, no Node.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

JS = r"""
<pre id="__itl_measure" style="display:none">__START__ pending __END__</pre>
<script>
(() => {
  const START = "__START__", END = "__END__";
  const T = __TIME__, VARS = __VARS__, SELECTOR = __SELECTOR__, AT_TIME = __AT_TIME__;
  const NO_TF = __NO_TF__, STAGE_W = __STAGE_W__;
  // stop the page's own rAF loop so the JS-driven progress vars stop moving
  window.requestAnimationFrame = function () { return 0; };

  const pre = document.getElementById('__itl_measure');
  if (STAGE_W > 0) {
    const style = document.createElement('style');
    style.textContent = SELECTOR.split(',').map(s => s.trim() + '{width:' + STAGE_W +
      'px !important;max-width:none !important}').join('') +
      'html,body{overflow:visible !important}';
    (document.head || document.documentElement).appendChild(style);
  }
  if (NO_TF) {
    const style = document.createElement('style');
    style.textContent = '*,*::before,*::after{transform:none !important;transition:none !important;' +
                        'animation:none !important}';
    (document.head || document.documentElement).appendChild(style);
  }

  const num = (v) => Math.round(v * 100) / 100;
  const stages = [...document.querySelectorAll(SELECTOR)]
      .filter(el => !(el.parentElement && el.parentElement.closest(SELECTOR)));
  const urls = [];
  const urlId = (u) => {
    if (!u) return -1;
    let i = urls.indexOf(u);
    if (i < 0) { urls.push(u); i = urls.length - 1; }
    return i;
  };
  const firstUrl = (value) => {
    if (!value || value === 'none') return -1;
    const m = /url\(\s*['"]?(.*?)['"]?\s*\)/i.exec(value);
    return m ? urlId(m[1]) : -1;
  };

  const px = (v) => parseFloat(v) || 0;

  const tidy = (out) => out.replace(/[\u00a0]/g, ' ').replace(/[ \t]+/g, ' ')
                              .replace(/ *\n */g, '\n').trim();
  const directText = (el) => {
    let out = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.nodeType === 1 && n.tagName === 'BR') out += '\n';
    }
    return tidy(out);
  };
  const flowText = (el) => {
    let out = '';
    const kids = [];
    for (const n of el.childNodes) {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.nodeType === 1) {
        if (n.tagName === 'BR') { out += '\n'; continue; }
        const display = getComputedStyle(n).display;
        if (display === 'contents' || display.startsWith('inline')) {
          out += n.innerText || n.textContent || '';
          kids.push(n);
        }
      }
    }
    return { text: tidy(out), kids };
  };

  const rectPct = (r, sr) => ({
    x: num((r.left - sr.left) / sr.width * 100),
    y: num((r.top - sr.top) / sr.height * 100),
    w: num(r.width / sr.width * 100),
    h: num(r.height / sr.height * 100),
  });

  const textRange = (el, sr) => {
    const nodes = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
    if (!nodes.length) return null;
    const range = document.createRange();
    range.setStartBefore(nodes[0]);
    range.setEndAfter(nodes[nodes.length - 1]);
    const r = range.getBoundingClientRect();
    if (!r.width && !r.height) return null;
    return rectPct(r, sr);
  };

  const clipChain = (el, sr, stage) => {
    let box = { x: 0, y: 0, w: 100, h: 100 };
    let radius = [0, 0, 0, 0, 0, 0, 0, 0], seenClip = false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (cs.overflow !== 'visible' || cs.clipPath !== 'none') {
        const rect = p.getBoundingClientRect();
        const c = rectPct(rect, sr);
        if (!seenClip && p !== stage) {
          // radii only apply when the ancestor actually clips this element
          const r = [cs.borderTopLeftRadius, cs.borderTopRightRadius,
                     cs.borderBottomRightRadius, cs.borderBottomLeftRadius]
                    .map(v => parseFloat(v) || 0);
          radius = [num(r[0] / rect.width * 100), num(r[0] / rect.height * 100),
                    num(r[1] / rect.width * 100), num(r[1] / rect.height * 100),
                    num(r[2] / rect.width * 100), num(r[2] / rect.height * 100),
                    num(r[3] / rect.width * 100), num(r[3] / rect.height * 100)];
          seenClip = true;
        }
        const x0 = Math.max(box.x, c.x), y0 = Math.max(box.y, c.y);
        const x1 = Math.min(box.x + box.w, c.x + c.w), y1 = Math.min(box.y + box.h, c.y + c.h);
        box = { x: num(x0), y: num(y0), w: num(Math.max(0, x1 - x0)), h: num(Math.max(0, y1 - y0)) };
      }
    }
    return { box, radius };
  };

  const timingOf = (el, stage) => {
    let start = 0, end = Infinity;
    for (let p = el; p && p !== stage.parentElement; p = p.parentElement) {
      if (!p.dataset) continue;
      const s = p.dataset.s, e = p.dataset.e;
      if (s !== undefined && s !== '') start = Math.max(start, parseFloat(s) || 0);
      if (e !== undefined && e !== '') end = Math.min(end, parseFloat(e));
    }
    return { start: num(start), end: Number.isFinite(end) ? num(end) : null };
  };

  const pathOf = (el, stage) => {
    const parts = [];
    for (let p = el; p && p !== stage; p = p.parentElement) {
      const i = p.parentElement ? [...p.parentElement.children].indexOf(p) : 0;
      parts.unshift(i);
    }
    return parts.join('.');
  };

  const videoPoster = (el) => {
    try {
      if (el.tagName !== 'VIDEO' || el.readyState < 2 || !el.videoWidth) return -1;
      const c = document.createElement('canvas');
      c.width = el.videoWidth; c.height = el.videoHeight;
      c.getContext('2d').drawImage(el, 0, 0);
      return urlId(c.toDataURL('image/jpeg', 0.85));
    } catch (e) { return -1; }
  };

  const build = () => {
    stages.forEach(stage => {
      Object.entries(VARS).forEach(([k, v]) => stage.style.setProperty('--' + k, String(v)));
      if (VARS.p === undefined) stage.style.setProperty('--p', '0');
      if (VARS.s === undefined) stage.style.setProperty('--s', '1');
      stage.querySelectorAll('*').forEach(el => {
        el.style.transition = 'none';
        el.style.animation = 'none';
        const s = el.dataset ? el.dataset.s : undefined;
        if (s !== undefined && s !== '') {
          const start = parseFloat(s) || 0;
          const end = (el.dataset.e !== undefined && el.dataset.e !== '') ? parseFloat(el.dataset.e) : Infinity;
          if (!AT_TIME || (T >= start && T < end)) el.classList.add('on');
        }
      });
    });
    document.body.offsetHeight;

    const data = stages.map(stage => {
      const sr = stage.getBoundingClientRect();
      const ss = getComputedStyle(stage);
      return {
        widthPx: num(sr.width), heightPx: num(sr.height),
        background: ss.backgroundColor,
        backgroundImage: ss.backgroundImage === 'none' ? '' : ss.backgroundImage.slice(0, 900),
        backgroundSize: ss.backgroundSize,
        backgroundPosition: ss.backgroundPosition,
        dataD: (stage.dataset && stage.dataset.d) || null,
        classes: stage.getAttribute("class") || "",
        id: stage.id || "",
        nodes: [...stage.querySelectorAll('*')].map(el => {
          const cs = getComputedStyle(el);
          const box = rectPct(el.getBoundingClientRect(), sr);
          const clipInfo = clipChain(el, sr, stage);
          const clip = clipInfo.box;
          const borderW = {
            t: px(cs.borderTopWidth), r: px(cs.borderRightWidth),
            b: px(cs.borderBottomWidth), l: px(cs.borderLeftWidth),
          };
          const pad = {
            t: px(cs.paddingTop), r: px(cs.paddingRight),
            b: px(cs.paddingBottom), l: px(cs.paddingLeft),
          };
          const content = {
            x: num(box.x + (borderW.l + pad.l) / sr.width * 100),
            y: num(box.y + (borderW.t + pad.t) / sr.height * 100),
            w: num(Math.max(0, box.w - (borderW.l + borderW.r + pad.l + pad.r) / sr.width * 100)),
            h: num(Math.max(0, box.h - (borderW.t + borderW.b + pad.t + pad.b) / sr.height * 100)),
          };
          const flow = flowText(el);
          const fsz = px(cs.fontSize) || 16;
          const lh = cs.lineHeight === 'normal' ? fsz * 1.2 : px(cs.lineHeight);
          return {
            path: pathOf(el, stage),
            tag: el.tagName.toLowerCase(),
            cls: (el.getAttribute && el.getAttribute('class')) || '',
            id: el.id || '',
            text: directText(el), flowText: flow.text,
            inlineKids: flow.kids.map(k => pathOf(k, stage)),
            allText: (el.innerText || '').trim().slice(0, 160),
            box, content, textBox: textRange(el, sr), clip,
            clipRadius: clipInfo.radius,
            offset: [el.offsetWidth, el.offsetHeight],
            client: [el.clientWidth, el.clientHeight],
            clipped: !(Math.abs(clip.x - box.x) < 0.2 && Math.abs(clip.y - box.y) < 0.2 &&
                       Math.abs(clip.w - box.w) < 0.2 && Math.abs(clip.h - box.h) < 0.2),
            pos: cs.position, disp: cs.display, vis: cs.visibility, op: num(parseFloat(cs.opacity)),
            z: cs.zIndex, tf: cs.transform,
            bg: cs.backgroundColor, bgKind: cs.backgroundImage === 'none' ? 'none' :
                (cs.backgroundImage.startsWith('url(') ? 'url' : 'gradient'),
            bgImage: firstUrl(cs.backgroundImage), bgPos: cs.backgroundPosition, bgSize: cs.backgroundSize,
            bgGradient: cs.backgroundImage.startsWith('url(') || cs.backgroundImage === 'none'
                ? '' : cs.backgroundImage.slice(0, 600),
            imgSrc: el.tagName === 'IMG' ? urlId(el.currentSrc || el.src) : -1,
            videoSrc: el.tagName === 'VIDEO' ? videoPoster(el) : -1,
            imgW: el.tagName === 'IMG' ? el.naturalWidth : 0,
            imgH: el.tagName === 'IMG' ? el.naturalHeight : 0,
            objFit: cs.objectFit,
            font: {
              size: num(fsz), family: cs.fontFamily.split(',')[0].trim().replace(/^["']|["']$/g, ''),
              weight: cs.fontWeight, style: cs.fontStyle, lineHeight: num(lh),
              letterSpacing: cs.letterSpacing === 'normal' ? 0 : num(px(cs.letterSpacing)),
              align: cs.textAlign, transform: cs.textTransform, color: cs.color,
              whiteSpace: cs.whiteSpace,
            },
            border: {
              widths: borderW, topColor: cs.borderTopColor, leftColor: cs.borderLeftColor,
              topStyle: cs.borderTopStyle, leftStyle: cs.borderLeftStyle,
            },
            pad, radius: (() => {
              const v = cs.borderTopLeftRadius || "0px";
              if (v.endsWith("%")) return num(parseFloat(v) / 100 * Math.min(box.w / 100 * sr.width, box.h / 100 * sr.height));
              return px(v);
            })(),
            maskImage: (cs.maskImage && cs.maskImage !== 'none') ? cs.maskImage.slice(0, 400) : '',
            filter: cs.filter, before: getComputedStyle(el, '::before').content,
            beforeColor: getComputedStyle(el, '::before').color,
            after: getComputedStyle(el, '::after').content,
            dataS: el.dataset && el.dataset.s !== undefined ? parseFloat(el.dataset.s) : null,
            dataE: el.dataset && el.dataset.e !== undefined ? parseFloat(el.dataset.e) : null,
            timing: timingOf(el, stage),
          };
        }),
      };
    });
    pre.textContent = START + JSON.stringify({
      fontsStatus: document.fonts ? document.fonts.status : 'unknown',
      urls,
      stages: data,
    }) + END;
  };

  const waitVideos = () => Promise.all([...document.querySelectorAll('video')].map(v => new Promise(res => {
    if (v.readyState >= 2) { res(); return; }
    let done = false;
    const finish = () => { if (!done) { done = true; res(); } };
    v.addEventListener('loadeddata', finish, {once: true});
    v.addEventListener('canplay', finish, {once: true});
    setTimeout(finish, 3000);
  })));
  const start = () => setTimeout(build, 30);
  const boot = () => waitVideos().then(start, start);
  if (document.fonts && document.fonts.status !== 'loaded') {
    Promise.race([
      document.fonts.ready,
      new Promise(resolve => setTimeout(resolve, 8000)),
    ]).then(boot, boot);
  } else {
    boot();
  }
})();
</script>
</body>"""



SAMPLES_JS = r"""
<script>
(() => {
  const START = "__MS__", END = "__ME__", MARK = "__MARK__";
  const num = v => Math.round(v * 1000) / 1000;
  const px = v => parseFloat(v) || 0;
  const stages = [...document.querySelectorAll(__SELECTOR__)]
      .filter(el => !(el.parentElement && el.parentElement.closest(__SELECTOR__)));
  const pathOf = (el, root) => {
    const parts = [];
    let n = el;
    while (n && n !== root) {
      const p = n.parentElement;
      if (!p) return "";
      parts.push([...p.children].indexOf(n));
      n = p;
    }
    return parts.reverse().join(".");
  };
  const directText = el => [...el.childNodes].filter(n => n.nodeType === 3)
      .map(n => n.textContent).join("").replace(/\s+/g, " ").trim();
  const snap = stage => {
    const sr = stage.getBoundingClientRect();
    return [...stage.querySelectorAll("*")].map(el => {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        path: pathOf(el, stage),
        tag: el.tagName.toLowerCase(),
        cls: el.getAttribute("class") || "",
        text: directText(el),
        box: {
          x: num((r.left - sr.left) / sr.width * 100),
          y: num((r.top - sr.top) / sr.height * 100),
          w: num(r.width / sr.width * 100),
          h: num(r.height / sr.height * 100),
        },
        op: num(parseFloat(cs.opacity)),
        effOp: (() => { let o = 1, n = el; while (n) { o *= parseFloat(getComputedStyle(n).opacity) || 0; if (n === stage) break; n = n.parentElement; } return num(o); })(),
        content: (() => {
          const bt = px(cs.borderTopWidth), br = px(cs.borderRightWidth);
          const bb = px(cs.borderBottomWidth), bl = px(cs.borderLeftWidth);
          const pt = px(cs.paddingTop), pr = px(cs.paddingRight);
          const pb = px(cs.paddingBottom), pl = px(cs.paddingLeft);
          const x = (r.left - sr.left) / sr.width * 100 + (bl + pl) / sr.width * 100;
          const y = (r.top - sr.top) / sr.height * 100 + (bt + pt) / sr.height * 100;
          const w = r.width / sr.width * 100 - (bl + br + pl + pr) / sr.width * 100;
          const h = r.height / sr.height * 100 - (bt + bb + pt + pb) / sr.height * 100;
          return {x: num(Math.max(0, x)), y: num(Math.max(0, y)),
                  w: num(Math.max(0, w)), h: num(Math.max(0, h))};
        })(),
        bg: cs.backgroundColor,
        color: cs.color,
        bgImage: cs.backgroundImage === "none" ? "" : cs.backgroundImage.slice(0, 900),
        bgPos: cs.backgroundPosition,
        bgSize: cs.backgroundSize,
        tf: cs.transform,
        maskImage: (cs.maskImage && cs.maskImage !== "none") ? cs.maskImage.slice(0, 400) : "",
        clipPath: (cs.clipPath && cs.clipPath !== "none") ? cs.clipPath.slice(0, 400) : "",
        filter: cs.filter && cs.filter !== "none" ? cs.filter.slice(0, 400) : "",
        backface: cs.backfaceVisibility || "",
        so: num(parseFloat(cs.strokeDashoffset) || 0),
        sd: cs.strokeDasharray,
      };
    });
  };
  const N = __SAMPLES__, DEFAULT_D = __DEFAULT_D__, STAGE_W = __STAGE_W__, VARS = __VARS__;
  const out = [];
  stages.forEach((stage, si) => {
    stage.style.width = STAGE_W + "px";
    stage.style.maxWidth = "none";
    Object.entries(VARS).forEach(([k, v]) => stage.style.setProperty("--" + k, v));
    document.body.offsetHeight;
    const vid = stage.querySelector('video');
    const D = +((stage.dataset && stage.dataset.d)
                || (vid && vid.duration ? Math.round(vid.duration * 1000) : DEFAULT_D));
    const rows = [];
    for (let i = 0; i < N; i++) {
      const t = Math.min((D * i) / (N - 1) / 1000, D / 1000 - 0.001);
      stage.querySelectorAll('video').forEach(v => {
        try {
          v.pause();
          v.currentTime = Math.min(t, Math.max(0, (v.duration || 1) - 0.05));
        } catch (e) {}
      });
      try {
        const progress = D > 0 ? Math.min(1, Math.max(0, (t * 1000) / D)) : 0;
        // Every detected progress variable follows the same time ratio.
        Object.keys(VARS).forEach(k => stage.style.setProperty("--" + k, String(progress)));
        if (typeof f === "function" && typeof o !== "undefined" && o[stage.id] !== undefined) {
          f(o[stage.id] + t * 1000);
        } else {
          stage.style.setProperty("--p", String(progress));
          stage.querySelectorAll("[data-s]").forEach(e => e.classList.toggle(
            "on", t >= +e.dataset.s && t < +(e.dataset.e || 99)));
        }
      } catch (err) {}
      const sr = stage.getBoundingClientRect();
      rows.push({t: num(t), widthPx: num(sr.width), heightPx: num(sr.height), nodes: snap(stage)});
    }
    out.push({stage: si, duration: D, rows});
  });
  document.body.innerHTML = '<pre id="' + MARK + '">' + JSON.stringify({samples: out}) + '</pre>';
})();
</script>
"""

def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")
    if found:
        return found
    sys.exit("Chrome/Chromium not found; pass --chrome /path/to/binary")


def build_page(source: str, time_s: float, css_vars: dict[str, str], selector: str,
               start: str, end: str, base_dir: Path, at_time: bool, no_transform: bool,
               stage_width: int) -> str:
    js = (JS
          .replace("__START__", start)
          .replace("__END__", end)
          .replace("__TIME__", json.dumps(time_s))
          .replace("__VARS__", json.dumps(css_vars))
          .replace("__SELECTOR__", json.dumps(selector))
          .replace("__AT_TIME__", "true" if at_time else "false")
          .replace("__NO_TF__", "true" if no_transform else "false")
          .replace("__STAGE_W__", str(stage_width)))
    page = source.replace("</body>", js, 1) if "</body>" in source else source + js
    # keep relative images/fonts working when the instrumented copy lives in /tmp
    base = f'<base href="{base_dir.as_uri().rstrip("/")}/">'
    if re.search(r"<head[^>]*>", page, re.I):
        page = re.sub(r"(<head[^>]*>)", r"\1" + base, page, count=1, flags=re.I)
    else:
        page = base + page
    return page


def run(args) -> dict:
    html_path = Path(args.html).expanduser().resolve()
    if not html_path.is_file():
        sys.exit(f"html not found: {html_path}")
    source = html_path.read_text(encoding="utf-8", errors="replace")
    start, end = "__MEASURE_" + uuid.uuid4().hex, "END_" + uuid.uuid4().hex
    css_vars = {}
    for item in args.var or []:
        if "=" not in item:
            sys.exit(f"--var expects name=value, got {item!r}")
        k, v = item.split("=", 1)
        css_vars[k.strip().lstrip("-")] = v.strip()

    tmpdir = Path(tempfile.mkdtemp(prefix="itl-measure-"))
    try:
        page = build_page(source, args.time, css_vars, args.selector, start, end,
                          html_path.parent, args.state == "at-time", args.no_transform,
                          args.stage_width)
        page_path = tmpdir / "measure.html"
        page_path.write_text(page, encoding="utf-8")

        cmd = [find_chrome(args.chrome), "--headless=new", "--disable-gpu",
               "--hide-scrollbars", "--force-device-scale-factor=1",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions",
               "--disable-crash-reporter", "--mute-audio",
               f"--user-data-dir={tmpdir / 'profile'}",
               f"--window-size={args.width},{args.height}",
               f"--virtual-time-budget={args.budget}",
               "--dump-dom", page_path.as_uri()]
        if args.no_sandbox:
            cmd.insert(2, "--no-sandbox")

        for attempt in (1, 2, 3):
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
            dom = done.stdout or ""
            match = re.search(re.escape(start) + r"(.*?)" + re.escape(end), dom, re.S)
            if match:
                break
            if attempt == 1 and "--no-sandbox" not in cmd:
                cmd.insert(2, "--no-sandbox")  # CI / container sandboxes
                continue
            if attempt == 2 and "--headless=new" in cmd:
                cmd[cmd.index("--headless=new")] = "--headless"  # older Chrome builds
                continue
            tail = "\n".join((done.stderr or "").strip().splitlines()[-6:])
            sys.exit(f"Chrome produced no measurement (exit {done.returncode}).\n{tail}")

        payload = html_mod.unescape(match.group(1)).strip()
        measured = json.loads(payload)

        samples = None
        if args.samples > 0:
            mark = "ms" + uuid.uuid4().hex[:8]
            sample_src = source
            stub = "<script>window.requestAnimationFrame=function(){return 0};</script>"
            if re.search(r"<head[^>]*>", sample_src, re.I):
                sample_src = re.sub(r"(<head[^>]*>)", r"\1" + stub, sample_src, count=1, flags=re.I)
            js = (SAMPLES_JS
                  .replace("__START__", start).replace("__END__", end)
                  .replace("__SELECTOR__", json.dumps(args.selector))
                  .replace("__SAMPLES__", str(args.samples))
                  .replace("__STAGE_W__", str(args.stage_width or 1080))
                  .replace("__VARS__", json.dumps(css_vars))
                  .replace("__DEFAULT_D__", str(int(round(args.time * 1000)) or 10000))
                  .replace("__MARK__", mark))
            sample_src = sample_src.replace("</body>",
                "<style>*{transition:none!important}</style>" + js + "</body>", 1) \
                if "</body>" in sample_src else sample_src + js
            base = f'<base href="{html_path.parent.as_uri().rstrip("/")}/">'
            if re.search(r"<head[^>]*>", sample_src, re.I):
                sample_src = re.sub(r"(<head[^>]*>)", r"\1" + base, sample_src, count=1, flags=re.I)
            sample_path = tmpdir / "samples.html"
            sample_path.write_text(sample_src, encoding="utf-8")
            sample_cmd = list(cmd)
            sample_cmd[-1] = sample_path.as_uri()
            for attempt in (1, 2):
                done = subprocess.run(sample_cmd, capture_output=True, text=True, timeout=args.timeout)
                dom = done.stdout or ""
                match = re.search(r'<pre id="' + mark + r'">(.*?)</pre>', dom, re.S)
                if match and match.group(1).strip():
                    samples = json.loads(html_mod.unescape(match.group(1))).get("samples")
                    break
                if attempt == 1 and "--no-sandbox" not in sample_cmd:
                    sample_cmd.insert(2, "--no-sandbox")
                    continue
                break
        return {
            "samples": samples,
            "source": html_path.name,
            "sourceSha256": _sha256(html_path),
            "selector": args.selector,
            "state": args.state,
            "timeSeconds": args.time,
            "cssVars": css_vars,
            "noTransform": args.no_transform,
            "stageWidth": args.stage_width,
            "viewport": {"width": args.width, "height": args.height},
            "chrome": Path(cmd[0]).name,
            "fontsStatus": measured.get("fontsStatus"),
            "urls": measured.get("urls", []),
            "stages": measured.get("stages", []),
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Measure stage geometry with headless Chrome")
    ap.add_argument("--html", required=True)
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    ap.add_argument("--selector", default=".stage, .post, .reel, .story")
    ap.add_argument("--state", choices=("on", "at-time"), default="on",
                    help="on = apply the .on class to every timed element (layer geometry); "
                         "at-time = only elements inside their data-s/data-e window (a frame)")
    ap.add_argument("--time", type=float, default=3.0, help="reel time in seconds for --state at-time")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE",
                    help="CSS custom property to set on each stage (repeatable)")
    ap.add_argument("--no-transform", action="store_true",
                    help="disable transforms to measure unrotated layout boxes")
    ap.add_argument("--stage-width", type=int, default=0,
                    help="force stages to this px width (e.g. 1080) so cqw values and border "
                         "rounding match the composition")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--budget", type=int, default=12000, help="Chrome virtual time budget (ms)")
    ap.add_argument("--samples", type=int, default=0,
                    help="also capture N time samples of the animated state")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--chrome")
    ap.add_argument("--no-sandbox", action="store_true", help="start with --no-sandbox")
    args = ap.parse_args(argv)

    result = run(args)
    text = json.dumps(result, indent=1)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out} ({len(result['stages'])} stage(s), "
              f"{sum(len(s['nodes']) for s in result['stages'])} nodes, "
              f"fonts={result.get('fontsStatus')})", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
