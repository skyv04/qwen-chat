import tempfile
import unittest
from pathlib import Path

from qwen_chat.images import ImageError, data_url, is_image, normalize

try:
    import gi

    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except (ImportError, ValueError):
    GdkPixbuf = None


class ImageDetectionTests(unittest.TestCase):
    def test_detects_jpeg_png_webp_and_heif_signatures(self):
        signatures = {
            "photo.bin": b"\xff\xd8\xff",
            "image.bin": b"\x89PNG\r\n\x1a\n",
            "clip.bin": b"RIFF\x00\x00\x00\x00WEBP",
            "phone.bin": b"\x00\x00\x00\x18ftypheic",
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, signature in signatures.items():
                path = Path(tmp) / name
                path.write_bytes(signature)
                with self.subTest(name=name):
                    self.assertTrue(is_image(path))

    def test_a_bad_image_extension_is_reported_as_an_image_decode_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.png"
            path.write_bytes(b"not an image")
            with self.assertRaisesRegex(ImageError, "format is not supported"):
                normalize(path)


@unittest.skipIf(GdkPixbuf is None, "GdkPixbuf is required for image decoding")
class ImageNormalizationTests(unittest.TestCase):
    def test_normalizes_png_to_small_jpeg_and_data_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "image.png"
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, 24, 16)
            pixbuf.fill(0x44AAFFFF)
            saved, png = pixbuf.save_to_bufferv("png", [], [])
            self.assertTrue(saved)
            path.write_bytes(png)

            mime, image = normalize(path)
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(image.startswith(b"\xff\xd8\xff"))
            self.assertLess(len(image), 25_000)
            decoded = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
            decoded.write(image)
            decoded.close()
            self.assertEqual((decoded.get_pixbuf().get_width(), decoded.get_pixbuf().get_height()),
                             (24, 16))
            self.assertTrue(data_url(path).startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
