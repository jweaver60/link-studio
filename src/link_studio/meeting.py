from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)


class MeetingError(RuntimeError):
    pass


def default_meeting_dir() -> Path:
    documents = Path(os.environ.get("XDG_DOCUMENTS_DIR", Path.home() / "Documents"))
    return documents / "Link Studio" / "Meetings"


def default_whisper_model() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "link-studio/models/ggml-base.bin"


@dataclass(frozen=True, slots=True)
class MeetingMarker:
    seconds: float
    kind: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class MeetingResult:
    directory: Path
    audio_path: Path
    markers_path: Path
    duration_seconds: float


def _gst_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class AudioNoteRecorder:
    """Record a local 16 kHz WAV voice note and timestamped markers."""

    def __init__(self, root: Path | None = None, audio_source: str | None = None) -> None:
        self.root = root or default_meeting_dir()
        self.audio_source = audio_source
        self.pipeline: Gst.Pipeline | None = None
        self.directory: Path | None = None
        self.audio_path: Path | None = None
        self.started_at = 0.0
        self.paused_at = 0.0
        self.paused_seconds = 0.0
        self.markers: list[MeetingMarker] = []
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self.pipeline is not None

    @property
    def paused(self) -> bool:
        return self.running and self.paused_at > 0

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.paused_at or time.monotonic()
        return max(0.0, end - self.started_at - self.paused_seconds)

    def start(self) -> Path:
        with self._lock:
            if self.pipeline:
                raise MeetingError("AI recording is already running")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            directory = self.root / f"Meeting-{stamp}"
            directory.mkdir(parents=True, exist_ok=False)
            audio_path = directory / "audio.wav"
            source = "pulsesrc do-timestamp=true"
            if self.audio_source:
                source += f' device="{_gst_quote(self.audio_source)}"'
            description = (
                f"{source} ! queue ! audioconvert ! audioresample "
                "! audio/x-raw,format=S16LE,rate=16000,channels=1 "
                f'! wavenc ! filesink location="{_gst_quote(str(audio_path))}"'
            )
            try:
                pipeline = Gst.parse_launch(description)
            except Exception as exc:
                raise MeetingError(f"Could not create the audio pipeline: {exc}") from exc
            if not isinstance(pipeline, Gst.Pipeline):
                raise MeetingError("GStreamer did not create an audio pipeline")
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                raise MeetingError("The selected microphone could not be opened")
            self.pipeline = pipeline
            self.directory = directory
            self.audio_path = audio_path
            self.started_at = time.monotonic()
            self.paused_at = 0.0
            self.paused_seconds = 0.0
            self.markers = []
            return directory

    def set_paused(self, paused: bool) -> bool:
        with self._lock:
            if not self.pipeline:
                raise MeetingError("AI recording is not running")
            if paused == self.paused:
                return paused
            if paused:
                self.pipeline.set_state(Gst.State.PAUSED)
                self.paused_at = time.monotonic()
            else:
                self.pipeline.set_state(Gst.State.PLAYING)
                self.paused_seconds += time.monotonic() - self.paused_at
                self.paused_at = 0.0
            return paused

    def add_marker(self, kind: str, note: str = "") -> MeetingMarker:
        if kind not in {"highlight", "note", "photo", "screenshot"}:
            raise ValueError(f"unsupported marker type: {kind}")
        with self._lock:
            if not self.pipeline:
                raise MeetingError("Start AI recording before adding a marker")
            marker = MeetingMarker(round(self.elapsed, 3), kind, note.strip()[:500])
            self.markers.append(marker)
            return marker

    def stop(self) -> MeetingResult:
        with self._lock:
            pipeline, self.pipeline = self.pipeline, None
            directory, audio_path = self.directory, self.audio_path
            duration = self.elapsed
            self.started_at = 0.0
            self.paused_at = 0.0
        if not pipeline or not directory or not audio_path:
            raise MeetingError("AI recording is not running")
        pipeline.set_state(Gst.State.PLAYING)
        pipeline.send_event(Gst.Event.new_eos())
        bus = pipeline.get_bus()
        message = bus.timed_pop_filtered(
            5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        pipeline.set_state(Gst.State.NULL)
        if message and message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            raise MeetingError(error.message)
        markers_path = directory / "markers.json"
        markers_path.write_text(
            json.dumps([asdict(marker) for marker in self.markers], indent=2) + "\n"
        )
        metadata = {
            "created": datetime.now().astimezone().isoformat(),
            "duration_seconds": round(duration, 3),
            "audio_source": self.audio_source,
        }
        (directory / "meeting.json").write_text(json.dumps(metadata, indent=2) + "\n")
        return MeetingResult(directory, audio_path, markers_path, duration)


def whisper_available() -> bool:
    return any(shutil.which(name) for name in ("whisper-cli", "whisper-cpp"))


def _whisper_command() -> str:
    command = next(
        (shutil.which(name) for name in ("whisper-cli", "whisper-cpp") if shutil.which(name)),
        None,
    )
    if not command:
        raise MeetingError(
            "Local transcription is not installed. Run link-studio-setup-local-ai first."
        )
    return command


def transcribe_meeting(
    result: MeetingResult,
    language: str = "auto",
    model_path: Path | None = None,
) -> tuple[Path, Path]:
    model = model_path or default_whisper_model()
    if not model.is_file():
        raise MeetingError(
            f"Whisper model is missing at {model}. Run link-studio-setup-local-ai first."
        )
    output_prefix = result.directory / "transcript"
    command = [
        _whisper_command(),
        "-m",
        str(model),
        "-f",
        str(result.audio_path),
        "-otxt",
        "-of",
        str(output_prefix),
        "-l",
        language,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=7200)
    transcript_path = output_prefix.with_suffix(".txt")
    if completed.returncode or not transcript_path.is_file():
        reason = completed.stderr.strip() or completed.stdout.strip() or "unknown Whisper error"
        raise MeetingError(f"Transcription failed: {reason[-800:]}")
    transcript = transcript_path.read_text(errors="replace").strip()
    summary_path = result.directory / "summary.md"
    summary_path.write_text(summarize_transcript(transcript))
    return transcript_path, summary_path


_STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "because",
    "been",
    "before",
    "being",
    "could",
    "does",
    "from",
    "have",
    "into",
    "just",
    "more",
    "most",
    "only",
    "other",
    "should",
    "some",
    "than",
    "that",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "very",
    "what",
    "when",
    "where",
    "which",
    "while",
    "will",
    "with",
    "would",
    "your",
}


def summarize_transcript(transcript: str, sentence_limit: int = 6) -> str:
    """Create a deterministic offline extractive summary and action-item list."""

    clean = re.sub(r"\s+", " ", transcript).strip()
    if not clean:
        return "# Meeting summary\n\nNo speech was transcribed.\n"
    sentences = [item.strip() for item in re.split(r"(?<=[.!?])\s+", clean) if item.strip()]
    words = re.findall(r"[\w'-]{3,}", clean.casefold())
    frequencies = Counter(word for word in words if word not in _STOP_WORDS)

    def score(sentence: str) -> float:
        sentence_words = re.findall(r"[\w'-]{3,}", sentence.casefold())
        if not sentence_words:
            return 0.0
        return sum(frequencies[word] for word in sentence_words) / len(sentence_words) ** 0.6

    selected_indexes = sorted(
        sorted(range(len(sentences)), key=lambda index: score(sentences[index]), reverse=True)[
            :sentence_limit
        ]
    )
    selected = [sentences[index] for index in selected_indexes]
    actions = [
        sentence
        for sentence in sentences
        if re.search(
            r"\b(action|todo|to-do|need(?:s)? to|will|follow up|deadline|assign(?:ed)?|next step)\b",
            sentence,
            re.IGNORECASE,
        )
    ][:10]
    lines = ["# Meeting summary", "", "## Key points", ""]
    lines.extend(f"- {sentence}" for sentence in selected)
    lines.extend(["", "## Action items", ""])
    lines.extend(f"- {sentence}" for sentence in actions)
    if not actions:
        lines.append("- No explicit action items detected.")
    lines.extend(["", "## Transcript", "", transcript.strip(), ""])
    return "\n".join(lines)
