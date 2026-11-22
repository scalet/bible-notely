import json
import tempfile
import unittest
import sys
from datetime import date, datetime, time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from colab import youtube_lib as yt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.models import Lecture
from backend.app.models import Speaker
from backend.app.services import youtube as ytsvc
from backend.app.services.youtube import (
    apply_upload_entry,
    lecture_in_upload_scope,
    load_local_state,
    save_local_state,
    upload_row_for_lecture,
)


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
        self.assertTrue(text.startswith("IBQB"))
        self.assertNotIn("Igreja Batista", text)
        self.assertNotIn("Quadrangular", text)
        self.assertIn("Gerson", text)
        self.assertIn("João 3:16", text)
        self.assertIn("Deus amou o mundo", text)
        self.assertIn("Amor de Deus", text)
        # regressão: string tratada como lista saía uma letra por bullet
        self.assertNotIn("• A\n• m", text)
        self.assertIn("Conclusão\nAmor de Deus", text)

    def test_theme_is_cleaned_in_youtube_title(self) -> None:
        title = yt.build_youtube_title(
            "Perfeitos em Unidade — EBD e Culto do Dia dos Pais",
            date(2026, 8, 9),
            yt.PLAYLIST_CULT_MANHA,
        )
        self.assertEqual(title, "IBQB - Culto - 09/08/2026 - Perfeitos em Unidade")

    def test_skip_without_study_title(self) -> None:
        row = yt.merge_video_row(
            {"id": "abc", "name": "20260104_120000.mp4", "path": "2026/01-Janeiro/04-01", "size": 2_000_000_000},
            {},
            yt.empty_state(),
        )
        self.assertEqual(row["skip_reason"], "sem título do estudo")

    def test_mounted_listing_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            day = root / "2026" / "01-Janeiro" / "04-01"
            day.mkdir(parents=True)
            video = day / "20260104_090354.mp4"
            video.write_bytes(b"x" * 100)
            listed = yt.list_mounted_mp4s(root)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0]["path"], "2026/01-Janeiro/04-01")
            self.assertEqual(listed[0]["local_path"], str(video))
            self.assertEqual(len(yt.list_mounted_mp4s(root, "2025")), 0)
            self.assertEqual(len(yt.list_mounted_mp4s(root, "2026/01-Janeiro")), 1)

    def test_catalog_match_by_path(self) -> None:
        catalog = {
            "items": [
                {
                    "drive_file_id": "id-1",
                    "drive_path": "2026/01-Janeiro/04-01",
                    "video_filename": "20260104_090354.mp4",
                    "titulo": "Tema da aula",
                }
            ]
        }
        index = yt.catalog_index(catalog)
        row = yt.merge_video_row(
            {
                "id": "",
                "name": "20260104_090354.mp4",
                "path": "2026/01-Janeiro/04-01",
                "size": 2_000_000_000,
                "local_path": "",
            },
            index,
            yt.empty_state(),
        )
        self.assertEqual(row["drive_file_id"], "id-1")
        self.assertIn("Tema da aula", row["youtube_title"])

    def test_missing_mounted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "a.mp4"
            row = yt.merge_video_row(
                {
                    "id": "abc",
                    "name": "a.mp4",
                    "path": "2026/01-Janeiro/04-01",
                    "size": 2_000_000_000,
                    "local_path": str(missing),
                },
                {"abc": {"titulo": "Tema"}},
                yt.empty_state(),
            )
        self.assertIn("não encontrado", row["skip_reason"])

    def test_quota_error(self) -> None:
        class Fake:
            def __init__(self) -> None:
                self.resp = type("R", (), {"status": 403})()
                self.content = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'

            def __str__(self) -> str:
                return "HttpError 403 quotaExceeded"

        self.assertTrue(yt.is_youtube_quota_error(Fake()))
        self.assertFalse(yt.is_youtube_quota_error(RuntimeError("arquivo sumiu")))

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


class ScopeTest(unittest.TestCase):
    def test_scope_prefix(self) -> None:
        from unittest import mock

        lecture = Lecture(drive_path="2025/09-Setembro/21-09")
        with mock.patch("backend.app.services.youtube.get_youtube_upload_path", return_value="2025/09-Setembro"):
            self.assertTrue(lecture_in_upload_scope(lecture))
        with mock.patch("backend.app.services.youtube.get_youtube_upload_path", return_value="2026/01-Janeiro"):
            self.assertFalse(lecture_in_upload_scope(lecture))
        with mock.patch("backend.app.services.youtube.get_youtube_upload_path", return_value=""):
            self.assertTrue(lecture_in_upload_scope(lecture))


class UploadRowTest(unittest.TestCase):
    def test_row_from_lecture(self) -> None:
        lecture = Lecture(
            drive_file_id="x",
            drive_path="2026/01-Janeiro/04-01",
            video_filename="20260104_090354.mp4",
            lecture_date=date(2026, 1, 4),
        )
        speaker = Speaker(name="Gerson")
        summary = {"titulo": "Tema da aula"}
        row = upload_row_for_lecture(lecture, speaker, summary)
        self.assertIn("IBQB - EBD", row["youtube_title"])
        self.assertIn("Tema da aula", row["youtube_title"])
        self.assertEqual(row["skip_reason"], "")


class LocalStateTest(unittest.TestCase):
    def test_save_and_load_local_state(self) -> None:
        from backend.app.config import settings

        path = settings.youtube_state_path
        backup = path.read_text(encoding="utf-8") if path.exists() else None
        try:
            state = yt.empty_state()
            yt.record_upload(
                state,
                drive_file_id="f1",
                youtube_id="v1",
                title="t",
                drive_path="p",
                playlist=yt.PLAYLIST_EBD,
            )
            save_local_state(state)
            loaded = load_local_state()
            self.assertEqual(loaded["uploads"]["f1"]["youtube_id"], "v1")
        finally:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(backup, encoding="utf-8")


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


class MetaFolderAndCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)

    def test_meta_folder_id_from_settings_skips_drive(self) -> None:
        with (
            mock.patch.object(ytsvc.settings, "youtube_meta_folder_id", " pasta-fixa "),
            mock.patch.object(ytsvc, "_drive", side_effect=AssertionError("não deveria usar o Drive")),
        ):
            self.assertEqual(ytsvc.ensure_meta_folder(), "pasta-fixa")

    def test_meta_folder_without_root_raises(self) -> None:
        fake = mock.Mock()
        fake.get_folder_id.return_value = ""
        with (
            mock.patch.object(ytsvc.settings, "youtube_meta_folder_id", ""),
            mock.patch.object(ytsvc, "_drive", return_value=fake),
            self.assertRaises(ytsvc.YouTubeError),
        ):
            ytsvc.ensure_meta_folder()

    def _export(self, existing_state: dict | None):
        uploads: list[tuple[str, str, dict]] = []
        with (
            mock.patch.object(ytsvc, "ensure_meta_folder", return_value="pasta"),
            mock.patch.object(ytsvc, "_download_json", return_value=existing_state),
            mock.patch.object(
                ytsvc, "_upload_json", side_effect=lambda f, n, p: uploads.append((f, n, p)) or "fid"
            ),
        ):
            result = ytsvc.export_catalog(self.db)
        return result, uploads

    def test_export_creates_empty_state_when_missing(self) -> None:
        (count, folder), uploads = self._export(None)
        self.assertEqual((count, folder), (0, "pasta"))
        self.assertEqual([u[1] for u in uploads], [ytsvc.CATALOG_NAME, ytsvc.STATE_NAME])
        self.assertEqual(uploads[1][2], yt.empty_state())

    def test_export_keeps_existing_state(self) -> None:
        existing = yt.empty_state()
        _, uploads = self._export(existing)
        self.assertEqual([u[1] for u in uploads], [ytsvc.CATALOG_NAME])

    def test_upload_json_uses_real_filename_and_cleans_up(self) -> None:
        seen = {}
        fake = mock.Mock()

        def upload_file(path, folder_id, mime):
            seen["name"] = path.name
            seen["body"] = json.loads(path.read_text(encoding="utf-8"))
            seen["path"] = path
            return "fid"

        fake.upload_file.side_effect = upload_file
        with mock.patch.object(ytsvc, "_drive", return_value=fake):
            self.assertEqual(ytsvc._upload_json("pasta", "state.json", {"a": "ção"}), "fid")
        self.assertEqual(seen["name"], "state.json")
        self.assertEqual(seen["body"], {"a": "ção"})
        self.assertFalse(seen["path"].exists())
