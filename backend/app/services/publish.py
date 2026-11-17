"""Passo opcional: sobe o vídeo processado ao Bunny Stream e grava um rascunho no Maná.

Não apaga arquivo no Google Drive. O MP4 original da gravação permanece na pasta do dia.
O mesmo conteúdo (SHA-256) não é enviado de novo: o vínculo fica em data/publish/state.json
e nas colunas da aula.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, Speaker
from backend.app.services.obsidian import _label, collect_references, parse_reference
from backend.app.services.pipeline import lecture_work_dir
from colab.youtube_lib import (
    PLAYLIST_EBD,
    classify_playlist,
    parse_path_date,
    resolve_event_time,
    study_title_from_summary,
)

log = logging.getLogger("bible_notely.publish")

# Vídeo já com overlay de hinos/versículos, se o processamento tiver deixado um arquivo ao lado do MP4.
PROCESSED_MARKERS = (
    "-hinos.mp4",
    "-hymns.mp4",
    "-overlay.mp4",
    "-versiculos.mp4",
    "-processado.mp4",
    "-comprimido.mp4",
)
PROCESSED_TOKENS = ("hino", "hymn", "overlay", "versiculo", "processado", "comprimido")

# https://docs.bunny.net — 4 Finished, 7/8 playable no JIT; 5 Error, 6 UploadFailed.
ENCODED_STATUSES = frozenset({4, 7, 8})
FAILED_STATUSES = frozenset({5, 6})
LESSON_DRAFT = "draft"
TUS_VERSION = "1.0.0"


class PublishError(RuntimeError):
    pass


@dataclass
class PublishResult:
    action: str
    bunny_video_id: str = ""
    lesson_id: str = ""
    detail: str = ""


def file_sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def tus_signature(library_id: str, api_key: str, expiration: int, video_id: str) -> str:
    """SHA256(library_id + api_key + expiration + video_id), como a TUS do Bunny exige."""
    raw = f"{library_id}{api_key}{expiration}{video_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def ffmpeg_720_command(source: Path, dest: Path) -> list[str]:
    """720p H.264 sem aumentar um vídeo que já é mais baixo. A vírgula do min() vai escapada."""
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        r"scale=-2:min(720\,ih)",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def transcode_to_720(
    source: Path,
    dest: Path,
    runner: Callable[..., object] | None = None,
) -> Path:
    """Gera a cópia 720p. Se o destino já é mais novo que a origem, reaproveita (hash estável)."""
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0 and dest.stat().st_mtime >= source.stat().st_mtime:
        log.info("720p reaproveitado: %s", dest.name)
        return dest
    command = ffmpeg_720_command(source, dest)
    run = runner or subprocess.run
    result = run(command, capture_output=True, text=True)
    returncode = getattr(result, "returncode", 1)
    if returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        stderr = getattr(result, "stderr", "") or getattr(result, "stdout", "") or "ffmpeg falhou"
        raise PublishError(f"transcode 720p falhou: {str(stderr)[-500:]}")
    return dest


def find_processed_video(lecture: Lecture) -> Path | None:
    """Prefere o MP4 com hinos/versículos. Sem ele, usa o MP4 da aula se ainda estiver em work/."""
    work = lecture_work_dir(lecture)
    stem = Path(lecture.video_filename or "").stem
    for marker in PROCESSED_MARKERS:
        candidate = work / f"{stem}{marker}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    if work.is_dir():
        marked = [
            path
            for path in sorted(work.glob("*.mp4"))
            if any(token in path.name.lower() for token in PROCESSED_TOKENS) and path.stat().st_size > 0
        ]
        if len(marked) == 1:
            return marked[0]
    original = work / (lecture.video_filename or "")
    if lecture.video_filename and original.is_file() and original.stat().st_size > 0:
        return original
    return None


def lesson_type_for(lecture: Lecture) -> str:
    """ebd antes das 10h; culto da manhã e da noite viram culto."""
    playlist = classify_playlist(resolve_event_time(lecture.video_filename or ""))
    return "ebd" if playlist == PLAYLIST_EBD else "culto"


def lesson_verses(summary: dict) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for ref in collect_references(summary):
        label = _label(ref)
        if label not in seen:
            seen.add(label)
            labels.append(label)
    for raw in summary.get("referencias") or []:
        text = str(raw).strip()
        if text and text not in seen and not parse_reference(text):
            seen.add(text)
            labels.append(text)
    return labels


def lesson_body(summary: dict) -> str:
    parts: list[str] = []
    base = str(summary.get("texto_base") or "").strip()
    if base:
        parts += ["## Texto-base", "", base, ""]
    points = [str(item).strip() for item in (summary.get("pontos") or []) if str(item).strip()]
    if points:
        parts += ["## Pontos principais", ""] + [f"- {item}" for item in points] + [""]
    applications = [str(item).strip() for item in (summary.get("aplicacoes") or []) if str(item).strip()]
    if applications:
        parts += ["## Aplicações", ""] + [f"- {item}" for item in applications] + [""]
    conclusion = str(summary.get("conclusao") or "").strip()
    if conclusion:
        parts += ["## Conclusão", "", conclusion, ""]
    closing = str(summary.get("encerramento") or "").strip()
    if closing:
        parts += ["## Encerramento", "", closing, ""]
    return "\n".join(parts).strip()


def build_lesson_payload(
    lecture: Lecture,
    speaker: Speaker | None,
    summary: dict,
    *,
    bunny_video_id: str,
    event_date=None,
) -> dict:
    """Corpo enviado ao Maná. status é sempre draft; o cliente recusa qualquer outro valor."""
    when = event_date or lecture.lecture_date or parse_path_date(lecture.drive_path or "")
    if when is None:
        raise PublishError("aula sem data")
    title = study_title_from_summary(summary) or "Sem título"
    payload = {
        "external_id": lecture.drive_file_id,
        "title": title,
        "date": when.isoformat(),
        "type": lesson_type_for(lecture),
        "speaker": (speaker.name.strip() if speaker and speaker.name else "") or "Não identificado",
        "verses": lesson_verses(summary),
        "body": lesson_body(summary),
        "bunny_video_id": bunny_video_id,
        "status": LESSON_DRAFT,
    }
    if payload["status"] != LESSON_DRAFT:
        raise PublishError("a aula só pode sair como rascunho")
    if payload["type"] not in {"ebd", "culto"}:
        raise PublishError(f"tipo de aula inválido: {payload['type']}")
    return payload


def lesson_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def lesson_endpoint(base_url: str) -> str:
    root = base_url.strip().rstrip("/")
    if root.endswith("/api"):
        return f"{root}/admin/pipeline/lessons"
    return f"{root}/api/admin/pipeline/lessons"


def empty_state() -> dict:
    return {"uploads": {}}


def load_state() -> dict:
    path = settings.publish_state_path
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("state de publicação ilegível; começando vazio")
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    uploads = data.get("uploads")
    if not isinstance(uploads, dict):
        data["uploads"] = {}
    return data


def save_state(state: dict) -> None:
    path = settings.publish_state_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_uploaded_at(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _summary(lecture: Lecture) -> dict | None:
    if not lecture.summary_text:
        return None
    try:
        data = json.loads(lecture.summary_text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _redact(text: str, secrets: list[str]) -> str:
    cleaned = text or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


def require_bunny() -> None:
    library = settings.bunny_stream_library_id.strip()
    key = settings.bunny_stream_api_key.strip()
    if not library or not key:
        raise PublishError("BUNNY_STREAM_LIBRARY_ID e BUNNY_STREAM_API_KEY são obrigatórios")
    if not library.replace("-", "").replace("_", "").isalnum():
        raise PublishError("BUNNY_STREAM_LIBRARY_ID inválido")


def require_mana() -> None:
    if not settings.mana_publish_enabled:
        return
    if not settings.mana_api_url.strip() or not settings.mana_api_token.strip():
        raise PublishError("MANA_PUBLISH_ENABLED exige MANA_API_URL e MANA_API_TOKEN")


class BunnyStream:
    """Create Video + TUS (https://video.bunnycdn.com/tusupload). A chave vai no header, nunca na URL."""

    def __init__(
        self,
        http: httpx.Client,
        *,
        library_id: str,
        api_key: str,
        api_base: str,
        tus_endpoint: str,
        chunk_size: int,
        limit_720: bool,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.http = http
        self.library_id = library_id.strip()
        self.api_key = api_key.strip()
        self.api_base = api_base.rstrip("/")
        self.tus_endpoint = tus_endpoint
        self.chunk_size = max(1, chunk_size)
        self.limit_720 = limit_720
        self._now = now or (lambda: int(time.time()))

    def _auth(self, video_id: str) -> dict[str, str]:
        expiration = self._now() + 86400
        headers = {
            "AuthorizationSignature": tus_signature(self.library_id, self.api_key, expiration, video_id),
            "AuthorizationExpire": str(expiration),
            "LibraryId": self.library_id,
            "VideoId": video_id,
            "Tus-Resumable": TUS_VERSION,
        }
        if self.limit_720:
            headers["EnabledResolutions"] = "720p"
            headers["EnabledOutputCodecs"] = "x264"
        return headers

    def _fail(self, response: httpx.Response, action: str) -> None:
        if response.status_code < 400:
            return
        detail = _redact(response.text, [self.api_key])
        raise PublishError(f"Bunny falhou ao {action} ({response.status_code}): {detail}")

    def create_video(self, title: str) -> str:
        response = self.http.post(
            f"{self.api_base}/library/{self.library_id}/videos",
            headers={"AccessKey": self.api_key, "Accept": "application/json"},
            json={"title": title},
        )
        self._fail(response, "criar o vídeo")
        try:
            guid = str(response.json().get("guid") or "")
        except ValueError as exc:
            raise PublishError("Bunny não devolveu JSON ao criar o vídeo") from exc
        if not guid:
            raise PublishError("Bunny não devolveu guid")
        return guid

    def get_video(self, video_id: str) -> dict:
        response = self.http.get(
            f"{self.api_base}/library/{self.library_id}/videos/{video_id}",
            headers={"AccessKey": self.api_key, "Accept": "application/json"},
        )
        self._fail(response, "consultar o vídeo")
        try:
            data = response.json()
        except ValueError as exc:
            raise PublishError("Bunny não devolveu JSON ao consultar o vídeo") from exc
        if not isinstance(data, dict):
            raise PublishError("resposta do Bunny sem objeto de vídeo")
        return data

    def upload_file(self, video_id: str, path: Path, *, title: str, resume_url: str = "") -> str:
        size = path.stat().st_size
        location = resume_url
        offset = 0
        if location:
            resumed = self._head_offset(video_id, location)
            if resumed is None:
                location = ""
            else:
                offset = resumed
        if not location:
            location = self._create_upload(video_id, size, title)
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            while offset < size:
                chunk = handle.read(self.chunk_size)
                if not chunk:
                    break
                offset = self._patch(video_id, location, offset, chunk)
        if offset < size:
            raise PublishError(f"upload TUS incompleto ({offset} de {size})")
        return location

    def _create_upload(self, video_id: str, size: int, title: str) -> str:
        metadata = ",".join(
            (
                f"filetype {base64.b64encode(b'video/mp4').decode()}",
                f"title {base64.b64encode(title.encode()).decode()}",
            )
        )
        headers = self._auth(video_id)
        headers["Upload-Length"] = str(size)
        headers["Upload-Metadata"] = metadata
        response = self.http.post(self.tus_endpoint, headers=headers)
        self._fail(response, "abrir o upload TUS")
        if response.status_code not in {201, 204}:
            raise PublishError(f"TUS recusou a abertura ({response.status_code})")
        location = response.headers.get("Location") or response.headers.get("location") or ""
        if not location:
            raise PublishError("TUS não devolveu Location")
        if location.startswith("/"):
            location = f"https://video.bunnycdn.com{location}"
        return location

    def _head_offset(self, video_id: str, location: str) -> int | None:
        response = self.http.head(location, headers=self._auth(video_id))
        if response.status_code == 404:
            return None
        self._fail(response, "retomar o upload")
        raw = response.headers.get("Upload-Offset") or response.headers.get("upload-offset")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _patch(self, video_id: str, location: str, offset: int, chunk: bytes) -> int:
        headers = self._auth(video_id)
        headers["Upload-Offset"] = str(offset)
        headers["Content-Type"] = "application/offset+octet-stream"
        response = self.http.patch(location, headers=headers, content=chunk)
        self._fail(response, "enviar um trecho")
        if response.status_code not in {204, 200}:
            raise PublishError(f"TUS recusou o trecho ({response.status_code})")
        raw = response.headers.get("Upload-Offset") or response.headers.get("upload-offset")
        if raw is None:
            return offset + len(chunk)
        try:
            return int(raw)
        except ValueError:
            return offset + len(chunk)

    def wait_until_encoded(
        self,
        video_id: str,
        *,
        timeout: float,
        interval: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> dict:
        started = clock()
        while True:
            video = self.get_video(video_id)
            try:
                status = int(video.get("status"))
            except (TypeError, ValueError) as exc:
                raise PublishError("Bunny devolveu status inválido") from exc
            if status in ENCODED_STATUSES:
                return video
            if status in FAILED_STATUSES:
                raise PublishError(f"Bunny recusou a codificação (status {status})")
            if clock() - started > timeout:
                raise PublishError("tempo esgotado esperando a codificação no Bunny")
            sleep(interval)


class ManaLessons:
    """Cliente do contrato em docs/mana-publish.md. Só envia status=draft."""

    def __init__(self, http: httpx.Client, base_url: str, token: str) -> None:
        self.http = http
        self.url = lesson_endpoint(base_url)
        self.token = token.strip()

    def upsert_draft(self, payload: dict) -> dict:
        if payload.get("status") != LESSON_DRAFT:
            raise PublishError("recusa de envio: status diferente de draft")
        response = self.http.put(
            self.url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code >= 400 and response.status_code != 409:
            detail = _redact(response.text, [self.token])
            raise PublishError(f"Maná recusou a aula ({response.status_code}): {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PublishError("Maná não devolveu JSON") from exc
        if not isinstance(body, dict):
            raise PublishError("Maná devolveu um JSON que não é objeto")
        if response.status_code == 409 and not body.get("lesson_id"):
            raise PublishError("Maná recusou atualizar a aula (409)")
        created = response.status_code == 201
        if "created" in body:
            created = bool(body["created"])
        status = str(body.get("status") or "")
        lesson_id = str(body.get("lesson_id") or "")
        if not lesson_id:
            raise PublishError("Maná não devolveu lesson_id")
        if status not in {LESSON_DRAFT, "published"}:
            raise PublishError(f"situação inesperada da aula: {status or 'vazia'}")
        return {"lesson_id": lesson_id, "status": status, "created": created, "bunny_video_id": body.get("bunny_video_id") or payload["bunny_video_id"]}


def _entry_for(lecture: Lecture, state: dict) -> dict:
    raw = (state.get("uploads") or {}).get(lecture.drive_file_id) or {}
    entry = dict(raw) if isinstance(raw, dict) else {}
    if not entry.get("bunny_video_id") and lecture.bunny_video_id:
        entry["bunny_video_id"] = lecture.bunny_video_id
        entry["sha256"] = lecture.bunny_sha256 or entry.get("sha256") or ""
        entry["upload_complete"] = True
        entry["mana_lesson_id"] = lecture.mana_lesson_id or entry.get("mana_lesson_id") or ""
        if lecture.publish_status == "synced":
            entry["status"] = "synced"
    return entry


def _persist(db: Session, lecture: Lecture, state: dict, entry: dict) -> None:
    state.setdefault("uploads", {})[lecture.drive_file_id] = entry
    save_state(state)
    lecture.bunny_video_id = str(entry.get("bunny_video_id") or "")
    lecture.bunny_sha256 = str(entry.get("sha256") or "")
    lecture.mana_lesson_id = str(entry.get("mana_lesson_id") or "")
    lecture.publish_status = str(entry.get("status") or "")
    lecture.publish_error = str(entry.get("error") or "")
    lecture.bunny_uploaded_at = _parse_uploaded_at(str(entry.get("uploaded_at") or ""))
    lecture.updated_at = datetime.utcnow()
    db.commit()


def _fail(db: Session, lecture: Lecture, state: dict, entry: dict, message: str) -> None:
    entry["status"] = "failed"
    entry["error"] = message
    _persist(db, lecture, state, entry)
    raise PublishError(message)


def _upload_path(lecture: Lecture, runner: Callable[..., object] | None) -> tuple[Path | None, str]:
    source = find_processed_video(lecture)
    if source is None:
        return None, ""
    if settings.publish_transcode_720:
        dest = settings.publish_work_dir / f"{lecture.drive_file_id}-720p.mp4"
        source = transcode_to_720(source, dest, runner=runner)
    return source, file_sha256(source)


def publish_lecture(
    db: Session,
    lecture: Lecture,
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    runner: Callable[..., object] | None = None,
    now: Callable[[], int] | None = None,
) -> PublishResult:
    """Sobe o vídeo se o hash ainda não está mapeado, espera a codificação e grava o rascunho."""
    require_bunny()
    require_mana()
    summary = _summary(lecture)
    if summary is None:
        raise PublishError(f"{lecture.video_filename}: aula sem resumo")
    speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
    draft = build_lesson_payload(lecture, speaker, summary, bunny_video_id="")
    state = load_state()
    entry = _entry_for(lecture, state)
    upload_path, digest = _upload_path(lecture, runner)
    video_id = str(entry.get("bunny_video_id") or "")
    same_file = bool(digest and video_id and entry.get("sha256") == digest)
    file_missing_but_done = bool(not digest and video_id and entry.get("upload_complete"))
    encoded = int(entry.get("encode_status") or 0) in ENCODED_STATUSES

    if same_file or file_missing_but_done:
        draft["bunny_video_id"] = video_id
        fingerprint = lesson_fingerprint(draft)
        lesson_ready = bool(entry.get("mana_lesson_id")) and entry.get("lesson_fingerprint") == fingerprint
        if not settings.mana_publish_enabled:
            lesson_ready = True
        if entry.get("upload_complete") and encoded and lesson_ready and not entry.get("refused_auto_publish"):
            return PublishResult("skipped", video_id, str(entry.get("mana_lesson_id") or ""), "já mapeado")

    if upload_path is None and not (video_id and entry.get("upload_complete")):
        raise PublishError(f"vídeo processado não encontrado para {lecture.video_filename}")

    owns_http = http is None
    client = http or httpx.Client(timeout=httpx.Timeout(60.0, write=600.0))
    try:
        bunny = BunnyStream(
            client,
            library_id=settings.bunny_stream_library_id,
            api_key=settings.bunny_stream_api_key,
            api_base=settings.bunny_stream_api_base,
            tus_endpoint=settings.bunny_tus_endpoint,
            chunk_size=settings.publish_chunk_bytes,
            limit_720=settings.publish_transcode_720,
            now=now,
        )
        uploaded_now = False
        content_changed = bool(digest and video_id and entry.get("sha256") and entry.get("sha256") != digest)
        needs_bytes = not (same_file and entry.get("upload_complete")) and not file_missing_but_done
        if content_changed:
            needs_bytes = True
        if needs_bytes:
            if upload_path is None:
                raise PublishError(f"vídeo processado não encontrado para {lecture.video_filename}")
            resume_same = bool(video_id and entry.get("sha256") == digest and not entry.get("upload_complete"))
            if not resume_same:
                if video_id:
                    entry["previous_bunny_video_id"] = video_id
                    log.info("arquivo mudou; o vídeo %s permanece no Bunny e um novo será criado", video_id)
                video_id = bunny.create_video(draft["title"])
                entry["tus_url"] = ""
                entry["upload_complete"] = False
                entry["encode_status"] = 0
            entry.update(
                {
                    "drive_file_id": lecture.drive_file_id,
                    "sha256": digest,
                    "source_name": upload_path.name,
                    "source_mtime_ns": upload_path.stat().st_mtime_ns,
                    "bunny_video_id": video_id,
                    "bunny_library_id": settings.bunny_stream_library_id,
                    "bytes": upload_path.stat().st_size,
                    "status": "uploading",
                    "error": "",
                    "refused_auto_publish": False,
                }
            )
            _persist(db, lecture, state, entry)
            location = bunny.upload_file(
                video_id,
                upload_path,
                title=draft["title"],
                resume_url=str(entry.get("tus_url") or ""),
            )
            entry["tus_url"] = location
            entry["upload_complete"] = True
            entry["uploaded_at"] = str(entry.get("uploaded_at") or _now_iso())
            entry["status"] = "encoding"
            _persist(db, lecture, state, entry)
            uploaded_now = True
            encoded = False

        if encoded and int(entry.get("encode_status") or 0) in ENCODED_STATUSES and not uploaded_now:
            video = {"status": int(entry["encode_status"]), "guid": video_id}
        else:
            try:
                video = bunny.wait_until_encoded(
                    video_id,
                    timeout=float(settings.publish_encode_timeout_seconds),
                    interval=float(settings.publish_encode_poll_seconds),
                    sleep=sleep,
                    clock=clock,
                )
            except PublishError as exc:
                _fail(db, lecture, state, entry, str(exc))
            entry["encode_status"] = int(video.get("status") or 0)
            entry["status"] = "bunny_ready"
            entry["error"] = ""
            _persist(db, lecture, state, entry)

        draft["bunny_video_id"] = video_id
        fingerprint = lesson_fingerprint(draft)
        if not settings.mana_publish_enabled:
            detail = "MANA_PUBLISH_ENABLED desligado; aula não enviada"
            entry["status"] = "bunny_ready"
            entry["lesson_fingerprint"] = ""
            _persist(db, lecture, state, entry)
            action = "uploaded" if uploaded_now else "skipped"
            log.info("Bunny %s pronto (%s); %s", video_id, lecture.video_filename, detail)
            return PublishResult(action, video_id, str(entry.get("mana_lesson_id") or ""), detail)

        if (
            not uploaded_now
            and entry.get("mana_lesson_id")
            and entry.get("lesson_fingerprint") == fingerprint
            and not entry.get("refused_auto_publish")
        ):
            return PublishResult("skipped", video_id, str(entry["mana_lesson_id"]), "aula já sincronizada")

        mana = ManaLessons(client, settings.mana_api_url, settings.mana_api_token)
        try:
            body = mana.upsert_draft(draft)
        except PublishError as exc:
            _fail(db, lecture, state, entry, str(exc))
        created = bool(body["created"])
        lesson_status = str(body["status"])
        if created and lesson_status != LESSON_DRAFT:
            entry["refused_auto_publish"] = True
            _fail(
                db,
                lecture,
                state,
                entry,
                "a API criou a aula fora de rascunho; publicação automática recusada",
            )
        if lesson_status == "published" and entry.get("refused_auto_publish"):
            _fail(
                db,
                lecture,
                state,
                entry,
                "a aula criada pelo pipeline foi publicada pela API; o vínculo não foi aceito",
            )
        entry["mana_lesson_id"] = body["lesson_id"]
        entry["lesson_status"] = lesson_status
        entry["lesson_fingerprint"] = fingerprint
        entry["status"] = "synced"
        entry["error"] = ""
        _persist(db, lecture, state, entry)
        log.info(
            "rascunho %s %s para %s (bunny %s)",
            body["lesson_id"],
            "criado" if created else "atualizado",
            lecture.video_filename,
            video_id,
        )
        action = "uploaded" if uploaded_now else "synced"
        return PublishResult(action, video_id, body["lesson_id"], lesson_status)
    finally:
        if owns_http:
            client.close()


def _is_complete(lecture: Lecture, entry: dict) -> bool:
    if not entry.get("bunny_video_id") or not entry.get("upload_complete"):
        return False
    if int(entry.get("encode_status") or 0) not in ENCODED_STATUSES:
        return False
    if entry.get("refused_auto_publish"):
        return False
    if not settings.mana_publish_enabled:
        return entry.get("status") in {"bunny_ready", "synced"} or lecture.publish_status in {"bunny_ready", "synced"}
    return bool(entry.get("mana_lesson_id")) and entry.get("status") == "synced" and bool(entry.get("lesson_fingerprint"))


def _current_fingerprint(lecture: Lecture, video_id: str) -> str | None:
    summary = _summary(lecture)
    if summary is None:
        return None
    speaker = lecture.speaker if lecture.speaker_id else None
    try:
        payload = build_lesson_payload(lecture, speaker, summary, bunny_video_id=video_id)
    except PublishError:
        return None
    return lesson_fingerprint(payload)


def _settled(lecture: Lecture, entry: dict, source: Path | None) -> bool:
    """True quando não há nada a enviar sem precisar reler o arquivo inteiro."""
    if not _is_complete(lecture, entry):
        return False
    if settings.mana_publish_enabled:
        current = _current_fingerprint(lecture, str(entry.get("bunny_video_id") or ""))
        if current != entry.get("lesson_fingerprint"):
            return False
    if source is None:
        return True
    if settings.publish_transcode_720:
        derivative = settings.publish_work_dir / f"{lecture.drive_file_id}-720p.mp4"
        if not derivative.is_file():
            return False
        if derivative.stat().st_mtime_ns < source.stat().st_mtime_ns:
            return False
        return entry.get("bytes") == derivative.stat().st_size and entry.get("source_mtime_ns") == derivative.stat().st_mtime_ns
    return (
        entry.get("source_name") == source.name
        and entry.get("bytes") == source.stat().st_size
        and entry.get("source_mtime_ns") == source.stat().st_mtime_ns
    )


def publish_pending(db: Session, *, max_items: int = 1) -> tuple[int, int, int, int]:
    """Retorna (enviados ao Bunny, aulas sincronizadas, pulados, falhas)."""
    require_bunny()
    require_mana()
    rows = (
        db.query(Lecture)
        .filter(Lecture.summarized.is_(True))
        .order_by(Lecture.lecture_date.desc(), Lecture.id.desc())
        .all()
    )
    sent = lessons = skipped = failed = 0
    attempted = 0
    for lecture in rows:
        if attempted >= max_items:
            break
        entry = _entry_for(lecture, load_state())
        source = find_processed_video(lecture)
        has_upload = bool(entry.get("bunny_video_id") and entry.get("upload_complete"))
        if source is None and not has_upload:
            continue
        if _settled(lecture, entry, source):
            continue
        attempted += 1
        try:
            result = publish_lecture(db, lecture)
        except PublishError as exc:
            failed += 1
            log.error("publicação de %s não concluiu: %s", lecture.video_filename, exc)
            continue
        if result.action == "uploaded":
            sent += 1
            if result.lesson_id:
                lessons += 1
        elif result.action == "synced":
            lessons += 1
        else:
            skipped += 1
    return sent, lessons, skipped, failed
