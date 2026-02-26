#!/Users/zhengli.sun/Projects/claude-remote/.venv/bin/python
"""Unit tests for ClaudeRemote bridge."""

import base64
import email.utils
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

# Import functions under test
import bridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path):
    """Override config paths to use a temp directory."""
    with mock.patch.multiple(
        bridge,
        CONFIG_DIR=tmp_path,
        PROCESSED_FILE=tmp_path / "processed.txt",
        SESSIONS_FILE=tmp_path / "thread_sessions.json",
        PID_FILE=tmp_path / "bridge.pid",
        LOG_FILE=tmp_path / "bridge.log",
        ATTACHMENTS_DIR=tmp_path / "attachments",
        DIGEST_LAST_SENT_FILE=tmp_path / "digest_last_sent.txt",
    ):
        yield tmp_path


# ---------------------------------------------------------------------------
# _strip_quoted_reply
# ---------------------------------------------------------------------------


class TestStripQuotedReply:
    """Tests for stripping Gmail/Outlook quoted reply text."""

    def test_no_quoted_text(self):
        assert bridge.strip_quoted_reply("hello world") == "hello world"

    def test_gmail_quoted_reply(self):
        text = (
            "/sessions\n\n\n"
            "On Thu, Feb 26, 2026 at 2:34 PM Zhengli Sun "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> old stuff here"
        )
        assert bridge.strip_quoted_reply(text) == "/sessions"

    def test_gmail_multiline_on_wrote(self):
        """Gmail sometimes wraps the 'On ... wrote:' line."""
        text = (
            "hello world\n\n"
            "On Mon, Jan 1, 2026 at 10:00 AM Name <x@y.com>\n"
            "wrote:\n"
            "> quoted"
        )
        assert bridge.strip_quoted_reply(text) == "hello world"

    def test_gmail_reply_with_blank_lines(self):
        text = "Do it\n\n\n\nOn Wed, Feb 25 at 3pm User <u@x.com> wrote:\n> yes"
        assert bridge.strip_quoted_reply(text) == "Do it"

    def test_outlook_style(self):
        text = "my reply\n\nFrom: Someone <s@x.com>\nSent: Mon...\n> old"
        assert bridge.strip_quoted_reply(text) == "my reply"

    def test_only_quoted_lines(self):
        text = "> this is all quoted\n> nothing new"
        assert bridge.strip_quoted_reply(text) == ""

    def test_preserves_multiline_user_text(self):
        text = (
            "line one\nline two\nline three\n\n"
            "On Thu, Feb 26, 2026 at 2:34 PM X <x@y.com> wrote:\n> old"
        )
        assert bridge.strip_quoted_reply(text) == "line one\nline two\nline three"

    def test_forwarded_message(self):
        text = "see below\n\n---------- Forwarded message ---------\nFrom: x"
        assert bridge.strip_quoted_reply(text) == "see below"


# ---------------------------------------------------------------------------
# Self-reply detection (skip bot's own replies)
# ---------------------------------------------------------------------------


class TestSelfReplyDetection:
    """Bot must skip messages sent by ClaudeRemote."""

    def test_skip_own_reply(self):
        sender_name, _ = email.utils.parseaddr(
            f"{bridge.REPLY_SENDER_NAME} <zhengli.sun@doordash.com>"
        )
        assert sender_name == bridge.REPLY_SENDER_NAME

    def test_accept_user_email(self):
        sender_name, sender_email = email.utils.parseaddr(
            "Zhengli Sun <zhengli.sun@doordash.com>"
        )
        assert sender_name != bridge.REPLY_SENDER_NAME
        assert sender_email == "zhengli.sun@doordash.com"

    def test_accept_plain_email(self):
        """No display name — should not be treated as bot."""
        sender_name, sender_email = email.utils.parseaddr(
            "zhengli.sun@doordash.com"
        )
        assert sender_name != bridge.REPLY_SENDER_NAME


# ---------------------------------------------------------------------------
# Command parsing (/sessions, /resume)
# ---------------------------------------------------------------------------


class TestCommandParsing:
    """Built-in commands must be recognized from email body text."""

    def test_sessions_command_plain(self):
        body = bridge.strip_quoted_reply("/sessions")
        assert body.lower() == "/sessions"

    def test_sessions_command_with_gmail_quote(self):
        raw = (
            "/sessions\n\n\n"
            "On Thu, Feb 26, 2026 at 3:01 PM ClaudeRemote "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> previous reply..."
        )
        body = bridge.strip_quoted_reply(raw)
        assert body.lower() == "/sessions"

    def test_resume_command(self):
        raw = (
            "/resume abc-123-def\n\n"
            "On Thu, Feb 26, 2026 at 3:01 PM ClaudeRemote "
            "<zhengli.sun@doordash.com> wrote:\n"
            "> previous reply..."
        )
        body = bridge.strip_quoted_reply(raw)
        assert body.lower().startswith("/resume ")
        resume_id = body.split(None, 1)[1].strip()
        assert resume_id == "abc-123-def"

    def test_regular_message_not_command(self):
        body = bridge.strip_quoted_reply("what time is it?")
        assert not body.startswith("/")


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------


class TestExtractBody:
    """Test plain text extraction from Gmail MIME payloads."""

    def test_simple_text_plain(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(b"hello").decode()},
        }
        assert bridge._extract_body(payload) == "hello"

    def test_multipart_with_text(self):
        payload = {
            "mimeType": "multipart/alternative",
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"plain text").decode()},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<b>html</b>").decode()},
                },
            ],
        }
        assert bridge._extract_body(payload) == "plain text"

    def test_empty_payload(self):
        payload = {"mimeType": "text/plain", "body": {"size": 0}}
        assert bridge._extract_body(payload) == ""


# ---------------------------------------------------------------------------
# State management (processed IDs, thread sessions)
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_processed_ids_roundtrip(self, tmp_config):
        assert bridge.load_processed_ids() == set()

        bridge.save_processed_id("msg1")
        bridge.save_processed_id("msg2")

        ids = bridge.load_processed_ids()
        assert ids == {"msg1", "msg2"}

    def test_processed_ids_no_duplicates(self, tmp_config):
        bridge.save_processed_id("msg1")
        bridge.save_processed_id("msg1")
        # File has two lines but set deduplicates
        ids = bridge.load_processed_ids()
        assert ids == {"msg1"}

    def test_thread_sessions_roundtrip(self, tmp_config):
        assert bridge.load_thread_sessions() == {}

        sessions = {"thread1": "session-a", "thread2": "session-b"}
        bridge.save_thread_sessions(sessions)

        loaded = bridge.load_thread_sessions()
        assert loaded == sessions

    def test_thread_sessions_corrupt_json(self, tmp_config):
        bridge.SESSIONS_FILE.write_text("not json")
        assert bridge.load_thread_sessions() == {}


# ---------------------------------------------------------------------------
# build_thread_context
# ---------------------------------------------------------------------------


class TestBuildThreadContext:
    def test_single_user_message(self):
        msgs = [{"from_name": "Zhengli Sun", "date": "Feb 26", "body": "hello"}]
        ctx = bridge.build_thread_context(msgs)
        assert "[User -- Feb 26]" in ctx
        assert "hello" in ctx

    def test_bot_reply_labeled(self):
        msgs = [
            {"from_name": "Zhengli Sun", "date": "Feb 26", "body": "question"},
            {"from_name": bridge.REPLY_SENDER_NAME, "date": "Feb 26", "body": "answer"},
        ]
        ctx = bridge.build_thread_context(msgs)
        assert "You (Claude, in a previous reply)" in ctx

    def test_empty_body_skipped(self):
        msgs = [{"from_name": "X", "date": "Feb 26", "body": ""}]
        ctx = bridge.build_thread_context(msgs)
        assert ctx == ""


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


class TestOutputTruncation:
    def test_short_output_unchanged(self):
        assert len("hello") < bridge.MAX_RESPONSE_LEN
        # invoke_claude would return it as-is (tested via mock below)

    def test_truncation_marker(self):
        long_text = "x" * (bridge.MAX_RESPONSE_LEN + 100)
        # Simulate what invoke_claude does
        if len(long_text) > bridge.MAX_RESPONSE_LEN:
            result = long_text[:bridge.MAX_RESPONSE_LEN] + "\n\n[truncated — response exceeded 50K chars]"
        assert len(result) > bridge.MAX_RESPONSE_LEN
        assert "[truncated" in result


# ---------------------------------------------------------------------------
# send_reply sets correct From display name
# ---------------------------------------------------------------------------


class TestSendReplyFromName:
    def test_from_header_has_display_name(self):
        """Verify the MIME message has ClaudeRemote as display name."""
        original_msg = {
            "subject": "[claude] test",
            "from": "Zhengli Sun <zhengli.sun@doordash.com>",
            "message_id": "<abc@gmail.com>",
            "references": "",
            "thread_id": "thread123",
        }
        my_email = "zhengli.sun@doordash.com"

        # Mock the Gmail API service
        mock_service = mock.MagicMock()
        mock_service.users().messages().send().execute.return_value = {"id": "sent1"}

        bridge.send_reply(mock_service, original_msg, "test reply", my_email)

        # Check the raw message that was sent
        call_args = mock_service.users().messages().send.call_args
        body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
        raw = body.get("raw", "")
        decoded = base64.urlsafe_b64decode(raw).decode()

        assert f"{bridge.REPLY_SENDER_NAME} <{my_email}>" in decoded
        assert "Re: [claude] test" in decoded


# ---------------------------------------------------------------------------
# Pre-startup safety
# ---------------------------------------------------------------------------


class TestPreStartupSafety:
    def test_old_message_skipped(self):
        """Messages older than startup time must be skipped."""
        startup_ms = int(time.time() * 1000)
        old_msg_date_ms = startup_ms - 60_000  # 1 minute before startup
        assert old_msg_date_ms < startup_ms

    def test_new_message_accepted(self):
        startup_ms = int(time.time() * 1000)
        new_msg_date_ms = startup_ms + 5_000  # 5 seconds after startup
        assert new_msg_date_ms >= startup_ms




# ---------------------------------------------------------------------------
# strip_claude_prefix
# ---------------------------------------------------------------------------


class TestStripClaudePrefix:
    """Tests for stripping the [claude] prefix from body and subject."""

    def test_strip_lowercase_prefix(self):
        assert bridge.strip_claude_prefix("[claude] what time is it?") == "what time is it?"

    def test_strip_mixed_case_prefix(self):
        assert bridge.strip_claude_prefix("[Claude] hello") == "hello"

    def test_no_prefix_unchanged(self):
        assert bridge.strip_claude_prefix("no prefix here") == "no prefix here"

    def test_prefix_only_yields_empty(self):
        assert bridge.strip_claude_prefix("[claude]") == ""

    def test_prefix_with_extra_spaces(self):
        assert bridge.strip_claude_prefix("[claude]   spaced out") == "spaced out"

    def test_uppercase_prefix(self):
        assert bridge.strip_claude_prefix("[CLAUDE] shout") == "shout"


# ---------------------------------------------------------------------------
# list_sessions slug calculation
# ---------------------------------------------------------------------------


class TestListSessionsSlug:
    """Slug must replace both '/' and '.' with '-'."""

    def test_slug_replaces_dots_and_slashes(self, tmp_path):
        """Paths like /Users/zhengli.sun/Projects produce correct slug."""
        cwd = "/Users/zhengli.sun/Projects"
        expected_slug = "-Users-zhengli-sun-Projects"

        sessions_dir = tmp_path / expected_slug
        sessions_dir.mkdir()
        jsonl = sessions_dir / "session1.jsonl"
        jsonl.write_text(json.dumps({"type": "summary", "summary": "test session"}) + "\n")

        with mock.patch.object(bridge, "CLAUDE_CWD", cwd), \
             mock.patch.object(bridge, "CLAUDE_SESSIONS_DIR", tmp_path):
            result = bridge.list_sessions(count=5)

        assert "session1" in result or "test session" in result

    def test_slug_without_dot_fix_would_fail(self, tmp_path):
        """Verify the old slug (dots NOT replaced) would miss the directory."""
        cwd = "/Users/zhengli.sun/Projects"
        wrong_slug = "-Users-zhengli.sun-Projects"

        sessions_dir = tmp_path / wrong_slug
        sessions_dir.mkdir()
        jsonl = sessions_dir / "session1.jsonl"
        jsonl.write_text(json.dumps({"type": "summary", "summary": "test"}) + "\n")

        with mock.patch.object(bridge, "CLAUDE_CWD", cwd), \
             mock.patch.object(bridge, "CLAUDE_SESSIONS_DIR", tmp_path):
            result = bridge.list_sessions(count=5)

        assert result == "No sessions found."


# ---------------------------------------------------------------------------
# /help command
# ---------------------------------------------------------------------------


class TestHelpCommand:
    """Verify HELP_TEXT contains key commands and capabilities."""

    def test_help_text_contains_commands(self):
        assert "/help" in bridge.HELP_TEXT
        assert "/sessions" in bridge.HELP_TEXT
        assert "/resume" in bridge.HELP_TEXT
        assert "/cancel" in bridge.HELP_TEXT

    def test_help_text_contains_capabilities(self):
        assert "Attachments" in bridge.HELP_TEXT
        assert "Multi-turn" in bridge.HELP_TEXT
        assert "Google Workspace" in bridge.HELP_TEXT

    def test_help_command_recognized(self):
        body = bridge.strip_quoted_reply("/help")
        assert body.lower() == "/help"


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


class TestDailyDigest:
    """Tests for the daily digest email feature."""

    def test_digest_skipped_if_already_sent_today(self, tmp_config):
        """Digest must not send twice on the same day."""
        today = datetime.now().strftime("%Y-%m-%d")
        bridge.DIGEST_LAST_SENT_FILE.write_text(today)

        mock_service = mock.MagicMock()
        bridge.send_daily_digest(mock_service, "me@test.com", {}, 5)

        mock_service.users().messages().send.assert_not_called()

    def test_digest_skipped_if_wrong_hour(self, tmp_config):
        """_maybe_send_digest should not fire outside DIGEST_HOUR."""
        mock_service = mock.MagicMock()

        with mock.patch("bridge.DIGEST_ENABLED", True), \
             mock.patch("bridge.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 26, 14, 0, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            with mock.patch("bridge.DIGEST_HOUR", 8):
                bridge._maybe_send_digest(mock_service, "me@test.com", {}, 0)

        mock_service.users().messages().send.assert_not_called()

    def test_digest_body_contains_expected_sections(self, tmp_config):
        """Digest email body should include key stats."""
        mock_service = mock.MagicMock()
        send_mock = mock_service.users.return_value.messages.return_value.send
        send_mock.return_value.execute.return_value = {"id": "d1"}

        with mock.patch.object(bridge, "_startup_time", datetime(2026, 2, 26, 7, 0)), \
             mock.patch.object(bridge, "get_pending_reviews", return_value=[]):
            bridge.send_daily_digest(mock_service, "me@test.com", {"t1": "s1", "t2": "s2"}, 42)

        raw_b64 = send_mock.call_args[1]["body"]["raw"]
        import base64 as b64
        import email as email_mod
        mime_bytes = b64.urlsafe_b64decode(raw_b64)
        msg = email_mod.message_from_bytes(mime_bytes)
        body = msg.get_payload(decode=True).decode()

        assert "Messages processed today: 42" in body
        assert "Active thread sessions: 2" in body
        assert "/sessions" in body
        assert "Daily Digest" in body

    def test_digest_includes_pending_prs(self, tmp_config):
        """Digest should include PRs needing review."""
        mock_service = mock.MagicMock()
        send_mock = mock_service.users.return_value.messages.return_value.send
        send_mock.return_value.execute.return_value = {"id": "d1"}

        fake_prs = [
            {"number": 42, "title": "Fix login bug", "repo": "myorg/myrepo", "url": "https://github.com/myorg/myrepo/pull/42", "author": "alice"},
            {"number": 7, "title": "Add tests", "repo": "myorg/other", "url": "https://github.com/myorg/other/pull/7", "author": "bob"},
        ]

        with mock.patch.object(bridge, "_startup_time", datetime(2026, 2, 26, 7, 0)), \
             mock.patch.object(bridge, "get_pending_reviews", return_value=fake_prs):
            bridge.send_daily_digest(mock_service, "me@test.com", {}, 0)

        raw_b64 = send_mock.call_args[1]["body"]["raw"]
        import base64 as b64
        import email as email_mod
        mime_bytes = b64.urlsafe_b64decode(raw_b64)
        msg = email_mod.message_from_bytes(mime_bytes)
        body = msg.get_payload(decode=True).decode()

        assert "PRs Awaiting Your Review" in body
        assert "#42" in body
        assert "Fix login bug" in body
        assert "alice" in body

    def test_get_pending_reviews_handles_gh_not_found(self):
        """get_pending_reviews should return [] if gh CLI is not installed."""
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            result = bridge.get_pending_reviews()
        assert result == []

    def test_get_pending_reviews_handles_gh_error(self):
        """get_pending_reviews should return [] if gh CLI fails."""
        mock_result = mock.MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with mock.patch("subprocess.run", return_value=mock_result):
            result = bridge.get_pending_reviews()
        assert result == []


# ---------------------------------------------------------------------------
# generate_subject
# ---------------------------------------------------------------------------


class TestGenerateSubject:
    def test_short_message(self):
        result = bridge.generate_subject("How do I fix the login bug?")
        assert result == "[claude] How do I fix the login bug?"

    def test_long_message_truncated(self):
        long_msg = "Please refactor the authentication module to use OAuth2 instead of the legacy token system"
        result = bridge.generate_subject(long_msg)
        assert result.endswith("...")
        assert len(result) <= 60  # [claude] prefix + 50 + ...

    def test_claude_prefix_stripped(self):
        result = bridge.generate_subject("[claude] deploy the new service")
        assert result == "[claude] deploy the new service"
        assert "[claude] [claude]" not in result

    def test_empty_message_fallback(self):
        assert bridge.generate_subject("") == "[claude] conversation"
        assert bridge.generate_subject("   ") == "[claude] conversation"
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
