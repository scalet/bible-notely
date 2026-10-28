"""Exporta os resumos da LLM como notas Markdown para o Obsidian (independente do pipeline)."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import Lecture, Speaker

# Livros (ARC) com variações de grafia/abreviação, sem acento e em minúsculas.
_BOOKS: dict[str, list[str]] = {
    "Gênesis": ["genesis", "gn", "gen"],
    "Êxodo": ["exodo", "ex", "exo"],
    "Levítico": ["levitico", "lv", "lev"],
    "Números": ["numeros", "nm", "num"],
    "Deuteronômio": ["deuteronomio", "dt", "deut"],
    "Josué": ["josue", "js"],
    "Juízes": ["juizes", "jz"],
    "Rute": ["rute", "rt"],
    "1 Samuel": ["1 samuel", "1 sm"],
    "2 Samuel": ["2 samuel", "2 sm"],
    "1 Reis": ["1 reis", "1 rs"],
    "2 Reis": ["2 reis", "2 rs"],
    "1 Crônicas": ["1 cronicas", "1 cr"],
    "2 Crônicas": ["2 cronicas", "2 cr"],
    "Esdras": ["esdras", "ed"],
    "Neemias": ["neemias", "ne"],
    "Ester": ["ester", "et"],
    "Jó": ["jo", "job"],
    "Salmos": ["salmos", "salmo", "sl"],
    "Provérbios": ["proverbios", "pv", "prov"],
    "Eclesiastes": ["eclesiastes", "ec"],
    "Cantares": ["cantares", "cantares de salomao", "ct", "cantico dos canticos"],
    "Isaías": ["isaias", "is"],
    "Jeremias": ["jeremias", "jr"],
    "Lamentações": ["lamentacoes", "lm"],
    "Ezequiel": ["ezequiel", "ez"],
    "Daniel": ["daniel", "dn"],
    "Oseias": ["oseias", "os"],
    "Joel": ["joel", "jl"],
    "Amós": ["amos", "am"],
    "Obadias": ["obadias", "ob"],
    "Jonas": ["jonas", "jn"],
    "Miqueias": ["miqueias", "mq"],
    "Naum": ["naum", "na"],
    "Habacuque": ["habacuque", "hc"],
    "Sofonias": ["sofonias", "sf"],
    "Ageu": ["ageu", "ag"],
    "Zacarias": ["zacarias", "zc"],
    "Malaquias": ["malaquias", "ml"],
    "Mateus": ["mateus", "mt"],
    "Marcos": ["marcos", "mc"],
    "Lucas": ["lucas", "lc"],
    "João": ["joao", "jo"],
    "Atos": ["atos", "atos dos apostolos", "at"],
    "Romanos": ["romanos", "rm"],
    "1 Coríntios": ["1 corintios", "1 co"],
    "2 Coríntios": ["2 corintios", "2 co"],
    "Gálatas": ["galatas", "gl"],
    "Efésios": ["efesios", "ef"],
    "Filipenses": ["filipenses", "fp"],
    "Colossenses": ["colossenses", "cl"],
    "1 Tessalonicenses": ["1 tessalonicenses", "1 ts"],
    "2 Tessalonicenses": ["2 tessalonicenses", "2 ts"],
    "1 Timóteo": ["1 timoteo", "1 tm"],
    "2 Timóteo": ["2 timoteo", "2 tm"],
    "Tito": ["tito", "tt"],
    "Filemom": ["filemom", "fm"],
    "Hebreus": ["hebreus", "hb"],
    "Tiago": ["tiago", "tg"],
    "1 Pedro": ["1 pedro", "1 pe"],
    "2 Pedro": ["2 pedro", "2 pe"],
    "1 João": ["1 joao", "1 jo"],
    "2 João": ["2 joao", "2 jo"],
    "3 João": ["3 joao", "3 jo"],
    "Judas": ["judas", "jd"],
    "Apocalipse": ["apocalipse", "ap"],
}
# Abreviações ambíguas ("jo", "is", "at"...) só valem se seguidas de capítulo; todas exigem capítulo.
_ALIASES: dict[str, str] = {}
for _canon, _names in _BOOKS.items():
    for _n in _names:
        _ALIASES.setdefault(_n, _canon)
# Nome completo tem prioridade sobre abreviação conflitante ("jo" → João; "jó" é Jó, mas só com acento).
_ALIASES["jo"] = "João"
_ALIASES["job"] = "Jó"

_BOOK_RE = "|".join(sorted((re.escape(a) for a in _ALIASES), key=len, reverse=True))
_REF_RE = re.compile(
    rf"(?<![\w])(?P<book>(?:[123]\s?)?(?:{_BOOK_RE}))\.?\s+(?P<ch>\d{{1,3}})"
    r"(?:\s*[:.,]\s*(?P<v1>\d{1,3})(?:\s*[-–]\s*(?P<v2>\d{1,3}))?)?(?![\w])"
)


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn").lower()


def _canonical(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", _fold(raw).replace(".", "")).strip()
    if key.startswith(("1", "2", "3")) and not key[1:2].isspace():
        key = f"{key[0]} {key[1:]}"
    return _ALIASES.get(key)


def reference_spans(text: str) -> list[tuple[int, int]]:
    """Posições das passagens no texto original, para realçar. Abreviações curtas só valem com versículo."""
    folded = _fold(text)
    if len(folded) != len(text):
        return []
    spans = []
    for m in _REF_RE.finditer(folded):
        if len(m.group("book").strip(" 123.")) <= 3 and not m.group("v1"):
            continue
        spans.append(m.span())
    return spans


def parse_reference(text: str) -> list[tuple[str, int, str]]:
    """Retorna (livro, capítulo, versículos|'') para cada passagem encontrada no texto."""
    found: list[tuple[str, int, str]] = []
    text = re.sub(r"(?<![\w])Jó(?=\.?\s+\d)", "job", text)  # "Jó" com acento não é "Jo" (João)
    for m in _REF_RE.finditer(_fold(text)):
        book = _canonical(m.group("book"))
        if not book:
            continue
        verses = ""
        if m.group("v1"):
            verses = m.group("v1") + (f"-{m.group('v2')}" if m.group("v2") else "")
        found.append((book, int(m.group("ch")), verses))
    return found


def collect_references(summary: dict) -> list[tuple[str, int, str]]:
    """Une as referências listadas pela LLM com as achadas no corpo do resumo, sem duplicar."""
    chunks: list[str] = [str(r) for r in summary.get("referencias") or []]
    chunks.append(str(summary.get("texto_base") or ""))
    chunks += [str(p) for p in summary.get("pontos") or []]
    chunks += [str(p) for p in summary.get("aplicacoes") or []]
    chunks.append(str(summary.get("conclusao") or ""))
    seen: dict[tuple[str, int, str], None] = {}
    for chunk in chunks:
        for ref in parse_reference(chunk):
            seen.setdefault(ref)
    return list(seen)


def _tag(text: str) -> str:
    return re.sub(r"[^\w/-]", "", re.sub(r"[ :.]", "_", text))


def _label(ref: tuple[str, int, str]) -> str:
    book, ch, verses = ref
    return f"{book} {ch}" + (f":{verses}" if verses else "")


def _tags(refs: list[tuple[str, int, str]], speaker: Speaker | None) -> list[str]:
    tags = ["ebd", "aula"]
    if speaker:
        tags.append(f"palestrante/{_tag(speaker.name)}")
    for book in dict.fromkeys(r[0] for r in refs):
        tags.append(f"livro/{_tag(book)}")
    for ref in refs:
        tags.append(f"passagem/{_tag(_label(ref))}")
    return list(dict.fromkeys(tags))


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _with_scripture(summary: dict) -> dict:
    if summary.get("escrituras"):
        return summary
    from backend.app.services.bible import attach_scripture

    return attach_scripture(summary)


def _verse_lines(passage: dict) -> list[str]:
    lines = [f"### {passage.get('label') or ''}", ""]
    for item in passage.get("items") or []:
        lines.append(f"> {item['n']} {item['text']}")
    if passage.get("truncated"):
        lines.append(">")
        lines.append("> …")
    url = passage.get("url")
    if url:
        lines += ["", f"Fonte: [ARC]({url})"]
    lines.append("")
    return lines


def render_markdown(lecture: Lecture, speaker: Speaker | None, summary: dict) -> str:
    summary = _with_scripture(summary)
    title = summary.get("titulo") or "Resumo da aula"
    refs = collect_references(summary)
    role = (speaker.role or "").strip() if speaker else ""
    lines = [
        "---",
        f"titulo: {_q(title)}",
        f"palestrante: {_q(speaker.name if speaker else '')}",
        f"funcao: {_q(role)}",
        f"data: {lecture.lecture_date.isoformat() if lecture.lecture_date else ''}",
        f"pasta: {_q(lecture.drive_path)}",
        "passagens: [" + ", ".join(_q(_label(r)) for r in refs) + "]",
        "tags:",
        *[f"  - {t}" for t in _tags(refs, speaker)],
        "---",
        "",
        f"# {title}",
        "",
    ]
    if speaker and speaker.name:
        who = f"{speaker.name} · {role}" if role else speaker.name
        if lecture.lecture_date:
            who += f" · {lecture.lecture_date.strftime('%d/%m/%Y')}"
        lines += [who, ""]

    escrituras = [p for p in (summary.get("escrituras") or []) if p.get("verses")]
    texto = str(summary.get("texto_base") or "").strip()
    if texto:
        lines += ["## Texto-base", "", texto, ""]
    if refs:
        lines += ["## Passagens citadas", ""] + [f"- [[{_label(r).replace(':', '.')}]]" for r in refs] + [""]
    if summary.get("pontos"):
        lines += ["## Pontos principais", ""] + [f"- {p}" for p in summary["pontos"]] + [""]
    if summary.get("aplicacoes"):
        lines += ["## Aplicações", ""] + [f"- {p}" for p in summary["aplicacoes"]] + [""]
    if summary.get("conclusao"):
        lines += ["## Conclusão", "", str(summary["conclusao"]), ""]
    if str(summary.get("encerramento") or "").strip():
        lines += ["## Encerramento da EBD", "", str(summary["encerramento"]).strip(), ""]
    if escrituras:
        lines += ["## Escrituras (ARC)", ""]
        for passage in escrituras:
            lines += _verse_lines(passage)
    return "\n".join(lines)


def note_path(lecture: Lecture) -> Path:
    stem = Path(lecture.video_filename).stem
    name = f"{lecture.lecture_date or 'sem-data'} - {stem}.md".replace("/", "-")
    return settings.obsidian_dir / name


def export_lecture(lecture: Lecture, speaker: Speaker | None, summary: dict) -> Path:
    dest = note_path(lecture)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_markdown(lecture, speaker, summary), encoding="utf-8")
    return dest


def export_all(db: Session) -> tuple[int, int]:
    """Regera as notas de todas as aulas com resumo salvo. Retorna (exportadas, sem resumo)."""
    done = skipped = 0
    for lecture in db.query(Lecture).filter(Lecture.summarized.is_(True)).all():
        try:
            summary = json.loads(lecture.summary_text)
        except (ValueError, TypeError):
            skipped += 1
            continue
        speaker = db.get(Speaker, lecture.speaker_id) if lecture.speaker_id else None
        export_lecture(lecture, speaker, summary)
        done += 1
    return done, skipped
