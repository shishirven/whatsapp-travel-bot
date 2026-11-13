"""Inbound WhatsApp media: download from Graph, transcribe audio, summarise.

`MediaDownloader` and `Transcriber` are the seams. Tests pass fakes. Whisper is
one transcriber; swap the class to change providers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

logger = logging.getLogger("tradie.media")

_MAX_BYTES = 16 * 1024 * 1024
_EXTENSIONS = {
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class MediaError(RuntimeError):
    """The media call was shaped correctly but did not succeed."""


class MediaNotConfigured(MediaError):
    """Credentials for this media step are empty."""


@dataclass(frozen=True)
class DownloadedMedia:
    media_id: str
    data: bytes
    mime_type: str
    filename: str


class MediaDownloader(Protocol):
    def download(self, media_id: str) -> DownloadedMedia:
        """Fetch one media object. Return the bytes and a filename."""


class Transcriber(Protocol):
    def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        """Turn audio bytes into text."""


class UnconfiguredMediaDownloader:
    def download(self, media_id: str) -> DownloadedMedia:
        raise MediaNotConfigured(
            "WhatsApp media download is not configured. TODO: set WHATSAPP_TOKEN."
        )


class GraphMediaDownloader:
    """GET the media metadata, then GET the temporary URL, both with the token."""

    def __init__(self, token: str, version: str = "v21.0", client=None) -> None:
        self.token = token
        self.version = version
        self._client = client

    def download(self, media_id: str) -> DownloadedMedia:
        if not self.token:
            raise MediaNotConfigured("TODO: set WHATSAPP_TOKEN to download WhatsApp media.")
        headers = {"Authorization": f"Bearer {self.token}"}
        meta_url = f"https://graph.facebook.com/{self.version}/{media_id}"
        meta = self._get(meta_url, headers)
        try:
            url = meta.json()["url"]
            mime = str(meta.json().get("mime_type") or "application/octet-stream")
        except (KeyError, TypeError, ValueError) as exc:
            raise MediaError(f"unexpected media metadata for {media_id}") from exc
        body = self._get(url, headers)
        data = body.content
        if len(data) > _MAX_BYTES:
            raise MediaError(f"media {media_id} is larger than {_MAX_BYTES} bytes")
        return DownloadedMedia(
            media_id=media_id,
            data=data,
            mime_type=mime,
            filename=f"{_safe_id(media_id)}.{extension_for(mime)}",
        )

    def _get(self, url: str, headers: dict):
        try:
            if self._client is not None:
                response = self._client.get(url, headers=headers, timeout=30)
            else:
                response = httpx.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise MediaError(str(exc)) from exc


class UnconfiguredTranscriber:
    def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        raise MediaNotConfigured(
            "Audio transcription is not configured. TODO: set OPENAI_API_KEY."
        )


class OpenAIWhisperTranscriber:
    """OpenAI audio transcriptions endpoint. Swap this class to change providers."""

    def __init__(self, api_key: str, model: str = "whisper-1", client=None) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client

    def transcribe(self, audio: bytes, *, filename: str, mime_type: str) -> str:
        if not self.api_key:
            raise MediaNotConfigured("TODO: set OPENAI_API_KEY to transcribe voice notes.")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": (filename or "note.ogg", audio, mime_type or "audio/ogg")}
        data = {"model": self.model}
        url = "https://api.openai.com/v1/audio/transcriptions"
        try:
            if self._client is not None:
                response = self._client.post(url, headers=headers, files=files, data=data, timeout=60)
            else:
                response = httpx.post(url, headers=headers, files=files, data=data, timeout=60)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise MediaError(str(exc)) from exc
        text = str((payload or {}).get("text") or "").strip()
        if not text:
            raise MediaError("transcription response had no text")
        return text[:8000]


def extension_for(mime_type: str | None) -> str:
    if not mime_type:
        return "bin"
    base = mime_type.split(";", 1)[0].strip().lower()
    return _EXTENSIONS.get(base, "bin")


def save_media(directory: Path, downloaded: DownloadedMedia) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    filename = downloaded.filename or f"{_safe_id(downloaded.media_id)}.{extension_for(downloaded.mime_type)}"
    path = directory / filename
    path.write_bytes(downloaded.data)
    return str(path)


def summarise_transcript(text: str, limit: int = 240) -> str:
    """First sentence, collapsed, capped. Enough for a job card, not a rewrite."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", cleaned, maxsplit=1)[0]
    if len(sentence) <= limit:
        return sentence
    return sentence[: limit - 1].rstrip() + "…"


def _safe_id(media_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "", media_id or "")
    return cleaned or "media"
