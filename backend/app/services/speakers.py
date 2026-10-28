from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Speaker
from backend.app.services.faces import FaceError, analyze_image, dumps_embedding, save_crop

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def parse_speaker_filename(path: Path) -> tuple[str, str]:
    """`Nome.jpg` ou `Nome | função.jpg`."""
    stem = " ".join(path.stem.replace("_", " ").split())
    if "|" not in stem:
        return stem, ""
    name, role = stem.split("|", 1)
    return " ".join(name.split()), " ".join(role.split())


def find_by_name(db: Session, name: str) -> Speaker | None:
    return (
        db.query(Speaker)
        .filter(func.lower(Speaker.name) == name.strip().lower())
        .one_or_none()
    )


def foto_path_for(name: str, suffix: str = ".jpg") -> Path:
    safe = name.replace("/", "-").replace("\\", "-").strip()
    return settings.speakers_fotos_dir / f"{safe}{suffix}"


def canonical_photo(speaker_id: int) -> Path:
    return settings.speakers_dir / f"{speaker_id}.jpg"


def apply_photo(speaker: Speaker, source: Path) -> str:
    """Copia a foto, gera embedding local e grava o recorte do rosto."""
    settings.speakers_dir.mkdir(parents=True, exist_ok=True)
    settings.speakers_fotos_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() if source.suffix else ".jpg"
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".jpg"
    named = foto_path_for(speaker.name, suffix)
    if source.resolve() != named.resolve():
        named.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, named)
    dest = canonical_photo(speaker.id)
    note = ""
    try:
        embedding, crop, note = analyze_image(named if named.exists() else source)
    except FaceError as exc:
        embedding, crop, note = None, None, str(exc)
    if embedding and crop is not None:
        save_crop(crop, dest)
        speaker.embedding = dumps_embedding(embedding)
        speaker.photo_note = ""
    else:
        shutil.copy2(named if named.exists() else source, dest)
        speaker.embedding = ""
        speaker.photo_note = note or "rosto não reconhecido na foto"
    speaker.photo_path = str(dest)
    return speaker.photo_note


def import_fotos(db: Session) -> dict:
    settings.speakers_fotos_dir.mkdir(parents=True, exist_ok=True)
    created = updated = skipped = 0
    errors: list[str] = []
    files = [
        path
        for path in sorted(settings.speakers_fotos_dir.iterdir())
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES and not path.name.startswith(".")
    ]
    for path in files:
        name, role = parse_speaker_filename(path)
        if not name:
            errors.append(f"{path.name}: nome vazio")
            continue
        speaker = find_by_name(db, name)
        dest_mtime = 0.0
        if speaker and speaker.photo_path:
            photo = Path(speaker.photo_path)
            if photo.exists():
                dest_mtime = photo.stat().st_mtime
        if speaker and speaker.embedding and dest_mtime >= path.stat().st_mtime:
            skipped += 1
            continue
        is_new = speaker is None
        if speaker is None:
            speaker = Speaker(name=name, role=role, active=True)
            db.add(speaker)
            db.flush()
        elif role and not speaker.role:
            speaker.role = role
        try:
            apply_photo(speaker, path)
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if is_new:
            created += 1
        else:
            updated += 1
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "directory": str(settings.speakers_fotos_dir),
    }
