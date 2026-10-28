from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from backend.app.config import settings

ERROR_LOG_NAME = "erros.log"
APP_LOG_NAME = "bible-notely.log"


def log_dir_for(day: date | None = None) -> Path:
    when = day or datetime.now().date()
    return settings.data_dir / "logs" / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"


class DailyDirFileHandler(logging.Handler):
    """Um arquivo por dia em data/logs/AAAA/MM/DD/."""

    terminator = "\n"

    def __init__(self, filename: str, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.filename = filename
        self._day: date | None = None
        self._stream = None

    def _switch(self, day: date) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None
        folder = log_dir_for(day)
        folder.mkdir(parents=True, exist_ok=True)
        self._stream = (folder / self.filename).open("a", encoding="utf-8")
        self._day = day

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = datetime.fromtimestamp(record.created).date()
            if day != self._day or self._stream is None:
                self._switch(day)
            assert self._stream is not None
            self._stream.write(self.format(record) + self.terminator)
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream:
            self._stream.close()
            self._stream = None
        super().close()


def setup_logging() -> None:
    log_dir_for().mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if any(getattr(handler, "name", "") == "bible-notely-erros" for handler in root.handlers):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = DailyDirFileHandler(APP_LOG_NAME)
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    error_handler = DailyDirFileHandler(ERROR_LOG_NAME)
    error_handler.name = "bible-notely-erros"
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    root.setLevel(logging.INFO)
    # httpx loga a URL completa, e a chave do Gemini vai na query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    root.addHandler(error_handler)
    root.addHandler(file_handler)
    root.addHandler(stream)
