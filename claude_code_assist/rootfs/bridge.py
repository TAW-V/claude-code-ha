"""HTTP bridge between the Home Assistant conversation agent and one long-running Claude Code session.

A single `claude -p --input-format stream-json` process stays alive and receives every Assist request as the next
message of one continuous conversation. The session id is persisted, so the conversation survives app restarts
(the process is restarted with --resume). Once a day the conversation is compacted with /compact.

The first result after a request is the Assist answer. Claude Code can keep working afterwards (background agents,
monitors) and later produce more results on its own. With /conversation/stream those are streamed into the same Assist
reply while the background work runs (up to FOLLOWUP_WAIT seconds). Results nobody is waiting for any more are
delivered as a notification and a `claude_code_assist_followup` event in Home Assistant.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
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
FOLLOWUP_WAIT = int(os.environ.get("CCA_FOLLOWUP_WAIT", "600"))
FOLLOWUP_NOTIFY = [s.strip() for s in os.environ.get("CCA_FOLLOWUP_NOTIFY", "").split(",") if s.strip()]
TOOLS = "Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite Agent"
HA_API = "http://supervisor/core/api"


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


def ha_call(path: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{HA_API}/{path}", method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
                 "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15).read()


def deliver_followup(text: str, requester: dict) -> None:
    """Send a result that arrived after the Assist answer to Home Assistant."""
    text = to_speech(text)
    if not text:
        return
    log(f"follow-up: {text[:100]!r}")
    try:
        ha_call("events/claude_code_assist_followup", {"text": text, **requester})
    except Exception as err:  # noqa: BLE001 - delivery is best effort
        log(f"follow-up event failed: {err}")
    targets = FOLLOWUP_NOTIFY or ["persistent_notification.create"]
    for target in targets:
        domain, _, service = target.partition(".")
        data = {"title": "Claude Code", "message": text}
        if domain == "notify":
            data["data"] = {"tag": "claude_code_assist", "group": "claude_code_assist"}
        try:
            ha_call(f"services/{domain}/{service}", data)
        except Exception as err:  # noqa: BLE001
            log(f"follow-up via {target} failed: {err}")


class ClaudeSession:
    """One persistent Claude Code process. A reader thread consumes its output continuously."""

    def __init__(self) -> None:
        self.lock = threading.Lock()  # one request at a time
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.state = load_state()
        self.busy = False  # Claude Code is in the middle of a turn
        self.waiter: dict | None = None  # request waiting for its first result
        self.requester: dict = {}  # who asked last; follow-ups are attributed to them
        self.bg_tasks: list = []  # background tasks reported by Claude Code
        self.listener: list | None = None  # open stream that wants follow-up results

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
        self.busy = False
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                with self.cond:
                    self.busy = True
            elif kind == "system" and event.get("subtype") == "background_tasks_changed":
                with self.cond:
                    self.bg_tasks = event.get("tasks") or []
                    self.cond.notify_all()
            elif kind == "result":
                self._on_result(event)
        with self.cond:  # process ended
            self.busy = False
            if self.waiter and self.proc is proc:
                self.waiter["dead"] = True
                self.waiter["done"].set()
            self.cond.notify_all()

    def _on_result(self, event: dict) -> None:
        if event.get("session_id") and event["session_id"] != self.state.get("session_id"):
            self.state["session_id"] = event["session_id"]
            save_state(self.state)
        text, ok = event.get("result") or "", not event.get("is_error")
        with self.cond:
            self.busy = False
            waiter, self.waiter = self.waiter, None
            listener = self.listener if not waiter else None
            if waiter:
                waiter["result"] = (text, ok)
                waiter["done"].set()
            elif listener is not None:
                listener.append(text)
            self.cond.notify_all()
        if not waiter and listener is None:
            threading.Thread(target=deliver_followup, args=(text, dict(self.requester)), daemon=True).start()

    @staticmethod
    def _drain_stderr(stream) -> None:
        for line in stream:
            log(f"stderr: {line.rstrip()[:300]}")

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None
        with self.cond:
            self.busy = False
            self.waiter = None
            self.bg_tasks = []
            self.cond.notify_all()

    def ask(self, text: str, timeout: int = TIMEOUT, requester: dict | None = None) -> tuple[str, bool]:
        """Send one message and wait for its first result. Returns (text, ok)."""
        with self.lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                self.end_stream()  # a new question ends waiting for the previous one's follow-ups
                with self.cond:  # let a turn Claude Code started on its own finish first (it becomes a follow-up)
                    self.cond.wait_for(lambda: not self.busy, timeout=timeout)
                    waiter = {"done": threading.Event(), "result": None, "dead": False}
                    self.waiter = waiter
                if requester is not None:
                    self.requester = requester
                msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
                try:
                    self.proc.stdin.write(json.dumps(msg) + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._kill()
                    continue
                if not waiter["done"].wait(timeout):
                    log(f"timeout after {timeout}s, restarting session")
                    self._kill()
                    return "That took too long, so I stopped. Please try again or ask something shorter.", False
                if waiter["dead"]:  # process died; resume it and retry once
                    log("Claude Code exited, resuming")
                    self._kill()
                    continue
                return waiter["result"]
            return "Claude Code is not responding. Check the log of the Claude Code Assist app.", False

    def follow(self, emit, max_wait: int = FOLLOWUP_WAIT) -> None:
        """After an answer: pass later results to emit() while Claude Code is still working.

        Stops when no turn and no background task is running, after max_wait seconds, when a new question arrives,
        or when emit() raises (client gone). Results after that become notifications.
        """
        mine: list = []
        with self.cond:
            self.listener = mine
        deadline = time.time() + max_wait
        had_bg = False  # only wait for a closing turn if background work was running
        try:
            while True:
                with self.cond:
                    had_bg = had_bg or bool(self.bg_tasks)
                    self.cond.wait_for(lambda: mine or self.listener is not mine
                                       or not (self.busy or self.bg_tasks), timeout=15)
                    had_bg = had_bg or bool(self.bg_tasks)
                    if had_bg and not mine and self.listener is mine and not (self.busy or self.bg_tasks):
                        # A finished background task is followed by a new turn a moment later; give it time to start.
                        self.cond.wait_for(lambda: mine or self.listener is not mine or self.busy or self.bg_tasks,
                                           timeout=8)
                    pending, mine[:] = list(mine), []
                    finished = self.listener is not mine or not (self.busy or self.bg_tasks)
                for text in pending:
                    emit({"type": "text", "text": to_speech(text)})
                    # HA keeps the stream open even if the chat was closed, so also notify when configured.
                    if FOLLOWUP_NOTIFY:
                        threading.Thread(target=deliver_followup, args=(text, dict(self.requester)),
                                         daemon=True).start()
                if finished or time.time() > deadline:
                    return
                emit({"type": "ping"})  # keeps the connection alive and detects a closed client
        finally:
            with self.cond:
                if self.listener is mine:
                    self.listener = None
                leftover, mine[:] = list(mine), []
            for text in leftover:
                threading.Thread(target=deliver_followup, args=(text, dict(self.requester)), daemon=True).start()

    def end_stream(self) -> None:
        with self.cond:
            self.listener = None
            self.cond.notify_all()

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
        elif path == "/instructions" and self.headers.get("Authorization") == f"Bearer {TOKEN}":
            with open(INSTRUCTIONS_FILE) as f:
                self._reply(200, {"instructions": f.read()})
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
        elif path == "/conversation/stream" and not self._from_ingress():
            self._stream_conversation(req)
        elif path == "/conversation" and not self._from_ingress():
            context = (
                f"[Home Assistant Assist | user: {req.get('user') or 'unknown'}"
                f" | device: {req.get('device') or 'unknown'} | language: {req.get('language') or 'unknown'}"
                f" | time: {time.strftime('%Y-%m-%d %H:%M')}]"
            )
            started = time.time()
            requester = {"user": req.get("user"), "device": req.get("device")}
            text, ok = SESSION.ask(f"{context}\n{req.get('text', '')}", requester=requester)
            log(f"{time.time() - started:.1f}s ok={ok} q={req.get('text', '')[:80]!r}")
            self._reply(200, {"speech": to_speech(text) or "I did not get an answer.",
                              "conversation_id": req.get("conversation_id")})
        else:
            self._reply(404, {"error": "not found"})


    def _stream_conversation(self, req: dict) -> None:
        """NDJSON stream: the answer first, then follow-ups while Claude Code keeps working in the background."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(obj: dict) -> None:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        context = (
            f"[Home Assistant Assist | user: {req.get('user') or 'unknown'}"
            f" | device: {req.get('device') or 'unknown'} | language: {req.get('language') or 'unknown'}"
            f" | time: {time.strftime('%Y-%m-%d %H:%M')}]"
        )
        requester = {"user": req.get("user"), "device": req.get("device")}
        started = time.time()
        text, ok = SESSION.ask(f"{context}\n{req.get('text', '')}", requester=requester)
        log(f"{time.time() - started:.1f}s ok={ok} q={req.get('text', '')[:80]!r}")
        try:
            emit({"type": "text", "text": to_speech(text) or "I did not get an answer."})
            if ok:
                SESSION.follow(emit)
            emit({"type": "done"})
        except (BrokenPipeError, ConnectionResetError):
            log("stream closed by client; further results become notifications")


if __name__ == "__main__":
    log(f"bridge listening on :{PORT} model={MODEL} effort={EFFORT or 'default'} timeout={TIMEOUT}s compact_hour={COMPACT_HOUR}")
    threading.Thread(target=compact_scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
