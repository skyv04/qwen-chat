"""Normalize attached images to small formats accepted by llama.cpp."""
from __future__ import annotations

import base64
from pathlib import Path

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_SIDE = 1280

_IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".heic", ".heif", ".jfif", ".jpe",
                   ".jpeg", ".jpg", ".jxl", ".png", ".svg", ".tif", ".tiff", ".webp"}
_IMAGE_SIGNATURES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
                     b"BM", b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis", b"jxl ")


class ImageError(ValueError):
    """An attachment cannot be decoded into an image for the vision model."""


def is_image(path: Path) -> bool:
    """Detect common images by signature, with suffix fallback for malformed images."""
    try:
        with path.open("rb") as source:
            header = source.read(64)
    except OSError:
        return path.suffix.lower() in _IMAGE_SUFFIXES
    if header.startswith(_IMAGE_SIGNATURES):
        return True
    if len(header) >= 12 and header[4:8] == b"ftyp" and any(
            brand in header[8:64] for brand in _HEIF_BRANDS):
        return True
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True
    if header.lstrip().startswith((b"<svg", b"<?xml")):
        return b"<svg" in header.lower()
    return path.suffix.lower() in _IMAGE_SUFFIXES


def normalize(path: Path) -> tuple[str, bytes]:
    """Decode and downscale an image, applying EXIF orientation, then encode for llama.cpp."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ImageError(f"Cannot read image {path.name}: {error.strerror or error}.") from error
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"{path.name} is too large to inspect ({size // (1024 * 1024)} MB; "
                         f"limit {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError) as error:
        raise ImageError("Image decoding is unavailable: install python3-gi and GdkPixbuf "
                         "loaders, then restart Qwen Chat.") from error
    try:
        fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(str(path))
        if fmt is None:
            raise ImageError(f"Cannot decode {path.name}: its image format is not supported.")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ImageError(f"{path.name} has unreasonable image dimensions ({width} × {height}).")
        scale = min(1.0, MAX_IMAGE_SIDE / max(width, height))
        target_width = max(1, round(width * scale))
        target_height = max(1, round(height * scale))
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(path), target_width, target_height, True)
        if pixbuf.get_has_alpha():
            image_format, options, values = "png", [], []
            mime = "image/png"
        else:
            image_format, options, values = "jpeg", ["quality"], ["88"]
            mime = "image/jpeg"
        saved, data = pixbuf.save_to_bufferv(image_format, options, values)
        if not saved or not data:
            raise ImageError(f"Could not encode {path.name} for the vision model.")
        return mime, bytes(data)
    except ImageError:
        raise
    except Exception as error:
        message = getattr(error, "message", None) or str(error)
        raise ImageError(f"Could not decode {path.name}: {message[:300]}") from error


def data_url(path: Path) -> str:
    mime, data = normalize(path)
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
