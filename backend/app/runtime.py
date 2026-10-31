from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock

from backend.app.config import settings

_runtime_lock = Lock()
_path = settings.data_dir / "runtime.json"

cancel_event = Event()
pause_event = Event()

_pipeline = {
    "running": False,
    "current_lecture_id": None,
    "current_filename": None,
    "stage": None,
    "message": "",
    "progress": None,
    "auto": False,
    "pause_requested": False,
    "prefetch_filename": None,
    "prefetch_progress": None,
}


def _load() -> dict:
    if not _path.exists():
        return {}
    return json.loads(_path.read_text(encoding="utf-8"))


def _save(data: dict) -> None:
    _path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_ai_provider() -> str:
    return _load().get("ai_provider") or settings.ai_provider


def get_folder_id() -> str:
    return _load().get("google_drive_folder_id") or settings.google_drive_folder_id


def get_youtube_upload_folder_id() -> str:
    return str(_load().get("youtube_upload_folder_id") or "")


def get_youtube_upload_path() -> str:
    return str(_load().get("youtube_upload_path") or "").replace("\\", "/").strip("/")


def get_drive_video_count() -> int | None:
    raw = _load().get("drive_video_count")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def get_drive_index() -> dict:
    raw = _load().get("drive_index")
    return dict(raw) if isinstance(raw, dict) else {}


def set_drive_index(index: dict) -> None:
    with _runtime_lock:
        data = _load()
        data["drive_index"] = index
        _save(data)


def update_runtime(**kwargs: str) -> None:
    with _runtime_lock:
        data = _load()
        data.update({k: v for k, v in kwargs.items() if v is not None})
        _save(data)


def pipeline_snapshot() -> dict:
    return dict(_pipeline)


def set_pipeline(**kwargs) -> None:
    _pipeline.update(kwargs)
