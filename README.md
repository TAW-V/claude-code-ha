# Claude Code for Home Assistant Assist

Use **Claude Code** — with your own Claude subscription and full tool use — as a Home Assistant **Assist conversation agent**.

Ask from the Companion app, a watch or voice: *"Why was my baseload so high last night?"*, *"Turn on the kitchen lights"*,
*"Is the UPS on battery?"*. Claude Code can read states and history, call services, inspect files and run shell commands.

It consists of two parts in this repository:

| Part | Path | What it does |
|---|---|---|
| **App** (add-on) *Claude Code Assist* | `claude_code_assist/` | Installs Claude Code into the app's persistent `/data` and keeps **one long-running Claude Code session** (`claude -p --input-format stream-json`). A small HTTP bridge on the internal Supervisor network (port 8099) feeds every Assist request into that session. |
| **Integration** *Claude Code Assist* | `custom_components/claude_code_agent/` | Adds a conversation agent (`conversation.claude_code`) that sends Assist requests to the app. |

Claude Code lives in an app because the regular terminal / code-server apps lose anything installed in `/root` when they restart.

## Installation

### 1. App
1. **Settings → Apps → App store → ⋮ → Repositories**, add `https://github.com/TAW-V/claude-code-ha`.
2. Install **Claude Code Assist** and start it once (it installs Claude Code on first start).
3. In any terminal with Claude Code, run `claude setup-token`, log in, and copy the token (`sk-ant-oat01-…`).
4. Paste it into the app's **Configuration → oauth_token**, save and restart the app.

### 2. Integration
1. Install via **HACS → ⋮ → Custom repositories** → `https://github.com/TAW-V/claude-code-ha` (type *Integration*),
   or copy `custom_components/claude_code_agent` into your `/config/custom_components/`.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Claude Code Assist**. The defaults work: URL
   `http://local-claude-code-assist:8099` (use the app's hostname if you installed it from this repository, see the app's
   *Info* page), token is read automatically from `/config/.claude_code_assist_token`.
4. **Settings → Voice assistants** → pick an assistant → **Conversation agent: Claude Code**.

## App options

| Option | Default | Description |
|---|---|---|
| `oauth_token` | – | Token from `claude setup-token` (valid ~1 year). |
| `model` | `sonnet` | Claude Code model alias or ID (`haiku` is faster, `opus` smarter). |
| `effort` | *(Claude Code default)* | Optional reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`. Lower is faster. |
| `timeout_seconds` | `180` | Maximum time per request. |
| `compact_hour` | `4` | Hour of the day (local time) at which the conversation is compacted with `/compact`. |
| `followup_wait_seconds` | `240` | How long an Assist reply stays open for results of background work (0 = answer only; Home Assistant ends Assist runs from the app after 300 s). |
| `task_max_minutes` | `30` | How long a task from an automation (`claude_code_agent.run_task`) may take, including its background work. |
| `auto_update_claude` | `true` | Check daily (at `compact_hour`) for a new Claude Code version; rolls back if it does not start. |
| `extra_instructions` | – | Appended to the system prompt. |

## Long tasks and follow-ups

For work that takes a while (subagents, larger analyses) the assistant answers right away ("I started an agent
that…") and keeps working in the background. The result goes back to **the device that asked**:

| Asked from | Result |
|---|---|
| Assist in the browser | Streamed into the **same reply** while the chat is open (up to `followup_wait_seconds`); a new question in the same chat takes over the wait. |
| Companion app (phone, watch) | The reply closes after the answer (the app mixes up streamed text of overlapping replies). The result is sent as a **short push notification** and shown in full at the **top of your next Assist reply** (send any message), separated by a blank line. Claude mentions this when it starts the task; the header tells it the client (`app`, `browser`, `voice`). |
| Voice satellite (e.g. Home Assistant Voice) | Answer right away; the result is **spoken on the same satellite** (`assist_satellite.announce`). |
| Unknown | Persistent notification in Home Assistant, and at the top of the user's next reply. |

Every follow-up also fires a `claude_code_assist_followup` event (`text`, `user`, `device`) for your own automations.

### Several users at once

Everyone shares one conversation, but **background results belong to whoever started the task**:

- The bridge runs Claude Code with `--replay-user-messages`, so it knows which result answers which question, and it
  records the owner of every background task (`task_started`) and when it finishes (`task_notification`).
- Another user can ask questions while your agent runs; they get their own answer and their reply closes normally.
- If Claude folds a finished task into someone else's turn, or someone else spoke while the task ran, the bridge
  asks Claude separately to report the result to the task's owner. The header of every message lists the running
  background tasks and who started them, and the default instructions tell Claude not to mention other people's
  results.
- An open reply waits for the work started by its question. If you ask something new in the same chat while a reply
  is still waiting, the new reply takes over that work, so the result appears under your newest answer instead of
  being pushed. Replies are open for at most `followup_wait_seconds`.
- Results of tools that wake Claude without a notification (e.g. Monitor output lines) are interim updates: shown in
  the owner's open chat when only one person has background work running, otherwise dropped.
- Saving instructions waits until no background task is running before restarting Claude Code. The web UI shows when
  a restart is pending and offers **Restart now**. *Restart now* and *Start new conversation* stop immediately and tell
  owners of running tasks that they were interrupted.
- Set `CCA_DEBUG=1` in the bridge environment to log the Claude Code task events it routes on.

Best effort: a folded answer can still occasionally mention someone else's result, and progress messages are only
shown in an open chat (never pushed or announced).

## Temporary automations

The default instructions tell Claude to give automations that are only needed for a while an alias starting with
`TMP until YYYY-MM-DD HH:MM | ` (local time), e.g. `TMP until 2026-11-03 18:00 | Boiler boost`. Every day at
`compact_hour`, just before the conversation is compacted, the bridge deletes temporary automations whose time has
passed (and fires a `claude_code_assist_cleanup` event). `POST /cleanup` runs it immediately.

## Tasks from automations

The integration adds the action **`claude_code_agent.run_task`**, so automations and scripts can give Claude Code a
task:

```yaml
- action: claude_code_agent.run_task
  data:
    run_id: morning-report
    prompt: >-
      Check whether any window is open while the heating is on. If so, send a short notification
      to notify.mobile_app_my_phone naming the windows.
    model: opus        # optional: Claude Code does the work with an agent on this model
    wait: true         # optional: wait for the result and return it as response
  response_variable: report
```

- The task is a turn in the same conversation, marked as coming from an automation. It does not start a second Claude
  Code process (which costs a few hundred MB of memory).
- Nobody reads the reply unless the prompt says how to report (a notification, for example). Claude does not ask
  questions in a task and skips anything that would need a confirmation.
- Events: `claude_code_assist_task_started` (`run_id`) and `claude_code_assist_task_finished` (`run_id`, `success`,
  `timed_out`, `result`, `duration_seconds`), after any background work the task started has finished (at most
  `task_max_minutes`). Results that arrive later fire `claude_code_assist_task_late_result`.
- With `wait: true` and a `response_variable`, the action returns the same data as the finished event.
- The **Settings** tab lists recent runs with their task and result.

## Claude Code updates

Every day at `compact_hour` the app runs `claude update`. If a new version was installed, it checks that it starts
(otherwise it switches back to the previous one), keeps only the current and the previous version, and restarts Claude
Code as soon as no request or background work is running. **Update Claude Code now** in the Settings tab does the same
right away; `auto_update_claude: false` turns the daily check off.

## Web UI

Open the app (**Open Web UI**, or **Claude Code** in the sidebar for admins).

**Chat tab:** a chat with the same Claude Code conversation that keeps its history (per Home Assistant user, also
in the Companion app's sidebar). Your Assist questions, answers and background results are added to it too, so
nothing is lost when you close Assist. Results of background work started from the chat appear there by
themselves.

**Settings tab:**

- edit the assistant's standing instructions (`CLAUDE.md`) in a full text editor — describe your home, preferences and
  rules; changes apply from the next message, the conversation itself is kept;
- see the session status, model/effort, Claude Code version and last compaction;
- **Compact now**, **Start new conversation** or **Update Claude Code now**;
- **Automation tasks**: recent runs of `claude_code_agent.run_task`;
- **Session**: a read-only, live view of the raw Claude Code session (messages, tool calls and results).

The default instructions are in `rootfs/defaults/CLAUDE.md`; by default the assistant always answers in the language of
the message it receives. The web UI is only reachable through Home Assistant ingress (Supervisor proxy).

## One continuous conversation

All Assist requests — from every user and device — go into **one continuous Claude Code conversation**, so it remembers
earlier questions and what it did. Each message carries a header with user, device, language and time.

- The process stays running, so there is no start-up cost per request (simple questions answer in a few seconds).
- The session id is stored in `/data/state.json`; after an app restart the session is resumed with `--resume`.
- Every day at `compact_hour` the conversation is compacted with `/compact` (Claude Code also auto-compacts when the
  context fills up).
- Bridge endpoints (Bearer token required): `POST /conversation`, `POST /compact` (compact now),
  `POST /reset` (start a new conversation), `POST /instructions`, `POST /cleanup`, `POST /task`, `POST /update`. `GET /health` shows the session id and last compaction.

## ⚠️ Security

The app runs Claude Code with **full access**: shell, read/write on `/config`, `/addons`, `/share`, and the
Home Assistant and Supervisor APIs (`hassio_role: admin`). Anyone who can use Assist in your Home Assistant can ask it to
do anything. The default instructions ask for confirmation before destructive actions, but that is guidance for the
model, not an enforced permission. Restrict `--allowedTools` in `rootfs/bridge.py` if you want less.

The bridge is only reachable on the internal Supervisor network and requires a shared token.

## Notes
- Requests count against your Claude plan's usage limits.
- Requests are handled one at a time; simple ones take a few seconds, tool-heavy ones longer.
- amd64 only for now (add more architectures in `config.yaml` / `build.yaml`).

This is an unofficial community project and is not affiliated with Anthropic or Home Assistant.
