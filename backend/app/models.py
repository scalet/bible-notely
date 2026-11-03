from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


class Speaker(Base):
    __tablename__ = "speakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    role: Mapped[str] = mapped_column(String(200), default="")
    photo_path: Mapped[str] = mapped_column(String(500), default="")
    embedding: Mapped[str] = mapped_column(Text, default="")
    photo_note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lectures: Mapped[list["Lecture"]] = relationship(back_populates="speaker")


class Lecture(Base):
    __tablename__ = "lectures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drive_file_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    drive_folder_id: Mapped[str] = mapped_column(String(128), default="")
    drive_path: Mapped[str] = mapped_column(String(400), default="")
    video_filename: Mapped[str] = mapped_column(String(400), default="")
    transcript_filename: Mapped[str] = mapped_column(String(400), default="")
    summary_filename: Mapped[str] = mapped_column(String(400), default="")
    video_size: Mapped[int] = mapped_column(Integer, default=0)
    video_md5: Mapped[str] = mapped_column(String(32), default="")
    original_size: Mapped[int] = mapped_column(Integer, default=0)
    compressed: Mapped[bool] = mapped_column(Boolean, default=False)
    compressed_path: Mapped[str] = mapped_column(String(500), default="")
    compressed_size: Mapped[int] = mapped_column(Integer, default=0)
    compressed_profile: Mapped[str] = mapped_column(String(200), default="")
    compressed_drive_file_id: Mapped[str] = mapped_column(String(128), default="")
    lecture_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    speaker_id: Mapped[int | None] = mapped_column(ForeignKey("speakers.id"), nullable=True)
    speaker_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    identification_status: Mapped[str] = mapped_column(String(32), default="pending")
    identification_note: Mapped[str] = mapped_column(Text, default="")
    frame_path: Mapped[str] = mapped_column(String(500), default="")
    summary_text: Mapped[str] = mapped_column(Text, default="")
    summary_speaker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warning: Mapped[str] = mapped_column(Text, default="")
    transcribed: Mapped[bool] = mapped_column(Boolean, default=False)
    summarized: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded: Mapped[bool] = mapped_column(Boolean, default=False)
    youtube_id: Mapped[str] = mapped_column(String(32), default="")
    youtube_url: Mapped[str] = mapped_column(String(256), default="")
    youtube_title: Mapped[str] = mapped_column(String(200), default="")
    youtube_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    speaker: Mapped[Speaker | None] = relationship(back_populates="lectures")
    lyric_pendings: Mapped[list["LyricPending"]] = relationship(back_populates="lecture")


class LyricPending(Base):
    """Música não reconhecida. O prazo evita que a aula fique presa para sempre."""

    __tablename__ = "lyric_pendings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lecture_id: Mapped[int] = mapped_column(ForeignKey("lectures.id"), index=True)
    start_seconds: Mapped[float] = mapped_column(Float, default=0)
    end_seconds: Mapped[float] = mapped_column(Float, default=0)
    snippet_path: Mapped[str] = mapped_column(String(500), default="")
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(String(500), default="")
    region_key: Mapped[str] = mapped_column(String(600), default="")
    status: Mapped[str] = mapped_column(String(32), default="open", index=True)
    resolution: Mapped[str] = mapped_column(String(32), default="")
    hymn_number: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    lyrics: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lecture: Mapped[Lecture] = relationship(back_populates="lyric_pendings")
