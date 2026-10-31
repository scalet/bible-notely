from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, Speaker
from backend.app.runtime import get_youtube_upload_path
from colab.youtube_lib import (
    PLAYLIST_CULT_MANHA,
    PLAYLIST_CULT_NOITE,
    PLAYLIST_EBD,
    build_catalog_payload,
    build_youtube_title,
    classify_playlist,
    load_state,
    parse_path_date,
    record_upload,
    render_description,
    resolve_event_time,
    study_title_from_summary,
    title_type_for_playlist,
    youtube_watch_url,
)

log = logging.getLogger("bible_notely.youtube")

YOUTUBE_META_FOLDER = "auto-ebd-youtube"
CATALOG_NAME = "catalog.json"
STATE_NAME = "state.json"
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
PLAYLIST_TITLES = {
    PLAYLIST_EBD: "IBQB — EBD",
    PLAYLIST_CULT_MANHA: "IBQB — Culto da manhã",
    PLAYLIST_CULT_NOITE: "IBQB — Culto da noite",
}


class YouTubeError(RuntimeError):
    pass


def lecture_in_upload_scope(lecture: Lecture) -> bool:
    prefix = get_youtube_upload_path()
    if not prefix:
        return True
    path = (lecture.drive_path or "").replace("\\", "/").strip("/")
    return path == prefix or path.startswith(f"{prefix}/")


def youtube_connected() -> bool:
    return settings.youtube_token_path.exists()


def list_drive_subfolders(parent_id: str) -> list[dict[str, str]]:
    drive = _drive()
    service = drive._service()
    folders: list[dict[str, str]] = []
    page_token = None
    while True:
        result = drive._execute(
            f"listagem de pastas em {parent_id}",
            service.files().list(
                q=f"'{parent_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="nextPageToken, files(id, name)",
                pageSize=200,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                orderBy="name",
            ),
        )
        for item in result.get("files") or []:
            folders.append({"id": str(item["id"]), "name": str(item["name"])})
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return folders


def join_drive_path(parent_path: str, name: str) -> str:
    parent = parent_path.replace("\\", "/").strip("/")
    if not parent:
        return name.strip("/")
    return f"{parent}/{name.strip('/')}"


def youtube_scope_stats(db: Session) -> dict[str, int]:
    rows = db.query(Lecture).filter(Lecture.summarized.is_(True)).all()
    eligible = pending = uploaded = 0
    for lecture in rows:
        if not lecture_in_upload_scope(lecture):
            continue
        eligible += 1
        if lecture.youtube_id:
            uploaded += 1
        else:
            pending += 1
    return {"eligible": eligible, "pending_youtube": pending, "on_youtube": uploaded}


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
        if not lecture_in_upload_scope(lecture):
            continue
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


def _client_config_installed() -> dict:
    if not settings.google_client_id or not settings.google_client_secret:
        raise YouTubeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET ausentes no .env")
    return {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def load_local_state() -> dict:
    path = settings.youtube_state_path
    if not path.exists():
        return load_state(None)
    return load_state(json.loads(path.read_text(encoding="utf-8")))


def save_local_state(state: dict) -> None:
    settings.youtube_dir.mkdir(parents=True, exist_ok=True)
    settings.youtube_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def youtube_credentials() -> Credentials:
    path = settings.youtube_token_path
    if not path.exists():
        raise YouTubeError(f"YouTube não conectado; rode: cli youtube-auth (token esperado em {path})")
    creds = Credentials.from_authorized_user_file(str(path), YOUTUBE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
    if not creds.valid:
        raise YouTubeError("Token do YouTube inválido; rode cli youtube-auth de novo")
    return creds


def youtube_auth_local() -> Path:
    flow = InstalledAppFlow.from_client_config(_client_config_installed(), scopes=YOUTUBE_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    settings.youtube_dir.mkdir(parents=True, exist_ok=True)
    settings.youtube_token_path.write_text(creds.to_json(), encoding="utf-8")
    settings.youtube_token_path.chmod(0o600)
    log.info("token YouTube salvo em %s", settings.youtube_token_path)
    return settings.youtube_token_path


def _youtube_service():
    return build("youtube", "v3", credentials=youtube_credentials(), cache_discovery=True)


def _ensure_playlists(youtube, state: dict) -> dict:
    state.setdefault("playlists", {})
    for key, title in PLAYLIST_TITLES.items():
        if state["playlists"].get(key):
            continue
        body = {
            "snippet": {"title": title, "description": "Arquivo IBQB (não listado)"},
            "status": {"privacyStatus": "unlisted"},
        }
        created = youtube.playlists().insert(part="snippet,status", body=body).execute()
        state["playlists"][key] = created["id"]
    return state


def _summary_for_lecture(lecture: Lecture) -> dict | None:
    if not lecture.summary_text:
        return None
    try:
        data = json.loads(lecture.summary_text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def upload_row_for_lecture(lecture: Lecture, speaker: Speaker | None, summary: dict) -> dict:
    event_date = lecture.lecture_date or parse_path_date(lecture.drive_path)
    event_time = resolve_event_time(lecture.video_filename)
    playlist = classify_playlist(event_time)
    study = study_title_from_summary(summary)
    youtube_title = build_youtube_title(study, event_date, playlist) if study else ""
    return {
        "drive_file_id": lecture.drive_file_id,
        "drive_path": lecture.drive_path,
        "video_filename": lecture.video_filename,
        "playlist": playlist,
        "study_title": study,
        "youtube_title": youtube_title,
        "description": render_description(
            playlist=playlist,
            event_date=event_date,
            speaker_name=speaker.name if speaker else "",
            summary=summary,
        ),
        "skip_reason": "" if study else "sem título do estudo",
    }


def _upload_video_file(youtube, row: dict, local_path: Path) -> str:
    body = {
        "snippet": {
            "title": row["youtube_title"],
            "description": row["description"],
            "categoryId": "29",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(local_path), chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = request.next_chunk()
    video_id = response["id"]
    return video_id


def _add_to_playlist(youtube, playlist_id: str, video_id: str) -> None:
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def upload_lectures_local(db: Session, *, max_items: int = 1) -> tuple[int, int, int]:
    """Baixa MP4 do Drive, envia ao YouTube e grava URL no SQLite + state local."""
    drive = _drive()
    youtube = _youtube_service()
    state = load_local_state()
    state = _ensure_playlists(youtube, state)
    save_local_state(state)

    pending = (
        db.query(Lecture)
        .filter(Lecture.summarized.is_(True))
        .filter(Lecture.youtube_id == "")
        .order_by(Lecture.lecture_date.desc(), Lecture.id.desc())
        .all()
    )
    pending = [lecture for lecture in pending if lecture_in_upload_scope(lecture)]
    sent = skipped = failed = 0
    settings.youtube_work_dir.mkdir(parents=True, exist_ok=True)

    for lecture in pending:
        if sent >= max_items:
            break
        summary = _summary_for_lecture(lecture)
        if not summary:
            skipped += 1
            continue
        speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
        row = upload_row_for_lecture(lecture, speaker, summary)
        if row["skip_reason"]:
            skipped += 1
            continue
        local_path = settings.youtube_work_dir / lecture.video_filename
        try:
            drive.download_file(
                lecture.drive_file_id,
                local_path,
                expected_size=lecture.video_size or 0,
                expected_md5=lecture.video_md5 or "",
            )
            video_id = _upload_video_file(youtube, row, local_path)
            playlist_id = state["playlists"][row["playlist"]]
            _add_to_playlist(youtube, playlist_id, video_id)
            uploaded_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            entry = {
                "youtube_id": video_id,
                "youtube_url": youtube_watch_url(video_id),
                "title": row["youtube_title"],
                "drive_path": row["drive_path"],
                "playlist": row["playlist"],
                "uploaded_at": uploaded_at,
            }
            apply_upload_entry(lecture, entry)
            record_upload(
                state,
                drive_file_id=lecture.drive_file_id,
                youtube_id=video_id,
                title=row["youtube_title"],
                drive_path=row["drive_path"],
                playlist=row["playlist"],
                uploaded_at=uploaded_at,
            )
            save_local_state(state)
            db.commit()
            sent += 1
            log.info("YouTube: %s → %s", lecture.video_filename, youtube_watch_url(video_id))
        except Exception as exc:
            db.rollback()
            failed += 1
            log.exception("falha ao enviar %s ao YouTube: %s", lecture.video_filename, exc)
            detail = str(exc).lower()
            if "quota" in detail or "403" in detail:
                raise YouTubeError(f"Cota da API esgotada após {sent} envio(s): {exc}") from exc
        finally:
            local_path.unlink(missing_ok=True)

    return sent, skipped, failed


def _apply_state_uploads(db: Session, state: dict) -> tuple[int, int]:
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
    return updated, missing


def sync_from_local_state(db: Session) -> tuple[int, int, int]:
    if not settings.youtube_state_path.exists():
        return 0, 0, 0
    state = load_local_state()
    updated, missing = _apply_state_uploads(db, state)
    db.commit()
    total = len(state.get("uploads") or {})
    log.info("youtube-sync local: %s atualizadas, %s sem aula, %s entradas", updated, missing, total)
    return updated, missing, total


def sync_from_drive_state(db: Session) -> tuple[int, int, int]:
    try:
        folder_id = ensure_meta_folder()
    except YouTubeError as exc:
        log.info("youtube-sync Drive ignorado: %s", exc)
        return 0, 0, 0
    except Exception as exc:
        drive = _drive()
        if isinstance(exc, drive.DriveError):
            log.info("youtube-sync Drive ignorado: %s", exc)
            return 0, 0, 0
        raise
    state_raw = _download_json(folder_id, STATE_NAME)
    if not state_raw:
        return 0, 0, 0
    state = load_state(state_raw)
    updated, missing = _apply_state_uploads(db, state)
    db.commit()
    total = len(state.get("uploads") or {})
    log.info("youtube-sync Drive: %s atualizadas, %s sem aula, %s entradas", updated, missing, total)
    return updated, missing, total


def sync_all(db: Session) -> tuple[int, int, int, int]:
    local_updated, local_missing, local_total = sync_from_local_state(db)
    drive_updated, drive_missing, drive_total = sync_from_drive_state(db)
    return (
        local_updated + drive_updated,
        local_missing + drive_missing,
        local_total + drive_total,
    )
