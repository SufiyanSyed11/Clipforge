"""Footage sources: Pexels, Pixabay (free API keys), permitted YouTube footage, and the
owner's own library. Only 1080p+ files are returned."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

MIN_SHORT_SIDE = 1080  # "require 1080p+"


@dataclass
class Candidate:
    source: str
    source_id: str
    title: str
    page_url: str
    download_url: str | None  # None for local files
    width: int
    height: int
    duration: float
    license: str
    tags: list[str] = field(default_factory=list)
    local_path: str | None = None

    @property
    def key(self) -> str:
        return f"{self.source}_{self.source_id}"


class Source(Protocol):
    name: str

    def available(self) -> bool: ...
    def search(self, query: str, per_page: int = 8) -> list[Candidate]: ...


class PexelsSource:
    name = "pexels"
    LICENSE = "Pexels License (free to use, no attribution required)"

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.key = api_key
        self.client = httpx.Client(
            base_url="https://api.pexels.com", timeout=20.0, transport=transport
        )

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, per_page: int = 8) -> list[Candidate]:
        r = self.client.get(
            "/videos/search",
            params={"query": query, "per_page": per_page, "size": "large"},
            headers={"Authorization": self.key},
        )
        r.raise_for_status()
        out = []
        for v in r.json().get("videos", []):
            files = [
                f
                for f in v.get("video_files", [])
                if min(f.get("width") or 0, f.get("height") or 0) >= MIN_SHORT_SIDE
                and f.get("file_type") == "video/mp4"
            ]
            if not files:
                continue
            f = min(files, key=lambda f: f["width"] * f["height"])  # smallest that is 1080p+
            slug = v.get("url", "").rstrip("/").split("/")[-1]
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(v["id"]),
                    title=slug.replace("-", " "),
                    page_url=v.get("url", ""),
                    download_url=f["link"],
                    width=int(f["width"]),
                    height=int(f["height"]),
                    duration=float(v.get("duration") or 0),
                    license=self.LICENSE,
                    tags=[t for t in slug.split("-") if t and not t.isdigit()],
                )
            )
        return out


class PixabaySource:
    name = "pixabay"
    LICENSE = "Pixabay Content License (free to use, no attribution required)"

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None) -> None:
        self.key = api_key
        self.client = httpx.Client(
            base_url="https://pixabay.com", timeout=20.0, transport=transport
        )

    def available(self) -> bool:
        return bool(self.key)

    def search(self, query: str, per_page: int = 8) -> list[Candidate]:
        r = self.client.get(
            "/api/videos/",
            params={
                "key": self.key,
                "q": query,
                "per_page": max(3, per_page),
                "safesearch": "true",
            },
        )
        r.raise_for_status()
        out = []
        for h in r.json().get("hits", []):
            vids = h.get("videos", {})
            best = None
            for q in ("large", "medium"):
                v = vids.get(q) or {}
                if (
                    v.get("url")
                    and min(v.get("width") or 0, v.get("height") or 0) >= MIN_SHORT_SIDE
                ):
                    best = v
                    break
            if not best:
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(h["id"]),
                    title=h.get("tags", ""),
                    page_url=h.get("pageURL", ""),
                    download_url=best["url"],
                    width=int(best["width"]),
                    height=int(best["height"]),
                    duration=float(h.get("duration") or 0),
                    license=self.LICENSE,
                    tags=[t.strip() for t in h.get("tags", "").split(",") if t.strip()],
                )
            )
        return out


class LibrarySource:
    """The owner's own footage: files in data/broll_library (with optional sidecar
    <name>.json {"title": ..., "tags": [...], "license": ...})."""

    name = "library"

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def available(self) -> bool:
        return self.folder.exists() and any(self.folder.glob("*.mp4"))

    def search(self, query: str, per_page: int = 8) -> list[Candidate]:
        from clipforge.core.ingest import probe

        terms = {t.lower() for t in query.split() if len(t) > 2}
        scored = []
        for f in sorted(self.folder.glob("*.mp4")):
            meta_path = f.with_suffix(".json")
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            title = meta.get("title") or f.stem.replace("_", " ").replace("-", " ")
            tags = [t.lower() for t in meta.get("tags", [])]
            hay = set(title.lower().split()) | set(tags)
            score = len(terms & hay)
            if score == 0:
                continue
            info = probe(f)
            if min(info.width or 0, info.height or 0) < MIN_SHORT_SIDE:
                continue
            scored.append(
                (
                    score,
                    Candidate(
                        source=self.name,
                        source_id=f.stem,
                        title=title,
                        page_url="",
                        download_url=None,
                        width=info.width or 0,
                        height=info.height or 0,
                        duration=info.duration,
                        license=meta.get("license", "owner's own footage"),
                        tags=tags,
                        local_path=str(f),
                    ),
                )
            )
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:per_page]]


class PermittedYouTubeSource:
    """YouTube videos the owner listed for this project with rights confirmed. Searched by
    title/description words; downloaded once with yt-dlp (one muxed file)."""

    name = "youtube"

    def __init__(self, entries: list[dict], cache: Path) -> None:
        self.entries = [e for e in entries if e.get("rights_confirmed") and e.get("url")]
        self.cache = cache

    def available(self) -> bool:
        return bool(self.entries)

    def _info(self, url: str) -> dict:
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        cache = self.cache / f"yt_{key}.info.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist", url],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        info = json.loads(proc.stdout)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    k: info.get(k)
                    for k in (
                        "id",
                        "title",
                        "description",
                        "tags",
                        "width",
                        "height",
                        "duration",
                        "webpage_url",
                        "license",
                    )
                }
            ),
            encoding="utf-8",
        )
        return json.loads(cache.read_text(encoding="utf-8"))

    def search(self, query: str, per_page: int = 8) -> list[Candidate]:
        terms = {t.lower() for t in query.split() if len(t) > 2}
        out = []
        for e in self.entries:
            info = self._info(e["url"])
            if not info:
                continue
            text = " ".join(
                [
                    info.get("title") or "",
                    info.get("description") or "",
                    " ".join(info.get("tags") or []),
                ]
            ).lower()
            if not terms & set(text.split()):
                continue
            if min(info.get("width") or 0, info.get("height") or 0) < MIN_SHORT_SIDE:
                continue
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(info.get("id")),
                    title=info.get("title") or "",
                    page_url=info.get("webpage_url") or e["url"],
                    download_url=e["url"],
                    width=int(info.get("width") or 0),
                    height=int(info.get("height") or 0),
                    duration=float(info.get("duration") or 0),
                    license=e.get("license") or "permission confirmed by the owner",
                    tags=list(info.get("tags") or []),
                )
            )
        return out[:per_page]
