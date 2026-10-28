import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import Lecture
from backend.app.runtime import cancel_event, pause_event
from backend.app.services import drive, pipeline

GIB = 1024**3


class EnsureFreeSpaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        patcher = mock.patch.object(settings, "work_dir", Path(self.work.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _free(self, free: int):
        return mock.patch.object(drive.shutil, "disk_usage", return_value=mock.Mock(free=free))

    def test_passes_when_size_plus_margin_fits(self) -> None:
        with self._free(GIB + drive.DISK_MARGIN_BYTES):
            drive.ensure_free_space(GIB)

    def test_raises_when_margin_does_not_fit(self) -> None:
        with self._free(GIB + drive.DISK_MARGIN_BYTES - 1):
            with self.assertRaises(drive.DriveError):
                drive.ensure_free_space(GIB)


class PrefetchVideoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / "aula" / "aula.mp4"
        state = mock.patch.object(pipeline, "set_pipeline")
        self.set_pipeline = state.start()
        self.addCleanup(state.stop)

    def _run(self, **patches) -> None:
        asyncio.run(pipeline._prefetch_video(1, "fid", self.path, 100, "aula.mp4"))

    def test_downloads_when_there_is_space(self) -> None:
        def fake_download(file_id, path, size, progress, cancel, expected_md5=""):
            path.write_bytes(b"x" * size)

        with (
            mock.patch.object(drive, "ensure_free_space"),
            mock.patch.object(drive, "download_file", side_effect=fake_download) as download,
        ):
            self._run()
        download.assert_called_once()
        self.assertTrue(self.path.exists())

    def test_skips_download_without_space(self) -> None:
        with (
            mock.patch.object(drive, "ensure_free_space", side_effect=drive.DriveError("sem espaço")),
            mock.patch.object(drive, "download_file") as download,
        ):
            self._run()
        download.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_reuses_video_already_downloaded(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"x" * 100)
        with (
            mock.patch.object(drive, "ensure_free_space") as space,
            mock.patch.object(drive, "download_file") as download,
        ):
            self._run()
        space.assert_not_called()
        download.assert_not_called()

    def test_failure_removes_partial_file_and_clears_status(self) -> None:
        def broken(file_id, path, size, progress, cancel, expected_md5=""):
            path.write_bytes(b"parcial")
            raise drive.DriveError("rede")

        with (
            mock.patch.object(drive, "ensure_free_space"),
            mock.patch.object(drive, "download_file", side_effect=broken),
        ):
            self._run()
        self.assertFalse(self.path.exists())
        self.set_pipeline.assert_called_with(prefetch_filename=None, prefetch_progress=None)


class RunBatchPrefetchTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        for target, value in (("work_dir", Path(self.work.name)),):
            patcher = mock.patch.object(settings, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("import_fotos", "sync_drive_videos", "recover_interrupted", "set_pipeline"):
            patcher = mock.patch.object(pipeline, name, return_value={"errors": []})
            patcher.start()
            self.addCleanup(patcher.stop)
        pause_event.clear()
        cancel_event.clear()
        self.addCleanup(pause_event.clear)
        self.addCleanup(cancel_event.clear)
        self.addCleanup(pipeline._prefetch.clear)
        self.prefetched: list[str] = []
        self.finished: list[str] = []
        self.cancelled_during_prefetch: list[bool] = []

    def _add(self, drive_id: str) -> Lecture:
        row = Lecture(
            drive_file_id=drive_id,
            video_filename=f"{drive_id}.mp4",
            status="pending",
            video_size=2 * GIB,
            lecture_date=date(2026, 1, 4),
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _run(self, max_items: int, transcribes: bool = True, final_status: str = "done") -> int:
        async def fake_prefetch(lecture_id, file_id, path, size, name, md5=""):
            await asyncio.sleep(0.05)
            self.cancelled_during_prefetch.append(cancel_event.is_set())
            self.finished.append(name)

        async def fake_process(db, lecture, on_transcribe=None):
            if transcribes and on_transcribe:
                on_transcribe()
            lecture.status = final_status
            db.commit()
            return lecture

        real_prefetch = pipeline._prefetch_video

        async def tracking_prefetch(lecture_id, file_id, path, size, name, md5=""):
            self.prefetched.append(name)
            await fake_prefetch(lecture_id, file_id, path, size, name, md5)

        with (
            mock.patch.object(pipeline, "process_one", side_effect=fake_process),
            mock.patch.object(pipeline, "_prefetch_video", side_effect=tracking_prefetch),
        ):
            result = asyncio.run(pipeline._run_locked(self.db, max_items))
        self.assertIs(pipeline._prefetch_video, real_prefetch)
        return result

    def test_prefetch_starts_even_for_single_item_batch(self) -> None:
        self._add("a")
        self._add("b")  # id maior: processada primeiro; "a" é a próxima
        self.assertEqual(self._run(max_items=1), 1)
        self.assertEqual(self.prefetched, ["a.mp4"])

    def test_prefetch_finishes_at_end_of_batch_instead_of_being_cancelled(self) -> None:
        self._add("a")
        self._add("b")
        self._run(max_items=1)
        self.assertEqual(self.finished, ["a.mp4"])
        self.assertEqual(self.cancelled_during_prefetch, [False])
        self.assertFalse(cancel_event.is_set())
        self.assertEqual(pipeline._prefetch, {})

    def test_no_prefetch_without_next_pending_lecture(self) -> None:
        self._add("a")
        self._run(max_items=5)
        self.assertEqual(self.prefetched, [])

    def test_no_prefetch_when_paused(self) -> None:
        self._add("a")
        self._add("b")
        pause_event.set()
        with mock.patch.object(pause_event, "clear"):
            self._run(max_items=1)
        self.assertEqual(self.prefetched, [])

    def test_no_prefetch_when_transcription_is_reused(self) -> None:
        self._add("a")
        self._add("b")
        self._run(max_items=1, transcribes=False)
        self.assertEqual(self.prefetched, [])

    def test_prefetched_lecture_is_the_one_processed_next(self) -> None:
        self._add("a")
        self._add("b")
        self.assertEqual(self._run(max_items=2), 2)
        self.assertEqual(self.prefetched, ["a.mp4"])

    def test_needs_review_still_prefetches_next(self) -> None:
        self._add("a")
        self._add("b")
        self._run(max_items=1, final_status="needs_review")
        self.assertIn("a.mp4", self.prefetched)


if __name__ == "__main__":
    unittest.main()
