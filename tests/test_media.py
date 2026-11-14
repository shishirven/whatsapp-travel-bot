import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.media import (
    DownloadedMedia,
    GraphMediaDownloader,
    MediaNotConfigured,
    OpenAIWhisperTranscriber,
    UnconfiguredTranscriber,
    summarise_transcript,
)
from app.messaging import parse_webhook
from tests.conftest import make_settings
from tests.test_messaging import _payload


class _Response:
    def __init__(self, payload=None, content=b""):
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_parse_audio_and_image_media_ids():
    audio = _payload(None)
    audio["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "15551230000",
            "id": "wamid.aud",
            "type": "audio",
            "audio": {"id": "aud-1", "mime_type": "audio/ogg"},
        }
    ]
    inbound = parse_webhook(audio)[0]
    assert inbound.message_type == "audio"
    assert inbound.text is None
    assert inbound.media_id == "aud-1"
    assert inbound.mime_type == "audio/ogg"

    image = _payload(None)
    image["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "15551230000",
            "id": "wamid.img",
            "type": "image",
            "image": {"id": "img-1", "mime_type": "image/jpeg", "caption": "the board"},
        }
    ]
    inbound = parse_webhook(image)[0]
    assert inbound.media_id == "img-1"
    assert inbound.text == "the board"


def test_graph_downloader_fetches_metadata_then_bytes(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        if "graph.facebook.com" in url:
            return _Response({"url": "https://cdn.example/file", "mime_type": "audio/ogg"})
        return _Response(content=b"OggS")

    monkeypatch.setattr(httpx, "get", fake_get)
    media = GraphMediaDownloader("token", "v21.0").download("55")
    assert media.data == b"OggS"
    assert media.mime_type == "audio/ogg"
    assert calls[0][0] == "https://graph.facebook.com/v21.0/55"
    assert calls[0][1]["Authorization"] == "Bearer token"
    assert calls[1][0] == "https://cdn.example/file"
    assert calls[1][1]["Authorization"] == "Bearer token"


def test_whisper_posts_the_audio_and_can_be_swapped(monkeypatch):
    captured = {}

    def fake_post(url, headers, files, data, timeout):
        captured.update(url=url, headers=headers, files=files, data=data)
        return _Response({"text": "No power in Newtown."})

    monkeypatch.setattr(httpx, "post", fake_post)
    text = OpenAIWhisperTranscriber("sk-test", "whisper-1").transcribe(
        b"abc", filename="note.ogg", mime_type="audio/ogg"
    )
    assert text == "No power in Newtown."
    assert captured["url"] == "https://api.openai.com/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["data"]["model"] == "whisper-1"
    assert captured["files"]["file"][0] == "note.ogg"

    try:
        UnconfiguredTranscriber().transcribe(b"abc", filename="n.ogg", mime_type="audio/ogg")
    except MediaNotConfigured as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected MediaNotConfigured")


def test_summary_is_the_first_sentence():
    text = "No power in Newtown. The lights are out in the kitchen and the oven is dead."
    assert summarise_transcript(text) == "No power in Newtown."


def test_voice_note_is_transcribed_onto_the_job(tmp_path, notifier):
    class Downloader:
        def download(self, media_id):
            assert media_id == "aud-9"
            return DownloadedMedia(media_id, b"OggS", "audio/ogg", "aud-9.ogg")

    class Transcriber:
        def transcribe(self, audio, *, filename, mime_type):
            assert audio == b"OggS"
            assert filename == "aud-9.ogg"
            return (
                "No power in Newtown. My name is Pat Lee. "
                "The lights are out in the whole house tonight."
            )

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            notifier=notifier,
            media_downloader=Downloader(),
            transcriber=Transcriber(),
        )
    )
    payload = _payload(None)
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "15551230000",
            "id": "wamid.aud",
            "type": "audio",
            "audio": {"id": "aud-9", "mime_type": "audio/ogg"},
        }
    ]
    assert client.post("/webhook", json=payload).json()["processed"] == 1
    detail = client.get("/staff/1")
    assert "No power in Newtown." in detail.text
    assert "Pat Lee" in detail.text
    assert "call 000" in detail.text.lower() or "000" in detail.text
    lead_page = detail.text
    assert "Voice note:" in lead_page


def test_image_bytes_are_saved_and_served_on_the_job_card(tmp_path, notifier):
    class Downloader:
        def download(self, media_id):
            return DownloadedMedia(media_id, b"\xff\xd8\xff\xd9", "image/jpeg", f"{media_id}.jpg")

    client = TestClient(
        create_app(
            settings=make_settings(tmp_path),
            notifier=notifier,
            media_downloader=Downloader(),
        )
    )
    payload = _payload(None)
    payload["entry"][0]["changes"][0]["value"]["messages"] = [
        {
            "from": "15551230000",
            "id": "wamid.img",
            "type": "image",
            "image": {"id": "img-7", "mime_type": "image/jpeg", "caption": "meter box"},
        }
    ]
    assert client.post("/webhook", json=payload).json()["processed"] == 1
    page = client.get("/staff/1")
    assert "Photo" in page.text
    assert "media img-7" in page.text
    media = client.get("/staff/media/1/img-7")
    assert media.status_code == 200
    assert media.content.startswith(b"\xff\xd8")
