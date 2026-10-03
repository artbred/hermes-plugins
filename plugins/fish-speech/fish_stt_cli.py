#!/usr/bin/env python3
"""Fish Audio ASR — transcribe an audio file, print JSON {"text": ...} to stdout.
Usage: fish_stt.py <audio_path> [language]
Env: FISH_API_KEY (required).
"""
import json, os, sys, uuid, urllib.request, urllib.error

API = "https://api.fish.audio/v1/asr"

def main():
    path = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else ""
    key = os.environ.get("FISH_API_KEY", "").strip()
    if not key:
        print("FISH_API_KEY not set", file=sys.stderr); sys.exit(1)

    audio = open(path, "rb").read()
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())

    fname = os.path.basename(path) or "audio.ogg"
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{fname}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(audio)
    parts.append(b"\r\n")
    if language and language not in ("auto", "none"):
        field("language", language)
    field("ignore_timestamps", "true")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Fish ASR HTTP {e.code}: {e.read().decode()[:400]}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"text": (data.get("text") or "").strip()}, ensure_ascii=False))

if __name__ == "__main__":
    main()
