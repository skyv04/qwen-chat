---
name: skill-creator
description: Create or improve a reusable skill package, or save a successful task workflow for future chats. Use when the user asks to make, package, save or update a skill.
compatibility: Saving needs Files enabled and a separate approval for the shared library.
---

# Create a reusable skill

Skills are reusable guidance, not restrictions on other tools or skills. A user's
suggestion of this skill is a starting point: explore other relevant skills too.

1. Establish the reusable task and its expected result. If the user asks to save
   this conversation's workflow, extract the approach that actually worked, not
   the whole chat, failed experiments, or one-off output. For a new skill, use the
   task they described; ask only for essential missing details.
2. Call list_skills to check for an existing match. Load useful related skills.
   Prefer improving a saved skill over making a duplicate. Built-ins are read-only;
   give a customized version its own clear name.
3. Draft a self-contained folder inside the workspace, such as
   `skills/ocean-quiz/`. It needs `SKILL.md` with YAML frontmatter:

   ```markdown
   ---
   name: ocean-quiz
   description: Plan ocean-themed reading quizzes with age-appropriate questions. Use when the task involves an ocean reading exercise.
   ---

   # Ocean quiz

   ## Workflow
   1. Establish the learner's grade, topic and page budget.
   2. Generate the passage and questions for that task.
   3. Use worksheet-pdf for the themed PDF presentation.
   4. Check the result and report any unresolved limitation.

   ## Inputs and checks
   Describe the required inputs, the expected output and how to check it.
   ```

4. Make the instructions concrete: when to use the skill, steps, required inputs,
   a small example, outcome checks and recovery from common errors. Keep SKILL.md
   concise (prefer under 6,000 characters; maximum 16,000 UTF-8 bytes). Put longer
   explanations in `references/`, tested helpers in `scripts/`, and necessary
   templates in `assets/`. Reference these with paths relative to the skill folder.
   A package may have at most 64 files and 10 MB in total.
5. Use lowercase names with single hyphens, at most 64 characters; the frontmatter
   name and folder name must match. The description must say what the skill does
   and when it is useful (at most 1,024 characters). Quote YAML text when it contains
   colons. Write long files in complete-line chunks with write_file/append.
6. Check the reusable procedure against a small example if possible. Test helper
   scripts with run_command when Run is enabled. Record honest limitations, not
   claims that an untested script works. Do not embed credentials, personal details,
   private chat transcripts, machine-specific absolute paths, caches or large output
   files. Package only reusable material; symlinks and hidden files are rejected.
7. Call `save_skill(path="skills/ocean-quiz")`. This validates and copies the package
   to Qwen Chat's shared library after the user reviews and approves it. Do not use
   shell commands to write the library yourself or evade a refusal. Updates go
   through the same approval and keep the previous version in the Trash.
8. On success, tell the user its `/name` and that it is available in all new and
   existing chats without restarting. Do not claim it was saved if the tool failed.

Loading a skill does not execute its scripts. If it contains supporting files,
load_skill makes a workspace copy when Files is enabled and reports its location.
Use those paths with the normal read_file/run_command tools. All command, network,
file-access and installation permissions still apply; skill metadata such as
allowed-tools never changes them.
