"""YouTube upload helpers for Colab and local tests (no FastAPI dependency)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

PATH_RE = re.compile(r"(?:^|/)(\d{4})/(\d{2})-([^/]+)/(\d{2})-(\d{2})$")
FILENAME_TS_RE = re.compile(r"(\d{8})[_-](\d{6})")
YOUTUBE_TITLE_MAX = 100
DESCRIPTION_MAX = 5000
MIN_VIDEO_BYTES = 50 * 1024 * 1024

PLAYLIST_EBD = "ebd"
PLAYLIST_CULT_MANHA = "culto_manha"
PLAYLIST_CULT_NOITE = "culto_noite"

TITLE_TYPE_EBD = "EBD"
TITLE_TYPE_CULTO = "Culto"


def parse_path_date(drive_path: str) -> date | None:
    match = PATH_RE.search(drive_path.replace("\\", "/"))
    if not match:
        return None
    year_s, month_folder_s, _month_name, day_s, month_day_s = match.groups()
    try:
        return date(int(year_s), int(month_day_s), int(day_s))
    except ValueError:
        try:
            return date(int(year_s), int(month_folder_s), int(day_s))
        except ValueError:
            return None


def time_from_filename(name: str) -> time | None:
    match = FILENAME_TS_RE.search(name)
    if not match:
        return None
    ymd, hms = match.groups()
    try:
        datetime.strptime(f"{ymd}{hms}", "%Y%m%d%H%M%S")
        return time(int(hms[0:2]), int(hms[2:4]), int(hms[4:6]))
    except ValueError:
        return None


def time_from_iso(iso: str) -> time | None:
    if not iso:
        return None
    try:
        normalized = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).time().replace(tzinfo=None)
    except ValueError:
        return None


def resolve_event_time(video_filename: str, created_time: str | None = None) -> time | None:
    from_name = time_from_filename(video_filename)
    if from_name is not None:
        return from_name
    return time_from_iso(created_time or "")


def classify_playlist(event_time: time | None) -> str:
    if event_time is None:
        return PLAYLIST_EBD
    if event_time < time(10, 0):
        return PLAYLIST_EBD
    if event_time < time(12, 0):
        return PLAYLIST_CULT_MANHA
    return PLAYLIST_CULT_NOITE


def title_type_for_playlist(playlist: str) -> str:
    return TITLE_TYPE_EBD if playlist == PLAYLIST_EBD else TITLE_TYPE_CULTO


def format_date_br(d: date | None) -> str:
    if not d:
        return "—"
    return d.strftime("%d/%m/%Y")


def build_youtube_title(study_title: str, event_date: date | None, playlist: str) -> str:
    kind = title_type_for_playlist(playlist)
    theme = (study_title or "").strip() or "Sem título"
    base = f"IBQB - {kind} - {format_date_br(event_date)} - {theme}"
    return truncate_youtube_title(base)


def truncate_youtube_title(title: str, limit: int = YOUTUBE_TITLE_MAX) -> str:
    title = " ".join(title.split())
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def study_title_from_summary(summary: dict | None) -> str:
    if not summary:
        return ""
    return str(summary.get("titulo") or "").strip()


def extract_study_title_from_transcript(text: str) -> str:
    """Heurística leve quando não há resumo JSON."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    patterns = (
        r"^(?:tema|título|titulo|assunto|lição|licao)\s*[:\-]\s*(.+)$",
        r"^(?:texto[- ]base|texto base)\s*[:\-]\s*(.+)$",
    )
    for line in lines[:80]:
        for pat in patterns:
            match = re.match(pat, line, flags=re.I)
            if match:
                return match.group(1).strip()[:200]
    return ""


def render_description(
    *,
    playlist: str,
    event_date: date | None,
    speaker_name: str = "",
    summary: dict | None = None,
) -> str:
    kind = title_type_for_playlist(playlist)
    header = [
        "Igreja Batista Quadrangular de Brasília (IBQB)",
        f"Tipo: {kind}",
        f"Data: {format_date_br(event_date)}",
    ]
    if speaker_name.strip():
        header.append(f"Palestrante: {speaker_name.strip()}")
    parts = ["\n".join(header), ""]
    if not summary:
        body = "\n".join(parts).strip()
        return body[:DESCRIPTION_MAX]

    if summary.get("texto_base"):
        parts += ["Texto-base", str(summary["texto_base"]).strip(), ""]
    for label, key in (
        ("Pontos", "pontos"),
        ("Aplicações", "aplicacoes"),
        ("Conclusão", "conclusao"),
    ):
        items = [str(x).strip() for x in (summary.get(key) or []) if str(x).strip()]
        if items:
            parts.append(label)
            parts.extend(f"• {item}" for item in items)
            parts.append("")
    text = "\n".join(parts).strip()
    return text[:DESCRIPTION_MAX]


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def empty_state() -> dict[str, Any]:
    return {"playlists": {}, "uploads": {}}


def load_state(raw: str | dict | None) -> dict[str, Any]:
    if raw is None:
        return empty_state()
    if isinstance(raw, dict):
        data = raw
    else:
        data = json.loads(raw)
    if "uploads" not in data:
        data["uploads"] = {}
    if "playlists" not in data:
        data["playlists"] = {}
    return data


def record_upload(
    state: dict[str, Any],
    *,
    drive_file_id: str,
    youtube_id: str,
    title: str,
    drive_path: str,
    playlist: str,
    uploaded_at: str | None = None,
) -> dict[str, Any]:
    when = uploaded_at or datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    state.setdefault("uploads", {})[drive_file_id] = {
        "youtube_id": youtube_id,
        "youtube_url": youtube_watch_url(youtube_id),
        "title": title,
        "drive_path": drive_path,
        "playlist": playlist,
        "uploaded_at": when,
    }
    return state


def already_uploaded(state: dict[str, Any], drive_file_id: str) -> bool:
    entry = (state.get("uploads") or {}).get(drive_file_id) or {}
    return bool(entry.get("youtube_id"))


def catalog_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = catalog.get("items") or catalog.get("aulas") or []
    return {str(item["drive_file_id"]): item for item in items if item.get("drive_file_id")}


def merge_video_row(
    video: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Monta linha de preview/upload a partir do MP4 listado no Drive."""
    file_id = str(video.get("id") or video.get("drive_file_id") or "")
    cat = catalog_by_id.get(file_id, {})
    summary = cat.get("summary") or cat.get("resumo")
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = None
    study = study_title_from_summary(summary if isinstance(summary, dict) else None)
    if not study and cat.get("titulo"):
        study = str(cat["titulo"]).strip()
    event_date = video.get("lecture_date")
    if isinstance(event_date, str):
        try:
            event_date = date.fromisoformat(event_date)
        except ValueError:
            event_date = parse_path_date(str(video.get("path") or ""))
    elif not isinstance(event_date, date):
        event_date = parse_path_date(str(video.get("path") or cat.get("drive_path") or ""))
    event_time = resolve_event_time(
        str(video.get("name") or video.get("video_filename") or ""),
        str(video.get("createdTime") or video.get("created_time") or ""),
    )
    playlist = classify_playlist(event_time)
    youtube_title = build_youtube_title(study, event_date, playlist) if study else ""
    return {
        "drive_file_id": file_id,
        "drive_folder_id": str(video.get("folder_id") or cat.get("drive_folder_id") or ""),
        "drive_path": str(video.get("path") or cat.get("drive_path") or ""),
        "video_filename": str(video.get("name") or cat.get("video_filename") or ""),
        "size": int(video.get("size") or 0),
        "study_title": study,
        "youtube_title": youtube_title,
        "playlist": playlist,
        "event_time": event_time.isoformat(timespec="minutes") if event_time else "",
        "lecture_date": event_date.isoformat() if event_date else "",
        "speaker_name": str(cat.get("speaker_name") or ""),
        "summary": summary if isinstance(summary, dict) else None,
        "description": render_description(
            playlist=playlist,
            event_date=event_date,
            speaker_name=str(cat.get("speaker_name") or ""),
            summary=summary if isinstance(summary, dict) else None,
        ),
        "skip_reason": _skip_reason(video, study, state, file_id),
        "already_on_youtube": already_uploaded(state, file_id),
    }


def _skip_reason(video: dict[str, Any], study: str, state: dict[str, Any], file_id: str) -> str:
    if already_uploaded(state, file_id):
        return "já enviado"
    size = int(video.get("size") or 0)
    if size and size < MIN_VIDEO_BYTES:
        return "arquivo pequeno (<50 MiB)"
    if not study:
        return "sem título do estudo"
    path = str(video.get("path") or "")
    if path and not PATH_RE.search(path.replace("\\", "/")):
        return "pasta fora do padrão"
    return ""


def build_catalog_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "items": items,
    }


def read_catalog_path(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
