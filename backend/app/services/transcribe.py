from __future__ import annotations

import importlib
import io
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from backend.app.config import settings


class TranscribeError(RuntimeError):
    pass


class TranscribeCancelled(TranscribeError):
    pass


def video_duration(video: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        value = float(result.stdout.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def extract_frames(video: Path, dest_dir: Path, stem: str) -> list[Path]:
    """Vários frames (início, ~20% e meio) para o matching. O do meio é o da revisão."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    duration = video_duration(video)
    if duration:
        points = [
            max(1, int(duration * 0.08)),
            max(1, int(duration * 0.2)),
            max(1, int(min(duration - 1, duration * 0.5))),
        ]
    else:
        points = [8, 45, 90]
    unique: list[int] = []
    for point in points:
        if point not in unique:
            unique.append(point)
    frames: list[Path] = []
    errors: list[str] = []
    for index, seconds in enumerate(unique):
        dest = dest_dir / f"{stem}-{index}.jpg"
        try:
            extract_frame(video, dest, at_seconds=seconds)
            frames.append(dest)
        except TranscribeError as exc:
            errors.append(str(exc))
    if not frames:
        raise TranscribeError(errors[-1] if errors else "ffmpeg não gerou frames")
    return frames


def extract_frame(video: Path, dest: Path, at_seconds: int = 90) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(at_seconds),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.exists():
        # tenta o início se o vídeo for curto
        cmd[3] = "8"
        result = subprocess.run(cmd, capture_output=True, text=True)
    if not dest.exists():
        raise TranscribeError(result.stderr[-800:] or "ffmpeg não gerou o frame")
    return dest


def transcribe_video(
    video: Path,
    progress: Callable[[float], None] | None = None,
    cancel: Event | None = None,
) -> str:
    try:
        import torch
        import whisper
    except ImportError as exc:
        raise TranscribeError("openai-whisper não instalado") from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"
    import tqdm
    whisper_transcribe = importlib.import_module("whisper.transcribe")

    class _Bar(tqdm.tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["file"] = io.StringIO()
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            if cancel is not None and cancel.is_set():
                raise TranscribeCancelled("Transcrição cancelada")
            super().update(n)
            if progress and self.total:
                progress(min(self.n / self.total, 1.0))

    model = whisper.load_model(settings.whisper_model, device=device)
    original = whisper_transcribe.tqdm
    whisper_transcribe.tqdm = SimpleNamespace(tqdm=_Bar)
    try:
        result = model.transcribe(
            str(video),
            language=settings.whisper_language,
            fp16=device == "cuda",
            condition_on_previous_text=False,
            verbose=False,
        )
    finally:
        whisper_transcribe.tqdm = original
    lines = [
        f"[{format_timestamp(segment.get('start', 0))}] {segment['text'].strip()}"
        for segment in result.get("segments", [])
        if segment["text"].strip()
    ]
    text = "\n".join(lines)
    if not text:
        raise TranscribeError("Whisper não gerou texto")
    return text


def format_timestamp(seconds: float) -> str:
    total = int(max(seconds, 0))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


_TIMESTAMP_RE = re.compile(r"^\[\d{2,}:\d{2}:\d{2}\]\s*", re.M)


def strip_timestamps(text: str) -> str:
    """Texto corrido, sem as marcas [HH:MM:SS], para a LLM e a leitura de referências."""
    return _TIMESTAMP_RE.sub("", text)
