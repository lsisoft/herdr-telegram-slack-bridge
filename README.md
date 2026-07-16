# Herdr Agent Telegram and Slack Bridge

Bidirectional Telegram and Slack bot bridge for Herdr, Codex, and Claude agent sessions. It delivers blocked-agent alerts to either chat platform and routes replies back to the exact Herdr or tmux pane that raised the alert.

## Repository identity

`x/agent_telegram_bridge/` is the local checkout of
[`lsisoft/herdr-telegram-slack-bridge`](https://github.com/lsisoft/herdr-telegram-slack-bridge).
It is a notification and reply transport, not an AI agent and not a tmux or
Herdr replacement.

The related session manager is the
[`lsisoft/herdr`](https://github.com/lsisoft/herdr) fork of
[`ogulcancelik/herdr`](https://github.com/ogulcancelik/herdr). Herdr owns
workspaces, tabs, agent processes, restart/resume metadata, models, compute
settings, and launch permissions. This bridge observes an existing pane,
sends alerts to Slack or Telegram, and forwards replies to that same pane. It
does not launch agents, grant permissions, or restore sessions.

The Python package name `agent_telegram_bridge` and the systemd filenames
ending in `-tmux-bridge.service` are retained for compatibility with existing
notify hooks and deployments. They do not identify a separate "tmux agent."
The same package implements both routing backends:

| Concern | Herdr backend | Direct tmux backend |
| --- | --- | --- |
| Session owner | Herdr server and workspace | Codex/Claude process running in a tmux pane |
| Alert source | Herdr plugin events or native Codex/Claude notify hooks | Native Codex/Claude notify hooks |
| Pane discovery | `HERDR_PANE_ID`, then `herdr pane/tab` metadata | `TMUX_PANE`, then `tmux display-message` metadata |
| Reply delivery | `herdr pane run <pane-id> <text>` | `tmux send-keys` with submission monitoring and retries |
| Public route | `herdr:<visible-tab-position>:<tab-name>` | `<host>:<tmux-session>:<window-index>:<window-name>` |
| Internal route | Opaque Herdr pane id such as `w1:p7` | Stable tmux pane id such as `%9` |

When both pane environment variables are present, the Herdr backend takes
precedence. The transport and state store are otherwise shared.

## What it does

- Runs as the existing Codex/Claude notify hook.
- Enriches each alert with hostname, Herdr/tmux routing details, pane id, cwd, and the question/message.
- Stores a short ticket id for each alert, preferring one unused digit and then lowercase ids like `ab1`.
- Splits long Telegram and Slack alerts/responses into multiple messages instead of truncating the question.
- Sends Telegram output as escaped `MarkdownV2`.
- Can route Telegram alerts into forum topics, one topic per exact Herdr tab or tmux window label, when `TELEGRAM_BRIDGE_FORUM_TOPICS=1` is enabled in a forum-enabled supergroup.
- Posts alerts to Slack, one Slack thread per exact pane alert, and receives Slack thread replies through Web API polling. Slack Events API mode is also available.
- Works with Herdr panes as well as tmux panes. The included `herdr-plugin.toml` emits alerts for Herdr `blocked` agents and sends replies through Herdr's pane API.
- Runs a Telegram long-poll daemon that accepts:
  - direct replies to the alert message
  - `/reply <text>` for the latest alert
  - `/reply <id> <text>` for a specific alert
  - `/status` for latest alert status
  - `/status <id>` for a specific alert status
  - `/alerts`
  - `/send <tmux-target> <text>`
- Runs a Slack bot daemon that polls alert threads for replies by default, with optional Events API webhook mode.
- Uses `tmux send-keys` plus a newline paste fallback to submit the reply to the original Codex or Claude pane, monitors once per second for up to two minutes, retries submission while the reply is visibly pending, clears and retypes if it remains pending, and sends warnings at 15 seconds and at the two-minute limit.

## Runtime configuration

The bridge reads environment variables first, then env files from:

1. `.env` beside the bridge package
2. `~/.config/agent_telegram_bridge/env`

Useful variables:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=<numeric-direct-chat-id>
TELEGRAM_BRIDGE_ALLOWED_USERNAME=<optional-telegram-username>
TELEGRAM_BRIDGE_STATE_FILE=~/.cache/agent_telegram_bridge/state.json
TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_SECONDS=120
TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL=1
TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL=1
TELEGRAM_BRIDGE_TMUX_SEND_WARNING_SECONDS=15
TELEGRAM_BRIDGE_ENABLED=0 # set to 0 to disable all Telegram send/poll behavior
TELEGRAM_BRIDGE_FORUM_TOPICS=0

SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C0123456789
SLACK_BRIDGE_MODE=poll
SLACK_BRIDGE_POLL_INTERVAL=3
SLACK_SIGNING_SECRET=... # only needed for SLACK_BRIDGE_MODE=events
SLACK_BRIDGE_HTTP_HOST=127.0.0.1
SLACK_BRIDGE_HTTP_PORT=8797
SLACK_BRIDGE_HTTP_PATH=/slack/events

HERDR_BRIDGE_STATUSES=blocked # use blocked,done to include completed agents
```

For local Slack setup, edit the ignored `.env` beside the bridge package, or use `~/.config/agent_telegram_bridge/env`.

When `TELEGRAM_BRIDGE_ENABLED=0` or `TELEGRAM_BRIDGE_DISABLED=1`, the notify hook still records alerts and posts to Slack, but it does not send Telegram messages. The Telegram daemon also stays quiet when launched with Telegram disabled.

Slack replies forwarded into tmux are monitored for up to 120 seconds by default. The bridge sends `Enter`, `C-m`, `C-j`, and a tmux buffer newline paste, checks the pane every second, and if the pane does not accept the line it clears pending input, retypes the reply, and submits it again every second. On timeout it clears the pending input and posts a final warning back to Slack.

Alert IDs are two letters from the Herdr tab or tmux window name plus one digit, for example `cr4` for `cry`. The digit is selected to avoid IDs already used in recent state for the last 24 hours. Tmux alert headers use `hostname:tmux-session:window-index:window-name [id]`. Herdr headers use the shorter `herdr:visible-tab-position:tab-name [id]`, for example `herdr:4:sys [sy1]`; the internal pane id is retained separately for reply routing.

For a direct Telegram user chat, `TELEGRAM_CHAT_ID` must be the numeric chat id for a chat where the bot has already been started. A Telegram username is used for authorization, not as a Bot API delivery address.

## Telegram forum topics

Telegram does not have Slack-style threads in ordinary direct chats. It does support forum topics inside supergroups.

To separate sessions in Telegram:

1. Create or use a supergroup.
2. Enable Topics for the group.
3. Add the bot to the group.
4. Make the bot an admin with permission to manage topics.
5. Set `TELEGRAM_CHAT_ID` to the numeric supergroup chat id.
6. Set `TELEGRAM_BRIDGE_FORUM_TOPICS=1`.

The bridge creates and reuses one topic per exact Herdr tab or tmux window label, such as `herdr:4:sys`, `codex:0:fx`, or `codex:1:ibkr`, stores the `message_thread_id` in state, and sends alerts plus Telegram replies back into that topic. If the tab/window name changes, the label changes and a new topic is used. If topic creation fails, it logs the error and falls back to normal chat messages.

## Slack setup

Slack support posts each alert as a new top-level channel message. The top line is a compact route header, for example `herdr:4:sys [sy1]` or `host:codex:2:cry [cr4]`. Replies in that Slack message thread route back to the captured pane for that alert. Thread text is always forwarded verbatim, including text beginning with words such as `help`, `status`, or `reply`; bot command parsing is only used outside a known alert thread. The default receiver mode is polling, so it does not require Slack Event Subscriptions, a public Request URL, or a tunnel. The Slack bridge uses normal thread messages, not Slack slash commands.

Register the Slack app:

1. Open `https://api.slack.com/apps` and create a new app.
2. Add a Bot User.
3. Under OAuth & Permissions, add bot token scopes:
   - `chat:write`
   - `channels:history` for reading public-channel thread replies
   - `groups:history` if using a private channel
4. Install the app to the workspace.
5. Copy the Bot User OAuth Token (`xoxb-...`) to `SLACK_BOT_TOKEN`.
6. Put the app/bot in the target channel and set `SLACK_CHANNEL_ID` to that channel id.
7. Set `SLACK_BRIDGE_MODE=poll`.

In polling mode, do not enable Event Subscriptions and do not configure a Request URL. The daemon periodically calls Slack `conversations.replies` for the bridge-created session threads.

Run the Slack receiver:

```bash
python3 -m agent_telegram_bridge slack-daemon
```

## Herdr plugin

This repository is also the chat integration plugin for
[`lsisoft/herdr`](https://github.com/lsisoft/herdr); there is not a second
Herdr-specific bridge checkout.

Install directly from GitHub:

```bash
herdr plugin install lsisoft/herdr-telegram-slack-bridge
```

For local development, link the repository checkout:

```bash
herdr plugin link .
```

Herdr status events are converted into the normal ticketed bridge alerts. The
Telegram and Slack daemons remain responsible for inbound replies, which are
sent to either tmux or Herdr panes.

By default it polls Slack every three seconds. If `SLACK_BRIDGE_POLL_INTERVAL` is configured above 15 seconds, the effective polling interval is capped at 15 seconds. No inbound HTTP listener is used in polling mode.

### Optional Slack Events API mode

Set `SLACK_BRIDGE_MODE=events` only if you prefer Slack Event Subscriptions over polling. Events mode also requires `SLACK_SIGNING_SECRET`.

Under Event Subscriptions, enable events, set the request URL to your public HTTPS forwarding URL ending in `/slack/events`, and subscribe to bot events:

- `message.channels` for a public channel
- `message.groups` for a private channel

Do not use `127.0.0.1` in Slack; that address is only reachable from the local machine.

In events mode, the bridge listens on `http://127.0.0.1:8797/slack/events`. Keep that local bind address in `.env`, then place it behind an HTTPS reverse proxy, Cloudflare Tunnel, or ngrok during setup. The Slack Request URL should be the public URL, for example `https://example.trycloudflare.com/slack/events`, forwarding to `http://127.0.0.1:8797/slack/events`.

Example Cloudflare quick tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8797
```

Example ngrok tunnel:

```bash
ngrok http 8797
```

## systemd deployment

```bash
scripts/install_systemd.sh
```

That installs:

- `~/.local/bin/agent_notify.py` as a wrapper around this project
- `~/.config/systemd/user/agent-telegram-tmux-bridge.service`
- `~/.config/systemd/user/agent-slack-tmux-bridge.service` (installed but not enabled)

The existing Codex notify hook and Claude Notification hook can point at the generated `agent_notify.py` wrapper.

After Slack env is configured:

```bash
systemctl --user enable --now agent-slack-tmux-bridge.service
```

## Local checks

```bash
python3 -m unittest discover -s tests
python3 -m agent_telegram_bridge state
python3 -m agent_telegram_bridge slack-daemon
```
