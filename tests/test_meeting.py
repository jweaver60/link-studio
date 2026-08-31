import unittest

from link_studio.meeting import summarize_transcript


class MeetingSummaryTests(unittest.TestCase):
    def test_summary_preserves_key_points_and_action_items(self):
        transcript = (
            "The launch is scheduled for Friday. "
            "Maya will prepare the release notes by Thursday. "
            "The camera pipeline passed every regression test. "
            "We need to confirm packaging on a clean Omarchy installation."
        )
        summary = summarize_transcript(transcript, sentence_limit=3)
        self.assertIn("# Meeting summary", summary)
        self.assertIn("## Action items", summary)
        self.assertIn("Maya will prepare", summary)
        self.assertIn("need to confirm packaging", summary)
        self.assertIn("## Transcript", summary)

    def test_empty_transcript_has_a_clear_result(self):
        self.assertIn("No speech was transcribed", summarize_transcript("   "))


if __name__ == "__main__":
    unittest.main()
