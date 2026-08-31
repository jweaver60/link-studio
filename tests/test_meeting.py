import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from link_studio.meeting import (
    MeetingResult,
    default_meeting_dir,
    summarize_transcript,
    transcribe_meeting,
)


class MeetingSummaryTests(unittest.TestCase):
    def test_meeting_directory_uses_the_configured_xdg_documents_location(self):
        with patch(
            "link_studio.meeting.GLib.get_user_special_dir", return_value="/media/archive/Documents"
        ):
            self.assertEqual(
                default_meeting_dir(),
                Path("/media/archive/Documents/Link Studio/Meetings"),
            )

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

    def test_transcript_and_summary_are_explicit_utf8(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "audio.wav"
            audio.touch()
            model = root / "model.bin"
            model.touch()
            result = MeetingResult(root, audio, root / "markers.json", 1.0)

            def run(command, **_kwargs):
                prefix = Path(command[command.index("-of") + 1])
                prefix.with_suffix(".txt").write_bytes("Résumé 日本語.".encode())
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with (
                patch("link_studio.meeting._whisper_command", return_value="whisper-cli"),
                patch("link_studio.meeting.subprocess.run", side_effect=run),
            ):
                transcript, summary = transcribe_meeting(result, model_path=model)

            self.assertEqual(transcript.read_text(encoding="utf-8"), "Résumé 日本語.")
            self.assertIn("Résumé 日本語", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
