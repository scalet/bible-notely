from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> None:
    """Cria tabelas e adiciona colunas novas em bancos SQLite já existentes."""
    import backend.app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    columns = {
        "video_size": "INTEGER NOT NULL DEFAULT 0",
        "video_md5": "TEXT NOT NULL DEFAULT ''",
        "warning": "TEXT NOT NULL DEFAULT ''",
        "summary_speaker_id": "INTEGER",
    }
    with engine.begin() as conn:
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
