import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qwen-install-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home with spaces"
        self.home.mkdir()
        self.env = {**os.environ, "HOME": str(self.home),
                    "XDG_CONFIG_HOME": str(self.home / ".config"),
                    "XDG_DATA_HOME": str(self.home / ".local/share"),
                    "XDG_CACHE_HOME": str(self.home / ".cache")}
        self.env.pop("LD_PRELOAD", None)
        self.env.pop("PYTHONPATH", None)

    def install(self):
        return subprocess.run([sys.executable, str(ROOT / "install.py"), "--desktop"],
                              env=self.env, check=True, capture_output=True, text=True, timeout=15)

    def test_install_launches_from_a_path_with_spaces_and_keeps_user_data(self):
        saved = self.home / ".local/share/qwen-chat/chats/keep.json"
        saved.parent.mkdir(parents=True)
        saved.write_text('{"title":"Keep my chat"}')
        self.install()
        launcher = self.home / ".local/bin/qwen-chat"
        result = subprocess.run([str(launcher), "--version"], env=self.env, check=True,
                                capture_output=True, text=True, timeout=10)
        self.assertIn("1.0.0", result.stdout)
        entry = self.home / ".local/share/applications/io.github.skyv04.QwenChat.desktop"
        self.assertIn(f'Exec="{launcher}"', entry.read_text())
        self.assertEqual((self.home / "Desktop/Qwen Chat.desktop").read_bytes(), entry.read_bytes())
        self.install()
        self.assertEqual(saved.read_text(), '{"title":"Keep my chat"}')

    def test_model_launchers_forward_flags_and_remove_preloaded_shims(self):
        self.install()
        binary_dir = self.home / "backend"
        binary_dir.mkdir()
        model = self.home / "test model.gguf"
        model.touch()
        gpu_env = self.home / "gpu.sh"
        gpu_env.write_text('export VK_DRIVER_FILES=/test/turnip.json\nexport LD_PRELOAD=/not-loaded.so\n')
        fixture = (
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "print(json.dumps({'tool': os.path.basename(sys.argv[0]), 'args': sys.argv[1:], "
            "'preload': os.environ.get('LD_PRELOAD'), 'driver': os.environ.get('VK_DRIVER_FILES')}))\n"
        )
        for name in ("llama-cli", "llama-server"):
            target = binary_dir / name
            target.write_text(fixture)
            target.chmod(0o755)
        env = {**self.env, "QWEN_LLAMA_BIN": str(binary_dir), "QWEN_MODEL": str(model),
               "QWEN_GPU_ENV": str(gpu_env), "QWEN_CTX": "8192", "QWEN_SKIP_MEM_CHECK": "1"}
        for name, tool in (("qwen", "llama-cli"), ("qwen-server", "llama-server")):
            with self.subTest(name=name):
                result = subprocess.run(
                    [str(self.home / ".local/bin" / name), "--port", "8181"], env=env,
                    check=True, capture_output=True, text=True, timeout=10)
                report = json.loads(result.stdout)
                self.assertEqual(report["tool"], tool)
                self.assertIsNone(report["preload"])
                self.assertEqual(report["driver"], "/test/turnip.json")
                args = report["args"]
                self.assertEqual(args[args.index("-m") + 1], str(model))
                self.assertEqual(args[args.index("-c") + 1], "8192")
                self.assertEqual(args[args.index("-ub") + 1], "128")
                self.assertEqual(args[-2:], ["--port", "8181"])
                if name == "qwen-server":
                    self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        gpu_env.unlink()
        result = subprocess.run([str(self.home / ".local/bin/qwen-server")], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GPU environment not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
