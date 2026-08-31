# Link Studio

Link Studio is an unofficial, production-ready Linux controller for Insta360 Link webcams. It is
hardware-tested with the **Insta360 Link 2** (`2e1a:4c04`) on Omarchy and combines direct,
cooperative V4L2/UVC control with a native GTK4/libadwaita interface. Video, audio, AI inference,
remote control, and configuration remain local unless you explicitly open an Insta360 download
link.

Version 1.0 completes the safe Linux application scope. It implements every official-client area
that can be reproduced through validated Link 2 controls or a local Linux equivalent. The few
vendor-only operations that cannot safely be cloned are identified in the
[parity contract](docs/PARITY.md); they are external protocol or service boundaries, not unfinished
buttons hidden in the application.

Project documentation lives at <https://jweaver60.github.io/link-studio/>. The canonical source
repository is `jweaver60/link-studio` and is private during the pre-AUR review period.

## Features

- Live 720p, 1080p, 1440p, and 4K preview at every frame rate currently advertised by the camera
- PNG screenshots and H.264/AAC MP4 recording, with selectable destinations and microphone
- Pan, tilt, center, 1–4× zoom, autofocus/manual focus, mirror, privacy, and output rotation
- Hardware AI Tracking, Whiteboard, Overhead, and DeskView modes
- Hardware framing, tracking speed, gesture zoom, HDR, exposure, white balance, anti-flicker, and
  complete color controls
- Local single-person and group tracking with a tracking boundary and up to six pause areas
- Local background blur, bokeh, background replacement, green screen, beautify, makeup, and Smart
  Whiteboard, powered by bundled MediaPipe models
- Link 2 Voice Focus, Voice Suppression, and Music Balance microphone modes; PipeWire mute and volume
- Processed virtual-camera output through an optional `v4l2loopback` device
- Ten full scene presets with rename/update/delete/default-on-startup lifecycle
- Twenty independent color templates
- Token-authenticated LAN phone remote with QR pairing and the active Omarchy palette
- Compact toolbar and 14 compositor-managed global actions through the XDG GlobalShortcuts portal
- Teleprompter scripts with import/editing, speed, size, colors, opacity, guide, loop, and countdown
- Local voice-note sessions with pause, timestamped notes and screenshot markers, optional offline
  Whisper transcription, and Markdown summaries
- Firmware/build reporting, JSON diagnostics, rotating logs, and privacy-conscious support bundles
- Automatic, live Omarchy palette and light/dark updates

## Run from the repository

On Omarchy/Arch, install the native stack if it is not already present: `gtk4`, `libadwaita`,
`python-gobject`, `gstreamer`, `gst-plugins-base`, `gst-plugins-good`, `gst-plugins-bad`, `gst-libav`,
and `v4l-utils`. The launcher creates a local environment anchored to the system Python so
Omarchy's GTK bindings and Link Studio's Python dependencies are both available.

```bash
./scripts/run-dev
```

Useful diagnostics:

```bash
./scripts/run-dev --diagnose
./scripts/run-dev --version
./scripts/run-dev --device /dev/video2
./scripts/run-dev --no-preview
```

`--diagnose` is read-only and prints detected cameras, camera state, firmware information, and the
active Omarchy palette as JSON.

## Install for the current user

```bash
./scripts/install-user
```

This installs an isolated environment below `${XDG_DATA_HOME:-~/.local/share}/link-studio`, creates
`link-studio` in `${XDG_BIN_HOME:-~/.local/bin}`, and installs desktop/AppStream entries. It neither
requires root nor edits Omarchy configuration.

The stable `link-studio` AUR recipe is staged under `packaging/aur`. It will be submitted when the
upstream release archive is publicly downloadable; publishing a package whose source requires
GitHub credentials would produce a broken AUR installation.

Optional local integrations:

```bash
link-studio-setup-virtual-camera
link-studio-setup-local-ai
```

The virtual-camera helper uses `omarchy pkg add` for the official Arch packages and loads a
temporary `/dev/video20` device. The AI helper installs `whisper-cpp` and verifies the downloaded
multilingual base model against a pinned SHA-256 checksum.

## Omarchy integration

Link Studio watches the active Omarchy color palette:

```text
~/.local/state/omarchy/current/theme/colors.toml
```

After `omarchy theme set …`, libadwaita light/dark mode, accent, selection, surfaces, text, popovers,
and semantic colors update immediately. The phone remote receives the same palette. No theme hook
or user configuration edit is required; other Linux desktops use the normal libadwaita theme.

Global actions use the desktop portal rather than editing Hyprland bindings. Enable them under
Device → Application. Desktops with a portal configuration dialog can assign keys there. On
Hyprland, list the registered action names with `hyprctl globalshortcuts`, then use its `global`
dispatcher in `~/.config/hypr/hyprland.lua`, for example:

```lua
hl.bind("SUPER + SHIFT + R", hl.dsp.global("io.github.linkstudio.LinkStudio:record"))
hl.bind("SUPER + SHIFT + S", hl.dsp.global("io.github.linkstudio.LinkStudio:screenshot"))
```

## Safety model

- The backend never detaches `uvcvideo`; this avoids a known Link 2 failure mode where detach/rebind
  cycles can leave USB present without a working video node.
- Hardware mode writes are serialized, paced, and read back where the protocol supports it.
- Unknown firmware-update, factory-reset, and compatibility-mode writes are never guessed. The
  Device page reports firmware and links to Insta360's recovery-safe U-Disk flow.
- Privacy mode is the camera's gimbal-down mode, not a physical shutter.
- Phone control is LAN-only, uses an unguessable per-session token, and is off by default.
- MediaPipe effects and Whisper transcription run locally.

## Architecture

```text
GTK4 / libadwaita application
 ├─ V4L2 standard controls ─────────── gimbal, zoom, image, focus
 ├─ validated UVC extension controls ─ AI modes, HDR, exposure, privacy, audio
 ├─ GStreamer pipelines ───────────── preview, screenshots, MP4, virtual camera
 ├─ MediaPipe + OpenCV ────────────── effects, face tracking, Smart Whiteboard
 ├─ local tools ───────────────────── phone remote, teleprompter, meetings
 ├─ XDG desktop portals ───────────── compositor-managed global actions
 ├─ XDG JSON stores ───────────────── scenes, scripts, output locations, settings
 └─ Omarchy theme bridge ──────────── live colors.toml monitoring
```

## Development and verification

```bash
.venv/bin/python -m unittest discover -v
uvx ruff check src tests scripts
.venv/bin/python -m compileall -q src tests
./scripts/run-dev --diagnose
```

Link Studio is MIT-licensed. Third-party model and research attribution is in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Insta360 and Insta360 Link are trademarks of their
respective owner; this project is not affiliated with or endorsed by Insta360.
