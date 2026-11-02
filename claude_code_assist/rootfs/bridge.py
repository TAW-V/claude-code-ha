"""HTTP bridge between the Home Assistant conversation agent and headless Claude Code."""

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8099
WORKSPACE = "/data/workspace"
SESSIONS_FILE = "/data/sessions.json"
SESSION_TTL = 30 * 60  # start a fresh Claude session after 30 min of silence
TOKEN = open("/data/bridge_token").read().strip()
MODEL = os.environ.get("CCA_MODEL", "sonnet")
TIMEOUT = int(os.environ.get("CCA_TIMEOUT", "180"))
EXTRA = os.environ.get("CCA_EXTRA", "")
TOOLS = "Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite"

_lock = threading.Lock()
_conv_locks: dict[str, threading.Lock] = {}


def log(msg: str) -> None:
    print(f"[cca] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sessions(sessions: dict) -> None:
    now = time.time()
    sessions = {k: v for k, v in sessions.items() if now - v["ts"] < SESSION_TTL}
    with open(SESSIONS_FILE + ".tmp", "w") as f:
        json.dump(sessions, f)
    os.replace(SESSIONS_FILE + ".tmp", SESSIONS_FILE)


def to_speech(text: str) -> str:
    """Strip markdown so TTS and the Assist bubble read cleanly."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    return text.strip()


def ask_claude(text: str, conversation_id: str, context: str) -> str:
    with _lock:
        conv_lock = _conv_locks.setdefault(conversation_id, threading.Lock())
    with conv_lock:
        sessions = load_sessions()
        entry = sessions.get(conversation_id)
        resume = entry["session_id"] if entry and time.time() - entry["ts"] < SESSION_TTL else None

        cmd = [
            "claude", "-p", f"{context}\n{text}",
            "--output-format", "json",
            "--model", MODEL,
            "--allowedTools", TOOLS,
            "--add-dir", "/config", "/addons", "/share",
        ]
        if EXTRA:
            cmd += ["--append-system-prompt", EXTRA]
        if resume:
            cmd += ["--resume", resume]

        started = time.time()
        try:
            proc = subprocess.run(
                cmd, cwd=WORKSPACE, capture_output=True, text=True, timeout=TIMEOUT
            )
        except subprocess.TimeoutExpired:
            log(f"timeout after {TIMEOUT}s: {text!r}")
            return "Dat duurde te lang, ik heb het afgebroken. Probeer het nog eens of vraag het korter."

        try:
            out = json.loads(proc.stdout)
        except ValueError:
            log(f"bad output rc={proc.returncode} stderr={proc.stderr[-500:]!r} stdout={proc.stdout[-500:]!r}")
            return "Claude Code gaf geen bruikbaar antwoord. Kijk in het log van de Claude Code Assist app."

        if out.get("session_id"):
            sessions[conversation_id] = {"session_id": out["session_id"], "ts": time.time()}
            save_sessions(sessions)
        log(f"{time.time() - started:.1f}s resume={bool(resume)} error={out.get('is_error')} q={text[:80]!r}")
        return to_speech(out.get("result") or "Ik heb geen antwoord gekregen.")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default access log
        pass

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "model": MODEL})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path != "/conversation":
            self._reply(404, {"error": "not found"})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            self._reply(400, {"error": "bad json"})
            return
        conv = req.get("conversation_id") or f"anon-{time.time()}"
        context = (
            f"[Home Assistant Assist | gebruiker: {req.get('user') or 'onbekend'}"
            f" | apparaat: {req.get('device') or 'onbekend'} | taal: {req.get('language') or 'nl'}"
            f" | tijd: {time.strftime('%Y-%m-%d %H:%M')}]"
        )
        speech = ask_claude(req.get("text", ""), conv, context)
        self._reply(200, {"speech": speech, "conversation_id": conv})


if __name__ == "__main__":
    log(f"bridge listening on :{PORT} model={MODEL} timeout={TIMEOUT}s")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
