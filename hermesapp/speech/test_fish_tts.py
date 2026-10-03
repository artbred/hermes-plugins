import asyncio
import json
import shutil
import subprocess
from itertools import pairwise

import httpx
import pytest
from hermes_fish_speech import fish_tts
from hermes_fish_speech.fish_tts import (
    CHUNK_CHARACTERS,
    MODEL,
    FishSpeech,
    SpeechError,
    combine_mp3,
    split_text,
)

VOICE = "9a9cf47702da476aa4629e2506d4a857"
MOCK_MP3 = b"ID3" + b"\x00" * 197


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


async def test_synthesis_returns_audio_and_honors_speed():
    text = "  Synthetic speech.\n"
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, content=MOCK_MP3, headers={"content-type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
        audio = await FishSpeech("provider-secret", client).synthesize(text, VOICE, MODEL, 1.25)
    assert audio == MOCK_MP3
    assert len(calls) == 1
    body = json.loads(calls[0].content)
    assert body == {"text": text, "reference_id": VOICE, "format": "mp3", "prosody": {"speed": 1.25}}
    assert calls[0].headers["model"] == MODEL


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
