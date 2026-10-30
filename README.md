# Qwen Chat

A native GTK 4 / libadwaita chat app for a local Qwen model that runs on the phone's
Adreno GPU through llama.cpp (Vulkan). Built for Debian in PRoot on Termux with XFCE.

## Install

The complete, pinned [Termux setup guide](docs/termux-setup.md) covers the GPU
prerequisite, Debian packages, llama.cpp build, model download/checksum and
first launch. Start there if you do not already have a working Vulkan backend.
The tested device is an Adreno 840 phone with about 14 GiB of usable RAM;
this is not a promise that the same model fits every Android phone.

Once those dependencies and the model are ready, in your Debian desktop account:

```sh
mkdir -p "$HOME/Documents/GitHub"
cd "$HOME/Documents/GitHub"
git clone https://github.com/skyv04/qwen-chat.git
cd qwen-chat
env -u LD_PRELOAD /usr/bin/python3 install.py --desktop
~/.local/bin/qwen-chat
```

This installs per-user launchers, the icon and an application-menu entry.
`--desktop` also adds a desktop icon. It does not change settings or chats,
start a model, or install packages. Keep the checkout: the app loads from it.
For updates, close the app, pull the checkout and rerun the installer.

## Launch

- Desktop icon or the **Qwen Chat** menu entry
- `qwen-chat` in a terminal
- `qwen-chat --ask "question"` for a one-off answer in the terminal (risky commands and
  big installs ask `[y/N]` first; `--yes` allows them without asking)

When the app opens, it starts the model server (`~/.local/bin/qwen-server`) if one isn't
already running. When you close the app, it stops the server again, but only if the app
started it.

## Features

- Answers stream in as they're written, with Markdown: code blocks with syntax
  highlighting and copy buttons, tables, lists, quotes and clickable links.
- The message box has one compact toolbar: **Attach**, **Skills**, **Tools** and
  **Send**, with the context-token counter always visible beside Send. **Tools**
  contains Web, Files, Run and Think switches, with explanations. Its button shows
  how many tools are on.
- **Web**: searches DuckDuckGo, reads pages and downloads files. Each page comes with its
  list of links, so answers can link to the exact article.
- **Files**: list, read, write, edit, move and delete files, plus create folders, all inside
  the workspace folder only. **edit** changes one exact passage of a file, so fixing a
  value doesn't mean rewriting the whole file.
- **Run** (needs Files on): Qwen can run shell commands in the workspace, for example to
  convert images, merge PDFs, run OCR or a Python script, and install the programs a task
  needs: Debian packages with apt, or Python packages with pip. Routine commands and small
  installs run right away. Risky commands and big installs (see *Safety model*) show a card
  with the reason, the exact command and **Run**/**Install** or **Don't allow**.
- PDF worksheets and quizzes have an on-demand **worksheet-pdf** design skill and
  reusable renderer: related color emoji, themed margins, consistent headings,
  portrait pages, numbered paragraphs and ruled answer areas, instead of a new
  hand-written PDF script for every request. A new worksheet and its parent answer key
  are checked and created together in one step.
- Answer keys have their own **answer-key** skill and source-checked renderer:
  exact question/option identities, complete answer coverage, quoted evidence and
  explicit flags for defective questions, not a student worksheet with empty lines.
- **Skills**: suggest reusable guidance with `/skill-name`, a searchable picker or
  **Ctrl+K**. Create skills or package a successful workflow for use in every chat.
  Suggestions never limit Qwen to just those skills or disable its other tools.
- Steps show what they're for, with the command underneath. Click a step to see its
  output; files it made get **Open** buttons. When an answer is done, three or more steps
  fold into one line such as "4 steps · Wrote a file, ran a command…". Files Qwen made or
  changed are listed under the answer as chips that open them.
- A replaced or deleted file goes to the Trash first. Its step shows **Restore**, which
  puts the old version back.
- You can type while Qwen works: **Enter** or the send button queues the message
  ("Next: …") and sends it when the answer is done. The Stop button stays available
  beside it. **Edit** takes the queued message and attachments back into the box;
  **×** drops it.
- Long files are written in parts: one reply holds at most about 4,000 tokens, so Qwen is
  told to keep each tool call under about 8,000 characters and append the rest. If a call
  is cut off anyway, nothing is written. Qwen is told why and tries again in parts, and
  the broken call is never saved, so the chat keeps working. A `.json` file that doesn't
  parse yet comes back with the line and column where it breaks.
- **Think**: optional step-by-step reasoning, shown in a collapsible "Thoughts" section.
- Attach files with the paperclip button or drag and drop. They're copied into
  `workspace/attachments`.
- Chats are saved, with a sidebar grouped by date (Today, Yesterday, …) to switch, delete
  and undo a delete. The pencil beside a chat renames it; for the current chat you can
  also use **F2** or menu → **Rename Chat...**. Renaming keeps its messages, settings
  and position in history, including when you reopen the app.
- Status line: reading progress, then the live GPU busy percentage and tokens per second.
  Each answer ends with how long it took.
- Stop at any time; the partial answer is kept.
- Light, dark and system color schemes.

## Skills in every chat

Click **Skills** under the message box, press **Ctrl+K**, or type `/` to browse.
Search names or descriptions, then select a skill. While completing a slash mention,
**Up/Down** chooses a result and **Tab/Enter** inserts it without sending the message;
**Escape** closes the picker without stopping an answer already in progress.

For example:

```text
/worksheet-pdf Make a new space-themed reading quiz for fourth grade.
```

These are suggestions, not exclusive modes. Qwen is instructed to discover other
relevant skills, combine them or skip an unsuitable suggestion. The mention stays in
the message, so queued follow-ups and reopened conversations keep it.

The picker offers **Create a skill...** and **Save this workflow as a skill**.
Both draft an editable `/skill-creator` request; they keep anything already typed and
do not send it automatically. You can also ask directly:

```text
/skill-creator Package the approach we just used as a reusable skill.
```

Qwen drafts a standard [Agent Skills](https://agentskills.io/specification) folder in
the workspace: `SKILL.md` with YAML `name` and `description`, plus optional `scripts/`,
`references/` and `assets/`. The name matches the folder and uses 1-64 lowercase
letters, digits and single hyphens. Keep instructions concise: `SKILL.md` is limited
to 16,000 UTF-8 bytes; the whole package to 64 files and 10 MiB. Hidden files, caches
and symlinks are rejected.

`save_skill` shows the instructions and packaged file list for approval, even when
routine commands run automatically. After approval, the package is saved to
`~/.local/share/qwen-chat-harness/skills/<name>/`, apart from the chats. It
immediately appears in **every new and existing chat**, including other open windows;
reopening the picker refreshes it.
There is no per-chat copy of the library and no restart is needed to discover a new
skill. Updating a saved skill requires the same approval and keeps the previous
version in the Trash. Built-ins are read-only; customize one under a new name.
The picker has an **Open shared skill library** folder button.

`list_skills` and `load_skill` work even with Files and Run off. Full instructions
load only when needed. Supporting files are copied into
`workspace/.qwen-skills/<version>/<name>/` when Files is enabled; edited copies are
preserved. Loading a skill never executes a script. Saving requires Files, executing
helpers requires Run, and normal command/install approvals still apply. Skill text
and metadata such as `allowed-tools` do not grant permissions. In the terminal,
`--yes` is explicit approval for saving skills as well as commands and installs.

## Designed worksheet PDFs

With **Files** and **Run** enabled, ask for a worksheet/quiz PDF or ask Qwen to improve
an existing one's layout. Qwen is instructed to load `worksheet-pdf`, choose a visual
theme for the topic, write a small JSON content/theme specification, and call
`render_worksheet`. New quizzes get fresh content; an appearance-only restyle preserves
the existing wording. The skill is loaded only when needed, not added to every
conversation's context. Its format and examples live in
[`qwen_chat/data/skills/worksheet-pdf/SKILL.md`](qwen_chat/data/skills/worksheet-pdf/SKILL.md).

The renderer uses existing Chromium and Poppler (`pdfinfo`, `pdftotext`) with local
Liberation Sans or DejaVu fonts and Noto Color Emoji (including Android's COLRv1 font).
It needs no ReportLab/Pillow packages. HTML is generated from escaped plain text,
with no network assets, and printed using a temporary, separate headless browser
profile. In PRoot it uses the installed bridge's PCI compatibility shim; it does not
open, control or reuse the desktop browser.

Pages are portrait US Letter with readable body type, repeated theme artwork and
page numbers. The passage starts on the first page, not after a wasted cover.
Each question stays with its requested answer space. Before saving, the renderer
checks the page budget, text bounds, actual color-emoji pixels, answer-line placement,
and that every supplied text block survives PDF export. These are automated checks,
not a visual review or a check of educational correctness.

Icons may be any single emoji, including joined ones such as 👨‍🚀, skin tones, keycaps
and flags. When the font can't draw a joined emoji as one picture, the renderer swaps
in a related single emoji and reports it, instead of failing the whole worksheet.
Errors name the exact field to fix, such as `theme.icons[1]` or `Question 3 icon`,
and an unknown field error lists the allowed ones.

Common near-misses don't cost a rewrite and another render. `passage` or `story` is
read as `reading`; `question` or `text` as `prompt`; `choices` as `options`;
`correct_answer` as `answer`. Fields such as `type` or `points` are ignored. Typed
`A.` option letters and `1.` question numbers are removed, since the PDF adds its own.
A theme given as plain text becomes the theme's name, top-level `icons` move into the
theme, and extra fields that are empty or only about looks (such as `margin_art` or
`font_size`) are dropped; extra fields that might hold content are still reported. An
`answer_text` beside a letter `answer` is read as the option text, so the two are
checked against each other.
An unavailable motif becomes `dots`, a too-pale accent is darkened, and line breaks
typed inside JSON text are accepted. The result lists these under "Handled
automatically"; the JSON file itself is not changed. A real JSON mistake is reported
with its line, column and text, so one `edit_file` call can fix it.

A theme can also set `background` (`plain`, or `rainbow` for a pastel rainbow fading
behind the text) and a `mascot` emoji shown first in the header. To change only the
look of an existing worksheet ("same quiz, but with a mermaid and a rainbow"), Qwen
passes the existing JSON and a `theme` with just the changes to `render_worksheet`.
Nothing is retyped, so the passage, questions and answers stay exactly the same. A new
mascot replaces the old animal and character icons. The restyled JSON is saved next to
the new PDF, and the report's `Look:` line shows the theme that was used. A mascot can
be an emoji or a name such as `mermaid`. Rainbow or mascot fields written in other ways,
such as `rainbow_fading: true`, are read too. If a failing draft retypes a worksheet
that already has a PDF beside it, the error points Qwen back to that worksheet and the
`theme` argument.

By default, the document uses as many pages as it needs, up to ten; there is no
arbitrary three-page cap. Set `max_pages` only for a limit the user requested.
If that limit is too small, Qwen Chat shows **Use N Pages** with the required count.
Approval finishes the same export without rewriting the passage, removing questions,
reducing answer lines or changing the source JSON. Refusing leaves any existing PDF
untouched and blocks further tool calls in that reply. Documents beyond the ten-page
limit pause for a decision on how to split them, rather than retrying or deleting content.
An older draft that failed its page limit can be retried with the same JSON.

When a request specifies question counts, Qwen is instructed to pass
`expected_questions` and/or `expected_multiple_choice` to `render_worksheet`.
A mismatch is rejected before rendering: three multiple-choice questions means
three separate questions with options. The report lists both the total and the
multiple-choice count; the optional bonus challenge is separate.

Input and output paths stay in the workspace. Rendering follows the command-approval
preference and Stop button; overwriting follows the normal file confirmation and
Trash/Restore rules. A failed or cancelled render leaves an existing PDF untouched.
Successful PDFs get an **Open** chip like other generated files.

### Homework with a parent answer key

For a new worksheet plus a key, Qwen writes each answer into the same JSON: `choice`
for multiple choice, `answer` for written questions, and optional `explanation` and
exact `evidence` quotes. One `render_worksheet` call with `answer_key_path` then:

- checks every answer first: each question answered, real option letters, quotes that
  really occur in the reading, and simple sums such as `3 + 2 = __` recomputed;
- recomputes worked steps written in an explanation, such as `$10 - $6 - $4 = $0`, and
  for "how many" or "how much" questions requires them to reach the chosen answer, so
  a letter that points at the wrong option is caught;
- lists every problem in one message, so one edit fixes them all, and renders nothing
  until they are fixed;
- creates the student PDF with no answers and a matching-theme key, after one approval,
  and replaces neither file unless both pass their checks.

On a sheet with no reading, such as a math page, there is nothing to quote: a reason
written as `evidence` is shown in the key as the explanation ("Why: …") and listed in
the audit. With a reading, evidence must quote it exactly.

`max_pages` and `answer_key_max_pages` keep the two page budgets separate.

### Answer keys for an existing quiz

Ask normally, or suggest `/answer-key Make a PDF answer key for practice/quiz.pdf`.
Qwen is instructed to reread the actual quiz using `read_quiz`, even in an old chat.
The reader uses worksheet JSON, checks a matching JSON companion against the PDF,
or extracts sequential numbered questions and lettered options from a text PDF.
Scans and unclear numbering need OCR or a verified structured source; failures are
reported rather than filled with invented questions.

`render_answer_key` binds the key to the source hash, requires every question
exactly once, copies prompts and selected option text from the quiz, and checks
that evidence quotations occur in the source. Missing answers, fabricated quotes,
invalid option letters and stale sources are rejected. Questions with no defensible
choice can be flagged rather than assigned a forced answer. These are grounding and
coverage checks, **not proof of semantic correctness**; review the explanations,
especially if the quiz itself contains contradictions.

Keys reuse the themed PDF engine and the same page-limit, cancellation, overwrite
and Trash safeguards. Simple sums in the quiz are recomputed for these keys too.
See the [answer-key format](qwen_chat/data/skills/answer-key/SKILL.md).
`open_file` checks that a file exists (and a PDF is readable), then reports whether
the desktop accepted the launch request, not whether a window was visibly displayed.
Simple legacy `xdg-open file &` calls go through this checked opener too.

An unresolved PDF render/open failure cannot end with an unqualified model-generated
success message. On reaching the 20-tool-round limit, the app pauses explicitly and
keeps completed steps; reply **continue** to resume. It does not ask a tool-less model
to print pretend commands. A second identical tool error tells Qwen its last change
didn't fix it; a third stops the reply with the error, instead of looping. Rewriting
a file with identical content is reported as "Unchanged".

When a chat outgrows the context window, older tool outputs and long file contents
are replaced with bracketed notes that end in "to save context space". Writes and
edits refuse text containing such a note, so a copied note can never replace real
content in a file. History is trimmed with room to spare and then only appended to,
so the server keeps its cached prompt instead of re-reading it every round. A file
read again after any change is read afresh, never from an earlier result.

### Follow-ups and separate chats

A short follow-up such as "try harder", "fifth grade, not first" or "make it better"
continues the work in progress instead of starting over. When space runs out, older
turns are first shortened to the request and its final answer. Only turns before the
last three are dropped, so the latest requests stay in view. Each new message also
carries a short note that the app writes once and saves. The note lists the last three
requests, how each ended (answered, incomplete with its error, stopped), and the files
this chat made, changed or read. Qwen is told the note isn't the user's words, and file
tools refuse to copy it into a file.

Each chat works with its own files. Files made in other chats are left out of
`list_files`, which says how many were hidden and from which chat. File tools refuse
to read, change, move or delete them, and won't move or delete a folder that holds
them. A file becomes available again once the user names it, its folder path or the
other chat's title. Generic words such as "homework" or "first grade" don't count.
Files no chat made, such as your own, stay visible.

Relaunch Qwen Chat after updating the application to load new tools and instructions.
Existing conversations can use them; a new chat is not required.

## Safety model

- Ordinary file-tool paths are resolved and must stay inside the workspace. Paths that
  escape with `..`, absolute paths or symlinks are refused. Skill tools additionally
  manage only their fixed shared library; saving there needs separate approval.
- Overwriting or deleting a file you had (not one Qwen wrote in this session) first moves
  the old file to the desktop Trash (`~/.local/share/Trash`), so **Restore** or the file
  manager can bring it back. To be asked first as well, turn on menu → *Ask Before
  Replacing or Deleting Files*.
- Routine commands run without asking. A command asks first, with the reason on the card,
  when it:
  - uses files outside the workspace (your home folder, `~`, `/sdcard`, `/storage`,
    `/data`, `/mnt`, `/media`, `/root`, or private files in `/etc`), apart from scratch
    places such as `/tmp` and `/dev/null`;
  - writes outside the workspace, deletes everything in it, or pipes a download into a
    shell (`curl … | sh`);
  - uses `sudo`/`su`, stops programs or changes the system (`kill`, `systemctl`, `mount`,
    …), uses your accounts or other computers (`ssh`, `gh`, `git push`, …), changes git
    settings, or installs software directly (`apt`, `pip`, `npm -g`) instead of through
    the install tool.
- Installs run without asking unless they're big:
  - apt: a download of 100 MB or more, 300 MB or more on disk, or a plan that would remove
    packages;
  - pip: a package on the known-big list (torch, tensorflow, opencv, …), a download of
    50 MB or more, or one with no ready-made build for this phone, so pip would compile it.
  The card shows the size (apt also shows the download and disk size).
- To approve every command and install yourself, turn on menu → *Ask Before Every
  Command*. Cards for routine commands then also offer **Allow All in This Chat**, which
  lets that chat run routine commands and small installs without asking again. Risky
  commands and big installs still ask.
- There is no sandbox beyond that: a command runs in the workspace with your account's
  access. Expand a step to see the exact command that ran. Commands have no keyboard input
  and are stopped after 5 minutes (Qwen can ask for up to 30), or when you click Stop.
- If you click **Don't allow**, Qwen isn't allowed to call any more tools in that reply.
  It has to answer in words, so it can't try a workaround behind your back.
- A command that starts a program that isn't installed is refused before you're asked,
  and Qwen is told which Debian package provides it.
- apt installs run as PRoot's fake root (`proot -0`), or `sudo -n` outside Termux. pip
  installs go into Qwen's own environment, `~/.local/share/qwen-chat-harness/venv`
  (it can still use the system's Python packages), so the system Python isn't changed.
- The web tools only fetch `http`/`https` URLs. Local and private addresses are refused,
  including through redirects.
- The model server listens on `127.0.0.1` only. It has no API key and allows any web
  origin, so a web page open in a browser on this device could talk to it while it runs.

## Settings

`~/.config/qwen-chat/settings.json` (created on first run):

| Key | Default | Meaning |
|---|---|---|
| `workspace` | `~/QwenWorkspace` | The only folder Qwen can read and write |
| `server_url` | `http://127.0.0.1:8080` | llama-server address |
| `server_command` | `["~/.local/bin/qwen-server"]` | How to start the server |
| `context_size` | `16384` | Context window for a server the app starts |
| `commands` | `true` | Let Qwen run commands and install programs (**Tools → Run**) |
| `ask_commands` | `false` | Ask before every command and install, not only risky commands and big installs (menu → *Ask Before Every Command*) |
| `ask_file_changes` | `false` | Also ask before overwriting or deleting a file, on top of the Trash backup (menu → *Ask Before Replacing or Deleting Files*) |
| `stop_server_on_exit` | `true` | Stop the server on quit, if the app started it (menu → *Unload Model When Closing*) |

Chats are stored in `~/.local/share/qwen-chat/chats`. The server log is
`~/.cache/qwen-chat/server.log`.

Everything Qwen needs to work is kept in a separate folder,
`~/.local/share/qwen-chat-harness`: saved skills (`skills/`), Qwen's Python
environment (`venv/`) and the GPU helper (`turnip-env.sh`). Deleting chats, even the
whole `qwen-chat` folder, doesn't touch it. Skills that an older version saved beside
the chats move there on the next start. The older `~/.local/share/qwen-chat/qwen-chat/venv`
is no longer used and can be deleted; Qwen reinstalls packages when it needs them.

## GPU notes

- All 33 layers run on the GPU (Vulkan0). The GPU sits at about 99% while answering.
- The server runs with `-ub 128`. llama.cpp's default micro-batch of 512 crashes the
  Adreno driver (`ErrorDeviceLost`) on prompts longer than about 512 tokens. 128 is just
  as fast and stable.
- Speeds: reading runs at about 30 tokens/s and writing at 5–10 tokens/s. Writing slows
  down when the phone gets hot and lowers the GPU clock.
- The first message after the server starts is slower because the instructions and
  tool descriptions have to be read once. Later messages reuse the cached prefix.
  A design skill adds its instructions only when that skill is loaded.

## Development

```sh
python3 -m unittest -q tests.test_store tests.test_skills tests.test_agent tests.test_tools tests.test_markdown tests.test_worksheet_pdf
```

The worksheet tests include a local headless render when Chromium, Poppler and the
fonts are available; they do not download or install anything.

Skill metadata uses PyYAML (`requirements.txt`, or Debian's `python3-yaml`).
The GUI needs Debian's `/usr/bin/python3.11` (python3-gi, GTK 4.8, libadwaita 1.2,
GtkSourceView 5). The launcher in `~/.local/bin/qwen-chat` sets this up.

Native picker, keyboard, approval and chat-renaming tests use Xvfb, xdotool and ImageMagick, with isolated
settings, chats, workspace and skills. They never start a model server. Run them
separately, not on the user's desktop:

```sh
env -u LD_PRELOAD GSK_RENDERER=cairo QWEN_TEST_GUI=1 \
  dbus-run-session -- xvfb-run -a /usr/bin/python3.11 -m unittest -q tests.test_window_ui
```

The per-user installer and model launchers can be checked without loading a model:

```sh
env -u LD_PRELOAD /usr/bin/python3 -m unittest -q tests.test_install
```

## License

The app and its bundled skills use the [MIT license](LICENSE), matching the
license shown in About. llama.cpp, Mesa, Chromium and the downloaded model
retain their own licenses.
