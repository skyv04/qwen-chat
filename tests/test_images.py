import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qwen_chat import images
from qwen_chat.images import ImageError, data_url, is_image, is_picture, normalize, ocr_source

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

    def test_only_images_made_of_pixels_are_pictures_to_read_with_ocr(self):
        cases = {
            "scan.bin": (b"BM" + bytes(12) + (40).to_bytes(4, "little"), True),
            "photo.bin": (b"\xff\xd8\xff", True),
            "bmi.txt": (b"BMI chart for the class, week 3", False),
            "logo.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", False),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, (data, picture) in cases.items():
                path = Path(tmp) / name
                path.write_bytes(data)
                with self.subTest(name=name):
                    self.assertEqual(is_picture(path), picture)
                    self.assertEqual(is_image(path), picture or name.endswith(".svg"))

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

    def test_ocr_gets_plain_pictures_as_they_are_and_see_through_ones_on_white(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, clear = Path(tmp) / "plain.png", Path(tmp) / "clear.png"
            for path, alpha in ((plain, False), (clear, True)):
                pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, alpha, 8, 24, 16)
                pixbuf.fill(0x00000000)  # black, or nothing at all where it can be see-through
                path.write_bytes(pixbuf.save_to_bufferv("png", [], [])[1])
            ready = Path(tmp) / "ready.png"
            self.assertEqual(ocr_source(plain, ready), plain)
            self.assertEqual(ocr_source(clear, ready), ready)
            self.assertGreater(GdkPixbuf.Pixbuf.new_from_file(str(ready)).get_pixels()[0], 0xF0)
            with mock.patch.object(images, "OCR_MAX_SIDE", 12):
                self.assertEqual(ocr_source(plain, ready), ready)
            self.assertEqual(GdkPixbuf.Pixbuf.new_from_file(str(ready)).get_width(), 12)


if __name__ == "__main__":
    unittest.main()
