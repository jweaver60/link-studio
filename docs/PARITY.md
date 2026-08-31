# Insta360 Link Controller parity contract

Baseline: the Link 2 feature set exposed by current Insta360 desktop documentation and Link
Controller 2.2.x. Hardware behavior below was verified on Link 2 firmware `v3.2.6.8_build5` on
Omarchy. “Local equivalent” means Link Studio produces the same usable output on the host and in its
recording/virtual-camera pipeline, without claiming an undocumented firmware implementation.

| Area | Official-client capability | Link Studio 1.0 | Implementation |
|---|---|---:|---|
| Camera | Discovery, connection, state | ✅ | Link-family USB discovery; Link 2 hardware-tested |
| Camera | Preview and format selection | ✅ | 720p–4K; dynamic rates advertised by V4L2 |
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
| Image | Anti-flicker | ✅ | Off, 50 Hz, and 60 Hz |
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
| App | Logs and support bundle | ✅ | Rotating logs, diagnostics, formats, runtime status; no images |
| Device | Firmware/build display | ✅ | Read directly from Link 2 |
| Device | Firmware acquisition | ✅ safe flow | Official download/U-Disk instructions open from Device page |
| Cloud | InSight-style AI recording | ✅ local | Voice notes, pause, markers, offline Whisper, local summary |
| Desktop | Automatic Omarchy theming | ✅ | Live palette/light-dark reload; shared with phone remote |

## Deliberate external boundaries

These operations are not safely reproducible Linux application code, so Link Studio exposes a safe
equivalent or leaves ownership with the vendor/compositor:

| Boundary | Resolution in Link Studio |
|---|---|
| Link 2 compatibility toggle for firmware-only 50/60 fps or 360p modes | Link Studio lists every format the active firmware advertises. On the tested camera that is 24/25/30 fps. It never sends an unidentified compatibility write. |
| Device-wide hardware vertical flip/horizon correction | The complete processed output path supplies deterministic flip and rotation. Unknown persistent firmware bits are not written. |
| Separate undocumented permission bits for tracking/whiteboard gestures | Their camera modes retain firmware gesture behavior; the known gesture-zoom bit is independently controllable. |
| In-app proprietary firmware flashing/factory reset | Current firmware is shown and the official recovery-safe U-Disk instructions are linked. No unauthenticated firmware blob is written over an inferred protocol. |
| Insta360 account, membership, or InSight cloud | Replaced with an account-free local recorder, offline transcription, and local summaries. Vendor identity/billing services cannot be cloned. |
| Forcing a teleprompter above every Wayland surface | Link Studio creates a dedicated teleprompter window. Final stacking authority belongs to Hyprland/Wayland and is intentionally not bypassed. |

This is the completed 1.0 contract: all rows in the application-controlled scope have a working
implementation, while the boundary table documents functionality owned by proprietary services,
unknown destructive protocols, camera firmware configuration, or the Wayland compositor.
