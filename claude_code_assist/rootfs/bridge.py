"""HTTP bridge between the Home Assistant conversation agent and one long-running Claude Code session.

A single `claude -p --input-format stream-json` process stays alive and receives every Assist request as the next
message of one continuous conversation. The session id is persisted, so the conversation survives app restarts
(the process is restarted with --resume). Once a day the conversation is compacted with /compact.

The first result after a request is the Assist answer. Claude Code can keep working afterwards (background agents,
monitors) and later produce more results on its own. With /conversation/stream those are streamed into the same Assist
reply while the background work runs (up to FOLLOWUP_WAIT seconds). Results nobody is waiting for any more are
delivered to the device that asked: spoken on a voice satellite, else a push notification to its mobile app (or the
user's mobile apps), else a persistent notification. Each also fires a `claude_code_assist_followup` event.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid
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
DEBUG = os.environ.get("CCA_DEBUG") == "1"
FOLLOWUP_WAIT = int(os.environ.get("CCA_FOLLOWUP_WAIT", "240"))  # HA ends Assist runs from the app after 300 s
TOOLS = "Bash Read Edit Write Glob Grep WebFetch WebSearch TodoWrite Agent Monitor"
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
    """Send a result that arrived after the Assist reply closed to the device that asked."""
    text = to_speech(text)
    if not text:
        return
    targets = requester.get("followup") or {}
    log(f"follow-up to {targets or 'persistent notification'}: {text[:100]!r}")
    calls = [("services/assist_satellite/announce", {"entity_id": ent, "message": text})
             for ent in targets.get("announce", [])]
    for service in targets.get("notify", []):
        domain, _, name = service.partition(".")
        calls.append((f"services/{domain}/{name}", {
            "title": "Claude Code", "message": text,
            "data": {"tag": "claude_code_assist", "group": "claude_code_assist"},
        }))
    if not calls:
        calls = [("services/persistent_notification/create", {"title": "Claude Code", "message": text})]
    calls.append(("events/claude_code_assist_followup",
                  {"text": text, "user": requester.get("user"), "device": requester.get("device")}))
    for path, body in calls:
        try:
            ha_call(path, body)
        except Exception as err:  # noqa: BLE001 - delivery is best effort
            log(f"follow-up via {path} failed: {err}")


def owner_key(owner: dict | None) -> tuple | None:
    """Follow-ups are routed per user and device, independent of which request is still open."""
    return (owner.get("user"), owner.get("device")) if owner else None


def owner_name(owner: dict | None) -> str:
    return (owner or {}).get("user") or "unknown"


class ClaudeSession:
    """One persistent Claude Code process shared by all users.

    A reader thread consumes the output continuously. Each request waits for the result of its own (replayed)
    message. Background tasks are owned by whoever's turn started them; when they finish, Claude Code reports them
    in a turn of its own (routed to the task's owner) or folds the notification into whatever turn is running. In
    the latter case the bridge asks Claude separately to report the result to the owner (a "report job").
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()  # one message to Claude Code at a time
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None
        self.state = load_state()
        self.busy = False  # Claude Code is in the middle of a turn
        self.waiter: dict | None = None  # request waiting for the result of its message
        self.turn_owner: dict | None = None  # owner of the turn in progress (None: spontaneous/unknown)
        self.turn_req: str | None = None  # request (message uuid) whose turn is in progress
        self.tasks: dict = {}  # task_id -> {"owner", "req", "root", "desc"}; kept, ids can notify more than once
        self.running: set = set()  # root background tasks still running (from background_tasks_changed)
        self.notified: list = []  # roots that notified since the current turn started (for the next turn)
        self.turn_roots: list = []  # roots that notified before the current turn started (what woke it up)
        self.folded: dict = {}  # root -> result text, notifications injected into the turn in progress
        self.listeners: dict = {}  # owner key -> list of pending texts, for an open Assist reply
        self.jobs: list = []  # report jobs: {"owner", "root", "fallback", "not_before", "if_notified"}
        self.restart_pending = False
        threading.Thread(target=self._job_worker, daemon=True).start()

    # ---------- process ----------
    def _start(self) -> None:
        cmd = [
            "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--replay-user-messages",  # echoes our messages, so we know which result answers which question
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
        with self.cond:
            self.busy = False
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self.proc.stderr,), daemon=True).start()

    @staticmethod
    def _drain_stderr(stream) -> None:
        for line in stream:
            log(f"stderr: {line.rstrip()[:300]}")

    def _kill(self, reason: str = "") -> None:
        """Stop the process. Owners of background tasks that were still running are told they were interrupted."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None
        with self.cond:
            roots = {self._root(r) for r in self.running if r in self.tasks}
            interrupted = [self.tasks[r] for r in roots if r in self.tasks]
            self.busy = False
            self.waiter = None
            self.turn_owner = self.turn_req = None
            self.restart_pending = False
            self.tasks, self.running, self.notified, self.folded, self.jobs = {}, set(), [], {}, []
            self.turn_roots = []
            self.cond.notify_all()
        for task in interrupted:
            self._route(f'Your background task "{task["desc"]}" was interrupted because Claude Code was restarted'
                        f'{" (" + reason + ")" if reason else ""}. Please ask again if you still need it.',
                        [task["owner"]], final=True)

    # ---------- reading ----------
    def _root(self, task_id: str) -> str:
        seen = set()
        while task_id in self.tasks and self.tasks[task_id]["root"] != task_id and task_id not in seen:
            seen.add(task_id)
            task_id = self.tasks[task_id]["root"]
        return task_id

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, sub = event.get("type"), event.get("subtype")
            if DEBUG and (kind == "result" or sub in ("init", "task_started", "task_notification",
                                                      "background_tasks_changed") or event.get("isReplay")):
                log("event " + json.dumps({k: event.get(k) for k in ("type", "subtype", "task_id", "status",
                                           "parent_task_id", "is_backgrounded", "uuid", "origin") if event.get(k)}
                                          | ({"tasks": [t.get("task_id") for t in event.get("tasks") or []]}
                                             if sub == "background_tasks_changed" else {})
                                          | ({"result": (event.get("result") or "")[:60]} if kind == "result" else {})))
            with self.cond:
                if kind == "system" and sub == "init":
                    # Notifications so far are what this turn reacts to; later ones belong to a later turn.
                    self.busy = True
                    self.turn_roots, self.notified = self.turn_roots + self.notified, []
                elif kind == "user" and event.get("isReplay"):
                    self._on_replay(event)
                elif kind == "system" and sub == "task_started":
                    self._on_task_started(event)
                elif kind == "system" and sub == "task_notification":
                    root = self._root(event.get("task_id", ""))
                    if root in self.tasks and root not in self.notified:
                        self.notified.append(root)
                elif kind == "system" and sub == "background_tasks_changed":
                    ids = {t.get("task_id") for t in event.get("tasks") or []}
                    self.running = {self._root(i) for i in ids}  # a subagent's own tasks count as its root task
                self.cond.notify_all()
            if kind == "result":
                self._on_result(event)
        with self.cond:  # process ended
            self.busy = False
            if self.waiter and self.proc is proc:
                self.waiter["dead"] = True
                self.waiter["done"].set()
            self.cond.notify_all()

    def _on_replay(self, event: dict) -> None:
        if self.waiter and event.get("uuid") == self.waiter["uuid"]:
            self.waiter["acked"] = True
            self.turn_owner = self.waiter["owner"]
            self.turn_req = self.waiter["uuid"]
            return
        if (event.get("origin") or {}).get("kind") == "task-notification":
            content = event.get("message", {}).get("content")
            if not isinstance(content, str):
                content = " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
            m = re.search(r"<task-id>([^<]+)</task-id>", content)
            if m:
                root = self._root(m.group(1).strip())
                r = re.search(r"<result>(.*?)</result>", content, flags=re.S)
                self.folded[root] = r.group(1).strip() if r else ""

    def _on_task_started(self, event: dict) -> None:
        task_id, parent = event.get("task_id"), event.get("parent_task_id")
        if not task_id:
            return
        if parent:
            root = self._root(parent)
            owner, req = self.tasks.get(root, {}).get("owner"), self.tasks.get(root, {}).get("req")
        elif event.get("is_backgrounded"):
            root = task_id
            owner, req = self.turn_owner, self.turn_req
            wakers = self.turn_roots or self.notified
            if owner is None and wakers:  # a spontaneous turn: it acts for the task that woke it up
                waker = self.tasks.get(wakers[0], {})
                owner, req = waker.get("owner"), waker.get("req")
        else:
            return  # foreground tool call inside a turn
        self.tasks[task_id] = {"owner": owner, "req": req, "root": root,
                               "desc": event.get("description") or "background task"}

    def _on_result(self, event: dict) -> None:
        if event.get("session_id") and event["session_id"] != self.state.get("session_id"):
            self.state["session_id"] = event["session_id"]
            save_state(self.state)
        text, ok = event.get("result") or "", not event.get("is_error")
        routes: list = []
        with self.cond:
            self.busy = False
            waiter = self.waiter if self.waiter and self.waiter["acked"] else None
            turn_owner = self.turn_owner
            folded, self.folded = self.folded, {}
            self.turn_owner = self.turn_req = None
            if waiter:
                self.waiter = None
                waiter["result"] = (text, ok)
                waiter["done"].set()
                # Notifications from just before this user's turn may or may not have been answered in it;
                # handle them like notifications that arrived during the turn.
                self.notified, self.turn_roots = self.turn_roots + self.notified, []
                # Someone else spoke while these tasks ran: Claude's own report turn may address the wrong person.
                for root in self.running:
                    task = self.tasks.get(root)
                    if task and owner_key(task["owner"]) != owner_key(turn_owner):
                        task["interleaved"] = True
            else:
                # Spontaneous turn: it reports the tasks that notified since the last result. If another user asked
                # something while a task ran, its text is not trusted for that owner; the bridge asks explicitly.
                roots, self.turn_roots = self.turn_roots, []
                owners = {}
                for root in roots:
                    task = self.tasks.get(root, {})
                    finished = root not in self.running
                    if task.get("interleaved"):
                        if finished:
                            self.jobs.append({"owner": task.get("owner"), "root": root, "fallback": text,
                                              "not_before": 0, "if_notified": False})
                        continue
                    owners[owner_key(task.get("owner"))] = (task.get("owner"), finished)
                if not roots and (self.running or self.notified):
                    # Woken by a running task without a notification (e.g. a Monitor output line): an interim
                    # update. Only attributable if a single person has background work running (or just finished;
                    # that finish is reported in the next turn).
                    candidates = {owner_key(self.tasks.get(r, {}).get("owner")): self.tasks.get(r, {}).get("owner")
                                  for r in self.running | set(self.notified)}
                    if len(candidates) == 1 and text.strip():
                        routes.append((text, list(candidates.values()), False))
                    elif text.strip():
                        log(f"interim update with several possible owners dropped: {text[:80]!r}")
                elif text.strip() and (owners or not roots):
                    final = any(f for _, f in owners.values()) if owners else True
                    routes.append((text, [o for o, _ in owners.values()] or [None], final))
            # Notifications folded into this turn: the owner gets a separate report, unless it was their turn.
            for root, fallback in folded.items():
                if root in self.notified:
                    self.notified.remove(root)
                owner = self.tasks.get(root, {}).get("owner")
                if waiter and owner_key(owner) == owner_key(turn_owner):
                    continue
                self.jobs.append({"owner": owner, "root": root, "fallback": fallback,
                                  "not_before": 0, "if_notified": False})
            # Notified during a user's turn but not folded: normally Claude Code reports it in a turn of its own
            # right after. If that does not happen within a few seconds, ask for the report.
            if waiter:
                for root in self.notified:
                    self.jobs.append({"owner": self.tasks.get(root, {}).get("owner"), "root": root,
                                      "fallback": "", "not_before": time.time() + 10, "if_notified": True})
            self.cond.notify_all()
        for text_, owners_, final_ in routes:
            self._route(text_, owners_, final_)
        self._maybe_restart()

    # ---------- routing ----------
    def _route(self, text: str, owners: list, final: bool) -> None:
        """Into the owner's open Assist reply if there is one; otherwise, if final, a notification/announcement."""
        if not text.strip():
            return
        for owner in owners:
            with self.cond:
                listener = self.listeners.get(owner_key(owner))
                if listener is not None:
                    listener.append(text)
                    self.cond.notify_all()
                    continue
            if final:
                threading.Thread(target=deliver_followup, args=(text, owner or {}), daemon=True).start()
            else:
                log(f"interim update for {owner_name(owner)} dropped (no open reply): {text[:80]!r}")

    def _request_active(self, req: str) -> bool:
        """Is work started by this request (its background tasks, their reports) still going on?

        Only the request's own tasks count, so a long-running monitor started earlier does not keep every later
        reply of the same user open.
        """
        def of_req(root: str) -> bool:
            return self.tasks.get(root, {}).get("req") == req
        if any(of_req(r) for r in self.running) or any(of_req(r) for r in self.notified + self.turn_roots):
            return True
        return any(of_req(j["root"]) for j in self.jobs)

    def task_summary(self, for_owner: dict | None) -> str:
        with self.cond:
            items = []
            for root in sorted(self.running):
                task = self.tasks.get(root)
                if task:
                    who = owner_name(task["owner"])
                    if owner_key(task["owner"]) == owner_key(for_owner):
                        who += ", the current user"
                    items.append(f'"{task["desc"]}" (started by {who})')
        return "; ".join(items)

    # ---------- asking ----------
    def ask(self, text: str, timeout: int = TIMEOUT, owner: dict | None = None) -> tuple[str, bool]:
        """Send one message and wait for the result of its turn. Returns (text, ok)."""
        with self.lock:
            for attempt in (1, 2):
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                with self.cond:  # let a turn Claude Code started on its own finish first
                    self.cond.wait_for(lambda: not self.busy, timeout=timeout)
                    waiter = {"done": threading.Event(), "result": None, "dead": False,
                              "uuid": str(uuid.uuid4()), "acked": False, "owner": owner}
                    self.waiter = waiter
                    if owner is not None:
                        owner["req"] = waiter["uuid"]
                msg = {"type": "user", "uuid": waiter["uuid"],
                       "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
                try:
                    self.proc.stdin.write(json.dumps(msg) + "\n")
                    self.proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._kill("it stopped responding")
                    continue
                if not waiter["done"].wait(timeout):
                    log(f"timeout after {timeout}s, restarting session")
                    self._kill("a request timed out")
                    return "That took too long, so I stopped. Please try again or ask something shorter.", False
                if waiter["dead"]:  # process died; resume it and retry once
                    log("Claude Code exited, resuming")
                    self._kill("it exited unexpectedly")
                    continue
                return waiter["result"]
            return "Claude Code is not responding. Check the log of the Claude Code Assist app.", False

    def _job_worker(self) -> None:
        """Ask Claude to report background results that ended up in someone else's turn."""
        while True:
            with self.cond:
                self.cond.wait_for(lambda: any(j["not_before"] <= time.time() for j in self.jobs), timeout=2)
                due = [j for j in self.jobs if j["not_before"] <= time.time()]
                if not due:
                    continue
                job = due[0]
                self.jobs.remove(job)
                if job["if_notified"]:
                    if job["root"] not in self.notified:
                        continue  # a spontaneous turn reported it in the meantime
                    self.notified.remove(job["root"])
                task = self.tasks.get(job["root"], {})
            owner = job["owner"]
            who = owner_name(owner)
            prompt = (
                f"[Claude Code Assist bridge | report for: {who} | device: {(owner or {}).get('device') or 'unknown'}"
                f" | time: {time.strftime('%Y-%m-%d %H:%M')}]\n"
                f'The background task "{task.get("desc", "background task")}" that {who} started has finished. '
                f"Report its outcome to {who} now in one short message, in the language {who} used when asking. "
                f"This message is delivered only to {who}."
            )
            log(f"report job for {who}: {task.get('desc')!r}")
            text, ok = self.ask(prompt, owner=dict(owner) if owner else None)
            if not ok or not text.strip():
                text = job["fallback"]
            self._route(text, [owner], final=True)

    def follow(self, owner: dict, emit, max_wait: int = FOLLOWUP_WAIT) -> None:
        """After an answer: pass results that belong to this owner to emit() while their work is still going on.

        Stops when nothing of this owner is running, after max_wait seconds, when the same owner opens a new reply,
        or when emit() raises (client gone). Later results become notifications/announcements.
        """
        key = owner_key(owner)
        req = owner.get("req")
        mine: list = []
        with self.cond:
            self.listeners[key] = mine
        deadline = time.time() + max_wait
        had_work = False
        try:
            while True:
                with self.cond:
                    active = lambda: self._request_active(req)  # noqa: E731
                    had_work = had_work or active()
                    self.cond.wait_for(lambda: mine or self.listeners.get(key) is not mine or not active(),
                                       timeout=15)
                    had_work = had_work or active()
                    if had_work and not mine and self.listeners.get(key) is mine and not active():
                        # A finished task is followed by a reporting turn a moment later; give it time to start.
                        self.cond.wait_for(lambda: mine or self.listeners.get(key) is not mine or active(),
                                           timeout=8)
                    pending, mine[:] = list(mine), []
                    finished = self.listeners.get(key) is not mine or not active()
                for text in pending:
                    emit({"type": "text", "text": to_speech(text)})
                if finished or time.time() > deadline:
                    return
                emit({"type": "ping"})  # keeps the connection alive and detects a closed client
        finally:
            with self.cond:
                if self.listeners.get(key) is mine:
                    del self.listeners[key]
                leftover, mine[:] = list(mine), []
            for text in leftover:
                threading.Thread(target=deliver_followup, args=(text, owner), daemon=True).start()

    # ---------- maintenance ----------
    def request_restart(self) -> None:
        """Restart to reload instructions, as soon as no turn and no background task is running."""
        with self.cond:
            self.restart_pending = True
        self._maybe_restart()

    def _maybe_restart(self) -> None:
        with self.cond:
            if (not self.restart_pending or self.busy or self.running or self.waiter or self.jobs or self.notified
                    or self.turn_roots):
                return
            self.restart_pending = False
        threading.Thread(target=self._restart_now, daemon=True).start()

    def _restart_now(self) -> None:
        with self.lock:
            self._kill()
        log("session restarted, instructions reloaded on next message")

    def restart_forced(self) -> None:
        with self.lock:
            self._kill("the instructions were changed")
        log("session restarted now (forced), running background tasks were interrupted")

    def compact(self) -> None:
        started = time.time()
        _, ok = self.ask("/compact", timeout=600)
        self.state["last_compact"] = datetime.now().isoformat(timespec="seconds")
        save_state(self.state)
        log(f"/compact done in {time.time() - started:.0f}s ok={ok}")


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
    """Instructions changed: restart once idle. New conversation: stop now (running tasks are reported)."""
    if not new_conversation:
        SESSION.request_restart()
        log("instructions saved, Claude Code restarts once it is idle")
        return
    with SESSION.lock:
        SESSION._kill("a new conversation was started")
        SESSION.state.pop("session_id", None)
        save_state(SESSION.state)
    log("session reset, next message starts a new conversation")


def status() -> dict:
    return {
        "ok": True, "model": MODEL, "effort": EFFORT or "default",
        "session_id": SESSION.state.get("session_id"),
        "last_compact": SESSION.state.get("last_compact"),
        "compact_hour": COMPACT_HOUR,
        "running": SESSION.proc is not None and SESSION.proc.poll() is None,
        "background_tasks": SESSION.task_summary(None) or None,
        "background_task_count": len(SESSION.running),
        "restart_pending": SESSION.restart_pending,
    }


def request_context(req: dict, owner: dict) -> str:
    tasks = SESSION.task_summary(owner)
    return (
        f"[Home Assistant Assist | user: {req.get('user') or 'unknown'}"
        f" | device: {req.get('device') or 'unknown'} | language: {req.get('language') or 'unknown'}"
        f" | time: {time.strftime('%Y-%m-%d %H:%M')}"
        + (f" | running background tasks: {tasks}" if tasks else "")
        + "]"
    )


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
        elif path == "/restart":
            SESSION.restart_forced()
            self._reply(200, {"ok": True})
        elif path == "/reset":
            restart_session(new_conversation=True)
            self._reply(200, {"ok": True})
        elif path == "/conversation/stream" and not self._from_ingress():
            self._stream_conversation(req)
        elif path == "/conversation" and not self._from_ingress():
            owner = {"user": req.get("user"), "device": req.get("device"), "followup": req.get("followup") or {}}
            started = time.time()
            text, ok = SESSION.ask(f"{request_context(req, owner)}\n{req.get('text', '')}", owner=owner)
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

        owner = {"user": req.get("user"), "device": req.get("device"), "followup": req.get("followup") or {}}
        started = time.time()
        text, ok = SESSION.ask(f"{request_context(req, owner)}\n{req.get('text', '')}", owner=owner)
        log(f"{time.time() - started:.1f}s ok={ok} device={req.get('device')!r} q={req.get('text', '')[:80]!r}")
        try:
            emit({"type": "text", "text": to_speech(text) or "I did not get an answer."})
            if ok and req.get("follow", True):
                SESSION.follow(owner, emit)
            emit({"type": "done"})
        except (BrokenPipeError, ConnectionResetError):
            log("stream closed by client; further results become notifications")


if __name__ == "__main__":
    log(f"bridge listening on :{PORT} model={MODEL} effort={EFFORT or 'default'} timeout={TIMEOUT}s compact_hour={COMPACT_HOUR}")
    threading.Thread(target=compact_scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
