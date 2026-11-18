"""Passo opcional: sobe o vídeo processado ao Bunny Stream e grava um rascunho no Maná.

Não apaga arquivo no Google Drive. O MP4 original da gravação permanece na pasta do dia.
O mesmo conteúdo (SHA-256) não é enviado de novo: o vínculo fica em data/publish/state.json
e nas colunas da aula.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import logging
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, Speaker
from backend.app.services.obsidian import _label, collect_references, parse_reference
from backend.app.services.pipeline import lecture_work_dir
from colab.youtube_lib import (
    PLAYLIST_CULT_MANHA,
    PLAYLIST_CULT_NOITE,
    PLAYLIST_EBD,
    classify_playlist,
    parse_path_date,
    resolve_event_time,
    clean_study_title,
    study_title_from_summary,
)

log = logging.getLogger("bible_notely.publish")

# Só a versão reduzida vai ao Bunny. O original da câmera nunca é enviado.
COMPRESSED_SUFFIX = ".compressed.mp4"

KIND_LABEL = {
    "ebd": "EBD",
    "culto_manha": "Culto da Manhã",
    "culto_noite": "Culto da Noite",
}

# https://docs.bunny.net — 4 Finished, 7/8 playable no JIT; 5 Error, 6 UploadFailed.
ENCODED_STATUSES = frozenset({4, 7, 8})
FAILED_STATUSES = frozenset({5, 6})
LESSON_DRAFT = "draft"
TUS_VERSION = "1.0.0"


class PublishError(RuntimeError):
    pass


RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_RETRIES = 3


def request_with_retry(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs,
) -> httpx.Response:
    """Repete falhas transitórias e transforma httpx.HTTPError em PublishError."""
    delay = 1.0
    attempts = max(1, retries)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise PublishError(f"falha de rede ao chamar {url}: {exc}") from exc
            sleep(delay)
            delay *= 2
            continue
        if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
            sleep(delay)
            delay *= 2
            continue
        return response
    raise PublishError(f"falha de rede ao chamar {url}: {last_error}")


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
    """Devolve só `{stem}.compressed.mp4`. O original da câmera não vai ao Bunny."""
    from backend.app.services.compress import resolve_stored_path

    candidates: list[Path] = []
    stored = getattr(lecture, "compressed_path", "") or ""
    if stored:
        path = resolve_stored_path(stored)
        if path is not None and path.is_file() and path.stat().st_size > 0:
            candidates.append(path.resolve())
    work = lecture_work_dir(lecture)
    stem = Path(lecture.video_filename or "").stem
    if stem:
        regular = work / f"{stem}{COMPRESSED_SUFFIX}"
        if regular.is_file() and regular.stat().st_size > 0:
            candidates.append(regular.resolve())
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        return path
    return None


def event_kind_for(lecture: Lecture | str) -> str:
    """ebd, culto_manha ou culto_noite, a partir do horário no nome do arquivo."""
    filename = lecture if isinstance(lecture, str) else (lecture.video_filename or "")
    return classify_playlist(resolve_event_time(filename))


def event_kind_label(kind: str) -> str:
    return KIND_LABEL.get(kind, kind)


def lesson_type_for(lecture: Lecture | str) -> str:
    """Valor de Lesson.type no Maná: ebd antes das 10h; culto da manhã e da noite são service."""
    kind = event_kind_for(lecture)
    return "ebd" if kind == PLAYLIST_EBD else "service"


def format_publish_date(when) -> str:
    """Data no padrão da biblioteca Bunny: 15/11/26."""
    return when.strftime("%d/%m/%y")


def build_publish_title(study_title: str, event_date, kind: str) -> str:
    """Ex.: IBQB - EBD - 15/11/26 - Estudo sobre o Milênio."""
    theme = clean_study_title(study_title) or "Sem título"
    return f"IBQB - {event_kind_label(kind)} - {format_publish_date(event_date)} - {theme}"


def build_publish_description(
    *,
    kind: str,
    event_date,
    speaker_name: str = "",
    summary: dict | None = None,
) -> str:
    """Resumo na descrição do Bunny: cabeçalho + texto-base, pontos, aplicações e conclusão."""
    header = [
        "IBQB",
        f"Tipo: {event_kind_label(kind)}",
        f"Data: {event_date.strftime('%d/%m/%Y') if event_date else '—'}",
    ]
    if (speaker_name or "").strip():
        header.append(f"Palestrante: {speaker_name.strip()}")
    parts: list[str] = ["\n".join(header), ""]
    if not summary:
        return "\n".join(parts).strip()
    if summary.get("texto_base"):
        parts += ["Texto-base", str(summary["texto_base"]).strip(), ""]
    for label, key in (("Pontos", "pontos"), ("Aplicações", "aplicacoes"), ("Conclusão", "conclusao")):
        items = [str(item).strip() for item in (summary.get(key) or []) if str(item).strip()]
        if not items:
            if key == "conclusao" and str(summary.get("conclusao") or "").strip():
                items = [str(summary["conclusao"]).strip()]
        if items:
            parts.append(label)
            parts.extend(f"• {item}" for item in items)
            parts.append("")
    return "\n".join(parts).strip()


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


# A API do Maná (PR #3) só autentica sessão Google. Não há token de script nem conta de serviço.
MACHINE_AUTH_MISSING = (
    "O Maná não autentica um script: as rotas de admin exigem sessão Google de uma pessoa. "
    "A aula não foi enviada. Veja docs/mana-publish.md."
)


def reject_unless_draft(payload: dict) -> None:
    """O corpo segue LessonInput. Este cliente nunca pede status published."""
    if payload.get("status") != LESSON_DRAFT:
        raise PublishError("recusa de envio: status diferente de draft")
    if payload.get("type") not in {"ebd", "service", "other"}:
        raise PublishError(f"tipo de aula inválido: {payload.get('type')}")


def normalize_person(name: str) -> str:
    folded = "".join(
        char for char in unicodedata.normalize("NFD", name or "") if unicodedata.category(char) != "Mn"
    )
    return " ".join(folded.lower().split())


def speaker_map() -> dict[str, str]:
    """nome normalizado → speaker_id. O arquivo local entra primeiro; MANA_SPEAKER_MAP cobre por cima."""
    found: dict[str, str] = {}

    def _take(data: object) -> None:
        if not isinstance(data, dict):
            raise PublishError("mapa de palestrantes deve ser um objeto nome → speaker_id")
        for key, value in data.items():
            speaker_id = str(value or "").strip()
            if speaker_id:
                found[normalize_person(str(key))] = speaker_id

    path = settings.publish_speakers_path
    if path.exists():
        try:
            _take(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise PublishError(f"{path.name} não é JSON") from exc
    raw = settings.mana_speaker_map.strip()
    if raw:
        try:
            _take(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise PublishError("MANA_SPEAKER_MAP não é JSON") from exc
    return found


def speaker_id_from_map(name: str) -> str | None:
    return speaker_map().get(normalize_person(name))


def build_lesson_payload(
    lecture: Lecture,
    speaker: Speaker | None,
    summary: dict,
    *,
    bunny_video_id: str,
    event_date=None,
) -> dict:
    """LessonInput do Maná. status é sempre draft. speaker_id sai do mapa local, se houver."""
    when = event_date or lecture.lecture_date or parse_path_date(lecture.drive_path or "")
    if when is None:
        raise PublishError("aula sem data")
    kind = event_kind_for(lecture)
    study = study_title_from_summary(summary) or "Sem título"
    title = build_publish_title(study, when, kind)
    speaker_name = (speaker.name.strip() if speaker and speaker.name else "") or "Não identificado"
    payload = {
        "title": title,
        "date": when.isoformat(),
        "type": lesson_type_for(lecture),
        "speaker_name": speaker_name,
        "speaker_id": speaker_id_from_map(speaker_name),
        "bunny_video_id": (bunny_video_id or "").strip() or None,
        "verses": lesson_verses(summary),
        "body": lesson_body(summary),
        "status": LESSON_DRAFT,
    }
    reject_unless_draft(payload)
    return payload



def date_from_filename(name: str) -> date | None:
    match = re.search(r"(20\d{2})(\d{2})(\d{2})_", Path(name).name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def event_date_for(lecture: Lecture) -> date | None:
    return lecture.lecture_date or parse_path_date(lecture.drive_path or "") or date_from_filename(lecture.video_filename or "")


def same_day_kind_conflict(db: Session | None, lecture: Lecture, state: dict | None = None) -> str | None:
    """Outro vídeo do mesmo tipo (EBD / Culto da Manhã / Culto da Noite) já enviado naquele dia."""
    if db is None or lecture.id is None:
        return None
    kind = event_kind_for(lecture)
    when = event_date_for(lecture)
    if when is None:
        return None
    state = state if state is not None else load_state()
    others = db.query(Lecture).filter(Lecture.lecture_date == when, Lecture.id != lecture.id).all()
    for other in others:
        if event_kind_for(other) != kind:
            continue
        if (other.bunny_video_id or "").strip():
            return other.video_filename or other.drive_file_id
        entry = _entry_for(other, state)
        if entry.get("bunny_video_id"):
            return other.video_filename or other.drive_file_id
    return None


@dataclass
class PublishPlan:
    file: str
    file_size: int
    title: str
    description: str
    library_id: str
    kind: str
    lesson: dict
    skip_reason: str = ""


def _public_lesson(payload: dict) -> dict:
    """Cópia do payload da aula, sem campos internos nem segredos."""
    skip = {"_bunny_description"}
    return {key: value for key, value in payload.items() if key not in skip}


def plan_publish(
    lecture: Lecture,
    speaker: Speaker | None,
    summary: dict,
    source: Path | None = None,
    db: Session | None = None,
    state: dict | None = None,
    force: bool = False,
) -> PublishPlan:
    when = event_date_for(lecture)
    if when is None:
        raise PublishError("aula sem data")
    kind = event_kind_for(lecture)
    draft = build_lesson_payload(lecture, speaker, summary, bunny_video_id="", event_date=when)
    speaker_name = str(draft.get("speaker_name") or "")
    description = build_publish_description(kind=kind, event_date=when, speaker_name=speaker_name, summary=summary)
    path = source or find_processed_video(lecture)
    skip = ""
    if path is None:
        skip = "versão reduzida (.compressed.mp4) não encontrada; o original da câmera não é enviado"
        size = 0
        file_s = ""
    else:
        size = path.stat().st_size
        file_s = str(path)
        conflict = same_day_kind_conflict(db, lecture, state)
        if conflict and not force:
            skip = f"já há um {event_kind_label(kind)} neste dia ({conflict})"
    return PublishPlan(
        file=file_s,
        file_size=size,
        title=str(draft["title"]),
        description=description,
        library_id=settings.bunny_stream_library_id.strip(),
        kind=kind,
        lesson=_public_lesson(draft),
        skip_reason=skip,
    )


def render_plan(plan: PublishPlan) -> str:
    preview = plan.description[:300]
    if len(plan.description) > 300:
        preview += "…"
    lines = []
    if plan.skip_reason:
        lines.append(f"não enviaria: {plan.skip_reason}")
    lines += [
        f"arquivo: {plan.file or '—'}",
        f"tamanho: {plan.file_size} bytes" if plan.file_size else "tamanho: —",
        f"biblioteca: {plan.library_id or '(BUNNY_STREAM_LIBRARY_ID não configurado)'}",
        f"tipo: {event_kind_label(plan.kind)}",
        f"título: {plan.title}",
        "descrição (primeiros 300 caracteres):",
        preview,
        "payload da aula (rascunho):",
        json.dumps(plan.lesson, ensure_ascii=False, indent=2),
    ]
    return "\n".join(lines)


def plan_from_paths(video: Path, summary_path: Path | None, speaker_name: str = "") -> PublishPlan:
    """Monta o plano a partir de um .compressed.mp4 e do JSON do resumo, sem falar com APIs."""
    video = video.expanduser().resolve()
    if not video.is_file():
        raise PublishError(f"não encontrei {video}")
    if not video.name.endswith(COMPRESSED_SUFFIX):
        raise PublishError("só a versão reduzida (.compressed.mp4) pode ir ao Bunny")
    summary: dict = {}
    if summary_path:
        summary_path = summary_path.expanduser().resolve()
        if not summary_path.is_file():
            raise PublishError(f"não encontrei {summary_path}")
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PublishError(f"{summary_path.name} não é JSON") from exc
        if not isinstance(loaded, dict):
            raise PublishError("resumo precisa ser um objeto JSON")
        summary = loaded
    stem = video.name[: -len(COMPRESSED_SUFFIX)]
    filename = f"{stem}.mp4"
    when = date_from_filename(filename)
    lecture = Lecture(
        drive_file_id=f"dry-run-{stem}",
        drive_path="",
        video_filename=filename,
        lecture_date=when,
        summary_text=json.dumps(summary, ensure_ascii=False),
        summarized=True,
    )
    speaker = Speaker(name=speaker_name.strip()) if speaker_name.strip() else None
    return plan_publish(lecture, speaker, summary, source=video)

def lesson_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


_LESSON_GUARD_FIELDS = ("title", "date", "type", "speaker_id", "verses", "body", "status")


def _norm_guard_value(key: str, value):
    if key == "verses":
        items = []
        for item in value or []:
            if isinstance(item, str):
                items.append(item)
            elif isinstance(item, dict):
                items.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
            else:
                items.append(str(item))
        return items
    if value is None:
        return ""
    if key == "status":
        return str(value).strip().lower()
    return value


def lesson_view(payload: dict | None) -> dict:
    """Campos que uma pessoa pode ter editado no site; usados para não sobrescrever."""
    if not payload:
        return {}
    return {key: _norm_guard_value(key, payload.get(key)) for key in _LESSON_GUARD_FIELDS}


def lesson_was_edited(current: dict | None, last_written: dict | None) -> bool:
    if not current or not last_written:
        return False
    return lesson_view(current) != lesson_view(last_written)


def lesson_is_published(data: dict | None) -> bool:
    status = str((data or {}).get("status") or "").strip().lower()
    return status in {"published", "publicada", "publicado"}


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
    """Não exige token. Sem sessão Google o cliente não chama a API."""
    return


class ManaLessons:
    """Cliente da aula no Maná.

    Hoje fala com POST/PUT /api/admin/lessons (sessão Google). A mesma interface
    `upsert_lesson(drive_file_id, …)` já está pronta para o futuro
    PUT /api/pipeline/lessons/{drive_file_id} (MANA_PIPELINE_LESSONS).
    A chave de idempotência é o drive_file_id.
    """

    ADMIN_LESSONS = "/api/admin/lessons"
    PIPELINE_LESSONS = "/api/pipeline/lessons"

    def __init__(
        self,
        http: httpx.Client,
        base_url: str,
        session_token: str,
        *,
        use_pipeline: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        root = base_url.strip().rstrip("/")
        if root.endswith("/api"):
            root = root[: -len("/api")]
        self.origin = root
        self.http = http
        self.token = session_token.strip()
        self.use_pipeline = settings.mana_pipeline_lessons if use_pipeline is None else use_pipeline
        self.sleep = sleep

    def _headers(self, drive_file_id: str = "") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if drive_file_id:
            headers["Idempotency-Key"] = drive_file_id
        return headers

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return request_with_retry(self.http, method, url, sleep=self.sleep, **kwargs)

    def _data(self, response: httpx.Response, action: str) -> object:
        if response.status_code >= 400:
            detail = _redact(response.text, [self.token])
            raise PublishError(f"Maná recusou ao {action} ({response.status_code}): {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise PublishError("Maná não devolveu JSON") from exc

    def get_lesson(self, lesson_id: str) -> dict | None:
        response = self._request("GET", f"{self.origin}{self.ADMIN_LESSONS}/{lesson_id}", headers=self._headers())
        if response.status_code == 404:
            return None
        data = self._data(response, "ler a aula")
        if not isinstance(data, dict):
            raise PublishError("Maná devolveu uma aula inválida")
        return data

    def list_speakers(self) -> list[dict]:
        response = self._request("GET", f"{self.origin}/api/admin/speakers", headers=self._headers())
        data = self._data(response, "listar palestrantes")
        if not isinstance(data, list):
            raise PublishError("Maná devolveu palestrantes inválidos")
        return [row for row in data if isinstance(row, dict)]

    def create_lesson(self, payload: dict, drive_file_id: str = "") -> dict:
        reject_unless_draft(payload)
        if not str(payload.get("speaker_id") or "").strip():
            raise PublishError("speaker_id obrigatório")
        response = self._request(
            "POST",
            f"{self.origin}{self.ADMIN_LESSONS}",
            headers=self._headers(drive_file_id),
            json=payload,
        )
        return self._lesson(self._data(response, "criar a aula"))

    def update_lesson(self, lesson_id: str, payload: dict, drive_file_id: str = "") -> dict:
        reject_unless_draft(payload)
        if not str(payload.get("speaker_id") or "").strip():
            raise PublishError("speaker_id obrigatório")
        response = self._request(
            "PUT",
            f"{self.origin}{self.ADMIN_LESSONS}/{lesson_id}",
            headers=self._headers(drive_file_id),
            json=payload,
        )
        return self._lesson(self._data(response, "atualizar a aula"))

    def upsert_lesson(self, drive_file_id: str, payload: dict, *, lesson_id: str = "") -> dict:
        """Grava a aula de forma idempotente pela chave drive_file_id."""
        if not str(drive_file_id or "").strip():
            raise PublishError("drive_file_id obrigatório para gravar a aula")
        if self.use_pipeline:
            return self._put_pipeline(drive_file_id, payload)
        if lesson_id:
            return self.update_lesson(lesson_id, payload, drive_file_id=drive_file_id)
        return self.create_lesson(payload, drive_file_id=drive_file_id)

    def _put_pipeline(self, drive_file_id: str, payload: dict) -> dict:
        reject_unless_draft(payload)
        if not str(payload.get("speaker_id") or "").strip():
            raise PublishError("speaker_id obrigatório")
        response = self._request(
            "PUT",
            f"{self.origin}{self.PIPELINE_LESSONS}/{drive_file_id}",
            headers=self._headers(drive_file_id),
            json=payload,
        )
        return self._lesson(self._data(response, "gravar a aula"))

    def _lesson(self, data: object) -> dict:
        if not isinstance(data, dict) or not str(data.get("lesson_id") or "").strip():
            raise PublishError("Maná não devolveu lesson_id")
        return data


def resolve_speaker_id(name: str, mana: ManaLessons | None) -> str:
    """Mapa local primeiro. Sem ele, GET /api/admin/speakers e o nome, sem acento e sem maiúsculas."""
    mapped = speaker_id_from_map(name)
    if mapped:
        return mapped
    if mana is None:
        raise PublishError(f"sem speaker_id para {name}; preencha data/publish/speakers.json")
    wanted = normalize_person(name)
    found: list[str] = []
    for row in mana.list_speakers():
        if normalize_person(str(row.get("name") or "")) != wanted:
            continue
        speaker_id = str(row.get("speaker_id") or "").strip()
        if speaker_id and speaker_id not in found:
            found.append(speaker_id)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise PublishError(f"mais de um palestrante no Maná com o nome {name}")
    raise PublishError(f"sem speaker_id para {name}; cadastre o palestrante no Maná ou em speakers.json")


def _lock_published(db: Session, lecture: Lecture, state: dict, entry: dict, lesson_id: str) -> PublishResult:
    """Aula já publicada por uma pessoa: nenhuma escrita daqui para a frente."""
    entry["mana_lesson_id"] = lesson_id
    entry["lesson_status"] = "published"
    entry["status"] = "skipped_published"
    entry["error"] = ""
    _persist(db, lecture, state, entry)
    log.info("aula %s publicada; sincronização não altera", lesson_id)
    return PublishResult("skipped", str(entry.get("bunny_video_id") or ""), lesson_id, "aula publicada; não alterada")


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.http = http
        self.library_id = library_id.strip()
        self.api_key = api_key.strip()
        self.api_base = api_base.rstrip("/")
        self.tus_endpoint = tus_endpoint
        self.chunk_size = max(1, chunk_size)
        self.limit_720 = limit_720
        self._now = now or (lambda: int(time.time()))
        self.sleep = sleep

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return request_with_retry(self.http, method, url, sleep=self.sleep, **kwargs)

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
        """CreateVideoModel aceita só title (e opcionalmente collectionId/thumbnailTime)."""
        response = self._request(
            "POST",
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

    def update_video_meta(self, video_id: str, *, title: str = "", description: str = "") -> None:
        """UpdateVideoModel não tem description; a descrição vai em metaTags.property=description."""
        body: dict = {}
        if title:
            body["title"] = title
        if description:
            body["metaTags"] = [{"property": "description", "value": description}]
        if not body:
            return
        response = self._request(
            "POST",
            f"{self.api_base}/library/{self.library_id}/videos/{video_id}",
            headers={"AccessKey": self.api_key, "Accept": "application/json"},
            json=body,
        )
        self._fail(response, "atualizar título e descrição")

    def get_video(self, video_id: str) -> dict:
        response = self._request(
            "GET",
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

    def upload_file(
        self,
        video_id: str,
        path: Path,
        *,
        title: str,
        resume_url: str = "",
        on_location: Callable[[str], None] | None = None,
    ) -> str:
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
            if on_location:
                on_location(location)
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
        response = self._request("POST", self.tus_endpoint, headers=headers)
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
        response = self._request("HEAD", location, headers=self._auth(video_id))
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
        response = self._request("PATCH", location, headers=headers, content=chunk)
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
    dry_run: bool = False,
    force: bool = False,
) -> PublishResult:
    """Sobe o vídeo se o hash ainda não está mapeado, espera a codificação e grava o rascunho."""
    summary = _summary(lecture)
    if summary is None:
        raise PublishError(f"{lecture.video_filename}: aula sem resumo")
    speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
    state = load_state()
    plan = plan_publish(lecture, speaker, summary, db=db, state=state, force=force)
    if dry_run:
        return PublishResult("dry-run", detail=render_plan(plan))
    if plan.skip_reason.startswith("já há um"):
        return PublishResult("skipped", detail=plan.skip_reason)
    require_bunny()
    require_mana()
    draft = build_lesson_payload(lecture, speaker, summary, bunny_video_id="")
    description = plan.description
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
            sleep=sleep,
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
                entry.update(
                    {
                        "drive_file_id": lecture.drive_file_id,
                        "sha256": digest,
                        "source_name": upload_path.name,
                        "source_mtime_ns": upload_path.stat().st_mtime_ns,
                        "bunny_video_id": video_id,
                        "bunny_library_id": settings.bunny_stream_library_id,
                        "bytes": upload_path.stat().st_size,
                        "tus_url": "",
                        "upload_complete": False,
                        "encode_status": 0,
                        "status": "created",
                        "error": "",
                        "refused_auto_publish": False,
                    }
                )
                _persist(db, lecture, state, entry)
                try:
                    bunny.update_video_meta(video_id, title=draft["title"], description=description)
                except PublishError as exc:
                    log.warning("descrição do Bunny não gravou para %s: %s", video_id, exc)
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

            def _save_tus(location: str) -> None:
                entry["tus_url"] = location
                entry["status"] = "uploading"
                _persist(db, lecture, state, entry)

            location = bunny.upload_file(
                video_id,
                upload_path,
                title=draft["title"],
                resume_url=str(entry.get("tus_url") or ""),
                on_location=_save_tus,
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

        draft["bunny_video_id"] = video_id or None
        mapped = speaker_id_from_map(str(draft.get("speaker_name") or ""))
        if mapped:
            draft["speaker_id"] = mapped
        reject_unless_draft(draft)
        action = "uploaded" if uploaded_now else "skipped"
        if not settings.mana_publish_enabled:
            detail = "MANA_PUBLISH_ENABLED desligado; aula não enviada"
            entry["lesson_payload"] = draft
            entry["status"] = "bunny_ready"
            entry["error"] = ""
            _persist(db, lecture, state, entry)
            log.info("Bunny %s pronto (%s); %s", video_id, lecture.video_filename, detail)
            return PublishResult(action, video_id, str(entry.get("mana_lesson_id") or ""), detail)

        session = settings.mana_session_token.strip()
        if not session or not settings.mana_api_url.strip():
            entry["lesson_payload"] = draft
            entry["status"] = "bunny_ready"
            entry["error"] = MACHINE_AUTH_MISSING
            _persist(db, lecture, state, entry)
            log.info("Bunny %s pronto (%s); %s", video_id, lecture.video_filename, MACHINE_AUTH_MISSING)
            return PublishResult(action, video_id, str(entry.get("mana_lesson_id") or ""), MACHINE_AUTH_MISSING)

        mana = ManaLessons(client, settings.mana_api_url, session, sleep=sleep)
        lesson_id = str(entry.get("mana_lesson_id") or "")
        current: dict | None = None
        if lesson_id:
            try:
                current = mana.get_lesson(lesson_id)
            except PublishError as exc:
                _fail(db, lecture, state, entry, str(exc))
            if current and lesson_is_published(current):
                return _lock_published(db, lecture, state, entry, lesson_id)
            if current is None:
                lesson_id = ""
            elif lesson_was_edited(current, entry.get("written_lesson")):
                entry["status"] = "skipped_edited"
                entry["error"] = ""
                _persist(db, lecture, state, entry)
                log.info("aula %s editada no site; não sobrescrevo", lesson_id)
                return PublishResult(
                    "skipped",
                    str(entry.get("bunny_video_id") or ""),
                    lesson_id,
                    "aula editada no site; não alterada",
                )

        if not str(draft.get("speaker_id") or "").strip():
            try:
                draft["speaker_id"] = resolve_speaker_id(str(draft.get("speaker_name") or ""), mana)
            except PublishError as exc:
                _fail(db, lecture, state, entry, str(exc))
        reject_unless_draft(draft)
        entry["lesson_payload"] = draft
        entry["lesson_fingerprint"] = lesson_fingerprint(draft)
        if not lesson_id:
            entry["status"] = "lesson_pending"
        _persist(db, lecture, state, entry)

        wire = dict(draft)
        youtube_id = str((current or {}).get("youtube_video_id") or "").strip()
        if youtube_id:
            wire["youtube_url"] = f"https://www.youtube.com/watch?v={youtube_id}"
        try:
            saved = mana.upsert_lesson(lecture.drive_file_id, wire, lesson_id=lesson_id)
        except PublishError as exc:
            _fail(db, lecture, state, entry, str(exc))
        saved_id = str(saved.get("lesson_id") or "")
        saved_status = str(saved.get("status") or "")
        if lesson_is_published(saved):
            return _lock_published(db, lecture, state, entry, saved_id)
        if saved_status != LESSON_DRAFT:
            _fail(db, lecture, state, entry, f"situação inesperada da aula: {saved_status or 'vazia'}")
        entry["mana_lesson_id"] = saved_id
        entry["lesson_status"] = LESSON_DRAFT
        entry["written_lesson"] = lesson_view({**wire, "status": saved_status})
        entry["status"] = "synced"
        entry["error"] = ""
        _persist(db, lecture, state, entry)
        log.info("rascunho %s gravado para %s (bunny %s)", saved_id, lecture.video_filename, video_id)
        synced_action = "uploaded" if uploaded_now else "synced"
        return PublishResult(synced_action, video_id, saved_id, LESSON_DRAFT)
    finally:
        if owns_http:
            client.close()


def _is_complete(lecture: Lecture, entry: dict) -> bool:
    if not entry.get("bunny_video_id") or not entry.get("upload_complete"):
        return False
    if int(entry.get("encode_status") or 0) not in ENCODED_STATUSES:
        return False
    if entry.get("lesson_status") == "published" and entry.get("mana_lesson_id"):
        return True
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
    published = entry.get("lesson_status") == "published" and entry.get("mana_lesson_id")
    if settings.mana_publish_enabled and not published:
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


def publish_pending(
    db: Session,
    *,
    max_items: int = 1,
    dry_run: bool = False,
    lecture_id: int | None = None,
    force: bool = False,
) -> tuple[int, int, int, int]:
    """Retorna (enviados ao Bunny, aulas sincronizadas, pulados, falhas)."""
    if not dry_run:
        require_bunny()
        require_mana()
    query = db.query(Lecture).filter(Lecture.summarized.is_(True))
    if lecture_id:
        query = query.filter(Lecture.id == lecture_id)
    rows = query.order_by(Lecture.lecture_date.desc(), Lecture.id.desc()).all()
    sent = lessons = skipped = failed = 0
    attempted = 0
    for lecture in rows:
        if attempted >= max_items:
            break
        entry = _entry_for(lecture, load_state())
        source = find_processed_video(lecture)
        has_upload = bool(entry.get("bunny_video_id") and entry.get("upload_complete"))
        if source is None and not has_upload and not dry_run:
            continue
        if not dry_run and _settled(lecture, entry, source):
            continue
        attempted += 1
        try:
            result = publish_lecture(db, lecture, dry_run=dry_run, force=force)
        except PublishError as exc:
            failed += 1
            log.error("publicação de %s não concluiu: %s", lecture.video_filename, exc)
            continue
        except httpx.HTTPError as exc:
            failed += 1
            log.error("publicação de %s não concluiu: %s", lecture.video_filename, exc)
            continue
        if result.action == "dry-run":
            print(result.detail)
            sent += 1
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
