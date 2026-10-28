"""Texto ARC (SBB) a partir de https://www.bibliaonline.com.br/arc/{livro}/{capitulo}.

A LLM não tem navegador. Quem busca a redação é este módulo, com cache local,
para o PDF citar a escritura e não uma paráfrase.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from html import unescape
from pathlib import Path

from backend.app.config import settings
from backend.app.services.obsidian import collect_references, parse_reference

log = logging.getLogger("bible_notely.bible")

# Slugs da Bíblia Online (ARC). Jó é "job"; João é "jo".
SLUGS: dict[str, str] = {
    "Gênesis": "gn",
    "Êxodo": "ex",
    "Levítico": "lv",
    "Números": "nm",
    "Deuteronômio": "dt",
    "Josué": "js",
    "Juízes": "jz",
    "Rute": "rt",
    "1 Samuel": "1sm",
    "2 Samuel": "2sm",
    "1 Reis": "1rs",
    "2 Reis": "2rs",
    "1 Crônicas": "1cr",
    "2 Crônicas": "2cr",
    "Esdras": "ed",
    "Neemias": "ne",
    "Ester": "et",
    "Jó": "job",
    "Salmos": "sl",
    "Provérbios": "pv",
    "Eclesiastes": "ec",
    "Cantares": "ct",
    "Isaías": "is",
    "Jeremias": "jr",
    "Lamentações": "lm",
    "Ezequiel": "ez",
    "Daniel": "dn",
    "Oseias": "os",
    "Joel": "jl",
    "Amós": "am",
    "Obadias": "ob",
    "Jonas": "jn",
    "Miqueias": "mq",
    "Naum": "na",
    "Habacuque": "hc",
    "Sofonias": "sf",
    "Ageu": "ag",
    "Zacarias": "zc",
    "Malaquias": "ml",
    "Mateus": "mt",
    "Marcos": "mc",
    "Lucas": "lc",
    "João": "jo",
    "Atos": "at",
    "Romanos": "rm",
    "1 Coríntios": "1co",
    "2 Coríntios": "2co",
    "Gálatas": "gl",
    "Efésios": "ef",
    "Filipenses": "fp",
    "Colossenses": "cl",
    "1 Tessalonicenses": "1ts",
    "2 Tessalonicenses": "2ts",
    "1 Timóteo": "1tm",
    "2 Timóteo": "2tm",
    "Tito": "tt",
    "Filemom": "fm",
    "Hebreus": "hb",
    "Tiago": "tg",
    "1 Pedro": "1pe",
    "2 Pedro": "2pe",
    "1 João": "1jo",
    "2 João": "2jo",
    "3 João": "3jo",
    "Judas": "jd",
    "Apocalipse": "ap",
}

_UA = "Mozilla/5.0 (compatible; bible-notely/1.0; +https://www.bibliaonline.com.br)"
_MAX_CHAPTERS = 12
_MAX_VERSES = 40
_cache: dict[tuple[str, int], dict[int, str]] = {}


def chapter_url(book: str, chapter: int) -> str | None:
    slug = SLUGS.get(book)
    if not slug:
        return None
    return f"https://www.bibliaonline.com.br/arc/{slug}/{chapter}"


def _join_words(words: list[str]) -> str:
    """A Bíblia Online às vezes omite o espaço entre os spans; a pontuação não ganha espaço antes."""
    parts: list[str] = []
    for raw in words:
        word = unescape(re.sub(r"<[^>]+>", "", raw))
        if not word:
            continue
        if (
            parts
            and not parts[-1][-1].isspace()
            and word[0] not in ",.;:!?)]»”'’"
            and parts[-1][-1] not in "([«“"
        ):
            parts.append(" ")
        parts.append(word)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_chapter_html(html: str) -> dict[int, str]:
    """Extrai o texto de cada versículo do HTML da Bíblia Online."""
    verses: dict[int, str] = {}
    html = re.sub(r"<h[1-6][^>]*>.*?</h[1-6]>", "", html, flags=re.S)  # títulos de seção não são versículo
    parts = re.split(r'aria-label="versículo (\d+)"', html)
    for i in range(1, len(parts) - 1, 2):
        number = int(parts[i])
        chunk = parts[i + 1]
        marker = re.search(r'<span role="text" data-v="\.\d+\."[^>]*>', chunk)
        if not marker:
            continue
        inner = chunk[marker.end() :]
        words = re.findall(r'<span data-t="\d+"[^>]*>(.*?)</span>', inner)
        text = _join_words(words)
        if text and number not in verses:
            verses[number] = text
    return verses


def _cache_path(book: str, chapter: int) -> Path | None:
    slug = SLUGS.get(book)
    if not slug:
        return None
    return settings.data_dir / "bible" / "arc" / f"{slug}-{chapter}.json"


def _read_cache(book: str, chapter: int) -> dict[int, str] | None:
    key = (book, chapter)
    if key in _cache:
        return _cache[key]
    path = _cache_path(book, chapter)
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    verses = {int(k): str(v) for k, v in raw.items() if str(v).strip()}
    if verses:
        _cache[key] = verses
    return verses or None


def _write_cache(book: str, chapter: int, verses: dict[int, str]) -> None:
    path = _cache_path(book, chapter)
    if path is None or not verses:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({str(k): v for k, v in sorted(verses.items())}, ensure_ascii=False),
        encoding="utf-8",
    )
    _cache[(book, chapter)] = verses


def fetch_chapter(book: str, chapter: int) -> dict[int, str]:
    cached = _read_cache(book, chapter)
    if cached is not None:
        return cached
    url = chapter_url(book, chapter)
    if not url:
        log.warning("livro sem slug na Bíblia Online: %s", book)
        return {}
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            html = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("falha ao buscar %s: %s", url, exc)
        return {}
    verses = parse_chapter_html(html)
    if not verses:
        log.warning("capítulo sem versículos em %s", url)
        return {}
    _write_cache(book, chapter, verses)
    return verses


def _wanted(spec: str, available: dict[int, str]) -> list[int]:
    if not spec:
        return sorted(available)[:_MAX_VERSES]
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
    return [n for n in numbers if n in available][:_MAX_VERSES]


def load_passage(book: str, chapter: int, verses: str) -> dict | None:
    chapter_text = fetch_chapter(book, chapter)
    if not chapter_text:
        return None
    numbers = _wanted(verses, chapter_text)
    if not numbers:
        return None
    label = f"{book} {chapter}" + (f":{verses}" if verses else "")
    truncated = not verses and len(chapter_text) > len(numbers)
    return {
        "book": book,
        "chapter": chapter,
        "verses": verses,
        "label": label,
        "url": chapter_url(book, chapter),
        "truncated": truncated,
        "items": [{"n": n, "text": chapter_text[n]} for n in numbers],
    }


def _expand(spec: str) -> set[int]:
    numbers: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if start.isdigit() and end.isdigit():
                numbers.update(range(int(start), int(end) + 1))
        elif part.isdigit():
            numbers.add(int(part))
    return numbers


def _compress(numbers: set[int]) -> str:
    ordered = sorted(numbers)
    if not ordered:
        return ""
    ranges: list[str] = []
    start = prev = ordered[0]
    for number in ordered[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = number
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _select_refs(summary: dict) -> list[tuple[str, int, str]]:
    """Une os versículos do mesmo capítulo. Capítulo inteiro só no texto-base."""
    base = parse_reference(str(summary.get("texto_base") or ""))
    base_chapters = {(book, chapter) for book, chapter, verses in base if not verses}
    grouped: dict[tuple[str, int], set[int] | None] = {}
    for book, chapter, verses in list(base) + collect_references(summary):
        key = (book, chapter)
        if not verses and key not in base_chapters:
            continue
        if not verses:
            grouped[key] = None
            continue
        if grouped.get(key) is None and key in grouped:
            continue
        grouped.setdefault(key, set()).update(_expand(verses))
        if len(grouped) >= _MAX_CHAPTERS:
            break
    selected: list[tuple[str, int, str]] = []
    for (book, chapter), numbers in grouped.items():
        selected.append((book, chapter, "" if numbers is None else _compress(numbers)))
    return selected


def attach_scripture(summary: dict) -> dict:
    out = dict(summary)
    passages = []
    for book, chapter, verses in _select_refs(summary):
        passage = load_passage(book, chapter, verses)
        if passage:
            passages.append(passage)
    out["escrituras"] = passages
    return out


def format_prompt_block(passages: list[dict]) -> str:
    if not passages:
        return ""
    lines = [
        "TEXTO ARC (Almeida Revista e Corrigida, SBB), obtido em bibliaonline.com.br/arc/{livro}/{capitulo}.",
        "Copie estes versículos literalmente. Não parafraseie, não complete e não invente redação.",
    ]
    for passage in passages:
        lines.append(f"\n{passage['label']} ({passage['url']})")
        for item in passage["items"]:
            lines.append(f"{item['n']} {item['text']}")
    return "\n".join(lines)
