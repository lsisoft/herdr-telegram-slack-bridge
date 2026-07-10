# Herdr Telegram and Slack integration

This directory is also a Herdr plugin. It uses Herdr's
`pane.agent_status_changed` event and forwards `blocked` agents to the same
Telegram and Slack bot bridge used by the Codex and Claude hooks. Replies are sent to
the Herdr pane with `herdr pane run`, so no tmux emulation is required.

## Local development

```bash
herdr plugin link .
herdr plugin list
```

The existing bridge daemons still need to be running for inbound replies:

```bash
python3 -m agent_telegram_bridge daemon
python3 -m agent_telegram_bridge slack-daemon
```

The plugin reads the bridge's normal environment variables. Set
`HERDR_BRIDGE_STATUSES=blocked,done` if notifications for completed agents are
also wanted; the default is `blocked`, which matches "needs input" behavior.

For a GitHub publication, this directory can be the repository root and must
be tagged with the `herdr-plugin` GitHub topic for marketplace discovery.
