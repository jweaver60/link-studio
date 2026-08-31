from __future__ import annotations

from .effects import Rect


def contained_rect(
    container_width: float,
    container_height: float,
    content_width: float,
    content_height: float,
) -> tuple[float, float, float, float]:
    """Return the centered rectangle used by a CONTAIN-fit image."""

    if min(container_width, container_height, content_width, content_height) <= 0:
        return 0.0, 0.0, 0.0, 0.0
    scale = min(container_width / content_width, container_height / content_height)
    width = content_width * scale
    height = content_height * scale
    return (container_width - width) / 2, (container_height - height) / 2, width, height


def frame_region_from_drag(
    start: tuple[float, float],
    end: tuple[float, float],
    container_size: tuple[float, float],
    frame_size: tuple[float, float],
) -> Rect | None:
    """Map a widget-space drag to normalized frame coordinates, excluding letterbox bars."""

    x, y, width, height = contained_rect(*container_size, *frame_size)
    if width <= 0 or height <= 0:
        return None

    def normalized(point: tuple[float, float]) -> tuple[float, float]:
        point_x = min(x + width, max(x, point[0]))
        point_y = min(y + height, max(y, point[1]))
        return (point_x - x) / width, (point_y - y) / height

    start_x, start_y = normalized(start)
    end_x, end_y = normalized(end)
    left, right = sorted((start_x, end_x))
    top, bottom = sorted((start_y, end_y))
    return left, top, right - left, bottom - top
