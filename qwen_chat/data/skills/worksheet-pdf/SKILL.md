---
name: worksheet-pdf
description: Render reading quizzes and worksheets with coherent visual themes, color emoji, portrait pages and usable answer space. Use for new quiz PDFs or visual redesigns.
compatibility: Qwen Chat with Files and Run enabled; uses its render_worksheet tool.
---

# Worksheet PDF design

Use this skill for reading-comprehension PDFs, quizzes, tests, activity sheets,
and visual redesigns of those documents. Use the bundled `render_worksheet`
tool, not a freshly invented ReportLab/FPDF script.
For a new quiz, generate fresh content for the requested topic and grade normally.
This skill handles the visual presentation, not a requirement to reuse old wording.

## Work in this order

1. Establish the requested question mix before drafting. "3 multiple choice" means
   exactly three separate questions with options, not one question with three options.
   Keep the requested total as well: five questions including three multiple choice
   means three with options and two written responses. Do not expand a short quiz
   into ten questions plus an unrequested challenge. Check that answers are supported
   by the passage; label predictions/inferences clearly.
2. Read the source/reference and the latest saved file. When the request is
   about appearance, copy the passage, questions and options exactly. Do not
   rewrite, simplify, remove questions, invent an answer key, or alter difficulty.
   PDF text extraction does not let you see a reference's visual appearance.
   Reuse a worksheet's existing JSON specification when it matches the current
   source, rather than rebuilding its content from decorative PDF headers and icons.
3. Choose a coherent theme: a short name, one dark accent color, 3-5 related emoji,
   and a margin motif. For example:
   - Rainforest: `#176B56`, `leaves`, 🌿 🦜 🐆 🌎
   - Ocean: `#086779`, `waves`, 🌊 🐙 🐠 🐚
   - Space: `#5143A6`, `stars`, 🚀 🪐 ⭐ 🔭
   - Another topic: pick suitable emoji and use `dots` if no other motif fits.
   Decorations belong in headers, margins, section markers and question badges;
   never behind the passage or across answer lines. More decoration should not
   mean less room to read or write.
4. Write a plain JSON specification in the workspace. Omit `max_pages` unless the
   user requested a page limit; do not copy a limit from an example. With no limit,
   the renderer uses the pages the content needs, up to ten. Keep each write_file call
   below 8,000 characters; append at JSON field/item boundaries if necessary,
   never inside quoted text. This is content
   data, not code. Preserve an existing worksheet by using a new output filename.
5. Call `render_worksheet(spec_path="practice/quiz.json", path="practice/quiz.pdf")`.
   Pass `expected_multiple_choice=3` when the user requested three multiple-choice
   questions, and `expected_questions=5` when they requested five total. These values
   come from the request, not the draft's current counts. A mismatch is reported
   before rendering, so correct the question mix, not the expected count.
   The renderer gives portrait Letter pages, embedded fonts and actual color emoji,
   numbered paragraphs, consistent headings, page numbers, thematic margin art,
   and ruled answer areas. It starts with useful content, not a mostly empty cover.
6. Read the result. It checks page limits, overflow, readable type, intact question
   blocks, answer-line placement, visible color emoji, and that every supplied text
   block survives PDF export. On failure, correct the indicated data/layout issue
   and retry. When a page limit is too tight, the tool asks the user directly to allow
   the pages needed, then finishes the same export if approved. Do not rewrite the
   whole JSON, remove questions, shorten the passage or reduce writing room to avoid
   that decision. For a previously failed page-limit run, reuse the saved JSON and
   call render_worksheet again. If the user declines, stop and ask how they want to
   revise the task. Never remove source text or shrink fonts to hide a failed check.
   These checks are not a visual review: you cannot see images. Do not claim you
   inspected the rendered pages or that the PDF is "beautiful" because a command
   exited successfully. Report the actual file, page count and any limitation.

## Specification

All text is literal: no HTML, CSS, Markdown markup, file URLs or scripts.
Optional fields can be omitted; unknown fields are rejected.

```json
{
  "title": "The Amazon Rainforest Mystery",
  "subtitle": "A reading adventure",
  "series": "Adventure Readers Club",
  "student_line": "Name: ____________________    Date: ____________________",
  "meta": "Time: 30-40 minutes",
  "instructions": "Read the passage, then answer using evidence from the text.",
  "theme": {
    "name": "Rainforest explorers",
    "accent": "#176B56",
    "motif": "leaves",
    "icons": ["🌿", "🦜", "🐆", "🌎"]
  },
  "reading_title": "Read & explore",
  "reading": [
    {"heading": "Optional section heading", "text": "The full original paragraph."},
    {"text": "The next full paragraph. Each object stays together on a page."}
  ],
  "reading_note": "Optional reminder, copied from the original when restyling.",
  "question_title": "Detective Questions",
  "questions": [
    {"prompt": "Which statement matches the passage?", "options": ["First option", "Second option", "Third option"], "lines": 0},
    {"prompt": "Which detail supports that idea?", "options": ["First detail", "Second detail", "Third detail"], "lines": 0},
    {"prompt": "Which conclusion follows from the evidence?", "options": ["First conclusion", "Second conclusion", "Third conclusion"], "lines": 0},
    {"prompt": "Explain using two details from the passage.", "lines": 5, "icon": "🔍"},
    {"prompt": "Compare the two ideas in the passage.", "lines": 4}
  ],
  "closing_note": "Check your evidence before you finish."
}
```

`reading` may also contain plain paragraph strings. A question can supply `label`
to preserve a section/question label; otherwise it gets "Question 1:", etc.
Do not add a challenge or reminder unless requested or present in the source.
An optional `challenge` has the same shape as a question and is not included in the
expected main-question counts.

Use about 4-6 answer lines for explanations, comparisons and evidence; fewer for
brief facts, and zero for multiple choice. `lines` accepts 1-12 without options
or 0-12 with options. Optional `max_pages` accepts 1-10. A specified limit is never
increased without approval. An unrequested limit of three pages is not a design rule.

Theme icons are individual emoji, not words, flags or joined family sequences.
If an icon fails its real rendering check, replace only that decoration with
another suitable one. The renderer uses the existing Chromium printing engine
because a phone's COLRv1 emoji font can be blank in ReportLab/Pillow.
No web images, design subscriptions or large publishing toolchain are needed.
Missing dependencies are reported explicitly; use check_programs/install_packages
only if requested by the error (Chromium, poppler-utils or the named fonts).
