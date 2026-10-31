from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.db import SessionLocal, get_db
from backend.app.models import Lecture, Speaker
from backend.app.runtime import (
    get_ai_provider,
    get_drive_video_count,
    get_folder_id,
    get_youtube_upload_folder_id,
    get_youtube_upload_path,
    cancel_event,
    pause_event,
    pipeline_snapshot,
    set_pipeline,
    update_runtime,
)
from backend.app.schemas import (
    AssignSpeakerIn,
    LectureOut,
    PipelineStatus,
    SettingsIn,
    SettingsOut,
    SpeakerIn,
    SpeakerOut,
    YouTubeBrowseOut,
    YouTubeFolderOut,
)
from backend.app.services import youtube as youtube_service
from backend.app.services import drive
from backend.app.services.llm import provider_flags
from backend.app.services.pipeline import (
    RESTART_STAGES,
    TOO_SMALL_NOTE,
    UNLIMITED,
    PipelineBusy,
    _remove_local_video,
    lecture_work_dir,
    pipeline_lock,
    prepare_restart,
    recover_if_idle,
    process_one,
    run_batch,
    sync_drive_videos,
)
from backend.app.services.speakers import IMAGE_SUFFIXES, apply_photo, find_by_name, foto_path_for, import_fotos

log = logging.getLogger("bible_notely.api")
router = APIRouter(prefix="/api")


def _photo_url(speaker: Speaker) -> str | None:
    if speaker.photo_path and Path(speaker.photo_path).exists():
        return f"/api/speakers/{speaker.id}/photo"
    return None


def _frame_url(lecture: Lecture) -> str | None:
    if lecture.frame_path and Path(lecture.frame_path).exists():
        return f"/api/lectures/{lecture.id}/frame"
    return None


def speaker_out(db: Session, speaker: Speaker) -> SpeakerOut:
    count = db.query(func.count(Lecture.id)).filter(Lecture.speaker_id == speaker.id).scalar() or 0
    return SpeakerOut.model_validate(speaker).model_copy(
        update={
            "lecture_count": count,
            "photo_url": _photo_url(speaker),
            "face_ready": bool(speaker.embedding),
        }
    )


def lecture_out(lecture: Lecture) -> LectureOut:
    return LectureOut.model_validate(lecture).model_copy(
        update={
            "speaker_name": lecture.speaker.name if lecture.speaker else None,
            "frame_url": _frame_url(lecture),
        }
    )


def require_speakers(db: Session) -> None:
    count = db.query(func.count(Speaker.id)).scalar() or 0
    if count == 0:
        raise HTTPException(409, "Cadastre pelo menos um professor antes das chaves")


@router.get("/health")
def health() -> dict:
    return {"ok": True}


def _settings_out(db: Session) -> SettingsOut:
    provider = get_ai_provider()
    stats = youtube_service.youtube_scope_stats(db)
    return SettingsOut(
        google_connected=drive.is_connected(),
        google_account=drive.google_account(),
        google_drive_folder_id=get_folder_id(),
        google_client_configured=bool(settings.google_client_id and settings.google_client_secret),
        ai_provider=provider,
        ai_model=settings.ai_model
        or {
            "gemini": settings.gemini_model,
            "grok": settings.grok_model,
            "claude": settings.claude_model,
            "copilot": settings.copilot_model,
        }.get(provider, ""),
        providers=provider_flags(),
        whisper_model=settings.whisper_model,
        speakers_photo_dir=str(settings.speakers_fotos_dir),
        whisper_language=settings.whisper_language,
        youtube_connected=youtube_service.youtube_connected(),
        youtube_upload_folder_id=get_youtube_upload_folder_id(),
        youtube_upload_path=get_youtube_upload_path(),
        youtube_scope_eligible=stats["eligible"],
        youtube_scope_pending=stats["pending_youtube"],
        youtube_scope_uploaded=stats["on_youtube"],
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return _settings_out(db)


@router.put("/settings", response_model=SettingsOut)
def put_settings(body: SettingsIn, db: Session = Depends(get_db)) -> SettingsOut:
    require_speakers(db)
    payload = {}
    if body.ai_provider:
        if body.ai_provider not in {"gemini", "grok", "claude", "copilot"}:
            raise HTTPException(400, "Provedor inválido")
        payload["ai_provider"] = body.ai_provider
    if body.google_drive_folder_id is not None:
        payload["google_drive_folder_id"] = body.google_drive_folder_id
    if body.youtube_upload_folder_id is not None:
        payload["youtube_upload_folder_id"] = body.youtube_upload_folder_id.strip()
    if body.youtube_upload_path is not None:
        payload["youtube_upload_path"] = body.youtube_upload_path.replace("\\", "/").strip("/")
    if payload:
        update_runtime(**payload)
    return _settings_out(db)


@router.get("/youtube/browse", response_model=YouTubeBrowseOut)
def youtube_browse(
    parent_id: str | None = None,
    parent_path: str = "",
    db: Session = Depends(get_db),
) -> YouTubeBrowseOut:
    require_speakers(db)
    root = get_folder_id()
    if not root:
        raise HTTPException(400, "Configure a pasta raiz do Drive em Ajustes")
    if not drive.is_connected():
        raise HTTPException(400, "Conecte o Google Drive em Ajustes")
    current_parent = (parent_id or root).strip()
    normalized_path = parent_path.replace("\\", "/").strip("/")
    try:
        raw = youtube_service.list_drive_subfolders(current_parent)
    except drive.DriveError as exc:
        raise HTTPException(400, str(exc)) from exc
    folders = [
        YouTubeFolderOut(
            id=item["id"],
            name=item["name"],
            path=youtube_service.join_drive_path(normalized_path, item["name"]),
        )
        for item in raw
    ]
    stats = youtube_service.youtube_scope_stats(db)
    return YouTubeBrowseOut(
        parent_id=current_parent,
        parent_path=normalized_path,
        selection_folder_id=get_youtube_upload_folder_id(),
        selection_path=get_youtube_upload_path(),
        stats=stats,
        folders=folders,
    )


@router.get("/drive/auth")
def drive_auth(origin: str | None = None, db: Session = Depends(get_db)) -> dict:
    require_speakers(db)
    try:
        return {"url": drive.auth_url(origin)}
    except drive.DriveError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/drive/callback")
def drive_callback(code: str | None = None, error: str | None = None):
    origin = drive.login_return_origin()
    drive.log.info("login Google volta para %s", origin)
    if error:
        drive.log.warning("Google recusou o login: %s", error)
        return RedirectResponse(f"{origin}/?drive=error&reason={quote(error)}")
    if not code:
        drive.log.warning("callback do Google sem código de autorização")
        return RedirectResponse(f"{origin}/?drive=error&reason={quote('código ausente')}")
    try:
        drive.exchange_code(code)
    except drive.DriveError as exc:
        return RedirectResponse(f"{origin}/?drive=error&reason={quote(str(exc))}")
    return RedirectResponse(f"{origin}/?drive=connected")


@router.get("/speakers", response_model=list[SpeakerOut])
def list_speakers(db: Session = Depends(get_db)) -> list[SpeakerOut]:
    rows = db.query(Speaker).order_by(Speaker.name).all()
    return [speaker_out(db, row) for row in rows]


async def _store_photo(speaker: Speaker, file: UploadFile) -> None:
    suffix = Path(file.filename or "photo.jpg").suffix.lower() or ".jpg"
    if suffix not in IMAGE_SUFFIXES:
        raise HTTPException(400, "Use jpg, png ou webp")
    settings.speakers_fotos_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.speakers_fotos_dir / f".upload-{speaker.id}{suffix}"
    tmp.write_bytes(await file.read())
    try:
        apply_photo(speaker, tmp)
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/speakers", response_model=SpeakerOut)
async def create_speaker(
    name: str = Form(...),
    role: str = Form(""),
    active: bool = Form(True),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SpeakerOut:
    name = name.strip()
    if not name:
        raise HTTPException(400, "Nome obrigatório")
    if not file.filename:
        raise HTTPException(400, "Envie a foto do palestrante")
    if find_by_name(db, name):
        raise HTTPException(409, "Já existe palestrante com esse nome")
    speaker = Speaker(name=name, role=role.strip(), active=active)
    db.add(speaker)
    try:
        db.flush()
        await _store_photo(speaker, file)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Já existe palestrante com esse nome") from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        for suffix in IMAGE_SUFFIXES:
            foto_path_for(name, suffix).unlink(missing_ok=True)
        raise
    db.refresh(speaker)
    return speaker_out(db, speaker)


@router.put("/speakers/{speaker_id}", response_model=SpeakerOut)
def update_speaker(speaker_id: int, body: SpeakerIn, db: Session = Depends(get_db)) -> SpeakerOut:
    speaker = db.get(Speaker, speaker_id)
    if not speaker:
        raise HTTPException(404, "Palestrante não encontrado")
    name = body.name.strip()
    other = find_by_name(db, name)
    if other and other.id != speaker.id:
        raise HTTPException(409, "Já existe palestrante com esse nome")
    old_name = speaker.name
    speaker.name = name
    speaker.role = body.role.strip()
    speaker.active = body.active
    if old_name != name:
        for suffix in IMAGE_SUFFIXES:
            old = foto_path_for(old_name, suffix)
            if old.exists():
                old.rename(foto_path_for(name, suffix))
    db.commit()
    db.refresh(speaker)
    return speaker_out(db, speaker)


@router.delete("/speakers/{speaker_id}")
def delete_speaker(speaker_id: int, db: Session = Depends(get_db)) -> dict:
    speaker = db.get(Speaker, speaker_id)
    if not speaker:
        raise HTTPException(404, "Palestrante não encontrado")
    for suffix in IMAGE_SUFFIXES:
        foto_path_for(speaker.name, suffix).unlink(missing_ok=True)
    db.query(Lecture).filter(Lecture.speaker_id == speaker_id).update({Lecture.speaker_id: None})
    if speaker.photo_path:
        Path(speaker.photo_path).unlink(missing_ok=True)
    db.delete(speaker)
    db.commit()
    return {"ok": True}


@router.post("/speakers/import-photos")
def import_speaker_photos(db: Session = Depends(get_db)) -> dict:
    return import_fotos(db)


@router.post("/speakers/{speaker_id}/photo", response_model=SpeakerOut)
async def upload_photo(
    speaker_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SpeakerOut:
    speaker = db.get(Speaker, speaker_id)
    if not speaker:
        raise HTTPException(404, "Palestrante não encontrado")
    await _store_photo(speaker, file)
    db.commit()
    db.refresh(speaker)
    return speaker_out(db, speaker)


@router.get("/speakers/{speaker_id}/photo")
def speaker_photo(speaker_id: int, db: Session = Depends(get_db)):
    speaker = db.get(Speaker, speaker_id)
    if not speaker or not speaker.photo_path or not Path(speaker.photo_path).exists():
        raise HTTPException(404, "Foto não encontrada")
    return FileResponse(speaker.photo_path)


@router.get("/lectures", response_model=list[LectureOut])
def list_lectures(status: str | None = None, db: Session = Depends(get_db)) -> list[LectureOut]:
    query = db.query(Lecture).order_by(Lecture.lecture_date.desc(), Lecture.id.desc())
    if status:
        query = query.filter(Lecture.status == status)
    return [lecture_out(row) for row in query.all()]


@router.get("/lectures/{lecture_id}", response_model=LectureOut)
def get_lecture(lecture_id: int, db: Session = Depends(get_db)) -> LectureOut:
    lecture = db.get(Lecture, lecture_id)
    if not lecture:
        raise HTTPException(404, "Aula não encontrada")
    return lecture_out(lecture)


@router.get("/lectures/{lecture_id}/frame")
def lecture_frame(lecture_id: int, db: Session = Depends(get_db)):
    lecture = db.get(Lecture, lecture_id)
    if not lecture or not lecture.frame_path or not Path(lecture.frame_path).exists():
        raise HTTPException(404, "Frame não encontrado")
    return FileResponse(lecture.frame_path)


@router.post("/lectures/{lecture_id}/assign-speaker", response_model=LectureOut)
def assign_speaker(
    lecture_id: int,
    body: AssignSpeakerIn,
    db: Session = Depends(get_db),
) -> LectureOut:
    lecture = db.get(Lecture, lecture_id)
    speaker = db.get(Speaker, body.speaker_id)
    if not lecture or not speaker:
        raise HTTPException(404, "Aula ou palestrante não encontrado")
    lecture.speaker_id = speaker.id
    lecture.identification_status = "reviewed"
    lecture.identification_note = "Confirmado no frontend"
    lecture.speaker_confidence = 1.0
    speaker_changed = lecture.summary_speaker_id != speaker.id
    if speaker_changed:
        lecture.summarized = False
        lecture.uploaded = False
    if lecture.status in {"needs_review", "failed", "pending"} or speaker_changed:
        lecture.status = "pending"
    lecture.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(lecture)
    return lecture_out(lecture)


@router.get("/review", response_model=list[LectureOut])
def review_queue(db: Session = Depends(get_db)) -> list[LectureOut]:
    rows = (
        db.query(Lecture)
        .filter(Lecture.status == "needs_review")
        .order_by(Lecture.lecture_date.desc())
        .all()
    )
    return [lecture_out(row) for row in rows]


@router.get("/pipeline", response_model=PipelineStatus)
def pipeline_status(db: Session = Depends(get_db)) -> PipelineStatus:
    recover_if_idle(db)
    snap = pipeline_snapshot()
    today = datetime.utcnow().date()
    processed = (
        db.query(func.count(Lecture.id))
        .filter(Lecture.status == "done", func.date(Lecture.updated_at) == today)
        .scalar()
        or 0
    )
    return PipelineStatus(
        **snap,
        processed_today=processed,
        work_free_bytes=shutil.disk_usage(settings.work_dir).free,
        drive_video_count=get_drive_video_count(),
    )


@router.post("/pipeline/sync")
def pipeline_sync(db: Session = Depends(get_db)) -> dict:
    require_speakers(db)
    try:
        created = sync_drive_videos(db, force=True)
    except drive.DriveError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": created}


@router.post("/pipeline/run")
async def pipeline_run(max_items: int = 1, db: Session = Depends(get_db)) -> dict:
    require_speakers(db)
    if pipeline_snapshot()["running"]:
        raise HTTPException(409, "Pipeline já em execução")

    async def _job() -> None:
        db = SessionLocal()
        try:
            await run_batch(db, max_items=max_items)
        finally:
            db.close()

    asyncio.create_task(_job())
    return {"ok": True}


def _artifact(lecture: Lecture, filename: str) -> Path:
    return lecture_work_dir(lecture) / filename


def _materialize(lecture: Lecture, filename: str) -> Path | None:
    path = _artifact(lecture, filename)
    if path.exists() and path.stat().st_size > 0:
        return path
    if not lecture.drive_folder_id:
        return None
    try:
        if drive.download_named(lecture.drive_folder_id, filename, path):
            return path
    except drive.DriveError:
        return None
    return None


@router.get("/lectures/{lecture_id}/transcript")
def lecture_transcript(lecture_id: int, db: Session = Depends(get_db)):
    lecture = db.get(Lecture, lecture_id)
    if not lecture:
        raise HTTPException(404, "Aula não encontrada")
    path = _materialize(lecture, lecture.transcript_filename)
    if not path:
        raise HTTPException(404, "Transcrição ainda não disponível")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@router.get("/lectures/{lecture_id}/pdf")
def lecture_pdf(lecture_id: int, db: Session = Depends(get_db)):
    lecture = db.get(Lecture, lecture_id)
    if not lecture:
        raise HTTPException(404, "Aula não encontrada")
    path = _materialize(lecture, lecture.summary_filename)
    if not path:
        raise HTTPException(404, "PDF ainda não disponível")
    return FileResponse(path, media_type="application/pdf", filename=lecture.summary_filename)


def _busy_with(lecture: Lecture) -> bool:
    snap = pipeline_snapshot()
    if snap["current_lecture_id"] == lecture.id:
        return True
    return snap.get("prefetch_filename") == lecture.video_filename


@router.post("/lectures/{lecture_id}/exclude", response_model=LectureOut)
def exclude_lecture(lecture_id: int, db: Session = Depends(get_db)) -> LectureOut:
    lecture = db.get(Lecture, lecture_id)
    if not lecture:
        raise HTTPException(404, "Aula não encontrada")
    if lecture.status not in {"pending", "failed", "needs_review", "skipped"}:
        raise HTTPException(409, f"Aula em '{lecture.status}' não pode ser excluída da importação")
    if _busy_with(lecture):
        raise HTTPException(409, "Aula em uso pelo pipeline; pause ou cancele antes")
    lecture.status = "excluded"
    lecture.error = "Excluída da importação pelo usuário"
    lecture.updated_at = datetime.utcnow()
    _remove_local_video(lecture)
    db.commit()
    return lecture_out(lecture)


@router.post("/lectures/{lecture_id}/restore", response_model=LectureOut)
def restore_lecture(lecture_id: int, db: Session = Depends(get_db)) -> LectureOut:
    lecture = db.get(Lecture, lecture_id)
    if not lecture or lecture.status != "excluded":
        raise HTTPException(404, "Aula excluída não encontrada")
    if lecture.error == TOO_SMALL_NOTE:
        raise HTTPException(409, "Vídeo menor que o tamanho mínimo; não pode voltar para a fila")
    lecture.status = "pending"
    lecture.error = ""
    lecture.updated_at = datetime.utcnow()
    db.commit()
    return lecture_out(lecture)


@router.delete("/lectures/{lecture_id}/download")
def delete_download(lecture_id: int, db: Session = Depends(get_db)) -> dict:
    """Apaga só o MP4 local em work/; transcrição, PDF e registro ficam."""
    lecture = db.get(Lecture, lecture_id)
    if not lecture:
        raise HTTPException(404, "Aula não encontrada")
    if _busy_with(lecture) or lecture.status in {"downloading", "identifying", "transcribing", "summarizing", "uploading"}:
        raise HTTPException(409, "Aula em uso pelo pipeline; pause ou cancele antes")
    return {"removed": _remove_local_video(lecture)}


@router.post("/pipeline/start")
async def pipeline_start(db: Session = Depends(get_db)) -> dict:
    """Modo automático: processa a fila inteira, da aula mais recente para a mais antiga."""
    require_speakers(db)
    if pipeline_snapshot()["running"]:
        raise HTTPException(409, "Pipeline já em execução")
    set_pipeline(running=True, message="Iniciando processamento automático", stage="sync", auto=True)

    async def _job() -> None:
        job_db = SessionLocal()
        try:
            await run_batch(job_db, max_items=UNLIMITED)
        finally:
            job_db.close()

    asyncio.create_task(_job())
    return {"ok": True}


@router.post("/pipeline/pause")
def pipeline_pause() -> dict:
    if not pipeline_snapshot()["running"]:
        raise HTTPException(409, "Nada em execução")
    pause_event.set()
    set_pipeline(pause_requested=True, message="Pausando ao fim da aula atual…")
    return {"ok": True}


@router.post("/pipeline/cancel")
def pipeline_cancel() -> dict:
    if not pipeline_snapshot()["running"]:
        raise HTTPException(409, "Nada em execução")
    cancel_event.set()
    set_pipeline(message="Cancelando…")
    return {"ok": True}


@router.post("/pipeline/continue/{lecture_id}")
async def pipeline_continue(lecture_id: int, from_stage: str | None = None, db: Session = Depends(get_db)) -> dict:
    """Reprocessa a aula; `from_stage` (transcribe|summarize|upload) refaz dali em diante."""
    if pipeline_snapshot()["running"]:
        raise HTTPException(409, "Pipeline já em execução")
    if from_stage:
        lecture = db.get(Lecture, lecture_id)
        if not lecture:
            raise HTTPException(404, "Aula não encontrada")
        if from_stage not in RESTART_STAGES:
            raise HTTPException(400, f"Etapa inválida: {from_stage}")
        if lecture.status in {"downloading", "identifying", "transcribing", "summarizing", "uploading"}:
            raise HTTPException(409, "Aula em uso pelo pipeline; pause ou cancele antes")
        try:
            prepare_restart(lecture, from_stage)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        lecture.status = "pending"
        db.commit()

    async def _job() -> None:
        db = SessionLocal()
        try:
            with pipeline_lock():
                lecture = db.get(Lecture, lecture_id)
                if not lecture:
                    return
                try:
                    await process_one(db, lecture)
                finally:
                    set_pipeline(running=False, current_lecture_id=None, current_filename=None)
        except PipelineBusy:
            set_pipeline(message="Pipeline já em execução")
        except Exception as exc:
            log.exception("falha ao continuar a aula %s", lecture_id)
            set_pipeline(message=f"Erro: {exc}", stage="failed")
        finally:
            db.close()

    asyncio.create_task(_job())
    return {"ok": True}
