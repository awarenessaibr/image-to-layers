import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "html_to_projects", ROOT / "tools" / "html-to-projects.py"
)
html_to_projects = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = html_to_projects
SPEC.loader.exec_module(html_to_projects)

PAGE = ROOT / "html-examples" / "Singl-end · news bulletin reels.html"
FIXTURE = ROOT / "tests" / "fixtures" / "news-bulletins-chrome-boxes.json"
CHECKER = ROOT / "tools" / "check-html-parse.py"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def chrome_available() -> bool:
    return (any(Path(p).is_file() for p in CHROME_CANDIDATES)
            or bool(shutil.which("google-chrome")) or bool(shutil.which("chromium")))


def run_cli(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "html-to-projects.py"), *args],
        capture_output=True, text=True, cwd=str(ROOT), timeout=timeout,
    )


EXAMPLES = sorted((ROOT / "html-examples").glob("*.html"))
EXAMPLE_FIXTURES = ROOT / "tests" / "fixtures" / "examples"


def example_slug(page: Path) -> str:
    import re
    import unicodedata
    base = unicodedata.normalize("NFKD", page.stem)
    return re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()[:60] or "example"


class ExampleCoverageTests(unittest.TestCase):
    def test_every_example_has_a_fixture_for_its_current_source(self):
        self.assertGreaterEqual(len(EXAMPLES), 12)
        for page in EXAMPLES:
            fixture = EXAMPLE_FIXTURES / f"{example_slug(page)}.json"
            self.assertTrue(fixture.is_file(), f"missing fixture for {page.name}")
            data = json.loads(fixture.read_text(encoding="utf-8"))
            digest = hashlib.sha256(page.read_bytes()).hexdigest()
            self.assertEqual(data["sourceSha256"], digest,
                             f"{page.name} changed — regenerate {fixture.name}")

    def test_every_example_has_a_report_and_image(self):
        for page in EXAMPLES:
            slug = example_slug(page)
            self.assertTrue((ROOT / "docs" / "examples" / f"{slug}.md").is_file())
            self.assertTrue((ROOT / "docs" / "examples" / "img" / f"{slug}.png").is_file())


class GradientAndProgressTests(unittest.TestCase):
    def test_gingham_gradient_becomes_a_tiled_pattern(self):
        from urllib.parse import unquote
        value = ("linear-gradient(90deg, rgba(168, 32, 44, 0.533) 50%, rgba(0, 0, 0, 0) 50%), "
                 "linear-gradient(rgba(168, 32, 44, 0.533) 50%, rgba(0, 0, 0, 0) 50%), none")
        url = html_to_projects.gradient_data_url(value, "43.2px 43.2px, 43.2px 43.2px, auto",
                                                1080, 162)
        self.assertIsNotNone(url)
        svg = unquote(url.split(",", 1)[1])
        self.assertIn("<pattern", svg)
        self.assertEqual(svg.count("<pattern"), 2)  # one tile per gradient layer
        self.assertNotIn("#ffffff", svg)

    def test_radial_prelude_is_not_mistaken_for_a_colour(self):
        from urllib.parse import unquote
        value = ("radial-gradient(circle at 50% 10.8px, rgba(0, 0, 0, 0) 140.4px, "
                 "rgba(10, 13, 43, 0.72) 388.8px)")
        url = html_to_projects.gradient_data_url(value, "auto", 1080, 1920)
        self.assertIsNotNone(url)
        svg = unquote(url.split(",", 1)[1])
        self.assertNotIn('stop-color="#ffffff"', svg)
        self.assertIn('cy="0.0056"', svg)

    def test_progress_vars_are_detected_from_the_stage_rule(self):
        css = ".stage{position:relative;--p:0;--u:0;--pr:url(x.png);--n:calc(1 - 0)}"
        self.assertEqual(html_to_projects.detect_progress_vars(css), ["n", "p", "u"])

    def test_page_duration_is_read_from_js(self):
        html = "<script>const D=10000,S=[]</script>"
        self.assertEqual(html_to_projects.detect_page_duration_ms(html), 10000)


class DurationAndTimingTests(unittest.TestCase):
    def test_page_declares_ten_seconds(self):
        html = PAGE.read_text(encoding="utf-8")
        self.assertEqual(html_to_projects.detect_page_duration_ms(html), 10000)

    def test_data_d_wins_and_sentinel_is_open(self):
        nodes = [{"timing": {"end": 99}}, {"timing": {"end": 9.5}}]
        self.assertEqual(
            html_to_projects._measured_stage_duration({"data-d": "8000"}, 10000, nodes, 30000),
            8000)
        self.assertEqual(
            html_to_projects._measured_stage_duration({}, 10000, nodes, 30000), 10000)
        # without a declared length the real 9.5 s end is used, not the 99 s sentinel
        self.assertEqual(
            html_to_projects._measured_stage_duration({}, None, nodes, 30000), 9500)

    def test_visibility_keyframes_are_hard_cuts(self):
        box = {"x": 1, "y": 2, "w": 3, "h": 4}
        kfs = html_to_projects._window_keyframes(box, 0, 1.0, 2.4, 5.6, 10000)
        opacity = {k["time"]: k["opacity"] for k in kfs}
        self.assertEqual(opacity[2400], 0.0)
        self.assertEqual(opacity[2401], 1.0)
        self.assertEqual(opacity[5599], 1.0)
        self.assertEqual(opacity[5600], 0.0)
        self.assertNotIn(2700, opacity)  # no invented 300 ms fade

    def test_open_ended_window_stays_visible(self):
        kfs = html_to_projects._window_keyframes(
            {"x": 0, "y": 0, "w": 10, "h": 10}, 0, 1.0, 0.0, None, 10000)
        self.assertEqual(kfs[0]["opacity"], 1.0)
        self.assertEqual(kfs[-1]["opacity"], 1.0)


class LayerBuilderTests(unittest.TestCase):
    def test_marquee_copy_is_deduplicated(self):
        run = "A · B · C · D · E · F · G · H · I · J · K · L · M · N · O · P"
        self.assertEqual(html_to_projects._dedupe_repeat(run + run), run)
        self.assertEqual(html_to_projects._dedupe_repeat("short"), "short")

    def test_colour_alpha(self):
        self.assertEqual(html_to_projects._colour_alpha("rgba(0,0,0,0)"), 0.0)
        self.assertEqual(html_to_projects._colour_alpha("#112233"), 1.0)
        self.assertAlmostEqual(html_to_projects._colour_alpha("rgba(11,24,64,0.87)"), 0.87, 2)

    def test_window_photo_is_not_the_stage_background(self):
        nodes = [
            {"tag": "div", "cls": "ph", "disp": "block", "vis": "visible", "op": 1,
             "bgKind": "none", "box": {"x": 6, "y": 45, "w": 88, "h": 21},
             "clip": {"x": 0, "y": 0, "w": 100, "h": 100}},
            {"tag": "div", "cls": "abs im", "disp": "block", "vis": "visible", "op": 1,
             "bgKind": "url", "bgImage": 0,
             "box": {"x": 6, "y": 45, "w": 88, "h": 21},
             "clip": {"x": 6, "y": 45, "w": 88, "h": 21}},
        ]
        self.assertIsNone(html_to_projects._pick_stage_background(nodes))
        full = dict(nodes[1], box={"x": 0, "y": 0, "w": 100, "h": 100},
                    clip={"x": 0, "y": 0, "w": 100, "h": 100})
        self.assertIs(html_to_projects._pick_stage_background([full]), full)

    def test_fixture_is_for_this_page(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        digest = hashlib.sha256(PAGE.read_bytes()).hexdigest()
        self.assertEqual(fixture["sourceSha256"], digest,
                         "the example changed — regenerate tests/fixtures/"
                         "news-bulletins-chrome-boxes.json with tools/measure-html-chrome.py")


class CssFallbackTests(unittest.TestCase):
    def test_fallback_is_labelled_and_uses_the_paper_background(self):
        done = run_cli("--html", str(PAGE), "--preview-only", "--layout", "css")
        self.assertEqual(done.returncode, 0, done.stderr)
        stages = json.loads(done.stdout)["stages"]
        self.assertEqual(len(stages), 3)
        for stage in stages:
            self.assertEqual(stage["metadata"]["layout"], "css-estimated")
            self.assertEqual(stage["duration"], 10000)
            self.assertTrue(any("estimated" in w for w in stage["warnings"]))
        bg = stages[2]["layers"][0]
        self.assertEqual(bg["id"], "html-bg")
        self.assertTrue(bg["url"].startswith("data:image/svg"), "stage 3 is paper, not the photo")
        # the `.on` state must be honoured even without Chrome
        self.assertTrue(all((l.get("opacity") or 0) > 0.5 for l in stages[0]["layers"]))


@unittest.skipUnless(chrome_available(), "Chrome/Chromium is required for measured layout")
class MeasuredLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.preview = run_cli("--html", str(PAGE), "--preview-only", "--layout", "measure")
        if cls.preview.returncode != 0:
            raise unittest.SkipTest(cls.preview.stderr.strip()[-400:])
        cls.stages = json.loads(cls.preview.stdout)["stages"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_duration_and_stage_size(self):
        for stage in self.stages:
            self.assertEqual(stage["duration"], 10000)
            self.assertEqual((stage["width"], stage["height"]), (1080, 1920))

    def test_every_visible_layer_is_visible(self):
        for stage in self.stages:
            self.assertFalse([l["id"] for l in stage["layers"] if (l.get("opacity") or 0) < 0.05])

    def test_text_and_shapes_are_present(self):
        first = self.stages[0]
        texts = [l["text"] for l in first["layers"] if l["type"] == "text"]
        self.assertIn("BREAKING NEWS", texts)
        self.assertIn("Cyclist diverts route for breakfast", texts)
        self.assertTrue(any("SINGL-END NEWS" in t for t in texts))
        shapes = [l for l in first["layers"] if l["type"] == "image" and l.get("meta", {}).get("isShape")]
        self.assertTrue(shapes, "shapes should be emitted as image layers")

    def test_stage_three_background_is_paper(self):
        bg = self.stages[2]["layers"][0]
        self.assertEqual(bg["id"], "html-bg")
        self.assertTrue(bg["url"].startswith("data:image/svg"))

    def test_geometry_matches_chrome(self):
        done = subprocess.run(
            [sys.executable, str(CHECKER), "--fixture", str(FIXTURE), "--html", str(PAGE),
             "--strict", "--layout", "measure"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=240,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_composition_uses_the_stage_duration(self):
        done = run_cli("--html", str(PAGE), "--out", str(self.out),
                       "--name", "Fixture", "--layout", "measure")
        self.assertEqual(done.returncode, 0, done.stderr)
        comps = sorted(self.out.glob("fixture-*/compositions/*.composition.json"))
        self.assertEqual(len(comps), 3)
        for path in comps:
            comp = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(comp["duration"], 10000)
            self.assertGreater(len(comp["layers"]), 5)
            for layer in comp["layers"]:
                if layer["type"] == "image":
                    self.assertNotIn("data:", layer["url"], "images must be materialised")


if __name__ == "__main__":
    unittest.main()
