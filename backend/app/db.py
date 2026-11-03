import threading
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)
_Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
_schema_ready = False
_schema_lock = threading.Lock()


def SessionLocal() -> Session:
    """Abre uma sessão e aplica colunas novas antes da primeira consulta."""
    ensure_schema()
    return _Session()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema(bind: Engine | None = None) -> None:
    """Cria tabelas e adiciona colunas novas em bancos SQLite já existentes.

    Roda na abertura da sessão, não só no CLI, para um `state.db` antigo
    aguentar o modelo novo.
    """
    global _schema_ready
    target = bind or engine
    if bind is None and _schema_ready:
        return
    with _schema_lock:
        if bind is None and _schema_ready:
            return
        _apply_schema(target)
        if bind is None:
            _schema_ready = True


def _apply_schema(target: Engine) -> None:
    import backend.app.models  # noqa: F401

    Base.metadata.create_all(bind=target)
    columns = {
        "video_size": "INTEGER NOT NULL DEFAULT 0",
        "video_md5": "TEXT NOT NULL DEFAULT ''",
        "warning": "TEXT NOT NULL DEFAULT ''",
        "summary_speaker_id": "INTEGER",
        "youtube_id": "TEXT NOT NULL DEFAULT ''",
        "youtube_url": "TEXT NOT NULL DEFAULT ''",
        "youtube_title": "TEXT NOT NULL DEFAULT ''",
        "youtube_uploaded_at": "DATETIME",
        "original_size": "INTEGER NOT NULL DEFAULT 0",
        "compressed": "INTEGER NOT NULL DEFAULT 0",
        "compressed_path": "TEXT NOT NULL DEFAULT ''",
        "compressed_size": "INTEGER NOT NULL DEFAULT 0",
        "compressed_profile": "TEXT NOT NULL DEFAULT ''",
        "compressed_drive_file_id": "TEXT NOT NULL DEFAULT ''",
    }
    with target.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(lectures)").fetchall()
        if not rows:
            return
        existing = {row[1] for row in rows}
        for name, ddl in columns.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE lectures ADD COLUMN {name} {ddl}")
        speaker_rows = conn.exec_driver_sql("PRAGMA table_info(speakers)").fetchall()
        speaker_existing = {row[1] for row in speaker_rows}
        speaker_columns = {
            "embedding": "TEXT NOT NULL DEFAULT ''",
            "photo_note": "TEXT NOT NULL DEFAULT ''",
        }
        for name, ddl in speaker_columns.items():
            if speaker_rows and name not in speaker_existing:
                conn.exec_driver_sql(f"ALTER TABLE speakers ADD COLUMN {name} {ddl}")
