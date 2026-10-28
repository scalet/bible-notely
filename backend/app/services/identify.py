from __future__ import annotations

from pathlib import Path

from backend.app.models import Speaker
from backend.app.services.faces import FaceError, analyze_image, cosine_similarity, decide, dumps_embedding, loads_embedding


def _speaker_embedding(speaker: Speaker) -> list[float] | None:
    stored = loads_embedding(speaker.embedding)
    if stored:
        return stored
    photo = Path(speaker.photo_path) if speaker.photo_path else None
    if not photo or not photo.exists():
        return None
    embedding, _crop, note = analyze_image(photo)
    if not embedding:
        speaker.photo_note = note
        return None
    speaker.embedding = dumps_embedding(embedding)
    speaker.photo_note = ""
    return embedding


async def identify_speaker(frames: list[Path], speakers: list[Speaker]) -> dict:
    frames = [path for path in frames if path.exists()]
    if not frames:
        return {
            "speaker_id": None,
            "confidence": 0,
            "status": "unknown",
            "note": "Nenhum frame extraído do vídeo.",
        }
    catalog = [speaker for speaker in speakers if speaker.active and speaker.photo_path]
    if not catalog:
        return {
            "speaker_id": None,
            "confidence": 0,
            "status": "unknown",
            "note": "Nenhum palestrante cadastrado com foto.",
        }

    frame_vectors: list[list[float]] = []
    try:
        for frame in frames:
            embedding, _crop, _note = analyze_image(frame)
            if embedding:
                frame_vectors.append(embedding)
    except FaceError as exc:
        raise FaceError(f"identificação local indisponível: {exc}") from exc

    if not frame_vectors:
        return {
            "speaker_id": None,
            "confidence": 0,
            "status": "unknown",
            "note": "Nenhum rosto nos frames do vídeo.",
        }

    scores: list[tuple[int, float]] = []
    ready = 0
    for speaker in catalog:
        try:
            reference = _speaker_embedding(speaker)
        except FaceError as exc:
            raise FaceError(f"identificação local indisponível: {exc}") from exc
        if not reference:
            continue
        ready += 1
        best = max(cosine_similarity(frame, reference) for frame in frame_vectors)
        scores.append((speaker.id, best))
    if ready == 0:
        return {
            "speaker_id": None,
            "confidence": 0,
            "status": "unknown",
            "note": "As fotos do cadastro não têm rosto detectável.",
        }
    return decide(scores)
