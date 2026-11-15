# Insta360 Link Controller parity contract

Baseline: the Link 2 feature set exposed by current Insta360 desktop documentation and Link
Controller 2.2.x. Hardware behavior below was verified on Link 2 firmware `v3.2.6.8_build5` on
Omarchy. “Local equivalent” means Link Studio produces the same usable output on the host and in its
recording/virtual-camera pipeline, without claiming an undocumented firmware implementation.

| Area | Official-client capability | Link Studio 1.0 | Implementation |
|---|---|---:|---|
| Camera | Discovery, connection, state | ✅ | Link-family USB discovery; Link 2 hardware-tested |
| Camera | Preview and format selection | ✅ | 720p–4K; dynamic rates advertised by V4L2 |
| Device | Portrait resolutions and high frame rates | ✅ original Link | Explicit Device-page toggle for `2e1a:4c01`; verified on `v1.4.5.8_build1`; Link 2 unverified and not enabled |
| Capture | Screenshot and MP4 recording | ✅ | PNG; H.264/AAC with selected Link microphone |
| Capture | Output-folder selection | ✅ | Independent screenshot and recording destinations |
| View | Pan, tilt, center, 1–4× zoom | ✅ | Cooperative V4L2 controls; no driver detach |
| View | Horizontal mirror | ✅ | Validated Link 2 hardware control |
| View | Vertical/portrait/180° output | ✅ local | Preview, capture, effects, and virtual-camera rotation |
| View | Privacy mode | ✅ | Validated gimbal-down privacy mode and stream stop |
| Focus | Autofocus and manual focus | ✅ | Standard UVC controls |
| Smart | AI Tracking | ✅ | Camera AI with streaming-aware settle/readback |
| Smart | Single/group tracking | ✅ | Camera single mode plus local MediaPipe single/group engines |
| Smart | Tracking and pause areas | ✅ local | One tracking boundary and up to six pause areas |
| Smart | Smart composition and speed | ✅ | Hardware head/half/whole framing and tracking speed |
| Smart | Whiteboard | ✅ | Validated hardware mode |
| Smart | Smart Whiteboard without markers | ✅ local | OpenCV detection and perspective rectification |
| Smart | Overhead and DeskView | ✅ | Validated hardware modes |
| Gestures | Gesture zoom | ✅ | Validated hardware toggle |
| Gestures | Tracking/whiteboard gestures | ✅ mode | Firmware handles them inside their AI modes |
| Image | HDR and exposure | ✅ | HDR, auto/manual exposure, curve, ISO, shutter |
| Image | White balance and color | ✅ | Auto/manual WB, brightness, contrast, saturation, hue, sharpness |
| Image | Anti-flicker | ✅ | Off, 50 Hz, 60 Hz, and Auto |
| Image | Color templates | ✅ | Twenty save/apply/delete templates |
| Effects | Basic filters | ✅ | None, monochrome, punch, soft |
| Effects | Blur, bokeh, replacement | ✅ local | Bundled person-segmentation model |
| Effects | Green screen | ✅ local | Adjustable key color and tolerance |
| Effects | Beautify and makeup | ✅ local | Face-landmark-aware OpenCV effects |
| Output | Processed virtual camera | ✅ optional | GStreamer to `v4l2loopback`; setup helper included |
| Audio | Voice Focus/Suppression/Music Balance | ✅ | Validated Link 2 hardware DSP modes |
| Audio | Source, volume, mute | ✅ | PipeWire/PulseAudio integration |
| Scenes | Save, apply, update, rename, delete | ✅ | Ten scenes with atomic XDG persistence |
| Scenes | Default scene | ✅ | Star/unstar and automatic startup apply |
| Remote | Phone controller | ✅ local | Authenticated LAN UI and QR pairing; no cloud/account |
| App | Compact controller | ✅ | Dedicated toolbar view and Ctrl+M |
| App | App keyboard shortcuts | ✅ | Preview, record, screenshot, compact, quit |
| App | Background global shortcuts | ✅ | Fourteen XDG GlobalShortcuts portal actions |
| App | Teleprompter | ✅ | Local scripts, display styling, guide, loop, countdown |
| App | Custom storage | ✅ | XDG-persisted capture destinations |
| App | Logs and support bundle | ✅ | Rotating logs, camera/formats, audio/video stack; no images |
| Device | Firmware/build display | ✅ | Read directly from Link 2 |
| Device | Firmware acquisition | ✅ safe flow | Official download/U-Disk instructions open from Device page |
| Cloud | InSight-style AI recording | ✅ local | Voice notes, pause, markers, offline Whisper, local summary |
| Desktop | Automatic Omarchy theming | ✅ | Live palette/light-dark reload; shared with phone remote |

## Original Link compatibility protocol and limits

Device → **Portrait resolutions and high frame rates** is user-operated only, for original Link
USB `2e1a:4c01`. Stop preview, recording and virtual output, and close other camera clients first.
The camera may restart. Link Studio reads the two-byte feature word from XU 9, selector 27
(`0x1b`), changes only bit 5 (`0x0020`), and verifies the entire word by readback under its camera
lock. GET_LEN must report exactly two bytes; no cached/fallback length is accepted. An already
matching state is not written. The separate portrait-lock bit 10 (`0x0400`) is left untouched.
Startup, scenes and phone remote never apply this setting.

After the initial write/readback, Link Studio closes the old camera descriptor and waits eight
seconds for the firmware's delayed restart. For up to 24 more seconds it polls for the same USB
VID/PID and nonempty physical-port `bus_info`, never choosing an arbitrary same-model camera.
The same Camera object is reopened under its lock, cached XU lengths are cleared, and the opened
descriptor's physical port is rechecked. Preview remains blocked during recovery. Format queries
have a one-second timeout so shutdown cancellation is checked between short attempts.

Success requires 720p and 1080p to advertise 60 fps when enabled, or neither to advertise 60 fps
when disabled, followed by another feature-bit readback on the reopened descriptor. The preview
uses the new video-node path, retaining a still-valid resolution/rate selection. This does not
force 60 fps or promise every mode on every firmware. Empty/stale formats, temporary disappearance,
and reopen/read errors are retried within this bound, but the setting write is **never repeated**.
If recovery expires or initial readback fails, the UI reports unknown state and keeps preview
blocked until you reconnect and reopen Link Studio rather than claiming success. No USB reset or firmware flashing
is performed. Missing physical-port identity is rejected before writing.

Provenance: offline analysis of the official
[Controller 2.0.3 (build 2) macOS package](https://file.insta360.com/static/cefa516e5f79760119332e093286255e/Insta360LinkController_2.0.3(build2).pkg)
traced `EnableVerticalScreen` to `EnableExtendFuncWork` with bit index 5, independently of forced
vertical/portrait lock (index 10). Targeted native verification on original Link firmware
`v1.4.5.8_build1` confirmed the protocol and high-rate output: 602 frames over 10.02 seconds;
a separate 180-frame/3-second framehash sample had 179 unique frames and one adjacent duplicate.
A live GUI off/on test also confirmed format rediscovery and feature readback after both firmware
restarts. No firmware was flashed. Unplug/reboot persistence, other firmware, and Link 2 remain
unverified; model gating is not a claim of testing every original Link firmware. Mocked CI covers
protocol and UI safety without opening a camera.

## Deliberate external boundaries

These operations are not safely reproducible Linux application code, so Link Studio exposes a safe
equivalent or leaves ownership with the vendor/compositor:

| Boundary | Resolution in Link Studio |
|---|---|
| Link 2 compatibility toggle for firmware-only 50/60 fps or 360p modes | Unverified on Link 2; no compatibility write is offered for that model. Link Studio lists every advertised format (24/25/30 fps on the tested Link 2). The original Link has a separately verified, model-gated toggle; see the protocol above. |
| Device-wide hardware vertical flip/horizon correction | The complete processed output path supplies deterministic flip and rotation. Unknown persistent firmware bits are not written. |
| Separate undocumented permission bits for tracking/whiteboard gestures | Their camera modes retain firmware gesture behavior; the known gesture-zoom bit is independently controllable. |
| In-app proprietary firmware flashing/factory reset | Current firmware is shown and the official recovery-safe U-Disk instructions are linked. No unauthenticated firmware blob is written over an inferred protocol. |
| Insta360 account, membership, or InSight cloud | Replaced with an account-free local recorder, offline transcription, and local summaries. Vendor identity/billing services cannot be cloned. |
| Forcing a teleprompter above every Wayland surface | Link Studio creates a dedicated teleprompter window. Final stacking authority belongs to Hyprland/Wayland and is intentionally not bypassed. |

Shutdown deliberately drains the camera worker on a non-daemon reaper before closing its file
descriptor. This keeps the window responsive and prevents descriptor reuse while an ioctl is in
flight. If a kernel driver ever leaves an ioctl permanently wedged, the window will close but the
process can remain alive until that kernel call returns; detaching the thread would make the camera
descriptor unsafe to close.

This is the completed 1.0 contract: all rows in the application-controlled scope have a working
implementation, while the boundary table documents functionality owned by proprietary services,
unknown destructive protocols, camera firmware configuration, or the Wayland compositor.
