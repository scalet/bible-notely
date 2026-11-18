"""Acha cânticos na transcrição e separa a letra em estrofes no tempo do canto.

O Whisper erra a melodia, mas costuma deixar palavras suficientes. A comparação
é por vocabulário (hinário + biblioteca). Só aceita o hino quando vários versos
da letra batem no trecho; uma menção isolada não basta. Sem modelo de áudio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.services.hymnal import SongText, clean_stanzas
from backend.app.services.songlib import library_songs, match_entry
from backend.app.services.textnorm import content_tokens, fold, signature


import re

MIN_LINE_HITS = 2
MIN_LINE_TOKEN_RATIO = 0.6
COMMON_SHARE = 0.03  # palavra em mais de 3% dos hinos (deus, senhor, jesus...) não prova nada
MUSIC_GAP = 8.0  # segundos sem fala na transcrição: o Whisper costuma calar durante o canto
_ANNOUNCE = re.compile(r"hino\s+(?:de\s+)?(?:numero\s+|n\s*)?(\d{1,3})\b")
_COMMON: set[str] = set()
_SHARED_LINES: set[str] = set()


@dataclass
class SongHit:
    start: float
    end: float
    title: str
    number: str
    stanzas: tuple[str, ...]
    source: str
    score: float = 0.0
    line_hits: int = 0


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


def _line_key(tokens: list[str] | str) -> str:
    if isinstance(tokens, str):
        tokens = content_tokens(tokens)
    return " ".join(tokens)


def _is_shared_line(tokens: list[str]) -> bool:
    return _line_key(tokens) in _SHARED_LINES


def _lyric_lines(stanzas: tuple[str, ...] | list[str], rare_only: bool = True) -> list[list[str]]:
    """Linhas distintas com 2+ palavras raras (refrão repetido conta uma vez)."""
    seen: set[str] = set()
    lines: list[list[str]] = []
    for stanza in stanzas:
        for line in str(stanza).splitlines():
            tokens = [token for token in content_tokens(line) if not (rare_only and token in _COMMON)]
            if len(tokens) < 2:
                continue
            key = " ".join(tokens)
            if key in seen:
                continue
            seen.add(key)
            lines.append(tokens)
    return lines


def _line_hits(window_tokens: set[str], stanzas: tuple[str, ...] | list[str], rare_only: bool = True) -> int:
    """Quantas linhas distintas e não compartilhadas da letra batem no trecho."""
    hits = 0
    for tokens in _lyric_lines(stanzas, rare_only):
        if _is_shared_line(tokens):
            continue
        unique = set(tokens)
        needed = len(unique) if len(unique) < 3 else max(3, int(MIN_LINE_TOKEN_RATIO * len(unique) + 0.999))
        if len(window_tokens & unique) >= needed:
            hits += 1
    return hits


def _lines_in_order(window_text: str, stanzas: tuple[str, ...] | list[str], rare_only: bool = True) -> bool:
    """Pelo menos dois versos da letra aparecem na ordem, palavra após palavra."""
    haystack = content_tokens(window_text)
    matched = 0
    for needle in _lyric_lines(stanzas, rare_only):
        if _is_shared_line(needle):
            continue
        if _find_seq(haystack, needle) is not None:
            matched += 1
            if matched >= MIN_LINE_HITS:
                return True
    return False


def _find_seq(haystack: list[str], needle: list[str]) -> int | None:
    if not needle:
        return None
    needed = needle if len(needle) < 3 else needle[: max(3, int(MIN_LINE_TOKEN_RATIO * len(needle) + 0.999))]
    first = needed[0]
    for index, token in enumerate(haystack):
        if token != first:
            continue
        rest = list(needed[1:])
        ok = True
        pos = index + 1
        for item in rest:
            try:
                pos = haystack.index(item, pos) + 1
            except ValueError:
                ok = False
                break
        if ok:
            return index
    return None


def _best_song(
    tokens: list[str],
    window_text: str,
    songs: list[SongText],
    postings: dict[str, set[int]],
    per_song: list[list[set[str]]],
    announced: set[str] | None = None,
) -> tuple[SongText, int, float, int] | None:
    unique = set(tokens)
    if len(unique) < 4:
        return None
    counts: dict[int, int] = {}
    for token in unique:
        for index in postings.get(token, ()):
            counts[index] = counts.get(index, 0) + 1
    best: tuple[int, float, int, int] | None = None
    for index, count in counts.items():
        if count < 3:
            continue
        song = songs[index]
        stanzas = clean_stanzas(song.stanzas, song.number, song.title)
        is_announced = bool(announced and song.number and song.number in announced)
        line_hits = _line_hits(unique, stanzas)
        ordered = line_hits >= MIN_LINE_HITS and _lines_in_order(window_text, stanzas)
        if not ordered and is_announced:
            line_hits = _line_hits(unique, stanzas, rare_only=False)
            ordered = line_hits >= MIN_LINE_HITS and _lines_in_order(window_text, stanzas, rare_only=False)
        if not ordered:
            continue
        stanza_index = 0
        score = 0.0
        for idx, stanza_tokens in enumerate(per_song[index]):
            if len(stanza_tokens) < 3:
                continue
            local = len(unique & stanza_tokens) / min(len(unique), len(stanza_tokens))
            if local > score:
                score = local
                stanza_index = idx
        rank = line_hits + (3 if is_announced else 0)
        if best is None or (rank, score) > (best[0], best[1]):
            best = (rank, score, index, stanza_index)
    if best is None:
        return None
    rank, score, index, stanza_index = best
    return songs[index], stanza_index, score, rank


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


def _looks_sung_text(text: str, duration_hint: float | None) -> bool:
    tokens = content_tokens(text)
    raw = [token for token in fold(text).split() if len(token) > 2]
    duration = max(duration_hint or 1.0, 1.0)
    if len(tokens) >= 4:
        unique_ratio = len(set(tokens)) / len(tokens)
        sparse = len(text) / duration < 7
        return unique_ratio <= 0.62 or sparse
    return len(raw) >= 3 and len(raw) / duration < 1.2


def _looks_sung(group) -> bool:
    text = " ".join(segment.text for segment in group)
    duration = max(group[-1].end - group[0].start, 1.0)
    if duration < 10:
        return False
    return _looks_sung_text(text, duration)


def stanzas_for_span(song: SongText, span: float, best_index: int, window_tokens: set[str] | None = None) -> tuple[str, ...]:
    """Só os slides/estrofes que de fato aparecem no trecho cantado (texto bruto; a tela limpa depois)."""
    raw = tuple(stanza for stanza in song.stanzas if clean_stanzas([stanza], song.number, song.title))
    if not raw:
        return ()
    if window_tokens:
        matched = [
            stanza
            for stanza in raw
            if _line_hits(window_tokens, clean_stanzas([stanza], song.number, song.title), rare_only=False) >= MIN_LINE_HITS
        ]
        if matched:
            first = raw.index(matched[0])
            last = raw.index(matched[-1])
            return raw[first : last + 1]
    if span < 18 or len(raw) == 1:
        return (raw[min(best_index, len(raw) - 1)],)
    count = min(len(raw), max(2, int(span // 12)))
    start = min(best_index, max(0, len(raw) - count))
    return raw[start : start + count]


def _speech_seconds(segment) -> float:
    return max(1.0, len(segment.text) / 14.0)


def _silent_tail(segment) -> float:
    return (segment.end - segment.start) - _speech_seconds(segment)


def _sung_span(segments, start: float, end: float, song: SongText) -> tuple[float, float] | None:
    """Trecho em que de fato se canta. Leitura falada da letra não conta.

    Se a transcrição cala (música) logo depois das linhas, o canto é o silêncio
    e os versos que voltam a aparecer depois dele. Sem silêncio, só aceita se o
    trecho tem jeito de canto (repetição, pouca fala por segundo).
    """
    lyric_tokens = {token for stanza in song.stanzas for token in content_tokens(stanza)}
    inside = [segment for segment in segments if start - 1 <= segment.start <= end + 45]
    for position, segment in enumerate(inside):
        if _silent_tail(segment) < MUSIC_GAP:
            continue
        span_start = segment.start + _speech_seconds(segment)
        span_end = segment.end
        misses = 0
        for after in inside[position + 1 :]:
            if after.start - span_end > 6:
                break
            if not (set(content_tokens(after.text)) & lyric_tokens):
                misses += 1
                if misses > 1:
                    break
                continue
            span_end = after.start + _speech_seconds(after) + 1.5
        return span_start, span_end
    group = [segment for segment in segments if start <= segment.start < end]
    if group and _looks_sung(group):
        return start, end
    return None


def _announced_numbers(segments, start: float, end: float) -> set[str]:
    text = " ".join(segment.text for segment in segments if start - 60 <= segment.start <= end)
    return {str(int(match.group(1))) for match in _ANNOUNCE.finditer(fold(text))}


def _set_common(catalog: list[SongText]) -> None:
    global _COMMON, _SHARED_LINES
    df: dict[str, int] = {}
    line_df: dict[str, int] = {}
    for song in catalog:
        for token in {token for stanza in song.stanzas for token in content_tokens(stanza)}:
            df[token] = df.get(token, 0) + 1
        seen: set[str] = set()
        for stanza in song.stanzas:
            for line in str(stanza).splitlines():
                key = _line_key(line)
                if len(key.split()) < 3 or key in seen:
                    continue
                seen.add(key)
                line_df[key] = line_df.get(key, 0) + 1
    limit = max(3, int(len(catalog) * COMMON_SHARE))
    _COMMON = {token for token, count in df.items() if count > limit} if len(catalog) >= 50 else set()
    # Refrão que aparece em 3+ hinos não prova o cântico sozinho.
    _SHARED_LINES = {key for key, count in line_df.items() if count >= 3} if len(catalog) >= 8 else set()


def _covered(start: float, end: float, intervals: list[tuple[float, float]]) -> bool:
    mid = (start + end) / 2
    return any(left - 1 <= mid <= right + 1 for left, right in intervals)


def _overlaps(a: tuple[float, float], b: tuple[float, float], slack: float = 1.0) -> bool:
    return a[0] < b[1] - slack and b[0] < a[1] - slack


def _song_from_entry(entry: dict) -> SongText | None:
    songs = library_songs([entry])
    return songs[0] if songs else None


def _resolve_overlaps(
    raw: list[tuple[float, float, SongText, int, float, int, set[str]]],
) -> list[tuple[float, float, SongText, int, float, int, set[str]]]:
    """Se dois hinos ocupam o mesmo trecho, fica só o de maior confiança."""
    ranked = sorted(raw, key=lambda item: (-item[5], -item[4], item[0]))
    kept: list[tuple[float, float, SongText, int, float, int, set[str]]] = []
    for item in ranked:
        if any(_overlaps((item[0], item[1]), (other[0], other[1])) for other in kept):
            continue
        kept.append(item)
    return sorted(kept, key=lambda item: item[0])


def detect_songs(segments, hymns: list[SongText], library: list[dict]) -> tuple[list[SongHit], list[UnknownSinging]]:
    if not segments:
        return [], []
    catalog = [*hymns, *library_songs(library)]
    _set_common(catalog)
    postings, per_song = _index(catalog)
    raw: list[tuple[float, float, SongText, int, float, int, set[str]]] = []
    suppressed: list[tuple[float, float]] = []
    for group in _windows(segments):
        text = " ".join(segment.text for segment in group)
        start, end = group[0].start, group[-1].end
        tokens = content_tokens(text)
        learned = match_entry(text, library)
        if learned is not None and learned.get("skip"):
            suppressed.append((start, end))
            continue
        if learned is not None:
            song = _song_from_entry(learned)
            if song is not None:
                raw.append((start, end, song, 0, 1.0, max(_line_hits(set(tokens), song.stanzas), 2), set(tokens)))
                continue
        found = _best_song(tokens, text, catalog, postings, per_song, _announced_numbers(segments, start, end))
        if found is not None:
            song, stanza_index, score, line_hits = found
            raw.append((start, end, song, stanza_index, score, line_hits, set(tokens)))
    merged: list[tuple[float, float, SongText, int, float, int, set[str]]] = []
    for item in raw:
        start, end, song, stanza_index, score, line_hits, tokens = item
        if merged and merged[-1][2].key == song.key and start <= merged[-1][1] + 8:
            previous = merged[-1]
            merged[-1] = (
                previous[0],
                max(previous[1], end),
                song,
                previous[3],
                max(previous[4], score),
                max(previous[5], line_hits),
                previous[6] | tokens,
            )
        else:
            merged.append(item)
    refined = []
    for start, end, song, stanza_index, score, line_hits, tokens in merged:
        span = _sung_span(segments, start, end, song)
        if span is None or span[1] - span[0] < 4:
            continue
        refined.append((span[0], span[1], song, stanza_index, score, line_hits, tokens))
    merged = _resolve_overlaps(refined)
    hits: list[SongHit] = []
    covered: list[tuple[float, float]] = list(suppressed)
    for start, end, song, stanza_index, score, line_hits, tokens in merged:
        stanzas = stanzas_for_span(song, end - start, stanza_index, tokens)
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
                score=score,
                line_hits=line_hits,
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
