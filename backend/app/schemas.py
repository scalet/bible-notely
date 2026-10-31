from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class SpeakerIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str = ""
    active: bool = True


class SpeakerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    role: str
    photo_path: str
    photo_note: str = ""
    active: bool
    created_at: datetime
    lecture_count: int = 0
    photo_url: str | None = None
    face_ready: bool = False


class LectureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    drive_file_id: str
    drive_folder_id: str
    drive_path: str
    video_filename: str
    transcript_filename: str
    summary_filename: str
    lecture_date: date | None
    speaker_id: int | None
    speaker_name: str | None = None
    speaker_confidence: float | None
    identification_status: str
    identification_note: str
    frame_path: str
    frame_url: str | None = None
    summary_text: str
    warning: str = ""
    video_size: int = 0
    transcribed: bool
    summarized: bool
    uploaded: bool
    youtube_id: str = ""
    youtube_url: str = ""
    youtube_title: str = ""
    youtube_uploaded_at: datetime | None = None
    status: str
    error: str
    updated_at: datetime


class AssignSpeakerIn(BaseModel):
    speaker_id: int


class SettingsOut(BaseModel):
    google_connected: bool
    google_account: str = ""
    google_drive_folder_id: str
    google_client_configured: bool
    ai_provider: str
    ai_model: str
    providers: dict[str, bool]
    whisper_model: str
    whisper_language: str
    speakers_photo_dir: str = ""
    youtube_connected: bool = False
    youtube_upload_folder_id: str = ""
    youtube_upload_path: str = ""
    youtube_scope_eligible: int = 0
    youtube_scope_pending: int = 0
    youtube_scope_uploaded: int = 0


class SettingsIn(BaseModel):
    ai_provider: str | None = None
    google_drive_folder_id: str | None = None
    youtube_upload_folder_id: str | None = None
    youtube_upload_path: str | None = None


class YouTubeFolderOut(BaseModel):
    id: str
    name: str
    path: str


class YouTubeBrowseOut(BaseModel):
    parent_id: str
    parent_path: str
    selection_folder_id: str = ""
    selection_path: str = ""
    stats: dict[str, int]
    folders: list[YouTubeFolderOut]


class PipelineStatus(BaseModel):
    running: bool
    current_lecture_id: int | None = None
    current_filename: str | None = None
    stage: str | None = None
    message: str = ""
    progress: int | None = None
    auto: bool = False
    pause_requested: bool = False
    prefetch_filename: str | None = None
    prefetch_progress: int | None = None
    processed_today: int = 0
    work_free_bytes: int = 0
    drive_video_count: int | None = None
