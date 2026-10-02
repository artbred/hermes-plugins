import base64
import json

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

import fish_tts
from fish_tts import DEFAULT_REFERENCE_ID, MODEL, FishSpeech
from speech_server import MAX_REQUEST_BYTES, Config, create_app

MOCK_MP3 = b"ID3" + b"\x00" * 197
AUTH = {"Authorization": "Bearer mobile-secret"}
CUSTOM_VOICE = "abcdef0123456789abcdef0123456789"


@pytest.fixture
async def setup():
    calls = []
    state = {"status": 200, "audio": MOCK_MP3, "content_type": "audio/mpeg", "error": None}

    async def provider(request):
        calls.append(request)
        if state.get("fail_after") == len(calls):
            state.update(status=503, audio=b"provider-secret echoed transcript")
        if state["error"]:
            raise state["error"]("Synthetic provider boundary failure", request=request)
        return httpx.Response(state["status"], content=state["audio"],
                              headers={"Content-Type": state["content_type"],
                                       "Location": "https://untrusted.example/tts"})

    provider_client = httpx.AsyncClient(transport=httpx.MockTransport(provider), follow_redirects=True)
    speech = FishSpeech("provider-secret", provider_client)
    client = TestClient(TestServer(create_app(Config("mobile-secret", "provider-secret"), speech)))
    await client.start_server()
    yield client, state, calls
    await client.close()
    assert provider_client.is_closed


async def test_authentication_precedes_body_and_never_synthesizes(setup):
    client, _, calls = setup
    for headers in ({}, {"Authorization": "Bearer provider-secret"},
                    {"Authorization": "Bearer mobile-secret-other"},
                    {"X-Hermes-Session-Token": "dashboard-secret"}):
        response = await client.post("/api/audio/speak", json={"text": "Synthetic speech."}, headers=headers)
        assert response.status == 401
        assert await response.json() == {"detail": "Unauthorized"}
    assert calls == []


async def test_defaults_and_server_only_fixed_provider_request(setup):
    client, _, calls = setup
    headers = {**AUTH, "Cookie": "private=value", "X-Hermes-Session-Token": "dashboard-secret",
               "model": "s2.1-pro-free"}
    response = await client.post("/api/audio/speak", json={"text": "Synthetic speech."}, headers=headers)
    assert response.status == 200
    assert await response.json() == {
        "ok": True, "data_url": "data:audio/mpeg;base64," + base64.b64encode(MOCK_MP3).decode(),
        "mime_type": "audio/mpeg", "provider": "fish", "reference_id": DEFAULT_REFERENCE_ID,
        "model": MODEL,
    }
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://api.fish.audio/v1/tts"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer provider-secret"
    assert request.headers["model"] == "s2.1-pro"
    assert "cookie" not in request.headers
    assert "x-hermes-session-token" not in request.headers
    assert json.loads(request.content) == {
        "text": "Synthetic speech.", "reference_id": DEFAULT_REFERENCE_ID, "format": "mp3",
    }


@pytest.mark.parametrize("voice", [CUSTOM_VOICE, CUSTOM_VOICE.upper()])
async def test_selected_voice_and_paid_model_identity(setup, voice):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers=AUTH,
                                 json={"text": "Selected voice.", "reference_id": voice, "model": MODEL})
    assert response.status == 200
    body = await response.json()
    assert body["reference_id"] == voice
    assert body["model"] == MODEL
    assert json.loads(calls[0].content)["reference_id"] == voice
    assert calls[0].headers["model"] == MODEL


async def test_missing_text_preserves_native_readiness_shape(setup):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers=AUTH, json={})
    assert response.status == 422
    assert await response.json() == {"detail": [
        {"type": "missing", "loc": ["body", "text"], "msg": "Field required"},
    ]}
    assert calls == []


@pytest.mark.parametrize("field,value", [
    ("text", None), ("text", True), ("text", []), ("text", ""), ("text", " \n\t "),
    ("text", "\ud800"), ("text", "a" * 64001),
    ("reference_id", None), ("reference_id", 12), ("reference_id", ""),
    ("reference_id", "a" * 31), ("reference_id", "g" * 32),
    ("model", None), ("model", []), ("model", ""), ("model", "s2.1-pro-free"),
    ("model", "s2-pro"), ("model", "unknown"),
])
async def test_invalid_body_values_never_reach_paid_provider(setup, field, value):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers=AUTH,
                                 json={"text": "Synthetic speech.", field: value})
    assert response.status == 422
    assert (await response.json())["detail"][0]["loc"] == ["body", field]
    assert calls == []


@pytest.mark.parametrize("body", [
    {"text": "Synthetic speech.", "voice": CUSTOM_VOICE},
    {"text": "Synthetic speech.", "provider": "other"},
    {"text": "Synthetic speech.", "api_key": "untrusted"},
    {"text": "Synthetic speech.", "speed": 1.5},
])
async def test_unknown_fields_are_not_silently_ignored(setup, body):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers=AUTH, json=body)
    assert response.status == 422
    assert (await response.json())["detail"][0]["type"] == "extra_forbidden"
    assert calls == []


@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"text":"one","text":"two"}'])
async def test_malformed_json_is_rejected(setup, raw):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers={**AUTH, "Content-Type": "application/json"},
                                 data=raw)
    assert response.status == 422
    assert calls == []


async def test_request_body_is_bounded(setup):
    client, _, calls = setup
    response = await client.post("/api/audio/speak", headers={**AUTH, "Content-Type": "application/json"},
                                 data=b" " * (MAX_REQUEST_BYTES + 1))
    assert response.status == 413
    assert calls == []


@pytest.mark.parametrize("path", ["/api/audio/transcribe", "/api/config", "/api/audio/voices"])
async def test_other_endpoints_are_not_exposed(setup, path):
    client, _, calls = setup
    response = await client.post(path, headers=AUTH, json={})
    assert response.status == 404
    assert calls == []


async def test_only_post_json_is_accepted(setup):
    client, _, calls = setup
    response = await client.get("/api/audio/speak", headers=AUTH)
    assert response.status == 405
    response = await client.post("/api/audio/speak", data="Synthetic speech.", headers=AUTH)
    assert response.status == 415
    assert calls == []


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500, 503])
async def test_upstream_errors_and_redirects_are_visible_but_not_leaked(setup, status, caplog):
    client, state, calls = setup
    state.update(status=status, audio=b"provider-secret private transcript")
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "Private transcript."})
    assert response.status == 502
    assert await response.json() == {"detail": f"Fish speech failed (HTTP {status})"}
    assert len(calls) == 1
    assert "provider-secret" not in caplog.text
    assert "Private transcript" not in caplog.text


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError])
async def test_network_failures_are_sanitized(setup, error):
    client, state, calls = setup
    state["error"] = error
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "Synthetic speech."})
    assert response.status == 502
    assert await response.json() == {"detail": "Fish speech could not be reached"}
    assert len(calls) == 1


@pytest.mark.parametrize("audio,content_type", [
    (b"", "audio/mpeg"), (b"ID3", "audio/mpeg"), (b"not an mp3" * 30, "audio/mpeg"),
    (MOCK_MP3, "application/json"),
])
async def test_empty_malformed_and_non_audio_responses_fail(setup, audio, content_type):
    client, state, _ = setup
    state.update(audio=audio, content_type=content_type)
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "Synthetic speech."})
    assert response.status == 502


async def test_audio_response_is_bounded(setup, monkeypatch):
    client, state, _ = setup
    monkeypatch.setattr(fish_tts, "MAX_AUDIO_BYTES", 250)
    state["audio"] = MOCK_MP3 + b"\x00" * 51
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "Synthetic speech."})
    assert response.status == 502
    assert (await response.json())["detail"] == "Fish speech exceeds the audio size limit"


async def test_longform_preserves_every_character_in_order(setup, monkeypatch):
    client, _, calls = setup
    text = "  Начало.\n" + ("A long sentence with spaces.\n" * 350) + "Конец.  "
    assembled = []

    async def combine(chunks):
        assembled.append(chunks)
        return MOCK_MP3

    monkeypatch.setattr(fish_tts, "combine_mp3", combine)
    response = await client.post("/api/audio/speak", headers=AUTH,
                                 json={"text": text, "reference_id": CUSTOM_VOICE, "model": MODEL})
    assert response.status == 200
    bodies = [json.loads(request.content) for request in calls]
    assert len(bodies) > 1
    assert "".join(body["text"] for body in bodies) == text
    assert all(0 < len(body["text"]) <= 4000 for body in bodies)
    assert all(body["reference_id"] == CUSTOM_VOICE for body in bodies)
    assert all(request.headers["model"] == MODEL for request in calls)
    assert assembled == [[MOCK_MP3] * len(bodies)]


async def test_later_chunk_failure_returns_no_partial_audio(setup, monkeypatch):
    client, state, calls = setup
    combined = []
    state["fail_after"] = 2

    async def combine(chunks):
        combined.append(chunks)
        return MOCK_MP3

    monkeypatch.setattr(fish_tts, "combine_mp3", combine)
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "a" * 8001})
    assert response.status == 502
    assert "data_url" not in await response.json()
    assert len(calls) == 2
    assert combined == []


async def test_audio_cap_is_shared_across_all_chunks(setup, monkeypatch):
    client, _, calls = setup
    monkeypatch.setattr(fish_tts, "MAX_AUDIO_BYTES", 350)
    response = await client.post("/api/audio/speak", headers=AUTH, json={"text": "a" * 8001})
    assert response.status == 502
    assert len(calls) == 2
