import os
import tempfile
import unittest
from pathlib import Path

from qwen_chat import models


def _sized(path: Path, mib: int) -> Path:
    path.write_bytes(b"")
    os.truncate(path, mib * 1024 * 1024)
    return path


class ModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_projectors_pair_only_with_their_own_model(self):
        big = _sized(self.dir / "Qwen3.5-9B-Q4_K_M.gguf", 3)
        f16 = _sized(self.dir / "Qwen3.5-9B-mmproj-F16.gguf", 1)
        _sized(self.dir / "Qwen3.5-9B-mmproj-BF16.gguf", 1)
        small = _sized(self.dir / "Qwen_Qwen3.5-4B-Q4_K_M.gguf", 2)
        small_projector = _sized(self.dir / "mmproj-Qwen_Qwen3.5-4B-f16.gguf", 1)
        tiny = _sized(self.dir / "Qwen3.5-2B-Q4_K_M.gguf", 1)

        self.assertEqual(models.projector_for(big), f16)
        # A projector saved in settings for another model is ignored rather than crashing the server.
        self.assertEqual(models.projector_for(small, f16), small_projector)
        self.assertIsNone(models.projector_for(tiny, f16))
        self.assertEqual(models.short_name(small), "4B")

    def test_memory_needs_match_the_launcher_estimate(self):
        model = _sized(self.dir / "Qwen3.5-4B-Q4_K_M.gguf", 2614)
        projector = _sized(self.dir / "Qwen3.5-4B-mmproj-F16.gguf", 641)
        self.assertEqual(models.need_mb(model), 3200)
        self.assertEqual(models.need_mb(model, projector), 4500)
        self.assertEqual(models.loaded_mb(model, projector), 3255)

    def test_choices_list_each_model_with_vision_first_and_largest_first(self):
        folder = self.dir / "models"
        folder.mkdir()
        big = _sized(folder / "Qwen3.5-9B-Q4_K_M.gguf", 5)
        _sized(folder / "Qwen3.5-9B-mmproj-F16.gguf", 1)
        small = _sized(folder / "Qwen3.5-4B-Q4_K_M.gguf", 3)
        elsewhere = _sized(self.dir / "Custom-1B-Q8_0.gguf", 2)
        settings = {"models_dir": str(folder), "model": str(elsewhere)}

        listed = [(c.path, c.vision) for c in models.choices(settings)]

        self.assertEqual(listed, [(big, True), (big, False), (small, False), (elsewhere, False)])
        self.assertEqual(models.choices(settings)[0].label, "Qwen3.5-9B · Vision")


if __name__ == "__main__":
    unittest.main()
