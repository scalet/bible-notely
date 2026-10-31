from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy.orm import Session

from backend.app.models import Lecture, Speaker
from colab.youtube_lib import (
    build_catalog_payload,
    build_youtube_title,
    classify_playlist,
    load_state,
    parse_path_date,
    resolve_event_time,
    study_title_from_summary,
    title_type_for_playlist,
    youtube_watch_url,
)

log = logging.getLogger("bible_notely.youtube")

YOUTUBE_META_FOLDER = "auto-ebd-youtube"
CATALOG_NAME = "catalog.json"
STATE_NAME = "state.json"


class YouTubeError(RuntimeError):
    pass


def _drive():
    from backend.app.services import drive

    return drive


def _find_child_folder(parent_id: str, name: str) -> str | None:
    drive = _drive()
    service = drive._service()
    safe = name.replace("'", "\\'")
    result = drive._execute(
        f"busca pasta {name}",
        service.files().list(
            q=f"name = '{safe}' and '{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ),
    )
    files = result.get("files") or []
    return files[0]["id"] if files else None


def ensure_meta_folder() -> str:
    drive = _drive()
    root = drive.get_folder_id()
    if not root:
        raise YouTubeError("GOOGLE_DRIVE_FOLDER_ID não configurado")
    existing = _find_child_folder(root, YOUTUBE_META_FOLDER)
    if existing:
        return existing
    service = drive._service()
    created = drive._execute(
        f"criação da pasta {YOUTUBE_META_FOLDER}",
        service.files().create(
            body={"name": YOUTUBE_META_FOLDER, "mimeType": "application/vnd.google-apps.folder", "parents": [root]},
            fields="id",
            supportsAllDrives=True,
        ),
    )
    folder_id = created["id"]
    log.info("pasta %s criada no Drive: %s", YOUTUBE_META_FOLDER, folder_id)
    return folder_id


def _download_json(folder_id: str, filename: str) -> dict | None:
    drive = _drive()
    dest = Path(NamedTemporaryFile(delete=False, suffix=".json").name)
    try:
        if not drive.download_named(folder_id, filename, dest):
            return None
        return json.loads(dest.read_text(encoding="utf-8"))
    finally:
        dest.unlink(missing_ok=True)


def _upload_json(folder_id: str, filename: str, payload: dict) -> str:
    drive = _drive()
    with NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        path = Path(fh.name)
    try:
        return drive.upload_file(path, folder_id, "application/json")
    finally:
        path.unlink(missing_ok=True)


def catalog_item_from_lecture(lecture: Lecture, speaker: Speaker | None, summary: dict) -> dict:
    event_date = lecture.lecture_date or parse_path_date(lecture.drive_path)
    event_time = resolve_event_time(lecture.video_filename)
    playlist = classify_playlist(event_time)
    study = study_title_from_summary(summary)
    return {
        "drive_file_id": lecture.drive_file_id,
        "drive_folder_id": lecture.drive_folder_id,
        "drive_path": lecture.drive_path,
        "video_filename": lecture.video_filename,
        "lecture_date": event_date.isoformat() if event_date else "",
        "playlist_hint": playlist,
        "title_type": title_type_for_playlist(playlist),
        "titulo": study,
        "speaker_name": speaker.name if speaker else "",
        "summary": summary,
    }


def export_catalog(db: Session) -> tuple[int, str]:
    items: list[dict] = []
    rows = db.query(Lecture).filter(Lecture.summarized.is_(True)).order_by(Lecture.id).all()
    for lecture in rows:
        if not lecture.summary_text:
            continue
        try:
            summary = json.loads(lecture.summary_text)
        except (TypeError, ValueError):
            log.warning("aula %s com summary_text inválido; ignorada no catálogo", lecture.id)
            continue
        speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
        items.append(catalog_item_from_lecture(lecture, speaker, summary))
    payload = build_catalog_payload(items)
    folder_id = ensure_meta_folder()
    file_id = _upload_json(folder_id, CATALOG_NAME, payload)
    log.info("catálogo YouTube enviado (%s itens) file_id=%s", len(items), file_id)
    return len(items), folder_id


def apply_upload_entry(lecture: Lecture, entry: dict) -> None:
    youtube_id = str(entry.get("youtube_id") or "").strip()
    if not youtube_id:
        return
    title = str(entry.get("title") or "").strip()
    if not title:
        event_date = lecture.lecture_date or parse_path_date(lecture.drive_path)
        playlist = str(entry.get("playlist") or classify_playlist(resolve_event_time(lecture.video_filename)))
        study = ""
        if lecture.summary_text:
            try:
                study = study_title_from_summary(json.loads(lecture.summary_text))
            except (TypeError, ValueError):
                study = ""
        title = build_youtube_title(study or "Sem título", event_date, playlist)
    lecture.youtube_id = youtube_id
    lecture.youtube_url = str(entry.get("youtube_url") or youtube_watch_url(youtube_id))
    lecture.youtube_title = title
    lecture.youtube_uploaded_at = _parse_uploaded_at(str(entry.get("uploaded_at") or ""))


def _parse_uploaded_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).replace(tzinfo=None)
    except ValueError:
        return None


def sync_from_drive_state(db: Session) -> tuple[int, int, int]:
    folder_id = ensure_meta_folder()
    state_raw = _download_json(folder_id, STATE_NAME)
    if not state_raw:
        raise YouTubeError(f"{STATE_NAME} não encontrado em {YOUTUBE_META_FOLDER}; rode o Colab primeiro")
    state = load_state(state_raw)
    uploads = state.get("uploads") or {}
    updated = missing = 0
    for drive_file_id, entry in uploads.items():
        youtube_id = str(entry.get("youtube_id") or "").strip()
        if not youtube_id:
            continue
        lecture = db.query(Lecture).filter(Lecture.drive_file_id == drive_file_id).one_or_none()
        if not lecture:
            missing += 1
            continue
        apply_upload_entry(lecture, entry)
        updated += 1
    db.commit()
    log.info("youtube-sync: %s atualizadas, %s sem aula no banco", updated, missing)
    return updated, missing, len(uploads)
