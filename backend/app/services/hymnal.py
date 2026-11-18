"""Cantor Cristão: importação e índice local.

O zip ou a pasta (hinos em .txt ou slides .pps/.pptx) fica na pasta "Igreja" do Drive
ou num caminho local. Slides são convertidos com LibreOffice headless.
O índice vai para data/hymnal e não entra no git (data/ já é ignorado).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.app.config import settings
from backend.app.services.textnorm import fold

log = logging.getLogger("bible_notely.hymnal")

HYMNAL_NAMES = (
    "Cantor_Cristão.zip",
    "Cantor_Cristao.zip",
    "Cantor Cristão.zip",
    "Cantor Cristao.zip",
)
_FILENAME = re.compile(r"^(\d{1,4})\s*[-–.]?\s*(.*?)\s*$")
_HEADER = re.compile(r"(?i)^hino\s+\d+\s*$")
_STANZA = re.compile(r"(?i)^(?:coro|refr[aã]o|chorus|est(?:rofe)?\.?\s*\d+|\d{1,2})\s*[:.)]?\s*$")
_NUMBER_QUERY = re.compile(r"(?i)^(?:hino|n[oº°]?\.?|cc|cantor(?:\s+cristao)?)?\s*(\d{1,4})\s*$")



_SLIDE_TITLE = re.compile(
    r"^\d{1,4}\s*(?:[-–—]\s*)?[“\"«].{1,80}[”\"»]\s*$"
)
_QUOTED_LINE = re.compile(r"^[“\"«].{1,80}[”\"»]\s*$")
_PAGE_MARK = re.compile(r"^\d{1,2}\s*/\s*\d{1,2}$")
_STROPH_MARK = re.compile(r"^(?:\d{1,2}\s*\.{1,2}|[•●])\s*")


def _bare_title(line: str) -> str:
    return fold((line or "").strip().strip("“”\"«»").strip())


def is_slide_chrome(line: str, number: str = "", title: str = "", *, first_on_slide: bool = False) -> bool:
    """Título e número que vêm dos slides .pps, não fazem parte da letra.

    `N “Título”` (com ou sem travessão) é sempre chrome. Uma linha só entre aspas
    só cai fora se for a primeira do slide ou igual ao título do hino. Versos
    como “Santo, santo, santo” ou `3 - Cristo vive em mim` permanecem.
    """
    stripped = (line or "").strip()
    if not stripped:
        return False
    if _HEADER.match(stripped) or _PAGE_MARK.match(stripped) or _SLIDE_TITLE.match(stripped):
        return True
    aliases = {fold(title)} if title else set()
    if number and title:
        aliases.update(
            {
                fold(f"{number} {title}"),
                fold(f"{number} - {title}"),
                fold(f"{number} – {title}"),
            }
        )
    if aliases and (_bare_title(stripped) in aliases or fold(stripped) in aliases):
        return True
    if first_on_slide and _QUOTED_LINE.match(stripped):
        return True
    return False


def stanza_number(stanza: str) -> int | None:
    """Número da estrofe pelo marcador do slide ('2..'), se houver."""
    for raw in str(stanza).splitlines():
        match = re.match(r"^\s*(\d{1,2})\s*\.{1,2}\s*\S", raw)
        if match:
            return int(match.group(1))
    return None


def clean_lyric_line(line: str) -> str:
    """Tira '2..', '1.' e bullets na frente da linha da estrofe."""
    return _STROPH_MARK.sub("", (line or "").strip()).strip()


_DIVINE = {
    "deus": "Deus", "senhor": "Senhor", "jesus": "Jesus", "cristo": "Cristo", "pai": "Pai",
    "espírito": "Espírito", "espirito": "Espírito", "salvador": "Salvador", "redentor": "Redentor",
    "jeová": "Jeová", "emanuel": "Emanuel", "messias": "Messias", "cordeiro": "Cordeiro",
    "trindade": "Trindade", "rei": "Rei", "filho": "Filho",
    "brasil": "Brasil", "israel": "Israel", "sião": "Sião", "jerusalém": "Jerusalém",
    "belém": "Belém", "calvário": "Calvário", "galileia": "Galileia", "canaã": "Canaã",
}
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)


def _mostly_upper(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ch.isupper() for ch in letters) / len(letters) > 0.8


def normal_case(stanza: str) -> str:
    """MAIÚSCULAS dos slides -> caixa de frase, como no culto de novembro.

    Início da estrofe e depois de . ! ? fica em maiúscula; Deus, Senhor, Jesus,
    Cristo, Espírito Santo, Pai e nomes próprios conhecidos também.
    """
    if not _mostly_upper(stanza):
        return stanza
    text = stanza.lower()
    text = _WORD.sub(lambda m: _DIVINE.get(m.group(0), m.group(0)), text)
    text = re.sub(r"(Espírito)\s+santo", r"\1 Santo", text)
    out: list[str] = []
    capitalize = True
    for ch in text:
        if capitalize and ch.isalpha():
            out.append(ch.upper())
            capitalize = False
            continue
        out.append(ch)
        if ch in ".!?:":
            capitalize = True
    return "".join(out)


def clean_stanzas(stanzas: tuple[str, ...] | list[str], number: str = "", title: str = "") -> tuple[str, ...]:
    """Letra só, sem restos de slide (título entre aspas, número, marcador de estrofe)."""
    cleaned: list[str] = []
    for stanza in stanzas:
        lines = []
        for raw in str(stanza).replace("\r\n", "\n").split("\n"):
            if is_slide_chrome(raw, number, title):
                continue
            line = clean_lyric_line(raw)
            if line:
                lines.append(line)
        if lines:
            cleaned.append(normal_case("\n".join(lines)))
    return tuple(cleaned)


class HymnalError(RuntimeError):
    pass


@dataclass(frozen=True)
class SongText:
    key: str
    number: str
    title: str
    stanzas: tuple[str, ...]
    source: str  # cantor | library | skip


def hymnal_dir(root: Path | None = None) -> Path:
    return (root or settings.data_dir) / "hymnal" / "cantor-cristao"


def index_path(root: Path | None = None) -> Path:
    return hymnal_dir(root) / "index.json"


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def split_stanzas(text: str) -> list[str]:
    """Separa estrofes por linha em branco, número ou 'Coro'. Bloco longo vira grupos de 4 linhas."""
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        cleaned = "\n".join(line.strip() for line in current if line.strip()).strip()
        current.clear()
        if cleaned:
            chunks.append(cleaned)

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip() or _STANZA.match(line.strip()):
            flush()
            continue
        current.append(line)
    flush()
    if len(chunks) == 1:
        lines = chunks[0].splitlines()
        if len(lines) > 6:
            chunks = ["\n".join(lines[i : i + 4]) for i in range(0, len(lines), 4)]
    return chunks


def parse_hymn(filename: str, text: str) -> SongText | None:
    stem = Path(filename).stem.strip()
    number = ""
    title = stem
    match = _FILENAME.match(stem)
    if match and match.group(1):
        number = str(int(match.group(1)))
        title = (match.group(2) or "").strip(" -–.") or f"Hino {number}"
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if _HEADER.match(stripped) or is_slide_chrome(stripped, number, title):
            continue
        kept.append(line)
    stanzas = split_stanzas("\n".join(kept))
    if not stanzas:
        return None
    key = f"cc:{number}" if number else f"cc:{fold(title)}"
    return SongText(key=key, number=number, title=title, stanzas=tuple(stanzas), source="cantor")


SLIDE_SUFFIXES = {".pps", ".ppt", ".pptx", ".ppsx", ".odp"}


def _visible_text(html: str) -> str:
    """Texto visível do HTML que o LibreOffice gera para cada slide."""
    from html.parser import HTMLParser

    class _Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in {"style", "script", "head", "title"}:
                self.skip += 1
            if tag in {"p", "div", "h1", "h2", "h3", "br", "li", "tr"}:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in {"style", "script", "head", "title"} and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    parser = _Parser()
    parser.feed(html)
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    lines = [line for line in lines if line and not re.fullmatch(r"(?i)slide\s*\d+", line)]
    return "\n".join(lines).strip() + "\n"


def _page_lyric_lines(lines: list[str], number: str = "", title: str = "") -> list[str]:
    out: list[str] = []
    first = True
    for line in lines:
        stripped = line.strip()
        if not stripped or _PAGE_MARK.match(stripped):
            continue
        if is_slide_chrome(stripped, number, title, first_on_slide=first):
            first = False
            if out and out[-1] != "":
                out.append("")
            continue
        first = False
        if _STROPH_MARK.match(stripped) and re.match(r"^\d", stripped) and out and out[-1] != "":
            out.append("")
        out.append(stripped)
    return out


def _pages_to_stanza_text(pages: list[list[str]], number: str = "", title: str = "") -> str:
    """Monta estrofes a partir das linhas de cada slide, sem chrome de título."""
    chunks = ["\n".join(_page_lyric_lines(lines, number, title)).strip() for lines in pages]
    return "\n\n".join(chunk for chunk in chunks if chunk).strip() + "\n"


def _slides_to_stanza_text(text: str, number: str = "", title: str = "") -> str:
    """Cada repetição do título do slide abre um bloco: vira linha em branco entre estrofes."""
    pages: list[list[str]] = []
    current: list[str] = []
    for line in text.replace("\r\n", "\n").splitlines():
        if line.strip() == "---":
            if current:
                pages.append(current)
                current = []
            continue
        current.append(line)
    if current:
        pages.append(current)
    return _pages_to_stanza_text(pages, number, title)


def _odp_text(element) -> str:
    """Texto de um nó ODF, respeitando text:line-break e text:s."""
    from xml.etree import ElementTree as ET

    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in list(element):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag in {"line-break", "br"}:
            parts.append("\n")
        elif tag == "s":
            count = 1
            for attr, value in child.attrib.items():
                if attr == "c" or attr.endswith("}c"):
                    try:
                        count = max(1, int(value))
                    except ValueError:
                        count = 1
            parts.append(" " * count)
        elif tag == "tab":
            parts.append("\t")
        else:
            parts.append(_odp_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def odp_to_stanza_text(path: Path, number: str = "", title: str = "") -> str:
    """Lê text:p de cada draw:page em content.xml e monta as estrofes."""
    from xml.etree import ElementTree as ET

    with zipfile.ZipFile(path) as archive:
        try:
            xml = archive.read("content.xml")
        except KeyError as exc:
            raise HymnalError(f"{path.name} não tem content.xml") from exc
    root = ET.fromstring(xml)
    text_ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    draw_ns = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"
    pages = list(root.iter(f"{draw_ns}page")) or [root]
    chunks: list[str] = []
    for page in pages:
        lines: list[str] = []
        for para in page.iter(f"{text_ns}p"):
            for line in _odp_text(para).splitlines():
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)
        if lines:
            chunks.append(lines)
    return _pages_to_stanza_text(chunks, number, title)


def convert_slides(files: list[Path], dest: Path, batch: int = 25) -> list[Path]:
    """Converte .pps/.ppt/.pptx com LibreOffice headless e grava um .txt por hino."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise HymnalError("o hinário veio em slides; instale o LibreOffice (soffice) para converter")
    dest.mkdir(parents=True, exist_ok=True)
    odp_dir = dest / "_odp"
    odp_dir.mkdir(exist_ok=True)
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="lo-hymnal-") as profile:
        for index in range(0, len(files), batch):
            chunk = files[index : index + batch]
            cmd = [
                soffice, "--headless", "--nologo", "--norestore", "--nofirststartwizard",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to", "odp", "--outdir", str(odp_dir), *[str(path) for path in chunk],
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900)
            if result.returncode != 0:
                log.warning("soffice falhou num lote: %s", (result.stdout or "")[-500:])
            for path in chunk:
                odp = odp_dir / f"{path.stem}.odp"
                if not odp.is_file():
                    log.warning("slide sem conversão: %s", path.name)
                    continue
                stem = path.stem.strip()
                number = title = ""
                match = _FILENAME.match(stem)
                if match and match.group(1):
                    number = str(int(match.group(1)))
                    title = (match.group(2) or "").strip(" -–.") or f"Hino {number}"
                text = odp_to_stanza_text(odp, number=number, title=title)
                target = dest / f"{path.stem}.txt"
                target.write_text(text, encoding="utf-8")
                written.append(target)
    shutil.rmtree(odp_dir, ignore_errors=True)
    return written


def _usable(path: Path) -> bool:
    return "__MACOSX" not in path.parts and not path.name.startswith(".")


def _iter_text_files(source: Path, scratch: Path) -> list[Path]:
    if source.is_dir():
        root = source
    elif source.suffix.lower() == ".zip":
        scratch.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            archive.extractall(scratch)
        root = scratch
    else:
        raise HymnalError("informe um zip ou uma pasta com os hinos (.txt ou slides .pps/.pptx)")
    texts = [path for path in root.rglob("*.txt") if _usable(path)]
    slides = [path for path in root.rglob("*") if path.suffix.lower() in SLIDE_SUFFIXES and _usable(path)]
    have = {path.stem for path in texts}
    pending = [path for path in sorted(slides) if path.stem not in have]
    if pending:
        log.info("convertendo %s slides do hinário com LibreOffice", len(pending))
        texts += convert_slides(pending, scratch / "_txt")
    return texts


def import_hymnal(source: Path, root: Path | None = None) -> int:
    """Lê o zip ou a pasta e grava o índice em data/hymnal. Substitui o índice anterior."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise HymnalError(f"não encontrei {source}")
    dest = hymnal_dir(root)
    scratch = dest / "_raw"
    if scratch.exists():
        shutil.rmtree(scratch)
    dest.mkdir(parents=True, exist_ok=True)
    hymns: list[SongText] = []
    seen: set[str] = set()
    try:
        for path in _iter_text_files(source, scratch):
            hymn = parse_hymn(path.name, read_text(path))
            if hymn is None or hymn.key in seen:
                continue
            seen.add(hymn.key)
            hymns.append(hymn)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
    if not hymns:
        raise HymnalError("nenhum hino em .txt nesse zip ou pasta")
    hymns.sort(key=lambda item: (int(item.number) if item.number.isdigit() else 10_000, fold(item.title)))
    payload = {
        "source_name": source.name,
        "imported_at": datetime.utcnow().isoformat(timespec="seconds"),
        "count": len(hymns),
        "hymns": [
            {"number": item.number, "title": item.title, "stanzas": list(item.stanzas)}
            for item in hymns
        ],
    }
    index_path(root).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Cantor Cristão indexado: %s hinos em %s", len(hymns), index_path(root))
    return len(hymns)


def load_hymns(root: Path | None = None) -> list[SongText]:
    path = index_path(root)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("índice do hinário ilegível: %s", exc)
        return []
    hymns: list[SongText] = []
    for item in raw.get("hymns") or []:
        number = str(item.get("number") or "")
        title = str(item.get("title") or (f"Hino {number}" if number else "Hino"))
        stanzas = tuple(str(part).strip() for part in item.get("stanzas") or [] if str(part).strip())
        if not stanzas:
            continue
        key = f"cc:{number}" if number else f"cc:{fold(title)}"
        hymns.append(SongText(key=key, number=number, title=title, stanzas=stanzas, source="cantor"))
    return hymns


def find_hymn(query: str, hymns: list[SongText]) -> SongText | None:
    """Número (12, hino 12, CC 12) ou nome. Nome exige o título, não um pedaço solto de letra."""
    text = (query or "").strip()
    if not text:
        return None
    number_match = _NUMBER_QUERY.match(fold(text))
    if number_match:
        wanted = str(int(number_match.group(1)))
        for hymn in hymns:
            if hymn.number == wanted:
                return hymn
        return None
    folded = fold(text)
    exact = [hymn for hymn in hymns if fold(hymn.title) == folded]
    if exact:
        return exact[0]
    partial = [hymn for hymn in hymns if folded in fold(hymn.title) or fold(hymn.title) in folded]
    if len(partial) == 1:
        return partial[0]
    return None


def pick_drive_file(candidates: list[dict], folder_name_of) -> dict:
    """Prefere o zip cujo pai se chama Igreja. Sem isso, só aceita um único arquivo."""
    if not candidates:
        raise HymnalError(
            "não achei Cantor_Cristão.zip no Drive. Coloque o zip na pasta Igreja ou use hymnal --path."
        )
    preferred = []
    for item in candidates:
        for parent in item.get("parents") or []:
            try:
                name = folder_name_of(parent)
            except Exception:
                continue
            if fold(str(name)) == "igreja":
                preferred.append(item)
                break
    pool = preferred or (candidates if len(candidates) == 1 else [])
    if len(pool) == 1:
        return pool[0]
    if preferred:
        names = ", ".join(item.get("name") or item.get("id", "") for item in preferred)
        raise HymnalError(f"há mais de um Cantor Cristão na pasta Igreja: {names}")
    raise HymnalError("há mais de um arquivo com esse nome no Drive. Use hymnal --path.")


def import_hymnal_from_drive(root: Path | None = None) -> tuple[int, Path]:
    """Baixa o zip pelo serviço do Drive já usado pelo pipeline e indexa."""
    from backend.app.services import drive

    found: list[dict] = []
    seen: set[str] = set()
    for name in HYMNAL_NAMES:
        for item in drive.search_by_name(name):
            file_id = str(item.get("id") or "")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            found.append(item)
    chosen = pick_drive_file(found, drive.file_name)
    dest_zip = hymnal_dir(root) / "_download.zip"
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        drive.download_file(chosen["id"], dest_zip, expected_size=int(chosen.get("size") or 0))
        count = import_hymnal(dest_zip, root)
    finally:
        dest_zip.unlink(missing_ok=True)
    return count, hymnal_dir(root)
