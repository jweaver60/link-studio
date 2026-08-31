import unittest

from link_studio.effects import EffectProcessor


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


if __name__ == "__main__":
    unittest.main()
