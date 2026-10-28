import logging
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from backend.app import logging_setup
from backend.app.config import settings


class LogDirTest(unittest.TestCase):
    def test_path_is_year_month_day(self) -> None:
        folder = logging_setup.log_dir_for(date(2026, 10, 20))
        self.assertEqual(folder.parts[-3:], ("2026", "10", "20"))

    def test_handler_writes_into_the_day_folder(self) -> None:
        root = Path(tempfile.mkdtemp())
        handler = logging_setup.DailyDirFileHandler("bible-notely.log")
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "ola", (), None)
        record.created = datetime(2026, 3, 9, 12, 0, 0).timestamp()
        with mock.patch.object(settings, "data_dir", root):
            handler.emit(record)
            handler.close()
            path = root / "logs" / "2026" / "03" / "09" / "bible-notely.log"
        self.assertTrue(path.exists())
        self.assertIn("ola", path.read_text(encoding="utf-8"))
