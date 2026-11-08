import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "image_to_layers_vision", ROOT / "tools" / "image-to-layers.py"
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


class VisionModelTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (8, 8), "white")
        self.scene_content = json.dumps({
            "description": "A simple scene.",
            "objects": [{
                "name": "red-box",
                "bbox": [0.1, 0.1, 0.8, 0.8],
                "point": [0.4, 0.4],
            }],
        })
        self.response = io.BytesIO(json.dumps({
            "choices": [{
                "message": {
                    "content": self.scene_content
                }
            }]
        }).encode("utf-8"))

    def _mock_request(self, model):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data)
            seen["timeout"] = timeout
            if request.full_url.endswith("/api/chat"):
                return io.BytesIO(json.dumps({
                    "message": {"content": self.scene_content}
                }).encode("utf-8"))
            return self.response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            scene = image_to_layers.describe_scene(self.image, model=model)
        return scene, seen

    def test_local_ollama_model_uses_local_endpoint_without_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            scene, seen = self._mock_request("ollama:qwen3.5:4b")

        self.assertEqual(seen["url"], "http://127.0.0.1:11434/api/chat")
        self.assertIsNone(seen["authorization"])
        self.assertEqual(seen["body"]["model"], "qwen3.5:4b")
        self.assertEqual(seen["body"]["format"], "json")
        self.assertFalse(seen["body"]["think"])
        self.assertFalse(seen["body"]["stream"])
        self.assertEqual(seen["timeout"], 600)
        self.assertEqual(len(seen["body"]["messages"][0]["images"]), 1)
        self.assertEqual(scene["model"], "ollama:qwen3.5:4b")
        self.assertEqual(len(scene["objects"]), 1)

    def test_describe_subprocess_timeout_allows_slow_local_vision_models(self):
        self.assertEqual(image_to_layers_server.VISION_SUBPROCESS_TIMEOUT, 660)

    def test_ollama_endpoint_and_optional_auth_can_be_configured(self):
        with patch.dict(os.environ, {
            "ITL_OLLAMA_BASE_URL": "http://ollama.internal:11434/v1/",
            "ITL_OLLAMA_API_KEY": "local-secret",
        }, clear=True):
            _, seen = self._mock_request("ollama:bonsai-2:latest")

        self.assertEqual(seen["url"], "http://ollama.internal:11434/api/chat")
        self.assertEqual(seen["authorization"], "Bearer local-secret")
        self.assertEqual(seen["body"]["model"], "bonsai-2:latest")

    def test_deepseek_uses_configured_key_and_endpoint(self):
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.example/v1/",
        }, clear=True):
            _, seen = self._mock_request("deepseek-flash")

        self.assertEqual(seen["url"], "https://api.deepseek.example/v1/chat/completions")
        self.assertEqual(seen["authorization"], "Bearer deepseek-secret")
        self.assertEqual(seen["body"]["model"], "deepseek-flash")

    def test_deepseek_requires_key_and_ollama_model_id_is_validated(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "DEEPSEEK_API_KEY is not set"):
                image_to_layers.describe_scene(self.image, model="deepseek-flash")
            with self.assertRaisesRegex(RuntimeError, "invalid Ollama vision model id"):
                image_to_layers.describe_scene(self.image, model="ollama:../outside")

    def test_ssl_verification_can_be_disabled_for_proxies(self):
        def fake_urlopen(request, timeout=None, context=None):
            self.assertIsNotNone(context)
            self.assertFalse(context.check_hostname)
            self.assertEqual(context.verify_mode, image_to_layers.ssl.CERT_NONE)
            return self.response

        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "ITL_SSL_VERIFY": "false",
        }, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            scene = image_to_layers.describe_scene(self.image, model="deepseek-flash")

        self.assertEqual(scene["model"], "deepseek-flash")

    def test_vision_catalog_lists_installed_ollama_vision_models(self):
        tags = {
            "models": [
                {
                    "name": "custom-vision:9b",
                    "capabilities": ["completion", "vision"],
                    "details": {"parameter_size": "9.0B"},
                },
                {
                    "name": "text-only:7b",
                    "capabilities": ["completion"],
                    "details": {"parameter_size": "7.0B"},
                },
                {
                    "name": "legacy-llava:latest",
                    "details": {"families": ["clip"]},
                },
            ],
        }

        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "http://ollama.internal:11434/api/tags")
            self.assertEqual(request.get_header("Authorization"), "Bearer ollama-secret")
            self.assertEqual(timeout, 5)
            return io.BytesIO(json.dumps(tags).encode("utf-8"))

        with patch.dict(os.environ, {
            "ITL_OLLAMA_BASE_URL": "http://ollama.internal:11434/v1/",
            "ITL_OLLAMA_API_KEY": "ollama-secret",
            "ITL_VISION_MODEL": "ollama:legacy-llava:latest",
        }, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
            catalog = image_to_layers_server.get_vision_model_catalog()

        self.assertTrue(catalog["ollama"]["available"])
        self.assertEqual(
            [model["id"] for model in catalog["ollama"]["models"]],
            ["custom-vision:9b", "legacy-llava:latest"],
        )
        self.assertEqual(catalog["ollama"]["models"][0]["parameterSize"], "9.0B")
        self.assertEqual(catalog["defaultModel"], "ollama:legacy-llava:latest")
        self.assertFalse(catalog["deepseek"]["available"])
        self.assertNotIn("ollama-secret", json.dumps(catalog))

    def test_vision_catalog_reports_ollama_unavailable_without_exposing_credentials(self):
        with patch.dict(os.environ, {
            "ITL_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
            "ITL_OLLAMA_API_KEY": "ollama-secret",
        }, clear=True), patch(
            "urllib.request.urlopen", side_effect=OSError("connection refused")
        ):
            catalog = image_to_layers_server.get_vision_model_catalog()

        self.assertFalse(catalog["ollama"]["available"])
        self.assertEqual(catalog["ollama"]["models"], [])
        self.assertIn("Could not load", catalog["ollama"]["error"])
        self.assertNotIn("ollama-secret", json.dumps(catalog))


if __name__ == "__main__":
    unittest.main()
