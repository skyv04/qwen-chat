import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from qwen_chat import worksheet_pdf as pdf


def fixture():
    return {
        "title": "An explorer's notebook",
        "reading": ["Observe the world carefully. Compare <sun> & <moon>; those labels are "
                    "literal text, not HTML. Record what changes, and explain what stays the same."],
        "questions": [
            {"prompt": "How could you use the observations from this passage to support an "
                       "explanation? Compare two details, describe their differences, and give "
                       "evidence for your reasoning.", "lines": 4},
            {"prompt": "Which action gives you evidence?",
             "options": ["Look carefully and record a detail.", "Guess without looking."], "lines": 0},
        ],
        "max_pages": 1,
    }


class WorksheetSpecTest(unittest.TestCase):
    def test_invalid_design_data_is_rejected_instead_of_silently_ignored(self):
        bad = [
            {"theme": {"accent": "#eeeeee"}},
            {"theme": {"accent": "red; background:url(https://example.com)"}},
            {"theme": {"icons": ["not an emoji"]}},
            {"theme": {"motif": "external-image"}},
            {"html": "<script>doSomething()</script>"},
            {"max_pages": True},
            {"questions": [{"prompt": "Explain.", "lines": 0}]},
            {"questions": [{"prompt": "Explain.", "lines": 13}]},
            {"reading": [], "questions": []},
        ]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(pdf.DesignError):
                pdf.validate_spec({**fixture(), **change})

    def test_markup_is_text_not_executable_content(self):
        spec = fixture()
        attack = "</p><script>document.body.remove()</script>"
        spec["reading"] = [attack]
        with mock.patch.object(pdf, "_fonts", return_value=""):
            document = pdf.make_html(pdf.validate_spec(spec))
        self.assertIn("&lt;script&gt;document.body.remove()&lt;/script&gt;", document)
        self.assertNotIn(attack, document)
        self.assertIn("connect-src 'none'", document)


class WorksheetRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not all(shutil.which(p) for p in ("chromium", "pdfinfo", "pdftotext")):
            raise unittest.SkipTest("Local Chromium/Poppler are not installed.")
        try:
            pdf._fonts()
        except pdf.DesignError as exc:
            raise unittest.SkipTest(str(exc)) from exc

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiple_themes_preserve_wrapped_text_and_fit_one_portrait_page(self):
        themes = [
            {"name": "Ocean detectives", "accent": "#086779", "motif": "waves",
             "icons": ["\U0001f30a", "\U0001f419", "\U0001f420", "\U0001f41a"]},
            {"name": "Space explorers", "accent": "#5143A6", "motif": "stars",
             "icons": ["\U0001f680", "\U0001fa90", "\u2b50", "\U0001f52d"]},
        ]
        for theme in themes:
            with self.subTest(theme=theme["name"]):
                spec = pdf.validate_spec({**fixture(), "theme": theme})
                target = self.root / (theme["motif"] + ".pdf")
                report = pdf.render(spec, Path(os.path.relpath(target)))
                self.assertEqual(report["pages"], 1)
                self.assertTrue(report["portrait"])
                self.assertTrue(report["text_preserved"])
                self.assertGreaterEqual(report["color_emoji_checked"], len(theme["icons"]))
                self.assertGreaterEqual(report["min_body_font_pt"], 11)
                self.assertEqual([(q["page"], q["lines"]) for q in report["answers"]], [(1, 4), (1, 0)])
                text = subprocess.run(["pdftotext", "-raw", str(target), "-"],
                                      check=True, capture_output=True, text=True).stdout
                self.assertIn(pdf._normalize(spec["questions"][0]["prompt"]), pdf._normalize(text))
                self.assertIn("Compare <sun> & <moon>", text)
                self.assertIn("Page 1 of 1", text)

    def test_page_overflow_keeps_existing_output(self):
        spec = fixture()
        spec["questions"] = [copy.deepcopy(spec["questions"][0]) for _ in range(8)]
        for question in spec["questions"]:
            question["lines"] = 8
        target = self.root / "existing.pdf"
        target.write_bytes(b"original PDF content")
        with self.assertRaisesRegex(pdf.PageLimitError, r"needs \d+ pages; max_pages is 1") as error:
            pdf.render(pdf.validate_spec(spec), target)
        self.assertGreater(error.exception.pages, 1)
        self.assertEqual(target.read_bytes(), b"original PDF content")
        self.assertEqual(list(self.root.glob(".worksheet-*")), [])

    def test_unspecified_page_budget_keeps_all_questions_and_writing_room(self):
        spec = fixture()
        spec.pop("max_pages")
        spec["questions"] = [copy.deepcopy(spec["questions"][0]) for _ in range(8)]
        for i, question in enumerate(spec["questions"]):
            question["prompt"] = f"Explain observation {i + 1}. " + question["prompt"]
            question["lines"] = 8
        report = pdf.render(pdf.validate_spec(spec), self.root / "complete.pdf")
        self.assertGreater(report["pages"], 3)
        self.assertEqual(report["questions"], 8)
        self.assertEqual([q["lines"] for q in report["answers"]], [8] * 8)
        self.assertTrue(report["text_preserved"])

    def test_blank_emoji_rendering_is_reported_without_saving_pdf(self):
        make_html = pdf.make_html

        def blank_canvas(spec):
            return make_html(spec).replace(
                "<body>", "<body><script>"
                "CanvasRenderingContext2D.prototype.fillText = function() {};"
                "</script>", 1)

        with mock.patch.object(pdf, "make_html", side_effect=blank_canvas), \
             self.assertRaisesRegex(pdf.DesignError, "not rendered in color"):
            pdf.render(pdf.validate_spec(fixture()), self.root / "bad.pdf")
        self.assertFalse((self.root / "bad.pdf").exists())


if __name__ == "__main__":
    unittest.main()
