"""Fish Audio speech providers for Hermes (TTS + transcription).

Registers:
  - TTS provider ``fish`` (FishSpeechTTS): Fish Audio s2.1-pro synthesis with
    per-reply language voice selection (Russian replies use the Russian
    reference voice, everything else the configured general voice).
  - Transcription provider ``fish`` (FishTranscription): Fish Audio ASR.

Credentials come from the environment (``FISH_API_KEY`` required,
``OPENROUTER_API_KEY`` optional for language classification). Never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import urllib.request
import uuid
from typing import Any, Dict, Optional

from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider

from . import fish_tts
from .language_voice import LanguageVoice

ASR_API = "https://api.fish.audio/v1/asr"


def _run(coro):
    """Drive an async body to completion from sync provider methods.

    Runs in a helper thread so it works whether or not the calling thread
    already has a running event loop. Exceptions propagate to the caller.
    """
    outcome: Dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as error:  # noqa: BLE001
            outcome["error"] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _fish_key() -> str:
    key = os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FISH_API_KEY is not set")
    return key


class FishSpeechTTS(TTSProvider):
    """Fish Audio TTS with language-aware voice selection."""

    @property
    def name(self) -> str:
        return "fish"

    def default_voice(self) -> Optional[str]:
        return fish_tts.DEFAULT_REFERENCE_ID

    def default_model(self) -> Optional[str]:
        return fish_tts.MODEL

    def list_voices(self):
        return [
            {"id": fish_tts.DEFAULT_REFERENCE_ID, "display": "Fish general",
             "language": "multi"},
            {"id": "c35aeeb5f9c145199fbffdbc2ef8ed95",
             "display": "Fish Russian", "language": "ru"},
        ]

    def get_setup_schema(self):
        return {"name": self.display_name, "badge": "Fish Audio",
                "tag": "paid TTS + language voices",
                "env_vars": [{"key": "FISH_API_KEY",
                              "prompt": "Fish Audio API key",
                              "url": "https://fish.audio/"},
                             {"key": "OPENROUTER_API_KEY",
                              "prompt": "OpenRouter key (optional, language voice selection)",
                              "url": "https://openrouter.ai/"}]}

    def synthesize(self, text, output_path, *, voice=None, model=None,
                   speed=None, format="mp3", **extra):
        if format != "mp3":
            output_path = (output_path[: -len(format)] + "mp3"
                           if output_path.endswith("." + format)
                           else output_path + ".mp3")
        reference = voice or self.default_voice()
        chosen = fish_tts.validate_reference_id(reference)
        chosen_model = fish_tts.validate_model(model or self.default_model())
        audio = _run(self._render(text, chosen, chosen_model, speed))
        with open(output_path, "wb") as out:
            out.write(audio)
        return output_path

    async def _render(self, text, reference, model, speed):
        fish_tts.validate_text(text)
        speech = fish_tts.FishSpeech(_fish_key())
        language = LanguageVoice(os.environ.get("OPENROUTER_API_KEY", ""))
        try:
            effective, _ = await language.select(text, reference)
            return await speech.synthesize(text, effective, model, speed)
        finally:
            try:
                await language.close()
            finally:
                await speech.close()


class FishTranscription(TranscriptionProvider):
    """Fish Audio ASR transcription."""

    @property
    def name(self) -> str:
        return "fish"

    def transcribe(self, file_path, *, model=None, language=None, **extra):
        try:
            with open(file_path, "rb") as source:
                audio = source.read()
        except OSError:
            return {"success": False, "error": "audio file is unreadable"}
        boundary = uuid.uuid4().hex
        parts = []
        fname = os.path.basename(file_path) or "audio.ogg"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
            f'filename="{fname}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
        parts.append(audio)
        parts.append(b"\r\n")
        if language and language not in ("auto", "none"):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="language"'
                f'\r\n\r\n{language}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="ignore_timestamps"'
            f"\r\n\r\ntrue\r\n".encode())
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            ASR_API, data=b"".join(parts),
            headers={"Authorization": f"Bearer {_fish_key()}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.loads(response.read())
        except Exception as error:  # network/API failure -> error envelope
            return {"success": False, "error": f"Fish ASR failed: {type(error).__name__}"}
        return {"success": True,
                "text": (payload.get("text") or "").strip()}


def register(ctx):
    ctx.register_tts_provider(FishSpeechTTS())
    ctx.register_transcription_provider(FishTranscription())
