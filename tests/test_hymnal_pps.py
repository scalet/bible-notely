"""Hinário: fixtures de texto versionadas e, se possível, .pps reais via LibreOffice."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from backend.app.services import hymnal

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hymnal"
CURLY = set("“”«»")


def _load_pages(name: str) -> list[list[str]]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    pages = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == "---":
            if current:
                pages.append(current)
                current = []
            continue
        if line.strip():
            current.append(line)
    if current:
        pages.append(current)
    return pages


def _sample_pps() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("CANTOR_CRISTAO_PPS") or os.environ.get("CANTOR_CRISTAO_DIR") or ""
    if env:
        root = Path(env).expanduser()
        if root.is_file() and root.suffix.lower() == ".pps":
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(sorted(root.rglob("*.pps")))
    if FIXTURES.is_dir():
        candidates.extend(sorted(FIXTURES.glob("*.pps")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    preferred = [path for path in unique if path.name.startswith(("001 ", "006 "))]
    return (preferred or unique)[:2]


class HymnalFixtureTest(unittest.TestCase):
    def test_001_fixture_has_four_stanzas_and_no_curly_quotes(self) -> None:
        pages = _load_pages("001-antifona.slides.txt")
        text = hymnal._pages_to_stanza_text(pages, number="1", title="Antífona")
        song = hymnal.parse_hymn("001 Antífona.txt", text)
        self.assertIsNotNone(song)
        cleaned = hymnal.clean_stanzas(song.stanzas, song.number, song.title)
        self.assertEqual(len(cleaned), 4, cleaned)
        joined = "\n".join(cleaned)
        self.assertFalse(CURLY & set(joined), joined)
        self.assertIn("fiel e bom Senhor", joined)
        self.assertIn("Aleluia", joined)

    def test_006_fixture_keeps_lyrics_without_slide_title(self) -> None:
        pages = _load_pages("006-gloria-ao-senhor.slides.txt")
        text = hymnal._pages_to_stanza_text(pages, number="6", title="Glória ao Senhor")
        song = hymnal.parse_hymn("006 Glória ao Senhor.txt", text)
        cleaned = hymnal.clean_stanzas(song.stanzas, song.number, song.title)
        self.assertGreaterEqual(len(cleaned), 4, cleaned)
        joined = "\n".join(cleaned)
        self.assertFalse(CURLY & set(joined), joined)
        self.assertIn("labio meu", hymnal.fold(joined))
        self.assertNotIn("1/4", joined)

    def test_real_lyric_lines_are_not_treated_as_titles(self) -> None:
        pages = [
            [
                "1 “A Ti ó Deus”",
                "A TI, Ó DEUS, FIEL E BOM SENHOR",
                "“Santo, santo, santo”",
                "3 - Cristo vive em mim",
            ]
        ]
        text = hymnal._pages_to_stanza_text(pages, number="1", title="A Ti ó Deus")
        self.assertNotIn("A Ti ó Deus", text)
        self.assertIn("Santo, santo, santo", text)
        self.assertIn("3 - Cristo vive em mim", text)
        self.assertFalse(hymnal.is_slide_chrome("“Santo, santo, santo”"))
        self.assertFalse(hymnal.is_slide_chrome("3 - Cristo vive em mim"))


@unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "sem LibreOffice")
class RealPpsHymnalTest(unittest.TestCase):
    def test_real_pps_extracts_lyrics(self) -> None:
        samples = _sample_pps()
        if not samples:
            self.skipTest("sem .pps do Cantor Cristão")
        with tempfile.TemporaryDirectory(prefix="hymnal-pps-") as tmp:
            texts = hymnal.convert_slides(samples, Path(tmp))
            self.assertEqual(len(texts), len(samples))
            joined = "\n".join(path.read_text(encoding="utf-8") for path in texts)
            self.assertGreater(len(joined.split()), 20, joined[:400])
            if any(path.name.startswith("001") for path in samples):
                song = hymnal.parse_hymn(texts[0].name, texts[0].read_text(encoding="utf-8"))
                cleaned = hymnal.clean_stanzas(song.stanzas, song.number, song.title)
                self.assertEqual(len(cleaned), 4, cleaned)
                self.assertFalse(CURLY & set("\n".join(cleaned)))
            if any(path.name.startswith("006") for path in samples):
                self.assertIn("labio meu", hymnal.fold(joined))
                self.assertIn("gloria ao senhor", hymnal.fold(joined))
                self.assertNotIn("1/4", joined)
