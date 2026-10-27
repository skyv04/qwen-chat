---
name: worksheet-pdf
description: Render reading quizzes, homework and worksheets with coherent visual themes, color emoji, portrait pages and usable answer space, plus a matching parent answer key in the same call. Use for new quiz PDFs or visual redesigns.
compatibility: Qwen Chat with Files and Run enabled; uses its render_worksheet tool.
---

# Worksheet PDF design

Write the content as a JSON file, then call the bundled `render_worksheet` tool. Do not
write a ReportLab/FPDF script. For a NEW worksheet that also needs a parent/teacher key,
put the answers in the same JSON and pass `answer_key_path`: one call checks every
answer, then makes the student PDF without answers and a key in the same theme. For a
key to an EXISTING quiz, load `answer-key` and use `read_quiz` and `render_answer_key`.

## Steps

1. Plan the exact question mix. "3 multiple choice" means three separate questions with
   options. Five questions including three multiple choice means three with options and
   two written. Do not add extra questions or an unrequested challenge.
2. Make the content correct. A story may be invented, but its science, history and math
   must be true. Each multiple-choice question needs exactly one correct option. When a
   key is wanted, work out every answer first: do each sum, find the sentence that
   proves each reading answer, and note the correct letter.
3. For an appearance-only change, read the latest file and keep the passage, questions
   and options exactly; reuse its JSON when it matches, and save the PDF under a new
   name. PDF text cannot show you how a reference looks.
4. Pick a theme: a short name, one dark accent color, 3-5 related emoji and a motif
   (`leaves`, `waves`, `stars` or `dots`). For example: Rainforest `#176B56` leaves
   🌿 🦜 🐆; Ocean `#086779` waves 🌊 🐙 🐠; Space `#5143A6` stars 🚀 🪐 ⭐. For other
   topics pick matching emoji and `dots`.
5. Write the JSON with write_file (it creates folders), with the answers in this first
   write. Keep each call under 8,000 characters; append more at item boundaries if
   needed. Omit `max_pages` unless the user set a page limit. Student and key budgets
   are separate: three student pages plus a one-page key is `max_pages: 3` and
   `answer_key_max_pages: 1`.
6. Call `render_worksheet(spec_path="homework/quiz.json", path="homework/quiz.pdf",
   answer_key_path="homework/quiz-key.pdf", expected_questions=5,
   expected_multiple_choice=3)`. Take the counts from the user's request, not from the
   draft. Leave out `answer_key_path` when no key is wanted. The three names must differ.
7. If it fails, the error names the exact field or value and lists every answer problem.
   Fix exactly those spots with edit_file, then render again. Do not rewrite the whole
   file to change one value, and never resubmit unchanged JSON: the same error three
   times stops the task.
8. If a page limit is too tight, the tool asks the user to allow more pages. Do not
   remove questions, shorten the reading or shrink writing space to avoid that. After a
   failed page-limit run, render the saved JSON again. If the user says no, stop and ask.
9. Report the actual files and page counts. The checks are not a visual review: you
   cannot see the pages, so never claim you looked at them.

## Format

All text is plain: no HTML, Markdown or scripts. Unknown fields are rejected.

```json
{
  "title": "A Day on the Farm",
  "instructions": "Read the story, then answer the questions.",
  "theme": {"name": "Farm day", "accent": "#2F6B2F", "motif": "dots",
            "icons": ["🚜", "🐄", "🍎", "🌽"]},
  "reading": ["Sam picked 3 big pumpkins and 2 small pumpkins at the farm.",
              "The farmer gave Sam a red apple to take home."],
  "questions": [
    {"prompt": "What did the farmer give Sam?",
     "options": ["A red apple", "A pumpkin pie", "A corn cob"],
     "choice": "A", "evidence": "The farmer gave Sam a red apple"},
    {"prompt": "3 + 2 = __", "lines": 1, "answer": "5",
     "explanation": "3 big pumpkins and 2 small pumpkins make 5."},
    {"prompt": "Draw your favorite pumpkin and give it a name.", "lines": 3,
     "answer": "Answers will vary, for example: a round pumpkin called Sunny."}
  ]
}
```

- `reading`: paragraphs as strings, or `{"heading": "...", "text": "..."}`. Leave it out
  for a math or activity sheet; never add sentences to it just to quote them.
- `prompt` is the question only and `options` are the choices only: the PDF adds
  "Question 1:" and the A/B/C letters itself.
- `lines`: Use about 4-6 answer lines for explanations and evidence, 1-2 for short facts
  or sums, and 0 for multiple choice (1-12 without options, 0-12 with options).
- Optional per question: `icon` (one emoji) and `label` (a custom label).
- `challenge`: optional, same shape as a question, only when requested; it is not
  counted in the expected question numbers.
- Other optional fields: `subtitle`, `series`, `student_line`, `meta`, `reading_title`,
  `reading_note`, `question_title`, `closing_note`, `max_pages`, `answer_key_max_pages`.

Answer fields, only for a parent key (never printed on the student PDF):
- `choice`: the correct option's letter (A is the first option); required for
  multiple choice.
- `answer`: the expected answer; required for written questions. For open or creative
  prompts write "Answers will vary, for example: ...".
- `evidence`: an exact quote, or a list of up to six, copied word for word from the
  reading. Without a reading, put the reason in `explanation` instead.
- `explanation`: an optional short reason, shown to the parent as "Why:". For a word
  problem, write the steps with the question's numbers, ending at the answer:
  `"$10 - $6 - $4 = $0"`.

The tool checks that every question has an answer, each letter is a real option,
quotes really appear, simple sums like `3 + 2 = __` or `What is 10 - 4?` are right, and
written steps add up and end at the chosen answer.
It cannot judge facts or reasoning, so check those yourself.

## Emoji and output

Each icon is one emoji picture, not words or two emoji. Joined emoji (👨‍🚀), skin tones,
keycaps (1️⃣) and flags are fine; if the font cannot draw one, a similar emoji is swapped
in automatically (`icon_substitutions`) and no retry is needed. Pages are portrait
Letter with embedded fonts, color emoji, numbered paragraphs, page numbers, margin art
and ruled answer space. If a program or font is missing, the error says what to
install with install_packages.
