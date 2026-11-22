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
    theme = clean_study_title(study_title) or "Sem título"
    base = f"IBQB - {kind} - {format_date_br(event_date)} - {theme}"
    return truncate_youtube_title(base)


def truncate_youtube_title(title: str, limit: int = YOUTUBE_TITLE_MAX) -> str:
    title = " ".join(title.split())
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


_TITLE_SPLIT = re.compile(r"\s+[—–\-|]\s+")
_TITLE_META = re.compile(
    r"(?i)(?:\bebd\b|\bculto\b|\bpalestrante\s*:|\b\d{1,2}/\d{1,2}/\d{2,4}\b)"
)


def clean_study_title(text: str) -> str:
    """Tema só desta aula: tira sufixos/prefixos que citam EBD, culto, outra data ou palestrante.

    Ex.: 'Perfeitos em Unidade — EBD e Culto do Dia dos Pais' -> 'Perfeitos em Unidade'.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return ""
    parts = [part.strip(" —–-|") for part in _TITLE_SPLIT.split(raw) if part.strip(" —–-|")]
    if not parts:
        return raw
    kept = [part for part in parts if not _TITLE_META.search(part)]
    return " ".join(kept) if kept else parts[0]


def study_title_from_summary(summary: dict | None) -> str:
    if not summary:
        return ""
    return clean_study_title(str(summary.get("titulo") or ""))


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
        "IBQB",
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
    ):
        items = _as_text_items(summary.get(key))
        if items:
            parts.append(label)
            parts.extend(f"• {item}" for item in items)
            parts.append("")
    for label, key in (
        ("Conclusão", "conclusao"),
        ("Encerramento da EBD", "encerramento"),
    ):
        block = _as_paragraph(summary.get(key))
        if block:
            parts += [label, block, ""]
    text = "\n".join(parts).strip()
    return text[:DESCRIPTION_MAX]


def _as_text_items(value) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_paragraph(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


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


def in_scope_path(drive_path: str, prefix: str) -> bool:
    if not prefix:
        return True
    path = drive_path.replace("\\", "/").strip("/")
    prefix = prefix.replace("\\", "/").strip("/")
    return path == prefix or path.startswith(f"{prefix}/")


def catalog_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = catalog.get("items") or catalog.get("aulas") or []
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        file_id = str(item.get("drive_file_id") or "")
        if file_id:
            index[file_id] = item
        path = str(item.get("drive_path") or "").replace("\\", "/").strip("/")
        name = str(item.get("video_filename") or item.get("name") or "")
        if path and name:
            index[f"{path}/{name}"] = item
    return index


def catalog_file_key(drive_path: str, filename: str) -> str:
    return f"{drive_path.replace('\\', '/').strip('/')}/{filename}"


def list_mounted_mp4s(ebd_root: Path, scope_prefix: str = "") -> list[dict[str, Any]]:
    """Lista MP4s no Drive montado (`/content/drive/MyDrive/...`)."""
    root = Path(ebd_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Pasta do Drive não encontrada: {root}")
    videos: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.mp4")):
        rel = path.relative_to(root)
        folder = str(rel.parent).replace("\\", "/")
        if folder == ".":
            folder = ""
        if not in_scope_path(folder, scope_prefix):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        event_date = parse_path_date(folder)
        videos.append(
            {
                "id": "",
                "name": path.name,
                "folder_id": "",
                "path": folder,
                "size": size,
                "local_path": str(path),
                "lecture_date": event_date.isoformat() if event_date else "",
            }
        )
    return videos


def resolve_local_video(ebd_root: Path, drive_path: str, filename: str) -> Path:
    return Path(ebd_root) / drive_path.replace("\\", "/").strip("/") / filename


def is_youtube_quota_error(exc: BaseException) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    raw = getattr(exc, "content", b"") or b""
    text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
    combined = f"{exc} {text}".lower()
    quota_mark = any(
        token in combined
        for token in ("quotaexceeded", "dailyLimitExceeded".lower(), "quota exceeded", "rateLimitExceeded".lower())
    )
    if status in {403, 429} and quota_mark:
        return True
    return "quota" in combined and ("exceed" in combined or "limit" in combined)


def merge_video_row(
    video: dict[str, Any],
    catalog_by_id: dict[str, dict[str, Any]],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Monta linha de preview/upload a partir do MP4 listado no Drive."""
    path = str(video.get("path") or "").replace("\\", "/").strip("/")
    filename = str(video.get("name") or video.get("video_filename") or "")
    file_id = str(video.get("id") or video.get("drive_file_id") or "")
    cat = catalog_by_id.get(file_id) or catalog_by_id.get(catalog_file_key(path, filename), {})
    if not file_id:
        file_id = str(cat.get("drive_file_id") or catalog_file_key(path, filename))
    summary = cat.get("summary") or cat.get("resumo")
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = None
    study = study_title_from_summary(summary if isinstance(summary, dict) else None)
    if not study and cat.get("titulo"):
        study = str(cat["titulo"]).strip()
    event_date = video.get("lecture_date") or cat.get("lecture_date")
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
        "drive_path": str(video.get("path") or cat.get("drive_path") or path),
        "video_filename": filename or str(cat.get("video_filename") or ""),
        "local_path": str(video.get("local_path") or ""),
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
        "skip_reason": _skip_reason(video, study, state, file_id, local_path=str(video.get("local_path") or "")),
        "already_on_youtube": already_uploaded(state, file_id),
    }


def _skip_reason(
    video: dict[str, Any],
    study: str,
    state: dict[str, Any],
    file_id: str,
    local_path: str = "",
) -> str:
    if already_uploaded(state, file_id):
        return "já enviado"
    if local_path and not Path(local_path).is_file():
        return f"arquivo não encontrado no Drive montado: {local_path}"
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


PLAYLIST_TITLES = {
    PLAYLIST_EBD: "IBQB — EBD",
    PLAYLIST_CULT_MANHA: "IBQB — Culto da manhã",
    PLAYLIST_CULT_NOITE: "IBQB — Culto da noite",
}


def load_colab_credentials(secret_path: Path, token_path: Path, scopes: list[str]):
    """OAuth para o Colab: reusa token no Drive; senão abre URL e pede o código."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        if creds and creds.valid:
            return creds
    if not secret_path.exists():
        raise FileNotFoundError(
            f"client_secret.json não encontrado em {secret_path}. "
            "Coloque o JSON OAuth Desktop nessa pasta do Drive."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret_path), scopes)
    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("1. Abra este link no navegador e autorize a conta do canal YouTube:")
    print(auth_url)
    print("2. O Google tenta abrir localhost:8080. Copie o valor de ?code= da barra de endereço")
    print("   (ou a URL inteira) e cole abaixo.")
    raw = input("Código ou URL: ").strip()
    if "code=" in raw:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(raw)
        raw = (parse_qs(parsed.query).get("code") or [""])[0]
    flow.fetch_token(code=raw)
    creds = flow.credentials
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"Token salvo em {token_path} (persiste no Drive).")
    return creds


def upload_video_file(youtube, local_path: Path, row: dict[str, Any], progress=None) -> str:
    from googleapiclient.http import MediaFileUpload

    if not Path(local_path).is_file():
        raise FileNotFoundError(f"Vídeo não encontrado no Drive montado: {local_path}")
    body = {
        "snippet": {
            "title": row["youtube_title"],
            "description": row.get("description") or "",
            "categoryId": "29",
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last = -1
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct != last:
                last = pct
                print(f"  upload {pct}%", flush=True)
                if progress:
                    progress(pct)
    return str(response["id"])


def add_to_playlist(youtube, playlist_id: str, video_id: str) -> None:
    youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }
        },
    ).execute()


def ensure_playlists(youtube, state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("playlists", {})
    for key, title in PLAYLIST_TITLES.items():
        if state["playlists"].get(key):
            continue
        created = youtube.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": "Arquivo IBQB (não listado)"},
                "status": {"privacyStatus": "unlisted"},
            },
        ).execute()
        state["playlists"][key] = created["id"]
    return state
