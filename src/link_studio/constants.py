from __future__ import annotations

from dataclasses import dataclass

APP_ID = "io.github.linkstudio.LinkStudio"
APP_NAME = "Link Studio"
INSTA360_VENDOR_ID = 0x2E1A

SUPPORTED_PRODUCTS = {
    0x4C01: "Insta360 Link",
    0x4C02: "Insta360 Link 2C",
    0x4C03: "Insta360 Link 2 Pro",
    0x4C04: "Insta360 Link 2",
}


@dataclass(frozen=True, slots=True)
class ControlSpec:
    key: str
    label: str
    control_id: int
    minimum: int
    maximum: int
    step: int = 1
    default: int = 0
    unit: str = ""


# Standard UVC controls exposed by the Link 2 through V4L2.
STANDARD_CONTROLS = {
    "brightness": ControlSpec("brightness", "Brightness", 0x00980900, 0, 100, 1, 50),
    "contrast": ControlSpec("contrast", "Contrast", 0x00980901, 0, 100, 1, 50),
    "saturation": ControlSpec("saturation", "Saturation", 0x00980902, 0, 100, 1, 50),
    "hue": ControlSpec("hue", "Hue", 0x00980903, -15, 15, 1, 0),
    "white_balance_auto": ControlSpec(
        "white_balance_auto", "Automatic white balance", 0x0098090C, 0, 1, 1, 1
    ),
    "anti_flicker": ControlSpec("anti_flicker", "Anti-flicker", 0x00980918, 0, 3, 1, 3),
    "white_balance_temperature": ControlSpec(
        "white_balance_temperature", "Color temperature", 0x0098091A, 2000, 10000, 100, 6400, "K"
    ),
    "sharpness": ControlSpec("sharpness", "Sharpness", 0x0098091B, 0, 100, 1, 50),
    "pan": ControlSpec("pan", "Pan", 0x009A0908, -145, 145, 1, 0, "°"),
    "tilt": ControlSpec("tilt", "Tilt", 0x009A0909, -90, 100, 1, 0, "°"),
    "focus": ControlSpec("focus", "Focus", 0x009A090A, 0, 100, 1, 50),
    "focus_auto": ControlSpec("focus_auto", "Autofocus", 0x009A090C, 0, 1, 1, 1),
    "zoom": ControlSpec("zoom", "Zoom", 0x009A090D, 100, 400, 1, 100, "%"),
}

VIDEO_MODES = {
    "normal": (0x00, 0x00, "Normal"),
    "tracking": (0x01, 0x00, "AI Tracking"),
    "whiteboard": (0x04, 0x01, "Whiteboard"),
    "overhead": (0x05, 0x03, "Overhead"),
    "deskview": (0x06, 0x10, "DeskView"),
}

FRAMING_MODES = {"head": 1, "half_body": 2, "whole_body": 3}
ANTI_FLICKER_LABELS = ("Disabled", "50 Hz", "60 Hz", "Auto")
TRACKING_SPEED_MAX = 255

# Link 2/2C microphone DSP modes carried by XU9 selector 0x07. The default
# firmware value is Voice Focus. Music Balance is the unprocessed/no-denoise
# mode exposed by the official client.
AUDIO_MODES = {"music_balance": 0, "voice_focus": 1, "voice_suppression": 2}
