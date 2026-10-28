"""YouTube Data/Analytics API access with the owner's own OAuth consent.

- OAuth "installed app" flow with PKCE and a loopback redirect to the local API. The owner
  creates a free OAuth client (type: Desktop app) in Google Cloud and puts its id/secret in .env.
- Read-only scopes by default. Upload (private, scheduled) is a separate, opt-in scope.
- Tokens are stored in data/secrets/youtube_token.json, readable only by the current user.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from clipforge.core.learn import VideoStat

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
READ_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeError(Exception):
    pass


def _token_path(data_dir: Path) -> Path:
    return data_dir / "secrets" / "youtube_token.json"


def save_token(data_dir: Path, token: dict) -> None:
    path = _token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(token), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # owner-only where the OS supports it
    except OSError:
        pass
    tmp.replace(path)


def load_token(data_dir: Path) -> dict | None:
    p = _token_path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def forget(data_dir: Path) -> None:
    _token_path(data_dir).unlink(missing_ok=True)


class OAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.http = httpx.Client(timeout=30.0, transport=transport)

    def start(self, allow_upload: bool) -> tuple[str, dict]:
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        state = secrets.token_urlsafe(24)
        scopes = READ_SCOPES + ([UPLOAD_SCOPE] if allow_upload else [])
        url = (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "redirect_uri": self.redirect_uri,
                    "response_type": "code",
                    "scope": " ".join(scopes),
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        return url, {"state": state, "verifier": verifier, "created": time.time()}

    def finish(self, code: str, verifier: str) -> dict:
        r = self.http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        )
        if r.status_code != 200:
            raise YouTubeError(f"Google refused the sign-in: {r.text[:300]}")
        tok = r.json()
        tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600)) - 60
        return tok

    def refresh(self, token: dict) -> dict:
        if not token.get("refresh_token"):
            raise YouTubeError("YouTube access expired; connect again")
        r = self.http.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if r.status_code != 200:
            raise YouTubeError(f"Couldn't refresh YouTube access: {r.text[:300]}")
        new = {**token, **r.json()}
        new["expires_at"] = time.time() + float(new.get("expires_in", 3600)) - 60
        return new


class YouTube:
    def __init__(self, oauth: OAuth, data_dir: Path, transport: httpx.BaseTransport | None = None):
        self.oauth = oauth
        self.data_dir = data_dir
        self.http = httpx.Client(timeout=60.0, transport=transport)

    def _auth(self) -> dict[str, str]:
        tok = load_token(self.data_dir)
        if not tok:
            raise YouTubeError("YouTube isn't connected")
        if tok.get("expires_at", 0) < time.time():
            tok = self.oauth.refresh(tok)
            save_token(self.data_dir, tok)
        return {"Authorization": f"Bearer {tok['access_token']}"}

    def _get(self, url: str, params: dict) -> dict:
        r = self.http.get(url, params=params, headers=self._auth())
        if r.status_code != 200:
            raise YouTubeError(f"YouTube API {r.status_code}: {r.text[:300]}")
        return r.json()

    def recent_uploads(self, days: int) -> list[VideoStat]:
        ch = self._get(f"{API}/channels", {"part": "contentDetails,snippet", "mine": "true"})
        items = ch.get("items", [])
        if not items:
            raise YouTubeError("No channel on this Google account")
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = self._get(
            f"{API}/playlistItems",
            {"part": "contentDetails", "playlistId": uploads, "maxResults": 50},
        )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        ids = [
            it["contentDetails"]["videoId"]
            for it in pl.get("items", [])
            if _parse_dt(it["contentDetails"].get("videoPublishedAt")) >= cutoff
        ]
        if not ids:
            return []
        vids = self._get(
            f"{API}/videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(ids)},
        )
        stats: dict[str, VideoStat] = {}
        for v in vids.get("items", []):
            st = v.get("statistics", {})
            stats[v["id"]] = VideoStat(
                video_id=v["id"],
                title=v["snippet"]["title"],
                published_at=_parse_dt(v["snippet"]["publishedAt"]),
                duration_s=_iso_duration(v["contentDetails"]["duration"]),
                views=float(st.get("viewCount", 0)),
                likes=float(st.get("likeCount", 0)) if "likeCount" in st else None,
                comments=float(st.get("commentCount", 0)) if "commentCount" in st else None,
            )
        try:
            rep = self._get(
                ANALYTICS,
                {
                    "ids": "channel==MINE",
                    "startDate": (cutoff - timedelta(days=1)).date().isoformat(),
                    "endDate": datetime.now(UTC).date().isoformat(),
                    "metrics": "views,averageViewDuration,averageViewPercentage",
                    "dimensions": "video",
                    "filters": "video==" + ",".join(ids),
                },
            )
            for row in rep.get("rows", []):
                vid, _views, avg_s, avg_pct = row[0], row[1], row[2], row[3]
                if vid in stats:
                    stats[vid].avg_view_s = float(avg_s)
                    stats[vid].avg_view_pct = float(avg_pct)
        except YouTubeError:
            pass  # analytics can lag a day or two; views alone still rank uploads
        return list(stats.values())

    def upload_private_scheduled(
        self, path: Path, title: str, description: str, tags: list[str], publish_at: datetime
    ) -> str:
        """Upload as PRIVATE with a scheduled publish time (YouTube publishes it then)."""
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": [t.lstrip("#") for t in tags][:15],
                "categoryId": "17",
            },
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "selfDeclaredMadeForKids": False,
            },
        }
        init = self.http.post(
            UPLOAD,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **self._auth(),
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(path.stat().st_size),
            },
            json=body,
        )
        if init.status_code != 200 or "location" not in init.headers:
            raise YouTubeError(f"Upload refused: {init.status_code} {init.text[:300]}")
        with path.open("rb") as f:
            r = self.http.put(
                init.headers["location"],
                content=f.read(),
                headers={**self._auth(), "Content-Type": "video/mp4"},
                timeout=600.0,
            )
        if r.status_code not in (200, 201):
            raise YouTubeError(f"Upload failed: {r.status_code} {r.text[:300]}")
        return r.json()["id"]


def _parse_dt(v: str | None) -> datetime:
    if not v:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _iso_duration(v: str) -> float:
    import re

    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", v or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def status(data_dir: Path) -> dict[str, Any]:
    tok = load_token(data_dir)
    return {
        "connected": bool(tok),
        "can_upload": bool(tok and UPLOAD_SCOPE in tok.get("scope", "")),
    }
