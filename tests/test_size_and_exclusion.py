import hashlib
import tempfile
import unittest
from datetime import date
from pathlib import Path
from threading import Event
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import api
from backend.app.config import settings
from backend.app.db import Base, get_db
from backend.app.models import Lecture
from backend.app.services import drive, pipeline
from backend.app.services.pipeline import TOO_SMALL_NOTE, next_pending, sync_drive_videos

GIB = 1024**3
FOLDER = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def execute(self, **_kwargs) -> dict:
        return self.payload


class _ChangeService:
    def __init__(self, changes: list[dict], new_token: str = "t2") -> None:
        self._changes = changes
        self._new_token = new_token

    def changes(self):
        return self

    def list(self, **_kwargs):
        return _Req({"changes": self._changes, "newStartPageToken": self._new_token})


class _FakeService:
    """Árvore: raiz → 2026 → 01-Janeiro → 04-01 → mp4s."""

    def __init__(self, files: list[dict]) -> None:
        self.tree = {
            "root": [{"id": "y", "name": "2026", "mimeType": FOLDER}],
            "y": [{"id": "m", "name": "01-Janeiro", "mimeType": FOLDER}],
            "m": [{"id": "d", "name": "04-01", "mimeType": FOLDER}],
            "d": files,
        }

    def files(self):
        return self

    def list(self, q: str, **_kwargs):
        parent = q.split("'")[1]
        return _Req({"files": self.tree.get(parent, [])})


def _mp4(file_id: str, size: int | str | None, md5: str = "") -> dict:
    item = {"id": file_id, "name": f"{file_id}.mp4", "mimeType": "video/mp4"}
    if size is not None:
        item["size"] = str(size)
    if md5:
        item["md5Checksum"] = md5
    return item


def _list(files: list[dict]) -> list[dict]:
    with (
        mock.patch.object(drive, "_service", return_value=_FakeService(files)),
        mock.patch.object(drive, "get_folder_id", return_value="root"),
        mock.patch.object(drive, "google_account", return_value="conta"),
    ):
        return drive.list_videos()


class ListVideosMinSizeTest(unittest.TestCase):
    def test_keeps_only_videos_at_or_above_minimum(self) -> None:
        videos = _list([_mp4("big", 2 * GIB), _mp4("exact", GIB), _mp4("small", GIB - 1)])
        self.assertEqual({v["id"] for v in videos}, {"big", "exact"})

    def test_video_without_size_is_ignored(self) -> None:
        self.assertEqual(_list([_mp4("nosize", None)]), [])

    def test_minimum_follows_settings(self) -> None:
        with mock.patch.object(settings, "min_video_bytes", 10):
            videos = _list([_mp4("a", 11), _mp4("b", 9)])
        self.assertEqual([v["id"] for v in videos], ["a"])

    def test_non_mp4_is_ignored(self) -> None:
        other = {"id": "t", "name": "x.txt", "mimeType": "text/plain", "size": str(5 * GIB)}
        self.assertEqual(_list([other]), [])

    def test_keeps_drive_md5(self) -> None:
        videos = _list([_mp4("big", 2 * GIB, md5="aabbcc")])
        self.assertEqual(videos[0]["md5"], "aabbcc")

    def test_outputs_are_seen_in_the_same_listing(self) -> None:
        files = [
            _mp4("aula", 2 * GIB),
            {"id": "txt", "name": "aula.txt", "mimeType": "text/plain"},
            {"id": "pdf", "name": "aula-resumo.pdf", "mimeType": "application/pdf"},
        ]
        videos = _list(files)
        self.assertEqual(videos[0]["has_outputs"], True)

    def test_unrelated_folder_is_not_opened(self) -> None:
        service = _FakeService([_mp4("aula", 2 * GIB)])
        service.tree["root"].append({"id": "junk", "name": "outras", "mimeType": FOLDER})
        service.tree["junk"] = [_mp4("escondido", 2 * GIB)]
        opened: list[str] = []
        original = service.list

        def wrapped(q: str, **kwargs):
            opened.append(q.split("'")[1])
            return original(q, **kwargs)

        service.list = wrapped
        with (
            mock.patch.object(drive, "_service", return_value=service),
            mock.patch.object(drive, "get_folder_id", return_value="root"),
            mock.patch.object(drive, "google_account", return_value="conta"),
        ):
            videos = drive.list_videos()
        self.assertEqual({item["id"] for item in videos}, {"aula"})
        self.assertNotIn("junk", opened)


class DriveChangeTest(unittest.TestCase):
    def test_change_outside_the_configured_tree_skips_listing(self) -> None:
        service = _ChangeService([{"fileId": "other", "file": {"parents": ["elsewhere"]}}])
        with (
            mock.patch.object(drive, "_service", return_value=service),
            mock.patch.object(drive, "get_folder_id", return_value="root"),
            mock.patch.object(
                drive,
                "get_drive_index",
                return_value={"token": "t1", "folder_id": "root", "folders": ["root", "day"]},
            ),
            mock.patch.object(drive, "set_drive_index") as saved,
        ):
            self.assertFalse(drive.should_list())
        self.assertEqual(saved.call_args.args[0]["token"], "t2")

    def test_change_inside_a_known_folder_requires_listing(self) -> None:
        service = _ChangeService([{"fileId": "vid", "file": {"parents": ["day"]}}])
        with (
            mock.patch.object(drive, "_service", return_value=service),
            mock.patch.object(drive, "get_folder_id", return_value="root"),
            mock.patch.object(
                drive,
                "get_drive_index",
                return_value={"token": "t1", "folder_id": "root", "folders": ["root", "day"]},
            ),
        ):
            self.assertTrue(drive.should_list())


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        self.work = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(settings, "work_dir", Path(self.work.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.work.cleanup)
        self.addCleanup(self.db.close)

    def _add(self, drive_id: str, status: str = "pending", size: int = 2 * GIB) -> Lecture:
        row = Lecture(
            drive_file_id=drive_id,
            video_filename=f"{drive_id}.mp4",
            status=status,
            video_size=size,
            lecture_date=date(2026, 1, 4),
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _video(self, lecture: Lecture) -> Path:
        _, path, _, _ = pipeline._work_paths(lecture)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        return path


class SyncMinSizeTest(_DbCase):
    def _sync(self, items: list[dict]) -> int:
        with (
            mock.patch.object(drive, "should_list", return_value=True),
            mock.patch.object(drive, "change_token", return_value="t"),
            mock.patch.object(drive, "commit_listing"),
            mock.patch.object(drive, "list_videos", return_value=items),
            mock.patch.object(drive, "folder_has_outputs", return_value=False),
            mock.patch.object(pipeline, "update_runtime"),
        ):
            return sync_drive_videos(self.db)

    def test_sync_skips_listing_when_drive_unchanged(self) -> None:
        with (
            mock.patch.object(drive, "should_list", return_value=False),
            mock.patch.object(drive, "list_videos") as listed,
            mock.patch.object(drive, "change_token") as token,
        ):
            self.assertEqual(sync_drive_videos(self.db), 0)
        listed.assert_not_called()
        token.assert_not_called()

    def test_small_lectures_already_in_db_become_excluded(self) -> None:
        for status in ("pending", "failed", "needs_review", "skipped"):
            self._add(f"s-{status}", status, size=GIB - 1)
        done = self._add("s-done", "done", size=GIB - 1)
        big = self._add("big", "pending", size=2 * GIB)
        small = self.db.query(Lecture).filter(Lecture.drive_file_id == "s-pending").one()
        video = self._video(small)

        self._sync([])

        self.db.expire_all()
        rows = {r.drive_file_id: r for r in self.db.query(Lecture).all()}
        for status in ("pending", "failed", "needs_review", "skipped"):
            self.assertEqual(rows[f"s-{status}"].status, "excluded")
            self.assertEqual(rows[f"s-{status}"].error, TOO_SMALL_NOTE)
        self.assertEqual(rows[done.drive_file_id].status, "done")
        self.assertEqual(rows[big.drive_file_id].status, "pending")
        self.assertFalse(video.exists())

    def test_excluded_is_not_requeued_by_sync(self) -> None:
        row = self._add("x", "excluded")
        item = {
            "id": "x", "name": "x.mp4", "folder_id": "d", "path": "2026/01-Janeiro/04-01",
            "size": 2 * GIB, "pattern_ok": True, "lecture_date": date(2026, 1, 4), "date_warning": "",
        }
        self._sync([item])
        self.db.refresh(row)
        self.assertEqual(row.status, "excluded")
        self.assertIsNone(next_pending(self.db))


class LectureEndpointsTest(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(api.router)

        def _get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = _get_db
        self.client = TestClient(app)
        patcher = mock.patch.object(
            api, "pipeline_snapshot", return_value={"current_lecture_id": None, "prefetch_filename": None}
        )
        self.snapshot = patcher.start()
        self.addCleanup(patcher.stop)

    def _status(self, lecture: Lecture) -> str:
        self.db.expire_all()
        return self.db.get(Lecture, lecture.id).status

    def test_exclude_removes_local_video_and_leaves_queue(self) -> None:
        lecture = self._add("a")
        video = self._video(lecture)
        res = self.client.post(f"/api/lectures/{lecture.id}/exclude")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "excluded")
        self.assertFalse(video.exists())
        self.assertIsNone(next_pending(self.db))

    def test_exclude_refuses_finished_or_running_status(self) -> None:
        for status in ("done", "transcribing", "excluded"):
            lecture = self._add(f"s-{status}", status)
            res = self.client.post(f"/api/lectures/{lecture.id}/exclude")
            self.assertEqual(res.status_code, 409, status)
            self.assertEqual(self._status(lecture), status)

    def test_exclude_refuses_current_and_prefetched_lecture(self) -> None:
        current = self._add("cur")
        ahead = self._add("ahead")
        self.snapshot.return_value = {"current_lecture_id": current.id, "prefetch_filename": ahead.video_filename}
        for lecture in (current, ahead):
            video = self._video(lecture)
            self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/exclude").status_code, 409)
            self.assertEqual(self._status(lecture), "pending")
            self.assertTrue(video.exists())

    def test_exclude_unknown_lecture_is_404(self) -> None:
        self.assertEqual(self.client.post("/api/lectures/999/exclude").status_code, 404)

    def test_restore_user_exclusion_returns_to_pending(self) -> None:
        lecture = self._add("r")
        self.client.post(f"/api/lectures/{lecture.id}/exclude")
        res = self.client.post(f"/api/lectures/{lecture.id}/restore")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "pending")
        self.assertEqual(res.json()["error"], "")
        chosen = next_pending(self.db)
        assert chosen is not None
        self.assertEqual(chosen.id, lecture.id)

    def test_restore_refuses_too_small_and_non_excluded(self) -> None:
        small = self._add("small", "excluded", size=GIB - 1)
        small.error = TOO_SMALL_NOTE
        self.db.commit()
        pending = self._add("p")
        self.assertEqual(self.client.post(f"/api/lectures/{small.id}/restore").status_code, 409)
        self.assertEqual(self.client.post(f"/api/lectures/{pending.id}/restore").status_code, 404)
        self.assertEqual(self._status(small), "excluded")

    def test_delete_download_removes_only_the_mp4(self) -> None:
        lecture = self._add("d", "done")
        video = self._video(lecture)
        transcript = video.parent / "d.txt"
        transcript.write_text("texto")
        res = self.client.delete(f"/api/lectures/{lecture.id}/download")
        self.assertEqual(res.json(), {"removed": True})
        self.assertFalse(video.exists())
        self.assertTrue(transcript.exists())
        self.assertEqual(self._status(lecture), "done")
        self.assertEqual(self.client.delete(f"/api/lectures/{lecture.id}/download").json(), {"removed": False})

    def test_delete_download_refuses_while_in_use(self) -> None:
        busy = self._add("busy", "transcribing")
        video = self._video(busy)
        self.assertEqual(self.client.delete(f"/api/lectures/{busy.id}/download").status_code, 409)
        self.assertTrue(video.exists())
        pending = self._add("cur")
        video = self._video(pending)
        self.snapshot.return_value = {"current_lecture_id": pending.id, "prefetch_filename": None}
        self.assertEqual(self.client.delete(f"/api/lectures/{pending.id}/download").status_code, 409)
        self.assertTrue(video.exists())

    def test_delete_download_unknown_lecture_is_404(self) -> None:
        self.assertEqual(self.client.delete("/api/lectures/999/download").status_code, 404)


class ResetIncompleteWorkTest(_DbCase):
    def test_removes_unfinished_mp4_and_keeps_done(self) -> None:
        pending = self._add("pend", "pending")
        pending.transcript_filename = "pend.txt"
        pending.summary_filename = "pend-resumo.pdf"
        pending_video = self._video(pending)
        pending_video.write_bytes(b"x" * 100)
        done = self._add("ok", "done")
        done.transcript_filename = "ok.txt"
        done.summary_filename = "ok-resumo.pdf"
        self.db.commit()
        done_video = self._video(done)
        _, _, txt, pdf = pipeline._work_paths(done)
        txt.write_text("t")
        pdf.write_bytes(b"%PDF")
        removed = pipeline.reset_incomplete_work(self.db)
        self.assertEqual(len(removed), 1)
        self.assertFalse(pending_video.exists())
        self.assertTrue(done_video.exists())
        self.assertTrue(txt.exists())
        self.assertTrue(pdf.exists())

    def test_in_progress_returns_to_pending(self) -> None:
        row = self._add("busy", "transcribing")
        video = self._video(row)
        pipeline.reset_incomplete_work(self.db)
        self.db.refresh(row)
        self.assertEqual(row.status, "pending")
        self.assertFalse(video.exists())


class DownloadMd5Test(unittest.TestCase):
    class _Service:
        def files(self):
            return self

        def get_media(self, fileId: str):
            return object()

    def _run(self, dest: Path, payload: bytes, expected_md5: str, expected_size: int = 0) -> None:
        class _Downloader:
            def __init__(self, fh, *_args, **_kwargs) -> None:
                self.fh = fh

            def next_chunk(self, num_retries=0):
                self.fh.write(payload)
                return None, True

        with (
            mock.patch.object(drive, "_service", return_value=self._Service()),
            mock.patch.object(drive, "google_account", return_value="conta"),
            mock.patch.object(drive, "MediaIoBaseDownload", _Downloader),
        ):
            drive.download_file("id", dest, expected_size=expected_size, expected_md5=expected_md5)

    def test_keeps_file_when_hash_matches(self) -> None:
        payload = b"aula-ebd"
        dest = Path(tempfile.mkdtemp()) / "aula.mp4"
        self._run(dest, payload, hashlib.md5(payload).hexdigest(), expected_size=len(payload))
        self.assertTrue(dest.exists())
        dest.unlink()
        dest.parent.rmdir()

    def test_deletes_file_when_hash_differs(self) -> None:
        dest = Path(tempfile.mkdtemp()) / "aula.mp4"
        with self.assertRaises(drive.DriveIncomplete):
            self._run(dest, b"errado", "0" * 32)
        self.assertFalse(dest.exists())
        dest.parent.rmdir()

    def test_resumes_after_dropped_connection(self) -> None:
        payload = b"aula-ebd"
        dest = Path(tempfile.mkdtemp()) / "aula.mp4"
        calls = {"n": 0}

        class _Downloader:
            def __init__(self, fh, *_args, **_kwargs) -> None:
                self.fh = fh
                self._progress = 0

            def next_chunk(self, num_retries=0):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise AttributeError("'NoneType' object has no attribute 'read'")
                self.fh.write(payload)
                self._progress = len(payload)
                return None, True

        with (
            mock.patch.object(drive, "_service", return_value=self._Service()),
            mock.patch.object(drive, "google_account", return_value="conta"),
            mock.patch.object(drive, "MediaIoBaseDownload", _Downloader),
            mock.patch.object(drive.time, "sleep"),
        ):
            drive.download_file(
                "id",
                dest,
                expected_size=len(payload),
                expected_md5=hashlib.md5(payload).hexdigest(),
            )
        self.assertEqual(calls["n"], 2)
        self.assertEqual(dest.read_bytes(), payload)
        dest.unlink()
        dest.parent.rmdir()

    def test_gives_up_when_connection_keeps_dropping(self) -> None:
        dest = Path(tempfile.mkdtemp()) / "aula.mp4"

        class _Downloader:
            def __init__(self, fh, *_args, **_kwargs) -> None:
                self.fh = fh

            def next_chunk(self, num_retries=0):
                raise AttributeError("'NoneType' object has no attribute 'read'")

        with (
            mock.patch.object(drive, "_service", return_value=self._Service()),
            mock.patch.object(drive, "google_account", return_value="conta"),
            mock.patch.object(drive, "MediaIoBaseDownload", _Downloader),
            mock.patch.object(drive.time, "sleep"),
            self.assertRaises(drive.DriveTransient),
        ):
            drive.download_file("id", dest, expected_size=8)
        dest.unlink(missing_ok=True)
        dest.parent.rmdir()


class _FakeResp:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "x"


def _http_error(status: int) -> HttpError:
    return HttpError(_FakeResp(status), b'{"error":{"message":"falhou"}}')


class DownloadResumeTest(unittest.TestCase):
    """Retomada por chunk: offset, limite de tentativas, backoff e erros definitivos."""

    class _Service:
        def files(self):
            return self

        def get_media(self, fileId: str):
            return object()

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "aula.mp4"
        self.sleep = mock.Mock()
        self.opened: list[int] = []

    def _download(self, script: list, *, cancel=None, **kwargs) -> None:
        """`script` é uma lista de ações por next_chunk: bytes (sucesso, escreve) ou exceção."""
        opened = self.opened
        steps = iter(script)

        class _Downloader:
            def __init__(self, fh, *_args, **_kwargs) -> None:
                self.fh = fh
                self._progress = 0
                opened.append(-1)

            def next_chunk(self, num_retries=0):
                step = next(steps)
                if isinstance(step, BaseException):
                    raise step
                self.fh.write(step)
                self._progress += len(step)
                return None, self._progress >= kwargs.get("expected_size", 10**9)

        real_open = drive._open_downloader

        def _open(file_id, fh, progress):
            downloader = real_open(file_id, fh, progress)
            opened[-1] = progress
            return downloader

        with (
            mock.patch.object(drive, "_service", return_value=self._Service()),
            mock.patch.object(drive, "google_account", return_value="conta"),
            mock.patch.object(drive, "MediaIoBaseDownload", _Downloader),
            mock.patch.object(drive, "_open_downloader", _open),
            mock.patch.object(drive.time, "sleep", self.sleep),
        ):
            drive.download_file("id", self.dest, cancel=cancel, **kwargs)

    def test_reopens_downloader_at_bytes_already_written(self) -> None:
        part1, part2 = b"aaaa", b"bbbb"
        self._download(
            [part1, ConnectionResetError("reset"), part2],
            expected_size=8,
            expected_md5=hashlib.md5(part1 + part2).hexdigest(),
        )
        self.assertEqual(self.opened, [0, 4])
        self.assertEqual(self.dest.read_bytes(), part1 + part2)

    def test_gives_up_after_chunk_tries_with_exponential_backoff(self) -> None:
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            return AttributeError("'NoneType' object has no attribute 'read'")

        script = [boom() for _ in range(drive._CHUNK_TRIES)]
        with self.assertRaises(drive.DriveTransient):
            self._download(script, expected_size=8)
        self.assertEqual(calls["n"], drive._CHUNK_TRIES)
        self.assertEqual(
            [c.args[0] for c in self.sleep.call_args_list],
            [1, 2, 4, 8, 16][: drive._CHUNK_TRIES - 1],
        )

    def test_success_resets_attempt_counter(self) -> None:
        chunk = b"x"
        # (limite-1) falhas, 1 sucesso, (limite-1) falhas, sucesso final: só passa se o contador zerar
        n = drive._CHUNK_TRIES - 1
        script = [TimeoutError("lento") for _ in range(n)] + [chunk]
        script += [TimeoutError("lento") for _ in range(n)] + [chunk]
        self._download(script, expected_size=2)
        self.assertEqual(self.dest.read_bytes(), chunk * 2)
        # o backoff recomeça em 1 depois do sucesso
        delays = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(delays[n], 1)

    def test_transient_http_errors_are_resumed(self) -> None:
        for status in (408, 429, 500, 503):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                self._download([_http_error(status), b"ok"], expected_size=2)
                self.assertEqual(self.sleep.call_count, 1)

    def test_definitive_http_errors_fail_without_retry(self) -> None:
        for status in (403, 404):
            with self.subTest(status=status):
                self.sleep.reset_mock()
                with self.assertRaises(drive.DriveError) as ctx:
                    self._download([_http_error(status), b"nunca"], expected_size=5)
                self.assertNotIsInstance(ctx.exception, drive.DriveTransient)
                self.sleep.assert_not_called()

    def test_permission_error_is_not_retried(self) -> None:
        with self.assertRaises(PermissionError):
            self._download([PermissionError("negado"), b"nunca"], expected_size=5)
        self.sleep.assert_not_called()

    def test_cancel_stops_before_next_chunk(self) -> None:
        cancel = Event()
        cancel.set()
        with self.assertRaises(drive.DriveCancelled):
            self._download([b"nunca"], cancel=cancel, expected_size=5)
        self.assertFalse(self.dest.exists())


class GoogleAccountCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = Path(tmp.name) / "token.json"
        self.token.write_text("{}")
        saved = dict(drive._account_cache)
        self.addCleanup(lambda: drive._account_cache.update(saved))
        drive._account_cache.update(mtime=0.0, label="", fail_until=0.0)
        patch = mock.patch.object(
            type(drive.settings), "google_token_path", new_callable=mock.PropertyMock, return_value=self.token
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _account(self, read_account: mock.Mock, *, creds: object | None = object()) -> str:
        with (
            mock.patch.object(drive, "credentials", return_value=creds),
            mock.patch.object(drive, "_service", return_value=object()),
            mock.patch.object(drive, "_read_account", read_account),
        ):
            return drive.google_account()

    def test_label_is_cached_per_token_mtime(self) -> None:
        read = mock.Mock(return_value="Ana <ana@x.com>")
        self.assertEqual(self._account(read), "Ana <ana@x.com>")
        self.assertEqual(self._account(read), "Ana <ana@x.com>")
        self.assertEqual(read.call_count, 1)

    def test_failure_is_remembered_for_ttl(self) -> None:
        read = mock.Mock(side_effect=RuntimeError("rede"))
        self.assertEqual(self._account(read), "")
        self.assertEqual(self._account(read), "")
        self.assertEqual(read.call_count, 1)

    def test_failure_keeps_previous_label_after_token_refresh(self) -> None:
        drive._account_cache.update(mtime=1.0, label="Ana <ana@x.com>", fail_until=0.0)
        read = mock.Mock(side_effect=RuntimeError("rede"))
        self.assertEqual(self._account(read), "Ana <ana@x.com>")

    def test_without_credentials_clears_cache(self) -> None:
        drive._account_cache.update(mtime=1.0, label="Ana", fail_until=0.0)
        self.assertEqual(self._account(mock.Mock(), creds=None), "")
        self.assertEqual(drive._account_cache["label"], "")


class ServiceTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        saved = dict(drive._service_cache)
        self.addCleanup(lambda: drive._service_cache.update(saved))
        drive._service_cache.update(mtime=0.0, service=None)
        self.creds = object()

    def _timeout_of(self, **kwargs) -> int:
        with (
            mock.patch.object(drive, "credentials", return_value=self.creds),
            mock.patch.object(drive, "build") as build,
            mock.patch.object(drive, "AuthorizedHttp") as authorized,
        ):
            drive._service(**kwargs)
        self.assertEqual(authorized.call_args.args[0], self.creds)
        return authorized.call_args.kwargs["http"].timeout

    def test_metadata_and_media_use_different_timeouts(self) -> None:
        self.assertEqual(self._timeout_of(), drive._METADATA_TIMEOUT)
        self.assertEqual(self._timeout_of(isolated=True), drive._MEDIA_TIMEOUT)

    def test_isolated_service_is_not_cached(self) -> None:
        with (
            mock.patch.object(drive, "credentials", return_value=self.creds),
            mock.patch.object(drive, "build", side_effect=lambda *a, **k: object()),
            mock.patch.object(drive, "AuthorizedHttp"),
        ):
            first = drive._service(isolated=True)
            second = drive._service(isolated=True)
        self.assertIsNot(first, second)
        self.assertIsNone(drive._service_cache["service"])


if __name__ == "__main__":
    unittest.main()
