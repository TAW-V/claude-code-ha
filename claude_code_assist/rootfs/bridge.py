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

import glob
import json
import os
import re
import socket
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
AUTO_UPDATE = os.environ.get("CCA_AUTO_UPDATE", "true") == "true"
TASK_MAX_MINUTES = int(os.environ.get("CCA_TASK_MAX_MINUTES", "30"))
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


def ha_request(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{HA_API}/{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
                 "Content-Type": "application/json"},
    )
    data = urllib.request.urlopen(req, timeout=15).read()
    return json.loads(data) if data else None


def ha_call(path: str, body: dict) -> None:
    ha_request("POST", path, body)


_PANEL_PATH: list = []


def panel_path() -> str | None:
    """Sidebar path of this app's panel (/<slug>), so tapping a notification opens it; cached."""
    if not _PANEL_PATH:
        try:
            req = urllib.request.Request(
                "http://supervisor/addons/self/info",
                headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"})
            slug = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"]["slug"]
            _PANEL_PATH.append(f"/{slug}")
        except Exception as err:  # noqa: BLE001
            log(f"panel path lookup failed: {err}")
            return None
    return _PANEL_PATH[0]


# ---------- inbox: results that reached the user outside the chat are shown at the top of their next reply ----------
INBOX_LOCK = threading.Lock()


def inbox_add(user: str | None, text: str) -> None:
    if not user:
        return
    with INBOX_LOCK:
        state = load_state()
        state.setdefault("inbox", {}).setdefault(user, []).append({"ts": time.time(), "text": text})
        save_state(state)
        if "SESSION" in globals():
            SESSION.state["inbox"] = state["inbox"]


INBOX_MAX_AGE = 24 * 3600  # older results are dropped (they are in the chat history anyway)


def inbox_pop(user: str | None) -> list:
    """The user's waiting results as (time HH:MM, text), oldest first; empties the inbox."""
    if not user:
        return []
    with INBOX_LOCK:
        state = load_state()
        items = state.get("inbox", {}).pop(user, [])
        if items:
            save_state(state)
            if "SESSION" in globals():
                SESSION.state["inbox"] = state["inbox"]
    now = time.time()
    return [(time.strftime("%H:%M", time.localtime(i["ts"])), i["text"]) for i in items
            if isinstance(i, dict) and now - i.get("ts", 0) < INBOX_MAX_AGE]


def summary(text: str, limit: int = 180) -> str:
    """Short version for a push notification: whole sentences up to about `limit` characters."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > 60 else cut.rsplit(" ", 1)[0]) + " …"


# ---------- chat history: per user, shown in the panel chat; includes Assist exchanges and results ----------
CHAT_FILE = "/data/chat_history.json"
CHAT_LOCK = threading.Lock()
CHAT_KEEP = 300  # messages per user


def _chat_load() -> dict:
    try:
        with open(CHAT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"next_id": 1, "users": {}}


def chat_add(user: str | None, role: str, text: str, source: str) -> None:
    if not user or not text.strip():
        return
    with CHAT_LOCK:
        data = _chat_load()
        msgs = data["users"].setdefault(user, [])
        msgs.append({"id": data["next_id"], "ts": time.strftime("%Y-%m-%d %H:%M"), "role": role,
                     "text": text.strip(), "source": source})
        data["next_id"] += 1
        data["users"][user] = msgs[-CHAT_KEEP:]
        with open(CHAT_FILE + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(CHAT_FILE + ".tmp", CHAT_FILE)


def chat_since(user: str, since: int) -> list:
    with CHAT_LOCK:
        return [m for m in _chat_load()["users"].get(user, []) if m["id"] > since]


def chat_clear(user: str) -> None:
    with CHAT_LOCK:
        data = _chat_load()
        data["users"].pop(user, None)
        with open(CHAT_FILE + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(CHAT_FILE + ".tmp", CHAT_FILE)


PANEL_DEVICE = "Claude Code panel"
PANEL_BUSY: dict = {}  # user -> number of panel messages waiting for an answer


# ---------- temporary automations ----------
TMP_ALIAS = re.compile(r"^\s*TMP until (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})\s*\|")


def cleanup_temporary_automations() -> list:
    """Delete automations whose alias starts with 'TMP until YYYY-MM-DD HH:MM |' and whose time has passed."""
    removed = []
    now = datetime.now()
    for state in ha_request("GET", "states") or []:
        if not state["entity_id"].startswith("automation."):
            continue
        name = state["attributes"].get("friendly_name") or ""
        m = TMP_ALIAS.match(name)
        auto_id = state["attributes"].get("id")
        if not m or not auto_id:
            continue
        try:
            until = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if until > now:
            continue
        try:
            ha_request("DELETE", f"config/automation/config/{auto_id}")
            removed.append(name)
            log(f"removed temporary automation {name!r} ({state['entity_id']})")
        except Exception as err:  # noqa: BLE001
            log(f"could not remove temporary automation {name!r}: {err}")
    if removed:
        try:
            ha_call("events/claude_code_assist_cleanup", {"removed": removed})
        except Exception:  # noqa: BLE001
            pass
    return removed


def deliver_followup(text: str, requester: dict) -> None:
    """Send a result that arrived after the Assist reply closed to the device that asked."""
    if requester.get("task"):
        log(f"late result for task {requester['task']}: {text[:100]!r}")
        try:
            ha_call("events/claude_code_assist_task_late_result", {"run_id": requester["task"], "result": text})
        except Exception as err:  # noqa: BLE001
            log(f"task event failed: {err}")
        return
    text = to_speech(text)
    if not text:
        return
    targets = requester.get("followup") or {}
    log(f"follow-up to {targets or 'persistent notification'}: {text[:100]!r}")
    calls = [("services/assist_satellite/announce", {"entity_id": ent, "message": text})
             for ent in targets.get("announce", [])]
    if not targets.get("announce"):
        # Not heard on a speaker: the full text is shown at the top of the user's next Assist reply.
        inbox_add(requester.get("user"), text)
    for service in targets.get("notify", []):
        domain, _, name = service.partition(".")
        short = summary(text)
        data = {"tag": "claude_code_assist", "group": "claude_code_assist"}
        if panel := panel_path():
            data |= {"clickAction": panel, "url": panel}  # Android / iOS: tap opens the Claude Code panel
        calls.append((f"services/{domain}/{name}", {
            "title": "Claude Code", "message": short + "\n(Send any message in Assist to see the full answer.)",
            "data": data,
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
        self.listener_reqs: dict = {}  # owner key -> requests whose work that open reply is waiting for
        # owner key -> {"reqs", "items"}: work and results of a reply that was closed because a new question arrived
        # from the same chat, kept until that question's reply takes them over.
        self.handover: dict = {}
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
            "--add-dir", "/config", "/addons", "/share", "/media",
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
                    task_id = event.get("task_id", "")
                    root = self._root(task_id)
                    # A subagent's own background task (a Bash command, a nested agent) finishing is not the end of
                    # the work: only the root task's notification is, or a late child of a root that already ended.
                    if (root in self.tasks and root not in self.notified
                            and (task_id == root or root not in self.running)):
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
            if owner and not owner.get("task") and (final or owner.get("panel")):
                chat_add(owner.get("user"), "assistant", to_speech(text) if not owner.get("panel") else text, "result")
            if owner and owner.get("panel"):
                continue  # the panel chat shows it from the history
            with self.cond:
                listener = self.listeners.get(owner_key(owner))
                if listener is None and owner_key(owner) in self.handover:
                    listener = self.handover[owner_key(owner)]["items"]
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

    def user_work(self, user: str) -> list:
        """Descriptions of this user's background work that has not been reported yet (running or being reported)."""
        with self.cond:
            roots = list(self.running) + self.notified + self.turn_roots + [j["root"] for j in self.jobs]
            out = []
            for root in roots:
                task = self.tasks.get(root)
                if task and (task.get("owner") or {}).get("user") == user and task["desc"] not in out:
                    out.append(task["desc"])
            return out

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
                    if job["root"] in self.running:
                        continue  # still running after all; its own notification will follow
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

    def follow(self, owner: dict, emit, alive=lambda: True, max_wait: int = FOLLOWUP_WAIT) -> None:
        """After an answer: pass results that belong to this owner to emit() while their work is still going on.

        Stops when nothing of this owner is running, after max_wait seconds, when the same owner opens a new reply,
        or when the client is gone. A text is only emitted after alive() confirms the client is still connected (the
        first write to a closed socket usually succeeds silently); texts that cannot be emitted become
        notifications/announcements, like later results.
        """
        key = owner_key(owner)
        with self.cond:
            # Take over the work (and any results already in) of the reply this question closed, so results of
            # earlier questions appear under the newest answer instead of being pushed.
            handed = self.handover.pop(key, None) or {"reqs": set(), "items": []}
            reqs = {owner.get("req")} | handed["reqs"]
            mine: list = list(handed["items"])
            self.listeners[key] = mine
            self.listener_reqs[key] = reqs
            self.cond.notify_all()
        deadline = time.time() + max_wait
        had_work = False
        undelivered: list = []
        try:
            while True:
                with self.cond:
                    active = lambda: any(self._request_active(r) for r in reqs)  # noqa: E731
                    had_work = had_work or active()
                    self.cond.wait_for(lambda: mine or self.listeners.get(key) is not mine or not active(),
                                       timeout=15)
                    had_work = had_work or active()
                    if self.listeners.get(key) is not mine:
                        # Closed because a new question came in from this chat: hand pending results over to it.
                        if key in self.handover:
                            self.handover[key]["items"].extend(mine)
                            mine[:] = []
                        return
                    if had_work and not mine and self.listeners.get(key) is mine and not active():
                        # A finished task is followed by a reporting turn a moment later; give it time to start.
                        self.cond.wait_for(lambda: mine or self.listeners.get(key) is not mine or active(),
                                           timeout=8)
                    pending, mine[:] = list(mine), []
                    finished = self.listeners.get(key) is not mine or not active()
                for i, text in enumerate(pending):
                    if not alive():
                        undelivered.extend(pending[i:])
                        return
                    try:
                        emit({"type": "text", "text": to_speech(text)})
                    except OSError:
                        undelivered.extend(pending[i:])
                        raise
                if finished or time.time() > deadline:
                    return
                if not alive():
                    return
                emit({"type": "ping"})  # keeps the connection alive and detects a closed client
        finally:
            with self.cond:
                if self.listeners.get(key) is mine:
                    del self.listeners[key]
                    self.listener_reqs.pop(key, None)
                leftover, mine[:] = undelivered + list(mine), []
            for text in leftover:
                threading.Thread(target=deliver_followup, args=(text, owner), daemon=True).start()

    def take_over(self, owner: dict) -> None:
        """A new question from this chat: close its open reply right away (before the new answer streams; the
        Companion app mixes up streamed text of overlapping replies) and keep its work for the new reply."""
        key = owner_key(owner)
        with self.cond:
            if key not in self.listeners:
                return
            self.listeners.pop(key)
            handed = self.handover.setdefault(key, {"reqs": set(), "items": []})
            handed["reqs"] |= self.listener_reqs.pop(key, set())
            self.cond.notify_all()

    def release_handover(self, owner: dict) -> None:
        """The new question did not open a reply after all: results kept for it become notifications."""
        with self.cond:
            handed = self.handover.pop(owner_key(owner), None)
        for text in (handed or {}).get("items", []):
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
        try:
            update_claude()
        except Exception as err:  # noqa: BLE001
            log(f"Claude Code update failed: {err}")
        try:
            cleanup_temporary_automations()
        except Exception as err:  # noqa: BLE001
            log(f"temporary automation cleanup failed: {err}")
        if SESSION.state.get("session_id"):
            SESSION.compact()


# ---------- Claude Code updates ----------
def update_claude() -> None:
    """Update the Claude Code binary; roll back if the new version does not start. Applied by a deferred restart."""
    if not AUTO_UPDATE:
        return
    link = os.path.join(os.environ.get("HOME", "/data/home"), ".local", "bin", "claude")
    if not os.path.islink(link):
        log("Claude Code update skipped: not a native install")
        return
    old = os.path.realpath(link)
    env = {k: v for k, v in os.environ.items() if k != "DISABLE_AUTOUPDATER"}
    out = subprocess.run([link, "update"], env=env, capture_output=True, text=True, timeout=900)
    new = os.path.realpath(link)
    if new == old:
        log(f"Claude Code is up to date ({os.path.basename(old)})" if out.returncode == 0
            else f"Claude Code update failed: {(out.stderr or out.stdout).strip()[-300:]}")
        return
    try:
        ok = subprocess.run([link, "--version"], env=env, capture_output=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    if not ok:
        os.remove(link)
        os.symlink(old, link)
        subprocess.run(["rm", "-rf", new])
        log(f"Claude Code {os.path.basename(new)} does not start; rolled back to {os.path.basename(old)}")
        return
    versions = os.path.dirname(new)
    for name in os.listdir(versions):  # keep the new and the previous version (each is ~300 MB)
        path = os.path.join(versions, name)
        if path not in (new, old):
            subprocess.run(["rm", "-rf", path])
    log(f"Claude Code updated {os.path.basename(old)} -> {os.path.basename(new)}; restarting when idle")
    SESSION.request_restart()


# ---------- tasks from automations (action claude_code_agent.run_task) ----------
TASKS_FILE = "/data/tasks.json"
TASKS_KEEP = 50
TASKS_LOCK = threading.Lock()
TASKS_ACTIVE: dict = {}  # run_id -> started
TASKS_MAX_QUEUED = 5


def _tasks_save(entry: dict) -> None:
    with TASKS_LOCK:
        try:
            with open(TASKS_FILE) as f:
                runs = json.load(f)
        except (OSError, ValueError):
            runs = []
        runs = [r for r in runs if r.get("run_id") != entry["run_id"] or r.get("started") != entry["started"]]
        runs.append(entry)
        with open(TASKS_FILE + ".tmp", "w") as f:
            json.dump(runs[-TASKS_KEEP:], f)
        os.replace(TASKS_FILE + ".tmp", TASKS_FILE)


def tasks_recent() -> list:
    with TASKS_LOCK:
        try:
            with open(TASKS_FILE) as f:
                runs = list(reversed(json.load(f)))
        except (OSError, ValueError):
            return []
    for run in runs:  # still marked running but not running any more: the app restarted during it
        if run.get("state") == "running" and run.get("run_id") not in TASKS_ACTIVE:
            run["state"] = "interrupted"
    return runs


def run_task(run_id: str, prompt: str, model: str) -> None:
    """One task from an automation: a turn in the shared conversation, owned by the task. Its answer and the
    results of background work it started are collected and fired as claude_code_assist_task_finished."""
    owner = {"user": "automation", "device": f"task:{run_id}", "task": run_id, "followup": {}}
    started = time.time()
    entry = {"run_id": run_id, "started": datetime.now().isoformat(timespec="seconds"), "prompt": prompt[:2000],
             "model": model or None, "state": "running"}
    _tasks_save(entry)
    try:
        ha_call("events/claude_code_assist_task_started", {"run_id": run_id})
    except Exception as err:  # noqa: BLE001
        log(f"task event failed: {err}")
    texts, success, timed_out = [], False, False
    try:
        header = (f"[Home Assistant automation task | run_id: {run_id} | time: {time.strftime('%Y-%m-%d %H:%M')}"
                  + (f" | model: {model}" if model else "") + "]")
        answer, success = SESSION.ask(f"{header}\n{prompt}", owner=owner)
        texts.append(answer)
        if success:
            SESSION.follow(owner, lambda obj: obj.get("type") == "text" and texts.append(obj["text"]),
                           max_wait=TASK_MAX_MINUTES * 60)
            timed_out = time.time() - started >= TASK_MAX_MINUTES * 60
    except Exception as err:  # noqa: BLE001
        texts.append(f"Task failed: {err}")
        success = False
    finally:
        TASKS_ACTIVE.pop(run_id, None)
    result = "\n\n".join(t for t in texts if t and t.strip())
    duration = round(time.time() - started)
    entry.update(state="timed out" if timed_out else "done" if success else "failed", duration_seconds=duration,
                 result=result[:4000])
    _tasks_save(entry)
    log(f"task {run_id} {entry['state']} in {duration}s: {result[:100]!r}")
    try:
        ha_call("events/claude_code_assist_task_finished",
                {"run_id": run_id, "success": success and not timed_out, "timed_out": timed_out,
                 "result": result, "duration_seconds": duration})
    except Exception as err:  # noqa: BLE001
        log(f"task event failed: {err}")


# ---------- session view (Settings → Session in the web UI) ----------
SESSION_TAIL = 1_500_000  # bytes of the transcript shown when the view opens
SESSION_CLIP = 4000  # characters per shown item


def session_file() -> str | None:
    sid = SESSION.state.get("session_id")
    if not sid:
        return None
    found = glob.glob(os.path.join(os.environ.get("HOME", "/data/home"), ".claude", "projects", "*", f"{sid}.jsonl"))
    return found[0] if found else None


def _clip(text: str) -> str:
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return text if len(text) <= SESSION_CLIP else text[:SESSION_CLIP] + f"\n… ({len(text) - SESSION_CLIP} more characters)"


def _tool_text(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return _clip(inp)
    if name == "Bash":
        return _clip(inp.get("command", "") + ("   [background]" if inp.get("run_in_background") else ""))
    if name in ("Read", "Write", "Edit") and "file_path" in inp:
        rest = {k: v for k, v in inp.items() if k != "file_path"}
        return _clip(inp["file_path"] + ("\n" + json.dumps(rest, ensure_ascii=False, indent=1) if rest else ""))
    if name == "Agent":
        return _clip(f"{inp.get('description', '')}\n{inp.get('prompt', '')}".strip())
    return _clip(json.dumps(inp, ensure_ascii=False, indent=1))


def _result_text(content) -> str:
    if isinstance(content, list):
        return _clip("\n".join(b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')}]"
                               for b in content if isinstance(b, dict)))
    return _clip(content or "")


def session_items(entry: dict) -> list:
    """Turn one transcript line into readable items: user, assistant, thinking, tool, result, system."""
    kind, ts = entry.get("type"), entry.get("timestamp") or ""
    base = {"ts": ts, "side": bool(entry.get("isSidechain"))}
    if kind == "system":
        if entry.get("subtype") == "compact_boundary":
            return [{**base, "kind": "system", "text": "Conversation compacted"}]
        return []
    if kind not in ("user", "assistant"):
        return []
    msg = entry.get("message") or {}
    content = msg.get("content")
    if kind == "user" and entry.get("isCompactSummary"):
        return [{**base, "kind": "system", "text": "Summary after compaction:\n" + _result_text(content)}]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    items = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and block.get("text", "").strip():
            items.append({**base, "kind": kind, "text": _clip(block["text"])})
        elif t == "thinking" and block.get("thinking", "").strip():
            items.append({**base, "kind": "thinking", "text": _clip(block["thinking"])})
        elif t in ("tool_use", "server_tool_use"):
            items.append({**base, "kind": "tool", "name": block.get("name", "?"),
                          "text": _tool_text(block.get("name", ""), block.get("input"))})
        elif t == "tool_result":
            items.append({**base, "kind": "result", "error": bool(block.get("is_error")),
                          "text": _result_text(block.get("content"))})
    return items


def last_compaction(path: str, size: int) -> int:
    """Byte offset of the line with the last compact boundary (0 if none): Claude Code keeps the history before a
    compaction in the same file, but the model only sees what comes after it."""
    pos, carry = size, b""
    with open(path, "rb") as f:
        while pos > 0:
            start = max(0, pos - 1_000_000)
            f.seek(start)
            chunk = f.read(pos - start) + carry
            lines = chunk.split(b"\n")
            carry = lines[0] if start > 0 else b""  # possibly partial: joined with the next (earlier) chunk
            off = start + len(lines[0]) + 1 if start > 0 else 0
            found = None
            for line in (lines[1:] if start > 0 else lines):
                if b"compact_boundary" in line:
                    try:
                        e = json.loads(line)
                        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
                            found = off
                    except ValueError:
                        pass
                off += len(line) + 1
            if found is not None:
                return found
            pos = start
    return 0


def session_since(offset: int | None, sid: str | None, full: bool = False) -> dict:
    """Items of the current transcript from byte offset on. On first load (or a new session): from the last
    compaction on, or with full=True the earlier history too; at most the last SESSION_TAIL bytes."""
    path = session_file()
    current = SESSION.state.get("session_id")
    out = {"session_id": current, "items": [], "offset": 0, "reset": False, "clipped": False, "earlier": False}
    if not path:
        out["reset"] = sid != current
        return out
    size = os.path.getsize(path)
    if offset is None or sid != current or offset > size:
        out["reset"] = True
        offset = max(0, size - SESSION_TAIL)
        out["clipped"] = offset > 0
        if not full:
            boundary = last_compaction(path, size)
            out["earlier"] = boundary > 0
            if boundary > offset:
                offset, out["clipped"] = boundary, False
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(size - offset)
    if out["reset"] and out["clipped"]:  # started mid-line
        cut = data.find(b"\n") + 1
        data, offset = data[cut:], offset + cut
    end = data.rfind(b"\n") + 1  # only complete lines; the rest is read next time
    for line in data[:end].splitlines():
        try:
            out["items"].extend(session_items(json.loads(line)))
        except (ValueError, AttributeError, TypeError):
            continue
    out["offset"] = offset + end
    return out


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
        "claude_version": os.path.basename(os.path.realpath(os.path.join(
            os.environ.get("HOME", "/data/home"), ".local", "bin", "claude"))),
        "auto_update": AUTO_UPDATE,
    }


def client_kind(req: dict) -> str:
    """voice: a satellite (results are spoken); app: Companion app (results as push + next reply); browser;
    panel: the chat in the app's own web page (results appear there by themselves)."""
    if req.get("client") == "panel":
        return "panel"
    if (req.get("followup") or {}).get("announce"):
        return "voice"
    return "app" if req.get("device") else "browser"


def request_context(req: dict, owner: dict) -> str:
    tasks = SESSION.task_summary(owner)
    return (
        f"[Home Assistant Assist | user: {req.get('user') or 'unknown'}"
        f" | device: {req.get('device') or 'unknown'} | client: {client_kind(req)}"
        f" | language: {req.get('language') or 'unknown'}"
        f" | time: {time.strftime('%Y-%m-%d %H:%M')}"
        + (f" | running background tasks: {tasks}" if tasks else "")
        + "]"
    )


def panel_ask(user: str, text: str) -> None:
    """A message from the panel chat; the answer and any later results go into the user's chat history."""
    owner = {"user": user, "device": PANEL_DEVICE, "panel": True, "followup": {}}
    inbox_pop(user)  # results waiting for the user's next Assist reply are in this chat's history already
    req = {"user": user, "device": PANEL_DEVICE, "client": "panel", "language": "unknown"}
    started = time.time()
    try:
        answer, ok = SESSION.ask(f"{request_context(req, owner)}\n{text}", owner=owner)
        chat_add(user, "assistant", answer or "I did not get an answer.", "panel")
        log(f"{time.time() - started:.1f}s ok={ok} device='panel' q={text[:80]!r}")
    finally:
        PANEL_BUSY[user] = max(0, PANEL_BUSY.get(user, 1) - 1)


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

    def _panel_user(self) -> str:
        """The Home Assistant user behind an ingress request (the Supervisor passes it in headers)."""
        return (self.headers.get("X-Remote-User-Display-Name") or self.headers.get("X-Remote-User-Name")
                or "unknown")

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            self._reply(200, status())
        elif self._from_ingress() and path == "/api/chat":
            user = self._panel_user()
            since = int((re.search(r"since=(\d+)", query) or [0, 0])[1])
            self._reply(200, {"user": user, "messages": chat_since(user, since),
                              "pending": PANEL_BUSY.get(user, 0) > 0,
                              "working_on": SESSION.user_work(user),
                              "background_tasks": SESSION.task_summary(None) or None})
        elif path == "/instructions" and self.headers.get("Authorization") == f"Bearer {TOKEN}":
            with open(INSTRUCTIONS_FILE) as f:
                self._reply(200, {"instructions": f.read()})
        elif self._from_ingress() and path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(UI_HTML)))
            self.end_headers()
            self.wfile.write(UI_HTML)
        elif self._from_ingress() and path == "/api/tasks":
            self._reply(200, {"runs": tasks_recent()[:20]})
        elif self._from_ingress() and path == "/api/session":
            offset = re.search(r"offset=(\d+)", query)
            sid = re.search(r"sid=([\w-]+)", query)
            self._reply(200, session_since(int(offset[1]) if offset else None, sid[1] if sid else None,
                                           "full=1" in query))
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
        if path == "/chat/send" and self._from_ingress():
            text = (req.get("text") or "").strip()
            if not text:
                self._reply(400, {"error": "empty message"})
                return
            user = self._panel_user()
            chat_add(user, "user", text, "panel")
            PANEL_BUSY[user] = PANEL_BUSY.get(user, 0) + 1
            threading.Thread(target=panel_ask, args=(user, text), daemon=True).start()
            self._reply(202, {"ok": True})
            return
        if path == "/chat/clear" and self._from_ingress():
            chat_clear(self._panel_user())
            self._reply(200, {"ok": True})
            return
        if path == "/instructions":
            text = (req.get("text") or "").replace("\r\n", "\n")
            with open(INSTRUCTIONS_FILE) as f:
                current = f.read()
            if req.get("base") is not None and req["base"].replace("\r\n", "\n") != current:
                self._reply(409, {"error": "the instructions were changed elsewhere since you opened them; "
                                           "copy your edits, press Discard changes and apply them again"})
                return
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
        elif path == "/cleanup":
            self._reply(200, {"ok": True, "removed": cleanup_temporary_automations()})
        elif path == "/compact":
            threading.Thread(target=SESSION.compact, daemon=True).start()
            self._reply(202, {"ok": True})
        elif path == "/restart":
            SESSION.restart_forced()
            self._reply(200, {"ok": True})
        elif path == "/reset":
            restart_session(new_conversation=True)
            self._reply(200, {"ok": True})
        elif path == "/task" and not self._from_ingress():
            prompt = (req.get("prompt") or "").strip()
            run_id = re.sub(r"[^\w.-]", "_", str(req.get("run_id") or "").strip())[:64] or time.strftime("task-%Y%m%d-%H%M%S")
            if not prompt:
                self._reply(400, {"error": "prompt is required"})
            elif run_id in TASKS_ACTIVE:
                self._reply(409, {"error": f"task {run_id} is already running"})
            elif len(TASKS_ACTIVE) >= TASKS_MAX_QUEUED:
                self._reply(429, {"error": "too many tasks waiting"})
            else:
                TASKS_ACTIVE[run_id] = time.time()
                threading.Thread(target=run_task, args=(run_id, prompt, str(req.get("model") or "").strip()),
                                 daemon=True).start()
                self._reply(202, {"ok": True, "run_id": run_id})
        elif path == "/update":
            threading.Thread(target=update_claude, daemon=True).start()
            self._reply(202, {"ok": True})
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

        def alive() -> bool:
            """False once the client closed the connection (a closed peer reads as EOF)."""
            try:
                return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
            except BlockingIOError:
                return True  # nothing to read: still connected
            except OSError:
                return False

        owner = {"user": req.get("user"), "device": req.get("device"), "followup": req.get("followup") or {}}
        SESSION.take_over(owner)
        started = time.time()
        chat_add(owner.get("user"), "user", req.get("text", ""), "assist")
        text, ok = SESSION.ask(f"{request_context(req, owner)}\n{req.get('text', '')}", owner=owner)
        chat_add(owner.get("user"), "assistant", to_speech(text), "assist")
        log(f"{time.time() - started:.1f}s ok={ok} device={req.get('device')!r} q={req.get('text', '')[:80]!r}")
        followed = False
        # Results that went out as a notification meanwhile: below the answer, so the reply arrives in one piece.
        earlier = inbox_pop(owner.get("user"))
        answer = to_speech(text) or "I did not get an answer."
        if earlier:
            answer += "".join(f"\n\n📋 Earlier result ({hhmm}): {t}" for hhmm, t in earlier)
        try:
            emit({"type": "text", "text": answer})
            # The Companion app (a request with a device) shows streamed text of overlapping replies in the newest
            # bubble, so its replies close after the answer; results then arrive as a push and in the next reply.
            if ok and req.get("follow", True) and not req.get("device"):
                followed = True  # follow() takes over the handed-over work itself
                SESSION.follow(owner, emit, alive)
            emit({"type": "done"})
        except OSError:
            log("stream closed by client; further results become notifications")
            for hhmm, t in earlier:
                inbox_add(owner.get("user"), t)
        finally:
            if not followed:  # this question opened no reply: work handed over to it must not be lost
                SESSION.release_handover(owner)


if __name__ == "__main__":
    log(f"bridge listening on :{PORT} model={MODEL} effort={EFFORT or 'default'} timeout={TIMEOUT}s compact_hour={COMPACT_HOUR}")
    threading.Thread(target=compact_scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
