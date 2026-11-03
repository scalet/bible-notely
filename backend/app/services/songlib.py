"""Biblioteca local do que o usuário ensinou: hino, letra colada ou 'seguir sem letra'."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from backend.app.config import settings
from backend.app.services.hymnal import SongText
from backend.app.services.textnorm import content_tokens, fold


def library_path(root: Path | None = None) -> Path:
    return (root or settings.data_dir) / "songs" / "library.json"


def load_library(root: Path | None = None) -> list[dict]:
    path = library_path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("songs") or []
    return [item for item in raw if isinstance(item, dict)]


def save_library(entries: list[dict], root: Path | None = None) -> None:
    path = library_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _as_song(entry: dict) -> SongText | None:
    if entry.get("skip"):
        return None
    stanzas = tuple(str(part).strip() for part in entry.get("stanzas") or [] if str(part).strip())
    if not stanzas:
        return None
    title = str(entry.get("title") or "Música")
    number = str(entry.get("number") or "")
    return SongText(
        key=f"lib:{entry.get('id')}",
        number=number,
        title=title,
        stanzas=stanzas,
        source="library",
    )


def library_songs(entries: list[dict] | None = None, root: Path | None = None) -> list[SongText]:
    songs = []
    for entry in entries if entries is not None else load_library(root):
        song = _as_song(entry)
        if song is not None:
            songs.append(song)
    return songs


def match_entry(text: str, entries: list[dict]) -> dict | None:
    """Reconhece um trecho já ensinado, inclusive os marcados para seguir sem letra."""
    tokens = set(content_tokens(text))
    if len(tokens) < 3:
        return None
    best: tuple[float, dict] | None = None
    for entry in entries:
        for stored in entry.get("signatures") or []:
            known = set(str(stored).split())
            if len(known) < 3:
                continue
            shared = len(tokens & known)
            score = shared / min(len(tokens), len(known))
            if shared >= 4 and score >= 0.72 and (best is None or score > best[0]):
                best = (score, entry)
    return best[1] if best else None


def _remember(entry: dict, text_signature: str) -> None:
    signatures = [str(item) for item in entry.get("signatures") or [] if str(item).strip()]
    if text_signature and text_signature not in signatures:
        signatures.append(text_signature)
    entry["signatures"] = signatures


def remember_song(
    *,
    title: str,
    stanzas: list[str],
    signature: str,
    number: str = "",
    source: str = "user",
    skip: bool = False,
    root: Path | None = None,
) -> dict:
    """Grava ou atualiza uma música. A mesma assinatura não cria outra ficha."""
    entries = load_library(root)
    folded = fold(title)
    found = None
    for entry in entries:
        if signature and signature in (entry.get("signatures") or []):
            found = entry
            break
        if not skip and number and str(entry.get("number") or "") == number and entry.get("source") == source:
            found = entry
            break
        if not skip and folded and fold(str(entry.get("title") or "")) == folded and not entry.get("skip"):
            found = entry
            break
    if found is None:
        found = {
            "id": uuid.uuid4().hex,
            "title": title.strip() or ("Sem letra" if skip else "Música"),
            "number": number,
            "source": source,
            "skip": skip,
            "stanzas": [] if skip else [part.strip() for part in stanzas if part.strip()],
            "signatures": [],
        }
        entries.append(found)
    else:
        if title.strip():
            found["title"] = title.strip()
        if number:
            found["number"] = number
        found["skip"] = skip
        found["source"] = source
        if not skip and stanzas:
            found["stanzas"] = [part.strip() for part in stanzas if part.strip()]
    _remember(found, signature)
    save_library(entries, root)
    return found


def find_in_library(query: str, entries: list[dict] | None = None, root: Path | None = None) -> SongText | None:
    from backend.app.services.hymnal import find_hymn

    songs = library_songs(entries if entries is not None else load_library(root))
    return find_hymn(query, songs)
