"""Narrow mobile speech endpoint; native chat, transcription and admin are untouched."""

import base64
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from fish_tts import (
    DEFAULT_REFERENCE_ID,
    MODEL,
    FishSpeech,
    SpeechError,
    validate_model,
    validate_reference_id,
    validate_text,
)

MAX_REQUEST_BYTES = 1024 * 1024


def environment_secret(name, credential):
    value = os.environ.get(name)
    if value is None:
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if directory:
            value = (Path(directory) / credential).read_text(encoding="utf-8")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


@dataclass(frozen=True)
class Config:
    api_key: str = field(repr=False)
    fish_key: str = field(repr=False)

    def __post_init__(self):
        if not self.api_key or not self.fish_key:
            raise ValueError("API_SERVER_KEY and FISH_API_KEY must not be empty")

    @classmethod
    def from_environment(cls):
        return cls(environment_secret("API_SERVER_KEY", "api-key"),
                   environment_secret("FISH_API_KEY", "fish-key"))


def validation_failure(location, message, kind="value_error"):
    return web.json_response({"detail": [{"type": kind, "loc": ["body", *location],
                                          "msg": message}]}, status=422)


def reject_duplicate_fields(pairs):
    body = {}
    for name, value in pairs:
        if name in body:
            raise ValueError("Duplicate JSON field")
        body[name] = value
    return body


def create_app(config, speech=None):
    speech = speech or FishSpeech(config.fish_key)

    @web.middleware
    async def authenticate(request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(),
                                   f"Bearer {config.api_key}".encode()):
            return web.json_response({"detail": "Unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": "Bearer"})
        return await handler(request)

    app = web.Application(middlewares=[authenticate], client_max_size=MAX_REQUEST_BYTES)

    async def speak(request):
        if request.content_type != "application/json":
            return web.json_response({"detail": "Expected application/json"}, status=415)
        try:
            raw = await request.read()
            body = json.loads(raw, object_pairs_hook=reject_duplicate_fields)
        except (ValueError, UnicodeDecodeError):
            return validation_failure([], "Invalid JSON object", "json_invalid")
        if not isinstance(body, dict):
            return validation_failure([], "Expected a JSON object", "dict_type")
        if "text" not in body:
            # Preserve the native FastAPI readiness probe without performing synthesis.
            return validation_failure(["text"], "Field required", "missing")
        if set(body) - {"text", "reference_id", "model"}:
            return validation_failure([], "Unknown request fields are not permitted", "extra_forbidden")
        text = body["text"]
        reference_id = body.get("reference_id", DEFAULT_REFERENCE_ID)
        model = body.get("model", MODEL)
        for name, value, validate in (
            ("text", text, validate_text), ("reference_id", reference_id, validate_reference_id),
            ("model", model, validate_model),
        ):
            try:
                validate(value)
            except ValueError as error:
                return validation_failure([name], str(error))
        try:
            audio = await speech.synthesize(text, reference_id, model)
        except SpeechError as error:
            return web.json_response({"detail": str(error)}, status=502)
        except OSError:
            return web.json_response({"detail": "Speech audio processing failed"}, status=502)
        return web.json_response({
            "ok": True, "data_url": "data:audio/mpeg;base64," + base64.b64encode(audio).decode("ascii"),
            "mime_type": "audio/mpeg", "provider": "fish", "reference_id": reference_id, "model": model,
        })

    async def lifecycle(_app):
        yield
        await speech.close()

    app.router.add_post("/api/audio/speak", speak)
    app.cleanup_ctx.append(lifecycle)
    return app


def main():
    # Never enable access logging or httpx request tracing: input and credentials stay server-only.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        app = create_app(Config.from_environment())
    except (ValueError, OSError):
        raise SystemExit("Speech service credentials are missing or unreadable") from None
    web.run_app(app, host=os.environ.get("SPEECH_HOST", "127.0.0.1"),
                port=int(os.environ.get("SPEECH_PORT", "9124")), access_log=None)


if __name__ == "__main__":
    main()
