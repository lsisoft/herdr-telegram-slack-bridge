from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_telegram_bridge import cli


class BridgeTests(unittest.TestCase):
    def test_herdr_target_uses_herdr_cli_for_input(self) -> None:
        target = "w1:p2"
        with mock.patch.object(
            cli,
            "herdr_command",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""),
        ) as command:
            result = cli.tmux_send_text(target, "ship it")
        self.assertTrue(result.processed)
        command.assert_called_once_with(["pane", "run", target, "ship it"])

    def test_herdr_event_ignores_non_blocked_status_by_default(self) -> None:
        with mock.patch.dict(
            cli.os.environ,
            {
                "HERDR_PLUGIN_EVENT_JSON": '{"data":{"pane_id":"w1:p2","agent_status":"working"}}',
                "HERDR_PLUGIN_CONTEXT_JSON": "{}",
            },
            clear=False,
        ), mock.patch.object(cli, "notify") as notify:
            self.assertEqual(cli.herdr_event(), 0)
        notify.assert_not_called()

    def test_herdr_event_converts_blocked_pane_to_normal_notify_payload(self) -> None:
        with mock.patch.dict(
            cli.os.environ,
            {
                "HERDR_PLUGIN_EVENT_JSON": '{"data":{"pane_id":"w1:p2","agent_status":"blocked","message":"approve"}}',
                "HERDR_PLUGIN_CONTEXT_JSON": "{}",
                "HERDR_BRIDGE_STATUSES": "blocked",
            },
            clear=False,
        ), mock.patch.object(cli, "notify", return_value=0) as notify:
            self.assertEqual(cli.herdr_event(), 0)
        payload = cli.json.loads(notify.call_args.args[0][0])
        self.assertEqual(payload["pane_id"], "w1:p2")
        self.assertEqual(payload["agent_status"], "blocked")
    def confirmed_send_result(self) -> cli.TmuxSendResult:
        return cli.TmuxSendResult(
            processed=True,
            registered=True,
            pending=False,
            working=False,
            attempts=1,
            elapsed_seconds=0.0,
        )

    def test_extract_one_digit_ticket_from_bracketed_alert(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("codex needs input [7]"), "7")

    def test_extract_lowercase_ticket_from_bracketed_alert(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("claude needs input [ab1]"), "ab1")

    def test_extract_short_ticket_from_bracketed_alert(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("Codex needs input [A1B]"), "A1B")

    def test_extract_old_long_ticket_from_bracketed_alert(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("Codex needs input [ABC123]"), "ABC123")

    def test_extract_old_numeric_ticket_from_bracketed_alert(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("Codex needs input [123]"), "123")

    def test_extract_ticket_from_command_text(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("ticket: z9y please"), "Z9Y")

    def test_extract_lowercase_ticket_from_command_text(self) -> None:
        self.assertEqual(cli.extract_ticket_from_text("ticket: ab1 please"), "ab1")

    def test_outbound_text_collapses_multiline_reply(self) -> None:
        self.assertEqual(cli.normalize_outbound_text(" first line\n\nsecond line "), "first line second line")

    def test_telegram_markdown_v2_escape_escapes_reserved_characters(self) -> None:
        self.assertEqual(
            cli.telegram_markdown_v2_escape(r"a_b [x](y) #1! path\name"),
            r"a\_b \[x\]\(y\) \#1\! path\\name",
        )

    def test_telegram_send_message_uses_markdown_v2_payload(self) -> None:
        with mock.patch.object(cli, "telegram_request", return_value={"message_id": 7}) as request:
            result = cli.telegram_send_message("99", "codex needs input [1] path=/tmp/a_b!")
        self.assertEqual(result, {"message_id": 7})
        request.assert_called_once()
        method, body = request.call_args.args
        self.assertEqual(method, "sendMessage")
        self.assertEqual(body["parse_mode"], "MarkdownV2")
        self.assertEqual(body["text"], r"codex needs input \[1\] path\=/tmp/a\_b\!")

    def test_telegram_send_message_includes_forum_thread_id(self) -> None:
        with mock.patch.object(cli, "telegram_request", return_value={"message_id": 7}) as request:
            cli.telegram_send_message("99", "hello", message_thread_id=123)
        body = request.call_args.args[1]
        self.assertEqual(body["message_thread_id"], 123)

    def test_telegram_topic_id_is_created_and_reused(self) -> None:
        alert = {
            "id": "1",
            "agent": "codex",
            "display_target": "codex:1.0",
            "session_name": "codex",
            "window_name": "fx",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            with mock.patch.object(cli, "telegram_forum_topics_enabled", return_value=True), mock.patch.object(
                cli, "telegram_create_forum_topic", return_value=321
            ) as create_topic:
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", alert), 321)
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", alert), 321)
        create_topic.assert_called_once_with("99", "tmux codex:1:fx")

    def test_telegram_enabled_flag_can_disable_cutover_env_values(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_ENABLED": "0",
                "LIVE_OPS_TELEGRAM_BOT_TOKEN": "token-from-shared-env",
                "LIVE_OPS_TELEGRAM_CHAT_ID": "123",
            },
        ):
            self.assertFalse(cli.telegram_enabled())

    def test_notify_skips_telegram_when_disabled(self) -> None:
        payload = {
            "type": "agent-turn-complete",
            "thread-id": "thread-1",
            "last-assistant-message": "Need input",
            "cwd": "/work",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_ENABLED": "0",
                "LIVE_OPS_TELEGRAM_BOT_TOKEN": "token-from-shared-env",
                "LIVE_OPS_TELEGRAM_CHAT_ID": "123",
                "TELEGRAM_BRIDGE_STATE_FILE": str(Path(tmp) / "state.json"),
            },
        ), mock.patch.object(cli, "telegram_send_messages") as telegram_send, mock.patch.object(
            cli, "slack_notify_alert"
        ) as slack_notify:
            self.assertEqual(cli.notify([cli.json.dumps(payload)]), 0)
            state = cli.StateStore(Path(tmp) / "state.json").read()

        telegram_send.assert_not_called()
        slack_notify.assert_called_once()
        alert = next(iter(state["alerts"].values()))
        self.assertNotIn("telegram_message_id", alert)
        self.assertEqual(alert["question"], "Need input")

    def test_telegram_topic_id_is_reused_for_same_tmux_window_label(self) -> None:
        first_alert = {
            "id": "1",
            "agent": "codex",
            "display_target": "codex:1.0",
            "session_name": "codex",
            "window_name": "fx",
        }
        second_alert = {
            "id": "2",
            "agent": "codex",
            "display_target": "codex:1.1",
            "session_name": "codex",
            "window_name": "fx",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            with mock.patch.object(cli, "telegram_forum_topics_enabled", return_value=True), mock.patch.object(
                cli, "telegram_create_forum_topic", return_value=321
            ) as create_topic:
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", first_alert), 321)
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", second_alert), 321)
            topics = store.read()["telegram_topics"]
        create_topic.assert_called_once_with("99", "tmux codex:1:fx")
        self.assertIn("codex:1:fx", topics)

    def test_telegram_topics_are_separate_for_different_tmux_windows(self) -> None:
        first_alert = {
            "id": "1",
            "agent": "codex",
            "display_target": "codex:0.0",
            "session_name": "codex",
            "window_name": "fx",
        }
        second_alert = {
            "id": "2",
            "agent": "codex",
            "display_target": "codex:1.0",
            "session_name": "codex",
            "window_name": "ibkr",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            with mock.patch.object(cli, "telegram_forum_topics_enabled", return_value=True), mock.patch.object(
                cli, "telegram_create_forum_topic", side_effect=[321, 654]
            ) as create_topic:
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", first_alert), 321)
                self.assertEqual(cli.ensure_telegram_topic_id(store, "99", second_alert), 654)
            topics = store.read()["telegram_topics"]
        self.assertEqual(create_topic.call_args_list[0].args, ("99", "tmux codex:0:fx"))
        self.assertEqual(create_topic.call_args_list[1].args, ("99", "tmux codex:1:ibkr"))
        self.assertIn("codex:0:fx", topics)
        self.assertIn("codex:1:ibkr", topics)

    def test_slack_send_message_posts_threaded_escaped_text(self) -> None:
        with mock.patch.object(cli, "slack_request", return_value={"ok": True, "ts": "1.2"}) as request:
            result = cli.slack_send_message("C1", "a < b & c > d", thread_ts="9.9")
        self.assertEqual(result["ts"], "1.2")
        method, body = request.call_args.args
        self.assertEqual(method, "chat.postMessage")
        self.assertEqual(body["channel"], "C1")
        self.assertEqual(body["thread_ts"], "9.9")
        self.assertEqual(body["text"], "a &lt; b &amp; c &gt; d")

    def test_slack_command_accepts_plain_thread_commands(self) -> None:
        self.assertEqual(cli.slack_command("reply A1B ship it"), ("reply", "A1B ship it"))
        self.assertEqual(cli.slack_command("status"), ("status", ""))
        self.assertEqual(cli.slack_command("ship it"), ("", "ship it"))

    def test_slack_notify_alert_posts_top_level_message_for_alert_thread(self) -> None:
        alert = {
            "id": "fx1",
            "agent": "codex",
            "hostname": "test-host",
            "display_target": "codex:1.0",
            "session_name": "codex",
            "window_index": "1",
            "send_target": "%9",
            "window_name": "fx",
            "pane_id": "%9",
            "pane_current_command": "node",
            "cwd": "/work",
            "question": "Continue?",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            with mock.patch.object(cli, "slack_enabled", return_value=True), mock.patch.object(
                cli, "slack_channel_id", return_value="C1"
            ), mock.patch.object(
                cli, "slack_request", return_value={"ok": True, "ts": "10.0"}
            ) as request:
                cli.slack_notify_alert(store, alert)
        self.assertEqual(alert["slack_thread_ts"], "10.0")
        self.assertEqual(alert["slack_message_ts"], "10.0")
        request.assert_called_once()
        method, body = request.call_args.args
        self.assertEqual(method, "chat.postMessage")
        self.assertNotIn("thread_ts", body)
        self.assertEqual(body["text"].splitlines()[0], "test-host:codex:1:fx [fx1]")
        self.assertNotIn("/reply", body["text"])

    def test_slack_notify_alert_creates_new_channel_message_per_alert(self) -> None:
        base_alert = {
            "agent": "codex",
            "hostname": "test-host",
            "session_name": "codex",
            "window_index": "1",
            "send_target": "%9",
            "pane_current_command": "node",
            "cwd": "/work",
            "question": "Continue?",
        }
        first_alert = {
            **base_alert,
            "id": "fx1",
            "display_target": "codex:1.0",
            "window_name": "fx",
            "pane_id": "%9",
        }
        second_alert = {
            **base_alert,
            "id": "fx2",
            "display_target": "codex:1.1",
            "window_name": "fx",
            "pane_id": "%10",
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            with mock.patch.object(cli, "slack_enabled", return_value=True), mock.patch.object(
                cli, "slack_channel_id", return_value="C1"
            ), mock.patch.object(
                cli,
                "slack_request",
                side_effect=[
                    {"ok": True, "ts": "10.0"},
                    {"ok": True, "ts": "20.0"},
                ],
            ) as request:
                cli.slack_notify_alert(store, first_alert)
                cli.slack_notify_alert(store, second_alert)
        self.assertEqual(first_alert["slack_thread_ts"], "10.0")
        self.assertEqual(second_alert["slack_thread_ts"], "20.0")
        self.assertEqual(len(request.call_args_list), 2)
        self.assertNotIn("thread_ts", request.call_args_list[0].args[1])
        self.assertNotIn("thread_ts", request.call_args_list[1].args[1])

    def test_slack_unknown_thread_does_not_fall_back_to_global_latest_alert(self) -> None:
        state = {
            "last_alert_id": "A1B",
            "alerts": {
                "A1B": {
                    "id": "A1B",
                    "status": "open",
                    "slack_channel_id": "C1",
                    "slack_thread_ts": "10.0",
                }
            },
        }
        self.assertEqual(cli.ticket_from_slack_text_or_thread("", "C1", "99.0", state), "")

    def test_slack_thread_reply_routes_to_answered_alert_in_same_thread(self) -> None:
        state = {
            "last_alert_id": "A1B",
            "alerts": {
                "A1B": {
                    "id": "A1B",
                    "status": "answered",
                    "created_at": int(time.time()),
                    "slack_channel_id": "C1",
                    "slack_thread_ts": "10.0",
                }
            },
        }
        self.assertEqual(cli.ticket_from_slack_text_or_thread("", "C1", "10.0", state), "A1B")

    def test_split_telegram_text_adds_ticketed_continuation_prefix(self) -> None:
        chunks = cli.split_telegram_text(
            "question:\n" + ("alpha beta gamma " * 20),
            continuation_prefix="codex needs input [1] continued\n\n",
            max_chars=80,
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))
        self.assertTrue(chunks[1].startswith("codex needs input [1] continued\n\n"))

    def test_telegram_send_messages_splits_and_marks_last_message_for_reply(self) -> None:
        reply_markup = {"force_reply": True}
        with mock.patch.object(cli, "MAX_TELEGRAM_TEXT", 80), mock.patch.object(
            cli, "telegram_send_message", return_value={"message_id": 1}
        ) as send:
            results = cli.telegram_send_messages(
                "99",
                "question:\n" + ("alpha beta gamma " * 20),
                reply_markup=reply_markup,
                continuation_prefix="codex needs input [1] continued\n\n",
            )
        self.assertEqual(results, [{"message_id": 1}] * send.call_count)
        self.assertGreater(send.call_count, 1)
        self.assertIsNone(send.call_args_list[0].kwargs["reply_markup"])
        self.assertEqual(send.call_args_list[-1].kwargs["reply_markup"], reply_markup)
        self.assertTrue(send.call_args_list[1].args[1].startswith(r"codex needs input \[1\] continued"))
        self.assertTrue(all(call.kwargs["markdown_escaped"] for call in send.call_args_list))

    def test_create_alert_uses_pane_metadata(self) -> None:
        pane = cli.PaneInfo(
            session_name="Codex",
            window_index="1",
            window_name="node",
            pane_index="0",
            pane_id="%9",
            pane_current_command="node",
            pane_current_path="/work",
            pane_title="new_algo",
        )
        alert = cli.create_alert(
            "codex",
            {"last-assistant-message": "What should I do?", "cwd": "/repo", "thread-id": "t"},
            pane,
            ticket_id="A1B",
        )
        self.assertEqual(alert["display_target"], "codex:1.0")
        self.assertEqual(alert["session_name"], "codex")
        self.assertEqual(alert["send_target"], "%9")
        self.assertEqual(alert["window_name"], "node")
        self.assertEqual(alert["question"], "What should I do?")

    def test_plain_reply_uses_ticket_from_replied_alert(self) -> None:
        state = {"last_alert_id": "", "alerts": {"1": {"id": "1"}}}
        message = {
            "text": "yes proceed",
            "reply_to_message": {"text": "codex needs input [1]\nQuestion..."},
        }
        self.assertEqual(cli.ticket_from_message(message, state), "1")

    def test_plain_reply_prefers_replied_alert_over_numeric_answer_text(self) -> None:
        state = {"last_alert_id": "", "alerts": {"1": {"id": "1"}, "2": {"id": "2"}}}
        message = {
            "text": "1",
            "reply_to_message": {"text": "codex needs input [2]\nQuestion..."},
        }
        self.assertEqual(cli.ticket_from_message(message, state), "2")

    def test_plain_reply_uses_replied_message_id_for_sliced_alert_chunk(self) -> None:
        state = {
            "last_alert_id": "1",
            "alerts": {
                "1": {
                    "id": "1",
                    "telegram_chat_id": "99",
                    "telegram_message_ids": [10, 11, 12],
                }
            },
        }
        message = {
            "chat": {"id": 99},
            "text": "yes proceed",
            "reply_to_message": {"message_id": 12, "text": "tail of a long block without a ticket"},
        }
        self.assertEqual(cli.ticket_from_message(message, state), "1")

    def test_replied_message_id_lookup_obeys_chat_id_when_present(self) -> None:
        state = {
            "last_alert_id": "2",
            "alerts": {
                "1": {"id": "1", "telegram_chat_id": "99", "telegram_message_ids": [12]},
                "2": {"id": "2", "telegram_chat_id": "100", "telegram_message_ids": [12]},
            },
        }
        message = {
            "chat": {"id": 100},
            "text": "yes proceed",
            "reply_to_message": {"message_id": 12, "text": "tail of a long block without a ticket"},
        }
        self.assertEqual(cli.ticket_from_message(message, state), "2")

    def test_reply_without_id_uses_latest_alert(self) -> None:
        state = {"last_alert_id": "A1B", "alerts": {"A1B": {"id": "A1B"}}}
        message = {"text": "/reply yes proceed"}
        ticket, outbound = cli.reply_target_and_text("reply", "yes proceed", message, state)
        self.assertEqual(ticket, "A1B")
        self.assertEqual(outbound, "yes proceed")

    def test_reply_with_id_uses_that_alert(self) -> None:
        state = {
            "last_alert_id": "A1B",
            "alerts": {"A1B": {"id": "A1B"}, "Z9Y": {"id": "Z9Y"}},
        }
        message = {"text": "/reply Z9Y no, wait"}
        ticket, outbound = cli.reply_target_and_text("reply", "Z9Y no, wait", message, state)
        self.assertEqual(ticket, "Z9Y")
        self.assertEqual(outbound, "no, wait")

    def test_status_without_id_uses_latest_alert(self) -> None:
        state = {"last_alert_id": "A1B", "alerts": {"A1B": {"id": "A1B"}}}
        self.assertEqual(cli.ticket_from_optional_argument("", {"text": "/status"}, state), "A1B")

    def test_slash_command_parses_long_forms(self) -> None:
        self.assertEqual(cli.slash_command("/send codex:0 hello"), ("send", "codex:0 hello"))
        self.assertEqual(cli.slash_command("/status"), ("status", ""))

    def test_generated_ticket_uses_two_window_letters_and_one_digit(self) -> None:
        with mock.patch.object(cli.random, "choice", return_value="7"):
            self.assertEqual(cli.make_ticket_id(window_name="crypto"), "cr7")

    def test_generated_ticket_pads_single_letter_window_name(self) -> None:
        with mock.patch.object(cli.random, "choice", return_value="3"):
            self.assertEqual(cli.make_ticket_id(window_name="x"), "xx3")

    def test_generated_ticket_avoids_recent_ids_for_same_window_prefix(self) -> None:
        now = int(time.time())
        alerts = {
            "cr7": {"id": "cr7", "created_at": now - 60},
            "cr8": {"id": "cr8", "created_at": now - 2 * 24 * 3600},
        }
        with mock.patch.object(cli.random, "choice", return_value="8"):
            self.assertEqual(cli.make_ticket_id(window_name="crypto", existing_alerts=alerts, now=now), "cr8")

    def test_send_reply_marks_alert_answered(self) -> None:
        state = {
            "alerts": {
                "A1B": {
                    "id": "A1B",
                    "status": "open",
                    "send_target": "%9",
                    "display_target": "Codex:1.0",
                }
            }
        }
        with mock.patch.object(cli, "tmux_target_exists", return_value=True), mock.patch.object(
            cli, "tmux_send_text", return_value=self.confirmed_send_result()
        ) as send_text:
            response = cli.send_reply_to_alert(state, "A1B", "ship it")
        self.assertIn("Forwarded to codex:1.0", response)
        self.assertEqual(state["alerts"]["A1B"]["status"], "answered")
        self.assertTrue(state["alerts"]["A1B"]["answer_confirmed"])
        send_text.assert_called_once_with(
            "%9",
            "ship it",
            warning_callback=None,
            target_label="codex:1.0",
        )

    def test_slack_thread_reply_routes_to_latest_open_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            store.write(
                {
                    "last_alert_id": "A1B",
                    "alerts": {
                        "A1B": {
                            "id": "A1B",
                            "status": "open",
                            "created_at": int(time.time()),
                            "send_target": "%9",
                            "display_target": "codex:1.0",
                            "slack_channel_id": "C1",
                            "slack_thread_ts": "10.0",
                        }
                    },
                }
            )
            with mock.patch.object(cli, "tmux_target_exists", return_value=True), mock.patch.object(
                cli, "tmux_send_text", return_value=self.confirmed_send_result()
            ), mock.patch.object(cli, "slack_send_messages") as slack_send:
                cli.process_slack_message_event(
                    {
                        "type": "message",
                        "channel": "C1",
                        "thread_ts": "10.0",
                        "ts": "10.2",
                        "text": "ship it",
                        "user": "U1",
                    },
                    store,
                )
            updated = store.read()["alerts"]["A1B"]
        self.assertEqual(updated["status"], "answered")
        self.assertEqual(updated["answer_preview"], "ship it")
        slack_send.assert_called_once()
        self.assertEqual(slack_send.call_args.kwargs["thread_ts"], "10.0")

    def test_slack_poll_once_routes_thread_reply_without_message_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            store.write(
                {
                    "last_alert_id": "A1B",
                    "alerts": {
                        "A1B": {
                            "id": "A1B",
                            "status": "open",
                            "created_at": int(time.time()),
                            "send_target": "%9",
                            "display_target": "codex:1.0",
                            "slack_channel_id": "C1",
                            "slack_thread_ts": "10.000000",
                            "slack_message_ts": "10.100000",
                        }
                    },
                }
            )
            with mock.patch.object(
                cli,
                "slack_request",
                return_value={
                    "ok": True,
                    "messages": [
                        {"type": "message", "ts": "10.100000", "text": "alert", "bot_id": "B1"},
                        {"type": "message", "ts": "10.200000", "text": "ship it", "user": "U1"},
                    ],
                },
            ) as slack_request, mock.patch.object(
                cli, "tmux_target_exists", return_value=True
            ), mock.patch.object(
                cli, "tmux_send_text", return_value=self.confirmed_send_result()
            ), mock.patch.object(
                cli, "slack_send_messages"
            ) as slack_send:
                processed = cli.slack_poll_once(store)
            updated = store.read()
        self.assertEqual(processed, 1)
        self.assertEqual(updated["alerts"]["A1B"]["status"], "answered")
        self.assertEqual(updated["alerts"]["A1B"]["answer_preview"], "ship it")
        self.assertEqual(updated["slack_poll_seen_ts"]["C1:10.000000"], "10.200000")
        method, body = slack_request.call_args.args
        self.assertEqual(method, "conversations.replies")
        self.assertEqual(body["oldest"], "10.100000")
        slack_send.assert_called_once()
        self.assertEqual(slack_send.call_args.kwargs["thread_ts"], "10.000000")

    def test_slack_poll_once_prunes_missing_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")
            store.write(
                {
                    "last_alert_id": "A1B",
                    "slack_threads": {
                        "codex:1:cry": {
                            "channel_id": "C1",
                            "thread_ts": "10.000000",
                        },
                        "codex:2:fx": {
                            "channel_id": "C1",
                            "thread_ts": "20.000000",
                        },
                    },
                    "slack_poll_seen_ts": {
                        "C1:10.000000": "10.100000",
                        "C1:20.000000": "20.100000",
                    },
                    "alerts": {
                        "A1B": {
                            "id": "A1B",
                            "status": "open",
                            "created_at": int(time.time()),
                            "slack_channel_id": "C1",
                            "slack_thread_ts": "10.000000",
                            "slack_message_ts": "10.100000",
                        }
                    },
                }
            )
            with mock.patch.object(
                cli,
                "slack_request",
                side_effect=[RuntimeError("Slack API error: {'ok': False, 'error': 'thread_not_found'}")],
            ):
                processed = cli.slack_poll_once(store)
            updated = store.read()
        self.assertEqual(processed, 0)
        self.assertNotIn("codex:1:cry", updated["slack_threads"])
        self.assertIn("codex:2:fx", updated["slack_threads"])
        self.assertNotIn("C1:10.000000", updated["slack_poll_seen_ts"])
        self.assertIn("C1:20.000000", updated["slack_poll_seen_ts"])
        self.assertNotIn("slack_thread_ts", updated["alerts"]["A1B"])
        self.assertIn("slack_thread_pruned_at", updated["alerts"]["A1B"])

    def test_slack_thread_targets_only_recent_alert_threads(self) -> None:
        now = int(time.time())
        state = {
            "slack_threads": {
                "old-window-cache": {
                    "channel_id": "C1",
                    "thread_ts": "1.0",
                }
            },
            "alerts": {
                "fx1": {
                    "created_at": now - 60,
                    "slack_channel_id": "C1",
                    "slack_thread_ts": "10.0",
                },
                "fx2": {
                    "created_at": now - 2 * 24 * 3600,
                    "slack_channel_id": "C1",
                    "slack_thread_ts": "20.0",
                },
            },
        }
        self.assertEqual(cli.slack_thread_targets(state), [("C1", "10.0")])

    def test_tmux_send_text_submits_with_enter_and_carriage_returns(self) -> None:
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_capture", return_value="baseline"), mock.patch.object(
            cli,
            "tmux_input_status",
            return_value=cli.TmuxInputStatus(
                registered=True,
                processed=True,
                pending=False,
                working=False,
                changed=True,
            ),
        ) as input_status, mock.patch.dict(
            os.environ,
            {"TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0"},
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it")
        self.assertTrue(result.processed)
        send_literal.assert_called_once_with("%9", "ship it")
        submit_input.assert_called_once_with("%9")
        input_status.assert_called_once()

    def test_tmux_submit_input_uses_multiple_submit_methods(self) -> None:
        with mock.patch.object(cli, "tmux_send_key") as send_key, mock.patch.object(
            cli, "tmux_submit_pasted_newline"
        ) as paste_newline:
            cli.tmux_submit_input("%9")
        self.assertEqual(
            send_key.call_args_list,
            [
                mock.call("%9", "Enter"),
                mock.call("%9", "C-m"),
                mock.call("%9", "C-j"),
            ],
        )
        paste_newline.assert_called_once_with("%9")

    def test_tmux_send_text_clears_retypes_and_resubmits_pending_input(self) -> None:
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            side_effect=[
                cli.TmuxInputStatus(
                    registered=True,
                    processed=False,
                    pending=True,
                    working=False,
                    changed=True,
                ),
                cli.TmuxInputStatus(
                    registered=True,
                    processed=True,
                    pending=False,
                    working=False,
                    changed=True,
                ),
            ],
        ), mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL": "0",
            },
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it")
        self.assertTrue(result.processed)
        self.assertEqual(send_literal.call_args_list, [mock.call("%9", "ship it"), mock.call("%9", "ship it")])
        self.assertEqual(submit_input.call_count, 2)
        clear_input.assert_called_once_with("%9")

    def test_tmux_send_text_clears_retypes_and_resubmits_when_pending_persists(self) -> None:
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            side_effect=[
                cli.TmuxInputStatus(
                    registered=True,
                    processed=False,
                    pending=True,
                    working=False,
                    changed=True,
                ),
                cli.TmuxInputStatus(
                    registered=True,
                    processed=False,
                    pending=True,
                    working=False,
                    changed=True,
                ),
                cli.TmuxInputStatus(
                    registered=True,
                    processed=True,
                    pending=False,
                    working=False,
                    changed=True,
                ),
            ],
        ), mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL": "0",
            },
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it")
        self.assertTrue(result.processed)
        self.assertEqual(
            send_literal.call_args_list,
            [mock.call("%9", "ship it"), mock.call("%9", "ship it"), mock.call("%9", "ship it")],
        )
        self.assertEqual(submit_input.call_count, 3)
        self.assertEqual(clear_input.call_args_list, [mock.call("%9"), mock.call("%9")])

    def test_tmux_send_text_clears_pending_input_on_unconfirmed_timeout(self) -> None:
        warning_callback = mock.Mock()
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            return_value=cli.TmuxInputStatus(
                registered=False,
                processed=False,
                pending=False,
                working=False,
                changed=False,
            ),
        ), mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_SECONDS": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_WARNING_SECONDS": "0",
            },
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it", warning_callback=warning_callback)
        self.assertFalse(result.processed)
        send_literal.assert_called_once_with("%9", "ship it")
        submit_input.assert_called_once_with("%9")
        clear_input.assert_called_once_with("%9")
        self.assertIn("clearing, retyping", warning_callback.call_args_list[0].args[0])

    def test_tmux_send_text_accepts_working_status(self) -> None:
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            return_value=cli.TmuxInputStatus(
                registered=True,
                processed=False,
                pending=False,
                working=True,
                changed=True,
            ),
        ), mock.patch.dict(
            os.environ,
            {"TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0"},
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it")
        self.assertTrue(result.processed)
        self.assertTrue(result.working)
        send_literal.assert_called_once_with("%9", "ship it")
        submit_input.assert_called_once_with("%9")
        clear_input.assert_not_called()

    def test_tmux_send_text_retries_if_pending_line_remains_with_working_status(self) -> None:
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            side_effect=[
                cli.TmuxInputStatus(
                    registered=True,
                    processed=False,
                    pending=True,
                    working=True,
                    changed=True,
                ),
                cli.TmuxInputStatus(
                    registered=True,
                    processed=False,
                    pending=False,
                    working=True,
                    changed=True,
                ),
            ],
        ), mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL": "0",
            },
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it")
        self.assertTrue(result.processed)
        self.assertTrue(result.working)
        self.assertEqual(send_literal.call_args_list, [mock.call("%9", "ship it"), mock.call("%9", "ship it")])
        self.assertEqual(submit_input.call_count, 2)
        clear_input.assert_called_once_with("%9")

    def test_tmux_send_text_warns_at_threshold_and_final_timeout(self) -> None:
        warning_callback = mock.Mock()
        with mock.patch.object(cli, "tmux_send_literal"), mock.patch.object(
            cli, "tmux_submit_input"
        ), mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            return_value=cli.TmuxInputStatus(
                registered=False,
                processed=False,
                pending=False,
                working=False,
                changed=False,
            ),
        ), mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_SECONDS": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_MONITOR_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_RETRY_INTERVAL": "0",
                "TELEGRAM_BRIDGE_TMUX_SEND_WARNING_SECONDS": "0",
            },
            clear=False,
        ):
            result = cli.tmux_send_text("%9", "ship it", warning_callback=warning_callback)
        self.assertFalse(result.processed)
        self.assertFalse(result.registered)
        self.assertTrue(result.final_warning_sent)
        self.assertEqual(warning_callback.call_count, 2)
        self.assertIn("Warning:", warning_callback.call_args_list[0].args[0])
        self.assertIn("Final warning:", warning_callback.call_args_list[1].args[0])
        clear_input.assert_called_once_with("%9")

    def test_tmux_send_text_defaults_to_120_second_monitor_and_15_second_warning(self) -> None:
        warning_callback = mock.Mock()
        with mock.patch.object(cli, "tmux_send_literal") as send_literal, mock.patch.object(
            cli, "tmux_submit_input"
        ) as submit_input, mock.patch.object(cli, "tmux_clear_pending_input") as clear_input, mock.patch.object(
            cli, "tmux_capture", return_value="baseline"
        ), mock.patch.object(
            cli,
            "tmux_input_status",
            return_value=cli.TmuxInputStatus(
                registered=False,
                processed=False,
                pending=False,
                working=False,
                changed=False,
            ),
        ), mock.patch.object(
            cli.time, "sleep"
        ), mock.patch.object(
            cli.time, "monotonic", side_effect=[0.0, 14.0, 14.0, 15.0, 15.0, 120.0]
        ), mock.patch.object(
            cli, "env_files", return_value=[]
        ), mock.patch.dict(
            os.environ, {}, clear=True
        ):
            result = cli.tmux_send_text("%9", "ship it", warning_callback=warning_callback)
        self.assertFalse(result.processed)
        self.assertTrue(result.warning_sent)
        self.assertTrue(result.final_warning_sent)
        self.assertEqual(warning_callback.call_count, 2)
        self.assertIn("after 15s", warning_callback.call_args_list[0].args[0])
        self.assertIn("monitor limit=120s", warning_callback.call_args_list[0].args[0])
        self.assertIn("after 120s", warning_callback.call_args_list[1].args[0])
        self.assertGreaterEqual(send_literal.call_count, 2)
        self.assertEqual(submit_input.call_count, send_literal.call_count)
        self.assertEqual(clear_input.call_count, send_literal.call_count)

    def test_slack_poll_interval_caps_large_configured_values(self) -> None:
        with mock.patch.object(cli, "env_files", return_value=[]), mock.patch.dict(
            os.environ, {"SLACK_BRIDGE_POLL_INTERVAL": "60"}, clear=True
        ):
            self.assertEqual(cli.slack_poll_interval_seconds(), 15.0)

    def test_slack_poll_interval_preserves_faster_values(self) -> None:
        with mock.patch.object(cli, "env_files", return_value=[]), mock.patch.dict(
            os.environ, {"SLACK_BRIDGE_POLL_INTERVAL": "3"}, clear=True
        ):
            self.assertEqual(cli.slack_poll_interval_seconds(), 3.0)

    def test_slack_retry_after_seconds_reads_http_429_header(self) -> None:
        exc = cli.urllib.error.HTTPError(
            url="https://slack.com/api/conversations.replies",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "17"},
            fp=None,
        )
        self.assertEqual(cli.slack_retry_after_seconds(exc), 17.0)

    def test_capture_has_pending_input_detects_prompt_line(self) -> None:
        self.assertTrue(cli.capture_has_pending_input("old\n› ship it", "ship it"))
        self.assertFalse(cli.capture_has_pending_input("old\n◦ Working (1s)", "ship it"))

    def test_capture_has_working_status_detects_processing(self) -> None:
        self.assertTrue(cli.capture_has_working_status("› ship it\n◦ Working (1s)"))
        self.assertFalse(cli.capture_has_working_status("› ship it"))

    def test_alert_message_uses_compact_header(self) -> None:
        message = cli.alert_message(
            {
                "id": "no1",
                "agent": "Codex",
                "hostname": "test-host",
                "display_target": "Codex:0.0",
                "session_name": "codex",
                "window_index": "0",
                "window_name": "node",
                "pane_id": "%0",
                "pane_current_command": "node",
                "cwd": "/work",
                "question": "Continue?",
            }
        )
        self.assertTrue(message.startswith("test-host:codex:0:node [no1]\n"))
        self.assertIn("codex needs input\n\nContinue?", message)
        self.assertNotIn("pane:", message)
        self.assertNotIn("cwd:", message)

    def test_alert_message_keeps_full_long_question(self) -> None:
        long_question = "start " + ("body " * 700) + "tail"
        message = cli.alert_message(
            {
                "id": "2",
                "agent": "codex",
                "hostname": "test-host",
                "display_target": "codex:2.0",
                "window_name": "crypto",
                "pane_id": "%348",
                "pane_current_command": "node",
                "cwd": "/work",
                "question": long_question,
            }
        )
        self.assertIn("tail", message)
        self.assertNotIn("[truncated", message)

    def test_lowercase_tmux_session_label_preserves_pane_ids(self) -> None:
        self.assertEqual(cli.lowercase_tmux_session_label("Codex:0.0"), "codex:0.0")
        self.assertEqual(cli.lowercase_tmux_session_label("%9"), "%9")

    def test_tmux_last_message_returns_last_non_empty_line(self) -> None:
        with mock.patch.object(cli, "tmux_capture", return_value="old\n\nlatest message\n"):
            self.assertEqual(cli.tmux_last_message("%9"), "latest message")

    def test_tmux_last_message_skips_terminal_chrome(self) -> None:
        capture = "\n".join(
            [
                "old",
                "› latest prompt",
                "  gpt-5.5 xhigh · /work/repo · Main [default]  Goal achieved (30m)",
            ]
        )
        with mock.patch.object(cli, "tmux_capture", return_value=capture):
            self.assertEqual(cli.tmux_last_message("%9"), "› latest prompt")

    def test_tmux_last_message_skips_truncated_footer(self) -> None:
        capture = "\n".join(["› latest prompt", "gpt-5.5 xhigh · /work/repo"])
        with mock.patch.object(cli, "tmux_capture", return_value=capture):
            self.assertEqual(cli.tmux_last_message("%9"), "› latest prompt")

    def test_tmux_last_message_skips_working_status(self) -> None:
        capture = "\n".join(["• useful assistant line", "◦ Working (1m 24s • esc to interrupt)"])
        with mock.patch.object(cli, "tmux_capture", return_value=capture):
            self.assertEqual(cli.tmux_last_message("%9"), "• useful assistant line")

    def test_format_status_returns_only_last_message(self) -> None:
        alert = {"id": "A1B", "send_target": "%9", "display_target": "codex:1.0", "status": "open"}
        with mock.patch.object(cli, "tmux_target_exists", return_value=True), mock.patch.object(
            cli, "tmux_last_message", return_value="latest message"
        ):
            self.assertEqual(cli.format_status(alert), "latest message")

    def test_format_status_reports_missing_target_without_history(self) -> None:
        alert = {"id": "A1B", "send_target": "%9", "display_target": "codex:1.0", "status": "open"}
        with mock.patch.object(cli, "tmux_target_exists", return_value=False):
            self.assertEqual(cli.format_status(alert), "tmux target missing: codex:1.0")

    def test_state_store_atomic_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = cli.StateStore(Path(tmp) / "state.json")

            def mutate(state: dict) -> None:
                state.setdefault("alerts", {})["A1B"] = {"id": "A1B", "created_at": int(time.time())}

            store.update(mutate)
            self.assertIn("A1B", store.read()["alerts"])

    def test_env_file_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "bridge.env"
            env_path.write_text("TELEGRAM_CHAT_ID=111\nLIVE_OPS_TELEGRAM_CHAT_ID=222\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TELEGRAM_BRIDGE_ENV_FILES": str(env_path)}, clear=False):
                self.assertEqual(cli.notify_chat_id(), "111")


if __name__ == "__main__":
    unittest.main()
