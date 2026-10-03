import asyncio

import httpx
import pytest
from hermes_fish_speech import language_voice
from hermes_fish_speech.fish_tts import DEFAULT_REFERENCE_ID
from hermes_fish_speech.language_voice import (
    MAX_RESPONSE_BYTES,
    RUSSIAN_REFERENCE_ID,
    LanguageVoice,
    select_voice,
)

GENERAL = "abcdef0123456789abcdef0123456789"


def judgment(choice="russian", confidence=0.99, probability=0.98):
    probabilities = {label: (1 - probability) / 2 for label in ("english", "russian", "other")}
    probabilities[choice] = probability
    return {"answers": {"language": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": probabilities,
    }}}


@pytest.mark.parametrize("general", [DEFAULT_REFERENCE_ID, GENERAL])
@pytest.mark.parametrize("choice,expected", [
    ("russian", RUSSIAN_REFERENCE_ID), ("english", None), ("other", None),
])
def test_only_russian_overrides_the_current_general_voice(general, choice, expected):
    assert select_voice(judgment(choice), general) == (expected or general, choice)


@pytest.mark.parametrize("confidence,probability,language", [
    (0.90, 0.95, "russian"), (0.899999, 0.98, "other"), (0.99, 0.949999, "other"),
    (0.0, 0.98, "other"), (0.99, 0.5, "other"),
])
def test_uncertain_decisions_abstain_at_exact_thresholds(confidence, probability, language):
    expected = RUSSIAN_REFERENCE_ID if language == "russian" else GENERAL
    assert select_voice(judgment(confidence=confidence, probability=probability), GENERAL) == (expected, language)


@pytest.mark.parametrize("payload", [None, [], {}, {"answers": []}, {"answers": {}},
                                           {"answers": {"language": None}}])
def test_missing_or_nonobject_judgments_fall_back(payload):
    assert select_voice(payload, GENERAL) == (GENERAL, "unknown")


@pytest.mark.parametrize("field,value", [
    ("type", "boolean"), ("type", None), ("choice", "ru"), ("choice", []),
    ("confidence", True), ("confidence", None), ("confidence", "0.99"),
    ("confidence", -0.1), ("confidence", 1.01), ("confidence", float("nan")),
    ("confidence", float("inf")), ("confidence", 10 ** 400),
    ("probabilities", []), ("probabilities", {"russian": 1}),
    ("probabilities", {"english": 0, "russian": 1, "other": 0, "injected": 0}),
    ("probabilities", {"english": 0, "russian": True, "other": 0}),
    ("probabilities", {"english": 0, "russian": "1", "other": 0}),
    ("probabilities", {"english": 0, "russian": float("nan"), "other": 0}),
    ("probabilities", {"english": 0, "russian": float("inf"), "other": 0}),
    ("probabilities", {"english": 0, "russian": 1.1, "other": -0.1}),
    ("probabilities", {"english": 0.02, "russian": 0.98, "other": 0.02}),
    ("probabilities", {"english": 0.99, "russian": 0.01, "other": 0}),
])
def test_invalid_schema_probabilities_and_confidence_never_select_russian(field, value):
    payload = judgment()
    payload["answers"]["language"][field] = value
    assert select_voice(payload, GENERAL) == (GENERAL, "unknown")


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500, 503])
async def test_http_failure_and_redirect_abstain_without_retry_or_leaking(status, caplog):
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(status, content=b"advisor-secret private reply",
                              headers={"location": "https://untrusted.example/classify"})

    classifier = LanguageVoice("advisor-secret", httpx.AsyncClient(
        transport=httpx.MockTransport(provider), follow_redirects=True,
    ))
    try:
        assert await classifier.select("private reply", GENERAL) == (GENERAL, "unknown")
    finally:
        await classifier.close()
    assert len(calls) == 1
    assert "advisor-secret" not in caplog.text
    assert "private reply" not in caplog.text


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_failure_has_one_request_and_general_fallback(error):
    calls = []

    async def provider(request):
        calls.append(request)
        raise error("private reply advisor-secret", request=request)

    classifier = LanguageVoice("advisor-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    try:
        assert await classifier.select("Private reply.", GENERAL) == (GENERAL, "unknown")
    finally:
        await classifier.close()
    assert len(calls) == 1


@pytest.mark.parametrize("raw", [
    b"not JSON", b"\xff", b"{}", b"x" * (MAX_RESPONSE_BYTES + 1),
    (b'{"answers":{},"answers":{"language":{"type":"choice","choice":"russian",'
     b'"confidence":1,"probabilities":{"english":0,"russian":1,"other":0}}}}'),
])
async def test_invalid_and_oversized_provider_responses_abstain(raw):
    async def provider(_request):
        return httpx.Response(200, content=raw)

    classifier = LanguageVoice("advisor-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    try:
        assert await classifier.select("Synthetic reply.", GENERAL) == (GENERAL, "unknown")
    finally:
        await classifier.close()


async def test_absent_key_makes_no_request_and_closes_injected_client():
    calls = []

    async def provider(request):
        calls.append(request)
        return httpx.Response(200, json=judgment())

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    classifier = LanguageVoice("", client)
    assert await classifier.select("Русский ответ.", GENERAL) == (GENERAL, "unknown")
    await classifier.close()
    assert calls == []
    assert client.is_closed


async def test_total_request_deadline_abstains_without_retry(monkeypatch):
    calls = []

    async def provider(request):
        calls.append(request)
        await asyncio.sleep(60)

    monkeypatch.setattr(language_voice, "REQUEST_SECONDS", 0.01)
    classifier = LanguageVoice("advisor-secret", httpx.AsyncClient(transport=httpx.MockTransport(provider)))
    try:
        assert await classifier.select("Synthetic reply.", GENERAL) == (GENERAL, "unknown")
    finally:
        await classifier.close()
    assert len(calls) == 1


async def test_cancellation_propagates_and_does_not_become_fallback():
    started = asyncio.Event()

    async def provider(_request):
        started.set()
        await asyncio.Future()

    client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    classifier = LanguageVoice("advisor-secret", client)
    task = asyncio.create_task(classifier.select("Synthetic reply.", GENERAL))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await classifier.close()
    assert client.is_closed
