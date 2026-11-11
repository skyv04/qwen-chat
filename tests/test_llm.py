import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat.llm import ServerError, ServerManager


def _sized(path: Path, mib: int) -> Path:
    path.write_bytes(b"")
    os.truncate(path, mib * 1024 * 1024)
    return path


def _manager(tmp: str, **settings) -> ServerManager:
    manager = ServerManager({
        "server_url": "http://127.0.0.1:8080",
        "context_size": 16384,
        "server_command": [sys.executable],
        "vision": True,
        **settings,
    })
    manager.log_path = Path(tmp) / "server.log"
    manager._read_props = mock.Mock()
    return manager


def _small_model(tmp: str) -> tuple[Path, Path]:
    model = _sized(Path(tmp) / "Small-4B-Q4_K_M.gguf", 2614)
    projector = _sized(Path(tmp) / "Small-4B-mmproj-F16.gguf", 641)
    return model, projector


class ServerVisionTests(unittest.TestCase):
    def test_starts_the_chosen_model_with_its_own_projector(self):
        with tempfile.TemporaryDirectory() as tmp:
            model, projector = _small_model(tmp)
            other = _sized(Path(tmp) / "Other-9B-mmproj-F16.gguf", 1)
            manager = _manager(tmp, model=str(model), vision_projector=str(other))
            manager.health = mock.Mock(side_effect=["down", "ok"])
            with mock.patch("qwen_chat.llm.available_memory_mb", return_value=8000), \
                    mock.patch("qwen_chat.llm.subprocess.Popen") as popen:
                manager.ensure_running()
            self.assertEqual(popen.call_args.args[0][-2:], ["--mmproj", str(projector)])
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["QWEN_MODEL"], str(model))
            self.assertEqual(env["QWEN_NEED_MB"], "4500")
            self.assertEqual(manager.vision_error, "")

    def test_low_memory_starts_text_only_instead_of_loading_vision(self):
        with tempfile.TemporaryDirectory() as tmp:
            model, _ = _small_model(tmp)
            manager = _manager(tmp, model=str(model))
            manager.health = mock.Mock(side_effect=["down", "ok"])
            with mock.patch("qwen_chat.llm.available_memory_mb", return_value=4000), \
                    mock.patch("qwen_chat.llm.subprocess.Popen") as popen:
                manager.ensure_running()
            self.assertNotIn("--mmproj", popen.call_args.args[0])
            self.assertEqual(popen.call_args.kwargs["env"]["QWEN_NEED_MB"], "3200")
            self.assertIn("4,500 MB", manager.vision_error)
            self.assertIn("Model menu", manager.vision_error)

    def test_restart_switches_models_without_letting_requests_in_between(self):
        with tempfile.TemporaryDirectory() as tmp:
            model, projector = _small_model(tmp)
            manager = _manager(tmp, model=str(model))
            old = mock.Mock()
            old.poll.return_value = None
            manager.proc = old
            new = mock.Mock()
            new.poll.return_value = None
            locked_at_start = []

            def start(*args, **kwargs):
                self.assertTrue(old.terminate.called)
                locked_at_start.append(manager._lock.locked())
                return new

            manager.health = mock.Mock(side_effect=["down", "ok"])
            with mock.patch("qwen_chat.llm.available_memory_mb", return_value=8000), \
                    mock.patch("qwen_chat.llm.subprocess.Popen", side_effect=start):
                manager.restart()
            self.assertEqual(locked_at_start, [True])
            self.assertIs(manager.proc, new)
            self.assertEqual(manager.unload_frees_mb(), 2614 + 641)
            self.assertFalse(manager._lock.locked())

    def test_stopping_a_model_while_it_loads_ends_the_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            model, _ = _small_model(tmp)
            manager = _manager(tmp, model=str(model), vision=False)
            proc = mock.Mock()
            proc.poll.return_value = None
            states = iter(["down"])

            def health():
                state = next(states, None)
                if state is None:
                    manager.stop()
                    return "loading"
                return state

            manager.health = health
            with mock.patch("qwen_chat.llm.subprocess.Popen", return_value=proc), \
                    mock.patch("qwen_chat.llm.time.sleep"):
                with self.assertRaisesRegex(ServerError, "stopped before it finished loading"):
                    manager.ensure_running()
            proc.terminate.assert_called_once()
            self.assertFalse(manager._lock.locked())

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
