import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from link_studio.effects import EffectProcessor, whiteboard_min_area


class _FakeConfidenceMask:
    def __init__(self, shape):
        self._mask = np.ones((*shape, 1), dtype=np.float32)

    def numpy_view(self):
        return self._mask


class _FakeSegmenter:
    def segment_for_video(self, image, _timestamp):
        height, width = image.data.shape[:2]
        return type("Result", (), {"confidence_masks": [_FakeConfidenceMask((height, width))]})()


class _FakeMediaPipe:
    class ImageFormat:
        SRGB = 1

    class Image:
        def __init__(self, image_format, data):
            self.image_format = image_format
            self.data = data


class EffectProcessorTests(unittest.TestCase):
    def test_inactive_processor_is_a_byte_exact_passthrough(self):
        processor = EffectProcessor()
        frame = bytes(range(18))
        self.assertEqual(processor.process(3, 2, 9, frame, 123), (3, 2, 9, frame, 123))

    def test_settings_are_validated_before_the_runtime_is_loaded(self):
        processor = EffectProcessor()
        settings = processor.update(
            mode="background_blur",
            intensity=80,
            orientation="rotate_right",
            tracking_mode="group",
        )
        self.assertEqual(settings.intensity, 80)
        self.assertTrue(processor.active)
        with self.assertRaisesRegex(ValueError, "unsupported effect"):
            processor.update(mode="cloud_magic")
        with self.assertRaisesRegex(ValueError, "0..100"):
            processor.update(intensity=101)

    def test_tracking_regions_support_single_group_and_pause_areas(self):
        processor = EffectProcessor()
        face_left = [(0.1, 0.1), (0.3, 0.35)]
        face_right = [(0.6, 0.2), (0.9, 0.7)]
        single = processor.update(
            tracking_mode="single",
            tracking_area=(0.0, 0.0, 1.0, 1.0),
            pause_areas=((0.7, 0.3, 0.2, 0.2),),
        )
        target = processor._tracking_target([face_left, face_right], single)
        self.assertIsNotNone(target)
        self.assertEqual(target.face_count, 1)
        self.assertTrue(target.paused)

        group = processor.update(tracking_mode="group", pause_areas=())
        target = processor._tracking_target([face_left, face_right], group)
        self.assertEqual(target.face_count, 2)
        self.assertAlmostEqual(target.center_x, 0.5)

    def test_segmentation_reset_is_safe_during_processing(self):
        processor = EffectProcessor()
        processor._runtime = (cv2, _FakeMediaPipe, np)
        processor._segmenter = _FakeSegmenter()
        image = np.zeros((24, 32, 3), dtype=np.uint8)
        errors = []

        def segment():
            for _index in range(100):
                try:
                    processor._segment(image)
                except Exception as exc:
                    errors.append(exc)

        def reset():
            for _index in range(100):
                processor.reset_analysis()

        threads = [threading.Thread(target=segment), threading.Thread(target=reset)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])

    def test_background_cache_invalidates_when_file_changes(self):
        processor = EffectProcessor()
        processor._runtime = (cv2, None, np)
        image = np.zeros((8, 8, 3), dtype=np.uint8)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "background.png"
            cv2.imwrite(str(path), np.full((8, 8, 3), (0, 0, 255), dtype=np.uint8))
            settings = processor.update(background_image=str(path))
            first = processor._background(image, settings)
            first_mtime = path.stat().st_mtime_ns

            cv2.imwrite(str(path), np.full((8, 8, 3), (0, 255, 0), dtype=np.uint8))
            os.utime(path, ns=(first_mtime + 1_000_000, first_mtime + 1_000_000))
            second = processor._background(image, settings)

        self.assertTrue(np.all(first == (255, 0, 0)))
        self.assertTrue(np.all(second == (0, 255, 0)))

    def test_whiteboard_threshold_counts_pixels_not_color_channels(self):
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        self.assertAlmostEqual(whiteboard_min_area(image), 700)

    def test_every_effect_image_path_produces_a_complete_rgb_frame(self):
        width, height = 96, 72
        image = np.zeros((height, width, 3), dtype=np.uint8)
        image[10:62, 12:84] = (245, 245, 245)
        data = image.tobytes()

        for mode in EffectProcessor.MODES - {"none"}:
            with self.subTest(mode=mode):
                processor = EffectProcessor()
                processor._runtime = (cv2, None, np)
                processor._segment = lambda frame: np.ones((*frame.shape[:2], 1), np.float32)
                processor._detect_faces = lambda _frame: []
                processor.update(mode=mode, background_color="#123456")

                result = processor.process(width, height, width * 3, data, 123)

                self.assertEqual(result[:3], (width, height, width * 3))
                self.assertEqual(len(result[3]), len(data))
                self.assertEqual(result[4], 123)

    def test_active_effects_always_receive_a_writable_contiguous_frame(self):
        processor = EffectProcessor()
        processor._runtime = (cv2, None, np)
        processor.update(mode="green_screen")
        observed = []

        def apply(image, _settings):
            observed.append((image.flags.writeable, image.flags.c_contiguous))
            image[0, 0] = (1, 2, 3)
            return image

        processor._apply_green_screen = apply
        result = processor.process(2, 2, 6, bytes(12), 5)

        self.assertEqual(observed, [(True, True)])
        self.assertEqual(result[3][:3], b"\x01\x02\x03")


if __name__ == "__main__":
    unittest.main()
