"""Installed GGUF models, their vision projectors and how much memory each needs to start."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "~/models/Qwen3.5-9B-Q4_K_M.gguf"
# Working buffers, KV cache and headroom for Android, added per loaded file. With it the 9B
# Q4_K_M model needs 6,000 MB (7,500 MB with vision), the limits tested on the phone.
# scripts/qwen computes the same estimate.
OVERHEAD_MB = 600
_QUANT = re.compile(r"-(?:UD-)?(?:I?Q\d\w*|B?F\d+)$", re.IGNORECASE)
_SIZE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?[BM])(?![\w])", re.IGNORECASE)


def _expand(path: str | os.PathLike) -> Path:
    return Path(os.path.expanduser(str(path)))


def _mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def is_projector(path: Path) -> bool:
    return "mmproj" in path.name.lower()


def base_name(path: Path) -> str:
    """"Qwen3.5-9B-Q4_K_M.gguf" -> "Qwen3.5-9B"."""
    return _QUANT.sub("", path.name.removesuffix(".gguf"))


def short_name(path: Path) -> str:
    """The parameter count ("9B") when the name has one, otherwise the model's base name."""
    name = base_name(path)
    match = _SIZE.search(name)
    return match.group(1).upper() if match else name


def _projector_key(path: Path) -> str:
    stem = path.name.removesuffix(".gguf").lower()
    if stem.startswith("mmproj-"):          # mmproj-Qwen_Qwen3.5-9B-f16.gguf
        return _QUANT.sub("", stem.removeprefix("mmproj-"))
    return stem.split("-mmproj")[0]         # Qwen3.5-9B-mmproj-F16.gguf


def projector_for(model: Path, preferred: str | os.PathLike = "") -> Path | None:
    """The vision projector made for this model. A projector from another model would make
    llama-server fail or describe images wrongly, so names must match."""
    key = base_name(model).lower()
    candidates = [_expand(preferred)] if str(preferred).strip() else []
    try:
        found = [p for p in model.parent.glob("*.gguf") if is_projector(p)]
    except OSError:
        found = []
    # Prefer F16: BF16 is about the same size but slower where the GPU lacks bfloat16.
    candidates += sorted(found, key=lambda p: ("-f16" not in p.name.lower(), p.name))
    for path in candidates:
        if is_projector(path) and _projector_key(path) == key and path.is_file():
            return path
    return None


def need_mb(model: Path, projector: Path | None = None) -> int:
    """Memory to have available before starting, rounded to 100 MB."""
    total = _mb(model) + OVERHEAD_MB
    if projector is not None:
        total += _mb(projector) + OVERHEAD_MB
    return int((total + 50) // 100 * 100)


def loaded_mb(model: Path, projector: Path | None = None) -> int:
    """Roughly what unloading a running model gives back: its weights."""
    try:
        return int(_mb(model) + (_mb(projector) if projector is not None else 0))
    except OSError:
        return 0


def selected_model(settings) -> Path:
    return _expand(settings.get("model") or os.environ.get("QWEN_MODEL") or DEFAULT_MODEL)


@dataclass(frozen=True)
class ModelChoice:
    path: Path
    projector: Path | None
    need_mb: int

    @property
    def vision(self) -> bool:
        return self.projector is not None

    @property
    def name(self) -> str:
        return base_name(self.path)

    @property
    def label(self) -> str:
        return f"{self.name} · {'Vision' if self.vision else 'Text only'}"


def choices(settings) -> list[ModelChoice]:
    """Each installed model with vision (when its projector is installed) and text only,
    largest model first."""
    folder = _expand(settings.get("models_dir") or "~/models")
    try:
        models = [p for p in folder.glob("*.gguf") if not is_projector(p) and p.is_file()]
    except OSError:
        models = []
    current = selected_model(settings)
    if current.is_file() and current not in models:
        models.append(current)
    out = []
    for model in sorted(models, key=lambda p: (-p.stat().st_size, p.name)):
        projector = projector_for(model, settings.get("vision_projector", ""))
        if projector is not None:
            out.append(ModelChoice(model, projector, need_mb(model, projector)))
        out.append(ModelChoice(model, None, need_mb(model)))
    return out


def gb(mb: int | float) -> str:
    return f"{mb / 1000:.1f} GB"
