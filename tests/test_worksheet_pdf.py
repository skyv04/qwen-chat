import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from qwen_chat import worksheet_pdf as pdf
from qwen_chat.answer_key import (arithmetic_target, check_math, key_document, quiz_from_pdf,
                                  quiz_from_spec, resolve_choice, worksheet_key)

ASTRONAUT = "\U0001f468\u200d\U0001f680"        # man + zero-width joiner + rocket
MAN_TREX = "\U0001f468\u200d\U0001f996"         # not a real emoji: draws as two pictures


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


def key_fixture():
    return {
        "source_path": "quiz.json", "source_sha256": "a" * 64,
        "answers": [
            {"question": 1, "answer": "Compare recorded changes and explain their differences.",
             "evidence": ["Record what changes, and explain what stays the same."]},
            {"question": 2, "choice": "A", "explanation": "Observing and recording supplies evidence.",
             "evidence": ["Observe the world carefully."]},
        ],
    }


def homework_fixture():
    return {
        "title": "Moon Mission Homework",
        "theme": {"name": "Space", "accent": "#3F3D9E", "motif": "stars",
                  "icons": [ASTRONAUT, "\U0001f680", "\U0001f319"]},
        "reading": ["Mia the astronaut flew her rocket to the Moon.",
                    "She counted 3 big craters and 2 small craters."],
        "questions": [
            {"prompt": "Where did Mia fly?", "options": ["To the Moon", "To the beach"],
             "choice": "A", "evidence": "Mia the astronaut flew her rocket to the Moon."},
            {"prompt": "3 + 2 = __", "answer": "5", "lines": 1},
            {"prompt": "Draw a rocket and give it a name.", "answer": "Answers will vary.",
             "lines": 3, "icon": MAN_TREX},
        ],
        "challenge": {"prompt": "What is 10 - 4?", "answer": "six"},
        "answer_key_max_pages": 1,
    }


class WorksheetKeyTest(unittest.TestCase):
    def test_answers_in_the_worksheet_make_a_checked_key_but_never_reach_the_student(self):
        data = homework_fixture()
        document, audit = worksheet_key(pdf.validate_spec(data))
        texts = [p["text"] for p in document["reading"]]
        self.assertEqual([p["heading"] for p in document["reading"]],
                         ["Question 1:", "Question 2:", "Question 3:", "Extra Challenge:"])
        self.assertIn("Answer: A. To the Moon", texts[0])
        self.assertIn('Paragraph [1]: "Mia the astronaut flew her rocket to the Moon."', texts[0])
        self.assertTrue(texts[1].startswith("3 + 2 = __\n\nAnswer: 5"))
        self.assertEqual((document["max_pages"], document["theme"]),
                         (1, pdf.validate_spec(data)["theme"]))
        self.assertEqual({k: audit[k] for k in ("answers_checked", "multiple_choice_checked",
                                                 "evidence_quotes_checked", "arithmetic_checked",
                                                 "unverified_questions")},
                         {"answers_checked": 4, "multiple_choice_checked": 1,
                          "evidence_quotes_checked": 1, "arithmetic_checked": 2,
                          "unverified_questions": [3]})
        self.assertFalse(audit["semantic_accuracy_verified"])
        student = pdf.without_answers(data)
        self.assertNotIn("answer_key_max_pages", student)
        for q in [*student["questions"], student["challenge"]]:
            self.assertFalse(q.keys() & set(pdf.KEY_FIELDS))
        self.assertEqual(data["questions"][0]["choice"], "A")  # the saved draft is untouched
        with mock.patch.object(pdf, "_fonts", return_value=""):
            for spec in (data, student):
                page = pdf.make_html(pdf.validate_spec(spec))
                self.assertNotIn("Answers will vary", page)
                self.assertNotIn("six", page)

    def test_every_answer_problem_is_reported_in_one_message(self):
        data = homework_fixture()
        data["questions"][0]["choice"] = "C"
        data["questions"][1]["answer"] = "6"
        del data["questions"][2]["answer"]
        data["challenge"]["evidence"] = "Mia found a dinosaur on the Moon."
        with self.assertRaises(pdf.DesignError) as error:
            worksheet_key(pdf.validate_spec(data))
        message = str(error.exception)
        for expected in ("4 answer problem(s)", "Nothing was rendered",
                         "Question 1: choice must be one of its actual option letters (A, B)",
                         "Question 2: 3 + 2 = __ gives 5, but its answer is '6'",
                         "Question 3 needs answer", "challenge: evidence is not in the source"):
            self.assertIn(expected, message)
        for question in data["questions"]:
            question.pop("choice", None)
            question.pop("answer", None)
        data.pop("challenge")
        with self.assertRaisesRegex(pdf.DesignError, "No answers found"):
            worksheet_key(pdf.validate_spec(data))

    def test_choices_accept_letters_or_exact_text_but_never_a_rewritten_option(self):
        options = ["A wet umbrella", "A book"]
        for choice, answer in (("B", ""), ("(b)", ""), ("", "B"), ("", "B. A book"),
                               ("", "a book"), ("B", "(B) A book")):
            with self.subTest(choice=choice, answer=answer):
                self.assertEqual(resolve_choice("Q", options, choice, answer), ("B", "A book"))
        for choice, answer, message in (("C", "", "actual option letters"),
                                        ("A", "A book", "choice A is 'A wet umbrella', but answer "
                                                        "'A book' is option B. Solve the question again"),
                                        ("A", "A blue book", "is not option A"),
                                        ("", "A blue book", "does not name exactly one option")):
            with self.subTest(choice=choice, answer=answer), \
                    self.assertRaisesRegex(pdf.DesignError, message):
                resolve_choice("Q", options, choice, answer)

    def test_simple_sums_are_checked_and_unclear_numbers_are_left_alone(self):
        for prompt, value in (("3 + 4 = __", 7), ("What is 10 - 3?", 7), ("4 + __ = 9", 5),
                              ("__ = 2 \u00d7 6", 12), ("Solve: 12 \u00f7 3 = ?", 4),
                              ("2 + 3 x 4 = ?", 14)):
            with self.subTest(prompt=prompt):
                self.assertEqual(arithmetic_target(prompt)[1], value)
        for prompt in ("Count the 3 apples and 2 pears.", "Read pages 1-2.", "7 - 9 = __",
                       "Is 2 + 2 = 4 and 3 + 3 = 6?", "It starts at 3:15 + 2 minutes = ?",
                       "Share 7 \u00f7 2 = ?"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(arithmetic_target(prompt))
        self.assertTrue(check_math("Q", "What is 9 + 9?", "eighteen"))
        self.assertFalse(check_math("Q", "Name a planet.", "Mars"))
        with self.assertRaisesRegex(pdf.DesignError, "gives 18"):
            check_math("Q", "What is 9 + 9?", "19")

    def test_worked_steps_must_add_up_and_reach_the_chosen_answer(self):
        def question(**key):
            return {"prompt": "We have $10. Apples cost $6 and bananas cost $4. "
                              "How much money is left?", "options": ["$0", "$1", "$2"], **key}

        # From a real Qwen draft: its letter pointed at $1 while its own working gave $0.
        for key, message in (
                ({"choice": "B", "explanation": "$10 - $6 - $4 = $0"},
                 "Question 1: the working ends at 0, but the answer is 'B. $1'"),
                ({"choice": "A", "explanation": "$10 - $6 - $4 = $1"},
                 "Question 1: the working says 10 - 6 - 4 = 1, but 10 - 6 - 4 = 0"),
                ({"choice": "B", "answer": "$0"},
                 "Question 1: choice B is '$1', but answer '$0' is option A")):
            with self.subTest(key=key), self.assertRaisesRegex(pdf.DesignError, re.escape(message)):
                worksheet_key(pdf.validate_spec({"title": "Shop", "questions": [question(**key)]}))
        data = {"title": "Shop", "questions": [
            question(choice="A", explanation="$10 - $6 - $4 = $0"),
            # 7 + 3 = 10 finds the missing 3, which is not one of the question's numbers.
            {"prompt": "Ana has 7 apples. How many more does she need to have 10?", "answer": "3",
             "explanation": "7 + 3 = 10"},
            # Not a "how many" question, so the steps need not end at the answer.
            {"prompt": "Which is bigger: 3 + 4 or 9?", "options": ["3 + 4", "9"], "choice": "B",
             "explanation": "3 + 4 = 7, and 9 is more than 7."},
            # A dash used as punctuation does not join unrelated numbers into one step
            # (30 - 10 - 3 - 2 would wrongly fail).
            {"prompt": "Mia has 30 stickers and Sam has 10. Sam gives away 3 and then 2. "
                       "How many does Sam have left?", "answer": "5",
             "explanation": "Mia has 30 - Sam has 10 - 3 - 2 = 5"}]}
        document, audit = worksheet_key(pdf.validate_spec(data))
        self.assertEqual((audit["worked_math_checked"], audit["unverified_questions"]), (2, [2, 3]))
        self.assertIn("Answer: A. $0\nWhy: $10 - $6 - $4 = $0", document["reading"][0]["text"])

    def test_reasons_given_as_evidence_on_a_math_sheet_become_explanations(self):
        data = {"title": "Harvest math", "questions": [
            {"prompt": "3 + 4 = __", "answer": "7", "evidence": "3 apples and 4 more make 7."},
            {"prompt": "Count the pumpkins: 2 and 2.", "answer": "4",
             "evidence": "Count the pumpkins"}]}
        document, audit = worksheet_key(pdf.validate_spec(data))
        texts = [p["text"] for p in document["reading"]]
        self.assertIn("Answer: 7\nWhy: 3 apples and 4 more make 7.", texts[0])
        self.assertNotIn("Evidence:", texts[0])
        self.assertIn('Evidence: "Count the pumpkins"', texts[1])
        self.assertEqual(audit["evidence_used_as_explanation"], [1])
        self.assertEqual(list(audit)[-1], "semantic_accuracy_verified")
        self.assertNotIn("evidence_used_as_explanation", worksheet_key(pdf.validate_spec(
            homework_fixture()))[1])
        # With a reading, evidence must still quote it; nothing is moved silently.
        data["reading"] = ["Sam picked 3 apples."]
        with self.assertRaises(pdf.DesignError) as error:
            worksheet_key(pdf.validate_spec(data))
        self.assertIn("2 answer problem(s)", str(error.exception))
        self.assertIn("or move this sentence to explanation. Do not add sentences to the reading",
                      str(error.exception))
        # A reason the explanation already gives is not repeated, and pieces stay sentences.
        data = {"title": "Shop", "questions": [
            {"prompt": "5 cookies cost $1 each. How much in total?", "answer": "$5",
             "explanation": "Total: 5 × $1 = $5", "evidence": ["5 × 1 = 5", "Each cookie is $1"]}]}
        text = worksheet_key(pdf.validate_spec(data))[0]["reading"][0]["text"]
        self.assertIn("Why: Total: 5 × $1 = $5. Each cookie is $1\n", text + "\n")


class NormalizeSpecTest(unittest.TestCase):
    def test_common_near_misses_are_fixed_and_named(self):
        draft = {
            "title": "Harvest", "passage": "Sam picked apples.\n\n  He counted them.\n",
            "theme": {"motif": "pumpkins", "accent": "#D4A574"},
            "questions": [
                {"question": "1. How many apples?", "type": "multiple_choice",
                 "choices": ["A. 3", "B) 4"], "correct_answer": "B", "lines": 2},
                {"text": "2. Name a fruit.", "points": 2, "lines": 0},
            ],
            "challenge": {"question": "What else grows on a farm?", "lines": 40},
        }
        original = copy.deepcopy(draft)
        data, notes = pdf.normalize_spec(draft)
        self.assertEqual(draft, original)  # the caller's draft is not changed
        self.assertEqual(data, {
            "title": "Harvest", "reading": ["Sam picked apples.", "He counted them."],
            "theme": {"motif": "dots", "accent": "#8A6B4B"},
            "questions": [{"prompt": "How many apples?", "options": ["3", "4"], "answer": "B", "lines": 2},
                          {"prompt": "Name a fruit.", "lines": 1}],
            "challenge": {"prompt": "What else grows on a farm?", "lines": 12}})
        self.assertGreaterEqual(pdf._contrast(data["theme"]["accent"]), 4.5)
        pdf.validate_spec(data)
        joined = "; ".join(notes)
        for expected in ("read 'passage' as 'reading'",
                         "read question field 'question' as 'prompt' (Question 1, challenge)",
                         "ignored question field 'type' (options make a question multiple choice) "
                         "(Question 1)",
                         "ignored question field 'points' (Question 2)",
                         "removed typed A./B. letters from options (the PDF adds them) (Question 1)",
                         "lines must be 1-12 here, so 1 was used (Question 2)",
                         "lines must be 1-12 here, so 12 was used (challenge)",
                         "removed typed numbers from prompts (the PDF numbers questions)"):
            self.assertIn(expected, joined)

    def test_text_theme_loose_icons_answer_text_and_extra_fields_are_handled(self):
        # Shaped like a real Qwen draft that failed twice before anything rendered.
        draft = {"title": "Shopping math", "passage": "", "theme": "colorful",
                 "icons": "\U0001f6d2 \U0001f34e", "margin_art": "", "font_size": 14,
                 "questions": [
                     {"question": "3 apples cost $2 each. How much in total?",
                      "options": ["$5", "$6"], "answer": "B", "answer_text": "$6",
                      "sticker": "\U0001f34e"},
                     {"prompt": "We have $20 and spend $12. How much is left?", "answer": "$8",
                      "answer_text": "$20 - $12 = $8", "hint": ""}]}
        original = copy.deepcopy(draft)
        data, notes = pdf.normalize_spec(draft)
        self.assertEqual(draft, original)
        self.assertEqual(data, {
            "title": "Shopping math", "reading": [],
            "theme": {"name": "Colorful", "icons": ["\U0001f6d2", "\U0001f34e"]},
            "questions": [
                {"prompt": "3 apples cost $2 each. How much in total?", "options": ["$5", "$6"],
                 "choice": "B", "answer": "$6"},
                {"prompt": "We have $20 and spend $12. How much is left?", "answer": "$8",
                 "explanation": "$20 - $12 = $8"}]})
        joined = "; ".join(notes)
        for expected in ("theme was text, so it was used as theme.name",
                         "moved 'icons' into theme.icons",
                         "ignored field 'margin_art' (empty)",
                         "ignored field 'font_size' (the theme sets the look)",
                         "read the option letter as 'choice' and the option text as 'answer' "
                         "(Question 1)",
                         "ignored question field 'sticker' (the theme sets the look) (Question 1)",
                         "read question field 'answer_text' as 'explanation' (Question 2)",
                         "ignored question field 'hint' (empty) (Question 2)"):
            self.assertIn(expected, joined)
        self.assertEqual(worksheet_key(pdf.validate_spec(data))[1]["worked_math_checked"], 1)
        # Extra fields that may hold content are still reported, never dropped.
        data, _ = pdf.normalize_spec({"title": "Math", "theme": "stars", "word_bank": ["apple"],
                                      "questions": [{"prompt": "Q?"}]})
        self.assertEqual(data["theme"], {"motif": "stars"})
        with self.assertRaisesRegex(pdf.DesignError, r"Unknown document field\(s\): word_bank\."):
            pdf.validate_spec(data)

    def test_lookalikes_are_left_for_validation_to_report(self):
        for draft in (
                {"title": "Math", "questions": [
                    {"prompt": "3 + 4 = __", "options": ["A. 7", "C. 8"]},
                    {"prompt": "1. Count.", "label": "Part 1:"}]},
                {"title": "Math", "questions": [{"prompt": "2. Read."}, {"prompt": "5. Write."}]},
                {"title": "Math", "reading": ["Kept."], "passage": "Also here.",
                 "questions": [{"prompt": "Q?", "hint": "x"}]}):
            with self.subTest(draft=draft):
                self.assertEqual(pdf.normalize_spec(draft), (draft, []))
        self.assertEqual(pdf.normalize_spec("text"), ("text", []))
        with self.assertRaisesRegex(pdf.DesignError, r"Unknown Question 1 field\(s\): hint\. "
                                    r"Allowed: answer, choice, evidence, explanation, icon, label"):
            pdf.validate_spec({"title": "Math", "questions": [{"prompt": "Q?", "hint": "x"}]})


class AnswerKeySpecTest(unittest.TestCase):
    def compile(self, key):
        return key_document(key, quiz_from_spec(fixture()), "quiz.json", "a" * 64)

    def test_source_identity_coverage_choices_and_evidence_fail_closed(self):
        cases = []
        stale = key_fixture()
        stale["source_sha256"] = "b" * 64
        cases.append((stale, "current source"))
        for numbers in ([1], [1, 1], [1, 2, 3], [True, 2]):
            key = key_fixture()
            key["answers"] = [{"question": n} for n in numbers]
            cases.append((key, "exactly once"))
        for changes, expected in (
                ({"evidence": ["Detective Martinez found a rope ladder at the broken window."]}, "not in the source"),
                ({"prompt": "What did Detective Martinez find?"}, "Unknown answer field"),
                ({"points": 10}, "Unknown answer field")):
            key = key_fixture()
            key["answers"][0].update(changes)
            cases.append((key, expected))
        for changes, expected in (
                ({"choice": "H"}, "actual option"),
                ({"choice": ""}, "defensible"),
                ({"answer": "A rope ladder."}, "do not invent")):
            key = key_fixture()
            key["answers"][1].update(changes)
            cases.append((key, expected))
        for key, message in cases:
            with self.subTest(key=key), self.assertRaisesRegex(pdf.DesignError, message):
                self.compile(key)

    def test_invalid_question_is_flagged_without_forcing_an_option(self):
        key = key_fixture()
        key["answers"][1].pop("choice")
        key["answers"][1]["issue"] = "The choices do not establish a unique answer."
        document, audit = self.compile(key)
        text = "\n".join(pdf.source_texts(pdf.validate_spec(document)))
        self.assertIn("Source issue:", text)
        self.assertIn(fixture()["questions"][1]["prompt"], text)
        self.assertEqual(audit["source_issues"], [2])
        self.assertEqual(audit["questions_answered"], 2)
        self.assertFalse(audit["semantic_accuracy_verified"])

    def test_companion_json_must_match_the_pdf_and_its_question_count(self):
        text = "\n".join(pdf.source_texts(pdf.validate_spec(fixture())))
        self.assertEqual(len(quiz_from_spec(fixture(), text)["questions"]), 2)
        for content in (text.replace("Observe the world", "Invent a story"),
                        text + "\nQuestion 3:\nAn omitted question"):
            with self.assertRaisesRegex(pdf.DesignError, "match"):
                quiz_from_spec(fixture(), content)

    def test_pdf_only_reader_rejects_gaps_and_preserves_wrapped_options(self):
        source = (
            "A rainy visit\n[1] Nora left her wet umbrella by the door.\n"
            "01\nQuestion 1:\nWhat did Nora leave?\nA. A wet\numbrella\nB. A book\n"
            "Page 1 of 2\n\fA rainy visit\n02\nQuestion 2:\nWhat might the umbrella tell us?\n"
            "\U0001f3af Check your evidence before you finish.\n"
            "Page 2 of 2\n")
        quiz = quiz_from_pdf(source, "A rainy visit")
        self.assertEqual(quiz["questions"][0]["options"], ["A wet umbrella", "A book"])
        self.assertEqual(quiz["questions"][1]["prompt"], "What might the umbrella tell us?")
        self.assertEqual(quiz["passages"], ["Nora left her wet umbrella by the door."])
        for bad in (source.replace("Question 2:", "Question 3:"),
                    source.replace("B. A book", "C. A book"), "Questions without numbering"):
            with self.assertRaises(pdf.DesignError):
                quiz_from_pdf(bad, "Quiz")

    def test_bonus_label_and_complete_short_evidence_are_preserved(self):
        quiz = quiz_from_spec({"title": "Quick maths", "challenge": {"prompt": "2 + 2 = ?"}})
        key = {"source_path": "quiz.json", "source_sha256": "a" * 64,
               "answers": [{"question": 1, "answer": "4", "evidence": ["2 + 2 = ?"]}]}
        document, audit = key_document(key, quiz, "quiz.json", "a" * 64)
        self.assertEqual(document["reading"][0]["heading"], "Extra Challenge:")
        self.assertEqual(audit["questions_answered"], 1)


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

    def test_joined_emoji_are_one_icon_and_errors_name_the_exact_field(self):
        for icon in (ASTRONAUT, "\U0001f469\U0001f3fd\u200d\U0001f52c", "1\ufe0f\u20e3",
                     "\U0001f1fa\U0001f1f8", "\u2764\ufe0f", MAN_TREX):
            with self.subTest(icon=icon):
                spec = pdf.validate_spec({**fixture(), "theme": {"icons": [icon]}})
                self.assertEqual(spec["theme"]["icons"], [icon])
        for change, message in (
                ({"theme": {"icons": ["\U0001f680", "rocket"]}}, r"theme\.icons\[1\] must be one emoji"),
                ({"questions": [{"prompt": "Count.", "icon": "\U0001f680\U0001f319"}]},
                 "Question 1 icon has 2 separate emoji"),
                ({"challenge": {"prompt": "Count.", "points": 5}}, "challenge")):
            with self.subTest(change=change), self.assertRaisesRegex(pdf.DesignError, message):
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

    def test_homework_and_its_key_render_with_joined_emoji_and_no_student_answers(self):
        data = homework_fixture()
        student = self.root / "homework.pdf"
        report = pdf.render(pdf.validate_spec(pdf.without_answers(data)), student)
        self.assertTrue(report["text_preserved"])
        self.assertEqual(report["icon_substitutions"], [{"icon": MAN_TREX, "used": "\U0001f996"}])
        text = subprocess.run(["pdftotext", "-raw", str(student), "-"],
                              check=True, capture_output=True, text=True).stdout
        self.assertNotIn("Answers will vary", text)
        self.assertNotIn("Answer:", text)
        document, _audit = worksheet_key(pdf.validate_spec(data))
        key = self.root / "homework-key.pdf"
        report = pdf.render(pdf.validate_spec(document), key)
        self.assertEqual(report["pages"], 1)
        self.assertNotIn("icon_substitutions", report)
        text = subprocess.run(["pdftotext", "-raw", str(key), "-"],
                              check=True, capture_output=True, text=True).stdout
        for expected in ("Answer: A. To the Moon", "Answer: 5", "Answer: six", "Paragraph [1]"):
            self.assertIn(expected, text)
        self.assertNotIn("Name: ______", text)

    def test_answer_key_pdf_preserves_questions_choices_and_evidence_without_blank_lines(self):
        document, source_audit = key_document(key_fixture(), quiz_from_spec(fixture()),
                                               "quiz.json", "a" * 64)
        target = self.root / "answer-key.pdf"
        report = pdf.render(pdf.validate_spec(document), target)
        text = subprocess.run(["pdftotext", "-raw", str(target), "-"],
                              check=True, capture_output=True, text=True).stdout
        for question in fixture()["questions"]:
            self.assertIn(pdf._normalize(question["prompt"]), pdf._normalize(text))
        self.assertIn("A. Look carefully and record a detail.", text)
        self.assertIn("Source excerpt 1", text)
        self.assertIn("Compare recorded changes", text)
        self.assertEqual(source_audit["questions_answered"], 2)
        self.assertTrue(report["text_preserved"])
        self.assertEqual(report["answers"], [])
        self.assertNotIn("Name: ______", text)


if __name__ == "__main__":
    unittest.main()
