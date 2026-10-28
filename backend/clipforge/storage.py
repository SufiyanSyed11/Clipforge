"""Media storage. Local folder by default; an S3 implementation slots in for the cloud."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO, Protocol

from clipforge.settings import get_settings


class Storage(Protocol):
    def project_dir(self, project_id: int) -> Path: ...
    def save_stream(self, project_id: int, name: str, stream: BinaryIO) -> Path: ...


class LocalStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_settings().data_dir

    def project_dir(self, project_id: int) -> Path:
        d = self.root / "projects" / f"{project_id:05d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_stream(self, project_id: int, name: str, stream: BinaryIO) -> Path:
        # Keep only the file name: never let an upload choose its own path.
        dest = self.project_dir(project_id) / Path(name).name
        with dest.open("wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)
        return dest


def get_storage() -> Storage:
    return LocalStorage()
