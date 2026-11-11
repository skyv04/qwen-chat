import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat.llm import ServerManager


class ServerVisionTests(unittest.TestCase):
    def test_starts_server_with_installed_projector(self):
        with tempfile.TemporaryDirectory() as tmp:
            projector = Path(tmp) / "mmproj.gguf"
            projector.touch()
            manager = ServerManager({
                "server_url": "http://127.0.0.1:8080",
                "context_size": 16384,
                "server_command": [sys.executable],
                "vision": True,
                "vision_projector": str(projector),
            })
            manager.log_path = Path(tmp) / "server.log"
            manager.health = mock.Mock(side_effect=["down", "ok"])
            manager._read_props = mock.Mock()
            with mock.patch("qwen_chat.llm.available_memory_mb", return_value=8000), \
                    mock.patch("qwen_chat.llm.subprocess.Popen") as popen:
                manager.ensure_running()
            command = popen.call_args.args[0]
            self.assertEqual(command[-2:], ["--mmproj", str(projector)])

    def test_low_memory_starts_text_only_instead_of_loading_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            projector = Path(tmp) / "mmproj.gguf"
            projector.touch()
            manager = ServerManager({
                "server_url": "http://127.0.0.1:8080",
                "context_size": 16384,
                "server_command": [sys.executable],
                "vision": True,
                "vision_projector": str(projector),
            })
            manager.log_path = Path(tmp) / "server.log"
            manager.health = mock.Mock(side_effect=["down", "ok"])
            manager._read_props = mock.Mock()
            with mock.patch("qwen_chat.llm.available_memory_mb", return_value=6000), \
                    mock.patch("qwen_chat.llm.subprocess.Popen") as popen:
                manager.ensure_running()
            command = popen.call_args.args[0]
            self.assertNotIn("--mmproj", command)
            self.assertIn("7,500 MB", manager.vision_error)

    def test_reads_vision_capability_from_server_props(self):
        manager = ServerManager({
            "server_url": "http://127.0.0.1:8080",
            "context_size": 16384,
        })
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "modalities": {"vision": True},
            "model_alias": "Qwen3.5-9B",
            "default_generation_settings": {"n_ctx": 8192},
        }).encode()
        with mock.patch("qwen_chat.llm.urllib.request.urlopen", return_value=response):
            manager._read_props()
        self.assertTrue(manager.vision_enabled)
        self.assertEqual(manager.n_ctx, 8192)
        self.assertEqual(manager.model_name, "Qwen3.5-9B")

    def test_text_only_server_does_not_claim_vision(self):
        manager = ServerManager({
            "server_url": "http://127.0.0.1:8080",
            "context_size": 16384,
        })
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "modalities": {"vision": False},
        }).encode()
        with mock.patch("qwen_chat.llm.urllib.request.urlopen", return_value=response):
            manager._read_props()
        self.assertFalse(manager.vision_enabled)


if __name__ == "__main__":
    unittest.main()
