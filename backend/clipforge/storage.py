"""Media storage. Local folder by default; an S3 implementation slots in for the cloud."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Protocol

from clipforge.settings import get_settings


class UploadTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"Upload is larger than {limit / 1024**3:.0f} GB")


class Storage(Protocol):
    def project_dir(self, project_id: int) -> Path: ...
    def save_stream(
        self, project_id: int, name: str, stream: BinaryIO, max_bytes: int | None = None
    ) -> Path: ...


class LocalStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_settings().data_dir

    def project_dir(self, project_id: int) -> Path:
        d = self.root / "projects" / f"{project_id:05d}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_stream(
        self, project_id: int, name: str, stream: BinaryIO, max_bytes: int | None = None
    ) -> Path:
        # Keep only the file name: never let an upload choose its own path.
        dest = self.project_dir(project_id) / Path(name).name
        tmp = dest.with_suffix(dest.suffix + ".part")
        size = 0
        with tmp.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise UploadTooLarge(max_bytes)
                out.write(chunk)
        tmp.replace(dest)
        return dest


def get_storage() -> Storage:
    return LocalStorage()
