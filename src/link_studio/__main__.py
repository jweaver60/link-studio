from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .camera import Camera, discover_cameras
from .theme import load_current_omarchy_palette


def _diagnose(device_path: str | None) -> int:
    devices = discover_cameras()
    report: dict[str, object] = {
        "link_studio_version": __version__,
        "devices": [device.as_dict() for device in devices],
        "omarchy_theme": None,
    }
    palette = load_current_omarchy_palette()
    if palette:
        report["omarchy_theme"] = palette.as_dict()

    selected = next((item for item in devices if item.path == device_path), None)
    if selected is None and devices:
        selected = devices[0]
    if selected:
        with Camera(selected) as camera:
            report["selected_device"] = selected.as_dict()
            report["state"] = camera.read_state()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if devices else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Native Linux controller for Insta360 Link webcams"
    )
    parser.add_argument("--device", help="V4L2 capture node (auto-detected by default)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="print a read-only JSON device/theme report and exit",
    )
    parser.add_argument("--no-preview", action="store_true", help="launch without opening video")
    args = parser.parse_args(argv)

    if args.diagnose:
        return _diagnose(args.device)

    # GTK may consult Settings through the desktop portal while it is imported. Native
    # processes must register their app ID on the same bus connection before that happens.
    from .shortcuts import try_register_portal_identity

    portal_connection, portal_identity_error = try_register_portal_identity()
    from .application import LinkStudioApplication

    application = LinkStudioApplication(
        device_path=args.device,
        start_preview=not args.no_preview,
        portal_connection=portal_connection,
        portal_identity_error=portal_identity_error,
    )
    return application.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
