"""Cantor Cristão: importação e índice local.

O zip fica na pasta "Igreja" do Drive do usuário, ou num caminho local.
O índice vai para data/hymnal e não entra no git (data/ já é ignorado).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
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



_SLIDE_TITLE = re.compile(r"^\d{1,4}\s*[-–—]\s*[“\"«].*[”\"»]\s*$")
_STROPH_MARK = re.compile(r"^(?:\d{1,2}\s*\.{1,2}|[•●])\s*")


def is_slide_chrome(line: str, number: str = "", title: str = "") -> bool:
    """Título e número que vêm dos slides .pps, não fazem parte da letra."""
    stripped = (line or "").strip()
    if not stripped:
        return False
    if _HEADER.match(stripped) or _SLIDE_TITLE.match(stripped):
        return True
    if number and fold(stripped) in {
        fold(f"{number} {title}"),
        fold(f"{number} - {title}"),
        fold(f"{number} – {title}"),
        fold(title),
    }:
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
            cleaned.append("\n".join(lines))
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
        if _HEADER.match(stripped):
            continue
        if number and fold(stripped) in {fold(f"{number} {title}"), fold(f"{number} - {title}"), fold(title)}:
            continue
        kept.append(line)
    stanzas = split_stanzas("\n".join(kept))
    if not stanzas:
        return None
    key = f"cc:{number}" if number else f"cc:{fold(title)}"
    return SongText(key=key, number=number, title=title, stanzas=tuple(stanzas), source="cantor")


def _iter_text_files(source: Path, scratch: Path) -> list[Path]:
    if source.is_dir():
        return [path for path in source.rglob("*.txt") if "__MACOSX" not in path.parts and not path.name.startswith(".")]
    if source.suffix.lower() != ".zip":
        raise HymnalError("informe um zip ou uma pasta com os hinos em .txt")
    scratch.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        archive.extractall(scratch)
    return [path for path in scratch.rglob("*.txt") if "__MACOSX" not in path.parts and not path.name.startswith(".")]


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
