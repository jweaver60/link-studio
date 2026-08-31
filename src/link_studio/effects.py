from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Any

Rect = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class EffectSettings:
    """Settings for Link Studio's local, software-rendered video effects."""

    mode: str = "none"
    intensity: int = 55
    background_color: str = "#242424"
    background_image: str | None = None
    key_color: str = "#00ff00"
    key_tolerance: int = 70
    orientation: str = "identity"
    tracking_mode: str = "off"
    tracking_area: Rect = (0.0, 0.0, 1.0, 1.0)
    pause_areas: tuple[Rect, ...] = ()


@dataclass(frozen=True, slots=True)
class TrackingTarget:
    center_x: float
    center_y: float
    size: float
    face_count: int
    paused: bool


class EffectsError(RuntimeError):
    pass


class EffectProcessor:
    """Apply on-device MediaPipe/OpenCV effects to RGB camera frames.

    The heavy runtime and models are opened lazily, so ordinary physical-camera
    control has no inference overhead. Processing occurs on GStreamer's sample
    thread and the most recent segmentation/landmark result is reused between
    inference frames to keep latency bounded.
    """

    MODES = frozenset(
        {
            "none",
            "background_blur",
            "bokeh",
            "background_replace",
            "green_screen",
            "beauty",
            "makeup",
            "smart_whiteboard",
        }
    )
    ORIENTATIONS = frozenset(
        {"identity", "vertical_flip", "rotate_right", "rotate_left", "rotate_180"}
    )
    TRACKING_MODES = frozenset({"off", "single", "group"})

    def __init__(self) -> None:
        self._settings = EffectSettings()
        self._lock = threading.RLock()
        self._tracking_callback: Callable[[TrackingTarget | None], None] | None = None
        self._runtime: tuple[Any, Any, Any] | None = None
        self._segmenter: Any = None
        self._landmarker: Any = None
        self._last_mask: Any = None
        self._last_faces: list[list[tuple[float, float]]] = []
        self._last_segmentation_frame = -100
        self._last_landmark_frame = -100
        self._last_tracking_notice = 0.0
        self._frame_number = 0
        self._timestamp_ms = 0
        self._background_cache: tuple[str, Any] | None = None
        self._whiteboard_quad: Any = None

    @property
    def settings(self) -> EffectSettings:
        with self._lock:
            return self._settings

    @property
    def active(self) -> bool:
        settings = self.settings
        return (
            settings.mode != "none"
            or settings.orientation != "identity"
            or settings.tracking_mode != "off"
        )

    def update(self, **changes: Any) -> EffectSettings:
        with self._lock:
            candidate = replace(self._settings, **changes)
            if candidate.mode not in self.MODES:
                raise ValueError(f"unsupported effect mode: {candidate.mode}")
            if candidate.orientation not in self.ORIENTATIONS:
                raise ValueError(f"unsupported orientation: {candidate.orientation}")
            if candidate.tracking_mode not in self.TRACKING_MODES:
                raise ValueError(f"unsupported tracking mode: {candidate.tracking_mode}")
            if not 0 <= candidate.intensity <= 100:
                raise ValueError("effect intensity must be 0..100")
            if not 0 <= candidate.key_tolerance <= 255:
                raise ValueError("key tolerance must be 0..255")
            self._settings = candidate
            return candidate

    def set_tracking_callback(
        self, callback: Callable[[TrackingTarget | None], None] | None
    ) -> None:
        with self._lock:
            self._tracking_callback = callback

    def reset_analysis(self) -> None:
        """Drop temporal inference state after a stream or scene change."""

        with self._lock:
            self._last_mask = None
            self._last_faces = []
            self._last_segmentation_frame = -100
            self._last_landmark_frame = -100
            self._whiteboard_quad = None

    @staticmethod
    def _model_path(name: str) -> str:
        return str(files("link_studio").joinpath("models", name))

    def _load_runtime(self) -> tuple[Any, Any, Any]:
        if self._runtime is not None:
            return self._runtime
        try:
            import cv2
            import mediapipe as mp
            import numpy as np
        except ImportError as exc:
            raise EffectsError(
                "Advanced effects require the mediapipe, opencv, and numpy packages"
            ) from exc
        self._runtime = (cv2, mp, np)
        return self._runtime

    def _ensure_segmenter(self) -> Any:
        if self._segmenter is not None:
            return self._segmenter
        _cv2, mp, _np = self._load_runtime()
        options = mp.tasks.vision.ImageSegmenterOptions(
            base_options=mp.tasks.BaseOptions(
                model_asset_path=self._model_path("selfie_segmenter.tflite")
            ),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            output_confidence_masks=True,
            output_category_mask=False,
        )
        self._segmenter = mp.tasks.vision.ImageSegmenter.create_from_options(options)
        return self._segmenter

    def _ensure_landmarker(self) -> Any:
        if self._landmarker is not None:
            return self._landmarker
        _cv2, mp, _np = self._load_runtime()
        options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(
                model_asset_path=self._model_path("face_landmarker.task")
            ),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=6,
            min_face_detection_confidence=0.45,
            min_face_presence_confidence=0.45,
            min_tracking_confidence=0.45,
        )
        self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        return self._landmarker

    def _next_timestamp(self) -> int:
        now = time.monotonic_ns() // 1_000_000
        self._timestamp_ms = max(self._timestamp_ms + 1, now)
        return self._timestamp_ms

    def _segment(self, image: Any) -> Any:
        cv2, mp, np = self._load_runtime()
        if self._frame_number - self._last_segmentation_frame >= 2 or self._last_mask is None:
            inference = image
            if image.shape[1] > 960:
                scale = 960 / image.shape[1]
                inference = cv2.resize(
                    image,
                    (960, max(1, round(image.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            result = self._ensure_segmenter().segment_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(inference)),
                self._next_timestamp(),
            )
            mask = result.confidence_masks[0].numpy_view()[..., 0].copy()
            self._last_mask = cv2.resize(
                mask, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR
            )
            self._last_segmentation_frame = self._frame_number
        mask = self._last_mask
        if mask.shape[:2] != image.shape[:2]:
            mask = cv2.resize(mask, (image.shape[1], image.shape[0]))
        mask = cv2.GaussianBlur(mask, (0, 0), 2.2)
        return np.clip((mask - 0.12) / 0.72, 0.0, 1.0)[..., None]

    def _detect_faces(self, image: Any) -> list[list[tuple[float, float]]]:
        cv2, mp, np = self._load_runtime()
        if self._frame_number - self._last_landmark_frame < 3:
            return self._last_faces
        inference = image
        if image.shape[1] > 960:
            scale = 960 / image.shape[1]
            inference = cv2.resize(
                image,
                (960, max(1, round(image.shape[0] * scale))),
                interpolation=cv2.INTER_AREA,
            )
        result = self._ensure_landmarker().detect_for_video(
            mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(inference)),
            self._next_timestamp(),
        )
        self._last_faces = [
            [(float(point.x), float(point.y)) for point in face] for face in result.face_landmarks
        ]
        self._last_landmark_frame = self._frame_number
        return self._last_faces

    @staticmethod
    def _parse_color(value: str) -> tuple[int, int, int]:
        clean = value.strip().lstrip("#")
        if len(clean) != 6:
            return 36, 36, 36
        try:
            return tuple(int(clean[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return 36, 36, 36

    def _background(self, image: Any, settings: EffectSettings) -> Any:
        cv2, _mp, np = self._load_runtime()
        height, width = image.shape[:2]
        path = settings.background_image
        if path:
            if self._background_cache is None or self._background_cache[0] != path:
                loaded = cv2.imread(path, cv2.IMREAD_COLOR)
                if loaded is not None:
                    loaded = cv2.cvtColor(loaded, cv2.COLOR_BGR2RGB)
                self._background_cache = (path, loaded)
            loaded = self._background_cache[1]
            if loaded is not None:
                source_h, source_w = loaded.shape[:2]
                scale = max(width / source_w, height / source_h)
                resized = cv2.resize(
                    loaded,
                    (max(width, round(source_w * scale)), max(height, round(source_h * scale))),
                    interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
                )
                y = (resized.shape[0] - height) // 2
                x = (resized.shape[1] - width) // 2
                return np.ascontiguousarray(resized[y : y + height, x : x + width])
        color = self._parse_color(settings.background_color)
        background = np.empty_like(image)
        background[:] = color
        return background

    @staticmethod
    def _composite(foreground: Any, background: Any, mask: Any) -> Any:
        return (foreground * mask + background * (1.0 - mask)).clip(0, 255).astype("uint8")

    def _apply_background_effect(self, image: Any, settings: EffectSettings) -> Any:
        cv2, _mp, _np = self._load_runtime()
        mask = self._segment(image)
        strength = max(0.05, settings.intensity / 100)
        if settings.mode == "background_replace":
            background = self._background(image, settings)
        else:
            sigma = (8 if settings.mode == "background_blur" else 18) * strength
            background = cv2.GaussianBlur(image, (0, 0), sigmaX=max(0.5, sigma))
            if settings.mode == "bokeh":
                background = cv2.convertScaleAbs(background, alpha=0.88, beta=-2)
        return self._composite(image, background, mask)

    def _apply_green_screen(self, image: Any, settings: EffectSettings) -> Any:
        cv2, _mp, np = self._load_runtime()
        target = np.array(self._parse_color(settings.key_color), dtype=np.float32)
        distance = np.linalg.norm(image.astype(np.float32) - target, axis=2)
        tolerance = max(1.0, float(settings.key_tolerance))
        foreground = np.clip((distance - tolerance * 0.55) / (tolerance * 0.6), 0.0, 1.0)
        foreground = cv2.GaussianBlur(foreground, (0, 0), 1.4)[..., None]
        return self._composite(image, self._background(image, settings), foreground)

    def _apply_beauty(self, image: Any, settings: EffectSettings) -> Any:
        cv2, _mp, np = self._load_runtime()
        strength = settings.intensity / 100
        smooth = cv2.bilateralFilter(image, 0, 22 + 46 * strength, 7 + 11 * strength)
        detailed = cv2.addWeighted(image, 1.08, cv2.GaussianBlur(image, (0, 0), 1.1), -0.08, 4)
        softened = cv2.addWeighted(detailed, 1.0 - 0.62 * strength, smooth, 0.62 * strength, 0)
        try:
            mask = self._segment(image)
        except Exception:
            return softened
        return self._composite(softened, image, np.clip(mask * 1.15, 0.0, 1.0))

    def _apply_makeup(
        self, image: Any, settings: EffectSettings, faces: list[list[tuple[float, float]]]
    ) -> Any:
        cv2, _mp, np = self._load_runtime()
        output = self._apply_beauty(
            image, replace(settings, intensity=max(20, settings.intensity // 2))
        )
        overlay = output.copy()
        height, width = image.shape[:2]
        amount = settings.intensity / 100
        lip_indices = [
            61,
            146,
            91,
            181,
            84,
            17,
            314,
            405,
            321,
            375,
            291,
            308,
            324,
            318,
            402,
            317,
            14,
            87,
            178,
            88,
            95,
        ]
        left_eye = [33, 160, 158, 133, 153, 144]
        right_eye = [362, 385, 387, 263, 373, 380]
        for face in faces:
            if len(face) < 468:
                continue
            points = np.array([(round(x * width), round(y * height)) for x, y in face], np.int32)
            cv2.fillPoly(overlay, [points[lip_indices]], (176, 46, 84), lineType=cv2.LINE_AA)
            for indices in (left_eye, right_eye):
                cv2.polylines(
                    overlay,
                    [points[indices]],
                    False,
                    (45, 22, 30),
                    max(1, round(2 * amount)),
                    lineType=cv2.LINE_AA,
                )
            for cheek_index in (50, 280):
                center = tuple(points[cheek_index])
                face_width = max(1, int((points[:, 0].max() - points[:, 0].min()) * 0.09))
                cv2.circle(overlay, center, face_width, (232, 96, 112), -1, lineType=cv2.LINE_AA)
        return cv2.addWeighted(overlay, 0.08 + 0.34 * amount, output, 0.92 - 0.34 * amount, 0)

    @staticmethod
    def _order_quad(points: Any, np: Any) -> Any:
        ordered = np.zeros((4, 2), dtype=np.float32)
        sums = points.sum(axis=1)
        diffs = np.diff(points, axis=1).ravel()
        ordered[0] = points[np.argmin(sums)]
        ordered[2] = points[np.argmax(sums)]
        ordered[1] = points[np.argmin(diffs)]
        ordered[3] = points[np.argmax(diffs)]
        return ordered

    def _apply_smart_whiteboard(self, image: Any) -> Any:
        cv2, _mp, np = self._load_runtime()
        height, width = image.shape[:2]
        if self._frame_number % 8 == 0 or self._whiteboard_quad is None:
            scale = min(1.0, 960 / width)
            small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            edges = cv2.Canny(gray, 45, 135)
            edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
            contours, _hierarchy = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            best = None
            best_area = 0.0
            for contour in contours:
                perimeter = cv2.arcLength(contour, True)
                polygon = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
                area = abs(cv2.contourArea(polygon))
                if len(polygon) == 4 and area > best_area and area > small.size * 0.035:
                    best = polygon.reshape(4, 2).astype(np.float32) / scale
                    best_area = area
            if best is not None:
                ordered = self._order_quad(best, np)
                if self._whiteboard_quad is None:
                    self._whiteboard_quad = ordered
                else:
                    self._whiteboard_quad = self._whiteboard_quad * 0.72 + ordered * 0.28
        if self._whiteboard_quad is None:
            return image
        destination = np.array(
            [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(self._whiteboard_quad.astype(np.float32), destination)
        warped = cv2.warpPerspective(image, matrix, (width, height))
        lab = cv2.cvtColor(warped, cv2.COLOR_RGB2LAB)
        light, a, b = cv2.split(lab)
        light = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(light)
        return cv2.cvtColor(cv2.merge((light, a, b)), cv2.COLOR_LAB2RGB)

    @staticmethod
    def _point_in_rect(x: float, y: float, rect: Rect) -> bool:
        left, top, width, height = rect
        return left <= x <= left + width and top <= y <= top + height

    def _tracking_target(
        self, faces: list[list[tuple[float, float]]], settings: EffectSettings
    ) -> TrackingTarget | None:
        boxes: list[tuple[float, float, float, float]] = []
        for face in faces:
            if not face:
                continue
            xs = [point[0] for point in face]
            ys = [point[1] for point in face]
            left, right = max(0.0, min(xs)), min(1.0, max(xs))
            top, bottom = max(0.0, min(ys)), min(1.0, max(ys))
            center_x, center_y = (left + right) / 2, (top + bottom) / 2
            if self._point_in_rect(center_x, center_y, settings.tracking_area):
                boxes.append((left, top, right, bottom))
        if not boxes:
            return None
        if settings.tracking_mode == "single":
            boxes = [max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))]
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        paused = any(
            self._point_in_rect(center_x, center_y, region) for region in settings.pause_areas
        )
        return TrackingTarget(
            center_x=center_x,
            center_y=center_y,
            size=max(right - left, bottom - top),
            face_count=len(boxes),
            paused=paused,
        )

    def _notify_tracking(
        self, faces: list[list[tuple[float, float]]], settings: EffectSettings
    ) -> None:
        now = time.monotonic()
        if now - self._last_tracking_notice < 0.24:
            return
        self._last_tracking_notice = now
        with self._lock:
            callback = self._tracking_callback
        if callback:
            callback(self._tracking_target(faces, settings))

    @staticmethod
    def _orient(image: Any, orientation: str, cv2: Any, np: Any) -> Any:
        if orientation == "vertical_flip":
            return np.ascontiguousarray(np.flipud(image))
        if orientation == "rotate_right":
            return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        if orientation == "rotate_left":
            return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if orientation == "rotate_180":
            return cv2.rotate(image, cv2.ROTATE_180)
        return image

    def process(
        self, width: int, height: int, stride: int, data: bytes, pts: int
    ) -> tuple[int, int, int, bytes, int]:
        settings = self.settings
        if (
            settings.mode == "none"
            and settings.orientation == "identity"
            and settings.tracking_mode == "off"
        ):
            return width, height, stride, data, pts
        cv2, _mp, np = self._load_runtime()
        rows = np.frombuffer(data, dtype=np.uint8).reshape(height, stride)
        image = np.ascontiguousarray(rows[:, : width * 3].reshape(height, width, 3))
        self._frame_number += 1

        faces: list[list[tuple[float, float]]] = []
        if settings.tracking_mode != "off" or settings.mode == "makeup":
            faces = self._detect_faces(image)
        if settings.tracking_mode != "off":
            self._notify_tracking(faces, settings)

        if settings.mode in {"background_blur", "bokeh", "background_replace"}:
            image = self._apply_background_effect(image, settings)
        elif settings.mode == "green_screen":
            image = self._apply_green_screen(image, settings)
        elif settings.mode == "beauty":
            image = self._apply_beauty(image, settings)
        elif settings.mode == "makeup":
            image = self._apply_makeup(image, settings, faces)
        elif settings.mode == "smart_whiteboard":
            image = self._apply_smart_whiteboard(image)

        image = np.ascontiguousarray(self._orient(image, settings.orientation, cv2, np))
        output_height, output_width = image.shape[:2]
        output_stride = output_width * 3
        return output_width, output_height, output_stride, image.tobytes(), pts

    def close(self) -> None:
        for runtime in (self._segmenter, self._landmarker):
            if runtime is not None:
                try:
                    runtime.close()
                except Exception:
                    pass
        self._segmenter = None
        self._landmarker = None
