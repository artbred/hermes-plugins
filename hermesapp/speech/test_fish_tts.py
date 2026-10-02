import asyncio
import json
import shutil
import subprocess
from itertools import pairwise

import httpx
import pytest

import fish_tts
from fish_tts import (
    CHUNK_CHARACTERS,
    DEFAULT_REFERENCE_ID,
    MAX_API_KEY_BYTES,
    MAX_INPUT_BYTES,
    MODEL,
    FishSpeech,
    SpeechError,
    combine_mp3,
    command_api_key,
    command_arguments,
    command_language_api_key,
    main,
    run_command,
    split_text,
)
from language_voice import (
    RUSSIAN_REFERENCE_ID,
    LanguageVoice,
)
from speech_server import Config

VOICE = "9a9cf47702da476aa4629e2506d4a857"
MOCK_MP3 = b"ID3" + b"\x00" * 197


@pytest.fixture(autouse=True)
def no_live_advisor_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.mark.parametrize("text", [
    "a" * 3999, "a" * 4000, "a" * 4001, "a" * 8000, "a" * 8001,
    "word " * 12800, "\n" + "Привет, мир. " * 700 + "  ", "a" * 64000,
])
def test_chunk_boundaries_preserve_all_input(text):
    chunks = split_text(text)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= CHUNK_CHARACTERS for chunk in chunks)
    if len(text) <= CHUNK_CHARACTERS:
        assert chunks == [text]
    else:
        assert len(chunks) >= 2


def test_default_command_identity_and_empty_native_placeholders():
    for argv in (["input.txt", "output.mp3"], ["input.txt", "output.mp3", "", "", ""]):
        args = command_arguments(argv, {})
        assert args.voice == DEFAULT_REFERENCE_ID
        assert args.model == MODEL
        assert args.speed is None


def test_command_honors_explicit_arguments_over_environment():
    args = command_arguments(["input.txt", "output.mp3", VOICE, MODEL, "1.25"],
                             {"FISH_REFERENCE_ID": DEFAULT_REFERENCE_ID,
                              "FISH_TTS_MODEL_HEADER": "s2.1-pro-free"})
    assert args.voice == VOICE
    assert args.model == MODEL
    assert args.speed == 1.25


def test_command_honors_valid_inherited_voice():
    args = command_arguments(["input.txt", "output.mp3"], {"FISH_REFERENCE_ID": VOICE})
    assert args.voice == VOICE
    assert args.model == MODEL


@pytest.mark.parametrize("model", ["s2.1-pro-free", "s2-pro", "unknown"])
def test_command_rejects_explicit_and_inherited_nonpaid_models(model):
    with pytest.raises(ValueError, match="model must be s2.1-pro"):
        command_arguments(["input.txt", "output.mp3", VOICE, model], {})
    with pytest.raises(ValueError, match="model must be s2.1-pro"):
        command_arguments(["input.txt", "output.mp3"], {"FISH_TTS_MODEL_HEADER": model})


@pytest.mark.parametrize("voice", ["alloy", "", "0" * 31, "z" * 32])
def test_command_invalid_voice_is_never_ignored(voice):
    if not voice:
        voice = " "
    with pytest.raises(ValueError, match="32 hexadecimal"):
        command_arguments(["input.txt", "output.mp3", voice, MODEL], {})


@pytest.mark.parametrize("speed", [True, 0.25, 4.0, float("nan"), float("inf"), "fast"])
async def test_invalid_speed_never_reaches_provider(speed):
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        with pytest.raises(ValueError, match="speed must be"):
            await FishSpeech("provider-secret", client).synthesize("Synthetic speech.", speed=speed)
    assert calls == []


async def test_command_writes_real_synthesis_result_and_honors_speed(tmp_path, monkeypatch):
    text = "  Synthetic speech.\n"
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text(text, encoding="utf-8")
    output_path.write_bytes(b"previous audio")
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    speech = FishSpeech("provider-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    keys = []

    def make_speech(key):
        keys.append(key)
        return speech

    monkeypatch.setattr(fish_tts, "FishSpeech", make_speech)
    args = command_arguments([str(input_path), str(output_path), VOICE, MODEL, "1.25"], {})
    await run_command(args, "provider-secret")
    assert keys == ["provider-secret"]
    assert output_path.read_bytes() == MOCK_MP3
    body = json.loads(calls[0].content)
    assert body == {"text": text, "reference_id": VOICE, "format": "mp3", "prosody": {"speed": 1.25}}
    assert calls[0].headers["model"] == MODEL
    assert not list(tmp_path.glob(".fish-speech-*"))
    assert speech.client.is_closed


async def test_command_failure_does_not_replace_output_or_leak_provider_body(tmp_path, monkeypatch):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text("Synthetic private transcript.", encoding="utf-8")
    output_path.write_bytes(b"previous audio")

    async def provider(request):
        return httpx.Response(402, content=b"provider-secret Synthetic private transcript.")

    speech = FishSpeech("provider-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(fish_tts, "FishSpeech", lambda _key: speech)
    args = command_arguments([str(input_path), str(output_path)], {})
    with pytest.raises(SpeechError, match=r"^Fish speech failed \(HTTP 402\)$"):
        await run_command(args, "provider-secret")
    assert output_path.read_bytes() == b"previous audio"
    assert not list(tmp_path.glob(".fish-speech-*"))
    assert speech.client.is_closed


async def test_command_input_file_read_is_bounded(tmp_path):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_bytes(b"a" * (MAX_INPUT_BYTES + 1))
    args = command_arguments([str(input_path), str(output_path)], {})
    with pytest.raises(ValueError, match="input byte limit"):
        await run_command(args, "provider-secret")
    assert not output_path.exists()


def test_main_reports_safe_failure_without_traceback(tmp_path, monkeypatch, capsys):
    input_path = tmp_path / "input.txt"
    input_path.write_text("Synthetic speech.")
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    monkeypatch.delenv("FISH_REFERENCE_ID", raising=False)
    monkeypatch.delenv("FISH_TTS_MODEL_HEADER", raising=False)
    assert main([str(input_path), str(tmp_path / "output.mp3")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FISH_API_KEY must not be empty\n"


def test_command_keeps_inherited_credential_support():
    args = command_arguments(["input.txt", "output.mp3"], {})
    assert command_api_key(args, {"FISH_API_KEY": "inherited-test-secret"}) == "inherited-test-secret"


def test_private_configured_key_file_is_authoritative(tmp_path):
    key_file = tmp_path / "fish-key"
    key_file.write_text("file-test-secret\n")
    key_file.chmod(0o600)
    args = command_arguments(["--api-key-file", str(key_file), "input.txt", "output.mp3"], {})
    assert command_api_key(args, {"FISH_API_KEY": "stale-test-secret"}) == "file-test-secret"


def test_configured_missing_key_file_never_falls_back_to_environment(tmp_path):
    args = command_arguments(
        ["--api-key-file", str(tmp_path / "missing"), "input.txt", "output.mp3"], {}
    )
    with pytest.raises(SpeechError, match="credential file is unavailable"):
        command_api_key(args, {"FISH_API_KEY": "inherited-test-secret"})


@pytest.mark.parametrize("invalid_kind", ["public", "symlink", "foreign_owner", "oversized"])
def test_configured_key_file_must_be_private_owned_regular_and_bounded(tmp_path, monkeypatch, invalid_kind):
    key_file = tmp_path / "fish-key"
    key_file.write_text("file-test-secret")
    key_file.chmod(0o600)
    if invalid_kind == "public":
        key_file.chmod(0o644)
    elif invalid_kind == "symlink":
        linked = tmp_path / "link"
        linked.symlink_to(key_file)
        key_file = linked
    elif invalid_kind == "foreign_owner":
        monkeypatch.setattr(fish_tts.os, "geteuid", lambda: key_file.stat().st_uid + 1)
    else:
        key_file.write_text("x" * (MAX_API_KEY_BYTES + 1))
    args = command_arguments(["--api-key-file", str(key_file), "input.txt", "output.mp3"], {})
    with pytest.raises(SpeechError):
        command_api_key(args, {})


@pytest.mark.parametrize("contents", [b"", b"\xff", b"first-secret\nsecond-secret"])
def test_invalid_file_credentials_are_sanitized(tmp_path, contents):
    key_file = tmp_path / "fish-key"
    key_file.write_bytes(contents)
    key_file.chmod(0o600)
    args = command_arguments(["--api-key-file", str(key_file), "input.txt", "output.mp3"], {})
    with pytest.raises(SpeechError, match=r"^Fish speech credential file is invalid$"):
        command_api_key(args, {})


def test_native_command_works_without_parent_environment_key(tmp_path, monkeypatch, capsys):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    key_file = tmp_path / "fish-key"
    input_path.write_text("Synthetic credential delivery.")
    key_file.write_text("file-test-secret\n")
    key_file.chmod(0o600)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    monkeypatch.setattr(
        fish_tts, "FishSpeech",
        lambda key: FishSpeech(key, httpx.AsyncClient(transport=httpx.MockTransport(provider))),
    )
    assert main(["--api-key-file", str(key_file), str(input_path), str(output_path), VOICE, MODEL]) == 0
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer file-test-secret"
    assert calls[0].headers["model"] == MODEL
    assert json.loads(calls[0].content)["reference_id"] == VOICE
    assert output_path.read_bytes() == MOCK_MP3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""



def test_config_reads_only_named_systemd_credentials(tmp_path, monkeypatch):
    (tmp_path / "api-key").write_text("mobile-secret\n")
    (tmp_path / "fish-key").write_text("provider-secret\n")
    (tmp_path / "jev-key").write_text("advisor-secret\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    config = Config.from_environment()
    assert config.api_key == "mobile-secret"
    assert config.fish_key == "provider-secret"
    assert config.jev_key == "advisor-secret"
    assert "mobile-secret" not in repr(config)
    assert "provider-secret" not in repr(config)
    assert "advisor-secret" not in repr(config)


def test_existing_environment_credentials_are_supported(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "mobile-secret")
    monkeypatch.setenv("FISH_API_KEY", "provider-secret")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert Config.from_environment() == Config("mobile-secret", "provider-secret")


async def test_multichunk_assembly_uses_real_ffmpeg_and_includes_both_chunks(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "ffmpeg and ffprobe are required for offline audio assembly tests"
    chunks = []
    for index, frequency in enumerate((440, 880)):
        path = tmp_path / f"tone-{index}.mp3"
        await asyncio.to_thread(subprocess.run, [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=0.2",
                        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        chunks.append(path.read_bytes())
    audio = await combine_mp3(chunks)
    assert fish_tts.valid_mp3(audio)
    assert audio != b"".join(chunks)
    assembled = tmp_path / "assembled.mp3"
    assembled.write_bytes(audio)
    result = await asyncio.to_thread(subprocess.run, [ffprobe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(assembled)],
                            check=True, capture_output=True, text=True)
    assert 0.35 <= float(result.stdout) <= 0.65
    # Decode and inspect the two tones to verify chunk ordering, not just total duration.
    result = await asyncio.to_thread(subprocess.run, [ffmpeg, "-nostdin", "-loglevel", "error", "-i", str(assembled),
                                    "-f", "s16le", "-ac", "1", "-ar", "44100", "pipe:1"],
                                    check=True, capture_output=True)
    decoded = result.stdout
    import array
    samples = array.array("h", decoded)

    def zero_crossings(start, end):
        segment = samples[int(start * 44100):int(end * 44100)]
        return sum((left < 0) != (right < 0) for left, right in pairwise(segment))

    first, second = zero_crossings(0.05, 0.15), zero_crossings(0.27, 0.37)
    assert 75 <= first <= 100
    assert 155 <= second <= 195


async def test_missing_ffmpeg_is_a_visible_failure(monkeypatch):
    async def missing(*_args, **_kwargs):
        raise FileNotFoundError("not installed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    with pytest.raises(SpeechError, match="ffmpeg is required"):
        await combine_mp3([MOCK_MP3, MOCK_MP3])


async def test_ffmpeg_failure_has_no_byte_concat_fallback(monkeypatch):
    class FailedProcess:
        returncode = 1

        async def wait(self):
            return 1

    async def failed(*_args, **_kwargs):
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed)
    with pytest.raises(SpeechError, match="Audio assembly failed"):
        await combine_mp3([MOCK_MP3, MOCK_MP3])


def test_language_file_is_authoritative_and_optional_environment_is_supported(tmp_path):
    key_file = tmp_path / "jev-key"
    key_file.write_text("advisor-file-secret\n")
    key_file.chmod(0o600)
    args = command_arguments(["--language-api-key-file", str(key_file), "input.txt", "output.mp3"], {})
    assert command_language_api_key(args, {"OPENROUTER_API_KEY": "stale-advisor-secret"}) == "advisor-file-secret"
    args = command_arguments(["input.txt", "output.mp3"], {})
    assert command_language_api_key(args, {"OPENROUTER_API_KEY": "inherited-advisor-secret"}) == "inherited-advisor-secret"
    assert command_language_api_key(args, {}) == ""


@pytest.mark.parametrize("invalid_kind", [
    "missing", "public", "symlink", "foreign_owner", "oversized", "directory", "fifo", "empty", "invalid_utf8",
    "multiline",
])
def test_optional_language_file_abstains_if_unavailable_or_not_private(tmp_path, monkeypatch, invalid_kind):
    key_file = tmp_path / "jev-key"
    key_file.write_text("advisor-file-secret")
    key_file.chmod(0o600)
    if invalid_kind == "missing":
        key_file.unlink()
    elif invalid_kind == "public":
        key_file.chmod(0o644)
    elif invalid_kind == "symlink":
        linked = tmp_path / "link"
        linked.symlink_to(key_file)
        key_file = linked
    elif invalid_kind == "foreign_owner":
        monkeypatch.setattr(fish_tts.os, "geteuid", lambda: key_file.stat().st_uid + 1)
    elif invalid_kind == "oversized":
        key_file.write_text("x" * (MAX_API_KEY_BYTES + 1))
    elif invalid_kind == "directory":
        key_file.unlink()
        key_file.mkdir()
    elif invalid_kind == "fifo":
        key_file.unlink()
        fish_tts.os.mkfifo(key_file, 0o600)
    elif invalid_kind == "empty":
        key_file.write_bytes(b"")
    elif invalid_kind == "invalid_utf8":
        key_file.write_bytes(b"\xff")
    else:
        key_file.write_text("advisor-first-secret\nadvisor-second-secret")
    args = command_arguments(["--language-api-key-file", str(key_file), "input.txt", "output.mp3"], {})
    assert command_language_api_key(args, {"OPENROUTER_API_KEY": "must-not-replace-file"}) == ""


def test_config_optional_named_jev_credential_never_breaks_existing_synthesis(tmp_path, monkeypatch):
    (tmp_path / "api-key").write_text("mobile-secret")
    (tmp_path / "fish-key").write_text("provider-secret")
    (tmp_path / "openrouter-key").write_text("wrong-named-secret")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    assert Config.from_environment() == Config("mobile-secret", "provider-secret")
    (tmp_path / "jev-key").write_bytes(b"\xff")
    assert Config.from_environment() == Config("mobile-secret", "provider-secret")


def test_config_openrouter_environment_overrides_named_credential_and_stays_private(tmp_path, monkeypatch):
    (tmp_path / "jev-key").write_text("stale-advisor-secret")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("API_SERVER_KEY", "mobile-secret")
    monkeypatch.setenv("FISH_API_KEY", "provider-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "advisor-env-secret")
    config = Config.from_environment()
    assert config.jev_key == "advisor-env-secret"
    assert "advisor-env-secret" not in repr(config)


@pytest.mark.parametrize("choice,expected", [
    ("russian", RUSSIAN_REFERENCE_ID), ("english", VOICE), ("other", VOICE),
    ("uncertain", VOICE), ("invalid", VOICE), ("transport", VOICE),
])
async def test_native_command_classifies_once_and_uses_routed_voice_for_every_chunk(
    tmp_path, monkeypatch, choice, expected,
):
    text = "  Это русский ответ с code_identifier и https://example.com.\n" * 180
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text(text, encoding="utf-8")
    key_file = tmp_path / "jev-key"
    key_file.write_text("advisor-file-secret")
    key_file.chmod(0o600)
    fish_calls, language_calls = [], []

    async def classify(request):
        language_calls.append(request)
        if choice == "transport":
            raise httpx.ConnectError("Private classifier error", request=request)
        selected = choice if choice in {"english", "russian", "other"} else "russian"
        probabilities = {
            label: 0.98 if label == selected else 0.01 for label in ("english", "russian", "other")
        }
        if choice == "invalid":
            probabilities = {"russian": 1}
        return httpx.Response(200, json={"answers": {"language": {
            "type": "choice", "choice": selected, "confidence": 0.89 if choice == "uncertain" else 0.99,
            "probabilities": probabilities,
        }}})

    async def provider(request):
        fish_calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    language_keys = []
    language_client = httpx.AsyncClient(transport=httpx.MockTransport(classify))

    def make_language(key):
        language_keys.append(key)
        return LanguageVoice(key, language_client)

    speech = FishSpeech("provider-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))

    async def combine(_chunks):
        return MOCK_MP3

    monkeypatch.setattr(fish_tts, "LanguageVoice", make_language)
    monkeypatch.setattr(fish_tts, "FishSpeech", lambda _key: speech)
    monkeypatch.setattr(fish_tts, "combine_mp3", combine)
    args = command_arguments(["--language-api-key-file", str(key_file), str(input_path), str(output_path),
                              VOICE, MODEL, "1.25"], {})
    await run_command(args, "provider-secret")
    assert language_keys == ["advisor-file-secret"]
    assert len(language_calls) == 1
    assert json.loads(language_calls[0].content)["state"] == {"text": text}
    assert language_calls[0].headers["Authorization"] == "Bearer advisor-file-secret"
    assert len(fish_calls) > 1
    bodies = [json.loads(request.content) for request in fish_calls]
    assert "".join(body["text"] for body in bodies) == text
    assert all(body["reference_id"] == expected for body in bodies)
    assert all(body["prosody"] == {"speed": 1.25} for body in bodies)
    assert all(len(body["text"]) <= CHUNK_CHARACTERS for body in bodies)
    assert all(request.headers["model"] == MODEL for request in fish_calls)
    assert all(request.headers["Authorization"] == "Bearer provider-secret" for request in fish_calls)
    assert language_client.is_closed
    assert speech.client.is_closed
    assert output_path.read_bytes() == MOCK_MP3


async def test_native_missing_language_key_synthesizes_general_without_classification(tmp_path, monkeypatch):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text("Это русский ответ.", encoding="utf-8")
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    language_calls = []

    async def classify(request):
        language_calls.append(request)
        raise AssertionError("Missing key must not classify")

    language_client = httpx.AsyncClient(transport=httpx.MockTransport(classify))
    speech = FishSpeech("provider-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    monkeypatch.setattr(fish_tts, "LanguageVoice", lambda key: LanguageVoice(key, language_client))
    monkeypatch.setattr(fish_tts, "FishSpeech", lambda _key: speech)
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-not-replace-configured-file")
    args = command_arguments(["--language-api-key-file", str(tmp_path / "missing-key"),
                              str(input_path), str(output_path), VOICE, MODEL], {})
    await run_command(args, "provider-secret")
    assert language_calls == []
    assert json.loads(calls[0].content)["reference_id"] == VOICE
    assert calls[0].headers["model"] == MODEL
    assert language_client.is_closed
    assert speech.client.is_closed


async def test_native_cancellation_closes_both_clients_and_never_synthesizes(tmp_path, monkeypatch):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text("Synthetic reply.")
    started = asyncio.Event()
    fish_calls = []

    async def classify(_request):
        started.set()
        await asyncio.Future()

    async def provider(request):
        fish_calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    language_client = httpx.AsyncClient(transport=httpx.MockTransport(classify))
    speech = FishSpeech("provider-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "advisor-secret")
    monkeypatch.setattr(fish_tts, "LanguageVoice", lambda key: LanguageVoice(key, language_client))
    monkeypatch.setattr(fish_tts, "FishSpeech", lambda _key: speech)
    args = command_arguments([str(input_path), str(output_path)], {})
    task = asyncio.create_task(run_command(args, "provider-secret"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert language_client.is_closed
    assert speech.client.is_closed
    assert fish_calls == []
    assert not output_path.exists()


def test_native_main_routes_russian_with_both_private_files_without_parent_keys(tmp_path, monkeypatch, capsys):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_text("Это русский ответ.", encoding="utf-8")
    fish_key, jev_key = tmp_path / "fish-key", tmp_path / "jev-key"
    fish_key.write_text("fish-file-secret")
    jev_key.write_text("advisor-file-secret")
    fish_key.chmod(0o600)
    jev_key.chmod(0o600)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    calls, clients = [], []

    async def provider(request):
        calls.append(request)
        if str(request.url).startswith("https://openrouter.ai/"):
            return httpx.Response(200, json={"answers": {"language": {
                "type": "choice", "choice": "russian", "confidence": 0.99,
                "probabilities": {"english": 0.01, "russian": 0.98, "other": 0.01},
            }}})
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    def client():
        transport = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        clients.append(transport)
        return transport

    monkeypatch.setattr(fish_tts, "FishSpeech", lambda key: FishSpeech(key, client()))
    monkeypatch.setattr(fish_tts, "LanguageVoice", lambda key: LanguageVoice(key, client()))
    assert main(["--api-key-file", str(fish_key), "--language-api-key-file", str(jev_key),
                 str(input_path), str(output_path), VOICE, MODEL]) == 0
    assert len(calls) == 2
    assert calls[0].headers["Authorization"] == "Bearer advisor-file-secret"
    assert calls[1].headers["Authorization"] == "Bearer fish-file-secret"
    assert calls[1].headers["model"] == MODEL
    assert json.loads(calls[1].content)["reference_id"] == RUSSIAN_REFERENCE_ID
    assert output_path.read_bytes() == MOCK_MP3
    assert all(transport.is_closed for transport in clients)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("raw", [b"", b" \n\t ", b"\xff", b"a" * 64001])
async def test_native_invalid_text_never_creates_classifier_or_fish_clients(tmp_path, monkeypatch, raw):
    input_path, output_path = tmp_path / "input.txt", tmp_path / "output.mp3"
    input_path.write_bytes(raw)

    def forbidden(_key):
        raise AssertionError("Invalid text must fail before opening provider clients")

    monkeypatch.setattr(fish_tts, "FishSpeech", forbidden)
    monkeypatch.setattr(fish_tts, "LanguageVoice", forbidden)
    args = command_arguments([str(input_path), str(output_path)], {})
    with pytest.raises(ValueError):
        await run_command(args, "provider-secret")
    assert not output_path.exists()
