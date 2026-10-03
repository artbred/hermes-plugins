"""Offline behavior tests through real Hermes discovery and native TTS dispatch.

Run with the supported Hermes checkout on PYTHONPATH and ffmpeg/ffprobe on PATH.
Every plugin installation, config, secret scope, and output belongs to tmp_path.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PLUGIN = Path(__file__).resolve().parents[1]
GENERAL = "933563129e564b19a115bedd57b7406a"
RUSSIAN = "c35aeeb5f9c145199fbffdbc2ef8ed95"
CUSTOM = "9a9cf47702da476aa4629e2506d4a857"
MODEL = "s2.1-pro"
SECRETS = {"FISH_API_KEY": "scoped-fish-secret", "OPENROUTER_API_KEY": "scoped-jev-secret"}


def judgment(choice="english", confidence=0.99, probability=0.99):
    probabilities = {language: (1 - probability) / 2 for language in ("english", "russian", "other")}
    probabilities[choice] = probability
    return {"answers": {"language": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": probabilities,
    }}}


@pytest.fixture
def audio(tmp_path):
    executable = shutil.which("ffmpeg")
    assert executable, "ffmpeg is required for the offline real-audio fixture"
    output = tmp_path / "fixture.mp3"
    subprocess.run([
        executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
        "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "128k", str(output),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output.read_bytes()


@pytest.fixture
def hermes(tmp_path, monkeypatch, request):
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    (launcher / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launcher))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.setenv("HERMES_SAFE_MODE", "0")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "cli")
    monkeypatch.chdir(tmp_path)

    def no_network(*args, **kwargs):
        raise AssertionError("Live network access is forbidden in plugin tests")

    async def no_async_network(*args, **kwargs):
        no_network()

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_network)

    constants = pytest.importorskip("hermes_constants")
    home_token = constants.set_hermes_home_override(launcher)
    request.addfinalizer(lambda: constants.reset_hermes_home_override(home_token))
    scope = pytest.importorskip("agent.secret_scope")
    multiplex_token = scope.set_multiplex_context(True)
    request.addfinalizer(lambda: scope.reset_multiplex_context(multiplex_token))
    secret_token = scope.set_secret_scope({}, profile_home=str(launcher))
    request.addfinalizer(lambda: scope.reset_secret_scope(secret_token))
    plugins = pytest.importorskip("hermes_cli.plugins")
    yaml = pytest.importorskip("hermes_yaml")
    registry = pytest.importorskip("agent.tts_registry")
    tts = pytest.importorskip("tools.tts_tool")
    monkeypatch.setattr(plugins, "_plugin_manager", None)
    monkeypatch.setattr(plugins, "_plugin_managers_by_home", {})
    loader = pytest.importorskip("hermes_cli.plugins_loader")
    bare_scopes = dict(loader._BARE_MODULE_SCOPE)
    monkeypatch.setattr(loader, "_BARE_MODULE_SCOPE", bare_scopes)
    monkeypatch.setattr(plugins, "_BARE_MODULE_SCOPE", bare_scopes)
    module_prefix = "hermes_plugins.fish_language_tts"
    previous_modules = {name for name in sys.modules if name.startswith(module_prefix)}
    managers = []

    @contextmanager
    def use(home, secrets=SECRETS):
        token = constants.set_hermes_home_override(home)
        credentials = scope.set_secret_scope(secrets, profile_home=str(home))
        try:
            yield
        finally:
            scope.reset_secret_scope(credentials)
            constants.reset_hermes_home_override(token)

    def install(name="profile", *, general_voice=None, enabled=True):
        home = tmp_path / name
        home.mkdir()
        shutil.copytree(PLUGIN, home / "plugins" / "fish-language-tts",
                        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.egg-info", "build", "dist"))
        settings = {} if general_voice is None else {"general_voice": general_voice}
        config = {
            "plugins": {"enabled": ["fish-language-tts"] if enabled else [],
                        "entries": {"fish-language-tts": {"settings": settings}}},
            "tts": {"provider": "fish-language", "voice": "general", "model": MODEL,
                    "output_format": "mp3"},
        }
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        with use(home, {}):
            manager = plugins.get_plugin_manager()
            managers.append(manager)
            manager.discover_and_load()
        return home, manager

    def provider(home):
        with use(home):
            result = registry.get_provider("fish-language")
            assert result is not None
            return result

    def dispatch(home, text="Hello, this is a completed reply.", *, config=None, secrets=SECRETS,
                 output=None):
        with use(home, secrets):
            return tts._dispatch_to_plugin_provider(
                text, str(output or home / "audio.mp3"), "fish-language", config or {})

    try:
        yield SimpleNamespace(install=install, use=use, provider=provider, dispatch=dispatch,
                              registry=registry, plugins=plugins, tts=tts, yaml=yaml)
    finally:
        for manager in managers:
            plugins._clear_plugin_submodules(manager)
            manager.unload("fish-language-tts")
        for name in set(sys.modules) - previous_modules:
            if name.startswith(module_prefix):
                sys.modules.pop(name, None)


@pytest.fixture
def wire(monkeypatch, audio):
    real_client = httpx.AsyncClient
    calls, clients = [], []

    def connect(*, classification=None, fish_status=200, fish_audio=None, jev_status=200,
                language_error=None, fail_at=None):
        def respond(request):
            calls.append(request)
            if request.url.host == "openrouter.ai":
                if language_error is not None:
                    raise language_error
                return httpx.Response(jev_status, json=(judgment() if classification is None else classification))
            assert request.url.host == "api.fish.audio", "Unexpected external provider"
            if fail_at is not None and len(fish_calls(calls)) == fail_at:
                return httpx.Response(503, content=b"PRIVATE TRANSCRIPT scoped-fish-secret")
            return httpx.Response(
                fish_status, content=(audio if fish_audio is None else fish_audio),
                headers={"content-type": "audio/mpeg"},
            )

        def client(*args, **kwargs):
            result = real_client(*args, **kwargs, transport=httpx.MockTransport(respond))
            clients.append(result)
            return result

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return calls

    yield SimpleNamespace(connect=connect, calls=calls, clients=clients, audio=audio)
    assert all(client.is_closed for client in clients), "Speech must close every per-call client"


def fish_calls(calls):
    return [request for request in calls if request.url.host == "api.fish.audio"]


@pytest.mark.parametrize(("text", "classification", "expected"), [
    ("A completed English reply.", judgment(), GENERAL),
    ("Законченный русский ответ.", judgment("russian"), RUSSIAN),
    ("Une réponse française.", judgment("other"), GENERAL),
    ("A mixed or uncertain reply.", judgment("russian", confidence=0.89), GENERAL),
    ("Invalid judgment reply.", {"answers": {"language": {"choice": "russian"}}}, GENERAL),
])
def test_real_discovery_and_native_dispatch_choose_voice(hermes, wire, text, classification, expected):
    home, _ = hermes.install()
    wire.connect(classification=classification)
    written = hermes.dispatch(home, text, config={"voice": "general", "model": MODEL, "speed": 1.25})
    assert Path(written).read_bytes() == wire.audio
    requests = fish_calls(wire.calls)
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body == {"text": text, "reference_id": expected, "format": "mp3", "prosody": {"speed": 1.25}}
    assert requests[0].headers["model"] == MODEL
    assert requests[0].headers["Authorization"] == "Bearer scoped-fish-secret"
    assert wire.calls[0].headers["Authorization"] == "Bearer scoped-jev-secret"


@pytest.mark.parametrize(("configured", "voice", "classification", "expected"), [
    (CUSTOM, "general", judgment(), CUSTOM),
    (None, CUSTOM, judgment(), CUSTOM),
    (CUSTOM, "general", judgment("russian"), RUSSIAN),
    (CUSTOM, CUSTOM, judgment("russian"), RUSSIAN),
    (CUSTOM, "general", judgment("other"), CUSTOM),
])
def test_custom_selection_is_general_voice_not_classifier_bypass(hermes, wire, configured, voice,
                                                               classification, expected):
    home, _ = hermes.install(general_voice=configured)
    wire.connect(classification=classification)
    hermes.dispatch(home, config={"voice": voice})
    assert json.loads(fish_calls(wire.calls)[0].content)["reference_id"] == expected


@pytest.mark.parametrize("failure", ["absent", "http", "transport", "malformed"])
def test_optional_jev_failure_uses_configured_general(hermes, wire, failure):
    home, _ = hermes.install(general_voice=CUSTOM)
    options = {}
    secrets = SECRETS
    if failure == "absent":
        secrets = {"FISH_API_KEY": SECRETS["FISH_API_KEY"]}
    elif failure == "http":
        options["jev_status"] = 503
    elif failure == "transport":
        options["language_error"] = httpx.ReadTimeout("PRIVATE CLASSIFIER ERROR")
    else:
        options["classification"] = ["not a judgment"]
    wire.connect(**options)
    written = hermes.dispatch(home, config={"voice": "general"}, secrets=secrets)
    assert Path(written).read_bytes() == wire.audio
    assert json.loads(fish_calls(wire.calls)[0].content)["reference_id"] == CUSTOM
    if failure == "absent":
        assert len(wire.calls) == 1


@pytest.mark.parametrize(("text", "arguments", "message"), [
    ("Synthetic reply.", {"model": "s2.1-pro-free"}, "model must be s2.1-pro"),
    ("Synthetic reply.", {"model": "s1"}, "model must be s2.1-pro"),
    ("Synthetic reply.", {"model": ""}, "model must be s2.1-pro"),
    ("Synthetic reply.", {"voice": "alloy"}, "32 hexadecimal"),
    ("Synthetic reply.", {"voice": ""}, "32 hexadecimal"),
    ("Synthetic reply.", {"speed": True}, "speed must be"),
    ("Synthetic reply.", {"speed": float("nan")}, "speed must be"),
    ("Synthetic reply.", {"speed": float("inf")}, "speed must be"),
    ("Synthetic reply.", {"speed": 0.25}, "speed must be"),
    ("Synthetic reply.", {"speed": 4}, "speed must be"),
    ("Synthetic reply.", {"speed": "fast"}, "speed must be"),
    ("  ", {}, "nonempty"),
    ("x" * 64001, {}, "64000-character"),
    ("invalid\ud800unicode", {}, "valid Unicode"),
])
def test_invalid_inputs_reject_before_any_paid_or_advisory_call(hermes, wire, text, arguments, message):
    home, _ = hermes.install()
    wire.connect()
    output = home / "previous.mp3"
    output.write_bytes(b"previous output")
    with hermes.use(home), pytest.raises(ValueError, match=message):
        hermes.provider(home).synthesize(text, str(output), **arguments)
    assert output.read_bytes() == b"previous output"
    assert wire.calls == []
    assert wire.clients == []


@pytest.mark.parametrize("general_voice", ["alloy", "", "z" * 32, 42])
def test_invalid_general_setting_cannot_register_provider(hermes, general_voice):
    home, _ = hermes.install(general_voice=general_voice)
    with hermes.use(home):
        assert hermes.registry.get_provider("fish-language") is None


@pytest.mark.parametrize(("status", "audio_body", "message"), [
    (402, b"PRIVATE TRANSCRIPT scoped-fish-secret", r"Fish speech failed \(HTTP 402\)"),
    (200, b"PRIVATE TRANSCRIPT scoped-fish-secret", "invalid MP3"),
])
def test_provider_failure_preserves_prior_output_and_redacts(hermes, wire, status, audio_body, message):
    home, manager = hermes.install()
    wire.connect(fish_status=status, fish_audio=audio_body)
    output = home / "audio.mp3"
    output.write_bytes(b"previous output")
    error_type = manager._plugins["fish-language-tts"].module.SpeechError
    with pytest.raises(error_type, match=message) as failure:
        hermes.dispatch(home, "PRIVATE TRANSCRIPT", output=output)
    assert output.read_bytes() == b"previous output"
    assert not list(home.glob(".fish-speech-*"))
    assert "PRIVATE" not in str(failure.value)
    assert "scoped-fish-secret" not in str(failure.value)


def test_later_chunk_failure_preserves_prior_output(hermes, wire):
    home, manager = hermes.install()
    wire.connect(fail_at=2)
    output = home / "audio.mp3"
    output.write_bytes(b"previous output")
    error_type = manager._plugins["fish-language-tts"].module.SpeechError
    with pytest.raises(error_type, match=r"Fish speech failed \(HTTP 503\)"):
        hermes.dispatch(home, "word " * 1000, output=output)
    assert len(fish_calls(wire.calls)) == 2
    assert output.read_bytes() == b"previous output"
    assert not list(home.glob(".fish-speech-*"))


def test_long_reply_is_preserved_and_assembled_as_real_audio(hermes, wire):
    home, _ = hermes.install()
    wire.connect()
    text = "word " * 1000
    written = hermes.dispatch(home, text)
    requests = fish_calls(wire.calls)
    assert len(requests) == 2
    assert "".join(json.loads(request.content)["text"] for request in requests) == text
    executable = shutil.which("ffprobe")
    assert executable, "ffprobe is required to inspect assembled plugin audio"
    probe = subprocess.run([
        executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", written,
    ], check=True, capture_output=True, text=True)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 0.35


def test_write_failure_preserves_prior_output_and_removes_temporary(hermes, wire, monkeypatch):
    home, manager = hermes.install()
    wire.connect()
    output = home / "audio.mp3"
    output.write_bytes(b"previous output")

    def failed_replace(self, destination):
        raise OSError("PRIVATE PATH")

    monkeypatch.setattr(Path, "replace", failed_replace)
    error_type = manager._plugins["fish-language-tts"].module.SpeechError
    with pytest.raises(error_type, match="could not write audio") as failure:
        hermes.dispatch(home, output=output)
    assert output.read_bytes() == b"previous output"
    assert not list(home.glob(".fish-speech-*"))
    assert "PRIVATE" not in str(failure.value)


def test_missing_fish_is_unavailable_and_never_uses_process_credentials(hermes, wire, monkeypatch):
    home, manager = hermes.install()
    wire.connect()
    monkeypatch.setenv("FISH_API_KEY", "foreign-process-secret")
    with hermes.use(home, {}):
        provider = hermes.provider(home)
        assert provider.is_available() is False
        error_type = manager._plugins["fish-language-tts"].module.SpeechError
        with pytest.raises(error_type, match="FISH_API_KEY is required"):
            provider.synthesize("A private reply.", str(home / "missing.mp3"))
    assert wire.calls == []
    assert not (home / "missing.mp3").exists()


def test_no_scope_fails_closed_without_process_key(hermes, wire, monkeypatch):
    home, manager = hermes.install()
    wire.connect()
    monkeypatch.setenv("FISH_API_KEY", "foreign-process-secret")
    provider = hermes.provider(home)
    with hermes.use(home, None):
        assert provider.is_available() is False
        error_type = manager._plugins["fish-language-tts"].module.SpeechError
        with pytest.raises(error_type, match="unavailable in this profile"):
            provider.synthesize("A private reply.", str(home / "missing.mp3"))
    assert wire.calls == []


def test_readiness_catalog_and_setup_are_passive_and_scope_live(hermes, monkeypatch):
    home, _ = hermes.install()
    provider = hermes.provider(home)

    def no_client(*args, **kwargs):
        raise AssertionError("Passive picker methods must not construct HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    for secrets, available in (({}, False), (SECRETS, True), ({"FISH_API_KEY": " "}, False)):
        with hermes.use(home, secrets):
            assert provider.is_available() is available
            assert hermes.tts.check_tts_requirements() is available
            provider.default_model()
            provider.list_models()
            provider.default_voice()
            provider.list_voices()
            schema = provider.get_setup_schema()
            assert "scoped-fish-secret" not in json.dumps(schema)


def test_rotated_secrets_are_resolved_per_synthesis_not_at_registration(hermes, wire):
    home, _ = hermes.install()
    wire.connect()
    for index in (1, 2):
        hermes.dispatch(home, secrets={"FISH_API_KEY": f"fish-{index}", "OPENROUTER_API_KEY": f"jev-{index}"})
    assert [request.headers["Authorization"] for request in wire.calls] == [
        "Bearer jev-1", "Bearer fish-1", "Bearer jev-2", "Bearer fish-2",
    ]


def test_profiles_and_foreign_context_unload_are_isolated(hermes, wire):
    first, first_manager = hermes.install("first", general_voice=GENERAL)
    second, _ = hermes.install("second", general_voice=CUSTOM)
    wire.connect()
    hermes.dispatch(first, secrets={"FISH_API_KEY": "first-fish"})
    hermes.dispatch(second, secrets={"FISH_API_KEY": "second-fish"})
    requests = fish_calls(wire.calls)
    assert [request.headers["Authorization"] for request in requests] == ["Bearer first-fish", "Bearer second-fish"]
    assert [json.loads(request.content)["reference_id"] for request in requests] == [GENERAL, CUSTOM]
    with hermes.use(second):
        assert first_manager.unload("fish-language-tts") is True
        assert hermes.registry.get_provider("fish-language") is not None
    with hermes.use(first):
        assert hermes.registry.get_provider("fish-language") is None
    hermes.dispatch(second, secrets={"FISH_API_KEY": "second-rotated"})
    assert fish_calls(wire.calls)[-1].headers["Authorization"] == "Bearer second-rotated"


def test_disabled_and_removed_plugin_do_not_leave_provider(hermes):
    home, manager = hermes.install(enabled=False)
    with hermes.use(home):
        assert hermes.registry.get_provider("fish-language") is None
        config = hermes.yaml.safe_load((home / "config.yaml").read_text())
        config["plugins"]["enabled"] = ["fish-language-tts"]
        (home / "config.yaml").write_text(hermes.yaml.safe_dump(config), encoding="utf-8")
        manager.discover_and_load(force=True)
        assert hermes.registry.get_provider("fish-language") is not None
        config["plugins"]["disabled"] = ["fish-language-tts"]
        (home / "config.yaml").write_text(hermes.yaml.safe_dump(config), encoding="utf-8")
        manager.discover_and_load(force=True)
        assert hermes.registry.get_provider("fish-language") is None
    assert hermes.dispatch(home) is None


def test_native_tool_returns_decodable_audio_without_command_or_edge(hermes, wire, monkeypatch):
    home, _ = hermes.install()
    wire.connect()

    def forbidden(*args, **kwargs):
        raise AssertionError("Native Fish dispatch must not delegate to command or free providers")

    monkeypatch.setattr(hermes.tts, "_generate_command_tts", forbidden)
    monkeypatch.setattr(hermes.tts, "_synthesize_builtin", forbidden)
    with hermes.use(home):
        result = json.loads(hermes.tts.text_to_speech_tool("A completed English reply.", str(home / "native.mp3")))
    assert result["success"] is True
    assert result["provider"] == "fish-language"
    assert Path(result["file_path"]).read_bytes() == wire.audio
    executable = shutil.which("ffprobe")
    assert executable, "ffprobe is required to prove decodable native plugin audio"
    probe = subprocess.run([
        executable, "-v", "error", "-show_entries", "format=duration", "-of", "json", result["file_path"],
    ], check=True, capture_output=True, text=True)
    assert float(json.loads(probe.stdout)["format"]["duration"]) > 0


def test_native_tool_rejects_free_model_without_fallback(hermes, wire, monkeypatch):
    home, _ = hermes.install()
    wire.connect()
    config = hermes.yaml.safe_load((home / "config.yaml").read_text())
    config["tts"]["model"] = "s2.1-pro-free"
    (home / "config.yaml").write_text(hermes.yaml.safe_dump(config), encoding="utf-8")

    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected paid config must not fall back to a free provider")

    monkeypatch.setattr(hermes.tts, "_synthesize_builtin", forbidden)
    with hermes.use(home):
        result = json.loads(hermes.tts.text_to_speech_tool("A completed reply.", str(home / "rejected.mp3")))
    assert result["success"] is False
    assert "model must be s2.1-pro" in result["error"]
    assert wire.calls == []
    assert not (home / "rejected.mp3").exists()


def test_unsupported_format_returns_actual_mp3_path(hermes, wire):
    home, _ = hermes.install()
    wire.connect()
    written = hermes.dispatch(home, config={"output_format": "wav"}, output=home / "requested.wav")
    assert written == str(home / "requested.mp3")
    assert Path(written).read_bytes() == wire.audio
    assert not (home / "requested.wav").exists()
