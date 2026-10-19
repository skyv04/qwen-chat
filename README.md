# Qwen Chat

A native GTK 4 / libadwaita chat app for a local Qwen model that runs on the phone's
Adreno GPU through llama.cpp (Vulkan). Built for Debian in PRoot on Termux with XFCE.

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
- **Web**: searches DuckDuckGo, reads pages and downloads files. Each page comes with its
  list of links, so answers can link to the exact article.
- **Files**: list, read, write, move and delete files, plus create folders, all inside the
  workspace folder only.
- **Run** (needs Files on): Qwen can run shell commands in the workspace, for example to
  convert images, merge PDFs, run OCR or a Python script, and install the programs a task
  needs: Debian packages with apt, or Python packages with pip. Routine commands and small
  installs run right away. Risky commands and big installs (see *Safety model*) show a card
  with the reason, the exact command and **Run**/**Install** or **Don't allow**.
- Steps show what they're for, with the command underneath. Click a step to see its
  output; files it made get **Open** buttons. When an answer is done, three or more steps
  fold into one line such as "4 steps · Wrote a file, ran a command…". Files Qwen made or
  changed are listed under the answer as chips that open them.
- A replaced or deleted file goes to the Trash first. Its step shows **Restore**, which
  puts the old version back.
- You can type while Qwen works: **Enter** queues the message ("Next: …") and sends it when
  the answer is done. **Edit** takes it back into the box; **×** drops it.
- Long files are written in parts: one reply holds at most about 4,000 tokens, so Qwen is
  told to keep each tool call under about 8,000 characters and append the rest. If a call
  is cut off anyway, nothing is written. Qwen is told why and tries again in parts, and
  the broken call is never saved, so the chat keeps working.
- **Think**: optional step-by-step reasoning, shown in a collapsible "Thoughts" section.
- Attach files with the paperclip button or drag and drop. They're copied into
  `workspace/attachments`.
- Chats are saved, with a sidebar grouped by date (Today, Yesterday, …) to switch, delete
  and undo a delete.
- Status line: reading progress, then the live GPU busy percentage, tokens per second,
  and how much of the context window is used. Each answer ends with how long it took.
- Stop at any time; the partial answer is kept.
- Light, dark and system color schemes.

## Safety model

- Every file path is resolved and must stay inside the workspace. Paths that escape with
  `..`, absolute paths or symlinks are refused.
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
  installs go into Qwen's own environment, `~/.local/share/qwen-chat/qwen-chat/venv`
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
| `commands` | `true` | Let Qwen run commands and install programs (the **Run** button) |
| `ask_commands` | `false` | Ask before every command and install, not only risky commands and big installs (menu → *Ask Before Every Command*) |
| `ask_file_changes` | `false` | Also ask before overwriting or deleting a file, on top of the Trash backup (menu → *Ask Before Replacing or Deleting Files*) |
| `stop_server_on_exit` | `true` | Stop the server on quit, if the app started it (menu → *Unload Model When Closing*) |

Chats are stored in `~/.local/share/qwen-chat/chats`. The server log is
`~/.cache/qwen-chat/server.log`.

## GPU notes

- All 33 layers run on the GPU (Vulkan0). The GPU sits at about 99% while answering.
- The server runs with `-ub 128`. llama.cpp's default micro-batch of 512 crashes the
  Adreno driver (`ErrorDeviceLost`) on prompts longer than about 512 tokens. 128 is just
  as fast and stable.
- Speeds: reading runs at about 30 tokens/s and writing at 5–10 tokens/s. Writing slows
  down when the phone gets hot and lowers the GPU clock.
- The first message after the server starts takes about 70 s, because the instructions
  and tool descriptions (about 2,100 tokens) are read once. Later messages reuse them
  from the cache.

## Development

```sh
python3 -m unittest -q tests.test_tools tests.test_markdown
```

The GUI needs Debian's `/usr/bin/python3.11` (python3-gi, GTK 4.8, libadwaita 1.2,
GtkSourceView 5). The launcher in `~/.local/bin/qwen-chat` sets this up.
