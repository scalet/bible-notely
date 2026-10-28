import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from backend.app.models import Lecture, Speaker
from backend.app.services import bible, obsidian, pdf


class RenderPdfTest(unittest.TestCase):
    def test_header_includes_speaker_name_and_role(self) -> None:
        speaker = Speaker(name="Ricardo", role="Diácono", photo_path="")
        lecture = Lecture(
            video_filename="aula.mp4",
            drive_path="2025/09-Setembro/21-09",
            lecture_date=date(2025, 9, 21),
        )
        summary = {
            "titulo": "Resumo da aula",
            "texto_base": "João 3:16",
            "pontos": ["Um ponto"],
            "aplicacoes": [],
            "conclusao": "Fim.",
            "encerramento": "Oração final e avisos da semana.",
            "escrituras": [
                {
                    "book": "João",
                    "chapter": 3,
                    "verses": "16",
                    "label": "João 3:16",
                    "url": "https://www.bibliaonline.com.br/arc/jo/3",
                    "truncated": False,
                    "items": [{"n": 16, "text": "Porque Deus amou o mundo de tal maneira que deu o seu Filho unigênito."}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "resumo.pdf"
            with mock.patch.object(pdf, "_speaker_photo", return_value=None):
                pdf.render_pdf(dest, lecture, speaker, summary)
            if not shutil.which("pdftotext"):
                self.skipTest("pdftotext não está no PATH")
            text = subprocess.check_output(["pdftotext", "-layout", str(dest), "-"], text=True)
        self.assertIn("ESCOLA BÍBLICA DOMINICAL", text)
        self.assertIn("Ricardo", text)
        self.assertIn("Diácono", text)
        self.assertIn("21/09/2025", text)
        self.assertNotIn("aula.mp4", text)
        self.assertNotIn("2025-09-21", text)
        self.assertIn("Texto-base", text)
        self.assertIn("João 3:16", text)
        self.assertIn("Porque Deus amou o mundo", text)
        self.assertIn("Encerramento da EBD", text)
        self.assertIn("Oração final e avisos da semana", text)
        self.assertIn("Página 1 de 1", text)


class ObsidianNoteTest(unittest.TestCase):
    def test_note_quotes_arc_text_and_role(self) -> None:
        speaker = Speaker(name="Ricardo", role="Diácono", photo_path="")
        lecture = Lecture(
            video_filename="aula.mp4",
            drive_path="2025/09-Setembro/21-09",
            lecture_date=date(2025, 9, 21),
        )
        summary = {
            "titulo": "Resumo",
            "texto_base": "Apocalipse 11:19",
            "pontos": [],
            "aplicacoes": [],
            "conclusao": "",
            "escrituras": [
                {
                    "book": "Apocalipse",
                    "chapter": 11,
                    "verses": "19",
                    "label": "Apocalipse 11:19",
                    "url": "https://www.bibliaonline.com.br/arc/ap/11",
                    "items": [{"n": 19, "text": "E abriu-se no céu o templo de Deus."}],
                }
            ],
        }
        note = obsidian.render_markdown(lecture, speaker, summary)
        self.assertIn('funcao: "Diácono"', note)
        self.assertIn("Ricardo · Diácono · 21/09/2025", note)
        self.assertNotIn("aula.mp4", note)
        self.assertIn("> 19 E abriu-se no céu o templo de Deus.", note)
        self.assertIn("[[Apocalipse 11.19]]", note)


class RichTextTest(unittest.TestCase):
    def test_references_are_highlighted_and_text_is_escaped(self) -> None:
        html = pdf._rich("Veja João 3:16 & Romanos 8:28-30; tenho 3 filhos.")
        self.assertIn("<b>João 3:16</b>", html)
        self.assertIn("<b>Romanos 8:28-30</b>", html)
        self.assertIn("&amp;", html)
        self.assertNotIn("<b>os 3", html)

    def test_long_summary_gets_running_header_and_page_total(self) -> None:
        speaker = Speaker(name="Ricardo", role="Diácono", photo_path="")
        lecture = Lecture(video_filename="a.mp4", drive_path="2025/09-Setembro/21-09", lecture_date=date(2025, 9, 21))
        summary = {"pontos": [f"Ponto {i} com João 3:16 e texto suficiente para ocupar linhas. " * 4 for i in range(60)]}
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "r.pdf"
            with mock.patch.object(pdf, "_speaker_photo", return_value=None):
                pdf.render_pdf(dest, lecture, speaker, summary)
            if not shutil.which("pdftotext"):
                self.skipTest("pdftotext não está no PATH")
            page2 = subprocess.check_output(["pdftotext", "-f", "2", "-l", "2", "-layout", str(dest), "-"], text=True)
        self.assertIn("Resumo da Aula da Escola Bíblica Dominical - 21/09/2025", page2)
        self.assertRegex(page2, r"Página 2 de \d+")


class TranscriptTimestampTest(unittest.TestCase):
    def test_format_and_strip(self) -> None:
        from backend.app.services.transcribe import format_timestamp, strip_timestamps

        self.assertEqual(format_timestamp(0), "00:00:00")
        self.assertEqual(format_timestamp(3725.9), "01:02:05")
        text = "[00:00:03] Bom dia.\n[01:02:05] Leiam João 3:16."
        self.assertEqual(strip_timestamps(text), "Bom dia.\nLeiam João 3:16.")


class BibleHtmlTest(unittest.TestCase):
    def test_parse_verse_words_not_just_the_first_span(self) -> None:
        html = """
        <span aria-label="versículo 19">19</span>
        <span role="text" data-v=".19.">
          <span data-t="1">E </span><span data-t="2">abriu-se </span><span data-t="3">no céu.</span>
        </span>
        <span aria-label="versículo 20">20</span>
        <span role="text" data-v=".20."><span data-t="4">Outro.</span></span>
        """
        verses = bible.parse_chapter_html(html)
        self.assertEqual(verses[19], "E abriu-se no céu.")
        self.assertEqual(verses[20], "Outro.")

    def test_section_heading_is_not_part_of_the_previous_verse(self) -> None:
        html = """
        <span aria-label="versículo 32">32</span>
        <span role="text" data-v=".32."><span data-t="1">para o crer.</span></span></p>
        <h2 data-section=""><span role="text" data-v=".33."><span data-t="2">A parábola dos lavradores maus</span></span></h2>
        <p><span aria-label="versículo 33">33</span>
        <span role="text" data-v=".33."><span data-t="3">Ouvi outra parábola.</span></span></p>
        """
        verses = bible.parse_chapter_html(html)
        self.assertEqual(verses[32], "para o crer.")
        self.assertEqual(verses[33], "Ouvi outra parábola.")

    def test_inserts_missing_space_between_word_spans(self) -> None:
        html = """
        <span aria-label="versículo 1">1</span>
        <span role="text" data-v=".1.">
          <span data-t="1">o Senhor</span><span data-t="2">me levou</span><span data-t="3">, e</span>
        </span>
        """
        self.assertEqual(bible.parse_chapter_html(html)[1], "o Senhor me levou, e")

    def test_same_chapter_is_not_repeated(self) -> None:
        summary = {
            "texto_base": "2 Reis 5:1-14",
            "referencias": ["2 Reis 5:1", "2 Reis 5:11-12", "2 Reis 5:17"],
        }
        with mock.patch.object(bible, "fetch_chapter", return_value={n: f"v{n}" for n in range(1, 20)}):
            attached = bible.attach_scripture(summary)
        self.assertEqual(len(attached["escrituras"]), 1)
        self.assertEqual(attached["escrituras"][0]["verses"], "1-14,17")

    def test_job_slug_is_not_john(self) -> None:
        self.assertEqual(bible.chapter_url("Jó", 1), "https://www.bibliaonline.com.br/arc/job/1")
        self.assertEqual(bible.chapter_url("João", 3), "https://www.bibliaonline.com.br/arc/jo/3")
        self.assertEqual(bible.chapter_url("Apocalipse", 11), "https://www.bibliaonline.com.br/arc/ap/11")
