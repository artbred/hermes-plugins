import asyncio
import json
import time
import uuid

import httpx
import pytest
from aiohttp.test_utils import (
    TestClient,
    TestServer,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server import (
    RECOVERY_SECONDS,
    Bridge,
    Config,
    create_app,
)


@pytest.fixture
async def setup(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    config = Config("test-auth", "http://native", tmp_path / "delivery.sqlite", key,
                    "TESTKEY", "TESTTEAM", "com.artbred.hermesapp", 3600, 0)
    native_calls, pushes = [], []
    state = {"status": "running", "output": "Private reply", "lost_admission": False}

    async def native(request):
        native_calls.append(request)
        if request.method == "POST":
            if state["lost_admission"]:
                state["lost_admission"] = False
                raise httpx.ReadError("lost admission receipt", request=request)
            return httpx.Response(202, json={"run_id": "run-original", "status": "started"})
        return httpx.Response(200, json={"run_id": "run-original", **state})

    async def apns(request):
        pushes.append(request)
        return httpx.Response(200)

    def make_bridge():
        return Bridge(config, httpx.AsyncClient(transport=httpx.MockTransport(native)),
                      httpx.AsyncClient(transport=httpx.MockTransport(apns)))

    bridge = make_bridge()
    client = TestClient(TestServer(create_app(bridge)))
    await client.start_server()
    device, chat = str(uuid.uuid4()), str(uuid.uuid4())
    headers = {"Authorization": "Bearer test-auth"}
    registration = {"device_id": device, "token": "ab" * 32, "environment": "development"}
    response = await client.post("/api/push/devices", json=registration, headers=headers)
    assert response.status == 200
    run_headers = {**headers, "X-Hermes-Push-Device": device, "X-Hermes-Push-Chat": chat,
                   "X-Hermes-Session-Key": "ios-chat:" + chat, "Idempotency-Key": "message-0"}
    yield bridge, client, state, native_calls, pushes, run_headers, make_bridge
    await client.close()


async def process_due(bridge):
    bridge.execute("UPDATE deliveries SET next_at=0")
    await bridge.tick()


async def submit(client, headers, body=None):
    return await client.post("/v1/runs", json=body or {"input": "Please reply"}, headers=headers)


async def test_reply_survives_restart_and_receipt_replay(setup):
    bridge, client, state, calls, pushes, headers, make_bridge = setup
    receipt = await (await submit(client, headers)).json()
    state["status"] = "completed"
    await process_due(bridge)
    assert not pushes  # Completion is durably ready, not sent in the status transaction.
    await client.close()
    restarted = make_bridge()
    try:
        await process_due(restarted)
        row = restarted.db.execute("SELECT state,body FROM deliveries").fetchone()
        assert tuple(row) == ("sent", b"")
        payload = json.loads(pushes[0].content)
        assert payload["chat_id"] == headers["X-Hermes-Push-Chat"]
        assert payload["run_id"] == receipt["run_id"]
        assert "Private reply" not in pushes[0].content.decode()
        assert pushes[0].url.host == "api.sandbox.push.apple.com"
        await process_due(restarted)
        assert len(pushes) == 1
        replay_client = TestClient(TestServer(create_app(restarted)))
        await replay_client.start_server()
        replay = await submit(replay_client, headers)
        assert await replay.json() == receipt
        assert len([r for r in calls if r.method == "POST"]) == 1
        changed = await submit(replay_client, headers, {"input": "Different action"})
        assert changed.status == 409
        await replay_client.close()
    finally:
        if not restarted.native.is_closed:
            await restarted.close()


async def test_foreground_ack_suppresses_only_its_device(setup):
    bridge, client, state, _, pushes, headers, _ = setup
    await submit(client, headers)
    state["status"] = "completed"
    await process_due(bridge)
    response = await client.post("/api/push/runs/run-original/ack",
                                 json={"device_id": str(uuid.uuid4())},
                                 headers={"Authorization": "Bearer test-auth"})
    assert response.status == 200
    assert bridge.db.execute("SELECT acknowledged FROM deliveries").fetchone()[0] == 0
    await client.post("/api/push/runs/run-original/ack",
                      json={"device_id": headers["X-Hermes-Push-Device"]},
                      headers={"Authorization": "Bearer test-auth"})
    await process_due(bridge)
    assert not pushes
    assert bridge.db.execute("SELECT state FROM deliveries").fetchone()[0] == "acknowledged"


@pytest.mark.parametrize("status,output", [("completed", ""), ("cancelled", "Partial"),
                                          ("failed", "Partial"), ("interrupted", "Partial")])
async def test_no_response_or_stopped_run_does_not_alert(setup, status, output):
    bridge, client, state, _, pushes, headers, _ = setup
    await submit(client, headers)
    state.update(status=status, output=output)
    await process_due(bridge)
    await process_due(bridge)
    assert not pushes
    assert bridge.db.execute("SELECT state FROM deliveries").fetchone()[0] == "no_reply"


async def test_unknown_admission_recovers_original_identity(setup):
    bridge, client, state, calls, pushes, headers, _ = setup
    state["lost_admission"] = True
    response = await submit(client, headers)
    assert response.status == 502
    await process_due(bridge)
    posts = [r for r in calls if r.method == "POST"]
    assert posts[0].content == posts[1].content
    assert posts[0].headers["idempotency-key"] == posts[1].headers["idempotency-key"]
    assert bridge.db.execute("SELECT run_id FROM deliveries").fetchone()[0] == "run-original"
    state["status"] = "completed"
    await process_due(bridge)
    await process_due(bridge)
    assert json.loads(pushes[0].content)["run_id"] == "run-original"


async def test_unknown_admission_never_replays_after_retention(setup):
    bridge, client, state, calls, _, headers, _ = setup
    state["lost_admission"] = True
    await submit(client, headers)
    bridge.execute("UPDATE deliveries SET created_at=?", (time.time() - RECOVERY_SECONDS - 1,))
    await process_due(bridge)
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert (await submit(client, headers)).status == 409


async def test_concurrent_replay_has_one_admission(setup):
    bridge, client, _, calls, _, headers, _ = setup
    first, second = await asyncio.gather(submit(client, headers), submit(client, headers))
    assert await first.json() == await second.json()
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert not bridge._locks


async def test_invalidated_old_token_cannot_delete_replacement(setup):
    bridge, client, state, _, _, headers, _ = setup
    await submit(client, headers)
    state["status"] = "completed"
    await process_due(bridge)

    async def replace_token(request):
        bridge.execute("UPDATE devices SET token=?", ("cd" * 32,))
        return httpx.Response(410, json={"reason": "Unregistered"})

    await bridge.apns.aclose()
    bridge.apns = httpx.AsyncClient(transport=httpx.MockTransport(replace_token))
    await process_due(bridge)
    assert bridge.db.execute("SELECT token FROM devices").fetchone()[0] == "cd" * 32


async def test_unauthorized_registration_cannot_mutate_devices(setup):
    bridge, client, _, _, _, _, _ = setup
    response = await client.post("/api/push/devices", json={"device_id": str(uuid.uuid4())})
    assert response.status == 401
    assert bridge.db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
