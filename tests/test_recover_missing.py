import base64
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "image_to_layers", ROOT / "tools" / "image-to-layers.py"
)
image_to_layers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_to_layers
SPEC.loader.exec_module(image_to_layers)

SERVER_SPEC = importlib.util.spec_from_file_location(
    "image_to_layers_server", ROOT / "image-to-layers-server.py"
)
image_to_layers_server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = image_to_layers_server
SERVER_SPEC.loader.exec_module(image_to_layers_server)


def build_fixture(root: Path) -> tuple[Path, Path]:
    reference = Image.new("RGBA", (120, 80), (255, 255, 255, 255))
    draw = ImageDraw.Draw(reference)
    draw.rectangle((10, 10, 30, 30), fill=(200, 30, 30, 255))
    draw.ellipse((60, 20, 85, 45), fill=(30, 150, 60, 255))
    draw.rectangle((40, 50, 55, 70), fill=(40, 60, 200, 255))
    reference_path = root / "reference.png"
    reference.save(reference_path)

    export = reference.copy()
    export.paste((0, 0, 0, 0), (10, 10, 31, 31))
    export.paste((0, 0, 0, 0), (40, 50, 56, 71))
    export_path = root / "export.png"
    export.save(export_path)
    return reference_path, export_path


class RecoverMissingTests(unittest.TestCase):
    def test_detects_transparent_holes_and_writes_composition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)

            result = image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "Recover Test",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])

            project = root / "projects" / "Recover-Test-recovered"
            composition_path = (
                project / "compositions" / "Recover-Test-recovered.composition.json"
            )
            self.assertEqual(result, 0)
            self.assertTrue(composition_path.is_file())
            composition = image_to_layers.json.loads(
                composition_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                [layer["name"] for layer in composition["layers"]],
                ["Final image", "Aligned export", "missing-01", "missing-02"],
            )
            self.assertEqual(composition["name"], "Recover Test recovered")

            base = composition["layers"][1]
            self.assertEqual(base["id"], "aligned-export")
            self.assertTrue(base["visible"])
            self.assertEqual(base["z"], 1)

            first = composition["layers"][2]
            self.assertAlmostEqual(first["x"], 10 / 120 * 100, places=3)
            self.assertAlmostEqual(first["y"], 10 / 80 * 100, places=3)
            self.assertAlmostEqual(first["w"], 21 / 120 * 100, places=3)
            self.assertAlmostEqual(first["h"], 21 / 80 * 100, places=3)

            self.assertTrue((project / "images" / "export.png").is_file())
            self.assertTrue((project / "images" / "missing-preview.png").is_file())
            with Image.open(project / "images" / "missing-01.png") as cutout:
                self.assertEqual(cutout.size, (21, 21))
            with Image.open(project / "images" / "missing-02.png") as cutout:
                self.assertEqual(cutout.size, (16, 21))

            recover_meta = composition["meta"]["recover"]
            self.assertEqual(recover_meta["regions"], 2)
            self.assertEqual(recover_meta["holeSource"], "alpha")
            self.assertEqual(recover_meta["baseLayerId"], "aligned-export")
            self.assertEqual(recover_meta["previewUrl"], (
                "/data/projects/Recover-Test-recovered/images/missing-preview.png"
            ))

    def test_ignores_edge_touching_transparent_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)
            export = Image.open(export_path).convert("RGBA")
            ImageDraw.Draw(export).rectangle(
                (0, 0, 119, 79), outline=(0, 0, 0, 0), width=2
            )
            export.save(export_path)

            image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "Framed",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])
            composition = image_to_layers.json.loads((
                root / "projects" / "Framed-recovered" / "compositions"
                / "Framed-recovered.composition.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(
                [layer["name"] for layer in composition["layers"]],
                ["Final image", "Aligned export", "missing-01", "missing-02"],
            )

    def test_keep_border_includes_the_frame(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)
            export = Image.open(export_path).convert("RGBA")
            ImageDraw.Draw(export).rectangle(
                (0, 0, 119, 79), outline=(0, 0, 0, 0), width=2
            )
            export.save(export_path)

            image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "Framed Kept",
                "--out", str(root / "projects"),
                "--feather", "0",
                "--recover-keep-border",
            ])
            composition = image_to_layers.json.loads((
                root / "projects" / "Framed-Kept-recovered" / "compositions"
                / "Framed-Kept-recovered.composition.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(len(composition["layers"]), 5)

    def test_min_area_drops_small_holes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)
            export = Image.open(export_path).convert("RGBA")
            export.paste((0, 0, 0, 0), (100, 10, 102, 12))
            export.save(export_path)

            image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "Small",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])
            composition = image_to_layers.json.loads((
                root / "projects" / "Small-recovered" / "compositions"
                / "Small-recovered.composition.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(len(composition["layers"]), 4)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)
            export = Image.open(export_path).convert("RGBA")
            export.paste((0, 0, 0, 0), (100, 10, 102, 12))
            export.save(export_path)

            image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "Small Kept",
                "--out", str(root / "projects"),
                "--feather", "0",
                "--recover-min-area", "0.001",
            ])
            composition = image_to_layers.json.loads((
                root / "projects" / "Small-Kept-recovered" / "compositions"
                / "Small-Kept-recovered.composition.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(len(composition["layers"]), 5)

    def test_falls_back_to_near_white_holes_for_opaque_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, _ = build_fixture(root)
            export = Image.open(reference_path).convert("RGB")
            draw = ImageDraw.Draw(export)
            draw.rectangle((10, 10, 30, 30), fill=(255, 255, 255))
            draw.rectangle((40, 50, 55, 70), fill=(255, 255, 255))
            export_path = root / "opaque.jpg"
            export.save(export_path, quality=100)

            image_to_layers.main([
                "--reference", str(reference_path),
                "--export", str(export_path),
                "--name", "White Holes",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])
            composition = image_to_layers.json.loads((
                root / "projects" / "White-Holes-recovered" / "compositions"
                / "White-Holes-recovered.composition.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual(composition["meta"]["recover"]["holeSource"], "white")
            self.assertEqual(
                [layer["name"] for layer in composition["layers"]],
                ["Final image", "Aligned export", "missing-01", "missing-02"],
            )

    def test_errors_when_no_holes_are_found(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path, export_path = build_fixture(root)
            Image.open(reference_path).save(export_path)

            with self.assertRaises(SystemExit):
                image_to_layers.main([
                    "--reference", str(reference_path),
                    "--export", str(export_path),
                    "--name", "No Holes",
                    "--out", str(root / "projects"),
                ])

    def test_solidifies_uniform_partial_alpha(self):
        image = Image.new("RGBA", (10, 10), (40, 80, 120, 197))
        image.paste((0, 0, 0, 0), (0, 0, 3, 3))
        solid, normalized_from = image_to_layers.solidify_base_alpha(image)

        self.assertEqual(normalized_from, 197)
        self.assertEqual(solid.getpixel((5, 5))[3], 255)
        self.assertEqual(solid.getpixel((1, 1))[3], 0)
        self.assertEqual(solid.getpixel((5, 5))[:3], (40, 80, 120))

    def test_server_builds_recover_command(self):
        def data_url(image: Image.Image, format: str) -> str:
            buffer = io.BytesIO()
            image.save(buffer, format=format)
            return f"data:image/{format.lower()};base64," + base64.b64encode(
                buffer.getvalue()
            ).decode()

        reference = Image.new("RGB", (10, 10), "white")
        export = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        payload = {
            "reference": data_url(reference, "PNG"),
            "referenceName": "reference.png",
            "export": data_url(export, "PNG"),
            "exportName": "export.png",
            "mode": "recover",
            "recoverMinArea": 0.2,
            "recoverHoleSource": "alpha",
            "recoverIgnoreBorder": False,
            "name": "Server Recover",
            "feather": 1,
            "dilate": 0,
            "outputMode": "project",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            argv, _ = image_to_layers_server.build_command(payload, Path(temp_dir))
        self.assertIn("--export", argv)
        self.assertIn("--recover-holes", argv)
        self.assertIn("alpha", argv)
        self.assertIn("--recover-min-area", argv)
        self.assertIn("0.2", argv)
        self.assertIn("--recover-keep-border", argv)
        self.assertNotIn("--sam-points", argv)

    def test_preview_parser_and_publisher_keep_project_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preview = root / "data" / "projects" / "Scene" / "images" / "missing-preview.png"
            preview.parent.mkdir(parents=True)
            preview.write_bytes(b"preview")
            original_root = image_to_layers_server.ROOT
            original_downloads = image_to_layers_server.DOWNLOADS_DIR
            try:
                image_to_layers_server.ROOT = root
                image_to_layers_server.DOWNLOADS_DIR = root / "data" / "downloads"
                found = image_to_layers_server.find_preview_output(
                    [f"preview {preview}", "wrote scene.json"]
                )
                self.assertEqual(found, preview)
                self.assertEqual(
                    image_to_layers_server.publish_preview(found),
                    "/data/projects/Scene/images/missing-preview.png",
                )
            finally:
                image_to_layers_server.ROOT = original_root
                image_to_layers_server.DOWNLOADS_DIR = original_downloads


if __name__ == "__main__":
    unittest.main()
