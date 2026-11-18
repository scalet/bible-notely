import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
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
from backend.app.models import Lecture, LyricPending, Speaker
from backend.app.services.compress import (
    CompressConfig,
    CompressResult,
    build_ffmpeg_command,
    compose_video_filter,
    profile_of,
    profile_with_overlay,
)
from backend.app.services.compress import _profile_matches
from backend.app.services.hymnal import SongText, find_hymn, import_hymnal, parse_hymn, pick_drive_file, split_stanzas
from backend.app.services.overlays import (
    OverlayConfig,
    build_plan,
    clock_label,
    cue_token,
    recording_kind,
    render_ass,
    resolve_pending,
    run_lecture_overlay,
    wait_decision,
)
from backend.app.services.songs import detect_songs
from backend.app.services.verses import detect_verse_cues, parse_segments, parse_spoken_references


ARC = {
    ("João", 11): {
        25: "Disse-lhe Jesus: Eu sou a ressurreição e a vida; quem crê em mim, ainda que esteja morto, viverá;",
        26: "e todo aquele que vive e crê em mim nunca morrerá. Crês tu isso?",
    },
    ("Salmos", 90): {
        1: "Senhor, tu tens sido o nosso refúgio, de geração em geração.",
        5: "Tu os levas como corrente de água; são como um sono; são como a erva que cresce de madrugada;",
        6: "de madrugada, cresce e floresce; à tarde, corta-se e seca.",
    },
}


def _load_passage(book, chapter, verses):
    chapter_text = ARC.get((book, chapter))
    if not chapter_text:
        return None
    numbers = []
    for part in verses.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            numbers.extend(range(int(start), int(end) + 1))
        elif part.isdigit():
            numbers.append(int(part))
    items = [{"n": number, "text": chapter_text[number]} for number in numbers if number in chapter_text]
    if not items:
        return None
    return {"items": items}


def _fetch_chapter(book, chapter):
    return dict(ARC.get((book, chapter), {}))


HYMN = """1
Grandes coisas fez o Senhor por nós
e por isso estamos alegres

Coro
O Senhor fez grandes coisas
e estamos muito alegres
"""


def _hymn() -> SongText:
    parsed = parse_hymn("042 Grandes coisas.txt", HYMN)
    assert parsed is not None
    return parsed


class VerseParseTest(unittest.TestCase):
    def test_spoken_ranges_and_chapter_words(self) -> None:
        self.assertEqual(parse_spoken_references("João 11, 25 e 26"), [("João", 11, "25-26")])
        self.assertEqual(parse_spoken_references("Salmo 90 versículos 5 e 6"), [("Salmos", 90, "5-6")])
        self.assertEqual(parse_spoken_references("1 Coríntios 15:55"), [("1 Coríntios", 15, "55")])
        self.assertEqual(
            parse_spoken_references("Apocalipse capítulo 1 versículo 18"),
            [("Apocalipse", 1, "18")],
        )
        self.assertEqual(
            parse_spoken_references("João capítulo onze versículo vinte e cinco"),
            [("João", 11, "25")],
        )
        self.assertEqual(parse_spoken_references("lá em 2 Pedro 2, 7 e 8"), [("2 Pedro", 2, "7-8")])

    def test_cues_follow_the_reading_and_use_arc_text(self) -> None:
        transcript = "\n".join(
            [
                "[00:31:20] João 11, 25 e 26. E disse-lhe Jesus, eu sou a ressurreição e a vida.",
                "[00:31:31] e todo aquele que vive e crê em mim nunca morrerá. Crês tu isso?",
                "[00:33:50] lá no Salmo 90 a vida é como a erva que cresce de madrugada e à tarde corta-se e seca.",
            ]
        )
        cues = detect_verse_cues(transcript, load_passage=_load_passage, fetch_chapter=_fetch_chapter)
        titles = [cue.title for cue in cues]
        self.assertIn("João 11:25", titles)
        self.assertIn("João 11:26", titles)
        self.assertIn("Salmos 90:5", titles)
        self.assertIn("Salmos 90:6", titles)
        self.assertNotIn("Salmos 90:1", titles)
        john = next(cue for cue in cues if cue.title == "João 11:25")
        self.assertIn("ressurreição e a vida", john.body)
        self.assertGreaterEqual(john.start, 31 * 60 + 20)
        self.assertLess(john.start, 31 * 60 + 40)
        self.assertGreater(john.end, john.start)

    def test_segments_keep_timestamps(self) -> None:
        segments = parse_segments("[00:01:02] um\n[00:01:10] dois")
        self.assertEqual(segments[0].start, 62)
        self.assertEqual(segments[0].end, 70)


class HymnMatchTest(unittest.TestCase):
    def test_filename_and_stanzas(self) -> None:
        hymn = _hymn()
        self.assertEqual(hymn.number, "42")
        self.assertEqual(hymn.title, "Grandes coisas")
        self.assertGreaterEqual(len(hymn.stanzas), 2)
        self.assertTrue(any("Coro" not in stanza and "Grandes coisas fez" in stanza for stanza in hymn.stanzas))

    def test_fuzzy_match_splits_stanzas(self) -> None:
        transcript = "\n".join(
            [
                "[00:02:00] grandes coisas fez o senhor por nos e por isso estamos alegres",
                "[00:02:16] o senhor fez grandes coisas e estamos muito alegres",
            ]
        )
        hits, unknown = detect_songs(parse_segments(transcript), [_hymn()], [])
        self.assertFalse(unknown)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].number, "42")
        self.assertGreaterEqual(len(hits[0].stanzas), 2)

    def test_plain_speech_is_not_a_song(self) -> None:
        transcript = "\n".join(
            [
                "[00:00:00] Hoje vamos estudar a esperança da ressurreição e o que Jesus disse aos discípulos.",
                "[00:00:12] Esse é o tema da mensagem e eu quero mostrar três pontos da caminhada cristã.",
            ]
        )
        hits, unknown = detect_songs(parse_segments(transcript), [_hymn()], [])
        self.assertFalse(hits)
        self.assertFalse(unknown)

    def test_import_zip_and_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "Cantor_Cristão.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("001 Antífona.txt", "Grandes e maravilhosas são\nas tuas obras\n\nCoro\nAleluia amém\n")
                handle.writestr("042 Grandes coisas.txt", HYMN)
            count = import_hymnal(archive, root)
            self.assertEqual(count, 2)
            from backend.app.services.hymnal import load_hymns

            hymns = load_hymns(root)
            self.assertEqual(find_hymn("42", hymns).title, "Grandes coisas")
            self.assertEqual(find_hymn("hino 1", hymns).number, "1")
            self.assertEqual(find_hymn("Antífona", hymns).number, "1")
            self.assertIsNone(find_hymn("não existe", hymns))

    def test_drive_pick_prefers_igreja_folder(self) -> None:
        files = [
            {"id": "a", "name": "Cantor_Cristão.zip", "parents": ["outro"]},
            {"id": "b", "name": "Cantor_Cristão.zip", "parents": ["igreja"]},
        ]
        chosen = pick_drive_file(files, lambda folder: {"igreja": "Igreja", "outro": "Cultos"}[folder])
        self.assertEqual(chosen["id"], "b")

    def test_split_pasted_lyrics(self) -> None:
        parts = split_stanzas("linha um\nlinha dois\n\nlinha três\nlinha quatro")
        self.assertEqual(len(parts), 2)


class PendingTimeoutTest(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.addCleanup(self.db.close)
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.lecture = Lecture(
            drive_file_id="fid",
            video_filename="20261101_190000.mp4",
            transcript_filename="aula.txt",
            status="pending",
        )
        self.db.add(self.lecture)
        self.db.commit()
        self.video = self.root / "aula.mp4"
        self.video.write_bytes(b"")
        self.transcript = "\n".join(
            [
                "[00:10:00] aleluia aleluia aleluia gloria",
                "[00:10:12] aleluia aleluia gloria gloria",
                "[00:10:24] aleluia gloria aleluia",
            ]
        )

    def _plan(self, *, no_wait: bool, now: datetime, hours: float):
        with mock.patch(
            "backend.app.services.overlays.load_overlay_config",
            return_value=OverlayConfig(enabled=True, wait_hours=hours),
        ):
            return build_plan(
                self.db,
                self.lecture,
                self.transcript,
                self.video,
                no_wait=no_wait,
                now=now,
                root=self.root,
                load_passage_fn=_load_passage,
                fetch_chapter_fn=_fetch_chapter,
            )

    def test_unknown_song_waits_then_times_out(self) -> None:
        start = datetime(2026, 11, 1, 12, 0, 0)
        waiting = self._plan(no_wait=False, now=start, hours=24)
        self.assertTrue(waiting.waiting)
        self.assertEqual(waiting.cues, [])
        row = self.db.query(LyricPending).one()
        self.assertEqual(row.status, "open")
        self.assertGreater(row.end_seconds - row.start_seconds, 10)
        later = self._plan(no_wait=False, now=start + timedelta(hours=25), hours=24)
        self.assertFalse(later.waiting)
        self.db.refresh(row)
        self.assertEqual(row.status, "timeout")
        self.assertFalse(any(cue.kind == "lyric" for cue in later.cues))

    def test_no_wait_leaves_the_question_and_does_not_block(self) -> None:
        plan = self._plan(no_wait=True, now=datetime(2026, 11, 1, 12, 0, 0), hours=24)
        self.assertFalse(plan.waiting)
        row = self.db.query(LyricPending).one()
        self.assertEqual(row.status, "open")

    def test_zero_hours_does_not_wait(self) -> None:
        decision = wait_decision(1, 0, datetime.utcnow(), datetime.utcnow() + timedelta(hours=1), False)
        self.assertEqual(decision, "skip")
        plan = self._plan(no_wait=False, now=datetime(2026, 11, 1, 12, 0, 0), hours=0)
        self.assertFalse(plan.waiting)

    def test_answer_is_remembered_and_matched_next_time(self) -> None:
        start = datetime(2026, 11, 1, 12, 0, 0)
        self._plan(no_wait=True, now=start, hours=24)
        row = self.db.query(LyricPending).one()
        resolve_pending(
            self.db,
            row,
            "lyrics",
            lyrics="Aleluia, glória ao Cordeiro\n\nDigno é o Cordeiro de Deus",
            title="Cordeiro",
            root=self.root,
        )
        self.assertEqual(row.status, "resolved")
        again = self._plan(no_wait=True, now=start, hours=24)
        lyric = [cue for cue in again.cues if cue.kind == "lyric"]
        self.assertGreaterEqual(len(lyric), 2)
        self.assertTrue(any("Cordeiro" in cue.title or "Aleluia" in cue.body for cue in lyric))
        self.assertEqual(self.db.query(LyricPending).filter_by(status="open").count(), 0)

    def test_skip_is_not_asked_again(self) -> None:
        start = datetime(2026, 11, 1, 12, 0, 0)
        self._plan(no_wait=True, now=start, hours=24)
        row = self.db.query(LyricPending).one()
        resolve_pending(self.db, row, "skip", root=self.root)
        self._plan(no_wait=True, now=start, hours=24)
        self.assertEqual(self.db.query(LyricPending).filter_by(status="open").count(), 0)

    def test_hymn_number_resolves_from_the_index(self) -> None:
        import_hymnal_source = self.root / "hinos"
        import_hymnal_source.mkdir()
        (import_hymnal_source / "042 Grandes coisas.txt").write_text(HYMN, encoding="utf-8")
        import_hymnal(import_hymnal_source, self.root)
        start = datetime(2026, 11, 1, 12, 0, 0)
        self._plan(no_wait=True, now=start, hours=24)
        row = self.db.query(LyricPending).one()
        resolve_pending(self.db, row, "hymn", query="42", root=self.root)
        self.assertEqual(row.hymn_number, "42")
        self.assertIn("Grandes coisas", row.lyrics)

    def test_cli_timeout_does_not_loop_forever(self) -> None:
        work = self.root / "work"
        folder = work / "20261101_190000"
        folder.mkdir(parents=True)
        (folder / "20261101_190000.mp4").write_bytes(b"video")
        (folder / "aula.txt").write_text(self.transcript, encoding="utf-8")
        state = {"now": datetime(2026, 11, 1, 12, 0, 0)}

        def clock():
            return state["now"]

        def sleeper(_seconds: float) -> None:
            state["now"] += timedelta(hours=30)

        with (
            mock.patch("backend.app.services.pipeline.lecture_work_dir", return_value=folder),
            mock.patch(
                "backend.app.services.overlays.load_overlay_config",
                return_value=OverlayConfig(enabled=True, wait_hours=24),
            ),
            mock.patch(
                "backend.app.services.compress.load_compress_config",
                return_value=CompressConfig(enabled=True),
            ),
            mock.patch(
                "backend.app.services.compress.compress_lecture",
                return_value=CompressResult(status="compressed", profile="v=h264"),
            ) as compress,
        ):
            message = run_lecture_overlay(
                self.db,
                self.lecture,
                no_wait=False,
                clock=clock,
                sleeper=sleeper,
                root=self.root,
            )
        self.assertIn("compressed", message)
        self.assertEqual(compress.call_count, 1)
        self.assertEqual(self.db.query(LyricPending).filter_by(status="timeout").count(), 1)


class FilterTest(unittest.TestCase):
    def test_scale_and_ass_share_one_encode(self) -> None:
        ass = Path("/tmp/aula overlays.ass")
        config = CompressConfig(crf=21, preset="veryfast", max_height=720, codec="h264", audio="copy")
        cmd = build_ffmpeg_command(Path("camera.mp4"), Path("camera.compressed.mp4"), config, ass_path=ass, overlay_token="abc")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=-2:'min(720,ih)',setsar=1", vf)
        self.assertIn("ass=", vf)
        self.assertLess(vf.index("scale="), vf.index("ass="))
        self.assertEqual(cmd[cmd.index("-crf") + 1], "21")
        self.assertIn("veryfast", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertIn("+faststart", cmd)
        self.assertEqual(cmd.count("-vf"), 1)
        self.assertEqual(profile_with_overlay(config, ""), profile_of(config))
        self.assertTrue(profile_with_overlay(config, "abc").endswith(";ov=abc"))
        comment = cmd[cmd.index("-metadata") + 1]
        self.assertIn(";ov=abc", comment)

    def test_without_overlay_the_command_stays_a_plain_compress(self) -> None:
        config = CompressConfig(crf=23, preset="veryfast", max_height=0, audio="copy")
        cmd = build_ffmpeg_command(Path("camera.mp4"), Path("out.mp4"), config)
        self.assertNotIn("-vf", cmd)
        self.assertNotIn("ass=", " ".join(cmd))
        self.assertTrue(_profile_matches(profile_of(config), config, None))
        self.assertTrue(_profile_matches(profile_of(config) + ";ov=abc", config, None))
        self.assertFalse(_profile_matches(profile_of(config), config, "abc"))

    def test_compose_is_empty_without_scale_or_ass(self) -> None:
        self.assertIsNone(compose_video_filter(0, None))

    def test_ass_text_is_a_lower_third(self) -> None:
        from backend.app.services.overlays import Cue

        body = render_ass([Cue(1, 8, "verse", "João 11:25", "Disse-lhe Jesus: Eu sou a ressurreição e a vida.")])
        self.assertIn("Dialogue:", body)
        self.assertIn("João 11:25", body)
        self.assertIn("ressurreição", body)
        self.assertIn("BorderStyle", body)
        self.assertNotEqual(cue_token([]), cue_token([Cue(1, 8, "verse", "João 11:25", "vida")]))


class KindAndApiTest(unittest.TestCase):
    def test_recording_kind_covers_every_service(self) -> None:
        self.assertEqual(recording_kind("20260104_090000.mp4"), "ebd")
        self.assertEqual(recording_kind("20260104_101500.mp4"), "culto_manha")
        self.assertEqual(recording_kind("20260104_190000.mp4"), "culto_noite")
        self.assertEqual(clock_label(31 * 60 + 20), "0:31:20")

    def test_settings_and_resolve_endpoint(self) -> None:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        db.add(Speaker(name="Ana"))
        lecture = Lecture(drive_file_id="fid", video_filename="aula.mp4", status="awaiting_lyrics")
        db.add(lecture)
        db.commit()
        db.add(
            LyricPending(
                lecture_id=lecture.id,
                start_seconds=600,
                end_seconds=630,
                transcript_excerpt="aleluia aleluia gloria",
                signature="aleluia gloria",
                region_key="600:aleluia gloria",
                status="open",
            )
        )
        db.commit()
        app = FastAPI()
        app.include_router(api.router)

        def _get_db():
            yield db

        app.dependency_overrides[get_db] = _get_db
        client = TestClient(app)
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime.json"
            with (
                mock.patch("backend.app.runtime._path", runtime),
                mock.patch.object(settings, "data_dir", Path(tmp)),
            ):
                saved = client.put("/api/settings", json={"overlay_wait_hours": 0, "overlay_enabled": True})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()["overlay_wait_hours"], 0)
                self.assertTrue(saved.json()["overlay_enabled"])
                rejected = client.put("/api/settings", json={"overlay_wait_hours": -1})
                self.assertEqual(rejected.status_code, 400)
                listed = client.get("/api/lyrics/pending")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(len(listed.json()), 1)
                self.assertEqual(listed.json()[0]["start_label"], "0:10:00")
                missing = client.post("/api/lyrics/1/resolve", json={"action": "hymn", "query": "999"})
                self.assertEqual(missing.status_code, 400)
                pasted = client.post(
                    "/api/lyrics/1/resolve",
                    json={"action": "lyrics", "title": "Cordeiro", "lyrics": "Aleluia glória\n\nDigno é o Cordeiro"},
                )
                self.assertEqual(pasted.status_code, 200, pasted.text)
                self.assertEqual(pasted.json()["status"], "resolved")
                self.assertEqual(client.get("/api/lyrics/pending").json(), [])
        db.close()


if __name__ == "__main__":
    unittest.main()
