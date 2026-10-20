# Qwen on Debian in Termux

This is the setup used for Qwen Chat on an Adreno 840 phone: Debian 12
ARM64 in PRoot, XFCE displayed by Termux:X11, Mesa Turnip with KGSL support,
and Qwen3.5-9B Q4_K_M running through llama.cpp's Vulkan backend.

It is not an Android APK, a virtual machine, a remote desktop, or an NPU app.
The model and the GTK application run as Linux processes on the phone.
Other chipsets, drivers and smaller-memory phones have not been verified.

## 1. Start with a working GPU desktop

Use the [phone hardware guide](https://skyel.net/blog/unlocking-phone-hardware-termux)
and the [SELinux Hardware Bridge source](https://github.com/skyv04/selinux-hardware-bridge)
for the Debian desktop, Mesa build and native hardware integration. Install
Termux and its companion Termux:X11 Android app from their official projects;
the X11 app and the Termux-side X11 package are both needed.

PRoot gives you a Debian filesystem but keeps Android's kernel and security
restrictions. Its root account is not Android root. The bridge app provides
camera, microphone and codec access; **Qwen inference does not go through that
app**. Turnip talks directly to the Adreno device through `/dev/kgsl-3d0`.

The tested Mesa prefix is `/opt/mesa-adreno`, with its `activate.sh` environment
script. In a Debian terminal:

```sh
. /opt/mesa-adreno/activate.sh
env -u LD_PRELOAD vulkaninfo --summary
```

Look for `Adreno (TM) 840` (or your supported Adreno), not `llvmpipe`.
Do not continue with a software Vulkan device and expect the same results.
Keep the previous guide's XFCE compositor precautions; this app does not
change the display server or the desktop's graphics settings.

## 2. Install the Debian dependencies

Run these inside **Debian**, in its root shell (or use `sudo` on a normal
Debian installation):

```sh
env -u LD_PRELOAD apt-get update
env -u LD_PRELOAD apt-get install -y \
  git curl ca-certificates build-essential cmake ninja-build glslc \
  libvulkan-dev vulkan-tools spirv-headers libcurl4-openssl-dev \
  python3 python3-gi python3-yaml python3-venv \
  gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 \
  adwaita-icon-theme xdg-utils dbus-x11 \
  chromium poppler-utils fonts-liberation fonts-dejavu-core fonts-noto-color-emoji
```

Chromium and Poppler are for PDF rendering, not model inference. A PDF viewer
such as Okular is optional; install it with `apt-get install okular`. For
scanned text, `tesseract-ocr` is useful. This app does not load Qwen's vision
projector: image-to-text work uses tools such as OCR, not native model vision.

On the tested PRoot desktop, the account is not fake root. The equivalent
package command there is
`env -u LD_PRELOAD -u LD_LIBRARY_PATH proot -0 /usr/bin/apt-get ...`.
Do not carry Debian's loader variables into Termux's Android binaries.

## 3. Build the known-working llama.cpp revision

These commands use new checkout directories. If you already have them, keep
your work and adjust the paths rather than overwriting an existing checkout.
The Vulkan headers are newer than Debian 12's packaged headers.

```sh
mkdir -p "$HOME/Documents/GitHub"
cd "$HOME/Documents/GitHub"
git clone --depth 1 --branch v1.4.354 \
  https://github.com/KhronosGroup/Vulkan-Headers.git
git clone --filter=blob:none --no-checkout \
  https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
git fetch --depth 1 origin 547208dca250ceb3e4c8615598d043f2b602a527
git checkout --detach 547208dca250ceb3e4c8615598d043f2b602a527

env -u LD_PRELOAD /usr/bin/cmake -S . -B build-vulkan -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_VULKAN=ON -DGGML_NATIVE=ON \
  -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
  -DVulkan_INCLUDE_DIR="$HOME/Documents/GitHub/Vulkan-Headers/include"
env -u LD_PRELOAD /usr/bin/cmake --build build-vulkan \
  --target llama-cli llama-server --parallel 2

env -u LD_PRELOAD ./build-vulkan/bin/llama-cli --list-devices
```

The final command must list a hardware `Vulkan0` device. Limiting compilation
to two jobs is deliberate: an earlier six-job build exhausted memory and
took down the Termux session. Close other heavy applications first.

Removing `LD_PRELOAD` avoids a child-process hang caused by inherited bridge
shims. Keep the Turnip library and driver paths from `activate.sh`.

## 4. Download and verify the model

Allow at least 12 GB of free storage for sources, build files and the model.
The model alone is **5,680,522,464 bytes**, about 5.68 GB. This example pins the
download revision and checksum rather than trusting an evolving filename:

```sh
mkdir -p "$HOME/models"
cd "$HOME/models"
curl --fail --location --continue-at - \
  --output Qwen3.5-9B-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/3885219b6810b007914f3a7950a8d1b469d598a5/Qwen3.5-9B-Q4_K_M.gguf
printf '%s  %s\n' \
  03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8 \
  Qwen3.5-9B-Q4_K_M.gguf | sha256sum -c -
```

The model is distributed by Unsloth; see its model card and license.
It is downloaded separately and is not included in this repository.

The tested phone has about 14 GiB of usable RAM. Our launcher asks for at
least 6,000 MB currently available before loading this model. That is a
practical guard, not a guarantee: weights, working buffers, Android apps and
the desktop all share the same memory. Android may kill Termux if it runs out.

## 5. Install Qwen Chat

Back in the same Debian desktop account:

```sh
cd "$HOME/Documents/GitHub"
git clone https://github.com/skyv04/qwen-chat.git
cd qwen-chat
env -u LD_PRELOAD /usr/bin/python3 install.py --desktop
~/.local/bin/qwen-chat
```

The installer adds Qwen Chat to the application menu. `--desktop` also adds
its desktop icon; if XFCE asks, choose **Allow Launching**. Leave out that
option if you do not want another desktop icon. Keep the checkout where it
is, because the launcher loads the Python code and bundled skills from it.
Installation does not start a model, change chats, or download packages.

The app reuses an existing server at `127.0.0.1:8080`, or starts one using
`~/.local/bin/qwen-server`. **Do not start a second server for a second window.**
To inspect startup yourself, close Qwen Chat first and run:

```sh
~/.local/bin/qwen-server
```

In another terminal, `curl --fail http://127.0.0.1:8080/health` should return
`{"status":"ok"}` after loading. Startup logs should identify Vulkan0 and
offload all 33 layers for this model. Ctrl+C stops a server you started
manually. Opening Qwen Chat while that server is healthy reuses it.

The shared launcher `scripts/qwen` supplies these defaults:

```text
-ngl 99 -t 6 -c 16384 -fa on -ctk q8_0 -ctv q8_0 -ub 128 --reasoning off
```

`-ub 128` matters. The default 512-token micro-batch caused
`ErrorDeviceLost` on longer prompts on this driver. The smaller batch was
stable in our runs. The 8-bit KV cache saves memory; neither setting makes
all phones equivalent. Do not treat an always-busy GPU as a performance target.

| Environment override | Default |
|---|---|
| `QWEN_LLAMA_BIN` | `~/Documents/GitHub/llama.cpp/build-vulkan/bin` |
| `QWEN_MODEL` | `~/models/Qwen3.5-9B-Q4_K_M.gguf` |
| `QWEN_GPU_ENV` | `/opt/mesa-adreno/activate.sh` |
| `QWEN_CTX` | `16384` for manual launches; the GUI uses `context_size` from settings |
| `QWEN_SKIP_MEM_CHECK` | Unset. `1` bypasses the memory check, at your own risk |

Flags passed to `qwen` or `qwen-server` come after the defaults. Keep the
server bound to `127.0.0.1`; it is not an authenticated public service.
See the README's safety model before enabling tools.

## 6. Try a worksheet

In the app, enable **Files** and **Run** under **Tools**, then send:

```text
/worksheet-pdf Make a short space-themed reading quiz for fourth grade.
Use three multiple-choice questions and three short-answer questions.
Use related color emoji and leave enough room to write. Open the PDF.
```

The skill writes JSON. The deterministic renderer turns it into a PDF using
local Chromium, fonts and Poppler. Review the facts and answers yourself.
Layout checks do not certify educational accuracy.

In PRoot, headless Chromium also needs the bridge's **PCI compatibility
shim**, even though inference does not use the bridge app. The native
hardware installation exports `BRIDGE_CLIENT`; the renderer finds
`native/libpcibridge.so` next to that client and loads only that shim.
If PDF rendering fails with `/proc/bus/pci/devices`, verify that the
bridge's native helpers were built and that the desktop was started after
`hw-enable install`. Do not reinstall the model to fix a browser problem.

## Update and troubleshoot

To update, stop an active answer, close Qwen Chat, run `git pull --ff-only`
in its checkout, then rerun `/usr/bin/python3 install.py` (add `--desktop`
if desired). Chats, settings and shared skills are outside the checkout
and are not replaced.

- **Missing GTK or YAML:** use Debian's `/usr/bin/python3`, not a custom
  Python or a venv that cannot see the distro's GI packages.
- **Only a software Vulkan device:** fix the Mesa/KGSL setup before starting
  the model. Zink is for OpenGL applications, not llama.cpp inference.
- **Low memory:** close Android applications and browser tabs. The guard
  refuses to risk the whole desktop just to start a model.
- **Slow first reply:** the server reads the system instructions and tool
  descriptions first. Later replies can reuse the cached prefix.
- **Slower after a while:** the GPU throttles when the phone gets hot.
- **PDF page limit:** approve the extra pages or decline and ask for a
  shorter worksheet. The renderer does not silently exceed a saved limit.
- **Server failure:** read `~/.cache/qwen-chat/server.log`. The app shows
  errors rather than treating an interrupted stream as a complete answer.
