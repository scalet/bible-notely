import hashlib
import json
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import Lecture, Speaker
from backend.app.services import pipeline, publish
from backend.app.services.publish import (
    LESSON_DRAFT,
    ManaLessons,
    PublishError,
    build_lesson_payload,
    ffmpeg_720_command,
    find_processed_video,
    lesson_endpoint,
    publish_lecture,
    publish_pending,
    tus_signature,
)


SUMMARY = {
    "titulo": "A fé que opera",
    "texto_base": "João 3:16",
    "pontos": ["Deus amou o mundo"],
    "aplicacoes": ["Crer"],
    "conclusao": "Amor de Deus",
    "referencias": ["João 3:16", "Romanos 5:3-5"],
}


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 1
        return self.value


def _signature(video_id: str, now: int = 1_700_000_000) -> str:
    return tus_signature("99", "secret-key", now + 86400, video_id)


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = {
            "data_dir": settings.data_dir,
            "work_dir": settings.work_dir,
            "publish_enabled": settings.publish_enabled,
            "publish_transcode_720": settings.publish_transcode_720,
            "publish_chunk_bytes": settings.publish_chunk_bytes,
            "publish_encode_timeout_seconds": settings.publish_encode_timeout_seconds,
            "publish_encode_poll_seconds": settings.publish_encode_poll_seconds,
            "bunny_stream_library_id": settings.bunny_stream_library_id,
            "bunny_stream_api_key": settings.bunny_stream_api_key,
            "mana_api_url": settings.mana_api_url,
            "mana_api_token": settings.mana_api_token,
            "mana_publish_enabled": settings.mana_publish_enabled,
        }
        settings.data_dir = self.root / "data"
        settings.work_dir = self.root / "work"
        settings.publish_enabled = False
        settings.publish_transcode_720 = False
        settings.publish_chunk_bytes = 4
        settings.publish_encode_timeout_seconds = 3600
        settings.publish_encode_poll_seconds = 5
        settings.bunny_stream_library_id = "99"
        settings.bunny_stream_api_key = "secret-key"
        settings.mana_api_url = "https://mana.example"
        settings.mana_api_token = "token-1"
        settings.mana_publish_enabled = True
        settings.publish_dir.mkdir(parents=True, exist_ok=True)
        settings.publish_work_dir.mkdir(parents=True, exist_ok=True)
        pipeline._duplicate_names = set()
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.speaker = Speaker(name="Gerson")
        self.db.add(self.speaker)
        self.db.commit()
        self.calls: list[tuple[str, str]] = []
        self.uploaded = bytearray()
        self.statuses = [2, 4]
        self.lesson_status = "draft"
        self.lesson_created = True
        self.expected_title = "A fé que opera"
        self.guids = ["guid-1", "guid-2"]

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            setattr(settings, key, value)
        self.db.close()
        self.tmp.cleanup()

    def _lecture(self, filename: str = "20260104_090354.mp4", drive_id: str = "drive-1") -> Lecture:
        lecture = Lecture(
            drive_file_id=drive_id,
            drive_path="2026/01-Janeiro/04-01",
            video_filename=filename,
            lecture_date=date(2026, 1, 4),
            speaker_id=self.speaker.id,
            summary_text=json.dumps(SUMMARY, ensure_ascii=False),
            summarized=True,
            uploaded=True,
            status="done",
        )
        self.db.add(lecture)
        self.db.commit()
        return lecture

    def _video(self, lecture: Lecture, name: str | None = None, payload: bytes = b"abcdefghij") -> Path:
        folder = pipeline.lecture_work_dir(lecture)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name or lecture.video_filename)
        path.write_bytes(payload)
        return path

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url)))
        self.assertNotEqual(request.method, "DELETE")
        if request.method == "POST" and request.url.path.endswith("/videos"):
            self.assertEqual(request.headers["AccessKey"], "secret-key")
            guid = self.guids.pop(0)
            return httpx.Response(200, json={"guid": guid, "title": "A fé que opera"})
        if request.method == "POST" and request.url.path == "/tusupload":
            video_id = request.headers["VideoId"]
            self.assertEqual(request.headers["AuthorizationSignature"], _signature(video_id))
            self.assertEqual(request.headers["LibraryId"], "99")
            self.assertEqual(request.headers["Tus-Resumable"], "1.0.0")
            self.assertIn("filetype", request.headers["Upload-Metadata"])
            if settings.publish_transcode_720:
                self.assertEqual(request.headers["EnabledResolutions"], "720p")
                self.assertEqual(request.headers["EnabledOutputCodecs"], "x264")
            else:
                self.assertNotIn("EnabledResolutions", request.headers)
            return httpx.Response(201, headers={"Location": f"https://video.bunnycdn.com/tusupload/{video_id}"})
        if request.method == "PATCH":
            offset = int(request.headers["Upload-Offset"])
            self.uploaded.extend(request.content)
            new_offset = offset + len(request.content)
            video_id = request.headers["VideoId"]
            self.assertEqual(request.headers["AuthorizationSignature"], _signature(video_id))
            self.assertEqual(request.headers["Content-Type"], "application/offset+octet-stream")
            return httpx.Response(204, headers={"Upload-Offset": str(new_offset)})
        if request.method == "GET" and "/videos/" in request.url.path:
            status = self.statuses.pop(0) if self.statuses else 4
            return httpx.Response(200, json={"guid": request.url.path.rsplit("/", 1)[-1], "status": status, "encodeProgress": 100 if status == 4 else 10})
        if request.method == "PUT" and request.url.path == "/api/admin/pipeline/lessons":
            self.assertEqual(request.headers["Authorization"], "Bearer token-1")
            body = json.loads(request.content.decode())
            self.assertEqual(body["status"], LESSON_DRAFT)
            self.assertNotEqual(body["status"], "published")
            self.assertEqual(body["external_id"], "drive-1")
            self.assertEqual(body["title"], self.expected_title)
            self.assertEqual(body["date"], "2026-01-04")
            self.assertIn(body["type"], {"ebd", "culto"})
            self.assertEqual(body["speaker"], "Gerson")
            self.assertIn("João 3:16", body["verses"])
            self.assertIn("Romanos 5:3-5", body["verses"])
            self.assertIn("Deus amou o mundo", body["body"])
            self.assertTrue(body["bunny_video_id"])
            created = self.lesson_created
            self.lesson_created = False
            return httpx.Response(
                201 if created else 200,
                json={
                    "lesson_id": "aula_1",
                    "external_id": body["external_id"],
                    "status": self.lesson_status,
                    "bunny_video_id": body["bunny_video_id"],
                    "created": created,
                },
            )
        return httpx.Response(500, text=f"inesperado {request.method} {request.url}")

    def _run(self, lecture: Lecture, **kwargs):
        clock = kwargs.pop("clock", Clock())
        sleep = kwargs.pop("sleep", lambda _seconds: None)
        transport = httpx.MockTransport(self._handler)
        http = httpx.Client(transport=transport)
        try:
            return publish_lecture(self.db, lecture, http=http, sleep=sleep, clock=clock, now=lambda: 1_700_000_000, **kwargs)
        finally:
            http.close()

    def test_signature_matches_bunny_formula(self) -> None:
        digest = hashlib.sha256(b"99secret-key1700086400guid-1").hexdigest()
        self.assertEqual(tus_signature("99", "secret-key", 1_700_086_400, "guid-1"), digest)

    def test_lesson_url_and_draft_guard(self) -> None:
        self.assertEqual(lesson_endpoint("https://mana.example"), "https://mana.example/api/admin/pipeline/lessons")
        self.assertEqual(lesson_endpoint("https://mana.example/api/"), "https://mana.example/api/admin/pipeline/lessons")
        lecture = self._lecture()
        payload = build_lesson_payload(lecture, self.speaker, SUMMARY, bunny_video_id="guid-1")
        self.assertEqual(payload["status"], "draft")
        self.assertEqual(payload["type"], "ebd")
        payload["status"] = "published"
        with self.assertRaises(PublishError):
            ManaLessons(httpx.Client(transport=httpx.MockTransport(self._handler)), "https://mana.example", "token-1").upsert_draft(payload)
        self.assertEqual(self.calls, [])

    def test_culto_after_noon(self) -> None:
        lecture = self._lecture("20260104_190000.mp4")
        payload = build_lesson_payload(lecture, self.speaker, SUMMARY, bunny_video_id="guid-1")
        self.assertEqual(payload["type"], "culto")

    def test_prefers_overlay_and_keeps_the_file(self) -> None:
        lecture = self._lecture()
        original = self._video(lecture, payload=b"original!")
        overlay = self._video(lecture, f"{Path(lecture.video_filename).stem}-hinos.mp4", b"overlay!!")
        self.assertEqual(find_processed_video(lecture), overlay)
        result = self._run(lecture)
        self.assertEqual(result.bunny_video_id, "guid-1")
        self.assertEqual(result.lesson_id, "aula_1")
        self.assertTrue(original.exists())
        self.assertTrue(overlay.exists())
        self.assertEqual(bytes(self.uploaded), b"overlay!!")
        self.assertGreater(len([item for item in self.calls if item[0] == "PATCH"]), 1)
        puts = [item for item in self.calls if item[0] == "PUT"]
        self.assertEqual(len(puts), 1)

    def test_same_file_is_not_uploaded_again(self) -> None:
        lecture = self._lecture()
        path = self._video(lecture)
        first = self._run(lecture)
        self.assertEqual(first.action, "uploaded")
        self.calls.clear()
        self.statuses = [4]
        second = self._run(lecture)
        self.assertEqual(second.action, "skipped")
        self.assertEqual(self.calls, [])
        self.assertEqual(lecture.bunny_video_id, "guid-1")
        self.assertEqual(lecture.mana_lesson_id, "aula_1")
        self.assertEqual(lecture.publish_status, "synced")
        sent, lessons, skipped, failed = publish_pending(self.db, max_items=5)
        self.assertEqual((sent, lessons, skipped, failed), (0, 0, 0, 0))
        path.unlink()
        self.calls.clear()
        third = self._run(lecture)
        self.assertEqual(third.action, "skipped")
        self.assertEqual(self.calls, [])

    def test_summary_change_updates_draft_without_new_video(self) -> None:
        lecture = self._lecture()
        self._video(lecture)
        self._run(lecture)
        self.calls.clear()
        self.guids = ["guid-should-not-be-used"]
        data = json.loads(lecture.summary_text)
        data["titulo"] = "Outro título"
        lecture.summary_text = json.dumps(data, ensure_ascii=False)
        self.db.commit()
        self.expected_title = "Outro título"
        result = self._run(lecture)
        self.assertEqual(result.action, "synced")
        self.assertEqual(result.bunny_video_id, "guid-1")
        self.assertNotIn(("POST", "https://video.bunnycdn.com/library/99/videos"), self.calls)
        self.assertTrue(any(method == "PUT" for method, _url in self.calls))

    def test_changed_bytes_create_a_new_video_and_keep_the_old_one(self) -> None:
        lecture = self._lecture()
        path = self._video(lecture, payload=b"abcdefghij")
        self._run(lecture)
        path.write_bytes(b"ZZZZZZZZZZ")
        self.statuses = [4]
        result = self._run(lecture)
        self.assertEqual(result.bunny_video_id, "guid-2")
        state = json.loads(settings.publish_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["uploads"]["drive-1"]["previous_bunny_video_id"], "guid-1")

    def test_incomplete_upload_reuses_the_same_video(self) -> None:
        lecture = self._lecture()
        path = self._video(lecture)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        settings.publish_state_path.write_text(
            json.dumps(
                {
                    "uploads": {
                        "drive-1": {
                            "bunny_video_id": "guid-resume",
                            "sha256": digest,
                            "upload_complete": False,
                            "tus_url": "",
                            "encode_status": 0,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = self._run(lecture)
        self.assertEqual(result.bunny_video_id, "guid-resume")
        self.assertFalse(any(method == "POST" and url.endswith("/videos") for method, url in self.calls))
        self.assertTrue(any(method == "PATCH" for method, url in self.calls))

    def test_encode_failure_does_not_create_lesson_or_second_video(self) -> None:
        lecture = self._lecture()
        self._video(lecture)
        self.statuses = [5]
        with self.assertRaises(PublishError) as caught:
            self._run(lecture)
        self.assertIn("codificação", str(caught.exception))
        self.assertFalse(any(method == "PUT" for method, _url in self.calls))
        self.assertEqual(lecture.bunny_video_id, "guid-1")
        self.calls.clear()
        self.statuses = [5]
        with self.assertRaises(PublishError):
            self._run(lecture)
        posts = [url for method, url in self.calls if method == "POST" and url.endswith("/videos")]
        self.assertEqual(posts, [])

    def test_mana_flag_off_skips_lesson_call(self) -> None:
        settings.mana_publish_enabled = False
        lecture = self._lecture()
        self._video(lecture)
        result = self._run(lecture)
        self.assertEqual(result.action, "uploaded")
        self.assertIn("MANA_PUBLISH_ENABLED", result.detail)
        self.assertFalse(any(method == "PUT" for method, _url in self.calls))
        self.assertEqual(lecture.publish_status, "bunny_ready")
        self.assertEqual(lecture.mana_lesson_id, "")

    def test_auto_publish_is_refused(self) -> None:
        lecture = self._lecture()
        self._video(lecture)
        self.lesson_status = "published"
        with self.assertRaises(PublishError) as caught:
            self._run(lecture)
        self.assertIn("rascunho", str(caught.exception))
        self.assertEqual(lecture.publish_status, "failed")
        self.assertEqual(lecture.mana_lesson_id, "")
        self.calls.clear()
        self.statuses = [4]
        self.lesson_created = False
        self.lesson_status = "published"
        with self.assertRaises(PublishError):
            self._run(lecture)
        self.assertFalse(any(url.endswith("/videos") and method == "POST" for method, url in self.calls))

    def test_missing_video_does_not_call_bunny(self) -> None:
        lecture = self._lecture()
        with self.assertRaises(PublishError):
            self._run(lecture)
        self.assertEqual(self.calls, [])

    def test_transcode_720_uploads_the_copy_and_keeps_the_source(self) -> None:
        settings.publish_transcode_720 = True
        lecture = self._lecture()
        source = self._video(lecture, payload=b"raw-video")
        commands: list[list[str]] = []

        def runner(command, capture_output, text):
            commands.append(command)
            dest = Path(command[-1])
            dest.write_bytes(b"small720")
            return mock.Mock(returncode=0, stderr="", stdout="")

        result = self._run(lecture, runner=runner)
        self.assertEqual(result.bunny_video_id, "guid-1")
        self.assertTrue(source.exists())
        self.assertEqual(commands[0][0], "ffmpeg")
        self.assertIn("libx264", commands[0])
        self.assertIn(r"scale=-2:min(720\,ih)", commands[0])
        copied = settings.publish_work_dir / "drive-1-720p.mp4"
        self.assertEqual(copied.read_bytes(), b"small720")
        self.calls.clear()
        self.statuses = [4]
        again = self._run(lecture, runner=runner)
        self.assertEqual(again.action, "skipped")
        self.assertEqual(len(commands), 1)
        self.assertEqual(self.calls, [])

    def test_ffmpeg_failure_does_not_upload(self) -> None:
        settings.publish_transcode_720 = True
        lecture = self._lecture()
        source = self._video(lecture)

        def runner(command, capture_output, text):
            return mock.Mock(returncode=1, stderr="boom", stdout="")

        with self.assertRaises(PublishError):
            self._run(lecture, runner=runner)
        self.assertEqual(self.calls, [])
        self.assertTrue(source.exists())
        self.assertFalse((settings.publish_work_dir / "drive-1-720p.mp4").exists())

    def test_ffmpeg_command_is_h264_720(self) -> None:
        command = ffmpeg_720_command(Path("in.mp4"), Path("out.mp4"))
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertIn("720", command[command.index("-vf") + 1])


class ReleaseLocalVideoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.saved = settings.publish_enabled
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.lecture = Lecture(
            drive_file_id="drive-9",
            video_filename="aula.mp4",
            summarized=True,
            uploaded=True,
            status="done",
        )
        self.db.add(self.lecture)
        self.db.commit()
        self.video = Path(self.tmp.name) / "aula.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self) -> None:
        settings.publish_enabled = self.saved
        self.db.close()
        self.tmp.cleanup()

    def test_disabled_removes_local_file_and_does_not_publish(self) -> None:
        settings.publish_enabled = False
        with mock.patch("backend.app.services.publish.publish_lecture") as publish_lecture_mock:
            kept = pipeline._release_local_video(self.db, self.lecture, self.video)
        self.assertFalse(kept)
        self.assertFalse(self.video.exists())
        publish_lecture_mock.assert_not_called()
        self.assertEqual(self.lecture.status, "done")

    def test_success_removes_local_file_after_publish(self) -> None:
        settings.publish_enabled = True
        with mock.patch("backend.app.services.publish.publish_lecture") as publish_lecture_mock:
            kept = pipeline._release_local_video(self.db, self.lecture, self.video)
        self.assertFalse(kept)
        self.assertFalse(self.video.exists())
        publish_lecture_mock.assert_called_once()

    def test_failure_keeps_local_file_and_the_drive_status(self) -> None:
        settings.publish_enabled = True
        with mock.patch("backend.app.services.publish.publish_lecture", side_effect=PublishError("rede")):
            kept = pipeline._release_local_video(self.db, self.lecture, self.video)
        self.assertTrue(kept)
        self.assertTrue(self.video.exists())
        self.assertEqual(self.video.read_bytes(), b"video")
        self.assertEqual(self.lecture.status, "done")
        self.assertEqual(self.lecture.publish_status, "failed")
        self.assertIn("rede", self.lecture.publish_error)


if __name__ == "__main__":
    unittest.main()
