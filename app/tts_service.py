"""Kazakh text-to-speech via Google Cloud Text-to-Speech (kk-KZ voice).

Browser Web Speech API has no real Kazakh voice - it mispronounces qazaq-specific sounds
(қ/ғ/etc get read as their Russian near-equivalents). Google Cloud's kk-KZ voice actually
pronounces Kazakh correctly.
"""

import base64

import httpx

from app.config import settings

API_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


class TTSNotConfigured(Exception):
    pass


def synthesize_kazakh(text: str) -> bytes:
    if not settings.google_tts_api_key:
        raise TTSNotConfigured("Google TTS API key is not set")

    body = {
        "input": {"text": text},
        "voice": {"languageCode": "kk-KZ", "ssmlGender": "FEMALE"},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    response = httpx.post(
        API_URL,
        params={"key": settings.google_tts_api_key},
        json=body,
        timeout=15,
    )
    response.raise_for_status()
    audio_content = response.json()["audioContent"]
    return base64.b64decode(audio_content)
