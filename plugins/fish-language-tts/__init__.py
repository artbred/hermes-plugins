"""Native Hermes TTS backend backed by the standalone Fish speech engine."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from agent.secret_scope import get_secret
from agent.tts_provider import TTSProvider

from .hermes_fish_speech.fish_tts import (
    DEFAULT_REFERENCE_ID,
    MODEL,
    FishSpeech,
    SpeechError,
    validate_model,
    validate_reference_id,
    validate_speed,
    validate_text,
)
from .hermes_fish_speech.language_voice import (
    RUSSIAN_REFERENCE_ID,
    LanguageVoice,
)


class FishLanguageTTSProvider(TTSProvider):
    def __init__(self, general_voice: str = DEFAULT_REFERENCE_ID):
        self.general_voice = validate_reference_id(general_voice)

    @property
    def name(self) -> str:
        return "fish-language"

    @property
    def display_name(self) -> str:
        return "Fish Audio · language-aware"

    def is_available(self) -> bool:
        try:
            key = get_secret("FISH_API_KEY")
        except RuntimeError:
            return False
        return isinstance(key, str) and bool(key.strip())

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Paid s2.1-pro; optional Jev language routing",
            "env_vars": [{
                "key": "FISH_API_KEY",
                "prompt": "Fish Audio API key",
                "url": "https://fish.audio/app/api-keys",
            }],
        }

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": MODEL, "display": "Fish s2.1-pro (paid)"}]

    def default_model(self) -> str:
        return MODEL

    def list_voices(self) -> list[dict[str, Any]]:
        return [
            {"id": "general", "display": "Configured general voice", "language": "multilingual"},
            {"id": RUSSIAN_REFERENCE_ID, "display": "Russian voice", "language": "ru"},
        ]

    def default_voice(self) -> str:
        return "general"

    async def _audio(self, text: str, general_voice: str, model: str, speed: float | None,
                     fish_key: str, language_key: str) -> bytes:
        speech = FishSpeech(fish_key)
        language = None
        try:
            language = LanguageVoice(language_key)
            reference_id, _ = await language.select(text, general_voice)
            return await speech.synthesize(text, reference_id, model, speed)
        finally:
            try:
                if language is not None:
                    await language.close()
            finally:
                await speech.close()

    def synthesize(self, text: str, output_path: str, *, voice: str | None = None,
                   model: str | None = None, speed: float | None = None,
                   format: str = "mp3", **extra: Any) -> str:
        validate_text(text)
        selected_model = validate_model(MODEL if model is None else model)
        general_voice = self.general_voice if voice is None or voice == "general" else voice
        validate_reference_id(general_voice)
        validate_speed(speed)
        # This backend emits MP3 only. The ABC requires returning the actual path
        # when a requested output format needs its closest supported equivalent.
        try:
            destination = Path(output_path).with_suffix(".mp3")
        except (TypeError, ValueError):
            raise ValueError("output_path must name an audio file") from None
        try:
            fish_key = get_secret("FISH_API_KEY")
            language_key = get_secret("OPENROUTER_API_KEY")
        except RuntimeError:
            raise SpeechError("Fish speech credentials are unavailable in this profile") from None
        if not isinstance(fish_key, str) or not fish_key.strip():
            raise SpeechError("FISH_API_KEY is required for Fish speech")
        language_key = language_key if isinstance(language_key, str) else ""
        try:
            audio = asyncio.run(self._audio(text, general_voice, selected_model, speed,
                                           fish_key, language_key))
        except (OSError, ValueError):
            raise SpeechError("Fish speech could not synthesize audio") from None
        # Synthesis and assembly finish before the old destination is touched.
        try:
            temporary = None
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".fish-speech-",
                                                 delete=False) as out:
                    temporary = Path(out.name)
                    out.write(audio)
                    out.flush()
                    os.fsync(out.fileno())
                temporary.replace(destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        except OSError:
            raise SpeechError("Fish speech could not write audio") from None
        return str(destination)


def register(ctx) -> None:
    general_voice = ctx.get_config("general_voice", DEFAULT_REFERENCE_ID)
    ctx.register_tts_provider(FishLanguageTTSProvider(general_voice))
