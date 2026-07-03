"""Media demux seam for video ingestion (TECHNICAL_DESIGN §6.1).

A video carries two text signals: spoken narration and on-screen text. A ``MediaDemuxer``
splits a video file into the pieces that extract those signals — the **audio track** (fed to
the ``Transcriber`` seam for ASR) and a bounded set of **keyframes** (fed to vision-OCR). The
default implementation shells out to the ``ffmpeg``/``ffprobe`` binaries; those are system
tools (not a Python dep), so there is nothing to install from PyPI — only the binaries need to
be on ``PATH`` in dev/prod. ``FakeDemuxer`` gives deterministic pieces offline.

The seam mirrors the ``Transcriber`` seam: the video adapter depends on the ``MediaDemuxer``
protocol, never on ffmpeg directly, and the worker builds the concrete one from settings.
"""

import asyncio
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.config import Settings


@dataclass(frozen=True)
class Keyframe:
    """A sampled video frame: its timestamp (seconds into the video) and encoded image bytes."""

    time: float
    image: bytes
    media_type: str


@runtime_checkable
class MediaDemuxer(Protocol):
    """Splits a video into an audio track (for ASR) and sampled keyframes (for OCR)."""

    async def extract_audio(self, path: Path, dest: Path) -> bool:
        """Demux the audio track of ``path`` to ``dest``. Returns False if there is none."""
        ...

    async def extract_keyframes(self, path: Path) -> list[Keyframe]:
        """Sample time-stamped keyframes from the video at ``path`` (bounded in count)."""
        ...


class FakeDemuxer:
    """Deterministic demuxer for tests — presets the audio track and keyframes, ignores the file."""

    def __init__(self, *, has_audio: bool = True, keyframes: Sequence[Keyframe] = ()) -> None:
        self._has_audio = has_audio
        self._keyframes = list(keyframes)

    async def extract_audio(self, path: Path, dest: Path) -> bool:
        if self._has_audio:
            dest.write_bytes(b"fake-demuxed-audio")  # a real path for the transcriber to open
        return self._has_audio

    async def extract_keyframes(self, path: Path) -> list[Keyframe]:
        return list(self._keyframes)


class FfmpegDemuxer:
    """Demuxer backed by the ffmpeg/ffprobe binaries (shelled out; bounded memory).

    Audio is streamed to a 16 kHz mono WAV (what whisper wants). Keyframes are sampled at
    ``max_keyframes`` evenly-spaced timestamps so even a long video yields a small, fixed set.
    """

    def __init__(
        self,
        *,
        ffmpeg_bin: str = "ffmpeg",
        ffprobe_bin: str = "ffprobe",
        max_keyframes: int = 20,
    ) -> None:
        self._ffmpeg = ffmpeg_bin
        self._ffprobe = ffprobe_bin
        self._max_keyframes = max_keyframes

    async def extract_audio(self, path: Path, dest: Path) -> bool:  # pragma: no cover - heavy
        code, _out, _err = await self._run(
            self._ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
            "-f", "wav", str(dest),
        )  # fmt: skip
        # ffmpeg exits non-zero when the input has no audio stream — treat that as "no audio".
        return code == 0 and dest.exists() and dest.stat().st_size > 0

    async def extract_keyframes(self, path: Path) -> list[Keyframe]:  # pragma: no cover - heavy
        duration = await self._probe_duration(path)
        if duration <= 0 or self._max_keyframes <= 0:
            return []
        n = self._max_keyframes
        times = [duration * (i + 0.5) / n for i in range(n)]  # evenly spaced, avoiding the edges
        frames: list[Keyframe] = []
        for t in times:
            image = await self._grab_frame(path, t)
            if image:
                frames.append(Keyframe(time=round(t, 2), image=image, media_type="image/png"))
        return frames

    async def _probe_duration(self, path: Path) -> float:  # pragma: no cover - heavy
        code, out, _err = await self._run(
            self._ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        )  # fmt: skip
        try:
            return float(out.strip()) if code == 0 else 0.0
        except ValueError:
            return 0.0

    async def _grab_frame(self, path: Path, at: float) -> bytes:  # pragma: no cover - heavy
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        frame_path = Path(tmp)
        try:
            code, _out, _err = await self._run(
                self._ffmpeg, "-y", "-ss", f"{at:.3f}", "-i", str(path),
                "-frames:v", "1", str(frame_path),
            )  # fmt: skip
            return frame_path.read_bytes() if code == 0 and frame_path.exists() else b""
        finally:
            frame_path.unlink(missing_ok=True)

    @staticmethod
    async def _run(*args: str) -> tuple[int, str, str]:  # pragma: no cover - heavy
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def build_demuxer(settings: Settings) -> MediaDemuxer:
    """Construct the configured demuxer (ffmpeg is only invoked on first extract, so this is cheap)."""
    return FfmpegDemuxer(
        ffmpeg_bin=settings.ffmpeg_bin,
        ffprobe_bin=settings.ffprobe_bin,
        max_keyframes=settings.video_max_keyframes,
    )
