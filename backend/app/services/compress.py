"""Reencode do MP4 local depois da transcrição.

O Whisper e o reconhecimento de rosto continuam no arquivo da câmera. Este passo
grava `{stem}.compressed.mp4` ao lado do original e só então marca a aula. A
duração (ffprobe) precisa bater com a origem. O original local só sai depois
que o resto da aula termina; o arquivo da câmera continua no Drive. O
comprimido fica.

O arquivo comprimido é o que o YouTube usa e o que um futuro Bunny Stream ou
backup deve copiar. O perfil (codec, CRF, preset, altura, áudio) entra no
SQLite: se o arquivo existe, a duração confere e o perfil é o mesmo, o passo
é pulado.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture
from backend.app.runtime import runtime_get
from backend.app.services.transcribe import video_duration

log = logging.getLogger("bible_notely.compress")

PRESETS = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)
CODECS = {"h264": "libx264", "h265": "libx265"}
_BITRATE = re.compile(r"^(\d+)k$")


class CompressError(RuntimeError):
    pass


class CompressCancelled(CompressError):
    pass


@dataclass(frozen=True)
class CompressConfig:
    enabled: bool = True
    crf: int = 23
    preset: str = "veryfast"
    max_height: int = 0
    codec: str = "h264"
    audio: str = "copy"
    audio_bitrate: str = "128k"
    delete_original: bool = True
    upload_drive: bool = False
    duration_tolerance: float = 1.0


@dataclass(frozen=True)
class CompressResult:
    status: str  # compressed | skipped | missing
    output: Path | None = None
    original_size: int = 0
    compressed_size: int = 0
    profile: str = ""


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "sim"}:
        return True
    if text in {"0", "false", "no", "off", "nao", "não"}:
        return False
    return default


def _as_int(value: object, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: object, default: str) -> str:
    if value is None or value == "":
        return default
    return str(value).strip()


def merged_config() -> CompressConfig:
    """Env como base; o que estiver em data/runtime.json (Ajustes) ganha."""

    def pick(key: str) -> object:
        return runtime_get(key)

    return CompressConfig(
        enabled=_as_bool(pick("compress_enabled"), settings.compress_enabled)
        if pick("compress_enabled") is not None
        else settings.compress_enabled,
        crf=_as_int(pick("compress_crf"), settings.compress_crf),
        preset=_as_text(pick("compress_preset"), settings.compress_preset),
        max_height=_as_int(pick("compress_max_height"), settings.compress_max_height),
        codec=_as_text(pick("compress_codec"), settings.compress_codec),
        audio=_as_text(pick("compress_audio"), settings.compress_audio),
        audio_bitrate=_as_text(pick("compress_audio_bitrate"), settings.compress_audio_bitrate),
        delete_original=_as_bool(pick("compress_delete_original"), settings.compress_delete_original)
        if pick("compress_delete_original") is not None
        else settings.compress_delete_original,
        upload_drive=_as_bool(pick("compress_upload_drive"), settings.compress_upload_drive)
        if pick("compress_upload_drive") is not None
        else settings.compress_upload_drive,
        duration_tolerance=_as_float(
            pick("compress_duration_tolerance"), settings.compress_duration_tolerance
        ),
    )


def load_compress_config() -> CompressConfig:
    return validate_config(merged_config())


def validate_config(config: CompressConfig) -> CompressConfig:
    codec = config.codec.strip().lower().replace(".", "")
    if codec in {"hevc", "x265"}:
        codec = "h265"
    if codec in {"avc", "x264"}:
        codec = "h264"
    preset = config.preset.strip().lower()
    audio = config.audio.strip().lower()
    bitrate = config.audio_bitrate.strip().lower()
    try:
        crf = int(config.crf)
        max_height = int(config.max_height)
        tolerance = float(config.duration_tolerance)
    except (TypeError, ValueError) as exc:
        raise CompressError("ajuste de compressão inválido") from exc
    if codec not in CODECS:
        raise CompressError("codec deve ser h264 ou h265")
    if preset not in PRESETS:
        raise CompressError(f"preset inválido: {preset}")
    if not 0 <= crf <= 51:
        raise CompressError("CRF deve estar entre 0 e 51")
    if max_height != 0 and not 144 <= max_height <= 2160:
        raise CompressError("altura máxima deve ser 0 (original) ou entre 144 e 2160")
    if audio not in {"copy", "aac"}:
        raise CompressError("áudio deve ser copy ou aac")
    if audio == "aac":
        match = _BITRATE.fullmatch(bitrate)
        if not match or not 32 <= int(match.group(1)) <= 320:
            raise CompressError("bitrate de áudio inválido (use 96k ou 128k)")
    if tolerance < 0 or tolerance > 30:
        raise CompressError("tolerância de duração deve estar entre 0 e 30 segundos")
    return replace(
        config,
        codec=codec,
        preset=preset,
        audio=audio,
        audio_bitrate=bitrate,
        crf=crf,
        max_height=max_height,
        duration_tolerance=tolerance,
    )


def profile_of(config: CompressConfig) -> str:
    audio = "copy" if config.audio == "copy" else f"aac:{config.audio_bitrate}"
    # fit=ar distingue o teto de altura que preserva a proporção do filtro antigo,
    # que esticava o quadro. Sem teto o perfil não muda, para não reencodar o que já ficou certo.
    fit = ";fit=ar" if config.max_height else ""
    return f"v={config.codec};preset={config.preset};crf={config.crf};h={config.max_height}{fit};a={audio}"


def profile_with_overlay(config: CompressConfig, overlay_token: str = "") -> str:
    """O perfil da compressão pura não muda. A legenda entra só quando há faixa."""
    base = profile_of(config)
    if overlay_token:
        return f"{base};ov={overlay_token}"
    return base


def _profile_matches(stored: str, config: CompressConfig, overlay_token: str | None) -> bool:
    base = profile_of(config)
    if overlay_token is None:
        # Quem só recomprime não descarta um arquivo que já levou legenda.
        return stored == base or stored.startswith(f"{base};ov=")
    if overlay_token:
        return stored == f"{base};ov={overlay_token}"
    return stored == base


def compressed_output_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}.compressed.mp4")


_PROFILE_PREFIX = "bible-notely "


def scale_filter(max_height: int) -> str | None:
    """Limita a altura e mantém a proporção. -2 deixa a largura par.

    Sem teto, não há filtro: o quadro da câmera segue igual.
    """
    if not max_height:
        return None
    return f"scale=-2:'min({int(max_height)},ih)',setsar=1"


def _filter_quote(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"'{escaped}'"


def ass_filter(ass_path: Path) -> str:
    """Queima o ASS com libass no mesmo -vf da escala. A fonte fica explícita se existir."""
    filename = _filter_quote(ass_path.resolve().as_posix())
    fonts = Path("/usr/share/fonts/truetype/dejavu")
    if fonts.is_dir():
        return f"ass={filename}:fontsdir={_filter_quote(fonts.as_posix())}"
    return f"ass={filename}"


def compose_video_filter(max_height: int, ass_path: Path | None = None) -> str | None:
    parts: list[str] = []
    scale = scale_filter(max_height)
    if scale:
        parts.append(scale)
    if ass_path is not None:
        parts.append(ass_filter(ass_path))
    return ",".join(parts) if parts else None


def build_ffmpeg_command(
    source: Path,
    dest: Path,
    config: CompressConfig,
    ass_path: Path | None = None,
    overlay_token: str = "",
) -> list[str]:
    config = validate_config(config)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        CODECS[config.codec],
        "-preset",
        config.preset,
        "-crf",
        str(config.crf),
        "-pix_fmt",
        "yuv420p",
    ]
    vf = compose_video_filter(config.max_height, ass_path)
    if vf:
        cmd += ["-vf", vf]
    if config.codec == "h265":
        cmd += ["-tag:v", "hvc1"]
    if config.audio == "aac":
        cmd += ["-c:a", "aac", "-b:a", config.audio_bitrate]
    else:
        cmd += ["-c:a", "copy"]
    cmd += [
        "-metadata",
        f"comment={_PROFILE_PREFIX}{profile_with_overlay(config, overlay_token)}",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        str(dest),
    ]
    return cmd


def _parse_clock(value: str) -> float | None:
    parts = value.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        return float(value)
    except ValueError:
        return None


def _run_ffmpeg(
    source: Path,
    dest: Path,
    config: CompressConfig,
    progress: Callable[[float], None] | None = None,
    cancel: Event | None = None,
    ass_path: Path | None = None,
    overlay_token: str = "",
) -> None:
    cmd = build_ffmpeg_command(source, dest, config, ass_path=ass_path, overlay_token=overlay_token)
    log.info("ffmpeg %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise CompressError("ffmpeg não está no PATH") from exc
    stderr_parts: list[str] = []

    def _drain() -> None:
        if proc.stderr is not None:
            stderr_parts.append(proc.stderr.read())

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    duration = video_duration(source) or 0
    cancelled = False
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                cancelled = True
                proc.terminate()
                break
            if progress and duration > 0 and line.startswith("out_time="):
                seconds = _parse_clock(line.split("=", 1)[1])
                if seconds is not None:
                    progress(min(0.99, seconds / duration))
        if not cancelled:
            proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        thread.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if cancelled:
        dest.unlink(missing_ok=True)
        raise CompressCancelled("Compressão cancelada")
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size <= 0:
        tail = (stderr_parts[0] if stderr_parts else "")[-800:].strip()
        dest.unlink(missing_ok=True)
        raise CompressError(tail or f"ffmpeg saiu com código {proc.returncode}")
    if progress:
        progress(1.0)


def durations_match(source: Path, dest: Path, tolerance: float) -> tuple[bool, str]:
    src = video_duration(source)
    out = video_duration(dest)
    if src is None or out is None:
        return False, "ffprobe não leu a duração"
    delta = abs(out - src)
    if delta > tolerance:
        return False, f"duração {out:.3f}s difere da origem {src:.3f}s (tolerância {tolerance:g}s)"
    return True, ""


def embedded_profile(path: Path) -> str:
    """Perfil gravado no comment do MP4. Vazio se o arquivo é de uma versão anterior."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=comment",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    text = result.stdout.strip()
    if text.startswith(_PROFILE_PREFIX):
        return text[len(_PROFILE_PREFIX) :]
    return ""


def output_is_valid(
    source: Path | None,
    dest: Path,
    config: CompressConfig,
    *,
    expected_size: int = 0,
    profile: str | None = None,
) -> bool:
    if not dest.is_file():
        return False
    size = dest.stat().st_size
    if size <= 0:
        return False
    if expected_size and size != expected_size:
        return False
    if profile is not None and embedded_profile(dest) != profile:
        return False
    if source is not None and source.is_file():
        ok, _reason = durations_match(source, dest, config.duration_tolerance)
        return ok
    return video_duration(dest) is not None


def store_compressed_path(path: Path) -> str:
    """Caminho relativo a work/ quando o arquivo está lá. Absoluto antigo continua válido."""
    resolved = path.resolve()
    root = settings.work_dir.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def resolve_stored_path(stored: str) -> Path:
    path = Path(stored)
    if not stored:
        return path
    if path.is_absolute():
        return path
    return (settings.work_dir / path).resolve()


def _record_matches(
    lecture: Lecture,
    dest: Path,
    config: CompressConfig,
    overlay_token: str | None = None,
) -> bool:
    if not lecture.compressed:
        return False
    if not _profile_matches(lecture.compressed_profile or "", config, overlay_token):
        return False
    if lecture.compressed_path and resolve_stored_path(lecture.compressed_path) != dest.resolve():
        return False
    return output_is_valid(
        None,
        dest,
        config,
        expected_size=lecture.compressed_size or 0,
    )


def compress_file(
    source: Path,
    dest: Path | None = None,
    config: CompressConfig | None = None,
    progress: Callable[[float], None] | None = None,
    cancel: Event | None = None,
    *,
    reuse: bool = False,
    ass_path: Path | None = None,
    overlay_token: str = "",
) -> CompressResult:
    """Encode para um arquivo ao lado do original. Não apaga a origem.

    `reuse=True` só pula o encode se a duração e o perfil gravado no arquivo
    forem os mesmos. Duração igual com outro CRF, altura ou codec não serve.
    """
    config = validate_config(config or load_compress_config())
    if not source.is_file() or source.stat().st_size <= 0:
        raise CompressError(f"arquivo de origem ausente: {source}")
    dest = dest or compressed_output_path(source)
    if dest.resolve() == source.resolve():
        raise CompressError("o destino da compressão não pode ser o próprio original")
    profile = profile_with_overlay(config, overlay_token)
    if reuse and output_is_valid(source, dest, config, profile=profile):
        log.info("compressão reaproveitada: %s", dest.name)
        return CompressResult(
            status="skipped",
            output=dest,
            original_size=source.stat().st_size,
            compressed_size=dest.stat().st_size,
            profile=profile,
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    partial.unlink(missing_ok=True)
    try:
        _run_ffmpeg(
            source,
            partial,
            config,
            progress,
            cancel,
            ass_path=ass_path,
            overlay_token=overlay_token,
        )
        ok, reason = durations_match(source, partial, config.duration_tolerance)
        if not ok:
            raise CompressError(reason)
    except CompressCancelled:
        partial.unlink(missing_ok=True)
        raise
    except Exception:
        partial.unlink(missing_ok=True)
        if not source.is_file():
            log.error("original sumiu durante a compressão: %s", source)
        raise
    partial.replace(dest)
    original_size = source.stat().st_size
    compressed_size = dest.stat().st_size
    log.info(
        "comprimido %s: %s bytes → %s bytes",
        source.name,
        original_size,
        compressed_size,
    )
    return CompressResult(
        status="compressed",
        output=dest,
        original_size=original_size,
        compressed_size=compressed_size,
        profile=profile,
    )


def _touch(lecture: Lecture) -> None:
    lecture.updated_at = datetime.utcnow()


def _source_complete(path: Path, lecture: Lecture) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    expected = lecture.video_size or 0
    if expected and path.stat().st_size < expected * 0.98:
        return False
    return True


def _dest_for(lecture: Lecture, source: Path | None) -> Path | None:
    if source is not None:
        return compressed_output_path(source)
    if lecture.compressed_path:
        return resolve_stored_path(lecture.compressed_path)
    return None


def preferred_upload_source(lecture: Lecture) -> Path | None:
    """Arquivo comprimido local, se o registro e o tamanho batem."""
    if not lecture.compressed or not lecture.compressed_path:
        return None
    path = resolve_stored_path(lecture.compressed_path)
    if not path.is_file() or path.stat().st_size <= 0:
        return None
    if lecture.compressed_size and path.stat().st_size != lecture.compressed_size:
        return None
    return path


def _upload_drive(path: Path, folder_id: str) -> str:
    from backend.app.services import drive

    return drive.upload_file(path, folder_id, "video/mp4")


def _ensure_space(needed: int) -> None:
    from backend.app.services import drive

    try:
        drive.ensure_free_space(needed)
    except drive.DriveError as exc:
        raise CompressError(str(exc)) from exc


def compress_lecture(
    db: Session,
    lecture: Lecture,
    *,
    source: Path | None,
    config: CompressConfig | None = None,
    progress: Callable[[float], None] | None = None,
    cancel: Event | None = None,
    upload: Callable[[Path, str], str] | None = None,
    ass_path: Path | None = None,
    overlay_token: str | None = None,
) -> CompressResult:
    """Comprime, confere, grava o SQLite e só então apaga ou envia, se pedido."""
    config = validate_config(config or load_compress_config())
    if source is not None and not _source_complete(source, lecture):
        if source.is_file() and lecture.video_size and source.stat().st_size < lecture.video_size * 0.98:
            raise CompressError("MP4 local incompleto; compressão cancelada para não arquivar um download pela metade")
        source = None
    dest = _dest_for(lecture, source)
    if dest is not None and _record_matches(lecture, dest, config, overlay_token) and (
        source is None or output_is_valid(source, dest, config, expected_size=lecture.compressed_size or 0)
    ):
        result = CompressResult(
            status="skipped",
            output=dest,
            original_size=lecture.original_size or (source.stat().st_size if source and source.is_file() else 0),
            compressed_size=dest.stat().st_size,
            profile=lecture.compressed_profile or profile_with_overlay(config, overlay_token or ""),
        )
    elif source is None:
        log.info("sem MP4 local para comprimir: %s", lecture.video_filename)
        return CompressResult(status="missing")
    else:
        _ensure_space(source.stat().st_size)
        # O perfil já foi comparado acima; se chegou aqui, o arquivo velho não serve.
        result = compress_file(
            source,
            dest,
            config,
            progress,
            cancel,
            reuse=False,
            ass_path=ass_path,
            overlay_token=overlay_token or "",
        )
        assert result.output is not None
        lecture.original_size = result.original_size
        lecture.compressed = True
        lecture.compressed_path = store_compressed_path(result.output)
        lecture.compressed_size = result.compressed_size
        lecture.compressed_profile = result.profile
        _touch(lecture)
        db.commit()

    if config.upload_drive and result.output is not None and (result.status == "compressed" or not lecture.compressed_drive_file_id):
        if not lecture.drive_folder_id:
            raise CompressError("pasta do Drive ausente; não dá para enviar o comprimido")
        sender = upload or _upload_drive
        file_id = sender(result.output, lecture.drive_folder_id)
        lecture.compressed_drive_file_id = file_id
        _touch(lecture)
        db.commit()
        log.info("comprimido enviado ao Drive: %s (%s)", result.output.name, file_id)
    return result


def default_source_path(lecture: Lecture) -> Path:
    from backend.app.services.pipeline import lecture_work_dir

    return lecture_work_dir(lecture) / lecture.video_filename


def _fetch_source(lecture: Lecture, dest: Path) -> None:
    from backend.app.services import drive

    if not lecture.drive_file_id:
        raise CompressError(f"aula {lecture.id} sem arquivo local e sem id do Drive")
    _ensure_space(lecture.video_size or 0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    drive.download_file(
        lecture.drive_file_id,
        dest,
        expected_size=lecture.video_size or 0,
        expected_md5=lecture.video_md5 or "",
    )


def discard_camera_copy(source: Path | None, output: Path | None, *, keep: bool) -> None:
    """Apaga a cópia local da câmera. O comprimido fica."""
    if keep or source is None or not source.is_file():
        return
    if output is not None and source.resolve() == output.resolve():
        return
    log.info("apagando original local; o comprimido fica: %s", source.name)
    source.unlink()


def _needs_attention(lecture: Lecture, source: Path | None, config: CompressConfig) -> bool:
    dest = _dest_for(lecture, source)
    current = (
        dest is not None
        and _record_matches(lecture, dest, config, None)
        and (source is None or output_is_valid(source, dest, config, expected_size=lecture.compressed_size or 0))
    )
    if not current:
        return source is not None
    if config.delete_original and source is not None and source.is_file():
        return True
    if config.upload_drive and not lecture.compressed_drive_file_id:
        return True
    return False


def compress_backfill(
    db: Session,
    *,
    lecture_id: int | None = None,
    max_items: int = 1,
    fetch: bool = False,
    config: CompressConfig | None = None,
    source_of: Callable[[Lecture], Path] | None = None,
    upload: Callable[[Path, str], str] | None = None,
) -> tuple[int, int, int, int]:
    """Comprime aulas já existentes. Devolve (comprimidos, pulados, sem_arquivo, falhas)."""
    from backend.app.services.pipeline import IN_PROGRESS

    config = validate_config(config or load_compress_config())
    locate = source_of or default_source_path
    if lecture_id:
        lecture = db.get(Lecture, lecture_id)
        if lecture is None:
            raise CompressError(f"aula {lecture_id} não existe")
        rows = [lecture]
    else:
        rows = (
            db.query(Lecture)
            .filter(Lecture.status.notin_(("excluded", "skipped")))
            .order_by(Lecture.lecture_date.desc(), Lecture.id.desc())
            .all()
        )
    compressed = skipped = missing = failed = 0
    attempts = 0
    limit = max(1, max_items)
    for lecture in rows:
        if lecture_id is None and lecture.status in IN_PROGRESS:
            continue
        source_path = locate(lecture)
        if not _source_complete(source_path, lecture) and fetch:
            try:
                _fetch_source(lecture, source_path)
            except Exception as exc:
                failed += 1
                attempts += 1
                log.exception("falha ao baixar %s para comprimir: %s", lecture.video_filename, exc)
                if lecture_id is not None or attempts >= limit:
                    break
                continue
        source = source_path if _source_complete(source_path, lecture) else None
        if lecture_id is None and not _needs_attention(lecture, source, config):
            continue
        attempts += 1
        try:
            result = compress_lecture(
                db,
                lecture,
                source=source,
                config=config,
                upload=upload,
            )
        except CompressCancelled:
            raise
        except Exception as exc:
            failed += 1
            log.exception("falha ao comprimir %s: %s", lecture.video_filename, exc)
        else:
            if result.status == "compressed":
                compressed += 1
                log.info(
                    "aula %s comprimida: %s bytes → %s bytes",
                    lecture.id,
                    result.original_size,
                    result.compressed_size,
                )
            elif result.status == "skipped":
                skipped += 1
                log.info("aula %s já estava comprimida (%s)", lecture.id, result.output)
            else:
                missing += 1
                log.info("aula %s sem MP4 local", lecture.id)
            if result.status in {"compressed", "skipped"}:
                discard_camera_copy(source, result.output, keep=not config.delete_original)
        if lecture_id is not None or attempts >= limit:
            break
    return compressed, skipped, missing, failed
