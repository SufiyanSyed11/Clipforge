"""Runtime configuration, read from environment variables and the repo-root `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = REPO_ROOT / "data"
    database_url: str = ""  # empty -> sqlite file inside data_dir

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://localhost:3000"

    worker_poll_seconds: float = 1.0
    # A running job whose heartbeat is older than this is considered abandoned and re-queued.
    job_stale_seconds: float = 300.0

    # Transcription. "auto" resolves from data/env_report.json (clipforge-check).
    whisper_model: str = "auto"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"
    whisper_chunk_minutes: float = 10.0
    whisper_chunk_overlap_seconds: float = 5.0

    # Diarization: auto | basic | pyannote
    diarization_backend: str = "auto"
    hf_token: str = ""

    # Clip picker
    anthropic_api_key: str = ""
    picker_model: str = "claude-opus-5-5"
    picker_effort: str = "high"
    picker_fallbacks: str = "default"  # "default" or "off"
    picker_prompt_version: str = "v1"
    picker_max_retries: int = 2

    # Test-only: replace whisper/diarization/Claude with deterministic stubs so the pipeline
    # plumbing can be exercised without models. Never enable for real runs.
    fake_models: bool = False

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'clipforge.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
