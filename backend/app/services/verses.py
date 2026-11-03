"""Referências faladas na transcrição e o texto ARC no intervalo em que são lidas.

A redação vem do cache/busca já usado pelo PDF (`bible.py`). Sem o texto ARC
o versículo não entra na tela: não mostramos a paráfrase do Whisper como se
fosse a Almeida.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.app.services.obsidian import _ALIASES, _canonical, _fold
from backend.app.services.textnorm import content_tokens, overlap_score

_BOOK_RE = "|".join(sorted((re.escape(alias) for alias in _ALIASES), key=len, reverse=True))
_UNITS = (
    "dois|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|treze|"
    "catorze|quatorze|quinze|dezesseis|dezasseis|dezessete|dezoito|dezenove|dezanove"
)
_TENS = "vinte|trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa"
_UNIT_VALUE = {
    "um": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12, "treze": 13,
    "catorze": 14, "quatorze": 14, "quinze": 15, "dezesseis": 16, "dezasseis": 16,
    "dezessete": 17, "dezoito": 18, "dezenove": 19, "dezanove": 19,
}
_TEN_VALUE = {
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "sessenta": 60,
    "setenta": 70, "oitenta": 80, "noventa": 90,
}
_NUMBER_WORD = re.compile(
    rf"\b(?:cento(?:\s+e\s+(?:{_TENS})(?:\s+e\s+(?:{_UNITS}|um))?)?|cem|"
    rf"(?:{_TENS})(?:\s+e\s+(?:{_UNITS}|um))|(?:{_UNITS})|(?<=capitulo )um|(?<=versiculo )um)\b"
)
_REF = re.compile(
    rf"(?<![\w])(?P<book>(?:[123]\s?)?(?:{_BOOK_RE}))\.?\s+"
    rf"(?:cap(?:itulo)?\s+)?"
    rf"(?P<ch>\d{{1,3}})"
    rf"(?:(?:\s*[,:.]\s*|\s+versiculos?\s+)(?P<vlist>\d{{1,3}}(?:\s*(?:e|a|ate|-|–)\s*\d{{1,3}})*))?"
    rf"(?![\w])"
)
_LINE = re.compile(r"^\[(\d+):(\d{2}):(\d{2})\]\s*(.*)$")


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SpokenRef:
    book: str
    chapter: int
    verses: str
    at: float


@dataclass
class VerseCue:
    start: float
    end: float
    title: str
    body: str


def _number_value(phrase: str) -> int | None:
    parts = phrase.split()
    if not parts:
        return None
    if parts[0] == "cem":
        return 100
    total = 0
    if parts[0] == "cento":
        total = 100
        parts = parts[2:] if len(parts) > 1 and parts[1] == "e" else parts[1:]
    if not parts:
        return total or None
    if parts[0] in _TEN_VALUE:
        total += _TEN_VALUE[parts[0]]
        if len(parts) >= 3 and parts[1] == "e" and parts[2] in _UNIT_VALUE:
            total += _UNIT_VALUE[parts[2]]
        return total
    if parts[0] in _UNIT_VALUE:
        return total + _UNIT_VALUE[parts[0]]
    return None


def spoken_numbers_to_digits(text: str) -> str:
    """Troca 'capítulo onze versículo vinte e cinco' por dígitos. O resto do texto fica."""
    folded = _fold(text)

    def replace(match: re.Match[str]) -> str:
        value = _number_value(match.group(0))
        return str(value) if value is not None else match.group(0)

    return _NUMBER_WORD.sub(replace, folded)


def _verse_spec(vlist: str | None) -> str:
    if not vlist:
        return ""
    numbers = [int(part) for part in re.findall(r"\d{1,3}", vlist)]
    if not numbers:
        return ""
    if len(numbers) == 1:
        return str(numbers[0])
    start, end = numbers[0], numbers[-1]
    if end < start:
        start, end = end, start
    return str(start) if start == end else f"{start}-{end}"


def parse_spoken_references(text: str) -> list[tuple[str, int, str]]:
    """(livro, capítulo, versículos) na ordem em que aparecem. Capítulo sem versículo fica com ''."""
    found: list[tuple[str, int, str]] = []
    prepared = spoken_numbers_to_digits(text)
    for match in _REF.finditer(prepared):
        book = _canonical(match.group("book"))
        if not book:
            continue
        verses = _verse_spec(match.group("vlist"))
        if len(match.group("book").strip(" 123.")) <= 3 and not verses:
            continue
        found.append((book, int(match.group("ch")), verses))
    return found


def parse_segments(transcript: str) -> list[Segment]:
    rows: list[Segment] = []
    for line in (transcript or "").splitlines():
        match = _LINE.match(line.strip())
        if not match:
            continue
        hours, minutes, seconds, text = match.groups()
        if not text.strip():
            continue
        start = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        rows.append(Segment(start=float(start), end=float(start), text=text.strip()))
    for index, segment in enumerate(rows):
        if index + 1 < len(rows):
            segment.end = max(rows[index + 1].start, segment.start + 1)
        else:
            segment.end = segment.start + max(4.0, min(20.0, len(segment.text) / 8))
    return rows


def _expand(spec: str) -> list[int]:
    numbers: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if start.isdigit() and end.isdigit():
                numbers.extend(range(int(start), int(end) + 1))
        elif part.isdigit():
            numbers.append(int(part))
    return numbers


def _clamp(start: float, end: float, floor: float = 6.0, ceiling: float = 22.0) -> tuple[float, float]:
    if end < start + floor:
        end = start + floor
    if end > start + ceiling:
        end = start + ceiling
    return start, end


def _align(segments: list[Segment], origin: float, until: float, verse: str) -> tuple[float, float]:
    wanted = content_tokens(verse)
    best: Segment | None = None
    best_score = 0.0
    for segment in segments:
        if segment.start < origin - 1 or segment.start > until:
            continue
        score = overlap_score(content_tokens(segment.text), wanted)
        if score > best_score:
            best_score = score
            best = segment
    if best is not None and best_score >= 0.34:
        return _clamp(best.start, best.end)
    length = max(8.0, min(18.0, 4 + len(verse) / 14))
    return origin, origin + length


def _phrase_in_order(haystack: list[str], needle: list[str]) -> bool:
    if not needle:
        return False
    pos = 0
    for token in haystack:
        if token == needle[pos]:
            pos += 1
            if pos == len(needle):
                return True
    return False


def _quotes_verse(window_tokens: list[str], verse: str) -> bool:
    tokens = content_tokens(verse)
    shared = len(set(window_tokens) & set(tokens))
    score = overlap_score(window_tokens, tokens)
    if shared >= 4 and score >= 0.4:
        return True
    if shared < 3 or len(tokens) < 3:
        return False
    for index in range(len(tokens) - 2):
        if _phrase_in_order(window_tokens, tokens[index : index + 3]):
            return True
    return False


def _items_from_passage(passage: dict | None) -> list[tuple[int, str]]:
    if not passage:
        return []
    items = []
    for item in passage.get("items") or []:
        text = str(item.get("text") or "").strip()
        if text:
            items.append((int(item["n"]), text))
    return items


def detect_verse_cues(transcript: str, *, load_passage, fetch_chapter) -> list[VerseCue]:
    """Uma faixa por versículo, no trecho em que ele é citado ou lido."""
    segments = parse_segments(transcript)
    cues: list[VerseCue] = []
    seen: set[tuple[str, int, int, int]] = set()
    for segment in segments:
        for book, chapter, verses in parse_spoken_references(segment.text):
            numbers: list[tuple[int, str]] = []
            if verses:
                passage = load_passage(book, chapter, verses)
                numbers = _items_from_passage(passage)
            else:
                chapter_text = fetch_chapter(book, chapter) or {}
                window = " ".join(
                    item.text for item in segments if segment.start <= item.start <= segment.start + 50
                )
                window_tokens = content_tokens(window)
                scored: list[tuple[float, int, str]] = []
                for number, text in chapter_text.items():
                    if not _quotes_verse(window_tokens, text):
                        continue
                    score = overlap_score(window_tokens, content_tokens(text))
                    scored.append((score, int(number), text))
                scored.sort(key=lambda item: (-item[0], item[1]))
                numbers = [(number, text) for _score, number, text in scored[:4]]
            for number, text in numbers:
                key = (book, chapter, number, int(segment.start))
                if key in seen:
                    continue
                seen.add(key)
                start, end = _align(segments, segment.start, segment.start + 40, text)
                cues.append(VerseCue(start=start, end=end, title=f"{book} {chapter}:{number}", body=text))
    cues.sort(key=lambda cue: (cue.start, cue.title))
    packed: list[VerseCue] = []
    for cue in cues:
        if packed and cue.start < packed[-1].end:
            packed[-1].end = max(packed[-1].start + 3, cue.start)
        if cue.end - cue.start >= 2:
            packed.append(cue)
    return packed
