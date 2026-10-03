#!/usr/bin/env python3
"""Paid Fish speech synthesis and Hermes command-provider entry point."""

import argparse
import asyncio
import math
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

import httpx

try:
    from .language_voice import LanguageVoice
except ImportError:  # run as a top-level module (speech_server.py, CLI)
    from language_voice import LanguageVoice

API = "https://api.fish.audio/v1/tts"
MODEL = "s2.1-pro"
DEFAULT_REFERENCE_ID = "933563129e564b19a115bedd57b7406a"
CHUNK_CHARACTERS = 4000
MAX_TEXT_CHARACTERS = 64000
MAX_INPUT_BYTES = 256 * 1024
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MIN_AUDIO_BYTES = 100
MAX_API_KEY_BYTES = 4096


class SpeechError(Exception):
    """An intentionally transcript- and credential-free synthesis failure."""


def validate_reference_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ValueError("reference_id must contain exactly 32 hexadecimal characters")
    return value


def validate_model(value):
    if value != MODEL:
        raise ValueError("model must be s2.1-pro")
    return value


def validate_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a nonempty string")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError("text exceeds the 64000-character limit")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("text must contain valid Unicode") from None
    if encoded_size > MAX_INPUT_BYTES:
        raise ValueError("text exceeds the input byte limit")
    return text


def split_text(text):
    """Keep every character, preferring whitespace boundaries over cutting words."""
    validate_text(text)
    chunks = []
    offset = 0
    while len(text) - offset > CHUNK_CHARACTERS:
        end = offset + CHUNK_CHARACTERS
        # Keep boundaries in the latter half to avoid tiny leading chunks.
        for position in range(end - 1, offset + CHUNK_CHARACTERS // 2 - 1, -1):
            if text[position].isspace():
                end = position + 1
                break
        chunks.append(text[offset:end])
        offset = end
    chunks.append(text[offset:])
    return chunks


def valid_mp3(audio):
    return len(audio) >= MIN_AUDIO_BYTES and (
        audio.startswith(b"ID3")
        or (audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0 and audio[1] & 0x06 != 0)
    )


async def combine_mp3(chunks):
    """Decode and re-encode ordered chunks with real ffmpeg, never byte-concatenate."""
    with tempfile.TemporaryDirectory(prefix="hermes-speech-") as directory:
        root = Path(directory)
        for index, audio in enumerate(chunks):
            (root / f"chunk-{index}.mp3").write_bytes(audio)
        manifest = root / "chunks.ffconcat"
        manifest.write_text("ffconcat version 1.0\n" + "".join(
            f"file chunk-{index}.mp3\n" for index in range(len(chunks))
        ), encoding="utf-8")
        output = root / "speech.mp3"
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-xerror",
                "-protocol_whitelist", "file,pipe", "-f", "concat", "-safe", "1", "-err_detect", "explode",
                "-i", str(manifest), "-map", "0:a:0", "-c:a", "libmp3lame",
                "-b:a", "128k", "-ac", "1", "-ar", "44100",
                "-fs", str(MAX_AUDIO_BYTES + 1), str(output),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise SpeechError("ffmpeg is required to assemble long speech") from None
        try:
            await asyncio.wait_for(process.wait(), timeout=180)
        except (TimeoutError, asyncio.CancelledError) as error:
            if process.returncode is None:
                process.kill()
            await process.wait()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise SpeechError("Audio assembly timed out") from None
        if process.returncode != 0 or not output.exists():
            raise SpeechError("Audio assembly failed")
        if output.stat().st_size > MAX_AUDIO_BYTES:
            raise SpeechError("Assembled audio exceeds the audio size limit")
        audio = output.read_bytes()
        if not valid_mp3(audio):
            raise SpeechError("Audio assembly returned invalid MP3 audio")
        return audio


class FishSpeech:
    def __init__(self, api_key, client=None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("FISH_API_KEY must not be empty")
        self.api_key = api_key.strip()
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=15), follow_redirects=False, trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def chunk(self, text, reference_id, model, remaining_bytes, speed=None):
        body = {"text": text, "reference_id": reference_id, "format": "mp3"}
        if speed is not None:
            body["prosody"] = {"speed": speed}
        try:
            async with self.client.stream(
                "POST", API, json=body,
                headers={"Authorization": f"Bearer {self.api_key}", "model": model,
                         "Content-Type": "application/json", "Accept": "audio/mpeg",
                         "Accept-Encoding": "identity"},
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    # Do not consume or expose a provider error body: it may echo input or secrets.
                    raise SpeechError(f"Fish speech failed (HTTP {response.status_code})")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                    raise SpeechError("Fish speech returned a non-audio response")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise SpeechError("Fish speech returned unsupported encoded audio")
                audio = bytearray()
                async for part in response.aiter_bytes(chunk_size=64 * 1024):
                    if len(audio) + len(part) > remaining_bytes:
                        raise SpeechError("Fish speech exceeds the audio size limit")
                    audio.extend(part)
        except httpx.HTTPError:
            raise SpeechError("Fish speech could not be reached") from None
        if not valid_mp3(audio):
            raise SpeechError("Fish speech returned invalid MP3 audio")
        return bytes(audio)

    async def synthesize(self, text, reference_id=DEFAULT_REFERENCE_ID, model=MODEL, speed=None):
        validate_reference_id(reference_id)
        validate_model(model)
        if speed is not None and (
            isinstance(speed, bool) or not isinstance(speed, (int, float))
            or not math.isfinite(speed) or not 0.5 <= speed <= 2.0
        ):
            raise ValueError("speed must be a finite number from 0.5 to 2.0")
        parts = split_text(text)
        chunks = []
        size = 0
        for part in parts:
            audio = await self.chunk(part, reference_id, model, MAX_AUDIO_BYTES - size, speed)
            chunks.append(audio)
            size += len(audio)
        return chunks[0] if len(chunks) == 1 else await combine_mp3(chunks)


def command_arguments(argv=None, environment=None):
    environment = os.environ if environment is None else environment
    parser = argparse.ArgumentParser(description="Hermes paid Fish Audio command provider")
    parser.add_argument("--api-key-file", type=Path)
    parser.add_argument("--language-api-key-file", type=Path)
    parser.add_argument("input_path", type=Path)
    parser.add_argument("output_path", type=Path)
    parser.add_argument("voice", nargs="?", default="")
    parser.add_argument("model", nargs="?", default="")
    parser.add_argument("speed", nargs="?", default="")
    args = parser.parse_args(argv)
    args.voice = validate_reference_id(
        args.voice or environment.get("FISH_REFERENCE_ID", "") or DEFAULT_REFERENCE_ID
    )
    args.model = validate_model(args.model or environment.get("FISH_TTS_MODEL_HEADER", "") or MODEL)
    try:
        args.speed = float(args.speed) if args.speed else None
    except ValueError:
        raise ValueError("speed must be a finite number from 0.5 to 2.0") from None
    return args


def command_api_key(args, environment=None):
    """Use an explicitly configured private key file, otherwise inherited FISH_API_KEY."""
    if args.api_key_file is None:
        environment = os.environ if environment is None else environment
        return environment.get("FISH_API_KEY", "")
    return private_api_key(args.api_key_file)


def private_api_key(path):
    """Read a bounded, owned, private regular file without following symlinks or blocking on FIFOs."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise SpeechError("Fish speech credential file must be private and owned by this user")
            raw = source.read(MAX_API_KEY_BYTES + 1)
    except OSError:
        raise SpeechError("Fish speech credential file is unavailable") from None
    if len(raw) > MAX_API_KEY_BYTES:
        raise SpeechError("Fish speech credential file exceeds the credential size limit")
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise SpeechError("Fish speech credential file is invalid") from None
    if not key or "\n" in key or "\r" in key:
        raise SpeechError("Fish speech credential file is invalid")
    return key


def command_language_api_key(args, environment=None):
    """Unavailable optional Jev credentials abstain, never overriding an explicit file with env."""
    if args.language_api_key_file is None:
        environment = os.environ if environment is None else environment
        return environment.get("OPENROUTER_API_KEY", "")
    try:
        return private_api_key(args.language_api_key_file)
    except SpeechError:
        return ""


async def run_command(args, api_key):
    # Bound the file read before decoding. Do not strip or silently truncate the input.
    with args.input_path.open("rb") as source:
        raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("text exceeds the input byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("text must contain valid UTF-8") from None
    validate_text(text)
    speech = FishSpeech(api_key)
    language = LanguageVoice(command_language_api_key(args))
    try:
        reference_id, _ = await language.select(text, args.voice)
        audio = await speech.synthesize(text, reference_id, args.model, args.speed)
    finally:
        try:
            await language.close()
        finally:
            await speech.close()
    # Leave no partial output, even when synthesis/assembly or a later chunk fails.
    args.output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.output_path.parent, prefix=".fish-speech-", delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(audio)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(args.output_path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    try:
        args = command_arguments(argv)
        asyncio.run(run_command(args, command_api_key(args)))
    except (ValueError, SpeechError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Fish speech could not read input or write audio", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
