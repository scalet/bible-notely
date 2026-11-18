"""Integração: .pps reais do Cantor Cristão via LibreOffice → .odp → content.xml."""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from backend.app.services import hymnal


def _sample_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env = os.environ.get("CANTOR_CRISTAO_PPS") or os.environ.get("CANTOR_CRISTAO_DIR") or ""
    if env:
        root = Path(env).expanduser()
        if root.is_file() and root.suffix.lower() == ".pps":
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(sorted(root.rglob("*.pps")))
    fixtures = here / "fixtures" / "hymnal"
    if fixtures.is_dir():
        candidates.extend(sorted(fixtures.glob("*.pps")))
    extra = [
        Path("/workspace/hinos") / "Cantor Cristão Power Point zip",
        Path.home() / "hinos" / "Cantor Cristão Power Point zip",
    ]
    for folder in extra:
        if folder.is_dir():
            candidates.extend(sorted(folder.glob("*.pps")))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    # Prefere 001 e 006 quando existirem.
    preferred = [path for path in unique if path.name.startswith(("001 ", "006 "))]
    return (preferred or unique)[:2]


@unittest.skipUnless(shutil.which("soffice") or shutil.which("libreoffice"), "sem LibreOffice")
class RealPpsHymnalTest(unittest.TestCase):
    def test_real_pps_extracts_lyrics(self) -> None:
        samples = _sample_paths()
        if not samples:
            self.skipTest("sem .pps do Cantor Cristão")
        import tempfile

        with tempfile.TemporaryDirectory(prefix="hymnal-pps-") as tmp:
            texts = hymnal.convert_slides(samples, Path(tmp))
            self.assertEqual(len(texts), len(samples))
            joined = "\n".join(path.read_text(encoding="utf-8") for path in texts)
            words = [item for item in joined.split() if item]
            self.assertGreater(len(words), 20, joined[:400])
            # O HTML do Impress só trazia títulos; a letra precisa aparecer.
            self.assertNotEqual(joined.strip(), "")
            lowered = hymnal.fold(joined)
            if any(path.name.startswith("006") for path in samples):
                self.assertIn("labio meu", lowered)
                self.assertIn("gloria ao senhor", lowered)
                self.assertNotIn("1/4", joined)
            if any(path.name.startswith("001") for path in samples):
                self.assertGreater(len(words), 10)
