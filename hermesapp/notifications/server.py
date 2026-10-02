"""Authenticated native run admission plus durable APNs reply delivery."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from aiohttp import web

LOG = logging.getLogger("hermes_push")
TERMINAL = {"completed", "failed", "cancelled", "interrupted", "incomplete"}
RECOVERY_SECONDS = 23 * 60 * 60  # Less than the native 24-hour idempotency retention.


@dataclass(frozen=True)
class Config:
    api_key: str
    native_url: str
    database: Path
    apns_key: str
    apns_key_id: str
    apns_team_id: str
    apns_topic: str
    poll_seconds: float = 1
    delivery_grace_seconds: float = 2

    @classmethod
    def from_environment(cls):
        key_path = Path(os.environ["APNS_KEY_PATH"])
        return cls(
            api_key=os.environ["API_SERVER_KEY"],
            native_url=os.environ.get("HERMES_NATIVE_URL", "http://172.23.0.1:8642"),
            database=Path(os.environ.get("PUSH_DATABASE", "/var/lib/hermes-push/delivery.sqlite")),
            apns_key=key_path.read_text(),
            apns_key_id=os.environ["APNS_KEY_ID"],
            apns_team_id=os.environ["APNS_TEAM_ID"],
            apns_topic=os.environ.get("APNS_TOPIC", "com.artbred.hermesapp"),
        )


class Bridge:
    def __init__(self, config: Config, native=None, apns=None):
        self.config = config
        if not all((config.api_key, config.apns_key_id, config.apns_team_id, config.apns_topic)):
            raise ValueError("API_SERVER_KEY and APNs identity fields must not be empty")
        # Fail startup, rather than advertise working notifications with invalid credentials.
        self._jwt = jwt.encode(
            {"iss": config.apns_team_id, "iat": int(time.time())},
            config.apns_key, algorithm="ES256", headers={"kid": config.apns_key_id},
        )
        self._jwt_time = time.time()
        config.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(config.database)
        self.db.row_factory = sqlite3.Row
        os.chmod(config.database, 0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY, token TEXT NOT NULL,
                environment TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                identity TEXT PRIMARY KEY, device_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                session_key TEXT NOT NULL, request_key TEXT NOT NULL, body BLOB NOT NULL,
                body_hash TEXT NOT NULL, receipt BLOB,
                run_id TEXT, state TEXT NOT NULL DEFAULT 'admitting',
                acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, next_at REAL NOT NULL, finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state, next_at);
            CREATE INDEX IF NOT EXISTS delivery_run ON deliveries(run_id, device_id);
        """)
        self.native = native or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self.apns = apns or httpx.AsyncClient(http2=True, timeout=20, follow_redirects=False)
        self._locks = {}

    @asynccontextmanager
    async def locked(self, identity):
        lock, count = self._locks.get(identity, (asyncio.Lock(), 0))
        self._locks[identity] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self._locks[identity]
            if count == 1:
                del self._locks[identity]
            else:
                self._locks[identity] = (lock, count - 1)

    def execute(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def finish(self, identity, state):
        self.execute("UPDATE deliveries SET state=?, finished_at=?, body=x'' WHERE identity=?",
                     (state, time.time(), identity))

    def headers(self, row):
        return {"Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json", "Idempotency-Key": row["request_key"],
                "X-Hermes-Session-Key": row["session_key"]}

    async def admit(self, row):
        response = await self.native.post(self.config.native_url + "/v1/runs",
                                          content=row["body"], headers=self.headers(row))
        if response.status_code == 202:
            receipt = response.json()
            run_id = receipt.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", run_id):
                raise ValueError("Native admission returned an invalid run ID")
            self.execute("UPDATE deliveries SET run_id=?, receipt=?, state='watching', next_at=? WHERE identity=?",
                         (run_id, response.content, time.time(), row["identity"]))
        elif response.status_code < 500:
            self.finish(row["identity"], "rejected")
        return response

    async def send_push(self, row, device):
        if time.time() - self._jwt_time >= 50 * 60:
            self._jwt_time = time.time()
            self._jwt = jwt.encode({"iss": self.config.apns_team_id, "iat": int(self._jwt_time)},
                                   self.config.apns_key, algorithm="ES256",
                                   headers={"kid": self.config.apns_key_id})
        host = "api.sandbox.push.apple.com" if device["environment"] == "development" else "api.push.apple.com"
        payload = {"aps": {"alert": {"title": "Hermes", "body": "Your reply is ready."},
                           "sound": "default", "thread-id": row["chat_id"]},
                   "kind": "hermes_reply", "chat_id": row["chat_id"], "run_id": row["run_id"]}
        response = await self.apns.post(
            f"https://{host}/3/device/{device['token']}", json=payload,
            headers={"authorization": f"bearer {self._jwt}", "apns-topic": self.config.apns_topic,
                     "apns-push-type": "alert", "apns-priority": "10",
                     "apns-expiration": str(int(time.time()) + 86400),
                     "apns-collapse-id": hashlib.sha256(row["run_id"].encode()).hexdigest()},
        )
        if response.status_code == 200:
            self.finish(row["identity"], "sent")
        elif response.status_code == 410 or (
            response.status_code == 400 and response.json().get("reason") == "BadDeviceToken"
        ):
            # A token update racing this response must not invalidate the new registration.
            self.execute("DELETE FROM devices WHERE device_id=? AND token=? AND environment=?",
                         (device["device_id"], device["token"], device["environment"]))
            LOG.warning("APNs rejected device registration: HTTP %s", response.status_code)
        else:
            LOG.error("APNs rejected reply delivery: HTTP %s", response.status_code)

    async def process(self, identity):
        async with self.locked(identity):
            row = self.db.execute("SELECT * FROM deliveries WHERE identity=?", (identity,)).fetchone()
            if not row or row["state"] not in {"admitting", "watching", "ready"} or row["next_at"] > time.time():
                return
            if row["acknowledged"]:
                self.finish(identity, "acknowledged")
                return
            now = time.time()
            self.execute("UPDATE deliveries SET next_at=? WHERE identity=?", (now + 10, identity))
            if row["state"] == "admitting":
                if now - row["created_at"] >= RECOVERY_SECONDS:
                    self.finish(identity, "admission_expired")
                    LOG.error("Unknown native admission exceeded safe idempotency recovery window")
                    return
                await self.admit(row)
                return
            if now - row["created_at"] > 30 * 86400:
                self.finish(identity, "expired")
                return
            if row["state"] == "watching":
                response = await self.native.get(self.config.native_url + "/v1/runs/" + row["run_id"],
                                                 headers={"Authorization": f"Bearer {self.config.api_key}"})
                if response.status_code == 404 and response.json().get("error", {}).get("code") == "run_not_found":
                    self.finish(identity, "unavailable")
                    return
                response.raise_for_status()
                run = response.json()
                if run.get("run_id") != row["run_id"]:
                    raise ValueError("Native status returned a different run")
                if run.get("status") in TERMINAL:
                    if run["status"] == "completed" and isinstance(run.get("output"), str) and run["output"].strip():
                        self.execute("UPDATE deliveries SET state='ready', next_at=?, body=x'' WHERE identity=?",
                                     (now + self.config.delivery_grace_seconds, identity))
                    else:
                        self.finish(identity, "no_reply")
                else:
                    self.execute("UPDATE deliveries SET next_at=? WHERE identity=?",
                                 (now + self.config.poll_seconds, identity))
                return
            device = self.db.execute("SELECT * FROM devices WHERE device_id=?", (row["device_id"],)).fetchone()
            if device:
                await self.send_push(row, device)

    async def tick(self):
        rows = self.db.execute("SELECT identity FROM deliveries WHERE state IN ('admitting','watching','ready') AND next_at<=? ORDER BY next_at LIMIT 10", (time.time(),)).fetchall()
        results = await asyncio.gather(*(self.process(row["identity"]) for row in rows), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                # Exceptions can embed URLs/tokens; log only the class, never their text.
                LOG.error("Reply delivery attempt failed: %s", type(result).__name__)
        self.execute("DELETE FROM deliveries WHERE finished_at<?", (time.time() - 7 * 86400,))
        self.execute("DELETE FROM devices WHERE updated_at<?", (time.time() - 90 * 86400,))

    async def monitor(self):
        while True:
            await self.tick()
            await asyncio.sleep(self.config.poll_seconds)

    async def close(self):
        await self.native.aclose()
        await self.apns.aclose()
        self.db.close()


def valid_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)).lower() == value.lower()
    except (ValueError, AttributeError):
        return False


def create_app(bridge: Bridge):
    @web.middleware
    async def authenticate(request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(),
                                   f"Bearer {bridge.config.api_key}".encode()):
            raise web.HTTPUnauthorized()
        try:
            return await handler(request)
        except (httpx.HTTPError, ValueError) as error:
            LOG.error("Notification request failed: %s", type(error).__name__)
            raise web.HTTPBadGateway(text="Notification service could not reach Hermes") from None

    app = web.Application(middlewares=[authenticate], client_max_size=4 * 1024 * 1024)

    async def register(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        device_id, token, environment = body.get("device_id"), body.get("token"), body.get("environment")
        if not valid_uuid(device_id) or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64,200}", token) or not isinstance(environment, str) or environment not in {"development", "production"}:
            raise web.HTTPBadRequest(text="Invalid push registration")
        bridge.execute("INSERT INTO devices VALUES (?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET token=excluded.token, environment=excluded.environment, updated_at=excluded.updated_at",
                       (device_id, token, environment, time.time()))
        return web.json_response({"ok": True})

    async def acknowledge(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object")
        device_id = body.get("device_id")
        if not valid_uuid(device_id):
            raise web.HTTPBadRequest(text="Invalid device ID")
        bridge.execute("UPDATE deliveries SET acknowledged=1 WHERE run_id=? AND device_id=?",
                       (request.match_info["run_id"], device_id))
        return web.json_response({"ok": True})

    async def submit(request):
        body = await request.read()
        device_id, chat_id = request.headers.get("X-Hermes-Push-Device"), request.headers.get("X-Hermes-Push-Chat")
        headers = {key: request.headers[key] for key in ["Authorization", "Content-Type", "X-Hermes-Session-Key", "Idempotency-Key"] if key in request.headers}
        if device_id is None and chat_id is None:
            response = await bridge.native.post(bridge.config.native_url + "/v1/runs", content=body, headers=headers)
        else:
            session_key, request_key = headers.get("X-Hermes-Session-Key", ""), headers.get("Idempotency-Key", "")
            try:
                body_hash = hashlib.sha256(json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            except (ValueError, UnicodeDecodeError):
                raise web.HTTPBadRequest(text="Invalid run JSON") from None
            if not valid_uuid(device_id) or not valid_uuid(chat_id) or session_key != "ios-chat:" + chat_id or not request_key or len(request_key) > 200:
                raise web.HTTPBadRequest(text="Invalid reply notification destination")
            if bridge.db.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone() is None:
                raise web.HTTPConflict(text="Register push notifications before sending")
            identity = hashlib.sha256(json.dumps([device_id, session_key, request_key]).encode()).hexdigest()
            async with bridge.locked(identity):
                row = bridge.db.execute("SELECT * FROM deliveries WHERE identity=?", (identity,)).fetchone()
                if row and row["body_hash"] != body_hash:
                    raise web.HTTPConflict(text="Idempotency key reused with different input")
                if row is None:
                    bridge.execute("INSERT INTO deliveries(identity,device_id,chat_id,session_key,request_key,body,body_hash,created_at,next_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (identity, device_id, chat_id, session_key, request_key, body, body_hash, time.time(), time.time() + 30))
                elif row["state"] == "rejected":
                    bridge.execute("UPDATE deliveries SET state='admitting', body=?, finished_at=NULL, next_at=? WHERE identity=?",
                                   (body, time.time() + 30, identity))
                row = bridge.db.execute("SELECT * FROM deliveries WHERE identity=?", (identity,)).fetchone()
                if row["receipt"]:
                    return web.Response(body=row["receipt"], status=202, content_type="application/json")
                elif row["state"] == "admission_expired":
                    raise web.HTTPConflict(text="Admission recovery expired; check history before an explicit new attempt")
                else:
                    response = await bridge.admit(row)
        return web.Response(body=response.content, status=response.status_code,
                            headers={key: response.headers[key] for key in ["content-type", "retry-after"] if key in response.headers})

    async def lifecycle(_app):
        monitor = asyncio.create_task(bridge.monitor())
        yield
        monitor.cancel()
        try:
            await monitor
        except asyncio.CancelledError:
            pass
        await bridge.close()

    app.router.add_post("/api/push/devices", register)
    app.router.add_post("/api/push/runs/{run_id}/ack", acknowledge)
    app.router.add_post("/v1/runs", submit)
    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    web.run_app(create_app(Bridge(Config.from_environment())),
                host=os.environ.get("PUSH_HOST", "127.0.0.1"),
                port=int(os.environ.get("PUSH_PORT", "9123")), access_log=None)
