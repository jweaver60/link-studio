from __future__ import annotations

import queue
import re
import subprocess
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp

from .effects import EffectProcessor, EffectSettings

Gst.init(None)


def _gst_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def parse_v4l2_formats(output: str) -> dict[tuple[int, int], tuple[int, ...]]:
    formats: dict[tuple[int, int], set[int]] = {}
    current: tuple[int, int] | None = None
    for line in output.splitlines():
        size = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size:
            current = (int(size.group(1)), int(size.group(2)))
            formats.setdefault(current, set())
            continue
        rate = re.search(r"\(([0-9.]+)\s+fps\)", line)
        if current and rate:
            formats[current].add(round(float(rate.group(1))))
    return {resolution: tuple(sorted(rates)) for resolution, rates in formats.items() if rates}


def discover_capture_formats(device_path: str) -> dict[tuple[int, int], tuple[int, ...]]:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device_path, "--list-formats-ext"],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_v4l2_formats(result.stdout)


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    width: int = 1280
    height: int = 720
    fps: int = 30

    @property
    def label(self) -> str:
        return f"{self.width}×{self.height} · {self.fps} fps"


@dataclass(frozen=True, slots=True)
class Frame:
    width: int
    height: int
    stride: int
    data: bytes
    pts: int


class PreviewError(RuntimeError):
    pass


class PreviewStream:
    """GStreamer V4L2 preview that exposes RGB frames to the GTK main loop."""

    def __init__(self, device_path: str, config: PreviewConfig | None = None):
        self.device_path = device_path
        self.config = config or PreviewConfig()
        self.pipeline: Gst.Pipeline | None = None
        self.appsink: GstApp.AppSink | None = None
        self.effects: Gst.Element | None = None
        self.processor = EffectProcessor()
        self._effect_error_reported = False
        self._frames: queue.Queue[Frame] = queue.Queue(maxsize=2)
        self._errors: queue.Queue[str] = queue.Queue(maxsize=8)
        self._consumers: list[Callable[[Frame], None]] = []
        self._consumer_lock = threading.Lock()
        self.running = False
        self._filter_name = "none"

    def add_consumer(self, consumer: Callable[[Frame], None]) -> None:
        with self._consumer_lock:
            if consumer not in self._consumers:
                self._consumers.append(consumer)

    def remove_consumer(self, consumer: Callable[[Frame], None]) -> None:
        with self._consumer_lock:
            if consumer in self._consumers:
                self._consumers.remove(consumer)

    def _put_latest(self, frame: Frame) -> None:
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            with suppress(queue.Empty):
                self._frames.get_nowait()
            with suppress(queue.Full):
                self._frames.put_nowait(frame)

    def _push_error(self, message: str) -> None:
        with suppress(queue.Full):
            self._errors.put_nowait(message)

    def _on_sample(self, sink: GstApp.AppSink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        buffer = sample.get_buffer()
        ok, map_info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            self._push_error("Could not map a preview frame")
            return Gst.FlowReturn.ERROR
        try:
            data = bytes(map_info.data)
            stride = len(data) // height if height else width * 3
            try:
                processed = self.processor.process(width, height, stride, data, int(buffer.pts))
                frame = Frame(*processed)
                self._effect_error_reported = False
            except Exception as exc:
                if not self._effect_error_reported:
                    self._push_error(f"Video effect failed: {exc}")
                    self._effect_error_reported = True
                frame = Frame(width, height, stride, data, int(buffer.pts))
        finally:
            buffer.unmap(map_info)
        self._put_latest(frame)
        with self._consumer_lock:
            consumers = tuple(self._consumers)
        for consumer in consumers:
            try:
                consumer(frame)
            except Exception as exc:
                self._push_error(f"Frame consumer failed: {exc}")
        return Gst.FlowReturn.OK

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            detail = f" ({debug})" if debug else ""
            self._push_error(f"{error.message}{detail}")
            self.running = False
        elif message.type == Gst.MessageType.EOS:
            self.running = False

    def start(self) -> None:
        if self.running:
            return
        self.stop()
        config = self.config
        pipeline_description = (
            f'v4l2src device="{_gst_quote(self.device_path)}" do-timestamp=true '
            f"! image/jpeg,width={config.width},height={config.height},framerate={config.fps}/1 "
            "! queue leaky=downstream max-size-buffers=2 "
            "! jpegdec ! videoconvert ! videobalance name=preview_effects "
            "! video/x-raw,format=RGB,pixel-aspect-ratio=1/1 "
            "! appsink name=preview_sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        try:
            pipeline = Gst.parse_launch(pipeline_description)
        except GLib.Error as exc:
            raise PreviewError(str(exc)) from exc
        if not isinstance(pipeline, Gst.Pipeline):
            raise PreviewError("GStreamer did not create a preview pipeline")
        sink = pipeline.get_by_name("preview_sink")
        if not isinstance(sink, GstApp.AppSink):
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError("GStreamer appsink is unavailable")
        sink.connect("new-sample", self._on_sample)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        change = pipeline.set_state(Gst.State.PLAYING)
        if change == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError("The camera stream could not be started")
        self.pipeline = pipeline
        self.appsink = sink
        self._bind_effects(pipeline.get_by_name("preview_effects"))
        self.processor.reset_analysis()
        self.running = True

    def stop(self) -> None:
        pipeline, self.pipeline = self.pipeline, None
        self.appsink = None
        self.effects = None
        self.running = False
        if pipeline:
            bus = pipeline.get_bus()
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
        while not self._frames.empty():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                break

    def take_latest(self) -> Frame | None:
        latest = None
        while True:
            try:
                latest = self._frames.get_nowait()
            except queue.Empty:
                return latest

    def take_error(self) -> str | None:
        try:
            return self._errors.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def _filter_values(name: str) -> tuple[float, float, float, float]:
        presets = {
            "none": (0.0, 1.0, 1.0, 1.0),
            "mono": (0.0, 0.0, 1.02, 1.0),
            "punch": (0.0, 1.22, 1.14, 1.0),
            "soft": (0.03, 0.90, 0.90, 1.0),
        }
        try:
            return presets[name]
        except KeyError as exc:
            raise ValueError(f"unsupported live filter: {name}") from exc

    def _bind_effects(self, effects: Gst.Element | None) -> None:
        self.effects = effects
        self._apply_filter()

    def _apply_filter(self) -> None:
        brightness, saturation, contrast, hue = self._filter_values(self._filter_name)
        if self.effects:
            self.effects.set_property("brightness", brightness)
            self.effects.set_property("saturation", saturation)
            self.effects.set_property("contrast", contrast)
            self.effects.set_property("hue", hue)

    def set_filter(self, name: str) -> None:
        self._filter_values(name)
        self._filter_name = name
        self._apply_filter()

    @property
    def filter_name(self) -> str:
        return self._filter_name

    @property
    def effect_settings(self) -> EffectSettings:
        return self.processor.settings

    @property
    def output_dimensions(self) -> tuple[int, int]:
        if self.effect_settings.orientation in {"rotate_right", "rotate_left"}:
            return self.config.height, self.config.width
        return self.config.width, self.config.height

    @property
    def output_label(self) -> str:
        width, height = self.output_dimensions
        return f"{width}×{height} · {self.config.fps} fps"

    def set_effects(self, **changes: object) -> EffectSettings:
        self._effect_error_reported = False
        return self.processor.update(**changes)

    def close(self) -> None:
        self.stop()
        self.processor.close()


class Recorder:
    """Encode RGB preview frames and an optional PipeWire source to MP4."""

    def __init__(
        self,
        output_path: Path,
        width: int,
        height: int,
        fps: int,
        audio_source: str | None = None,
    ):
        self.output_path = output_path
        self.width = width
        self.height = height
        self.fps = fps
        self.audio_source = audio_source
        self.pipeline: Gst.Pipeline | None = None
        self.source: GstApp.AppSrc | None = None
        self._lock = threading.Lock()
        self._error: str | None = None

    def start(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        location = _gst_quote(str(self.output_path))
        if Gst.ElementFactory.find("openh264enc"):
            encoder = "openh264enc bitrate=12000000 complexity=low ! h264parse"
        elif Gst.ElementFactory.find("avenc_mpeg4"):
            encoder = "avenc_mpeg4 bitrate=12000000"
        else:
            raise PreviewError("No GStreamer H.264 or MPEG-4 encoder is installed")
        description = f'mp4mux name=mux faststart=true ! filesink location="{location}" '
        description += (
            "appsrc name=record_source is-live=true block=false format=time do-timestamp=true "
            "caps=video/x-raw,format=RGB,"
            f"width={self.width},height={self.height},framerate={self.fps}/1 "
            f"! queue max-size-buffers=120 ! videoconvert ! {encoder} ! queue ! mux. "
        )
        if self.audio_source:
            source_name = _gst_quote(self.audio_source)
            if Gst.ElementFactory.find("fdkaac"):
                audio_encoder = "fdkaac bitrate=128000"
            elif Gst.ElementFactory.find("avenc_aac"):
                audio_encoder = "avenc_aac bitrate=128000"
            else:
                raise PreviewError("No GStreamer AAC encoder is installed")
            description += (
                f'pulsesrc device="{source_name}" do-timestamp=true '
                "! queue ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 "
                f"! {audio_encoder} ! aacparse ! queue ! mux."
            )
        pipeline = Gst.parse_launch(description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise PreviewError("GStreamer did not create a recording pipeline")
        source = pipeline.get_by_name("record_source")
        if not isinstance(source, GstApp.AppSrc):
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError("GStreamer appsrc is unavailable")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError("The recording encoder could not be started")
        self.pipeline = pipeline
        self.source = source

    def push(self, frame: Frame) -> None:
        with self._lock:
            if not self.source or frame.width != self.width or frame.height != self.height:
                return
            buffer = Gst.Buffer.new_wrapped(frame.data)
            duration = Gst.SECOND // self.fps
            buffer.duration = duration
            result = self.source.emit("push-buffer", buffer)
            if result != Gst.FlowReturn.OK:
                self._error = f"recording pipeline returned {result.value_nick}"

    def stop(self) -> Path:
        with self._lock:
            source, self.source = self.source, None
            pipeline, self.pipeline = self.pipeline, None
        if not pipeline:
            return self.output_path
        if source:
            source.emit("end-of-stream")
        # A pipeline-level EOS also stops an optional live microphone branch;
        # otherwise mp4mux waits forever for pulsesrc and never writes `moov`.
        pipeline.send_event(Gst.Event.new_eos())
        bus = pipeline.get_bus()
        message = bus.timed_pop_filtered(
            5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        if message and message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self._error = error.message
        pipeline.set_state(Gst.State.NULL)
        if self._error:
            raise PreviewError(self._error)
        return self.output_path


def discover_virtual_camera_devices(sys_root: Path = Path("/sys/class/video4linux")) -> list[str]:
    """Return capture/output nodes backed by the v4l2loopback driver."""

    devices = []
    if not sys_root.exists():
        return devices
    for node in sorted(sys_root.glob("video*"), key=lambda path: path.name):
        try:
            name = (node / "name").read_text(encoding="utf-8", errors="replace").strip().casefold()
            driver = (node / "device/driver").resolve().name.casefold()
        except OSError:
            continue
        if "v4l2loopback" in driver or "link studio virtual camera" in name:
            devices.append(f"/dev/{node.name}")
    return devices


class VirtualCameraPublisher:
    """Publish Link Studio's processed RGB frames to a v4l2loopback node."""

    def __init__(self, device_path: str, width: int, height: int, fps: int):
        self.device_path = device_path
        self.width = width
        self.height = height
        self.fps = fps
        self.pipeline: Gst.Pipeline | None = None
        self.source: GstApp.AppSrc | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        description = (
            "appsrc name=virtual_source is-live=true block=false format=time do-timestamp=true "
            "caps=video/x-raw,format=RGB,"
            f"width={self.width},height={self.height},framerate={self.fps}/1 "
            "! queue leaky=downstream max-size-buffers=2 ! videoconvert "
            "! video/x-raw,format=YUY2,"
            f"width={self.width},height={self.height},framerate={self.fps}/1 "
            f'! v4l2sink sync=false device="{_gst_quote(self.device_path)}"'
        )
        pipeline = Gst.parse_launch(description)
        if not isinstance(pipeline, Gst.Pipeline):
            raise PreviewError("GStreamer did not create a virtual-camera pipeline")
        source = pipeline.get_by_name("virtual_source")
        if not isinstance(source, GstApp.AppSrc):
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError("GStreamer appsrc is unavailable")
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise PreviewError(f"Could not open virtual camera {self.device_path}")
        self.pipeline = pipeline
        self.source = source

    def push(self, frame: Frame) -> None:
        with self._lock:
            if not self.source or frame.width != self.width or frame.height != self.height:
                return
            buffer = Gst.Buffer.new_wrapped(frame.data)
            duration = Gst.SECOND // self.fps
            buffer.duration = duration
            self.source.emit("push-buffer", buffer)

    def stop(self) -> None:
        with self._lock:
            self.source = None
            pipeline, self.pipeline = self.pipeline, None
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
