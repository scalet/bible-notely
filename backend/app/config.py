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
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
