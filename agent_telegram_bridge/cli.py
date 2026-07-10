from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import random
import re
import socket
import ssl
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_ENV_FILES = (
    PROJECT_ENV_FILE,
    Path.home() / ".config" / "agent_telegram_bridge" / "env",
)
DEFAULT_STATE_FILE = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    / "agent_telegram_bridge"
    / "state.json"
)
DEFAULT_POLL_TIMEOUT_SECONDS = 20
DEFAULT_POLL_LIMIT = 50
DEFAULT_SLACK_POLL_INTERVAL_SECONDS = 3.0
MAX_SLACK_POLL_INTERVAL_SECONDS = 15.0
DEFAULT_SLACK_POLL_LIMIT = 100
MAX_TELEGRAM_TEXT = 3900
MAX_SLACK_TEXT = 3900
TELEGRAM_MARKDOWN_V2_SPECIAL_CHARS = frozenset("\\_*[]()~`>#+-=|{}.!")
TICKET_RE = re.compile(
    r"\b(?:ticket|reply|id)\s*[:#]?\s*([a-z]{2}\d|[A-Z0-9]{3,12}|\d)\b",
    re.IGNORECASE,
)
BRACKET_TICKET_RE = re.compile(r"\[([a-z]{2}\d|[A-Z0-9]{3,12}|\d)\]", re.IGNORECASE)
TMUX_SEND_MONITOR_SECONDS = 120.0
TMUX_SEND_MONITOR_INTERVAL_SECONDS = 1.0
TMUX_SEND_RETRY_INTERVAL_SECONDS = 1.0
TMUX_SEND_WARNING_SECONDS = 15.0
SLACK_RATE_LIMIT_DEFAULT_SLEEP_SECONDS = 60.0
RECENT_ALERT_ID_SECONDS = 24 * 3600


@dataclass
class PaneInfo:
    session_name: str = ""
    window_index: str = ""
    window_name: str = ""
    pane_index: str = ""
    pane_id: str = ""
    pane_current_command: str = ""
    pane_current_path: str = ""
    pane_title: str = ""

    @property
    def display_target(self) -> str:
        if self.session_name and self.window_index and self.pane_index:
            return f"{self.session_name}:{self.window_index}.{self.pane_index}"
        return self.pane_id or ""

    @property
    def send_target(self) -> str:
        return self.pane_id or self.display_target


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH", "herdr").strip() or "herdr"


def herdr_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess_text([herdr_bin(), *args])


def herdr_json_command(args: list[str]) -> dict[str, Any]:
    # Herdr's control commands emit JSON by default. Some releases reject a
    # generic --json flag even though plugin list and a few other commands
    # accept one explicitly.
    result = herdr_command(args)
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result_payload = payload.get("result")
    return result_payload if isinstance(result_payload, dict) else payload


def herdr_pane_info(target: str) -> PaneInfo:
    payload = herdr_json_command(["pane", "get", target])
    pane = payload.get("pane") if isinstance(payload.get("pane"), dict) else payload
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    tab = payload.get("tab") if isinstance(payload.get("tab"), dict) else {}
    if not isinstance(pane, dict):
        return PaneInfo(pane_id=target)
    agent = pane.get("agent") if isinstance(pane.get("agent"), dict) else {}
    return PaneInfo(
        session_name=str(
            workspace.get("label") or workspace.get("workspace_id") or pane.get("workspace_id") or "herdr"
        ),
        window_index=str(tab.get("index") or tab.get("tab_id") or pane.get("tab_id") or ""),
        window_name=str(tab.get("label") or pane.get("tab_id") or ""),
        pane_index=str(pane.get("pane_index") or str(pane.get("pane_id") or "").partition(":p")[2]),
        pane_id=str(pane.get("pane_id") or target),
        pane_current_command=str(pane.get("foreground_command") or pane.get("command") or ""),
        pane_current_path=str(pane.get("foreground_cwd") or pane.get("cwd") or ""),
        pane_title=str(pane.get("title") or agent.get("display_agent") or agent.get("agent") or ""),
    )


def is_herdr_target(target: str) -> bool:
    return bool(target and (target.startswith("w") and ":p" in target))


def herdr_target_exists(target: str) -> bool:
    return bool(target and herdr_command(["pane", "get", target]).returncode == 0)


def herdr_capture(target: str, lines: int = 80) -> str:
    if not target:
        return ""
    result = herdr_command(["pane", "read", target, "--source", "recent-unwrapped", "--lines", str(lines)])
    return (result.stdout or result.stderr or "").strip()


def herdr_send_text(target: str, text: str) -> None:
    result = herdr_command(["pane", "run", target, text])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Herdr rejected input for {target}")


@dataclass
class TmuxInputStatus:
    registered: bool
    processed: bool
    pending: bool
    working: bool
    changed: bool
    capture: str = ""


@dataclass
class TmuxSendResult:
    processed: bool
    registered: bool
    pending: bool
    working: bool
    attempts: int
    elapsed_seconds: float
    warning_sent: bool = False
    final_warning_sent: bool = False


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def _empty(self) -> dict[str, Any]:
        return {"alerts": {}, "last_update_id": None, "last_alert_id": ""}

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self._empty()
        if not isinstance(payload, dict):
            return self._empty()
        alerts = payload.get("alerts")
        if not isinstance(alerts, dict):
            payload["alerts"] = {}
        return payload

    def write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def update(self, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("w", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
            payload = self.read()
            result = mutator(payload)
            prune_alerts(payload)
            self.write(payload)
            return result


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def env_files() -> list[Path]:
    raw = os.environ.get("TELEGRAM_BRIDGE_ENV_FILES", "").strip()
    if raw:
        return [Path(part).expanduser() for part in raw.split(":") if part.strip()]
    plugin_config = os.environ.get("HERDR_PLUGIN_CONFIG_DIR", "").strip()
    paths = [Path(plugin_config) / ".env"] if plugin_config else []
    return paths + list(DEFAULT_ENV_FILES)


def env_value(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    for path in env_files():
        values = load_env_file(path)
        for name in names:
            value = values.get(name, "").strip()
            if value:
                return value
    return default


def state_file() -> Path:
    return Path(env_value("TELEGRAM_BRIDGE_STATE_FILE", default=str(DEFAULT_STATE_FILE))).expanduser()


def bot_token() -> str:
    return env_value("TELEGRAM_BRIDGE_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "LIVE_OPS_TELEGRAM_BOT_TOKEN")


def notify_chat_id() -> str:
    return env_value(
        "TELEGRAM_BRIDGE_NOTIFY_CHAT_ID",
        "TELEGRAM_BRIDGE_CHAT_ID",
        "TELEGRAM_CHAT_ID",
        "LIVE_OPS_TELEGRAM_CHAT_ID",
    )


def allowed_chat_ids() -> set[str]:
    raw = env_value("TELEGRAM_BRIDGE_ALLOWED_CHAT_IDS", "TELEGRAM_BRIDGE_ALLOWED_CHAT_ID")
    ids = {part.strip() for part in re.split(r"[, ]+", raw) if part.strip()}
    notify_id = notify_chat_id()
    if notify_id:
        ids.add(notify_id)
    return ids


def allowed_username() -> str:
    value = env_value("TELEGRAM_BRIDGE_ALLOWED_USERNAME").strip()
    return value.lstrip("@").lower()


def bool_env_value(*names: str, default: bool = False) -> bool:
    raw = env_value(*names, default="1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def telegram_enabled() -> bool:
    if bool_env_value(
        "TELEGRAM_BRIDGE_DISABLED",
        "AGENT_TELEGRAM_BRIDGE_DISABLED",
        "DISABLE_TELEGRAM_BRIDGE",
        "DISABLE_TELEGRAM",
    ):
        return False
    return bool_env_value(
        "TELEGRAM_BRIDGE_ENABLED",
        "AGENT_TELEGRAM_BRIDGE_ENABLED",
        "ENABLE_TELEGRAM_BRIDGE",
        "ENABLE_TELEGRAM",
        default=True,
    )


def telegram_forum_topics_enabled() -> bool:
    return telegram_enabled() and bool_env_value(
        "TELEGRAM_BRIDGE_FORUM_TOPICS",
        "TELEGRAM_BRIDGE_ENABLE_FORUM_TOPICS",
    )


def slack_bot_token() -> str:
    return env_value("SLACK_BRIDGE_BOT_TOKEN", "SLACK_BOT_TOKEN")


def slack_channel_id() -> str:
    return env_value("SLACK_BRIDGE_CHANNEL_ID", "SLACK_CHANNEL_ID")


def slack_signing_secret() -> str:
    return env_value("SLACK_BRIDGE_SIGNING_SECRET", "SLACK_SIGNING_SECRET")


def slack_http_host() -> str:
    return env_value("SLACK_BRIDGE_HTTP_HOST", default="127.0.0.1")


def slack_http_port() -> int:
    return int(env_value("SLACK_BRIDGE_HTTP_PORT", default="8787"))


def slack_http_path() -> str:
    value = env_value("SLACK_BRIDGE_HTTP_PATH", default="/slack/events").strip()
    return value if value.startswith("/") else f"/{value}"


def slack_bridge_mode() -> str:
    value = env_value("SLACK_BRIDGE_MODE", default="poll").strip().lower()
    return value or "poll"


def slack_poll_interval_seconds() -> float:
    raw = env_value("SLACK_BRIDGE_POLL_INTERVAL", default=str(DEFAULT_SLACK_POLL_INTERVAL_SECONDS))
    try:
        return min(MAX_SLACK_POLL_INTERVAL_SECONDS, max(1.0, float(raw)))
    except Exception:
        return DEFAULT_SLACK_POLL_INTERVAL_SECONDS


def slack_poll_limit() -> int:
    raw = env_value("SLACK_BRIDGE_POLL_LIMIT", default=str(DEFAULT_SLACK_POLL_LIMIT))
    try:
        return max(1, min(1000, int(raw)))
    except Exception:
        return DEFAULT_SLACK_POLL_LIMIT


def ca_bundle() -> str:
    explicit = env_value("TELEGRAM_CA_BUNDLE", "SSL_CERT_FILE")
    if explicit and Path(explicit).exists():
        return explicit
    for candidate in (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/ssl/cert.pem",
        ssl.get_default_verify_paths().cafile or "",
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def telegram_request(method: str, params: dict[str, Any], *, use_post: bool = False) -> Any:
    token = bot_token()
    if not token:
        raise RuntimeError("Telegram bot token is not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    if use_post:
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", method="GET")
    bundle = ca_bundle()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=35, context=context) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload.get("result")


def telegram_markdown_v2_escape(text: str) -> str:
    return "".join(
        f"\\{char}" if char in TELEGRAM_MARKDOWN_V2_SPECIAL_CHARS else char for char in str(text)
    )


def telegram_send_message(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    markdown_escaped: bool = False,
    message_thread_id: int | str | None = None,
) -> dict[str, Any]:
    outbound_text = text if markdown_escaped else telegram_markdown_v2_escape(text)
    if len(outbound_text) > MAX_TELEGRAM_TEXT:
        raise ValueError("telegram_send_message received oversized text; use telegram_send_messages")
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": outbound_text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        body["reply_markup"] = reply_markup
    if message_thread_id not in (None, ""):
        body["message_thread_id"] = int(message_thread_id)
    result = telegram_request("sendMessage", body, use_post=True)
    return result if isinstance(result, dict) else {}


def split_text_at_boundary(text: str, max_chars: int) -> tuple[str, str]:
    if len(text) <= max_chars:
        return text, ""
    minimum_boundary = max(80, max_chars // 4)
    for pattern in ("\n\n", "\n", " "):
        index = text.rfind(pattern, 0, max_chars + 1)
        if index >= minimum_boundary:
            end = index + (len(pattern) if pattern == " " else 0)
            return text[:end].rstrip(), text[end:].lstrip("\n ")
    return text[:max_chars].rstrip(), text[max_chars:].lstrip()


def split_telegram_text(
    text: str,
    *,
    continuation_prefix: str = "",
    max_chars: int | None = None,
) -> list[str]:
    limit = MAX_TELEGRAM_TEXT if max_chars is None else max_chars
    if limit <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    first = True
    while remaining:
        prefix = "" if first else continuation_prefix
        budget = limit - len(prefix)
        if budget <= 0:
            raise ValueError("continuation prefix is too long for Telegram message limit")
        chunk, remaining = split_text_at_boundary(remaining, budget)
        if not chunk and remaining:
            chunk, remaining = remaining[:budget], remaining[budget:]
        chunks.append(f"{prefix}{chunk}")
        first = False
    return chunks


def telegram_send_messages(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
    continuation_prefix: str = "",
    message_thread_id: int | str | None = None,
) -> list[dict[str, Any]]:
    chunks = split_telegram_text(
        telegram_markdown_v2_escape(text),
        continuation_prefix=telegram_markdown_v2_escape(continuation_prefix),
    )
    results: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        results.append(
            telegram_send_message(
                chat_id,
                chunk,
                reply_markup=markup,
                markdown_escaped=True,
                message_thread_id=message_thread_id,
            )
        )
    return results


def telegram_get_updates(offset: int | None, timeout: int, limit: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": timeout, "limit": limit}
    if offset is not None:
        params["offset"] = offset
    result = telegram_request("getUpdates", params)
    return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []


def telegram_create_forum_topic(chat_id: str, name: str) -> int:
    result = telegram_request(
        "createForumTopic",
        {"chat_id": chat_id, "name": name[:128]},
        use_post=True,
    )
    if not isinstance(result, dict) or result.get("message_thread_id") is None:
        raise RuntimeError(f"Telegram createForumTopic returned unexpected result: {result}")
    return int(result["message_thread_id"])


def alert_tmux_session_name(alert: dict[str, Any]) -> str:
    session = str(alert.get("session_name") or "").strip()
    if session:
        return lowercase_tmux_session_label(session)
    display_target = str(alert.get("display_target") or "").strip()
    if display_target and not display_target.startswith("%"):
        return lowercase_tmux_session_label(display_target.split(":", 1)[0])
    return lowercase_tmux_session_label(str(alert.get("agent") or "agent").strip())


def alert_tmux_window_name(alert: dict[str, Any]) -> str:
    return lowercase_tmux_session_label(str(alert.get("window_name") or "").strip())


def alert_tmux_window_index(alert: dict[str, Any]) -> str:
    window_index = str(alert.get("window_index") or "").strip()
    if window_index:
        return window_index
    display_target = str(alert.get("display_target") or "").strip()
    match = re.match(r"^[^:]+:(\d+)(?:\.\d+)?$", display_target)
    return match.group(1) if match else ""


def alert_thread_label(alert: dict[str, Any]) -> str:
    session = alert_tmux_session_name(alert)
    window_index = alert_tmux_window_index(alert)
    window = alert_tmux_window_name(alert)
    if session and window_index and window and window != "(unnamed)":
        return f"{session}:{window_index}:{window}"
    if session and window_index:
        return f"{session}:{window_index}"
    if session and window and window not in {"(unnamed)", session}:
        return f"{session}:{window}"
    return session or window or "agent"


def alert_thread_key(alert: dict[str, Any]) -> str:
    return alert_thread_label(alert)


def alert_legacy_thread_keys(alert: dict[str, Any]) -> list[str]:
    if alert_tmux_session_name(alert) and alert_tmux_window_index(alert) and alert_tmux_window_name(alert):
        return []
    keys: list[str] = []
    values = [
        alert.get("display_target"),
        alert.get("send_target"),
    ]
    if ":" not in alert_thread_key(alert):
        values.extend([alert.get("session_name"), alert.get("agent")])
    for value in values:
        key = lowercase_tmux_session_label(str(value or "").strip())
        if key and key not in keys:
            keys.append(key)
    return keys


def thread_record_for_alert(records: dict[str, Any], alert: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    key = alert_thread_key(alert)
    existing = records.get(key)
    if isinstance(existing, dict):
        return key, existing
    for candidate in alert_legacy_thread_keys(alert):
        existing = records.get(candidate)
        if isinstance(existing, dict):
            return candidate, existing
    if ":" not in alert_thread_key(alert):
        session = alert_tmux_session_name(alert)
        if not session:
            return "", None
        for candidate in sorted(records):
            normalized = lowercase_tmux_session_label(str(candidate))
            if normalized == session or normalized.split(":", 1)[0] == session:
                existing = records.get(candidate)
                if isinstance(existing, dict):
                    return candidate, existing
    return "", None


def alert_thread_name(alert: dict[str, Any]) -> str:
    label = alert_thread_label(alert)
    name = f"tmux {label}" if label else "tmux session"
    return re.sub(r"\s+", " ", name).strip()[:128] or "agent session"


def ensure_telegram_topic_id(store: StateStore, chat_id: str, alert: dict[str, Any]) -> int | None:
    if not telegram_forum_topics_enabled():
        return None
    key = alert_thread_key(alert)
    topic_name = alert_thread_name(alert)
    state = store.read()
    topics = state.get("telegram_topics") if isinstance(state.get("telegram_topics"), dict) else {}
    existing_key, existing = thread_record_for_alert(topics, alert)
    if existing and existing.get("message_thread_id") is not None:
        if existing_key != key:
            def migrate(payload: dict[str, Any]) -> None:
                payload.setdefault("telegram_topics", {})[key] = {
                    **existing,
                    "name": topic_name,
                    "updated_at": int(time.time()),
                }
                payload["updated_at"] = int(time.time())

            store.update(migrate)
        return int(existing["message_thread_id"])
    try:
        thread_id = telegram_create_forum_topic(chat_id, topic_name)
    except Exception as exc:
        print(f"[agent_telegram_bridge telegram-topic] {exc}", file=sys.stderr)
        return None

    def mutate(payload: dict[str, Any]) -> None:
        payload.setdefault("telegram_topics", {})[key] = {
            "message_thread_id": thread_id,
            "name": topic_name,
            "updated_at": int(time.time()),
        }
        payload["updated_at"] = int(time.time())

    store.update(mutate)
    return thread_id


def telegram_message_thread_id(message: dict[str, Any]) -> int | None:
    raw = message.get("message_thread_id")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except Exception:
        return None


def slack_enabled() -> bool:
    return bool(slack_bot_token() and slack_channel_id())


def slack_escape_text(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_request(method: str, params: dict[str, Any], *, use_get: bool = False) -> dict[str, Any]:
    token = slack_bot_token()
    if not token:
        raise RuntimeError("Slack bot token is not configured")
    if use_get:
        query = urllib.parse.urlencode(params)
        url = f"https://slack.com/api/{method}?{query}"
        req = urllib.request.Request(url, method="GET")
    else:
        url = f"https://slack.com/api/{method}"
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {token}")
    bundle = ca_bundle()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=35, context=context) as response:
        payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"Slack API error: {payload}")
    return payload


def slack_send_message(channel_id: str, text: str, *, thread_ts: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {
        "channel": channel_id,
        "text": slack_escape_text(text),
        "mrkdwn": True,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts
    return slack_request("chat.postMessage", body)


def slack_send_messages(channel_id: str, text: str, *, thread_ts: str = "") -> list[dict[str, Any]]:
    chunks = split_telegram_text(text, max_chars=MAX_SLACK_TEXT)
    return [slack_send_message(channel_id, chunk, thread_ts=thread_ts) for chunk in chunks]


def slack_retry_after_seconds(exc: Exception) -> float | None:
    if not isinstance(exc, urllib.error.HTTPError) or exc.code != 429:
        return None
    raw = str(exc.headers.get("Retry-After") or "").strip() if exc.headers else ""
    try:
        return max(float(raw), 1.0) if raw else SLACK_RATE_LIMIT_DEFAULT_SLEEP_SECONDS
    except ValueError:
        return SLACK_RATE_LIMIT_DEFAULT_SLEEP_SECONDS


def compact_alert_header(alert: dict[str, Any]) -> str:
    hostname = str(alert.get("hostname") or socket.gethostname() or "").strip()
    session = lowercase_tmux_session_label(alert.get("session_name") or alert.get("agent") or "agent")
    window_index = str(alert.get("window_index") or "").strip()
    window = str(alert.get("window_name") or "(unnamed)").strip()
    ticket = canonical_ticket_id(alert.get("id") or "")
    return f"{hostname}:{session}:{window_index}:{window} [{ticket}]"


def slack_alert_message(alert: dict[str, Any]) -> str:
    question = str(alert.get("question") or "")
    return (
        f"{compact_alert_header(alert)}\n"
        f"{str(alert.get('agent', 'agent')).lower()} needs input\n\n"
        f"{question}\n\n"
        f"Reply in this thread to send text to that tmux pane."
    )


def slack_notify_alert(store: StateStore, alert: dict[str, Any]) -> None:
    if not slack_enabled():
        return
    channel_id = slack_channel_id()
    results = slack_send_messages(channel_id, slack_alert_message(alert))
    result = results[-1] if results else {}
    thread_ts = str((results[0] if results else {}).get("ts") or result.get("ts") or "")
    if not thread_ts:
        raise RuntimeError(f"Slack chat.postMessage returned no ts: {results}")
    alert["slack_channel_id"] = channel_id
    alert["slack_thread_ts"] = thread_ts
    alert["slack_message_ts"] = result.get("ts")
    alert["slack_message_ts_values"] = [item.get("ts") for item in results if item.get("ts")]
    mark_slack_thread_seen(store, channel_id, thread_ts, str(alert.get("slack_message_ts") or ""))


def latest_alert_for_slack_thread(state: dict[str, Any], channel_id: str, thread_ts: str) -> dict[str, Any] | None:
    alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    candidates = [
        alert
        for alert in alerts.values()
        if isinstance(alert, dict)
        and str(alert.get("slack_channel_id") or "") == channel_id
        and str(alert.get("slack_thread_ts") or "") == thread_ts
    ]
    candidates.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    return candidates[0] if candidates else None


def ticket_from_slack_text_or_thread(text: str, channel_id: str, thread_ts: str, state: dict[str, Any]) -> str:
    ticket = extract_ticket_from_text(text)
    if ticket:
        return ticket
    alert = latest_alert_for_slack_thread(state, channel_id, thread_ts)
    if alert:
        return canonical_ticket_id(alert.get("id") or "")
    return ""


def slack_command(text: str) -> tuple[str, str]:
    command, rest = slash_command(text)
    if command:
        return command, rest
    first, _, remainder = text.strip().partition(" ")
    command = first.lower()
    if command in {"start", "help", "alerts", "status", "reply"}:
        return command, remainder.strip()
    return "", text


def process_slack_message_event(event: dict[str, Any], store: StateStore) -> None:
    if str(event.get("type") or "") != "message":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    channel_id = str(event.get("channel") or "").strip()
    text = str(event.get("text") or "").strip()
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip()
    if not channel_id or not text or not thread_ts:
        return

    response_holder: dict[str, str] = {}

    def send_warning(message: str) -> None:
        slack_send_messages(channel_id, message, thread_ts=thread_ts)

    def mutate(state: dict[str, Any]) -> None:
        command, rest = slack_command(text)
        if command in {"start", "help"}:
            response_holder["text"] = (
                "Commands:\n"
                "alerts - list open routed alerts\n"
                "status [id] - show the tmux pane tail; defaults to this thread"
            )
            return
        if command == "alerts":
            response_holder["text"] = format_alerts(state)
            return
        if command == "status":
            ticket = ticket_from_command_token(rest.split(maxsplit=1)[0], state) if rest.strip() else ""
            if not ticket:
                ticket = ticket_from_slack_text_or_thread("", channel_id, thread_ts, state)
            alert = find_alert(state, ticket)
            response_holder["text"] = format_status(alert) if alert else f"Unknown alert id: {ticket}"
            return
        if command == "reply":
            ticket, outbound = reply_target_and_text(
                command,
                rest,
                {"text": rest},
                {
                    **state,
                    "last_alert_id": ticket_from_slack_text_or_thread("", channel_id, thread_ts, state),
                },
            )
            if not outbound:
                response_holder["text"] = "Usage: reply [id] <text>"
                return
            response_holder["text"] = send_reply_to_alert(state, ticket, outbound, warning_callback=send_warning)
            return
        if command:
            response_holder["text"] = "Unknown command. Send /help."
            return
        ticket = ticket_from_slack_text_or_thread(text, channel_id, thread_ts, state)
        if not ticket:
            response_holder["text"] = "No alert selected. Reply in an alert thread."
            return
        response_holder["text"] = send_reply_to_alert(state, ticket, text, warning_callback=send_warning)

    store.update(mutate)
    if response_holder.get("text"):
        slack_send_messages(channel_id, response_holder["text"], thread_ts=thread_ts)


def slack_ts_decimal(value: str) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def slack_ts_greater(left: str, right: str) -> bool:
    return slack_ts_decimal(left) > slack_ts_decimal(right)


def slack_thread_poll_key(channel_id: str, thread_ts: str) -> str:
    return f"{channel_id}:{thread_ts}"


def mark_slack_thread_seen(store: StateStore, channel_id: str, thread_ts: str, ts: str) -> None:
    if not channel_id or not thread_ts or not ts:
        return

    def mutate(state: dict[str, Any]) -> None:
        key = slack_thread_poll_key(channel_id, thread_ts)
        seen = state.setdefault("slack_poll_seen_ts", {})
        if not isinstance(seen, dict):
            state["slack_poll_seen_ts"] = seen = {}
        previous = str(seen.get(key) or "")
        if slack_ts_greater(ts, previous):
            seen[key] = ts
            state["updated_at"] = int(time.time())

    store.update(mutate)


def is_slack_thread_not_found_error(exc: Exception) -> bool:
    return "thread_not_found" in str(exc)


def prune_stale_slack_thread(store: StateStore, channel_id: str, thread_ts: str) -> list[str]:
    if not channel_id or not thread_ts:
        return []

    def mutate(state: dict[str, Any]) -> list[str]:
        removed: list[str] = []
        threads = state.get("slack_threads") if isinstance(state.get("slack_threads"), dict) else {}
        for name, item in list(threads.items()):
            if not isinstance(item, dict):
                continue
            if str(item.get("channel_id") or "") == channel_id and str(item.get("thread_ts") or "") == thread_ts:
                removed.append(str(name))
                del threads[name]

        seen = state.get("slack_poll_seen_ts") if isinstance(state.get("slack_poll_seen_ts"), dict) else {}
        if seen.pop(slack_thread_poll_key(channel_id, thread_ts), None) is not None:
            removed.append(slack_thread_poll_key(channel_id, thread_ts))

        alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
        for alert in alerts.values():
            if not isinstance(alert, dict):
                continue
            if str(alert.get("slack_channel_id") or "") != channel_id:
                continue
            if str(alert.get("slack_thread_ts") or "") != thread_ts:
                continue
            for field in (
                "slack_channel_id",
                "slack_thread_ts",
                "slack_message_ts",
                "slack_message_ts_values",
            ):
                alert.pop(field, None)
            alert["slack_thread_pruned_at"] = int(time.time())
            removed.append(str(alert.get("id") or "alert"))

        if removed:
            state["updated_at"] = int(time.time())
        return removed

    return store.update(mutate)


def slack_conversations_replies(
    channel_id: str,
    thread_ts: str,
    *,
    oldest: str = "",
    cursor: str = "",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": slack_poll_limit(),
    }
    if oldest:
        params["oldest"] = oldest
        params["inclusive"] = False
    if cursor:
        params["cursor"] = cursor
    return slack_request("conversations.replies", params, use_get=True)


def slack_thread_targets(state: dict[str, Any]) -> list[tuple[str, str]]:
    targets: dict[str, tuple[str, str]] = {}
    alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    now = int(time.time())
    for alert in alerts.values():
        if not isinstance(alert, dict):
            continue
        try:
            created_at = int(alert.get("created_at"))
        except Exception:
            continue
        if now - created_at > RECENT_ALERT_ID_SECONDS:
            continue
        channel_id = str(alert.get("slack_channel_id") or "").strip()
        thread_ts = str(alert.get("slack_thread_ts") or "").strip()
        if channel_id and thread_ts:
            targets[slack_thread_poll_key(channel_id, thread_ts)] = (channel_id, thread_ts)
    return sorted(targets.values())


def initial_slack_poll_ts(state: dict[str, Any], channel_id: str, thread_ts: str) -> str:
    key = slack_thread_poll_key(channel_id, thread_ts)
    seen = state.get("slack_poll_seen_ts") if isinstance(state.get("slack_poll_seen_ts"), dict) else {}
    current = str(seen.get(key) or "")
    alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    for alert in alerts.values():
        if not isinstance(alert, dict):
            continue
        if str(alert.get("slack_channel_id") or "") != channel_id:
            continue
        if str(alert.get("slack_thread_ts") or "") != thread_ts:
            continue
        values = alert.get("slack_message_ts_values")
        ts_values = values if isinstance(values, list) else []
        for ts in [alert.get("slack_message_ts"), *ts_values]:
            ts_text = str(ts or "")
            if ts_text and slack_ts_greater(ts_text, current):
                current = ts_text
    return current


def slack_poll_thread(store: StateStore, channel_id: str, thread_ts: str) -> int:
    state = store.read()
    oldest = initial_slack_poll_ts(state, channel_id, thread_ts)
    cursor = ""
    messages: list[dict[str, Any]] = []
    while True:
        payload = slack_conversations_replies(channel_id, thread_ts, oldest=oldest, cursor=cursor)
        page = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        messages.extend(item for item in page if isinstance(item, dict))
        metadata = payload.get("response_metadata") if isinstance(payload.get("response_metadata"), dict) else {}
        cursor = str(metadata.get("next_cursor") or "").strip()
        if not cursor:
            break

    messages.sort(key=lambda item: slack_ts_decimal(str(item.get("ts") or "")))
    newest = oldest
    processed = 0
    for message in messages:
        ts = str(message.get("ts") or "").strip()
        if not ts or not slack_ts_greater(ts, oldest):
            continue
        if slack_ts_greater(ts, newest):
            newest = ts
        event = dict(message)
        event.setdefault("type", "message")
        event["channel"] = channel_id
        event["thread_ts"] = str(event.get("thread_ts") or thread_ts)
        if event.get("bot_id") or event.get("subtype"):
            continue
        if not str(event.get("text") or "").strip():
            continue
        process_slack_message_event(event, store)
        processed += 1
    mark_slack_thread_seen(store, channel_id, thread_ts, newest)
    return processed


def slack_poll_once(store: StateStore) -> int:
    state = store.read()
    total = 0
    for channel_id, thread_ts in slack_thread_targets(state):
        try:
            total += slack_poll_thread(store, channel_id, thread_ts)
        except RuntimeError as exc:
            if not is_slack_thread_not_found_error(exc):
                raise
            removed = prune_stale_slack_thread(store, channel_id, thread_ts)
            print(
                "[agent_telegram_bridge slack-daemon] pruned missing Slack thread "
                f"{channel_id}:{thread_ts} ({', '.join(removed) or 'state only'})",
                file=sys.stderr,
            )
    return total


def slack_verify_signature(headers: Any, body: bytes) -> bool:
    secret = slack_signing_secret()
    if not secret:
        return False
    timestamp = str(headers.get("X-Slack-Request-Timestamp") or "")
    signature = str(headers.get("X-Slack-Signature") or "")
    try:
        ts = int(timestamp)
    except Exception:
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    base = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def make_slack_http_handler(store: StateStore) -> type[BaseHTTPRequestHandler]:
    expected_path = slack_http_path()

    class SlackEventsHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            print(f"[agent_telegram_bridge slack-http] {format % args}", file=sys.stderr)

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != expected_path:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length)
            if not slack_verify_signature(self.headers, body):
                self._send_json(401, {"ok": False, "error": "bad_signature"})
                return
            try:
                payload = json.loads(body.decode("utf-8", errors="ignore"))
            except Exception:
                self._send_json(400, {"ok": False, "error": "bad_json"})
                return
            if payload.get("type") == "url_verification":
                self._send_json(200, {"challenge": payload.get("challenge", "")})
                return
            if payload.get("type") == "event_callback":
                event = payload.get("event")
                if isinstance(event, dict):
                    process_slack_message_event(event, store)
                self._send_json(200, {"ok": True})
                return
            self._send_json(200, {"ok": True})

    return SlackEventsHandler


def load_payload(argv: list[str]) -> dict[str, Any]:
    if argv and argv[0].lstrip().startswith("{"):
        return json.loads(argv[0])
    data = sys.stdin.read().strip()
    return json.loads(data) if data else {}


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict) and any(k in data for k in ("thread-id", "last-assistant-message", "cwd")):
        merged = dict(data)
        for key in ("type", "event", "event_type", "name"):
            if key in payload and key not in merged:
                merged[key] = payload[key]
        return merged
    return payload


def payload_kind(payload: dict[str, Any]) -> str:
    event = payload.get("type") or payload.get("event") or payload.get("event_type") or payload.get("name")
    if event in {"agent-turn-complete", "agent.turn-complete", "agent.turn_complete", "agent_turn_complete"}:
        return "codex"
    if "last-assistant-message" in payload and "thread-id" in payload:
        return "codex"
    if payload.get("hook_event_name") == "Notification":
        return "claude"
    if payload.get("herdr_event") or payload.get("pane_id") and payload.get("agent_status"):
        return "herdr"
    return ""


def subprocess_text(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def current_tmux_pane() -> PaneInfo:
    herdr_target = os.environ.get("HERDR_PANE_ID", "").strip()
    if herdr_target:
        return herdr_pane_info(herdr_target)
    target = os.environ.get("TMUX_PANE", "").strip()
    if not target:
        return PaneInfo()
    return tmux_pane_info(target)


def tmux_pane_info(target: str) -> PaneInfo:
    fmt = "\t".join(
        [
            "#{session_name}",
            "#{window_index}",
            "#{window_name}",
            "#{pane_index}",
            "#{pane_id}",
            "#{pane_current_command}",
            "#{pane_current_path}",
            "#{pane_title}",
        ]
    )
    result = subprocess_text(["tmux", "display-message", "-p", "-t", target, fmt])
    if result.returncode != 0:
        return PaneInfo(pane_id=target if target.startswith("%") else "")
    parts = (result.stdout.rstrip("\n").split("\t") + [""] * 8)[:8]
    return PaneInfo(*parts)


def tmux_target_exists(target: str) -> bool:
    if not target:
        return False
    if is_herdr_target(target):
        return herdr_target_exists(target)
    return subprocess_text(["tmux", "display-message", "-p", "-t", target, "#{pane_id}"]).returncode == 0


def tmux_capture(target: str, lines: int = 30) -> str:
    if not target:
        return ""
    if is_herdr_target(target):
        return herdr_capture(target, lines)
    result = subprocess_text(["tmux", "capture-pane", "-p", "-t", target, "-S", f"-{lines}"])
    return (result.stdout or result.stderr or "").strip()


def is_terminal_chrome_line(line: str) -> bool:
    clean = line.strip()
    if not clean:
        return True
    if re.match(r"^gpt-[\w.-]+\s+.*\[[^\]]+\]", clean):
        return True
    if re.match(r"^gpt-[\w.-]+(?:\s+\w+)?\s+\u00b7\s+", clean):
        return True
    if re.match(r"^[\-\u2500]{12,}$", clean):
        return True
    if re.match(r"^[\u25e6*]?\s*Working \(", clean):
        return True
    if re.match(r"^\(\d+\s*[smh]", clean):
        return True
    if " ctrl + t to view transcript" in clean:
        return True
    return False


def is_working_status_line(line: str) -> bool:
    return bool(re.match(r"^[\u25e6*]?\s*Working \(", line.strip()))


def tmux_last_message(target: str, lines: int = 80) -> str:
    capture = tmux_capture(target, lines=lines)
    for line in reversed(capture.splitlines()):
        clean = line.strip()
        if not is_terminal_chrome_line(clean):
            return clean
    return ""


def tmux_send_key(target: str, key: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, key], check=True)


def tmux_send_literal(target: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", text], check=True)


def tmux_clear_pending_input(target: str) -> None:
    for key in ("C-u", "C-a", "C-k"):
        tmux_send_key(target, key)


def tmux_paste_text(target: str, text: str) -> None:
    buffer_name = f"agent_bridge_submit_{os.getpid()}_{int(time.monotonic() * 1000)}"
    subprocess.run(["tmux", "set-buffer", "-b", buffer_name, text], check=True)
    try:
        subprocess.run(["tmux", "paste-buffer", "-d", "-b", buffer_name, "-t", target], check=True)
    except Exception:
        subprocess.run(["tmux", "delete-buffer", "-b", buffer_name], check=False)
        raise


def tmux_submit_pasted_newline(target: str) -> None:
    tmux_paste_text(target, "\n")


def tmux_submit_input(target: str) -> None:
    for key in ("Enter", "C-m", "C-j"):
        tmux_send_key(target, key)
    tmux_submit_pasted_newline(target)


def line_has_pending_input(line: str, text: str) -> bool:
    clean_line = " ".join(line.strip().split())
    clean_text = normalize_outbound_text(text)
    if not clean_line or not clean_text:
        return False
    prompt_line = re.sub(r"^[>\u203a]\s*", "", clean_line).strip()
    return prompt_line == clean_text


def capture_has_pending_input(capture: str, text: str) -> bool:
    for line in reversed(capture.splitlines()):
        clean = line.strip()
        if is_terminal_chrome_line(clean):
            continue
        return line_has_pending_input(clean, text)
    return False


def capture_has_working_status(capture: str) -> bool:
    return any(is_working_status_line(line) for line in capture.splitlines())


def tmux_input_status(target: str, text: str, baseline_capture: str) -> TmuxInputStatus:
    capture = tmux_capture(target, lines=80)
    pending = capture_has_pending_input(capture, text)
    working = capture_has_working_status(capture)
    changed = bool(capture.strip()) and capture != baseline_capture
    registered = pending or working or changed
    processed = registered and not pending and not working
    return TmuxInputStatus(
        registered=registered,
        processed=processed,
        pending=pending,
        working=working,
        changed=changed,
        capture=capture,
    )


def tmux_send_warning(
    callback: Callable[[str], None] | None,
    *,
    final: bool,
    target_label: str,
    elapsed_seconds: float,
    monitor_seconds: float,
    retry_seconds: float,
    attempts: int,
    status: TmuxInputStatus,
) -> None:
    if callback is None:
        return
    state = []
    if status.registered:
        state.append("registered")
    if status.pending:
        state.append("pending")
    if status.working:
        state.append("working")
    if not state:
        state.append("not registered")
    prefix = "Warning" if not final else "Final warning"
    if status.working and not status.pending:
        retry_note = "pane accepted the message and is working"
    else:
        retry_note = f"clearing, retyping, and retrying Enter/C-m every {retry_seconds:g}s"
    try:
        callback(
            f"{prefix}: input to {target_label} has not been confirmed processed after "
            f"{int(elapsed_seconds)}s. State: {', '.join(state)}; attempts={attempts}; "
            f"{retry_note}; monitor limit={int(monitor_seconds)}s."
        )
    except Exception as exc:
        print(f"[agent_telegram_bridge tmux-send-warning] {exc}", file=sys.stderr)


def tmux_send_text(
    target: str,
    text: str,
    *,
    warning_callback: Callable[[str], None] | None = None,
    target_label: str | None = None,
) -> TmuxSendResult:
    clean = normalize_outbound_text(text)
    if not clean:
        raise ValueError("empty reply")
    if is_herdr_target(target):
        start = time.monotonic()
        herdr_send_text(target, clean)
        return TmuxSendResult(
            processed=True,
            registered=True,
            pending=False,
            working=False,
            attempts=1,
            elapsed_seconds=time.monotonic() - start,
        )
    monitor_seconds = float(
        env_value("TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_SECONDS", default=str(TMUX_SEND_MONITOR_SECONDS))
    )
    poll_seconds = float(
        env_value("TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL", default=str(TMUX_SEND_MONITOR_INTERVAL_SECONDS))
    )
    retry_seconds = float(
        env_value("TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL", default=str(TMUX_SEND_RETRY_INTERVAL_SECONDS))
    )
    warning_seconds = float(
        env_value("TELEGRAM_BRIDGE_TMUX_SEND_WARNING_SECONDS", default=str(TMUX_SEND_WARNING_SECONDS))
    )
    label = lowercase_tmux_session_label(target_label or target)
    baseline = tmux_capture(target, lines=80)
    start = time.monotonic()
    deadline = start + max(0.0, monitor_seconds)
    next_retry_at = start + max(0.0, retry_seconds)
    warned = False
    attempts = 0

    def submit_attempt() -> None:
        nonlocal attempts
        tmux_submit_input(target)
        attempts += 1

    def retype_attempt() -> None:
        tmux_clear_pending_input(target)
        tmux_send_literal(target, clean)
        submit_attempt()

    tmux_send_literal(target, clean)
    submit_attempt()
    last_status = TmuxInputStatus(
        registered=False,
        processed=False,
        pending=False,
        working=False,
        changed=False,
        capture=baseline,
    )

    while True:
        now = time.monotonic()
        elapsed = max(0.0, now - start)
        last_status = tmux_input_status(target, clean, baseline)
        if (last_status.working and not last_status.pending) or last_status.processed:
            return TmuxSendResult(
                processed=True,
                registered=last_status.registered,
                pending=last_status.pending,
                working=last_status.working,
                attempts=attempts,
                elapsed_seconds=elapsed,
                warning_sent=warned,
            )
        if not warned and elapsed >= warning_seconds:
            tmux_send_warning(
                warning_callback,
                final=False,
                target_label=label,
                elapsed_seconds=elapsed,
                monitor_seconds=monitor_seconds,
                retry_seconds=retry_seconds,
                attempts=attempts,
                status=last_status,
            )
            warned = True
        if now >= deadline:
            if not last_status.working or last_status.pending:
                tmux_clear_pending_input(target)
            tmux_send_warning(
                warning_callback,
                final=True,
                target_label=label,
                elapsed_seconds=elapsed,
                monitor_seconds=monitor_seconds,
                retry_seconds=retry_seconds,
                attempts=attempts,
                status=last_status,
            )
            return TmuxSendResult(
                processed=False,
                registered=last_status.registered,
                pending=last_status.pending,
                working=last_status.working,
                attempts=attempts,
                elapsed_seconds=elapsed,
                warning_sent=warned,
                final_warning_sent=True,
            )
        if now >= next_retry_at:
            if not last_status.working or last_status.pending:
                retype_attempt()
            next_retry_at = now + max(0.0, retry_seconds)
        sleep_for = min(max(0.0, poll_seconds), max(0.0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)


def normalize_outbound_text(text: str) -> str:
    return " ".join(part.strip() for part in text.replace("\r", "\n").splitlines() if part.strip())


def lowercase_tmux_session_label(target: Any) -> str:
    text = str(target or "")
    if text.startswith("%"):
        return text
    session, sep, rest = text.partition(":")
    return f"{session.lower()}{sep}{rest}"


def canonical_ticket_id(ticket_id: Any) -> str:
    text = str(ticket_id or "").strip().strip("[]")
    if re.fullmatch(r"\d", text):
        return text
    if re.fullmatch(r"[A-Za-z]{2}\d", text):
        return text.lower()
    return text.upper()


def ticket_prefix_from_window_name(window_name: Any) -> str:
    letters = re.findall(r"[a-z]", str(window_name or "").lower())
    if not letters:
        return "tm"
    if len(letters) == 1:
        return f"{letters[0]}x"
    return "".join(letters[:2])


def recent_alert_ids(alerts: Any, *, now: int | None = None) -> set[str]:
    current = int(time.time()) if now is None else now
    if not isinstance(alerts, dict):
        return set()
    ids: set[str] = set()
    for ticket_id, alert in alerts.items():
        if not isinstance(alert, dict):
            ids.add(canonical_ticket_id(ticket_id))
            continue
        created_at = int(alert.get("created_at") or current)
        if current - created_at <= RECENT_ALERT_ID_SECONDS:
            ids.add(canonical_ticket_id(alert.get("id") or ticket_id))
    return ids


def make_ticket_id(
    existing_ids: set[str] | None = None,
    *,
    window_name: Any = "",
    existing_alerts: dict[str, Any] | None = None,
    now: int | None = None,
) -> str:
    existing = {canonical_ticket_id(ticket) for ticket in (existing_ids or set())}
    existing.update(recent_alert_ids(existing_alerts, now=now))
    prefix = ticket_prefix_from_window_name(window_name)
    available = [digit for digit in string.digits if f"{prefix}{digit}" not in existing]
    if available:
        return f"{prefix}{random.choice(available)}"
    return f"{prefix}{random.choice(string.digits)}"


def question_from_payload(kind: str, payload: dict[str, Any]) -> str:
    if kind == "codex":
        return str(payload.get("last-assistant-message") or "Turn complete").strip()
    if kind == "herdr":
        status = str(payload.get("agent_status") or payload.get("status") or "blocked").strip().lower()
        message = str(payload.get("message") or "").strip()
        return f"Herdr agent is {status}" + (f": {message}" if message else "")
    ntype = str(payload.get("notification_type") or "notification")
    message = str(payload.get("message") or "").strip()
    return f"{ntype}: {message}".strip(": ")


def cwd_from_payload(payload: dict[str, Any], pane: PaneInfo) -> str:
    return str(payload.get("cwd") or pane.pane_current_path or os.getcwd()).strip()


def create_alert(
    kind: str,
    payload: dict[str, Any],
    pane: PaneInfo,
    ticket_id: str | None = None,
    existing_ticket_ids: set[str] | None = None,
    existing_alerts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at = int(time.time())
    ticket = ticket_id or make_ticket_id(
        existing_ticket_ids,
        window_name=pane.window_name,
        existing_alerts=existing_alerts,
        now=created_at,
    )
    hostname = socket.gethostname()
    question = question_from_payload(kind, payload)
    cwd = cwd_from_payload(payload, pane)
    return {
        "id": ticket,
        "status": "open",
        "agent": kind,
        "hostname": hostname,
        "created_at": created_at,
        "updated_at": created_at,
        "cwd": cwd,
        "question": question,
        "send_target": pane.send_target,
        "display_target": lowercase_tmux_session_label(pane.display_target),
        "session_name": pane.session_name.lower(),
        "window_index": pane.window_index,
        "window_name": pane.window_name,
        "pane_index": pane.pane_index,
        "pane_id": pane.pane_id,
        "pane_current_command": pane.pane_current_command,
        "pane_title": pane.pane_title,
        "backend": "herdr" if kind == "herdr" or is_herdr_target(pane.send_target) else "tmux",
        "source": {
            "thread_id": payload.get("thread-id", ""),
            "turn_id": payload.get("turn-id", ""),
            "session_id": payload.get("session_id", ""),
            "notification_type": payload.get("notification_type", ""),
        },
    }


def alert_message(alert: dict[str, Any]) -> str:
    question = str(alert.get("question") or "")
    return (
        f"{compact_alert_header(alert)}\n"
        f"{str(alert.get('agent', 'agent')).lower()} needs input\n\n"
        f"{question}\n\n"
        f"reply to this Telegram message, or send:\n"
        f"/reply your response\n"
        f"/status"
    )


def alert_message_continuation_prefix(alert: dict[str, Any]) -> str:
    return f"{compact_alert_header(alert)} continued\n\n"


def prune_alerts(state: dict[str, Any]) -> None:
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        state["alerts"] = {}
        return
    now = int(time.time())
    ordered = sorted(alerts.items(), key=lambda item: int(item[1].get("created_at") or 0), reverse=True)
    keep: dict[str, Any] = {}
    for idx, (alert_id, alert) in enumerate(ordered):
        age = now - int(alert.get("created_at") or now)
        if idx < 100 and age < 7 * 24 * 3600:
            keep[alert_id] = alert
    state["alerts"] = keep


def notify(argv: list[str]) -> int:
    try:
        payload = normalize_payload(load_payload(argv))
        kind = payload_kind(payload)
        if not kind:
            return 0
        pane = current_tmux_pane()
        store = StateStore(state_file())
        existing = store.read().get("alerts", {})
        alert = create_alert(
            kind,
            payload,
            pane,
            existing_alerts=existing if isinstance(existing, dict) else {},
        )
        chat_id = notify_chat_id() if telegram_enabled() else ""
        if chat_id:
            message_thread_id = ensure_telegram_topic_id(store, chat_id, alert)
            results = telegram_send_messages(
                chat_id,
                alert_message(alert),
                continuation_prefix=alert_message_continuation_prefix(alert),
                reply_markup={
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder": (
                        f"Reply for {lowercase_tmux_session_label(alert.get('display_target') or alert['id'])}"
                    ),
                },
                message_thread_id=message_thread_id,
            )
            result = results[-1] if results else {}
            alert["telegram_chat_id"] = str(result.get("chat", {}).get("id") or chat_id)
            alert["telegram_message_id"] = result.get("message_id")
            alert["telegram_message_thread_id"] = message_thread_id
            alert["telegram_message_ids"] = [
                result.get("message_id") for result in results if result.get("message_id") is not None
            ]
        try:
            slack_notify_alert(store, alert)
        except Exception as exc:
            print(f"[agent_telegram_bridge slack-notify] {exc}", file=sys.stderr)

        def mutate(state: dict[str, Any]) -> None:
            state.setdefault("alerts", {})[alert["id"]] = alert
            state["last_alert_id"] = alert["id"]
            state["updated_at"] = int(time.time())

        store.update(mutate)
    except Exception as exc:
        print(f"[agent_telegram_bridge notify] {exc}", file=sys.stderr)
    return 0


def update_chat_trust(message: dict[str, Any], state: dict[str, Any]) -> bool:
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    username = str(sender.get("username") or "").strip().lstrip("@").lower()
    allowed_ids = allowed_chat_ids()
    allowed_user = allowed_username()
    if chat_id in allowed_ids:
        return True
    trusted = str(state.get("trusted_private_chat_id") or "").strip()
    if trusted and chat_id == trusted:
        return True
    if allowed_user and username == allowed_user and str(chat.get("type") or "") == "private":
        state["trusted_private_chat_id"] = chat_id
        return True
    return False


def message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "").strip()


def extract_ticket_from_text(text: str) -> str:
    for pattern in (BRACKET_TICKET_RE, TICKET_RE):
        match = pattern.search(text)
        if match and any(ch.isdigit() for ch in match.group(1)):
            return canonical_ticket_id(match.group(1))
    return ""


def alert_message_ids(alert: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    primary = alert.get("telegram_message_id")
    if primary is not None:
        ids.add(str(primary))
    raw_ids = alert.get("telegram_message_ids")
    if isinstance(raw_ids, list):
        ids.update(str(item) for item in raw_ids if item is not None)
    return ids


def ticket_from_replied_message_id(message: dict[str, Any], state: dict[str, Any]) -> str:
    reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    reply_message_id = str(reply.get("message_id") or "").strip()
    if not reply_message_id:
        return ""
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        return ""
    for ticket_id, alert in alerts.items():
        if not isinstance(alert, dict):
            continue
        alert_chat_id = str(alert.get("telegram_chat_id") or "").strip()
        if chat_id and alert_chat_id and chat_id != alert_chat_id:
            continue
        if reply_message_id in alert_message_ids(alert):
            return canonical_ticket_id(alert.get("id") or ticket_id)
    return ""


def ticket_from_message(message: dict[str, Any], state: dict[str, Any]) -> str:
    ticket = ticket_from_replied_message_id(message, state)
    if ticket:
        return ticket
    reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
    reply_text = message_text(reply)
    ticket = extract_ticket_from_text(reply_text)
    if ticket:
        return ticket
    text = message_text(message)
    ticket = extract_ticket_from_text(text)
    if ticket:
        return ticket
    return canonical_ticket_id(state.get("last_alert_id") or "")


def slash_command(text: str) -> tuple[str, str]:
    if not text.startswith("/"):
        return "", text
    command, _, rest = text.partition(" ")
    command = command[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def ticket_from_command_token(token: str, state: dict[str, Any]) -> str:
    cleaned = canonical_ticket_id(token)
    if not cleaned:
        return ""
    if find_alert(state, cleaned):
        return cleaned
    if re.fullmatch(r"(?:\d|[a-z]{2}\d|[A-Z0-9]{3,12})", cleaned) and any(
        ch.isdigit() for ch in cleaned
    ):
        return cleaned
    return ""


def ticket_from_optional_argument(rest: str, message: dict[str, Any], state: dict[str, Any]) -> str:
    if rest.strip():
        ticket = ticket_from_command_token(rest.split(maxsplit=1)[0], state)
        if ticket:
            return ticket
    return ticket_from_message(message, state)


def reply_target_and_text(command: str, rest: str, message: dict[str, Any], state: dict[str, Any]) -> tuple[str, str]:
    rest = rest.strip()
    if not rest:
        return ticket_from_message(message, state), ""
    first, _, remainder = rest.partition(" ")
    ticket = ticket_from_command_token(first, state)
    if ticket and remainder.strip():
        return ticket, remainder.strip()
    if command == "reply" and ticket:
        return ticket, remainder.strip()
    return ticket_from_message(message, state), rest


def find_alert(state: dict[str, Any], ticket_id: str) -> dict[str, Any] | None:
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        return None
    candidates = [
        str(ticket_id or "").strip(),
        canonical_ticket_id(ticket_id),
        str(ticket_id or "").strip().upper(),
        str(ticket_id or "").strip().lower(),
    ]
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
        alert = alerts.get(candidate)
        if isinstance(alert, dict):
            return alert
    return None


def format_alerts(state: dict[str, Any]) -> str:
    alerts = state.get("alerts") if isinstance(state.get("alerts"), dict) else {}
    open_alerts = [
        alert
        for alert in alerts.values()
        if isinstance(alert, dict) and str(alert.get("status") or "") == "open"
    ]
    open_alerts.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    if not open_alerts:
        return "No open alerts."
    lines = ["Open alerts:"]
    for alert in open_alerts[:10]:
        target = lowercase_tmux_session_label(
            alert.get("display_target") or alert.get("send_target") or "unknown"
        )
        window = alert.get("window_name") or "(unnamed)"
        question = " ".join(str(alert.get("question") or "").split())[:120]
        lines.append(f"{alert['id']} {alert.get('agent')} {target} window={window} - {question}")
    return "\n".join(lines)


def format_status(alert: dict[str, Any]) -> str:
    target = str(alert.get("send_target") or "")
    exists = tmux_target_exists(target)
    if not exists:
        return f"tmux target missing: {lowercase_tmux_session_label(alert.get('display_target') or target)}"
    return tmux_last_message(target) or "(no message)"


def format_send_result(prefix: str, result: TmuxSendResult, preview: str) -> str:
    if result.processed:
        return f"{prefix}: {preview[:200]}"
    state = []
    if result.registered:
        state.append("registered")
    if result.pending:
        state.append("pending")
    if result.working:
        state.append("working")
    if not state:
        state.append("not registered")
    return (
        f"{prefix}, but processing was not confirmed after {int(result.elapsed_seconds)}s "
        f"({', '.join(state)}, attempts={result.attempts}): {preview[:200]}"
    )


def send_reply_to_alert(
    state: dict[str, Any],
    ticket_id: str,
    reply: str,
    *,
    warning_callback: Callable[[str], None] | None = None,
) -> str:
    alert = find_alert(state, ticket_id)
    if not alert:
        return f"Unknown alert id: {ticket_id}"
    target = str(alert.get("send_target") or "")
    if not tmux_target_exists(target):
        return f"tmux target not found for {ticket_id}: {target}"
    clean = normalize_outbound_text(reply)
    if not clean:
        return "Nothing to send."
    target_label = lowercase_tmux_session_label(alert.get("display_target") or target)
    result = tmux_send_text(target, clean, warning_callback=warning_callback, target_label=target_label)
    now = int(time.time())
    alert["status"] = "answered" if result.processed else "answer_unconfirmed"
    alert["answered_at"] = now
    alert["updated_at"] = now
    alert["answer_preview"] = clean[:500]
    alert["answer_confirmed"] = result.processed
    alert["answer_registered"] = result.registered
    alert["answer_attempts"] = result.attempts
    state["last_alert_id"] = ticket_id
    state["updated_at"] = now
    return format_send_result(f"Forwarded to {target_label} [{ticket_id}]", result, clean)


def process_message(update: dict[str, Any], store: StateStore) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    if not isinstance(message, dict):
        return
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "").strip()
    text = message_text(message)
    if not text:
        return
    message_thread_id = telegram_message_thread_id(message)

    response_holder: dict[str, str] = {}

    def send_warning(text: str) -> None:
        telegram_send_messages(chat_id, text, message_thread_id=message_thread_id)

    def mutate(state: dict[str, Any]) -> None:
        if not update_chat_trust(message, state):
            return
        command, rest = slash_command(text)
        if command in {"start", "help"}:
            response_holder["text"] = (
                "Commands:\n"
                "/alerts - list open routed alerts\n"
                "/status [id] - show the tmux pane tail; defaults to latest alert\n"
                "/reply [id] <text> - send text; defaults to latest alert\n"
                "/send <tmux-target> <text> - send to an explicit tmux target\n"
                "\nReplying directly to an alert message also works."
            )
            return
        if command == "alerts":
            response_holder["text"] = format_alerts(state)
            return
        if command == "status":
            ticket = ticket_from_optional_argument(rest, message, state)
            alert = find_alert(state, ticket)
            response_holder["text"] = format_status(alert) if alert else f"Unknown alert id: {ticket}"
            return
        if command == "reply":
            ticket, outbound = reply_target_and_text(command, rest, message, state)
            if not outbound:
                response_holder["text"] = "Usage: /reply [id] <text>"
                return
            response_holder["text"] = send_reply_to_alert(
                state,
                ticket,
                outbound,
                warning_callback=send_warning,
            )
            return
        if command == "send":
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                response_holder["text"] = "Usage: /send <tmux-target> <text>"
                return
            target, outbound = parts[0], parts[1]
            if not tmux_target_exists(target):
                response_holder["text"] = f"tmux target not found: {target}"
                return
            clean = normalize_outbound_text(outbound)
            result = tmux_send_text(
                target,
                clean,
                warning_callback=send_warning,
                target_label=lowercase_tmux_session_label(target),
            )
            response_holder["text"] = format_send_result(
                f"Forwarded to {lowercase_tmux_session_label(target)}",
                result,
                clean,
            )
            return
        if command:
            response_holder["text"] = "Unknown command. Send /help."
            return

        ticket = ticket_from_message(message, state)
        if not ticket:
            response_holder["text"] = "No alert selected. Reply to an alert or use /reply <text>."
            return
        response_holder["text"] = send_reply_to_alert(state, ticket, text, warning_callback=send_warning)

    store.update(mutate)
    if response_holder.get("text"):
        telegram_send_messages(chat_id, response_holder["text"], message_thread_id=message_thread_id)


def initial_offset(state: dict[str, Any]) -> int | None:
    raw = state.get("last_update_id")
    try:
        return int(raw) + 1 if raw is not None else None
    except Exception:
        return None


def bootstrap_offset(offset: int | None) -> int | None:
    if offset is not None:
        return offset
    try:
        updates = telegram_get_updates(offset=None, timeout=0, limit=100)
    except Exception:
        return None
    if not updates:
        return None
    return max(int(update.get("update_id") or 0) for update in updates) + 1


def daemon() -> int:
    if not telegram_enabled():
        print("[agent_telegram_bridge daemon] Telegram bridge is disabled by configuration", file=sys.stderr)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    if not bot_token():
        print("[agent_telegram_bridge daemon] Telegram bot token is not configured", file=sys.stderr)
        return 1
    if not notify_chat_id() and not allowed_username():
        print("[agent_telegram_bridge daemon] Telegram chat id or allowed username is required", file=sys.stderr)
        return 1

    store = StateStore(state_file())
    state = store.read()
    offset = bootstrap_offset(initial_offset(state))

    def init_state(payload: dict[str, Any]) -> None:
        payload["last_update_id"] = offset - 1 if offset is not None else payload.get("last_update_id")
        payload["updated_at"] = int(time.time())

    store.update(init_state)

    timeout = int(env_value("TELEGRAM_BRIDGE_POLL_TIMEOUT", default=str(DEFAULT_POLL_TIMEOUT_SECONDS)))
    limit = int(env_value("TELEGRAM_BRIDGE_POLL_LIMIT", default=str(DEFAULT_POLL_LIMIT)))
    backoff = 1.0
    while True:
        try:
            updates = telegram_get_updates(offset=offset, timeout=timeout, limit=limit)
            if updates:
                for update in updates:
                    update_id = int(update.get("update_id") or 0)
                    offset = update_id + 1

                    def mark_seen(payload: dict[str, Any]) -> None:
                        payload["last_update_id"] = update_id
                        payload["updated_at"] = int(time.time())

                    store.update(mark_seen)
                    process_message(update, store)
            backoff = 1.0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"[agent_telegram_bridge daemon] {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def slack_poll_daemon() -> int:
    if not slack_bot_token():
        print("[agent_telegram_bridge slack-daemon] Slack bot token is not configured", file=sys.stderr)
        return 1
    interval = slack_poll_interval_seconds()
    store = StateStore(state_file())
    print(
        f"[agent_telegram_bridge slack-daemon] polling Slack threads every {interval:g}s",
        file=sys.stderr,
    )
    backoff = interval
    while True:
        try:
            slack_poll_once(store)
            backoff = interval
            time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            retry_after = slack_retry_after_seconds(exc)
            if retry_after is not None:
                print(
                    f"[agent_telegram_bridge slack-daemon] Slack rate limited; sleeping {retry_after:g}s",
                    file=sys.stderr,
                )
                time.sleep(retry_after)
                backoff = interval
                continue
            print(f"[agent_telegram_bridge slack-daemon] poll error: {exc}", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def slack_events_daemon() -> int:
    if not slack_bot_token():
        print("[agent_telegram_bridge slack-daemon] Slack bot token is not configured", file=sys.stderr)
        return 1
    if not slack_signing_secret():
        print("[agent_telegram_bridge slack-daemon] Slack signing secret is not configured", file=sys.stderr)
        return 1
    host = slack_http_host()
    port = slack_http_port()
    store = StateStore(state_file())
    handler = make_slack_http_handler(store)
    server = ThreadingHTTPServer((host, port), handler)
    print(
        f"[agent_telegram_bridge slack-daemon] listening on http://{host}:{port}{slack_http_path()}",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def slack_daemon() -> int:
    mode = slack_bridge_mode()
    if mode in {"events", "event", "http", "webhook"}:
        return slack_events_daemon()
    if mode in {"poll", "polling", "webapi", "web-api"}:
        return slack_poll_daemon()
    print(f"[agent_telegram_bridge slack-daemon] Unknown SLACK_BRIDGE_MODE={mode!r}", file=sys.stderr)
    return 1


def herdr_event() -> int:
    """Handle Herdr's pane.agent_status_changed plugin event."""
    try:
        event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON", "{}"))
        context = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    except (TypeError, ValueError) as exc:
        print(f"[agent_telegram_bridge herdr-event] invalid Herdr JSON: {exc}", file=sys.stderr)
        return 0
    if not isinstance(event, dict):
        event = {}
    if not isinstance(context, dict):
        context = {}
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    pane_id = str(
        data.get("pane_id")
        or context.get("focused_pane_id")
        or context.get("pane_id")
        or os.environ.get("HERDR_PANE_ID", "")
    ).strip()
    status = str(data.get("agent_status") or data.get("status") or "").strip().lower()
    allowed = {
        value.strip().lower()
        for value in os.environ.get("HERDR_BRIDGE_STATUSES", "blocked").split(",")
        if value.strip()
    }
    if not pane_id or status not in allowed:
        return 0
    os.environ["HERDR_PANE_ID"] = pane_id
    payload = {
        "herdr_event": True,
        "pane_id": pane_id,
        "agent_status": status,
        "message": data.get("message") or data.get("reason") or "",
        "agent": data.get("display_agent") or data.get("agent") or context.get("agent") or "herdr-agent",
        "cwd": data.get("cwd") or context.get("cwd") or "",
    }
    return notify([json.dumps(payload)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Telegram and Slack bridge for Herdr, Codex, and Claude agent sessions"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("notify", help="run as Codex/Claude notification hook")
    sub.add_parser("daemon", help="poll Telegram replies and forward them to tmux")
    sub.add_parser("slack-daemon", help="serve Slack Events API replies and forward them to tmux")
    sub.add_parser("herdr-event", help="handle a Herdr pane status event")
    sub.add_parser("state", help="print current bridge state")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lstrip().startswith("{"):
        return notify(argv)
    parser = build_parser()
    args, remaining = parser.parse_known_args(argv)
    if args.command == "notify":
        return notify(remaining)
    if args.command == "daemon":
        return daemon()
    if args.command == "slack-daemon":
        return slack_daemon()
    if args.command == "herdr-event":
        return herdr_event()
    if args.command == "state":
        print(json.dumps(StateStore(state_file()).read(), indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 2
