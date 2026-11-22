import unittest

from backend.app.services.hymnal import SongText
from backend.app.services.songs import detect_songs
from backend.app.services.verses import parse_segments


CHORUS = "Glória ao Senhor para sempre\naleluia aleluia amém Senhor"


def _hymn(number: str, title: str, unique: str) -> SongText:
    return SongText(key=f"cc:{number}", number=number, title=title, stanzas=(unique, CHORUS), source="cantor")


CATALOG = [
    _hymn("1", "Hino Um", "A nosso pai do céu tributa lábio meu\nA quem seu filho deu o qual por nós morreu"),
    _hymn("2", "Hino Dois", "Onde quer que Deus me mandar eu vou"),
    _hymn("3", "Hino Três", "Castelo forte é nosso Deus"),
    _hymn("4", "Hino Quatro", "Quão grande és tu Senhor da criação"),
    _hymn("5", "Hino Cinco", "Rude cruz se erigiu e nela eu vi Jesus"),
    _hymn("6", "Hino Seis", "Jesus me ama isso sei da Bíblia é que aprendi"),
    _hymn("7", "Hino Sete", "Porque ele vive posso crer no amanhã"),
    _hymn("8", "Hino Oito", "A Deus demos glória com grande fervor"),
]


class CommonChorusFilterTest(unittest.TestCase):
    def test_shared_chorus_alone_does_not_pick_a_hymn(self) -> None:
        transcript = "\n".join(
            [
                "[00:10:00] glória ao senhor para sempre",
                "[00:10:08] aleluia aleluia amém senhor",
                "[00:10:16] glória ao senhor para sempre",
            ]
        )
        hits, _unknown = detect_songs(parse_segments(transcript), CATALOG, [])
        self.assertEqual(hits, [])

    def test_unique_lines_still_identify_the_hymn(self) -> None:
        transcript = "\n".join(
            [
                "[00:10:00] a nosso pai do céu tributa lábio meu",
                "[00:10:06] a quem seu filho deu o qual por nós morreu",
                "[00:10:10] glória ao senhor para sempre",
                "[00:10:18] aleluia aleluia amém senhor",
            ]
        )
        hits, _unknown = detect_songs(parse_segments(transcript), CATALOG, [])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].number, "1")
