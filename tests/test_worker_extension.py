"""Thumbnail fallback checks without a running Forge worker or Pillow install."""

import base64
import importlib.util
import os
from pathlib import Path
from queue import Queue
import sys
import types
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeImage:
    mode = "RGB"

    def copy(self):
        return FakeImage()

    def thumbnail(self, size):
        self.requested_size = size

    def save(self, buffer, format, quality):
        buffer.write(b"fake-jpeg")


class WorkerExtensionThumbnailTest(unittest.TestCase):
    def test_result_image_produces_fallback_thumbnail_event(self):
        with self.subTest("Forge result gallery can supply an image when save callback missed it"):
            with patch.dict(os.environ, {"USERPROFILE": os.environ.get("TEMP", "C:\\Temp"), "HOME": os.environ.get("TEMP", "C:\\Temp")}):
                fake_modules = types.ModuleType("modules")
                fake_progress = types.SimpleNamespace(
                    current_task=None,
                    start_task=lambda task_id: None,
                    record_results=lambda task_id, results: results,
                    finish_task=lambda task_id: None,
                )
                fake_modules.progress = fake_progress
                fake_modules.script_callbacks = types.SimpleNamespace(on_image_saved=lambda callback: None)
                fake_pil = types.ModuleType("PIL")
                fake_pil_image = types.ModuleType("PIL.Image")
                fake_pil_image.Image = FakeImage
                fake_pil.Image = fake_pil_image
                source = Path(__file__).parents[1] / "extension" / "lush-forge-history" / "scripts" / "lush_forge_history.py"
                spec = importlib.util.spec_from_file_location("_lush_forge_history_test", source)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                try:
                    with patch.dict(sys.modules, {"modules": fake_modules, "PIL": fake_pil, "PIL.Image": fake_pil_image}):
                        spec.loader.exec_module(module)
                    module.CONFIG = {"worker_id": "forge1", "key": "test"}
                    module.EVENTS = Queue()
                    first_image = FakeImage()
                    result_images = [first_image]
                    selected = module._first_result_image((result_images, "generation-info", "html", "log"))

                    self.assertIs(selected, first_image)
                    self.assertTrue(module._emit_thumbnail("task(THUMB-FALLBACK)", selected))
                    event = module.EVENTS.get_nowait()
                    self.assertEqual(event["task_id"], "task(THUMB-FALLBACK)")
                    self.assertEqual(event["event"], "thumbnail")
                    self.assertEqual(base64.b64decode(event["thumbnail_b64"]), b"fake-jpeg")
                    self.assertNotIn("image_path", event)

                    module.EVENTS = Queue()
                    module.THUMBNAILS.clear()
                    module.LAST_TASK_ID = "task(CURRENT-FALLBACK)"
                    module.progress.current_task = None
                    module._on_image_saved(types.SimpleNamespace(image=first_image, filename=None))
                    fallback_event = module.EVENTS.get_nowait()
                    self.assertEqual(fallback_event["task_id"], "task(CURRENT-FALLBACK)")
                finally:
                    sys.modules.pop(spec.name, None)

    def test_stream_upload_can_be_cancelled_before_or_after_commit(self):
        with TemporaryDirectory() as temporary:
            fake_modules = types.ModuleType("modules")
            fake_modules.progress = types.SimpleNamespace(
                current_task=None, start_task=lambda task_id: None,
                record_results=lambda task_id, results: results,
                finish_task=lambda task_id: None,
            )
            fake_modules.script_callbacks = types.SimpleNamespace(on_image_saved=lambda callback: None)
            fake_modules.shared = types.SimpleNamespace(cmd_opts=types.SimpleNamespace(lora_dir=temporary))
            fake_networks = types.ModuleType("networks")
            fake_networks.available_networks = {}
            fake_networks.list_available_networks = lambda: None
            fake_network = types.ModuleType("network")

            class FakeNetworkOnDisk:
                def __init__(self, name, filename):
                    self.name = name
                    self.filename = filename
                    self.alias = name

                def get_alias(self):
                    return self.alias

            fake_network.NetworkOnDisk = FakeNetworkOnDisk
            fake_pil = types.ModuleType("PIL")
            fake_pil_image = types.ModuleType("PIL.Image")
            fake_pil_image.Image = FakeImage
            fake_pil.Image = fake_pil_image
            source = Path(__file__).parents[1] / "extension" / "lush-forge-history" / "scripts" / "lush_forge_history.py"
            spec = importlib.util.spec_from_file_location("_lush_forge_history_upload_test", source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                with patch.dict(sys.modules, {
                    "modules": fake_modules, "networks": fake_networks, "network": fake_network,
                    "PIL": fake_pil, "PIL.Image": fake_pil_image,
                }):
                    spec.loader.exec_module(module)
                    module.CONFIG = {"worker_id": "forge1", "key": "worker-secret"}
                    app = FastAPI()
                    module._register_lora_upload_api(None, app)
                    with TestClient(app) as client:
                        first_id = "43f28be4-957e-4d20-943e-f3ce881bd8ae"
                        account_headers = {"X-Forge-Worker-Key": "worker-secret", "X-Lush-Account-Id": "17"}
                        uploaded = client.post(
                            f"/internal/lora/uploads/{first_id}?filename=style.safetensors",
                            content=b"sample-data", headers=account_headers,
                        )
                        self.assertEqual(uploaded.status_code, 200, uploaded.text)
                        stored = Path(temporary) / "__lush_owner_17__style.safetensors"
                        self.assertEqual(stored.read_bytes(), b"sample-data")
                        cancelled = client.post(
                            f"/internal/lora/uploads/{first_id}/cancel",
                            headers=account_headers,
                        )
                        self.assertTrue(cancelled.json()["cancelled"])
                        self.assertFalse(stored.exists())
                        second_id = "943e91d2-7021-4696-a28e-b243788769a2"
                        client.post(f"/internal/lora/uploads/{second_id}/cancel", headers={**account_headers, "X-Lush-Account-Id": "17"})
                        rejected = client.post(
                            f"/internal/lora/uploads/{second_id}?filename=other.safetensors",
                            content=b"sample-data", headers=account_headers,
                        )
                        self.assertEqual(rejected.status_code, 409)
                        self.assertFalse((Path(temporary) / "__lush_owner_17__other.safetensors").exists())
                        denied = client.post(
                            f"/internal/lora/uploads/{first_id}/cancel",
                            headers={**account_headers, "X-Forge-Worker-Key": "wrong"},
                        )
                        self.assertEqual(denied.status_code, 403)
                        other_account = client.post(
                            f"/internal/lora/uploads/{first_id}/cancel",
                            headers={**account_headers, "X-Lush-Account-Id": "18"},
                        )
                        self.assertEqual(other_account.status_code, 403)
            finally:
                sys.modules.pop(spec.name, None)


if __name__ == "__main__":
    unittest.main()
