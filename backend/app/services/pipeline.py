from __future__ import annotations

import asyncio
import fcntl
import logging
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, Speaker
from backend.app.runtime import cancel_event, pause_event, pipeline_snapshot, set_pipeline, update_runtime
from backend.app.services import drive
from backend.app.services.identify import identify_speaker
from backend.app.services.obsidian import export_lecture
from backend.app.services.pdf import build_summary, render_pdf, summary_plaintext
from backend.app.services.speakers import import_fotos
from backend.app.services.transcribe import TranscribeCancelled, extract_frames, transcribe_video

log = logging.getLogger("bible_notely.pipeline")
_lock_path = settings.data_dir / "pipeline.lock"
_thread_lock = threading.Lock()


class PipelineBusy(RuntimeError):
    pass


def _touch(lecture: Lecture) -> None:
    lecture.updated_at = datetime.utcnow()


def work_dirname(filename: str) -> str:
    """Nome da pasta em work/: o do vídeo sem a extensão, para achar a aula sem o id do Drive."""
    name = Path(filename or "").stem.replace("\x00", "").strip().rstrip(".")
    name = name.replace("/", "_").replace("\\", "_")
    return name or "sem-nome"


_duplicate_names: set[str] | None = None


def duplicate_video_names() -> set[str]:
    """Nomes que aparecem em mais de uma aula. Cacheado; o lote não cria aula no meio."""
    global _duplicate_names
    if _duplicate_names is not None:
        return _duplicate_names
    from sqlalchemy import func

    from backend.app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(Lecture.video_filename)
            .group_by(Lecture.video_filename)
            .having(func.count(Lecture.id) > 1)
            .all()
        )
        _duplicate_names = {name for (name,) in rows if name}
    except Exception:
        _duplicate_names = set()
    finally:
        db.close()
    return _duplicate_names


def lecture_work_dir(lecture: Lecture) -> Path:
    name = work_dirname(lecture.video_filename)
    if lecture.video_filename in duplicate_video_names():
        stamp = lecture.lecture_date.isoformat() if lecture.lecture_date else (lecture.drive_file_id or "sem-id")[:12]
        name = f"{name} {stamp}"
    return settings.work_dir / name


def _work_paths(lecture: Lecture) -> tuple[Path, Path, Path, Path]:
    work = lecture_work_dir(lecture)
    video_path = work / lecture.video_filename
    transcript_path = work / lecture.transcript_filename
    pdf_path = work / lecture.summary_filename
    return work, video_path, transcript_path, pdf_path


_prefetch: dict[int, asyncio.Task] = {}


async def _prefetch_video(
    lecture_id: int,
    file_id: str,
    video_path: Path,
    size: int,
    name: str,
    md5: str = "",
) -> None:
    """Baixa o próximo MP4 enquanto o Whisper trabalha. Não mexe no banco; falha vira download normal depois."""
    try:
        if _video_ready(video_path, size):
            return
        drive.ensure_free_space(size)
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.unlink(missing_ok=True)
        last = {"pct": -1}

        def _progress(fraction: float) -> None:
            pct = int(fraction * 100)
            if pct != last["pct"]:
                last["pct"] = pct
                set_pipeline(prefetch_progress=pct)

        set_pipeline(prefetch_filename=name, prefetch_progress=0)
        log.info("baixando %s adiantado, durante a transcrição", name)
        await asyncio.to_thread(
            drive.download_file,
            file_id,
            video_path,
            size,
            _progress,
            cancel_event,
            md5,
        )
    except Exception as exc:
        video_path.unlink(missing_ok=True)
        log.warning("download adiantado de %s não concluiu: %s", name, exc)
    finally:
        set_pipeline(prefetch_filename=None, prefetch_progress=None)


def _video_ready(path: Path, expected: int) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    if expected and path.stat().st_size < expected * 0.98:
        return False
    return True


@contextmanager
def pipeline_lock():
    """Lock de processo (cron) e de thread (API). Não reentra no mesmo processo."""
    if not _thread_lock.acquire(blocking=False):
        raise PipelineBusy("Pipeline já em execução")
    _lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path.open("w")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        _thread_lock.release()
        raise PipelineBusy("Pipeline já em execução") from None
    try:
        yield
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
        _thread_lock.release()


TOO_SMALL_NOTE = "vídeo abaixo do tamanho mínimo (MIN_VIDEO_BYTES)"


def _remove_local_video(lecture: Lecture) -> bool:
    _, video_path, _, _ = _work_paths(lecture)
    existed = video_path.exists()
    video_path.unlink(missing_ok=True)
    return existed


def sync_drive_videos(db: Session, *, force: bool = False) -> int:
    """Importa o Drive. Sem force, uma checagem de alterações substitui a varredura se nada mudou."""
    if not force and not drive.should_list():
        log.info("Drive sem alteração; busca de pastas pulada")
        return 0
    token = ""
    try:
        token = drive.change_token()
    except drive.DriveError as exc:
        log.warning("token de alterações indisponível (%s); a busca segue sem checkpoint", exc)
    created = 0
    items = drive.list_videos()
    drive.commit_listing(token)
    update_runtime(drive_video_count=len(items))
    for item in items:
        existing = db.query(Lecture).filter_by(drive_file_id=item["id"]).one_or_none()
        warning = item.get("date_warning") or ""
        if existing:
            if item.get("size"):
                existing.video_size = item["size"]
            if item.get("md5"):
                existing.video_md5 = item["md5"]
            if warning:
                existing.warning = warning
            if item.get("pattern_ok") and existing.status == "skipped" and (existing.error or "").startswith(
                "pasta fora do padrão"
            ):
                existing.status = "pending"
                existing.error = ""
                existing.lecture_date = item.get("lecture_date")
                existing.warning = warning
            elif not item.get("pattern_ok") and existing.status == "pending":
                existing.status = "skipped"
                existing.error = warning
            continue
        stem = Path(item["name"]).stem
        pattern_ok = bool(item.get("pattern_ok"))
        lecture = Lecture(
            drive_file_id=item["id"],
            drive_folder_id=item["folder_id"],
            drive_path=item["path"],
            video_filename=item["name"],
            transcript_filename=f"{stem}.txt",
            summary_filename=f"{stem}-resumo.pdf",
            video_size=item.get("size") or 0,
            video_md5=item.get("md5") or "",
            lecture_date=item.get("lecture_date"),
            warning=warning,
            status="pending" if pattern_ok else "skipped",
            error="" if pattern_ok else warning,
        )
        has_outputs = item.get("has_outputs")
        if has_outputs is None:
            has_outputs = drive.folder_has_outputs(item["folder_id"], stem)
        if pattern_ok and has_outputs:
            lecture.transcribed = True
            lecture.summarized = True
            lecture.uploaded = True
            lecture.status = "done"
        elif not pattern_ok:
            log.info("pasta fora do padrão, marcada skipped: %s", item["path"])
        db.add(lecture)
        created += 1
    small = (
        db.query(Lecture)
        .filter(
            Lecture.status.in_(("pending", "failed", "needs_review", "skipped")),
            Lecture.video_size < settings.min_video_bytes,
        )
        .all()
    )
    for lecture in small:
        lecture.status = "excluded"
        lecture.error = TOO_SMALL_NOTE
        _touch(lecture)
        _remove_local_video(lecture)
    db.commit()
    return created


STAGE_LABELS = {
    "download": "download do vídeo",
    "identify": "identificação do palestrante",
    "transcribe": "transcrição (Whisper)",
    "summarize": "geração do resumo/PDF",
    "upload": "envio ao Drive",
}
DOWNLOAD_ATTEMPTS = 5
DOWNLOAD_BACKOFF = 15
MAX_CONSECUTIVE_FAILURES = 3
UNLIMITED = 10**9
IN_PROGRESS = ("downloading", "identifying", "transcribing", "summarizing", "uploading")


def recover_interrupted(db: Session) -> int:
    """Com o lock em mãos, nada está rodando: status de etapa é sobra de execução interrompida."""
    stale = db.query(Lecture).filter(Lecture.status.in_(IN_PROGRESS)).all()
    for lecture in stale:
        log.warning("aula %s estava em %s; voltando para pending", lecture.id, lecture.status)
        lecture.status = "pending"
        _touch(lecture)
        _remove_incomplete_video(lecture)
    db.commit()
    return len(stale)


def _remove_incomplete_video(lecture: Lecture) -> bool:
    _, video_path, _, _ = _work_paths(lecture)
    if not video_path.exists():
        return False
    if _video_ready(video_path, lecture.video_size):
        return False
    log.warning("apagando MP4 incompleto: %s", video_path)
    video_path.unlink(missing_ok=True)
    return True


def reset_incomplete_work(db: Session) -> list[str]:
    """Apaga MP4 local de aula que não terminou, para o próximo lote baixar de novo."""
    removed: list[str] = []
    rows = db.query(Lecture).filter(Lecture.status != "done").all()
    for lecture in rows:
        if lecture.status in IN_PROGRESS:
            lecture.status = "pending"
            lecture.error = ""
            _touch(lecture)
        _, video_path, transcript_path, pdf_path = _work_paths(lecture)
        if video_path.is_file():
            log.warning("eliminando trabalho incompleto: %s", video_path)
            video_path.unlink()
            removed.append(str(video_path))
        if not lecture.transcribed and transcript_path.is_file():
            transcript_path.unlink()
        if not lecture.summarized and pdf_path.is_file():
            pdf_path.unlink()
    db.commit()
    return removed


def recover_if_idle(db: Session) -> int:
    """Devolve aulas presas em etapa quando o pipeline não está rodando (status, painel, CLI)."""
    if pipeline_snapshot().get("running"):
        return 0
    if not db.query(Lecture).filter(Lecture.status.in_(IN_PROGRESS)).first():
        return 0
    try:
        with pipeline_lock():
            return recover_interrupted(db)
    except PipelineBusy:
        return 0


def next_pending(db: Session, skip_ids: set[int] | None = None) -> Lecture | None:
    """Pendentes antes de falhas, para um item failed não travar a fila diária."""
    skip_ids = skip_ids or set()

    def _query(status: str):
        query = db.query(Lecture).filter(Lecture.status == status)
        if skip_ids:
            query = query.filter(~Lecture.id.in_(skip_ids))
        return query

    pending = _query("pending").order_by(Lecture.lecture_date.desc(), Lecture.id.desc()).first()
    if pending:
        return pending
    return _query("failed").order_by(Lecture.updated_at.asc(), Lecture.id.asc()).first()


RESTART_STAGES = ("transcribe", "summarize", "upload")


def prepare_restart(lecture: Lecture, stage: str) -> None:
    """Invalida o que vem a partir de `stage`; o process_one reaproveita o que ficou válido antes dele."""
    if stage not in RESTART_STAGES:
        raise ValueError(f"Etapa desconhecida: {stage}")
    _, _, transcript_path, pdf_path = _work_paths(lecture)
    if stage == "transcribe":
        transcript_path.unlink(missing_ok=True)
        lecture.transcribed = False
    elif not lecture.transcribed and not (transcript_path.exists() and transcript_path.stat().st_size > 0):
        raise ValueError("Aula sem transcrição; reinicie a partir da transcrição")
    if stage in {"transcribe", "summarize"}:
        lecture.summarized = False
    elif not lecture.summarized or not pdf_path.exists():
        raise ValueError("Aula sem PDF gerado; reinicie a partir do resumo")
    lecture.uploaded = False
    lecture.error = ""
    _touch(lecture)


def _load_transcript(lecture: Lecture, transcript_path: Path) -> str | None:
    if transcript_path.exists() and transcript_path.stat().st_size > 0:
        return transcript_path.read_text(encoding="utf-8")
    if not lecture.transcribed or not lecture.drive_folder_id:
        return None
    try:
        found = drive.download_named(lecture.drive_folder_id, lecture.transcript_filename, transcript_path)
    except drive.DriveError:
        log.info("transcrição não baixada do Drive: %s", lecture.transcript_filename)
        return None
    if not found:
        return None
    return transcript_path.read_text(encoding="utf-8")


async def process_one(
    db: Session, lecture: Lecture, on_transcribe: Callable[[], None] | None = None
) -> Lecture:
    work, video_path, transcript_path, pdf_path = _work_paths(lecture)
    work.mkdir(parents=True, exist_ok=True)
    frame_dir = settings.frames_dir / str(lecture.id)
    stem = Path(lecture.video_filename).stem

    cancel_event.clear()
    set_pipeline(
        running=True,
        progress=None,
        current_lecture_id=lecture.id,
        current_filename=lecture.video_filename,
        stage="download",
        message=f"Preparando {lecture.video_filename}",
    )
    lecture.error = ""
    _touch(lecture)
    db.commit()

    try:
        if lecture.status == "skipped":
            set_pipeline(stage="skipped", message=lecture.warning or "Pasta fora do padrão")
            return lecture

        if lecture.uploaded:
            lecture.status = "done"
            _touch(lecture)
            db.commit()
            video_path.unlink(missing_ok=True)
            set_pipeline(stage="done", message=f"Já enviado: {lecture.video_filename}")
            return lecture

        held_for_review = False
        speaker_ready = lecture.identification_status in {"reviewed", "auto"} and bool(lecture.speaker_id)
        transcript = _load_transcript(lecture, transcript_path) if speaker_ready else None
        if not speaker_ready or transcript is None:
            set_pipeline(stage="download", message=f"Baixando {lecture.video_filename}")
            ahead = _prefetch.pop(lecture.id, None)
            if ahead:
                set_pipeline(message=f"Aguardando download adiantado de {lecture.video_filename}")
                await ahead
            if not _video_ready(video_path, lecture.video_size):
                drive.ensure_free_space(lecture.video_size)
                lecture.status = "downloading"
                _touch(lecture)
                db.commit()
                name = lecture.video_filename
                last = {"pct": -1}

                def _progress(fraction: float) -> None:
                    pct = int(fraction * 100)
                    if pct != last["pct"]:
                        last["pct"] = pct
                        set_pipeline(progress=pct, message=f"Baixando {name}: {pct}%")

                for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                    if video_path.exists():
                        log.warning("removendo download incompleto: %s", video_path.name)
                        video_path.unlink()
                    set_pipeline(progress=0)
                    try:
                        await asyncio.to_thread(
                            drive.download_file,
                            lecture.drive_file_id,
                            video_path,
                            lecture.video_size,
                            _progress,
                            cancel_event,
                            lecture.video_md5 or "",
                        )
                        break
                    except (drive.DriveIncomplete, drive.DriveTransient) as exc:
                        if attempt == DOWNLOAD_ATTEMPTS:
                            raise
                        delay = DOWNLOAD_BACKOFF * attempt
                        log.warning(
                            "download de %s falhou (%s); tentativa %s/%s em %ss",
                            name, exc, attempt + 1, DOWNLOAD_ATTEMPTS, delay,
                        )
                        set_pipeline(message=f"Falha ao baixar {name}; nova tentativa em {delay}s")
                        await asyncio.sleep(delay)
                    except Exception:
                        video_path.unlink(missing_ok=True)
                        raise
                set_pipeline(progress=None)
            else:
                log.info("vídeo local reaproveitado: %s", video_path.name)

        if not speaker_ready:
            set_pipeline(stage="identify", message="Identificando palestrante")
            lecture.status = "identifying"
            _touch(lecture)
            db.commit()
            frames = await asyncio.to_thread(extract_frames, video_path, frame_dir, stem)
            lecture.frame_path = str(frames[len(frames) // 2])
            speakers = db.query(Speaker).filter_by(active=True).all()
            result = await identify_speaker(frames, speakers)
            lecture.speaker_id = result["speaker_id"]
            lecture.speaker_confidence = result["confidence"]
            lecture.identification_status = result["status"]
            lecture.identification_note = result["note"]
            _touch(lecture)
            db.commit()
            if result["status"] in {"needs_review", "unknown"}:
                held_for_review = True
                log.info("aula %s precisa de revisão; transcrevendo antes de liberar o MP4", lecture.id)

        set_pipeline(stage="transcribe", message="Transcrevendo com Whisper")
        lecture.status = "transcribing"
        _touch(lecture)
        db.commit()
        if transcript is None:
            transcript = _load_transcript(lecture, transcript_path)
        if transcript is None:
            if not video_path.exists():
                raise drive.DriveError("vídeo local ausente e transcrição não encontrada")
            def _transcribe_progress(fraction: float) -> None:
                pct = int(fraction * 100)
                if pct != set_pipeline_progress["pct"]:
                    set_pipeline_progress["pct"] = pct
                    set_pipeline(progress=pct, message=f"Transcrevendo {lecture.video_filename}: {pct}%")

            set_pipeline_progress = {"pct": -1}
            set_pipeline(progress=0)
            if on_transcribe:
                on_transcribe()
            transcript = await asyncio.to_thread(transcribe_video, video_path, _transcribe_progress, cancel_event)
            set_pipeline(progress=None)
            transcript_path.write_text(transcript, encoding="utf-8")
        else:
            log.info("transcrição reaproveitada: %s", transcript_path.name)
        lecture.transcribed = True
        _touch(lecture)
        db.commit()

        if held_for_review:
            lecture.status = "needs_review"
            _touch(lecture)
            db.commit()
            video_path.unlink(missing_ok=True)
            set_pipeline(stage="needs_review", message="Aguardando revisão do palestrante")
            log.info("aula %s em revisão; transcrição salva, MP4 removido", lecture.id)
            return lecture

        speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
        pdf_current = (
            lecture.summarized
            and pdf_path.exists()
            and pdf_path.stat().st_size > 0
            and lecture.summary_speaker_id == lecture.speaker_id
        )
        rebuilt_pdf = False
        if pdf_current:
            log.info("PDF reaproveitado: %s", pdf_path.name)
        else:
            set_pipeline(stage="summarize", message="Gerando resumo em PDF")
            lecture.status = "summarizing"
            _touch(lecture)
            db.commit()
            summary = await build_summary(lecture, speaker, transcript)
            lecture.summary_text = summary_plaintext(summary)
            render_pdf(pdf_path, lecture, speaker, summary)
            try:
                export_lecture(lecture, speaker, summary)
            except Exception:
                log.exception("falha ao exportar markdown de %s", lecture.video_filename)
            lecture.summarized = True
            lecture.summary_speaker_id = lecture.speaker_id
            rebuilt_pdf = True
            _touch(lecture)
            db.commit()

        if not lecture.uploaded or rebuilt_pdf:
            set_pipeline(stage="upload", message="Enviando transcrição e PDF ao Drive")
            lecture.status = "uploading"
            _touch(lecture)
            db.commit()
            await asyncio.to_thread(drive.upload_file, transcript_path, lecture.drive_folder_id, "text/plain")
            await asyncio.to_thread(drive.upload_file, pdf_path, lecture.drive_folder_id, "application/pdf")
        lecture.uploaded = True
        lecture.status = "done"
        _touch(lecture)
        db.commit()
        video_path.unlink(missing_ok=True)
        log.info("PDF da aula mantido em %s", pdf_path)
        set_pipeline(stage="done", message=f"Concluído: {lecture.video_filename}")
        return lecture
    except (drive.DriveCancelled, TranscribeCancelled):
        lecture.status = "pending"
        lecture.error = ""
        _touch(lecture)
        db.commit()
        set_pipeline(stage=None, progress=None, message="Processamento cancelado")
        log.info("processamento de %s cancelado", lecture.video_filename)
        return lecture
    except Exception as exc:
        stage = pipeline_snapshot().get("stage")
        where = STAGE_LABELS.get(stage, stage or "etapa desconhecida")
        detail = str(exc) or type(exc).__name__
        lecture.status = "failed"
        lecture.error = f"Falhou em {where} ({type(exc).__name__}): {detail}"
        _touch(lecture)
        db.commit()
        set_pipeline(stage="failed", message=lecture.error)
        log.exception("falha ao processar %s", lecture.video_filename)
        return lecture


async def run_batch(db: Session, max_items: int = 1) -> int:
    try:
        with pipeline_lock():
            return await _run_locked(db, max_items)
    except PipelineBusy:
        set_pipeline(message="Pipeline já em execução", running=False, auto=False)
        return 0
    except Exception as exc:
        log.exception("falha no processamento")
        set_pipeline(message=f"Erro: {exc}", stage="failed")
        return 0


async def _run_locked(db: Session, max_items: int) -> int:
    try:
        set_pipeline(running=True, message="Importando fotos de palestrantes", stage="sync")
        imported = import_fotos(db)
        if imported["errors"]:
            log.warning("fotos não importadas: %s", "; ".join(imported["errors"]))
        set_pipeline(running=True, message="Sincronizando Drive", stage="sync")
        sync_drive_videos(db)
        recover_interrupted(db)
        processed = 0
        failures = 0
        seen: set[int] = set()
        pause_event.clear()
        set_pipeline(auto=max_items >= UNLIMITED, pause_requested=False)
        while processed < max_items:
            if pause_event.is_set():
                set_pipeline(message="Pausado. Clique em continuar para retomar.", stage=None)
                break
            lecture = next_pending(db, skip_ids=seen)
            if not lecture:
                break
            seen.add(lecture.id)
            def _start_prefetch(current_id: int = lecture.id) -> None:
                # Sem limite pelo tamanho do lote: se a aula cair em revisão o loop segue para a próxima,
                # e o MP4 baixado fica pronto para a próxima execução (_video_ready).
                if pause_event.is_set():
                    return
                nxt = (
                    db.query(Lecture)
                    .filter(Lecture.status == "pending", Lecture.id.notin_(seen | {current_id}))
                    .order_by(Lecture.lecture_date.desc(), Lecture.id.desc())
                    .first()
                )
                if not nxt or nxt.id in _prefetch:
                    return
                _, nxt_video, _, _ = _work_paths(nxt)
                _prefetch[nxt.id] = asyncio.create_task(
                    _prefetch_video(
                        nxt.id,
                        nxt.drive_file_id,
                        nxt_video,
                        nxt.video_size,
                        nxt.video_filename,
                        nxt.video_md5 or "",
                    )
                )

            await process_one(db, lecture, on_transcribe=_start_prefetch)
            db.refresh(lecture)
            if cancel_event.is_set():
                break
            if lecture.status == "failed":
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("%s falhas seguidas; processamento automático interrompido", failures)
                    set_pipeline(message=f"Interrompido após {failures} falhas seguidas. Veja o erro da aula.", stage="failed")
                    break
            else:
                failures = 0
            if lecture.status == "needs_review":
                continue
            processed += 1
        if processed == 0:
            set_pipeline(message="Nada novo para processar", stage=None)
        return processed
    finally:
        leftovers = [task for task in _prefetch.values() if not task.done()]
        if leftovers:
            # Fim normal do lote: deixa o download adiantado terminar para a próxima execução reaproveitá-lo.
            # Cancelamento do usuário (cancel_event já ativo) ou erro: aborta.
            if sys.exc_info()[0] is not None:
                cancel_event.set()
            await asyncio.gather(*leftovers, return_exceptions=True)
            cancel_event.clear()
        _prefetch.clear()
        set_pipeline(auto=False, pause_requested=False, running=False, current_lecture_id=None, current_filename=None)
