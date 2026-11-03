"""Acha cânticos na transcrição e separa a letra em estrofes no tempo do canto.

O Whisper erra a melodia, mas costuma deixar palavras suficientes. A comparação
é por vocabulário (hinário + biblioteca), com um sinal extra de canto: pouca
fala por segundo ou muita repetição. Sem modelo de áudio.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.hymnal import SongText
from backend.app.services.songlib import library_songs, match_entry
from backend.app.services.textnorm import content_tokens, fold, signature


@dataclass
class SongHit:
    start: float
    end: float
    title: str
    number: str
    stanzas: tuple[str, ...]
    source: str


@dataclass
class UnknownSinging:
    start: float
    end: float
    excerpt: str
    signature: str


def _index(songs: list[SongText]) -> tuple[dict[str, set[int]], list[list[set[str]]]]:
    postings: dict[str, set[int]] = {}
    per_song: list[list[set[str]]] = []
    for index, song in enumerate(songs):
        stanzas: list[set[str]] = []
        for stanza in song.stanzas:
            tokens = set(content_tokens(stanza))
            stanzas.append(tokens)
            for token in tokens:
                postings.setdefault(token, set()).add(index)
        per_song.append(stanzas)
    return postings, per_song


def _best_song(
    tokens: list[str],
    songs: list[SongText],
    postings: dict[str, set[int]],
    per_song: list[list[set[str]]],
) -> tuple[SongText, int, float] | None:
    unique = set(tokens)
    if len(unique) < 4:
        return None
    counts: dict[int, int] = {}
    for token in unique:
        for index in postings.get(token, ()):
            counts[index] = counts.get(index, 0) + 1
    best: tuple[float, int, int] | None = None
    for index, count in counts.items():
        if count < 4:
            continue
        for stanza_index, stanza_tokens in enumerate(per_song[index]):
            if len(stanza_tokens) < 3:
                continue
            shared = len(unique & stanza_tokens)
            if shared < 4:
                continue
            score = shared / min(len(unique), len(stanza_tokens))
            if score >= 0.48 and (best is None or score > best[0]):
                best = (score, index, stanza_index)
    if best is None:
        return None
    score, index, stanza_index = best
    return songs[index], stanza_index, score


def _windows(segments) -> list[list]:
    windows = []
    for start_index in range(len(segments)):
        group = [segments[start_index]]
        for segment in segments[start_index + 1 :]:
            if segment.start - group[0].start > 28:
                break
            if segment.start - group[-1].end > 6:
                break
            group.append(segment)
        if group[-1].end - group[0].start < 6 and len(group) < 2:
            continue
        windows.append(group)
    return windows


def _looks_sung(group) -> bool:
    text = " ".join(segment.text for segment in group)
    duration = max(group[-1].end - group[0].start, 1.0)
    if duration < 10:
        return False
    tokens = content_tokens(text)
    raw = [token for token in fold(text).split() if len(token) > 2]
    if len(tokens) >= 4:
        unique_ratio = len(set(tokens)) / len(tokens)
        sparse = len(text) / duration < 7
        return unique_ratio <= 0.62 or sparse
    return len(raw) >= 3 and len(raw) / duration < 1.2


def stanzas_for_span(song: SongText, span: float, best_index: int) -> tuple[str, ...]:
    """Estrofes que cabem no canto. Um trecho curto mostra só a estrofe reconhecida."""
    if not song.stanzas:
        return ()
    if span < 18 or len(song.stanzas) == 1:
        return (song.stanzas[min(best_index, len(song.stanzas) - 1)],)
    count = min(len(song.stanzas), max(2, int(span // 12)))
    if span >= 12 * len(song.stanzas):
        return song.stanzas
    start = min(best_index, max(0, len(song.stanzas) - count))
    chosen = song.stanzas[start : start + count]
    return chosen or (song.stanzas[min(best_index, len(song.stanzas) - 1)],)


def _covered(start: float, end: float, intervals: list[tuple[float, float]]) -> bool:
    mid = (start + end) / 2
    return any(left - 1 <= mid <= right + 1 for left, right in intervals)


def _song_from_entry(entry: dict) -> SongText | None:
    songs = library_songs([entry])
    return songs[0] if songs else None


def detect_songs(segments, hymns: list[SongText], library: list[dict]) -> tuple[list[SongHit], list[UnknownSinging]]:
    if not segments:
        return [], []
    catalog = [*hymns, *library_songs(library)]
    postings, per_song = _index(catalog)
    raw: list[tuple[float, float, SongText, int]] = []
    suppressed: list[tuple[float, float]] = []
    for group in _windows(segments):
        text = " ".join(segment.text for segment in group)
        start, end = group[0].start, group[-1].end
        learned = match_entry(text, library)
        if learned is not None and learned.get("skip"):
            suppressed.append((start, end))
            continue
        if learned is not None:
            song = _song_from_entry(learned)
            if song is not None:
                raw.append((start, end, song, 0))
                continue
        found = _best_song(content_tokens(text), catalog, postings, per_song)
        if found is not None:
            song, stanza_index, _score = found
            raw.append((start, end, song, stanza_index))
    merged: list[tuple[float, float, SongText, int]] = []
    for start, end, song, stanza_index in raw:
        if merged and merged[-1][2].key == song.key and start <= merged[-1][1] + 8:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), song, previous[3])
        else:
            merged.append((start, end, song, stanza_index))
    hits: list[SongHit] = []
    covered: list[tuple[float, float]] = list(suppressed)
    for start, end, song, stanza_index in merged:
        stanzas = stanzas_for_span(song, end - start, stanza_index)
        if not stanzas:
            continue
        hits.append(
            SongHit(
                start=start,
                end=end,
                title=song.title,
                number=song.number,
                stanzas=stanzas,
                source=song.source,
            )
        )
        covered.append((start, end))
    unknown: list[UnknownSinging] = []
    group: list = []

    def flush() -> None:
        if not group:
            return
        if not _covered(group[0].start, group[-1].end, covered) and _looks_sung(group):
            excerpt = " ".join(segment.text for segment in group).strip()
            if excerpt:
                unknown.append(
                    UnknownSinging(
                        start=group[0].start,
                        end=max(group[-1].end, group[0].start + 10),
                        excerpt=excerpt[:400],
                        signature=signature(excerpt),
                    )
                )
        group.clear()

    for segment in segments:
        if _covered(segment.start, segment.end, covered):
            flush()
            continue
        if group and segment.start - group[-1].end > 5:
            flush()
        group.append(segment)
    flush()
    return hits, unknown
