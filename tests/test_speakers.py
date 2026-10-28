import unittest

from backend.app.services.faces import AUTO_THRESHOLD, REVIEW_THRESHOLD, cosine_similarity, decide
from backend.app.services.speakers import parse_speaker_filename
from pathlib import Path


class FilenameTest(unittest.TestCase):
    def test_name_is_the_stem(self) -> None:
        name, role = parse_speaker_filename(Path("João Silva.jpg"))
        self.assertEqual(name, "João Silva")
        self.assertEqual(role, "")

    def test_optional_role(self) -> None:
        name, role = parse_speaker_filename(Path("João_Silva | pastor.jpg"))
        self.assertEqual(name, "João Silva")
        self.assertEqual(role, "pastor")


class FaceScoreTest(unittest.TestCase):
    def test_cosine_identical(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)

    def test_auto_when_clear(self) -> None:
        result = decide([(3, AUTO_THRESHOLD + 0.1), (4, 0.2)])
        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["speaker_id"], 3)

    def test_review_when_close(self) -> None:
        result = decide([(3, 0.80), (4, 0.78)])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["speaker_id"], 3)

    def test_unknown_below_threshold(self) -> None:
        result = decide([(3, REVIEW_THRESHOLD - 0.05)])
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["speaker_id"])


if __name__ == "__main__":
    unittest.main()
