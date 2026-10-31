import json
import unittest
from datetime import date, datetime, time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from colab import youtube_lib as yt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import Lecture
from backend.app.services.youtube import apply_upload_entry


class ClassifyTest(unittest.TestCase):
    def test_before_ten_is_ebd(self) -> None:
        self.assertEqual(yt.classify_playlist(time(9, 30)), yt.PLAYLIST_EBD)
        self.assertEqual(yt.title_type_for_playlist(yt.PLAYLIST_EBD), "EBD")

    def test_morning_cult(self) -> None:
        self.assertEqual(yt.classify_playlist(time(10, 15)), yt.PLAYLIST_CULT_MANHA)

    def test_night_cult(self) -> None:
        self.assertEqual(yt.classify_playlist(time(18, 0)), yt.PLAYLIST_CULT_NOITE)
        self.assertEqual(yt.title_type_for_playlist(yt.PLAYLIST_CULT_NOITE), "Culto")

    def test_time_from_filename(self) -> None:
        self.assertEqual(yt.time_from_filename("20260104_090354.mp4"), time(9, 3, 54))

    def test_youtube_title_format(self) -> None:
        title = yt.build_youtube_title("A fé que opera", date(2026, 1, 4), yt.PLAYLIST_EBD)
        self.assertEqual(title, "IBQB - EBD - 04/01/2026 - A fé que opera")

    def test_title_truncation(self) -> None:
        long_theme = "x" * 120
        title = yt.build_youtube_title(long_theme, date(2026, 1, 4), yt.PLAYLIST_CULT_NOITE)
        self.assertLessEqual(len(title), yt.YOUTUBE_TITLE_MAX)
        self.assertTrue(title.endswith("…"))

    def test_description_from_summary(self) -> None:
        summary = {
            "texto_base": "João 3:16",
            "pontos": ["Deus amou o mundo"],
            "aplicacoes": ["Crer"],
            "conclusao": "Amor de Deus",
        }
        text = yt.render_description(
            playlist=yt.PLAYLIST_EBD,
            event_date=date(2026, 1, 4),
            speaker_name="Gerson",
            summary=summary,
        )
        self.assertIn("IBQB", text)
        self.assertIn("Gerson", text)
        self.assertIn("João 3:16", text)
        self.assertIn("Deus amou o mundo", text)

    def test_skip_without_study_title(self) -> None:
        row = yt.merge_video_row(
            {"id": "abc", "name": "20260104_120000.mp4", "path": "2026/01-Janeiro/04-01", "size": 2_000_000_000},
            {},
            yt.empty_state(),
        )
        self.assertEqual(row["skip_reason"], "sem título do estudo")

    def test_state_record(self) -> None:
        state = yt.empty_state()
        yt.record_upload(
            state,
            drive_file_id="file1",
            youtube_id="vid123",
            title="IBQB - EBD - 04/01/2026 - Tema",
            drive_path="2026/01-Janeiro/04-01",
            playlist=yt.PLAYLIST_EBD,
            uploaded_at="2026-10-31T20:00:00Z",
        )
        self.assertTrue(yt.already_uploaded(state, "file1"))
        self.assertEqual(state["uploads"]["file1"]["youtube_url"], "https://www.youtube.com/watch?v=vid123")


class SyncSqliteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)()

    def tearDown(self) -> None:
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)

    def test_sync_updates_lecture(self) -> None:
        lecture = Lecture(
            drive_file_id="drive-99",
            drive_folder_id="folder-1",
            drive_path="2026/01-Janeiro/04-01",
            video_filename="20260104_090354.mp4",
            summarized=True,
            summary_text=json.dumps({"titulo": "Tema da aula"}),
        )
        self.db.add(lecture)
        self.db.commit()

        state = {
            "uploads": {
                "drive-99": {
                    "youtube_id": "abc123",
                    "youtube_url": "https://www.youtube.com/watch?v=abc123",
                    "title": "IBQB - EBD - 04/01/2026 - Tema da aula",
                    "drive_path": "2026/01-Janeiro/04-01",
                    "playlist": "ebd",
                    "uploaded_at": "2026-10-31T20:00:00Z",
                }
            }
        }

        apply_upload_entry(lecture, state["uploads"]["drive-99"])
        self.db.commit()
        row = self.db.query(Lecture).filter(Lecture.drive_file_id == "drive-99").one()
        self.assertEqual(row.youtube_id, "abc123")
        self.assertEqual(row.youtube_url, "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(row.youtube_title, "IBQB - EBD - 04/01/2026 - Tema da aula")
        self.assertIsInstance(row.youtube_uploaded_at, datetime)


if __name__ == "__main__":
    unittest.main()
