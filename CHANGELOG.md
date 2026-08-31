# Changelog

All notable changes to Link Studio are recorded here.

## Unreleased

- Kept live palette synchronization while removing theme names from the application UI and shared
  theme data.
- Fixed teleprompter restart and per-window styling, preset control resynchronization, anti-flicker
  Auto, the full tracking-speed range, and frame-accurate tracking-area geometry.
- Made local data explicitly UTF-8, contained malformed firmware responses, serialized camera
  shutdown with worker completion, and removed unrelated development-runtime diagnostics.
- Rotated phone-remote credentials on every start, hardened token and GStreamer input handling,
  and changed recordings to real-time timestamps so dropped preview frames cannot cause A/V drift.
- Reduced frame copies and idle polling, made effect caches concurrency- and file-change-aware,
  corrected Smart Whiteboard area detection, declared OpenCV directly, strengthened Ruff, and
  expanded regression coverage.
- Added AppStream screenshot metadata and the desktop-entry specification version.
- Made camera teardown immediately responsive while preserving descriptor safety, restored every
  preset-only effect/region control, retained live filters across pipeline rebuilds, retired stale
  frames after stream failures, and honored relocated XDG Documents directories.
- Finalized recordings and stopped virtual-camera output on every preview failure, fully reset UI
  state after rejected stream formats, and made shutdown cancellation authoritative across firmware
  readers and queued main-thread completions.

## 1.0.0 — 2026-08-31

- Added complete Link 2 hardware controls, validated AI modes, image controls, and microphone DSP
  modes.
- Added local MediaPipe/OpenCV effects, group tracking, tracking and pause areas, and Smart
  Whiteboard.
- Added H.264/AAC capture, custom storage, processed virtual camera output, scenes, color templates,
  phone remote, compact mode, XDG global actions, teleprompter, and local AI voice recording.
- Added automatic live Omarchy theming.
- Added the system-Python-aware user installer, diagnostics, support bundles, tests, distribution
  archives, Arch/AUR packaging, CI/release automation, and the GitHub Pages documentation site.
