from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    data_dir: Path = ROOT / "data"
    work_dir: Path = ROOT / "work"
    obsidian_dir: Path = ROOT / "data" / "obsidian"

    google_drive_folder_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/drive/callback"

    ai_provider: str = "gemini"
    ai_model: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    grok_api_key: str = ""
    grok_model: str = "grok-2-vision-latest"
    grok_base_url: str = "https://api.x.ai/v1"

    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-5"

    copilot_api_key: str = ""
    copilot_model: str = "gpt-4o"
    copilot_base_url: str = "https://models.github.ai/inference"

    min_video_bytes: int = 1024**3  # MP4 abaixo disso não entra na lista

    whisper_model: str = "medium"
    whisper_language: str = "pt"

    # Reencode depois da transcrição. O original local sai no fim da aula; false guarda a cópia.
    compress_enabled: bool = True
    compress_crf: int = 23
    compress_preset: str = "veryfast"
    compress_max_height: int = 0  # 0 mantém a altura
    compress_codec: str = "h264"  # h264 | h265
    compress_audio: str = "copy"  # copy | aac
    compress_audio_bitrate: str = "128k"
    compress_delete_original: bool = True
    compress_upload_drive: bool = False
    compress_duration_tolerance: float = 1.0

    # Versículos e letras no mesmo encode. 0 horas não espera.
    overlay_enabled: bool = True
    overlay_wait_hours: float = 24.0

    # Passo opcional publish. Desligado: o pipeline não fala com o Bunny nem com o Maná.
    publish_enabled: bool = False
    publish_transcode_720: bool = False
    publish_chunk_bytes: int = 8 * 1024 * 1024
    publish_encode_timeout_seconds: int = 3600
    publish_encode_poll_seconds: float = 15.0
    bunny_stream_library_id: str = ""
    bunny_stream_api_key: str = ""
    bunny_stream_api_base: str = "https://video.bunnycdn.com"
    bunny_tus_endpoint: str = "https://video.bunnycdn.com/tusupload"
    mana_api_url: str = ""
    mana_session_token: str = ""
    mana_speaker_map: str = ""
    # Desligado: não chama o Maná. Ligado ainda exige sessão Google; ver docs/mana-publish.md.
    mana_publish_enabled: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def speakers_dir(self) -> Path:
        return self.data_dir / "speakers"

    @property
    def speakers_fotos_dir(self) -> Path:
        return self.speakers_dir / "fotos"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def frames_dir(self) -> Path:
        return self.data_dir / "frames"

    @property
    def google_token_path(self) -> Path:
        return self.data_dir / "google_token.json"

    @property
    def youtube_dir(self) -> Path:
        return self.data_dir / "youtube"

    @property
    def youtube_token_path(self) -> Path:
        return self.youtube_dir / "token.json"

    @property
    def youtube_state_path(self) -> Path:
        return self.youtube_dir / "state.json"

    @property
    def youtube_work_dir(self) -> Path:
        return self.work_dir / "youtube"

    @property
    def publish_dir(self) -> Path:
        return self.data_dir / "publish"

    @property
    def publish_state_path(self) -> Path:
        return self.publish_dir / "state.json"

    @property
    def publish_speakers_path(self) -> Path:
        return self.publish_dir / "speakers.json"

    @property
    def publish_work_dir(self) -> Path:
        return self.work_dir / "publish"

    @property
    def prompt_path(self) -> Path:
        return ROOT / "prompts" / "resumo.txt"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.work_dir,
            self.obsidian_dir,
            self.speakers_dir,
            self.speakers_fotos_dir,
            self.models_dir,
            self.frames_dir,
            self.youtube_dir,
            self.youtube_work_dir,
            self.publish_dir,
            self.publish_work_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
