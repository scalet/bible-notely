import asyncio
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import api
from backend.app.config import settings
from backend.app.db import Base, get_db
from backend.app.models import Lecture, Speaker
from backend.app.runtime import cancel_event, set_pipeline
from backend.app.services import drive, pipeline
from backend.app.services.compress import (
    CompressConfig,
    CompressError,
    build_ffmpeg_command,
    compress_backfill,
    compress_file,
    compress_lecture,
    preferred_upload_source,
    resolve_stored_path,
    validate_config,
)
from backend.app.services.pipeline import lecture_work_dir, process_one, release_local_original
from backend.app.services.transcribe import video_duration


def _codec(path: Path, selector: str) -> str:
    return subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ],
        text=True,
    ).strip()


def _synthetic(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-b:v",
            "8M",
            "-minrate",
            "8M",
            "-maxrate",
            "8M",
            "-bufsize",
            "8M",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(path),
        ],
        check=True,
    )


class CommandTest(unittest.TestCase):
    def test_h264_copies_audio_and_keeps_height(self) -> None:
        cmd = build_ffmpeg_command(
            Path("camera.mp4"),
            Path("camera.compressed.mp4"),
            CompressConfig(crf=23, preset="veryfast", codec="h264", audio="copy", max_height=0),
        )
        self.assertIn("libx264", cmd)
        self.assertIn("veryfast", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertNotIn("-b:a", cmd)
        self.assertNotIn("-vf", cmd)
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd[-1], "camera.compressed.mp4")

    def test_h265_scales_and_reencodes_audio(self) -> None:
        cmd = build_ffmpeg_command(
            Path("camera.mp4"),
            Path("out.mp4"),
            CompressConfig(codec="h265", max_height=720, audio="aac", audio_bitrate="96k", crf=21),
        )
        self.assertIn("libx265", cmd)
        self.assertIn("hvc1", cmd)
        self.assertIn("scale=-2:'min(720,ih)',setsar=1", cmd)
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "96k")

    def test_rejects_bad_settings(self) -> None:
        with self.assertRaises(CompressError):
            validate_config(CompressConfig(crf=80))
        with self.assertRaises(CompressError):
            validate_config(CompressConfig(preset="placebo"))
        with self.assertRaises(CompressError):
            validate_config(CompressConfig(audio="mp3"))
        self.assertEqual(validate_config(CompressConfig(codec="hevc")).codec, "h265")


class FailureKeepsOriginalTest(unittest.TestCase):
    def test_duration_mismatch_discards_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "a.mp4"
            source.write_bytes(b"camera-bytes")
            dest = folder / "a.compressed.mp4"

            def fake_run(src, partial, config, progress=None, cancel=None):
                partial.write_bytes(b"not-a-match")

            with (
                mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
                mock.patch(
                    "backend.app.services.compress.video_duration",
                    side_effect=lambda path: 10.0 if path == source else 4.0,
                ),
            ):
                with self.assertRaises(CompressError) as caught:
                    compress_file(source, dest, CompressConfig(duration_tolerance=1))
            self.assertIn("duração", str(caught.exception))
            self.assertFalse(dest.exists())
            self.assertFalse((folder / "a.compressed.part.mp4").exists())
            self.assertEqual(source.read_bytes(), b"camera-bytes")

    def test_reuse_requires_the_same_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "a.mp4"
            dest = folder / "a.compressed.mp4"
            source.write_bytes(b"camera")
            dest.write_bytes(b"already")
            cfg = CompressConfig(delete_original=False)
            profile = "v=h264;preset=veryfast;crf=23;h=0;a=copy"

            def fake_run(src, partial, config, progress=None, cancel=None):
                partial.write_bytes(b"new")

            with (
                mock.patch("backend.app.services.compress.video_duration", return_value=5.0),
                mock.patch("backend.app.services.compress.embedded_profile", return_value=profile),
                mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run) as encode,
            ):
                reused = compress_file(source, dest, cfg, reuse=True)
                encode.assert_not_called()
                self.assertEqual(reused.status, "skipped")
                self.assertEqual(dest.read_bytes(), b"already")
                forced = compress_file(source, dest, cfg)
            encode.assert_called_once()
            self.assertEqual(forced.status, "compressed")

    def test_reuse_rejects_a_different_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "a.mp4"
            dest = folder / "a.compressed.mp4"
            source.write_bytes(b"camera")
            dest.write_bytes(b"old-profile")

            def fake_run(src, partial, config, progress=None, cancel=None):
                partial.write_bytes(b"new")

            with (
                mock.patch("backend.app.services.compress.video_duration", return_value=5.0),
                mock.patch("backend.app.services.compress.embedded_profile", return_value="v=h264;preset=veryfast;crf=18;h=720;a=copy"),
                mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run) as encode,
            ):
                result = compress_file(source, dest, CompressConfig(delete_original=False), reuse=True)
            encode.assert_called_once()
            self.assertEqual(result.status, "compressed")
            self.assertEqual(dest.read_bytes(), b"new")


class LectureRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)

    def _lecture(self, name: str = "aula.mp4", size: int = 0) -> tuple[Lecture, Path]:
        lecture = Lecture(
            drive_file_id=name,
            drive_folder_id="pasta",
            video_filename=name,
            video_size=size,
            status="pending",
        )
        self.db.add(lecture)
        self.db.commit()
        path = Path(self.tmp.name) / name
        return lecture, path

    def test_records_sizes_and_skips_the_second_run(self) -> None:
        lecture, source = self._lecture()
        source.write_bytes(b"original-bytes")
        lecture.video_size = source.stat().st_size
        self.db.commit()
        calls = {"n": 0}

        def fake_run(src, partial, config, progress=None, cancel=None):
            calls["n"] += 1
            partial.write_bytes(b"compressed-bytes")

        cfg = CompressConfig(delete_original=False, upload_drive=False)
        with (
            mock.patch("backend.app.services.compress.video_duration", return_value=8.0),
            mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
            mock.patch("backend.app.services.compress._ensure_space"),
        ):
            first = compress_lecture(self.db, lecture, source=source, config=cfg)
            second = compress_lecture(self.db, lecture, source=source, config=cfg)
        self.assertEqual(first.status, "compressed")
        self.assertEqual(second.status, "skipped")
        self.assertEqual(calls["n"], 1)
        self.assertTrue(lecture.compressed)
        self.assertEqual(lecture.original_size, len(b"original-bytes"))
        self.assertEqual(lecture.compressed_size, len(b"compressed-bytes"))
        self.assertTrue(lecture.compressed_path.endswith("aula.compressed.mp4"))
        self.assertTrue(source.exists())
        self.assertTrue(preferred_upload_source(lecture) is not None)

    def test_profile_change_reencodes(self) -> None:
        lecture, source = self._lecture()
        source.write_bytes(b"original-bytes")
        lecture.video_size = source.stat().st_size
        self.db.commit()
        calls = {"n": 0}

        def fake_run(src, partial, config, progress=None, cancel=None):
            calls["n"] += 1
            partial.write_bytes(f"out-{calls['n']}".encode())

        base = CompressConfig(crf=23, delete_original=False, upload_drive=False)
        tighter = CompressConfig(crf=18, delete_original=False, upload_drive=False)
        with (
            mock.patch("backend.app.services.compress.video_duration", return_value=8.0),
            mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
            mock.patch("backend.app.services.compress._ensure_space"),
        ):
            compress_lecture(self.db, lecture, source=source, config=base)
            again = compress_lecture(self.db, lecture, source=source, config=tighter)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(again.status, "compressed")
        self.assertIn("crf=18", lecture.compressed_profile)

    def test_compress_keeps_original_until_the_caller_finishes(self) -> None:
        lecture, source = self._lecture()
        source.write_bytes(b"original-bytes")
        lecture.video_size = source.stat().st_size
        self.db.commit()

        def fake_run(src, partial, config, progress=None, cancel=None):
            partial.write_bytes(b"compressed-bytes")

        cfg = CompressConfig(delete_original=True, upload_drive=False)
        with (
            mock.patch("backend.app.services.compress.video_duration", return_value=8.0),
            mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
            mock.patch("backend.app.services.compress._ensure_space"),
        ):
            compress_lecture(self.db, lecture, source=source, config=cfg)
        self.assertTrue(source.exists())
        stored = resolve_stored_path(lecture.compressed_path)
        self.assertTrue(stored.is_file())
        self.assertNotEqual(stored.resolve(), source.resolve())

    def test_path_is_relative_to_work_dir(self) -> None:
        with mock.patch.object(settings, "work_dir", Path(self.tmp.name)):
            lecture, source = self._lecture("clip.mp4")
            source.write_bytes(b"original-bytes")
            lecture.video_size = source.stat().st_size
            self.db.commit()

            def fake_run(src, partial, config, progress=None, cancel=None):
                partial.write_bytes(b"compressed-bytes")

            with (
                mock.patch("backend.app.services.compress.video_duration", return_value=8.0),
                mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
                mock.patch("backend.app.services.compress._ensure_space"),
            ):
                compress_lecture(
                    self.db,
                    lecture,
                    source=source,
                    config=CompressConfig(delete_original=False, upload_drive=False),
                )
        self.assertFalse(Path(lecture.compressed_path).is_absolute())
        self.assertTrue(lecture.compressed_path.endswith("clip.compressed.mp4"))
        with mock.patch.object(settings, "work_dir", Path(self.tmp.name)):
            found = preferred_upload_source(lecture)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertTrue(found.is_file())

    def test_incomplete_download_is_not_compressed(self) -> None:
        lecture, source = self._lecture(size=1000)
        source.write_bytes(b"short")
        with mock.patch("backend.app.services.compress._run_ffmpeg") as encode:
            with self.assertRaises(CompressError):
                compress_lecture(self.db, lecture, source=source, config=CompressConfig())
        encode.assert_not_called()
        self.assertFalse(lecture.compressed)
        self.assertEqual(source.read_bytes(), b"short")

    def test_upload_failure_keeps_the_compressed_record(self) -> None:
        lecture, source = self._lecture()
        source.write_bytes(b"original-bytes")
        lecture.video_size = source.stat().st_size
        self.db.commit()
        calls = {"n": 0, "uploads": 0}

        def fake_run(src, partial, config, progress=None, cancel=None):
            calls["n"] += 1
            partial.write_bytes(b"compressed-bytes")

        def fake_upload(path: Path, folder: str) -> str:
            calls["uploads"] += 1
            raise CompressError("rede")

        cfg = CompressConfig(upload_drive=True, delete_original=False)
        with (
            mock.patch("backend.app.services.compress.video_duration", return_value=8.0),
            mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
            mock.patch("backend.app.services.compress._ensure_space"),
        ):
            with self.assertRaises(CompressError):
                compress_lecture(self.db, lecture, source=source, config=cfg, upload=fake_upload)
            self.assertTrue(lecture.compressed)
            self.assertEqual(lecture.compressed_drive_file_id, "")
            with self.assertRaises(CompressError):
                compress_lecture(self.db, lecture, source=source, config=cfg, upload=fake_upload)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(calls["uploads"], 2)

    def test_missing_source_without_archive(self) -> None:
        lecture, source = self._lecture()
        result = compress_lecture(self.db, lecture, source=source, config=CompressConfig())
        self.assertEqual(result.status, "missing")
        self.assertFalse(lecture.compressed)


class BackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)

    def test_only_local_files_and_one_id_without_file(self) -> None:
        ready = Lecture(drive_file_id="ready", video_filename="ready.mp4", video_size=4, status="done")
        absent = Lecture(drive_file_id="absent", video_filename="absent.mp4", status="done")
        self.db.add_all([ready, absent])
        self.db.commit()
        ready_path = Path(self.tmp.name) / "ready.mp4"
        ready_path.write_bytes(b"1234")

        def locate(lecture: Lecture) -> Path:
            return Path(self.tmp.name) / lecture.video_filename

        def fake_run(src, partial, config, progress=None, cancel=None):
            partial.write_bytes(b"out!")

        cfg = CompressConfig()
        with (
            mock.patch("backend.app.services.compress.video_duration", return_value=3.0),
            mock.patch("backend.app.services.compress._run_ffmpeg", side_effect=fake_run),
            mock.patch("backend.app.services.compress._ensure_space"),
        ):
            done, skipped, missing, failed = compress_backfill(
                self.db, max_items=5, config=cfg, source_of=locate
            )
            self.assertEqual((done, skipped, missing, failed), (1, 0, 0, 0))
            one = compress_backfill(
                self.db, lecture_id=absent.id, max_items=1, config=cfg, source_of=locate
            )
        self.assertEqual(one, (0, 0, 1, 0))
        self.db.refresh(ready)
        self.assertTrue(ready.compressed)
        self.assertFalse(ready_path.exists())
        self.assertTrue(resolve_stored_path(ready.compressed_path).is_file())


class ReleaseOriginalTest(unittest.TestCase):
    def test_keeps_original_when_compressed_and_delete_is_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aula.mp4"
            path.write_bytes(b"camera")
            lecture = Lecture(compressed=True, video_filename="aula.mp4")
            with mock.patch(
                "backend.app.services.pipeline.load_compress_config",
                return_value=CompressConfig(delete_original=False),
            ):
                release_local_original(lecture, path)
            self.assertTrue(path.exists())
            with mock.patch(
                "backend.app.services.pipeline.load_compress_config",
                return_value=CompressConfig(delete_original=True),
            ):
                release_local_original(lecture, path)
            self.assertFalse(path.exists())

    def test_uncompressed_lecture_still_drops_the_working_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "aula.mp4"
            path.write_bytes(b"camera")
            lecture = Lecture(compressed=False, video_filename="aula.mp4")
            with mock.patch(
                "backend.app.services.pipeline.load_compress_config",
                return_value=CompressConfig(enabled=False, delete_original=False),
            ):
                release_local_original(lecture, path)
            self.assertFalse(path.exists())


class PipelineHookTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.addCleanup(self.db.close)
        pipeline._duplicate_names = set()
        patcher = mock.patch.object(settings, "work_dir", Path(self.work.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        set_pipeline(running=False, stage=None)
        cancel_event.clear()

    def _ready_lecture(self) -> tuple[Lecture, Path]:
        speaker = Speaker(name="Ana")
        self.db.add(speaker)
        self.db.commit()
        lecture = Lecture(
            drive_file_id="fid",
            drive_folder_id="folder",
            video_filename="aula.mp4",
            transcript_filename="aula.txt",
            summary_filename="aula-resumo.pdf",
            speaker_id=speaker.id,
            identification_status="reviewed",
            transcribed=True,
            summarized=True,
            summary_speaker_id=speaker.id,
            uploaded=False,
            status="pending",
            lecture_date=date(2026, 1, 4),
        )
        self.db.add(lecture)
        self.db.commit()
        work = lecture_work_dir(lecture)
        work.mkdir(parents=True)
        video = work / "aula.mp4"
        video.write_bytes(b"original-video")
        (work / "aula.txt").write_text("texto", encoding="utf-8")
        (work / "aula-resumo.pdf").write_bytes(b"%PDF")
        return lecture, video

    def test_compress_runs_before_upload_and_keeps_the_file(self) -> None:
        lecture, video = self._ready_lecture()
        order: list[str] = []

        def fake_compress(db, row, **kwargs):
            order.append("compress")
            self.assertTrue(kwargs["source"].is_file())
            row.compressed = True
            row.original_size = 15
            row.compressed_size = 4
            row.compressed_path = str(kwargs["source"].with_name("aula.compressed.mp4"))
            from backend.app.services.compress import CompressResult

            return CompressResult(status="compressed", output=Path(row.compressed_path), original_size=15, compressed_size=4)

        def fake_upload(*_args, **_kwargs):
            order.append("upload")
            return "drive-id"

        cfg = CompressConfig(enabled=True, delete_original=False)
        with (
            mock.patch("backend.app.services.pipeline.load_compress_config", return_value=cfg),
            mock.patch("backend.app.services.pipeline.compress_lecture", side_effect=fake_compress),
            mock.patch.object(drive, "upload_file", side_effect=fake_upload),
        ):
            asyncio.run(process_one(self.db, lecture))
        self.assertEqual(order[0], "compress")
        self.assertIn("upload", order)
        self.assertTrue(video.exists())
        self.db.refresh(lecture)
        self.assertEqual(lecture.status, "done")

    def test_disabled_compression_still_removes_the_working_copy(self) -> None:
        lecture, video = self._ready_lecture()
        called = {"n": 0}

        def fake_compress(*_args, **_kwargs):
            called["n"] += 1

        cfg = CompressConfig(enabled=False, delete_original=False)
        with (
            mock.patch("backend.app.services.pipeline.load_compress_config", return_value=cfg),
            mock.patch("backend.app.services.pipeline.compress_lecture", side_effect=fake_compress),
            mock.patch.object(drive, "upload_file", return_value="drive-id"),
        ):
            asyncio.run(process_one(self.db, lecture))
        self.assertEqual(called["n"], 0)
        self.assertFalse(video.exists())

    def test_deletes_original_only_after_upload(self) -> None:
        lecture, video = self._ready_lecture()

        def fake_compress(db, row, **kwargs):
            self.assertTrue(video.exists())
            row.compressed = True
            row.compressed_size = 4
            row.original_size = video.stat().st_size
            row.compressed_path = "aula/aula.compressed.mp4"
            from backend.app.services.compress import CompressResult

            return CompressResult(
                status="compressed",
                output=video.with_name("aula.compressed.mp4"),
                original_size=row.original_size,
                compressed_size=4,
            )

        def fake_upload(*_args, **_kwargs):
            self.assertTrue(video.exists())
            return "drive-id"

        cfg = CompressConfig(enabled=True, delete_original=True)
        with (
            mock.patch("backend.app.services.pipeline.load_compress_config", return_value=cfg),
            mock.patch("backend.app.services.pipeline.compress_lecture", side_effect=fake_compress),
            mock.patch.object(drive, "upload_file", side_effect=fake_upload),
        ):
            asyncio.run(process_one(self.db, lecture))
        self.assertFalse(video.exists())


class SettingsApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        runtime = Path(self.tmp.name) / "runtime.json"
        patcher = mock.patch("backend.app.runtime._path", runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.db.add(Speaker(name="Ana"))
        self.db.commit()
        app = FastAPI()
        app.include_router(api.router)

        def _get_db():
            yield self.db

        app.dependency_overrides[get_db] = _get_db
        self.client = TestClient(app)

    def test_rejects_crf_out_of_range(self) -> None:
        response = self.client.put("/api/settings", json={"compress_crf": 80})
        self.assertEqual(response.status_code, 400)

    def test_saves_video_settings(self) -> None:
        response = self.client.put(
            "/api/settings",
            json={
                "compress_enabled": False,
                "compress_crf": 21,
                "compress_max_height": 720,
                "compress_codec": "h265",
                "compress_audio": "aac",
                "compress_audio_bitrate": "96k",
                "compress_delete_original": False,
                "compress_upload_drive": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["compress_enabled"])
        self.assertEqual(body["compress_crf"], 21)
        self.assertEqual(body["compress_max_height"], 720)
        self.assertEqual(body["compress_codec"], "h265")
        self.assertEqual(body["compress_audio_bitrate"], "96k")
        self.assertTrue(body["compress_upload_drive"])


def _geometry(path: Path) -> dict[str, str]:
    text = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
            "-of",
            "default=nw=1",
            str(path),
        ],
        text=True,
    )
    data: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    return data


class LegacyDatabaseTest(unittest.TestCase):
    def test_opening_a_session_adds_compress_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            legacy = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(legacy)
            seed = sessionmaker(bind=legacy)()
            seed.add(Lecture(drive_file_id="old", video_filename="old.mp4", status="done"))
            seed.commit()
            seed.close()
            with legacy.begin() as conn:
                for column in (
                    "original_size",
                    "compressed",
                    "compressed_path",
                    "compressed_size",
                    "compressed_profile",
                    "compressed_drive_file_id",
                ):
                    conn.exec_driver_sql(f"ALTER TABLE lectures DROP COLUMN {column}")
            legacy.dispose()

            from backend.app import db as dbmod

            opened = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
            previous = (dbmod.engine, dbmod._Session, dbmod._schema_ready)
            dbmod.engine = opened
            dbmod._Session = sessionmaker(bind=opened, autoflush=False, autocommit=False)
            dbmod._schema_ready = False
            try:
                session = dbmod.SessionLocal()
                row = session.query(Lecture).filter_by(drive_file_id="old").one()
                self.assertFalse(row.compressed)
                self.assertEqual(row.compressed_path, "")
                self.assertEqual(row.compressed_size, 0)
                self.assertEqual(row.original_size, 0)
                session.close()
            finally:
                dbmod.engine, dbmod._Session, dbmod._schema_ready = previous
                opened.dispose()


class FfmpegIntegrationTest(unittest.TestCase):
    def test_synthetic_video_is_smaller_valid_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "aula.mp4"
            _synthetic(source)
            original_size = source.stat().st_size
            engine = create_engine("sqlite://")
            Base.metadata.create_all(engine)
            db = sessionmaker(bind=engine)()
            lecture = Lecture(
                drive_file_id="sintetico",
                video_filename="aula.mp4",
                video_size=original_size,
                status="pending",
            )
            db.add(lecture)
            db.commit()
            cfg = CompressConfig(
                enabled=True,
                crf=28,
                preset="ultrafast",
                max_height=0,
                codec="h264",
                audio="copy",
                delete_original=False,
                upload_drive=False,
                duration_tolerance=1,
            )
            first = compress_lecture(db, lecture, source=source, config=cfg)
            output = Path(lecture.compressed_path)
            self.assertEqual(first.status, "compressed")
            self.assertTrue(output.is_file())
            self.assertLess(output.stat().st_size, original_size)
            self.assertEqual(lecture.original_size, original_size)
            self.assertEqual(lecture.compressed_size, output.stat().st_size)
            self.assertEqual(lecture.video_size, original_size)
            self.assertTrue(source.is_file())
            self.assertEqual(source.stat().st_size, original_size)
            src_duration = video_duration(source)
            out_duration = video_duration(output)
            self.assertIsNotNone(src_duration)
            self.assertIsNotNone(out_duration)
            assert src_duration is not None and out_duration is not None
            self.assertLessEqual(abs(out_duration - src_duration), 1)
            self.assertEqual(_codec(output, "v:0"), "h264")
            self.assertEqual(_codec(output, "a:0"), "aac")
            stamp = output.stat().st_mtime_ns
            second = compress_lecture(db, lecture, source=source, config=cfg)
            self.assertEqual(second.status, "skipped")
            self.assertEqual(output.stat().st_mtime_ns, stamp)

            h265 = CompressConfig(
                enabled=True,
                crf=30,
                preset="ultrafast",
                codec="h265",
                audio="aac",
                audio_bitrate="96k",
                max_height=360,
                delete_original=False,
                upload_drive=False,
                duration_tolerance=1,
            )
            third = compress_lecture(db, lecture, source=source, config=h265)
            self.assertEqual(third.status, "compressed")
            self.assertEqual(_codec(output, "v:0"), "hevc")
            self.assertLess(output.stat().st_size, original_size)
            self.assertIn("v=h265", lecture.compressed_profile)
            self.assertTrue(source.is_file())
            db.close()

    def test_max_height_keeps_aspect_on_a_wide_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "wide.mp4"
            dest = Path(tmp) / "wide.compressed.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=480x220:rate=15",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100",
                    "-t",
                    "1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "18",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    str(source),
                ],
                check=True,
            )
            compress_file(
                source,
                dest,
                CompressConfig(
                    max_height=160,
                    crf=30,
                    preset="ultrafast",
                    audio="copy",
                    duration_tolerance=1,
                    delete_original=False,
                ),
            )
            geo = _geometry(dest)
            width = int(geo["width"])
            height = int(geo["height"])
            self.assertLess(height, 220)
            self.assertLessEqual(height, 160)
            self.assertLess(width, 480)
            self.assertAlmostEqual(width / height, 480 / 220, delta=0.05)
            self.assertEqual(geo.get("sample_aspect_ratio"), "1:1")
            self.assertTrue(source.is_file())


if __name__ == "__main__":
    unittest.main()
