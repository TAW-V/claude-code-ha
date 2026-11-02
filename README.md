# Claude Code for Home Assistant

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
| `extra_instructions` | – | Appended to the system prompt. |
| `instructions` | built-in default | The assistant's `CLAUDE.md` (standing instructions). |

The assistant's standing instructions (its `CLAUDE.md`) are the **`instructions`** option. Edit them in the app's
**Configuration** tab (easiest via ⋮ → *Edit in YAML*, using a `|` block) to describe your home, then restart the app.
If the option is empty, the built-in default from `rootfs/defaults/CLAUDE.md` is used. By default the assistant always
answers in the language of the message it receives.

## One continuous conversation

All Assist requests — from every user and device — go into **one continuous Claude Code conversation**, so it remembers
earlier questions and what it did. Each message carries a header with user, device, language and time.

- The process stays running, so there is no start-up cost per request (simple questions answer in a few seconds).
- The session id is stored in `/data/state.json`; after an app restart the session is resumed with `--resume`.
- Every day at `compact_hour` the conversation is compacted with `/compact` (Claude Code also auto-compacts when the
  context fills up).
- Bridge endpoints (Bearer token required): `POST /conversation`, `POST /compact` (compact now),
  `POST /reset` (start a new conversation). `GET /health` shows the session id and last compaction.

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
