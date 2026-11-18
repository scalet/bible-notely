import tempfile
import unittest
from pathlib import Path

from PIL import Image

from backend.app.services.compress import CompressConfig, build_ffmpeg_command
from backend.app.services.hymnal import SongText, clean_stanzas, parse_hymn
from backend.app.services.overlay_cards import OverlayCard, render_hymn_card, render_verse_card
from backend.app.services.overlays import Cue, lyric_cues_for_span, pack_cues, verse_cues_for_span
from backend.app.services.songs import detect_songs
from backend.app.services.verses import parse_segments

PPS_STYLE = """6 - “GLÓRIA AO SENHOR”
1..A NOSSO PAI DO CÉU,
TRIBUTA LÁBIO MEU
GLÓRIA E LOUVOR!
A QUEM SEU FILHO DEU,
O QUAL POR NÓS MORREU!
A QUEM ME PROSTRO EU:
GLÓRIA AO SENHOR!

6 - “GLÓRIA AO SENHOR”
2..LOUVEMOS AO SENHOR,
O SANTO REDENTOR,
O REI JESUS!
"""

ONDE_QUER = SongText(
    key="cc:299",
    number="299",
    title="Onde Quer Que Seja",
    stanzas=(
        "Onde quer, onde quer\nque Deus me mandar,\nperto do meu Salvador\neu quero andar.",
        "Seja, pois, para onde quer que me levar,\nacharei com ele ali meu doce lar.",
    ),
    source="cantor",
)

GLORIA = SongText(
    key="cc:6",
    number="6",
    title="Glória ao Senhor",
    stanzas=(
        "A nosso Pai do céu,\ntributa lábio meu\nglória e louvor!\nA quem seu Filho deu,\no qual por nós morreu!",
        "Louvemos ao Senhor,\no santo Redentor,\no Rei Jesus!\nSua morte me remiu,\na mim tão pecador",
    ),
    source="cantor",
)


class LyricCleanupTest(unittest.TestCase):
    """(c) restos dos slides .pps não entram na letra."""

    def test_pps_title_number_and_stanza_markers_are_removed(self) -> None:
        hymn = parse_hymn("006 Glória ao Senhor.txt", PPS_STYLE)
        assert hymn is not None
        cues = lyric_cues_for_span(hymn.title, hymn.number, hymn.stanzas, 100, 140)
        joined = "\n".join(line for cue in cues for line in cue.lines)
        self.assertNotIn("“", joined)
        self.assertNotIn("6 -", joined)
        self.assertNotIn("1..", joined)
        self.assertNotIn("2..", joined)
        self.assertEqual(cues[0].lines[0], "A nosso Pai do céu,")
        self.assertEqual(cues[0].label, "1ª estrofe")
        self.assertEqual(cues[-1].label, "2ª estrofe")
        self.assertEqual(cues[-1].lines[0], "Louvemos ao Senhor,")

    def test_cleaning_index_entries_already_imported(self) -> None:
        dirty = ['6 - “GLÓRIA AO SENHOR”\n2..LOUVEMOS AO SENHOR,\nO SANTO REDENTOR,', "•A GRAÇA DE NOSSO SENHOR"]
        cleaned = clean_stanzas(dirty, "6", "Glória ao Senhor")
        self.assertEqual(cleaned[0], "Louvemos ao Senhor,\no santo Redentor,")
        self.assertEqual(cleaned[1], "A graça de nosso Senhor")

    def test_lyric_cues_have_clean_lines_and_a_stanza_label(self) -> None:
        cues = lyric_cues_for_span("Glória ao Senhor", "6", ['6 - “GLÓRIA AO SENHOR”\n2..LOUVEMOS AO SENHOR,'], 10, 20)
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].lines, ("Louvemos ao Senhor,",))
        self.assertEqual(cues[0].label, "2ª estrofe")


class FalsePositiveTest(unittest.TestCase):
    """(a) uma menção ou palavras soltas não viram hino."""

    def test_sermon_words_that_brush_a_hymn_are_not_shown(self) -> None:
        transcript = "\n".join(
            [
                "[00:01:40] Onde quer que Deus me mandar eu vou, dizia aquele irmão,",
                "[00:01:50] porque o Salvador estava perto dele, e ele queria andar com Deus no lar e no trabalho.",
                "[00:02:05] Então, queridos irmãos, a unidade da igreja começa em casa.",
            ]
        )
        hits, _unknown = detect_songs(parse_segments(transcript), [ONDE_QUER], [])
        self.assertEqual(hits, [])

    def test_announcing_the_hymn_number_alone_is_not_enough(self) -> None:
        transcript = "\n".join(
            [
                "[00:29:23] e vamos cantar o hino de número 6, Glória ao Senhor",
                "[00:29:29] vamos ficar de pé para cantar",
            ]
        )
        hits, _unknown = detect_songs(parse_segments(transcript), [GLORIA], [])
        self.assertEqual(hits, [])

    def test_several_sung_lines_are_accepted(self) -> None:
        transcript = "\n".join(
            [
                "[00:29:34] a nosso pai do céu tributa lábio meu glória e louvor",
                "[00:29:44] a quem seu filho deu o qual por nós morreu",
                "[00:29:57] louvemos ao senhor o santo redentor o rei jesus",
            ]
        )
        hits, _unknown = detect_songs(parse_segments(transcript), [GLORIA, ONDE_QUER], [])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].number, "6")
        self.assertGreaterEqual(hits[0].line_hits, 2)


class OverlapTest(unittest.TestCase):
    """(b) nunca dois hinos na tela; nada fora do trecho cantado."""

    def test_two_hymns_never_share_the_screen(self) -> None:
        a = lyric_cues_for_span("Filhos de Deus", "364", ["linha um\nlinha dois", "linha três\nlinha quatro"], 1956, 1989)
        b = lyric_cues_for_span("Cristo Volta", "102", ["cristo volta\nnosso amado"], 1977, 2002)
        packed = pack_cues([*a, *b])
        numbers = {cue.number for cue in packed}
        self.assertEqual(numbers, {"364"})
        for left, right in zip(packed, packed[1:]):
            self.assertLessEqual(left.end, right.start + 1e-6)

    def test_lyric_never_runs_past_its_sung_span(self) -> None:
        cues = lyric_cues_for_span("Glória ao Senhor", "6", ["a\nb", "c\nd", "e\nf"], 100, 112)
        self.assertAlmostEqual(cues[0].start, 100)
        self.assertAlmostEqual(cues[-1].end, 112)

    def test_detection_keeps_only_the_strongest_song_in_a_window(self) -> None:
        transcript = "\n".join(
            [
                "[00:29:34] a nosso pai do céu tributa lábio meu glória e louvor",
                "[00:29:44] a quem seu filho deu o qual por nós morreu",
                "[00:29:50] onde quer onde quer que deus me mandar",
            ]
        )
        hits, _ = detect_songs(parse_segments(transcript), [GLORIA, ONDE_QUER], [])
        for left, right in zip(hits, hits[1:]):
            self.assertLessEqual(left.end, right.start + 1.0)

    def test_verse_wins_over_lyric_without_overlap(self) -> None:
        verse = verse_cues_for_span("João 17:23", "Eu neles, e tu em mim.", 10, 16)
        lyric = lyric_cues_for_span("Glória ao Senhor", "6", ["a\nb"], 12, 30)
        packed = pack_cues([*verse, *lyric])
        for left, right in zip(packed, packed[1:]):
            self.assertLessEqual(left.end, right.start + 1e-6)


class VersePagingTest(unittest.TestCase):
    """(d) versículo longo vira telas seguidas, sem reticências."""

    def test_long_verse_is_split_without_ellipsis(self) -> None:
        text = (
            "Eu neles, e tu em mim, para que eles sejam perfeitos em unidade, e para que o mundo "
            "conheça que tu me enviaste a mim e que tens amado a eles como me tens amado a mim."
        )
        # Cabe no cartão (2 linhas em pixels); não inventa segunda tela nem reticências.
        cues = verse_cues_for_span("João 17:23", text, 195, 201)
        self.assertEqual(len(cues), 1)
        self.assertTrue(all("…" not in cue.body for cue in cues))
        rebuilt = " ".join(" ".join(cue.lines) for cue in cues)
        self.assertEqual(rebuilt.split(), text.split())
        long_text = (
            text + " Porque Deus enviou o seu Filho ao mundo, não para que condenasse o mundo, "
            "mas para que o mundo fosse salvo por ele; quem crê nele não é condenado."
        )
        long_cues = verse_cues_for_span("João 17:23", long_text, 195, 220)
        self.assertGreaterEqual(len(long_cues), 2)
        self.assertTrue(all("…" not in cue.body for cue in long_cues))
        self.assertEqual(" ".join(" ".join(cue.lines) for cue in long_cues).split(), long_text.split())
        for left, right in zip(long_cues, long_cues[1:]):
            self.assertAlmostEqual(left.end, right.start)
            last = left.lines[-1].split()[-1] if left.lines else left.body.split()[-1]
            self.assertNotIn(last.strip(".,;:!?").lower(), {"que", "e", "de", "do", "da", "tu", "para", "com"})


class NovemberCardTest(unittest.TestCase):
    def test_cards_are_transparent_png_with_a_gold_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hymn = render_hymn_card(
                Path(tmp) / "h.png",
                number="327",
                title="O Piloto",
                label="2ª estrofe",
                lines=["Como sabe serenar", "boa mãe o filho seu,"],
                instrumental=True,
                size=(1280, 720),
            )
            verse = render_verse_card(Path(tmp) / "v.png", reference="João 17:23", text="Eu neles, e tu em mim.", size=(1280, 720))
            for path in (hymn, verse):
                img = Image.open(path).convert("RGBA")
                self.assertEqual(img.size, (1280, 720))
                self.assertEqual(img.getpixel((5, 5))[3], 0)
                golds = [px for px in img.getdata() if px[0] > 200 and 160 < px[1] < 200 and px[2] < 120 and px[3] > 200]
                self.assertTrue(golds)

    def test_cards_become_overlay_inputs_not_ass(self) -> None:
        cards = [OverlayCard(path=Path("/tmp/a.png"), start=10, end=20, fade_in=0.35, fade_out=0.35)]
        cmd = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), CompressConfig(), cards=cards)
        self.assertIn("-filter_complex", cmd)
        self.assertNotIn("-vf", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("overlay=0:0", graph)
        self.assertIn("between(t,10.000,20.000)", graph)
        self.assertEqual(cmd[cmd.index("-map") + 1], "[vout]")


if __name__ == "__main__":
    unittest.main()


class RetouchTest(unittest.TestCase):
    def test_uppercase_slides_become_sentence_case(self) -> None:
        from backend.app.services.hymnal import normal_case

        text = normal_case("A NOSSO PAI DO CÉU,\nTRIBUTA LÁBIO MEU\nGLÓRIA E LOUVOR! O ESPÍRITO SANTO E JESUS CRISTO,\nSENHOR DEUS DO BRASIL")
        self.assertEqual(
            text,
            "A nosso Pai do céu,\ntributa lábio meu\nglória e louvor! O Espírito Santo e Jesus Cristo,\nSenhor Deus do Brasil",
        )
        self.assertEqual(normal_case("Já em caixa normal, Senhor."), "Já em caixa normal, Senhor.")

    def test_cues_show_lyrics_in_normal_case(self) -> None:
        cues = lyric_cues_for_span("Glória ao Senhor", "6", ['6 - “GLÓRIA AO SENHOR”\n1..A NOSSO PAI DO CÉU,\nTRIBUTA LÁBIO MEU'], 0, 10)
        self.assertEqual(cues[0].lines, ("A nosso Pai do céu,", "tributa lábio meu"))

    def test_long_verse_breaks_on_punctuation_by_pixels(self) -> None:
        from backend.app.services.overlays import _WEAK_END, split_verse_pages

        text = (
            "Porque Deus amou o mundo de tal maneira que deu o seu Filho unigênito, para que todo aquele "
            "que nele crê não pereça, mas tenha a vida eterna. Porque Deus enviou o seu Filho ao mundo, "
            "não para que condenasse o mundo, mas para que o mundo fosse salvo por ele; quem crê nele não é "
            "condenado, mas quem não crê já está condenado, porquanto não crê no nome do unigênito Filho de Deus."
        )
        pages = split_verse_pages(text, max_lines=3)
        self.assertGreaterEqual(len(pages), 2)
        self.assertEqual(" ".join(pages).split(), text.split())
        for page in pages[:-1]:
            last = page.split()[-1]
            self.assertNotIn(last.strip(".,;:!?").lower(), _WEAK_END)
            self.assertTrue(last.endswith((",", ";", ".", ":", "!", "?")), page)

    def test_short_verse_stays_on_one_screen(self) -> None:
        from backend.app.services.overlays import split_verse_pages

        self.assertEqual(len(split_verse_pages("Eu sou a ressurreição e a vida.", max_lines=3)), 1)


def _sample_odp(path: Path, pages: list[list[str]]) -> None:
    """ODP mínimo com text:p e text:line-break, como o LibreOffice gera do .pps."""
    import zipfile
    from xml.sax.saxutils import escape

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"',
        ' xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"',
        ' xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"',
        ' xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0">',
        "<office:body><office:presentation>",
    ]
    for page in pages:
        parts.append("<draw:page><draw:frame><draw:text-box>")
        for block in page:
            lines = block.split("\n")
            parts.append("<text:p>")
            for index, line in enumerate(lines):
                if index:
                    parts.append("<text:span><text:line-break/></text:span>")
                parts.append(f"<text:span>{escape(line)}</text:span>")
            parts.append("</text:p>")
        parts.append("</draw:text-box></draw:frame></draw:page>")
    parts.append("</office:presentation></office:body></office:document-content>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.presentation")
        archive.writestr("content.xml", "\n".join(parts))


class SlideHymnalTest(unittest.TestCase):
    PAGES = [
        ["6 - “GLÓRIA AO SENHOR”", "1..A NOSSO PAI DO CÉU,\nTRIBUTA LÁBIO MEU"],
        ["6 - “GLÓRIA AO SENHOR”", "2..LOUVEMOS AO SENHOR,\nO SANTO REDENTOR,"],
    ]

    def test_zip_of_pps_is_converted_and_indexed(self) -> None:
        from unittest import mock

        from backend.app.services import hymnal

        def fake_run(cmd, **kwargs):
            self.assertIn("odp", cmd)
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            for name in cmd[cmd.index("--outdir") + 2 :]:
                _sample_odp(outdir / f"{Path(name).stem}.odp", self.PAGES)
            return mock.Mock(returncode=0, stdout="")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "Cantor_Cristão.zip"
            import zipfile

            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("Cantor Cristão Power Point zip/006 Glória ao Senhor.pps", b"binario")
            with mock.patch("shutil.which", return_value="/usr/bin/soffice"), mock.patch("subprocess.run", side_effect=fake_run):
                count = hymnal.import_hymnal(archive, root)
            self.assertEqual(count, 1)
            [song] = hymnal.load_hymns(root)
            self.assertEqual((song.number, song.title), ("6", "Glória ao Senhor"))
            self.assertEqual(len(song.stanzas), 2)
            cleaned = clean_stanzas(song.stanzas, song.number, song.title)
            self.assertEqual(cleaned[0], "A nosso Pai do céu,\ntributa lábio meu")
            self.assertEqual(cleaned[1], "Louvemos ao Senhor,\no santo Redentor,")

    def test_quoted_and_unquoted_titles_are_chrome(self) -> None:
        from backend.app.services.hymnal import is_slide_chrome

        self.assertTrue(is_slide_chrome("6 - “GLÓRIA AO SENHOR”"))
        self.assertTrue(is_slide_chrome("6 - GLÓRIA AO SENHOR"))
        self.assertTrue(is_slide_chrome("“GLÓRIA AO SENHOR”"))
        self.assertTrue(is_slide_chrome('"Glória ao Senhor"'))
        self.assertTrue(is_slide_chrome("1/4"))
        self.assertFalse(is_slide_chrome("A NOSSO PAI DO CÉU,"))
        self.assertFalse(is_slide_chrome("tributa lábio meu glória e louvor"))

    def test_missing_libreoffice_is_a_clear_error(self) -> None:
        from unittest import mock

        from backend.app.services import hymnal

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "pps"
            folder.mkdir()
            (folder / "001 Antífona.pps").write_bytes(b"x")
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(hymnal.HymnalError):
                    hymnal.import_hymnal(folder, Path(tmp))
