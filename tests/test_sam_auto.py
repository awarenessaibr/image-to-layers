import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "image_to_layers", ROOT / "tools" / "image-to-layers.py"
)
image_to_layers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = image_to_layers
SPEC.loader.exec_module(image_to_layers)


def fake_numpy():
    module = types.ModuleType("numpy")
    module.asarray = lambda value, dtype=None: value
    module.uint8 = "uint8"
    return module


class SamAutoTests(unittest.TestCase):
    def test_zip_export_keeps_named_layer_metadata(self):
        reference = Image.new("RGB", (20, 20), "white")
        mask = Image.new("L", reference.size, 0)
        mask.paste(255, (4, 5, 15, 16))
        layer = image_to_layers.LayerMask(
            "scones-blueberry-coconut",
            mask,
            group="Scones",
            role="prop",
            group_slug="scones",
            source="scene",
            label="Blueberry coconut scone",
            description="A scone topped with blueberries and coconut.",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                archive_path = image_to_layers.emit_zip(reference, [layer], "bakery").resolve()
            finally:
                os.chdir(previous_cwd)

            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("image-to-layers.json"))

        self.assertEqual(manifest["schema"], "image-to-layers/segments-v1")
        self.assertEqual(len(manifest["layers"]), 1)
        exported = manifest["layers"][0]
        self.assertEqual(exported["file"], "scones_scones-blueberry-coconut.png")
        self.assertEqual(exported["name"], "scones-blueberry-coconut")
        self.assertEqual(exported["group"], "Scones")
        self.assertEqual(exported["role"], "prop")
        self.assertEqual(exported["label"], "Blueberry coconut scone")

    def test_device_selection_prefers_cuda_then_mps_then_cpu(self):
        torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(
                mps=types.SimpleNamespace(is_available=lambda: True)
            ),
        )
        self.assertEqual(image_to_layers.resolve_sam_device("auto", torch), "mps")
        self.assertEqual(image_to_layers.resolve_sam_device("cpu", torch), "cpu")

        torch.cuda.is_available = lambda: True
        self.assertEqual(image_to_layers.resolve_sam_device("auto", torch), "cuda")

    def test_builds_official_sam2_generator_with_selected_model_and_device(self):
        build_model = Mock(return_value=object())
        generator_instance = object()
        generator_class = Mock(return_value=generator_instance)
        numpy = fake_numpy()
        torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(
                mps=types.SimpleNamespace(is_available=lambda: True)
            ),
        )
        sam2 = types.ModuleType("sam2")
        sam2.__path__ = []
        build_module = types.ModuleType("sam2.build_sam")
        build_module.build_sam2_hf = build_model
        generator_module = types.ModuleType("sam2.automatic_mask_generator")
        generator_module.SAM2AutomaticMaskGenerator = generator_class

        with patch.dict(
            sys.modules,
            {
                "numpy": numpy,
                "torch": torch,
                "sam2": sam2,
                "sam2.build_sam": build_module,
                "sam2.automatic_mask_generator": generator_module,
            },
        ):
            generator, returned_numpy = image_to_layers.build_sam2_auto_generator(
                "sam2-small", "auto"
            )

        self.assertIs(generator, generator_instance)
        self.assertIs(returned_numpy, numpy)
        build_model.assert_called_once_with(
            "facebook/sam2.1-hiera-small", device="mps"
        )
        generator_class.assert_called_once_with(
            build_model.return_value,
            points_per_side=16,
            pred_iou_thresh=0.8,
            stability_score_thresh=0.85,
            output_mode="binary_mask",
        )

    def test_filters_masks_and_converts_valid_masks(self):
        valid = Image.new("L", (10, 10), 0)
        valid.paste(255, (2, 2, 7, 7))
        empty = Image.new("L", (10, 10), 0)
        low_stability = Image.new("L", (10, 10), 255)
        generator = Mock()
        generator.generate.return_value = [
            {"segmentation": empty, "area": 0, "stability_score": 1.0},
            {"segmentation": low_stability, "area": 100, "stability_score": 0.2},
            {"segmentation": valid, "area": 25, "stability_score": 0.95},
        ]

        with patch.object(
            image_to_layers,
            "build_sam2_auto_generator",
            return_value=(generator, fake_numpy()),
        ):
            masks = image_to_layers.generate_sam_auto(
                Image.new("RGB", (10, 10)), "sam2-tiny", "cpu"
            )

        self.assertEqual(len(masks), 1)
        self.assertEqual(masks[0][0], "subject-01")
        self.assertEqual(masks[0][1].getbbox(), (2, 2, 7, 7))
        self.assertEqual(masks[0].group, "Subject")
        self.assertEqual(masks[0].role, "character")
        generator.generate.assert_called_once()

    def test_automatic_masks_are_named_and_grouped_by_shape_and_position(self):
        reference = Image.new("RGB", (120, 120), "white")
        raw_masks = []

        def add_mask(box):
            mask = Image.new("L", reference.size, 0)
            mask.paste(255, box)
            area = (box[2] - box[0]) * (box[3] - box[1])
            raw_masks.append({
                "segmentation": mask,
                "area": area,
                "stability_score": 0.98,
            })

        add_mask((0, 0, 120, 30))       # wide top band: Background
        for x in (10, 28, 46, 64):      # repeated tall slivers: Books
            add_mask((x, 5, x + 8, 45))
        add_mask((40, 40, 80, 80))      # central object: Subject
        add_mask((108, 108, 118, 118))  # small corner object: Props
        add_mask((0, 117, 3, 120))      # tiny fragment: Details

        generator = Mock()
        generator.generate.return_value = raw_masks

        with patch.object(
            image_to_layers,
            "build_sam2_auto_generator",
            return_value=(generator, fake_numpy()),
        ):
            masks = image_to_layers.generate_sam_auto(
                reference, "sam2-tiny", "cpu", min_area_pct=0.001
            )

        self.assertEqual([mask.name for mask in masks], [
            "background-01",
            "book-01", "book-02", "book-03", "book-04",
            "subject-01",
            "prop-01",
            "detail-01",
        ])
        self.assertEqual([mask.group for mask in masks], [
            "Background",
            "Books", "Books", "Books", "Books",
            "Subject",
            "Props",
            "Details",
        ])
        self.assertEqual(masks[0].role, "background")
        self.assertEqual(masks[1].role, "prop")
        self.assertEqual(masks[5].role, "character")
        self.assertEqual(masks[-1].role, "prop")

    def test_cli_writes_composition_from_automatic_masks(self):
        mask = Image.new("L", (12, 8), 0)
        mask.paste(255, (3, 2, 9, 6))
        generator = Mock()
        generator.generate.return_value = [
            {"segmentation": mask, "area": 24, "stability_score": 0.98}
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path = root / "reference.jpg"
            Image.new("RGB", (12, 8), "white").save(reference_path)

            with patch.object(
                image_to_layers,
                "build_sam2_auto_generator",
                return_value=(generator, fake_numpy()),
            ):
                result = image_to_layers.main(
                    [
                        "--reference", str(reference_path),
                        "--sam-auto",
                        "--sam-model", "sam2-tiny",
                        "--device", "cpu",
                        "--feather", "0",
                        "--name", "Auto Test",
                        "--out", str(root / "projects"),
                    ]
                )

            composition_path = (
                root / "projects" / "Auto-Test" / "compositions"
                / "Auto-Test.composition.json"
            )
            self.assertEqual(result, 0)
            self.assertTrue(composition_path.is_file())
            composition = image_to_layers.json.loads(
                composition_path.read_text(encoding="utf-8")
            )
            self.assertEqual([layer["name"] for layer in composition["layers"]], [
                "Final image", "subject-01"
            ])
            generated_layer = composition["layers"][1]
            self.assertEqual(generated_layer["role"], "character")
            self.assertEqual(generated_layer["segmentGroup"], "Subject")
            generated_image = root / "projects" / "Auto-Test" / "images" / "subject-01.png"
            self.assertTrue(generated_image.is_file())
            with Image.open(generated_image) as opened_image:
                self.assertEqual(opened_image.size, (6, 4))
            with Image.open(root / "projects" / "Auto-Test" / "images" / "reference.jpg") as saved_reference:
                self.assertEqual(saved_reference.mode, "RGB")

    def test_point_prompt_layer_becomes_an_explicit_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path = root / "reference.png"
            Image.new("RGB", (40, 40), "white").save(reference_path)

            result = image_to_layers.main([
                "--reference", str(reference_path),
                "--sam-points", "man:0.5,0.5",
                "--feather", "0",
                "--name", "Point Test",
                "--out", str(root / "projects"),
            ])

            self.assertEqual(result, 0)
            composition_path = (
                root / "projects" / "Point-Test" / "compositions"
                / "Point-Test.composition.json"
            )
            composition = image_to_layers.json.loads(
                composition_path.read_text(encoding="utf-8")
            )
            generated_layer = composition["layers"][1]
            self.assertEqual(generated_layer["name"], "man")
            self.assertEqual(generated_layer["segmentGroup"], "man")

    def test_knockout_background_matches_reference_outside_cutout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
            cutout = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
            cutout.paste((0, 0, 0, 255), (6, 6, 14, 14))

            reference_path = root / "reference.png"
            cutout_path = root / "cutout.png"
            reference.save(reference_path)
            cutout.save(cutout_path)

            result = image_to_layers.main([
                "--reference", str(reference_path),
                "--knockout", str(cutout_path),
                "--knockout-underlap", "1",
                "--name", "Knockout Test",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])

            self.assertEqual(result, 0)
            background_path = root / "projects" / "Knockout-Test-background" / "images" / "Background.png"
            self.assertTrue(background_path.is_file())
            background = Image.open(background_path).convert("RGBA")
            self.assertEqual(background.convert("RGB").tobytes(), reference.convert("RGB").tobytes())
            self.assertIsNotNone(background.getchannel("A").getbbox())

    def test_knockout_exclude_keeps_selected_objects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
            cutout = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
            cutout.paste((0, 0, 0, 255), (2, 2, 6, 6))
            cutout.paste((0, 0, 0, 255), (12, 12, 16, 16))

            reference_path = root / "reference.png"
            cutout_path = root / "cutout.png"
            reference.save(reference_path)
            cutout.save(cutout_path)

            result = image_to_layers.main([
                "--reference", str(reference_path),
                "--knockout", str(cutout_path),
                "--knockout-underlap", "0",
                "--knockout-exclude", "0.2,0.2",
                "--name", "Keep Objects",
                "--out", str(root / "projects"),
                "--feather", "0",
            ])

            self.assertEqual(result, 0)
            background_path = (
                root / "projects" / "Keep-Objects-background" / "images" / "Background.png"
            )
            self.assertTrue(background_path.is_file())
            background = Image.open(background_path).convert("RGBA")
            # The clicked object stays opaque; the other one is knocked out.
            self.assertEqual(background.getpixel((4, 4))[3], 255)
            self.assertEqual(background.getpixel((14, 14))[3], 0)

    def test_knockout_exclude_reports_when_everything_is_kept(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference = Image.new("RGBA", (20, 20), (255, 0, 0, 255))
            cutout = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
            cutout.paste((0, 0, 0, 255), (6, 6, 14, 14))

            reference_path = root / "reference.png"
            cutout_path = root / "cutout.png"
            reference.save(reference_path)
            cutout.save(cutout_path)

            with self.assertRaisesRegex(SystemExit, "nothing left to remove"):
                image_to_layers.main([
                    "--reference", str(reference_path),
                    "--knockout", str(cutout_path),
                    "--knockout-exclude", "0.5,0.5",
                    "--name", "Keep Everything",
                    "--out", str(root / "projects"),
                ])

    def test_missing_sam_dependencies_are_actionable(self):
        with patch.dict(sys.modules, {"numpy": None, "sam2": None}):
            with self.assertRaisesRegex(RuntimeError, "Optional SAM 2 setup"):
                image_to_layers.build_sam2_auto_generator("sam2-small", "auto")

    def test_missing_huggingface_client_is_actionable(self):
        build_model = Mock(side_effect=ModuleNotFoundError("huggingface_hub"))
        generator_class = Mock()
        numpy = fake_numpy()
        torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: False),
            backends=types.SimpleNamespace(
                mps=types.SimpleNamespace(is_available=lambda: False)
            ),
        )
        sam2 = types.ModuleType("sam2")
        sam2.__path__ = []
        build_module = types.ModuleType("sam2.build_sam")
        build_module.build_sam2_hf = build_model
        generator_module = types.ModuleType("sam2.automatic_mask_generator")
        generator_module.SAM2AutomaticMaskGenerator = generator_class

        with patch.dict(
            sys.modules,
            {
                "numpy": numpy,
                "torch": torch,
                "sam2": sam2,
                "sam2.build_sam": build_module,
                "sam2.automatic_mask_generator": generator_module,
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "requirements-sam.txt"):
                image_to_layers.build_sam2_auto_generator("sam2-small", "auto")


if __name__ == "__main__":
    unittest.main()