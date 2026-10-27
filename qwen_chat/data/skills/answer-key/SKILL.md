---
name: answer-key
description: Make source-grounded answer-key PDFs for existing quizzes. Verify every question and option, quote evidence, check simple math, flag invalid questions and confirm the exported file exists.
compatibility: Qwen Chat with Files and Run enabled; uses read_quiz, render_answer_key and open_file.
---

# Answer keys from the actual quiz

The source quiz is fixed. Your job is to answer it, not generate a different story
or rewrite questions to match remembered answers. This is not appearance-only
restyling and it is not the student worksheet JSON format.

## Short workflow

1. Call `read_quiz(path="practice/quiz.pdf")` on the actual requested file, even
   when continuing an earlier conversation. Read every continuation with the
   returned `start_line`. It returns `source_path`, `source_sha256`, numbered
   `source_excerpts` and the exact `questions` and `options`.
   An existing, matching worksheet JSON is checked against the PDF. Without one,
   the reader supports extractable text with sequential `Question 1:` or `1.`
   headings and `A.` option labels. Check that extraction contains the whole quiz;
   do not use a partial inventory or trust decorative headers as question text.
   If extraction fails, use `read_file`/OCR and resolve the structure first.
   A scan or unnumbered PDF is not an excuse to invent questions.
2. Answer every numbered question exactly once, including a bonus if returned.
   Work each one out before writing it: do the arithmetic, then find the sentence
   that proves the answer. Simple sums in prompts are recomputed by the tool.
   Read the passage and actual choices, not an old draft key or the chat summary.
   For each answer, select short **exact source quotations** supporting it.
   A quote's presence alone does not prove the conclusion: check its meaning.
   Distinguish what the passage says, an inference, and a possible model response.
3. Write a separate JSON file in the format below. Keep each write_file call
   under 8,000 characters; append at object boundaries when needed. Copy the
   `source_path` and `source_sha256` exactly from read_quiz. Do not overwrite the
   quiz, its JSON, or the only existing key while drafting.
4. Call `render_answer_key(spec_path="practice/key.json", path="practice/key.pdf")`.
   This uses the existing themed PDF renderer, with answers instead of blank
   writing lines. It copies question text and chosen options from the source.
   Read the source/coverage and layout reports. Fix the specific failed field,
   not the story or question identities. Never use render_worksheet for this
   key JSON, rebuild the quiz as new questions, or pass a text file to Ghostscript.
5. Only report a PDF ready after a successful render. Mention source defects
   that affect marking. If asked to open it, call `open_file(path="practice/key.pdf")`;
   no `xdg-open ... &`. Say the desktop accepted an open request, not that you
   saw the window. If an operation fails, say what remains unfinished.

## Key JSON

All text is plain text, not Markdown/HTML. Unknown fields are rejected.
The example assumes a source with exactly two questions; use the actual inventory,
hash, letters and quotes from read_quiz, never this example's answers.

```json
{
  "source_path": "practice/quiz.pdf",
  "source_sha256": "COPY THE EXACT HASH RETURNED BY read_quiz",
  "answers": [
    {
      "question": 1,
      "choice": "B",
      "explanation": "The wet umbrella supports the inference that Nora was outside in the rain.",
      "evidence": ["Nora placed her dripping umbrella beside the door."]
    },
    {
      "question": 2,
      "answer": "A possible response is to ask when the umbrella was last used. This could help establish the timing.",
      "evidence": ["Nora placed her dripping umbrella beside the door."]
    }
  ]
}
```

- `question`: the integer number returned by read_quiz. No duplicates, omissions
  or extra answers. Do not copy/rewrite prompts, options, story names or labels
  into the key format; the renderer gets them from the source.
- `choice`: for multiple choice, one actual uppercase option letter. Omit `answer`
  for these: the exact chosen option is inserted automatically.
- `answer`: required for written questions. Accept equivalent supported answers.
  Use concise complete sentences, normally 1-3 sentences per answer.
- `explanation`: explain a multiple-choice selection; optional for written answers
  whose reasoning is already in `answer`. Do not exaggerate a clue into proof.
- `evidence`: 1-6 exact quotes from `source_excerpts`, each at least 12 non-space
  characters (or the complete excerpt if it is shorter). No invented quotes,
  paraphrases in quotation marks or ellipses.
  Use the smallest meaningful detail; the PDF adds source-excerpt references.
- `issue`: optional explanation of a defective/ambiguous question, contradiction
  or unsupported deduction. If **no option is defensible**, omit `choice`,
  explain the defect in `issue`, and quote the relevant evidence. Never force
  a wrong letter just to fill the key. Where an intended choice is only weakly
  supported, give that choice and state the caveat in `issue`.
- `max_pages`: set only when the user requested a page limit, from 1 to 10.
  Otherwise omit it. Approval is required if the complete readable key needs more.
- Do not invent `points`, scores, rubrics, answer choices or factual corrections.
  If corrections are wanted, ask before changing the original quiz.

## Combined quiz and parent page

For a NEW quiz or homework plus a parent key, skip this read_quiz flow. Load
worksheet-pdf, put each answer in the worksheet JSON, and create both PDFs with one
`render_worksheet(spec_path=..., path=..., answer_key_path=...)` call. It checks every
answer first and keeps answers off the student copy.
"At most three student pages plus a one-page parent key" means `max_pages: 3` and
`answer_key_max_pages: 1` in that JSON, not four student pages.
Check both actual page reports before using `pdfunite` if one combined file was
requested. Preserve the source files and verify the merged PDF's page count.
Do not claim the key is the fourth page when there are already four student pages.

The harness checks source identity, question coverage, choice identity, quote
presence, readable layout and exported text. It **does not independently certify
educational correctness** or prove that a quotation logically supports an answer.
