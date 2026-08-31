from __future__ import annotations

import ctypes
import errno
import os
import re
import struct
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Self

from .constants import (
    AUDIO_MODES,
    FRAMING_MODES,
    INSTA360_VENDOR_ID,
    STANDARD_CONTROLS,
    SUPPORTED_PRODUCTS,
    VIDEO_MODES,
)


class CameraError(RuntimeError):
    """A device or control operation failed."""


class CameraOperationCancelled(CameraError):
    """A camera operation was cooperatively cancelled during application shutdown."""


@dataclass(frozen=True, slots=True)
class CameraDevice:
    path: str
    name: str
    vendor_id: int
    product_id: int
    bus_info: str = ""
    driver: str = "uvcvideo"

    @property
    def model(self) -> str:
        return SUPPORTED_PRODUCTS.get(self.product_id, self.name)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["vendor_id"] = f"{self.vendor_id:04x}"
        data["product_id"] = f"{self.product_id:04x}"
        data["model"] = self.model
        return data


class _V4L2Capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class _V4L2Control(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("value", ctypes.c_int32)]


class _UvcXuControlQuery(ctypes.Structure):
    _fields_ = [
        ("unit", ctypes.c_uint8),
        ("selector", ctypes.c_uint8),
        ("query", ctypes.c_uint8),
        ("size", ctypes.c_uint16),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, ioctl_type: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ioctl_type << 8) | number


def _iorw(ioctl_type: str, number: int, ctype: type[ctypes.Structure]) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, ord(ioctl_type), number, ctypes.sizeof(ctype))


def _ior(ioctl_type: str, number: int, ctype: type[ctypes.Structure]) -> int:
    return _ioc(_IOC_READ, ord(ioctl_type), number, ctypes.sizeof(ctype))


VIDIOC_QUERYCAP = _ior("V", 0, _V4L2Capability)
VIDIOC_G_CTRL = _iorw("V", 27, _V4L2Control)
VIDIOC_S_CTRL = _iorw("V", 28, _V4L2Control)
UVCIOC_CTRL_QUERY = _iorw("u", 0x21, _UvcXuControlQuery)

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_LEN = 0x85

XU_FEATURE_UNIT = 9
XU_PRIVACY_UNIT = 10
XU_VIDEO_MODE = 0x02
XU_DEVICE_INFO = 0x03
XU_NOISE_CANCEL = 0x07
XU_EXPOSURE_COMP = 0x09
XU_TRACK_SPEED = 0x12
XU_FRAMING = 0x13
XU_ISO = 0x19
XU_FEATURE_MASK = 0x1B
XU_SHUTTER = 0x1D
XU_AUTO_EXPOSURE = 0x1E
XU_PRIVACY = 0x0F

FEATURE_HDR = 2
FEATURE_MIRROR = 3
FEATURE_GESTURE_ZOOM = 4
FEATURE_PRIVACY = 11

_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.ioctl.restype = ctypes.c_int


def _decode_c_string(value: bytes | ctypes.Array[ctypes.c_char]) -> str:
    return bytes(value).split(b"\0", 1)[0].decode(errors="replace")


def _usb_ids_for_video(sys_video_path: Path) -> tuple[int, int] | None:
    try:
        current = (sys_video_path / "device").resolve()
    except OSError:
        return None
    for parent in (current, *current.parents):
        vendor_path = parent / "idVendor"
        product_path = parent / "idProduct"
        if vendor_path.is_file() and product_path.is_file():
            try:
                return int(vendor_path.read_text().strip(), 16), int(
                    product_path.read_text().strip(), 16
                )
            except (OSError, ValueError):
                return None
    return None


def _query_capability(path: str) -> _V4L2Capability | None:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        capability = _V4L2Capability()
        if _LIBC.ioctl(fd, VIDIOC_QUERYCAP, ctypes.byref(capability)) < 0:
            return None
        return capability
    finally:
        os.close(fd)


def discover_cameras(
    dev_root: Path = Path("/dev"), sys_root: Path = Path("/sys/class/video4linux")
) -> list[CameraDevice]:
    """Return Insta360 capture nodes, excluding UVC metadata-only nodes."""

    devices: list[CameraDevice] = []
    if not sys_root.exists():
        return devices
    for sys_node in sorted(sys_root.glob("video*"), key=lambda item: item.name):
        ids = _usb_ids_for_video(sys_node)
        if not ids or ids[0] != INSTA360_VENDOR_ID:
            continue
        path = str(dev_root / sys_node.name)
        capability = _query_capability(path)
        if capability is None:
            continue
        device_caps = capability.device_caps or capability.capabilities
        if not device_caps & V4L2_CAP_VIDEO_CAPTURE:
            continue
        product_id = ids[1]
        devices.append(
            CameraDevice(
                path=path,
                name=_decode_c_string(capability.card),
                vendor_id=ids[0],
                product_id=product_id,
                bus_info=_decode_c_string(capability.bus_info),
                driver=_decode_c_string(capability.driver),
            )
        )
    return devices


class Camera:
    """Cooperative V4L2 and UVC Extension Unit controller.

    This backend deliberately never detaches the kernel UVC driver. Standard
    controls and vendor XU controls therefore remain usable while a preview or
    another camera client owns the video stream.
    """

    def __init__(self, device: CameraDevice):
        self.device = device
        self._fd = os.open(device.path, os.O_RDWR | os.O_NONBLOCK)
        self._lock = threading.RLock()
        self._xu_lengths: dict[tuple[int, int], int] = {}
        self._last_mode = "unknown"
        self._last_mode_time = 0.0
        self._cancel_operations = threading.Event()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

    def cancel_pending_operations(self) -> None:
        self._cancel_operations.set()

    def _check_cancelled(self) -> None:
        if self._cancel_operations.is_set():
            raise CameraOperationCancelled("camera operation cancelled during shutdown")

    def _wait_or_cancel(self, seconds: float) -> None:
        if self._cancel_operations.wait(seconds):
            self._check_cancelled()

    def _ioctl(self, request: int, value: ctypes.Structure) -> None:
        self._check_cancelled()
        if self._fd < 0:
            raise CameraError("camera is closed")
        ctypes.set_errno(0)
        result = _LIBC.ioctl(self._fd, request, ctypes.byref(value))
        if result < 0:
            error_number = ctypes.get_errno() or errno.EIO
            raise OSError(error_number, os.strerror(error_number), self.device.path)

    def get_control(self, key: str) -> int:
        spec = STANDARD_CONTROLS[key]
        value = _V4L2Control(id=spec.control_id, value=0)
        with self._lock:
            self._ioctl(VIDIOC_G_CTRL, value)
        if key in {"pan", "tilt"}:
            return round(value.value / 3600)
        return value.value

    def set_control(self, key: str, new_value: int | bool) -> int:
        spec = STANDARD_CONTROLS[key]
        numeric = int(new_value)
        if not spec.minimum <= numeric <= spec.maximum:
            raise ValueError(f"{key} must be between {spec.minimum} and {spec.maximum}")
        wire_value = numeric * 3600 if key in {"pan", "tilt"} else numeric
        value = _V4L2Control(id=spec.control_id, value=wire_value)
        with self._lock:
            self._ioctl(VIDIOC_S_CTRL, value)
        return self.get_control(key)

    def center(self) -> None:
        # These stay on the cooperative V4L2 path. Some firmware revisions may
        # reject PTZ SET; callers get the kernel error instead of a driver detach.
        self.set_control("pan", 0)
        self.set_control("tilt", 0)
        self.set_control("zoom", 100)

    def _xu_query(self, unit: int, selector: int, query: int, payload: bytearray) -> bytes:
        if not payload:
            raise ValueError("XU payload cannot be empty")
        data = (ctypes.c_uint8 * len(payload)).from_buffer(payload)
        request = _UvcXuControlQuery(
            unit=unit,
            selector=selector,
            query=query,
            size=len(payload),
            data=ctypes.cast(data, ctypes.POINTER(ctypes.c_uint8)),
        )
        with self._lock:
            self._ioctl(UVCIOC_CTRL_QUERY, request)
        return bytes(payload)

    def xu_length(self, unit: int, selector: int, fallback: int | None = None) -> int:
        cache_key = (unit, selector)
        if cache_key in self._xu_lengths:
            return self._xu_lengths[cache_key]
        try:
            raw = self._xu_query(unit, selector, UVC_GET_LEN, bytearray(2))
            length = int.from_bytes(raw, "little")
            if length <= 0:
                raise CameraError(f"invalid XU length {length}")
        except CameraOperationCancelled:
            raise
        except (OSError, CameraError):
            if fallback is None:
                raise
            length = fallback
        self._xu_lengths[cache_key] = length
        return length

    def xu_get(self, unit: int, selector: int, fallback_length: int | None = None) -> bytes:
        length = self.xu_length(unit, selector, fallback_length)
        return self._xu_query(unit, selector, UVC_GET_CUR, bytearray(length))

    def xu_set(self, unit: int, selector: int, payload: bytes) -> None:
        self._xu_query(unit, selector, UVC_SET_CUR, bytearray(payload))

    def _feature_mask(self) -> int:
        raw = self.xu_get(XU_FEATURE_UNIT, XU_FEATURE_MASK, 2)
        return int.from_bytes(raw[:2], "little")

    def get_feature(self, bit: int) -> bool:
        return bool(self._feature_mask() & (1 << bit))

    def set_feature(self, bit: int, enabled: bool) -> bool:
        with self._lock:
            mask = self._feature_mask()
            if enabled:
                mask |= 1 << bit
            else:
                mask &= ~(1 << bit)
            self.xu_set(XU_FEATURE_UNIT, XU_FEATURE_MASK, mask.to_bytes(2, "little"))
        return self.get_feature(bit)

    def read_video_mode(self) -> str:
        raw = self.xu_get(XU_FEATURE_UNIT, XU_VIDEO_MODE, 61)
        if not raw:
            return "unknown"
        mode_id = raw[0]
        by_id = {values[0]: key for key, values in VIDEO_MODES.items()}
        if mode_id == 0xFF and time.monotonic() - self._last_mode_time < 10:
            return self._last_mode
        return by_id.get(mode_id, "transition" if mode_id == 0xFF else "unknown")

    def _write_video_mode_buffer(self, mode_id: int, flag: int) -> None:
        length = self.xu_length(XU_FEATURE_UNIT, XU_VIDEO_MODE, 61)
        payload = bytearray(length)
        if length >= 61:
            try:
                current = self.xu_get(XU_FEATURE_UNIT, XU_VIDEO_MODE, 61)
                payload[52:] = current[52:]
            except OSError:
                pass
        payload[0] = mode_id
        if length > 1:
            payload[1] = flag
        self.xu_set(XU_FEATURE_UNIT, XU_VIDEO_MODE, bytes(payload))

    def set_video_mode(self, mode: str, verify_streaming: bool = True) -> str:
        """Set a Link 2 hardware AI mode without detaching the UVC driver.

        The firmware consumes mode commands only while its AI engine has an
        active stream. Link Studio's preview satisfies that requirement. The
        spaced retries mirror the observed firmware handshake and avoid the
        rapid writes known to wedge the device.
        """

        if mode not in VIDEO_MODES:
            raise ValueError(f"unsupported video mode: {mode}")
        self._check_cancelled()
        mode_id, flag, _label = VIDEO_MODES[mode]
        self._last_mode = mode
        self._last_mode_time = time.monotonic()

        if mode == "normal":
            self._write_video_mode_buffer(mode_id, flag)
            return mode

        if verify_streaming:
            ready_deadline = time.monotonic() + 4.0
            while time.monotonic() < ready_deadline:
                self._check_cancelled()
                raw = self.xu_get(XU_FEATURE_UNIT, XU_VIDEO_MODE, 61)
                if raw and raw[0] != 0xFF:
                    break
                self._wait_or_cancel(0.3)

        self._write_video_mode_buffer(0, 0)
        self._wait_or_cancel(0.6)
        self._write_video_mode_buffer(mode_id, flag)

        if not verify_streaming:
            return mode
        stable_reads = 0
        deadline = time.monotonic() + 9.0
        reassert_at = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            self._wait_or_cancel(0.4)
            raw = self.xu_get(XU_FEATURE_UNIT, XU_VIDEO_MODE, 61)
            current = raw[0] if raw else None
            if current == mode_id:
                stable_reads += 1
                if stable_reads >= 3:
                    return mode
            else:
                stable_reads = 0
            if time.monotonic() >= reassert_at:
                self._write_video_mode_buffer(mode_id, flag)
                reassert_at = time.monotonic() + 2.5
        raise CameraError(f"camera did not settle into {mode} mode while streaming")

    def get_exposure_compensation(self) -> int:
        raw = self.xu_get(XU_FEATURE_UNIT, XU_EXPOSURE_COMP, 2)
        return max(0, min(100, struct.unpack("<h", raw[:2])[0] // 6 + 50))

    def set_exposure_compensation(self, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("exposure compensation must be 0..100")
        self.xu_set(XU_FEATURE_UNIT, XU_EXPOSURE_COMP, struct.pack("<h", (value - 50) * 6))
        return self.get_exposure_compensation()

    def get_auto_exposure(self) -> bool:
        return self.xu_get(XU_FEATURE_UNIT, XU_AUTO_EXPOSURE, 1)[0] == 2

    def set_auto_exposure(self, enabled: bool) -> bool:
        self.xu_set(XU_FEATURE_UNIT, XU_AUTO_EXPOSURE, bytes([2 if enabled else 1]))
        return self.get_auto_exposure()

    def get_manual_iso(self) -> int:
        return int.from_bytes(self.xu_get(XU_FEATURE_UNIT, XU_ISO, 2)[:2], "little")

    def set_manual_iso(self, value: int) -> int:
        if not 0 <= value <= 65535:
            raise ValueError("ISO must be 0..65535")
        self.xu_set(XU_FEATURE_UNIT, XU_ISO, struct.pack("<H", value))
        return self.get_manual_iso()

    def get_shutter(self) -> int:
        return int.from_bytes(self.xu_get(XU_FEATURE_UNIT, XU_SHUTTER, 2)[:2], "little")

    def set_shutter(self, microseconds: int) -> int:
        if not 0 <= microseconds <= 65535:
            raise ValueError("shutter must be 0..65535 microseconds")
        self.xu_set(XU_FEATURE_UNIT, XU_SHUTTER, struct.pack("<H", microseconds))
        return self.get_shutter()

    def get_tracking_speed(self) -> int:
        return self.xu_get(XU_FEATURE_UNIT, XU_TRACK_SPEED, 1)[0]

    def set_tracking_speed(self, value: int) -> int:
        if not 0 <= value <= 255:
            raise ValueError("tracking speed must be 0..255")
        self.xu_set(XU_FEATURE_UNIT, XU_TRACK_SPEED, bytes([value]))
        return self.get_tracking_speed()

    def get_framing(self) -> str:
        current = self.xu_get(XU_FEATURE_UNIT, XU_FRAMING, 1)[0]
        return next((name for name, value in FRAMING_MODES.items() if value == current), "unknown")

    def set_framing(self, framing: str) -> str:
        value = FRAMING_MODES[framing]
        self.xu_set(XU_FEATURE_UNIT, XU_FRAMING, bytes([value]))
        return self.get_framing()

    def get_noise_cancellation(self) -> bool:
        return self.get_audio_mode() != "music_balance"

    def set_noise_cancellation(self, enabled: bool) -> bool:
        self.set_audio_mode("voice_focus" if enabled else "music_balance")
        return self.get_noise_cancellation()

    def get_audio_mode(self) -> str:
        current = self.xu_get(XU_FEATURE_UNIT, XU_NOISE_CANCEL, 1)[0]
        return next((name for name, value in AUDIO_MODES.items() if value == current), "unknown")

    def set_audio_mode(self, mode: str) -> str:
        if mode not in AUDIO_MODES:
            raise ValueError(f"unsupported audio mode: {mode}")
        self.xu_set(XU_FEATURE_UNIT, XU_NOISE_CANCEL, bytes([AUDIO_MODES[mode]]))
        return self.get_audio_mode()

    def get_privacy(self) -> bool:
        return self.get_feature(FEATURE_PRIVACY)

    def set_privacy(self, enabled: bool) -> bool:
        self.set_feature(FEATURE_PRIVACY, enabled)
        length = self.xu_length(XU_PRIVACY_UNIT, XU_PRIVACY, 2)
        wire_value = 2 if enabled else 0
        payload = wire_value.to_bytes(length, "little")
        # The feature bit is authoritative; some firmware rejects this companion write.
        with suppress(OSError):
            self.xu_set(XU_PRIVACY_UNIT, XU_PRIVACY, payload)
        return self.get_privacy()

    def get_device_information(self) -> dict[str, str]:
        """Return stable, read-only identity and firmware fields from Link firmware."""

        raw = self.xu_get(XU_FEATURE_UNIT, XU_DEVICE_INFO, 234)
        strings = [
            match.decode("ascii", errors="replace")
            for match in re.findall(rb"[\x20-\x7e]{4,}", raw)
        ]
        firmware = next((value for value in strings if value.casefold().startswith("v")), "unknown")
        hardware = next(
            (
                value
                for value in strings
                if re.fullmatch(r"[A-Z0-9]{18,30}", value)
                and any(character.isdigit() for character in value)
            ),
            "unknown",
        )
        build = next(
            (
                value
                for value in strings
                if value not in {firmware, hardware} and "-" not in value and len(value) < 40
            ),
            "unknown",
        )
        return {
            "firmware_version": firmware,
            "hardware_revision": hardware,
            "firmware_build": build,
        }

    def read_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key in STANDARD_CONTROLS:
            try:
                state[key] = self.get_control(key)
            except OSError as exc:
                errors[key] = str(exc)

        custom_readers = {
            "mode": self.read_video_mode,
            "hdr": lambda: self.get_feature(FEATURE_HDR),
            "mirror": lambda: self.get_feature(FEATURE_MIRROR),
            "gesture_zoom": lambda: self.get_feature(FEATURE_GESTURE_ZOOM),
            "privacy": self.get_privacy,
            "auto_exposure": self.get_auto_exposure,
            "exposure_compensation": self.get_exposure_compensation,
            "tracking_speed": self.get_tracking_speed,
            "framing": self.get_framing,
            "noise_cancellation": self.get_noise_cancellation,
            "audio_mode": self.get_audio_mode,
            "iso": self.get_manual_iso,
            "shutter_us": self.get_shutter,
            "device_information": self.get_device_information,
        }
        for key, reader in custom_readers.items():
            try:
                state[key] = reader()
            except CameraOperationCancelled:
                raise
            except (OSError, CameraError, ValueError, struct.error, IndexError) as exc:
                errors[key] = str(exc)
        if errors:
            state["unavailable"] = errors
        return state
