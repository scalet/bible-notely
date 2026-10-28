import unittest
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import Lecture
from backend.app.services.drive import parse_lecture_date
from backend.app.services import pipeline
from backend.app.runtime import set_pipeline
from backend.app.services.pipeline import (
    recover_if_idle,
    recover_interrupted,
    lecture_work_dir,
    next_pending,
    work_dirname,
)


class ParseLectureDateTest(unittest.TestCase):
    def test_path_standard(self) -> None:
        parsed, warning = parse_lecture_date("2026/01-Janeiro/04-01")
        self.assertEqual(parsed, date(2026, 1, 4))
        self.assertEqual(warning, "")

    def test_month_mismatch_uses_day_month_and_warns(self) -> None:
        parsed, warning = parse_lecture_date("2024/04-Abril/10-03")
        self.assertEqual(parsed, date(2024, 3, 10))
        self.assertIn("mês divergente", warning)
        self.assertIn("2024-03-10", warning)

    def test_invalid_day_month_falls_back_to_folder_month(self) -> None:
        parsed, warning = parse_lecture_date("2024/01-Janeiro/31-02")
        self.assertEqual(parsed, date(2024, 1, 31))
        self.assertIn("Dia-NroMes inválido", warning)

    def test_month_name_mismatch_warns_but_keeps_date(self) -> None:
        parsed, warning = parse_lecture_date("2026/01-Fevereiro/04-01")
        self.assertEqual(parsed, date(2026, 1, 4))
        self.assertIn("nome do mês divergente", warning)

    def test_old_pattern_is_not_accepted(self) -> None:
        parsed, warning = parse_lecture_date("2024/03/10-03")
        self.assertIsNone(parsed)
        self.assertEqual(warning, "")

    def test_outside_pattern(self) -> None:
        parsed, warning = parse_lecture_date("avulsos/aula")
        self.assertIsNone(parsed)
        self.assertEqual(warning, "")


class WorkDirTest(unittest.TestCase):
    def test_directory_is_the_video_filename(self) -> None:
        lecture = Lecture(drive_file_id="abc123", video_filename="EBD-20250921.mp4")
        pipeline._duplicate_names = set()
        self.assertEqual(work_dirname(lecture.video_filename), "EBD-20250921")
        self.assertEqual(lecture_work_dir(lecture).name, "EBD-20250921")
        self.assertNotEqual(lecture_work_dir(lecture).name, lecture.drive_file_id)

    def test_repeated_filename_gets_the_date(self) -> None:
        lecture = Lecture(
            drive_file_id="abc123",
            video_filename="EBD.mp4",
            lecture_date=date(2026, 1, 4),
        )
        pipeline._duplicate_names = {"EBD.mp4"}
        self.assertEqual(lecture_work_dir(lecture).name, "EBD 2026-01-04")


class NextPendingTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def _add(self, drive_id: str, status: str, day: int) -> Lecture:
        row = Lecture(
            drive_file_id=drive_id,
            video_filename=f"{drive_id}.mp4",
            status=status,
            lecture_date=date(2024, 1, day),
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_pending_before_failed(self) -> None:
        failed = self._add("f", "failed", 1)
        pending = self._add("p", "pending", 2)
        chosen = next_pending(self.db)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual(chosen.id, pending.id)
        self.assertNotEqual(chosen.id, failed.id)

    def test_most_recent_pending_first(self) -> None:
        self._add("old", "pending", 1)
        newest = self._add("new", "pending", 20)
        self._add("mid", "pending", 10)
        chosen = next_pending(self.db)
        assert chosen is not None
        self.assertEqual(chosen.id, newest.id)

    def test_skip_ids_does_not_retry_same_failure(self) -> None:
        failed = self._add("f", "failed", 1)
        self.assertEqual(next_pending(self.db, skip_ids={failed.id}), None)

    def tearDown(self) -> None:
        self.db.close()


class RecoverInterruptedTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        set_pipeline(running=False)

    def tearDown(self) -> None:
        set_pipeline(running=False)
        self.db.close()

    def test_in_progress_returns_to_pending(self) -> None:
        row = Lecture(drive_file_id="stuck", video_filename="stuck.mp4", status="transcribing")
        self.db.add(row)
        self.db.commit()
        self.assertEqual(recover_interrupted(self.db), 1)
        self.db.refresh(row)
        self.assertEqual(row.status, "pending")

    def test_idle_recovery_skips_when_pipeline_is_running(self) -> None:
        row = Lecture(drive_file_id="live", video_filename="live.mp4", status="transcribing")
        self.db.add(row)
        self.db.commit()
        set_pipeline(running=True)
        self.assertEqual(recover_if_idle(self.db), 0)
        self.db.refresh(row)
        self.assertEqual(row.status, "transcribing")


if __name__ == "__main__":
    unittest.main()
