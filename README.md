# Claude Code for Home Assistant

Use **Claude Code** — with your own Claude subscription and full tool use — as a Home Assistant **Assist conversation agent**.

Ask from the Companion app, a watch or voice: *"Why was my baseload so high last night?"*, *"Turn on the kitchen lights"*,
*"Is the UPS on battery?"*. Claude Code can read states and history, call services, inspect files and run shell commands.

It consists of two parts in this repository:

| Part | Path | What it does |
|---|---|---|
| **App** (add-on) *Claude Code Assist* | `claude_code_assist/` | Installs Claude Code into the app's persistent `/data`, runs a small HTTP bridge on the internal Supervisor network (port 8099) that forwards each request to `claude -p`. |
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
| `timeout_seconds` | `180` | Maximum time per request. |
| `extra_instructions` | – | Appended to the system prompt. |

The assistant's standing instructions live in the app at `/data/workspace/CLAUDE.md` (created from
`rootfs/defaults/CLAUDE.md` on first start; edit it to describe your home).

Follow-up questions within 30 minutes continue the same Claude Code session.

## ⚠️ Security

The app runs Claude Code with **full access**: shell, read/write on `/config`, `/addons`, `/share`, and the
Home Assistant and Supervisor APIs (`hassio_role: admin`). Anyone who can use Assist in your Home Assistant can ask it to
do anything. The default instructions ask for confirmation before destructive actions, but that is guidance for the
model, not an enforced permission. Restrict `--allowedTools` in `rootfs/bridge.py` if you want less.

The bridge is only reachable on the internal Supervisor network and requires a shared token.

## Notes
- Requests count against your Claude plan's usage limits.
- Expect roughly 5–10 s for simple requests and longer when tools are used; fine for chat, a bit slow for voice.
- amd64 only for now (add more architectures in `config.yaml` / `build.yaml`).

This is an unofficial community project and is not affiliated with Anthropic or Home Assistant.
