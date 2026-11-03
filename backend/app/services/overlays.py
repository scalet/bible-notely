"""Versículos e letras queimados no mesmo encode da compressão.

Pendência sem resposta não trava o pipeline: o prazo de Ajustes (padrão 24 h,
0 = não espera) solta o vídeo com o que já foi encontrado.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, LyricPending
from backend.app.runtime import runtime_get
from backend.app.services.bible import fetch_chapter, load_passage
from backend.app.services.hymnal import find_hymn, load_hymns
from backend.app.services.songlib import find_in_library, load_library, remember_song
from backend.app.services.songs import SongHit, UnknownSinging, detect_songs
from backend.app.services.verses import VerseCue, detect_verse_cues, parse_segments

log = logging.getLogger("bible_notely.overlays")

SNIPPET_SECONDS = 20.0
_FONTS = Path("/usr/share/fonts/truetype/dejavu")


class OverlayError(RuntimeError):
    pass


class OverlayCancelled(OverlayError):
    pass


@dataclass(frozen=True)
class OverlayConfig:
    enabled: bool = True
    wait_hours: float = 24.0


@dataclass
class Cue:
    start: float
    end: float
    kind: str  # verse | lyric
    title: str
    body: str


@dataclass
class OverlayPlan:
    cues: list[Cue]
    waiting: bool
    ass_path: Path | None
    token: str
    open_count: int


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "sim"}:
        return True
    if text in {"0", "false", "no", "off", "nao", "não"}:
        return False
    return default


def load_overlay_config() -> OverlayConfig:
    enabled_raw = runtime_get("overlay_enabled")
    hours_raw = runtime_get("overlay_wait_hours")
    enabled = settings.overlay_enabled if enabled_raw is None else _as_bool(enabled_raw, settings.overlay_enabled)
    if hours_raw is None or hours_raw == "":
        hours = float(settings.overlay_wait_hours)
    else:
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            hours = float(settings.overlay_wait_hours)
    if hours < 0:
        hours = 0.0
    return OverlayConfig(enabled=enabled, wait_hours=hours)


def recording_kind(filename: str) -> str:
    """Antes das 10h é EBD, até as 12h o culto da manhã, depois o da noite.

    A legenda entra nos três. A hora vem do nome do arquivo, como no YouTube.
    """
    from colab.youtube_lib import PLAYLIST_CULT_MANHA, PLAYLIST_CULT_NOITE, classify_playlist, resolve_event_time

    playlist = classify_playlist(resolve_event_time(filename))
    if playlist == PLAYLIST_CULT_MANHA:
        return "culto_manha"
    if playlist == PLAYLIST_CULT_NOITE:
        return "culto_noite"
    return "ebd"


def clock_label(seconds: float) -> str:
    total = int(max(seconds, 0))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def ass_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    centis = int(round(seconds * 100))
    whole, centis = divmod(centis, 100)
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def present_body(text: str, width: int = 42, max_lines: int = 4) -> str:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        words = raw.split()
        if not words:
            continue
        current = ""
        for word in words:
            trial = word if not current else f"{current} {word}"
            if len(trial) > width and current:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .") + "…"
    return "\\N".join(_escape_ass(line) for line in lines)


def render_ass(cues: list[Cue]) -> str:
    """Lower-third: faixa escura semitransparente, referência em negrito, texto embaixo."""
    events = []
    for cue in cues:
        if cue.end <= cue.start:
            continue
        body = present_body(cue.body)
        if not body:
            continue
        title = _escape_ass(cue.title)
        text = f"{{\\an2\\q2}}{{\\b1\\fs28}}{title}{{\\b0\\fs32}}\\N{body}"
        events.append(
            f"Dialogue: 0,{ass_timestamp(cue.start)},{ass_timestamp(cue.end)},Lower,,0,0,0,,{text}"
        )
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lower,DejaVu Sans,32,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,14,0,2,80,80,52,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return header + "\n".join(events) + ("\n" if events else "")


def cue_token(cues: list[Cue]) -> str:
    if not cues:
        return ""
    payload = "|".join(f"{cue.kind}:{cue.start:.2f}:{cue.end:.2f}:{cue.title}:{cue.body}" for cue in cues)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def lyric_cues_for_span(title: str, number: str, stanzas: tuple[str, ...] | list[str], start: float, end: float) -> list[Cue]:
    """Uma faixa por estrofe, no intervalo do canto. Nunca um bloco único com o hino inteiro."""
    parts = [part.strip() for part in stanzas if part and part.strip()]
    if not parts:
        return []
    label = f"CC {number} · {title}" if number else title
    span = max(end - start, len(parts) * 6)
    end = start + span
    weights = [max(len(part), 24) for part in parts]
    total = sum(weights) or 1
    cursor = start
    cues: list[Cue] = []
    for index, (part, weight) in enumerate(zip(parts, weights)):
        if index == len(parts) - 1:
            piece_end = end
        else:
            piece_end = cursor + max(6.0, span * weight / total)
        cues.append(Cue(start=cursor, end=piece_end, kind="lyric", title=label, body=part))
        cursor = piece_end
    return cues


def pack_cues(cues: list[Cue]) -> list[Cue]:
    """Versículo tem prioridade se cruzar com a letra. Os dois continuam legíveis."""
    ordered = sorted(cues, key=lambda cue: (cue.start, 0 if cue.kind == "verse" else 1))
    packed: list[Cue] = []
    for cue in ordered:
        if packed and cue.start < packed[-1].end - 0.2:
            if packed[-1].kind == "verse" and cue.kind == "lyric":
                cue = Cue(
                    start=packed[-1].end,
                    end=max(cue.end, packed[-1].end + 6),
                    kind=cue.kind,
                    title=cue.title,
                    body=cue.body,
                )
            else:
                previous = packed[-1]
                packed[-1] = Cue(
                    start=previous.start,
                    end=max(previous.start + 2, cue.start),
                    kind=previous.kind,
                    title=previous.title,
                    body=previous.body,
                )
        if cue.end - cue.start >= 1.5:
            packed.append(cue)
    return packed


def open_pendings(db: Session, lecture_id: int) -> list[LyricPending]:
    return (
        db.query(LyricPending)
        .filter(LyricPending.lecture_id == lecture_id, LyricPending.status == "open")
        .order_by(LyricPending.start_seconds, LyricPending.id)
        .all()
    )


def actionable_pendings(db: Session) -> list[LyricPending]:
    return (
        db.query(LyricPending)
        .filter(LyricPending.status.in_(("open", "timeout")))
        .order_by(LyricPending.created_at.desc(), LyricPending.id.desc())
        .all()
    )


def lyrics_ready(db: Session, lecture_id: int, now: datetime | None = None) -> bool:
    """True quando dá para seguir: sem pergunta aberta, prazo zerado ou prazo vencido."""
    items = open_pendings(db, lecture_id)
    if not items:
        return True
    config = load_overlay_config()
    if config.wait_hours <= 0:
        return True
    moment = now or datetime.utcnow()
    deadline = min((item.deadline_at or moment) for item in items)
    return moment >= deadline


def wait_decision(open_count: int, wait_hours: float, now: datetime, deadline: datetime | None, no_wait: bool) -> str:
    """ready | skip | timeout | wait. `skip` segue agora e deixa a pergunta na tela."""
    if open_count <= 0:
        return "ready"
    if no_wait or wait_hours <= 0:
        return "skip"
    if deadline is not None and now >= deadline:
        return "timeout"
    return "wait"


def extract_audio_snippet(video: Path, dest: Path, start: float, seconds: float = SNIPPET_SECONDS) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{seconds:.3f}",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size <= 0:
        dest.unlink(missing_ok=True)
        log.warning("trecho de áudio não gerado (%s): %s", video.name, (result.stderr or "")[-300:])
        return False
    return True


def _sync_pendings(
    db: Session,
    lecture: Lecture,
    unknown: list[UnknownSinging],
    video: Path | None,
    config: OverlayConfig,
    now: datetime,
    root: Path | None,
) -> None:
    deadline = now if config.wait_hours <= 0 else now + timedelta(hours=config.wait_hours)
    existing = {
        row.region_key: row
        for row in db.query(LyricPending).filter(LyricPending.lecture_id == lecture.id).all()
    }
    for region in unknown:
        if not region.signature:
            continue
        region_key = f"{int(region.start)}:{region.signature}"
        if region_key in existing:
            continue
        snippet = ""
        if video is not None and video.is_file() and video.stat().st_size > 4096:
            dest = (root or settings.data_dir) / "lyrics" / str(lecture.id) / f"{int(region.start)}.m4a"
            if extract_audio_snippet(video, dest, region.start):
                try:
                    snippet = dest.resolve().relative_to((root or settings.data_dir).resolve()).as_posix()
                except ValueError:
                    snippet = str(dest.resolve())
        row = LyricPending(
            lecture_id=lecture.id,
            start_seconds=region.start,
            end_seconds=region.end,
            snippet_path=snippet,
            transcript_excerpt=region.excerpt,
            signature=region.signature,
            region_key=region_key,
            status="open",
            deadline_at=deadline,
        )
        db.add(row)
    db.commit()


def _mark_timeout(db: Session, lecture_id: int) -> None:
    for row in open_pendings(db, lecture_id):
        row.status = "timeout"
        row.resolved_at = datetime.utcnow()
    db.commit()


def _resolved_cues(db: Session, lecture_id: int, covered: list[tuple[float, float]]) -> list[Cue]:
    rows = (
        db.query(LyricPending)
        .filter(LyricPending.lecture_id == lecture_id, LyricPending.status == "resolved", LyricPending.resolution != "skip")
        .all()
    )
    cues: list[Cue] = []
    for row in rows:
        if any(abs(row.start_seconds - start) < 2 and abs(row.end_seconds - end) < 8 for start, end in covered):
            continue
        stanzas = [part.strip() for part in (row.lyrics or "").split("\n\n") if part.strip()]
        if not stanzas and row.lyrics.strip():
            from backend.app.services.hymnal import split_stanzas

            stanzas = split_stanzas(row.lyrics)
        cues.extend(lyric_cues_for_span(row.title or "Música", row.hymn_number, stanzas, row.start_seconds, row.end_seconds))
    return cues


def _hits_to_cues(hits: list[SongHit]) -> list[Cue]:
    cues: list[Cue] = []
    for hit in hits:
        cues.extend(lyric_cues_for_span(hit.title, hit.number, hit.stanzas, hit.start, hit.end))
    return cues


def _verse_to_cue(item: VerseCue) -> Cue:
    return Cue(start=item.start, end=item.end, kind="verse", title=item.title, body=item.body)


def build_plan(
    db: Session,
    lecture: Lecture,
    transcript: str,
    video: Path | None,
    *,
    no_wait: bool,
    now: datetime | None = None,
    root: Path | None = None,
    load_passage_fn=load_passage,
    fetch_chapter_fn=fetch_chapter,
) -> OverlayPlan:
    config = load_overlay_config()
    moment = now or datetime.utcnow()
    if not config.enabled:
        return OverlayPlan(cues=[], waiting=False, ass_path=None, token="", open_count=0)
    kind = recording_kind(lecture.video_filename or "")
    segments = parse_segments(transcript)
    verses = [
        _verse_to_cue(item)
        for item in detect_verse_cues(transcript, load_passage=load_passage_fn, fetch_chapter=fetch_chapter_fn)
    ]
    hymns = load_hymns(root)
    library = load_library(root)
    hits, unknown = detect_songs(segments, hymns, library)
    _sync_pendings(db, lecture, unknown, video, config, moment, root)
    opened = open_pendings(db, lecture.id)
    deadline = min((row.deadline_at for row in opened if row.deadline_at), default=None)
    decision = wait_decision(len(opened), config.wait_hours, moment, deadline, no_wait)
    log.info(
        "overlays %s (%s): %s versículos, %s músicas, %s pendências, decisão %s",
        lecture.video_filename,
        kind,
        len(verses),
        len(hits),
        len(opened),
        decision,
    )
    if decision == "wait":
        return OverlayPlan(cues=[], waiting=True, ass_path=None, token="", open_count=len(opened))
    if decision == "timeout":
        _mark_timeout(db, lecture.id)
    lyric = _hits_to_cues(hits)
    covered = [(hit.start, hit.end) for hit in hits]
    lyric.extend(_resolved_cues(db, lecture.id, covered))
    cues = pack_cues([*verses, *lyric])
    if not cues or video is None:
        return OverlayPlan(cues=cues, waiting=False, ass_path=None, token=cue_token(cues), open_count=0)
    ass_path = video.with_name(f"{video.stem}.overlays.ass")
    ass_path.write_text(render_ass(cues), encoding="utf-8")
    return OverlayPlan(cues=cues, waiting=False, ass_path=ass_path, token=cue_token(cues), open_count=0)


def resolve_pending(
    db: Session,
    row: LyricPending,
    action: str,
    *,
    query: str = "",
    lyrics: str = "",
    title: str = "",
    root: Path | None = None,
) -> LyricPending:
    """Ensina a biblioteca. Vale para pergunta aberta e para prazo já esgotado."""
    action = (action or "").strip().lower()
    if row.status not in {"open", "timeout"}:
        raise OverlayError("essa pendência já foi respondida")
    if action == "skip":
        remember_song(title=title or "Sem letra", stanzas=[], signature=row.signature, skip=True, source="user", root=root)
        row.status = "skipped"
        row.resolution = "skip"
        row.title = title or row.title
    elif action == "lyrics":
        text = lyrics.strip()
        if len(text) < 8:
            raise OverlayError("cole a letra da música")
        from backend.app.services.hymnal import split_stanzas

        stanzas = split_stanzas(text)
        if not stanzas:
            raise OverlayError("cole a letra da música")
        name = title.strip() or "Música da congregação"
        remember_song(title=name, stanzas=stanzas, signature=row.signature, source="user", root=root)
        row.status = "resolved"
        row.resolution = "lyrics"
        row.title = name
        row.lyrics = "\n\n".join(stanzas)
    elif action == "hymn":
        hymns = load_hymns(root)
        song = find_hymn(query, hymns) or find_in_library(query, root=root)
        if song is None:
            raise OverlayError("não encontrei esse hino no Cantor Cristão nem na biblioteca")
        remember_song(
            title=song.title,
            stanzas=list(song.stanzas),
            signature=row.signature,
            number=song.number,
            source="cantor" if song.source == "cantor" else "user",
            root=root,
        )
        row.status = "resolved"
        row.resolution = "hymn"
        row.hymn_number = song.number
        row.title = song.title
        row.lyrics = "\n\n".join(song.stanzas)
    else:
        raise OverlayError("resposta inválida")
    row.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row


def snippet_file(row: LyricPending, root: Path | None = None) -> Path | None:
    if not row.snippet_path:
        return None
    path = Path(row.snippet_path)
    base = (root or settings.data_dir).resolve()
    if not path.is_absolute():
        path = base / path
    try:
        path.resolve().relative_to(base)
    except ValueError:
        return None
    if path.is_file():
        return path
    return None


def run_lecture_overlay(
    db: Session,
    lecture: Lecture,
    *,
    no_wait: bool,
    fetch: bool = False,
    clock=None,
    sleeper=None,
    cancel: Event | None = None,
    root: Path | None = None,
    lock=None,
) -> str:
    """Detecta, espera no máximo o prazo e comprime com as legendas no mesmo ffmpeg.

    `lock`, se vier, só cobre a leitura e o encode. A espera fica de fora,
    para o prazo não segurar o restante do pipeline.
    """
    from contextlib import nullcontext

    from backend.app.services.compress import CompressError, compress_lecture, load_compress_config
    from backend.app.services.pipeline import lecture_work_dir

    clock = clock or datetime.utcnow
    sleeper = sleeper or time.sleep
    guard = lock or (lambda: nullcontext())
    work = lecture_work_dir(lecture)
    video = work / lecture.video_filename
    transcript_path = work / lecture.transcript_filename
    if not transcript_path.is_file() or transcript_path.stat().st_size <= 0:
        raise OverlayError(f"aula {lecture.id} ainda não tem transcrição")
    if not video.is_file() and fetch:
        from backend.app.services import drive

        if not lecture.drive_file_id:
            raise OverlayError("aula sem arquivo local e sem id do Drive")
        video.parent.mkdir(parents=True, exist_ok=True)
        drive.download_file(lecture.drive_file_id, video, expected_size=lecture.video_size or 0)
    transcript = transcript_path.read_text(encoding="utf-8")
    source = video if video.is_file() else None
    while True:
        if cancel is not None and cancel.is_set():
            raise OverlayCancelled("overlay cancelado")
        with guard():
            plan = build_plan(db, lecture, transcript, source, no_wait=no_wait, now=clock(), root=root)
            if plan.waiting and not no_wait:
                pass
            else:
                try:
                    config = load_compress_config()
                except CompressError as exc:
                    raise OverlayError(str(exc)) from exc
                if not config.enabled:
                    raise OverlayError(
                        "a compressão está desligada em Ajustes; sem encode não há onde queimar a legenda"
                    )
                if source is None:
                    raise OverlayError(f"aula {lecture.id} sem MP4 local. Use --fetch se ele ainda está no Drive.")
                result = compress_lecture(
                    db,
                    lecture,
                    source=source,
                    config=config,
                    ass_path=plan.ass_path,
                    overlay_token=plan.token,
                )
                return f"{result.status} versos_e_letras={len(plan.cues)} perfil={result.profile}"
        sleeper(2)
        db.expire_all()

