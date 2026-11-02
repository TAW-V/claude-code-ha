"""HTTP bridge between the Home Assistant conversation agent and one long-running Claude Code session.

A single `claude -p --input-format stream-json` process stays alive and receives every Assist request as the next
message of one continuous conversation. The session id is persisted, so the conversation survives app restarts
(the process is restarted with --resume). Once a day the conversation is compacted with /compact.
"""

import json
import os
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8099
WORKSPACE = "/data/workspace"
STATE_FILE = "/data/state.json"
TOKEN = open("/data/bridge_token").read().strip()
MODEL = os.environ.get("CCA_MODEL", "sonnet")
TIMEOUT = int(os.environ.get("CCA_TIMEOUT", "180"))
EXTRA = os.environ.get("CCA_EXTRA", "")
EFFORT = os.environ.get("CCA_EFFORT", "")
COMPACT_HOUR = int(os.environ.get("CCA_COMPACT_HOUR", "4"))
TOOLS = "Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite"


def log(msg: str) -> None:
    print(f"[cca] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def to_speech(text: str) -> str:
    """Strip markdown so TTS and the Assist bubble read cleanly."""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.M)
    return text.strip()


class ClaudeSession:
    """One persistent Claude Code process; requests are handled one at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.state = load_state()

    def _start(self) -> None:
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--model", MODEL,
            "--allowedTools", TOOLS,
            "--add-dir", "/config", "/addons", "/share",
        ]
        if EFFORT:
            cmd += ["--effort", EFFORT]
        if EXTRA:
            cmd += ["--append-system-prompt", EXTRA]
        if self.state.get("session_id"):
            cmd += ["--resume", self.state["session_id"]]
        log(f"starting Claude Code (resume={self.state.get('session_id', 'new')})")
        self.proc = subprocess.Popen(
            cmd, cwd=WORKSPACE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,), daemon=True).start()

    @staticmethod
    def _pump(stream, q: queue.Queue) -> None:
        for line in stream:
            q.put(line)
        q.put(None)  # process ended

    @staticmethod
    def _drain_stderr(stream) -> None:
        for line in stream:
            log(f"stderr: {line.rstrip()[:300]}")

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def ask(self, text: str, timeout: int = TIMEOUT) -> tuple[str, bool]:
        """Send one message and wait for its result. Returns (text, ok)."""
        with self.lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
                try:
                    self.proc.stdin.write(json.dumps(msg) + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._kill()
                    continue
                deadline = time.time() + timeout
                while True:
                    try:
                        line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                    except queue.Empty:
                        log(f"timeout after {timeout}s, restarting session")
                        self._kill()
                        return "That took too long, so I stopped. Please try again or ask something shorter.", False
                    if line is None:  # process died; resume it and retry once
                        log(f"Claude Code exited (rc={self.proc.returncode})")
                        self._kill()
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") != "result":
                        continue
                    if event.get("session_id") and event["session_id"] != self.state.get("session_id"):
                        self.state["session_id"] = event["session_id"]
                        save_state(self.state)
                    return event.get("result") or "", not event.get("is_error")
            return "Claude Code is not responding. Check the log of the Claude Code Assist app.", False

    def compact(self) -> None:
        started = time.time()
        _, ok = self.ask("/compact", timeout=600)
        self.state["last_compact"] = datetime.now().isoformat(timespec="seconds")
        save_state(self.state)
        log(f"daily /compact done in {time.time() - started:.0f}s ok={ok}")


SESSION = ClaudeSession()


def compact_scheduler() -> None:
    while True:
        now = datetime.now()
        nxt = now.replace(hour=COMPACT_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        if SESSION.state.get("session_id"):
            SESSION.compact()


INGRESS_PROXY = "172.30.32.2"  # Supervisor ingress; HA already authenticated the (admin) user
INSTRUCTIONS_FILE = os.path.join(WORKSPACE, "CLAUDE.md")
UI_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html"), "rb").read()


def restart_session(new_conversation: bool = False) -> None:
    """Stop the Claude Code process; the next message starts it again (resumed unless new_conversation)."""
    with SESSION.lock:
        SESSION._kill()
        if new_conversation:
            SESSION.state.pop("session_id", None)
            save_state(SESSION.state)
    log("session reset, next message starts a new conversation" if new_conversation
        else "session restarted, instructions reloaded on next message")


def status() -> dict:
    return {
        "ok": True, "model": MODEL, "effort": EFFORT or "default",
        "session_id": SESSION.state.get("session_id"),
        "last_compact": SESSION.state.get("last_compact"),
        "compact_hour": COMPACT_HOUR,
        "running": SESSION.proc is not None and SESSION.proc.poll() is None,
    }


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

    def _from_ingress(self) -> bool:
        return self.client_address[0] == INGRESS_PROXY

    def _body(self) -> dict | None:
        try:
            return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            self._reply(400, {"error": "bad json"})
            return None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._reply(200, status())
        elif self._from_ingress() and path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(UI_HTML)))
            self.end_headers()
            self.wfile.write(UI_HTML)
        elif self._from_ingress() and path == "/api/status":
            with open(INSTRUCTIONS_FILE) as f:
                self._reply(200, {**status(), "instructions": f.read()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        # Web UI (via ingress) uses /api/...; HA integration and scripts use the bearer token.
        if path.startswith("/api/"):
            if not self._from_ingress():
                self._reply(403, {"error": "web UI only via Home Assistant ingress"})
                return
            path = path[4:]
        elif self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"error": "unauthorized"})
            return
        req = self._body()
        if req is None:
            return
        if path == "/instructions":
            text = (req.get("text") or "").replace("\r\n", "\n")
            if not text.strip():
                self._reply(400, {"error": "instructions cannot be empty"})
                return
            with open(INSTRUCTIONS_FILE + ".tmp", "w") as f:
                f.write(text if text.endswith("\n") else text + "\n")
            os.replace(INSTRUCTIONS_FILE + ".tmp", INSTRUCTIONS_FILE)
            restart_session()
            self._reply(200, {"ok": True})
        elif path == "/instructions/default":
            with open("/defaults/CLAUDE.md") as f:
                self._reply(200, {"ok": True, "instructions": f.read()})
        elif path == "/compact":
            threading.Thread(target=SESSION.compact, daemon=True).start()
            self._reply(202, {"ok": True})
        elif path == "/reset":
            restart_session(new_conversation=True)
            self._reply(200, {"ok": True})
        elif path == "/conversation" and not self._from_ingress():
            context = (
                f"[Home Assistant Assist | user: {req.get('user') or 'unknown'}"
                f" | device: {req.get('device') or 'unknown'} | language: {req.get('language') or 'unknown'}"
                f" | time: {time.strftime('%Y-%m-%d %H:%M')}]"
            )
            started = time.time()
            text, ok = SESSION.ask(f"{context}\n{req.get('text', '')}")
            log(f"{time.time() - started:.1f}s ok={ok} q={req.get('text', '')[:80]!r}")
            self._reply(200, {"speech": to_speech(text) or "I did not get an answer.",
                              "conversation_id": req.get("conversation_id")})
        else:
            self._reply(404, {"error": "not found"})


if __name__ == "__main__":
    log(f"bridge listening on :{PORT} model={MODEL} effort={EFFORT or 'default'} timeout={TIMEOUT}s compact_hour={COMPACT_HOUR}")
    threading.Thread(target=compact_scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
