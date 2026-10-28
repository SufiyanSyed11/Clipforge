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

    # Language model for clip picking, B-roll queries and copy. "ollama" (local, free) is the
    # default; "anthropic" is optional and needs ANTHROPIC_API_KEY.
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    # Context window in tokens. 0 = sized by clipforge-check for this GPU (12288 on 6 GB VRAM:
    # qwen3:4b ~2.5 GB + KV cache fits with room to spare). Long transcripts are split into
    # windows that fit.
    ollama_num_ctx: int = 0
    ollama_keep_alive: str = "2m"
    ollama_timeout_seconds: float = 900.0
    ollama_temperature: float = 0.2
    # Vision model for checking B-roll contact sheets: none (manual approval) | ollama | anthropic
    vision_provider: str = "none"
    ollama_vision_model: str = "qwen2.5vl:3b"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "high"
    anthropic_fallbacks: str = "default"  # "default" or "off"

    # Video encoder: auto (NVENC if the GPU supports it, else libx264) | h264_nvenc | libx264
    render_encoder: str = "auto"

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
