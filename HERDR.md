# Herdr Telegram and Slack integration

This directory is the bridge repository and is also a plugin for the
[`lsisoft/herdr`](https://github.com/lsisoft/herdr) agent multiplexer. It is
not the Herdr runtime and does not own agent launch, restart, model, compute,
or permission state. It uses Herdr's
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

Herdr alerts use the ordered position and label from `herdr tab list` for their
public route, such as `herdr:4:sys`. The opaque pane id, such as `w1:p7`, is
stored separately and remains the target used for replies.

For a GitHub publication, this directory can be the repository root and must
be tagged with the `herdr-plugin` GitHub topic for marketplace discovery.
