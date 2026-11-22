import unittest

from backend.app.services.verses import _align, _verse_duration, _verse_search_window, parse_segments


LONG = (
    "Eu neles, e tu em mim, para que eles sejam perfeitos em unidade, e para que o mundo "
    "conheça que tu me enviaste a mim e que tens amado a eles como me tens amado a mim. "
    "Pai, aqueles que me deste quero que, onde eu estiver, também eles estejam comigo, "
    "para que vejam a minha glória que me deste, porque tu me amaste antes da fundação do mundo."
)


class LongVerseAlignTest(unittest.TestCase):
    def test_window_and_fallback_grow_with_length(self) -> None:
        short = "Eu sou a porta."
        self.assertGreater(_verse_search_window(LONG), 40)
        self.assertGreater(_verse_search_window(LONG), _verse_search_window(short))
        self.assertGreater(_verse_duration(LONG), 18)
        self.assertGreater(_verse_duration(LONG), _verse_duration(short))

    def test_align_finds_a_match_past_the_old_40s_window(self) -> None:
        transcript = "\n".join(
            [
                "[00:00:01] Vamos ler João 17, o versículo 23 e o 24.",
                "[00:00:50] Eu neles, e tu em mim, para que eles sejam perfeitos em unidade, e para que o mundo conheça que tu me enviaste.",
            ]
        )
        segments = parse_segments(transcript)
        start, end = _align(segments, 1.0, 1.0 + _verse_search_window(LONG), LONG)
        self.assertGreaterEqual(start, 49)
        self.assertGreater(end, start)
        # A janela antiga de 40s não acharia esse segmento e recuaria para a origem.
        missed_start, _missed_end = _align(segments, 1.0, 1.0 + 40, LONG)
        self.assertLess(missed_start, 10)
