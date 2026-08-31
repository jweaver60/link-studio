import unittest

from link_studio.audio import AudioSource, _percent, _sample_spec, preferred_audio_source


class AudioTests(unittest.TestCase):
    def test_pulse_values_are_parsed(self):
        self.assertEqual(_percent("42%"), 42)
        self.assertEqual(_sample_spec("s16le 1ch 48000Hz"), (1, 48000))

    def test_insta360_source_is_preferred(self):
        system = AudioSource("system", "Laptop mic", 100, False, 2, 48000, False)
        link = AudioSource("link", "Insta360 Link 2", 80, False, 1, 48000, True)
        self.assertEqual(preferred_audio_source([system, link]), link)
