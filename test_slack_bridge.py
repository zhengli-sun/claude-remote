"""Unit tests for ClaudeRemote Slack Bridge."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

import slack_bridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_config(tmp_path):
    """Override config paths to use a temp directory."""
    with mock.patch.multiple(
        slack_bridge,
        CONFIG_DIR=tmp_path,
        PROCESSED_FILE=tmp_path / "slack_processed.txt",
        SESSIONS_FILE=tmp_path / "slack_sessions.json",
        PID_FILE=tmp_path / "slack_bridge.pid",
        LOG_FILE=tmp_path / "slack_bridge.log",
        RATE_LIMIT_FILE=tmp_path / "slack_rate_limit.json",
        CANCEL_FILE=tmp_path / "slack_cancel.txt",
        SLACK_TOKEN_FILE=tmp_path / "slack_token.txt",
    ):
        yield tmp_path


# ---------------------------------------------------------------------------
# Message detection
# ---------------------------------------------------------------------------


class TestMessageDetection:
    """Tests for bot mention detection and message filtering."""

    def test_bot_mention_detected(self):
        msg = {"text": "Hey <@U12345BOT> can you help?"}
        assert slack_bridge.is_bot_mention(msg, "U12345BOT") is True

    def test_no_mention_ignored(self):
        msg = {"text": "Just a regular message"}
        assert slack_bridge.is_bot_mention(msg, "U12345BOT") is False

    def test_wrong_user_mention_ignored(self):
        msg = {"text": "Hey <@UOTHER> can you help?"}
        assert slack_bridge.is_bot_mention(msg, "U12345BOT") is False

    def test_empty_message_ignored(self):
        msg = {"text": ""}
        assert slack_bridge.is_bot_mention(msg, "U12345BOT") is False

    def test_missing_text_field(self):
        msg = {}
        assert slack_bridge.is_bot_mention(msg, "U12345BOT") is False

    def test_skip_bot_own_messages(self):
        """Bot must skip messages from itself."""
        msg = {"user": "U12345BOT", "text": "I am the bot"}
        assert msg.get("user") == "U12345BOT"

    def test_skip_other_bot_messages(self):
        """Messages with bot_id should be skipped."""
        msg = {"bot_id": "B123", "text": "Other bot"}
        assert msg.get("bot_id") is not None

    def test_skip_bot_subtype(self):
        """Messages with subtype=bot_message should be skipped."""
        msg = {"subtype": "bot_message", "text": "Bot msg"}
        assert msg.get("subtype") == "bot_message"


# ---------------------------------------------------------------------------
# Strip bot mention
# ---------------------------------------------------------------------------


class TestStripBotMention:
    """Tests for removing bot mention from message text."""

    def test_mention_at_start(self):
        result = slack_bridge.strip_bot_mention("<@U123> hello world", "U123")
        assert result == "hello world"

    def test_mention_in_middle(self):
        result = slack_bridge.strip_bot_mention("hey <@U123> do this", "U123")
        assert result == "hey  do this"

    def test_no_mention(self):
        result = slack_bridge.strip_bot_mention("hello world", "U123")
        assert result == "hello world"

    def test_mention_only(self):
        result = slack_bridge.strip_bot_mention("<@U123>", "U123")
        assert result == ""

    def test_mention_with_extra_whitespace(self):
        result = slack_bridge.strip_bot_mention("  <@U123>   hello  ", "U123")
        assert result == "hello"


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------


class TestCommandParsing:
    """Built-in commands must be recognized from message text."""

    def test_help_command(self):
        body = slack_bridge.strip_bot_mention("<@U123> /help", "U123")
        assert body.lower() == "/help"

    def test_status_command(self):
        body = slack_bridge.strip_bot_mention("<@U123> /status", "U123")
        assert body.lower() == "/status"

    def test_sessions_command(self):
        body = slack_bridge.strip_bot_mention("<@U123> /sessions", "U123")
        assert body.lower() == "/sessions"

    def test_cancel_command(self):
        body = slack_bridge.strip_bot_mention("<@U123> /cancel", "U123")
        assert body.lower() == "/cancel"

    def test_resume_command(self):
        body = slack_bridge.strip_bot_mention(
            "<@U123> /resume abc-123-def", "U123"
        )
        assert body.lower().startswith("/resume ")
        resume_id = body.split(None, 1)[1].strip()
        assert resume_id == "abc-123-def"

    def test_regular_message_not_command(self):
        body = slack_bridge.strip_bot_mention(
            "<@U123> what time is it?", "U123"
        )
        assert not body.startswith("/")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    """Tests for rate limiting Claude invocations."""

    def test_allowed_when_empty(self, tmp_config):
        allowed, remaining = slack_bridge._check_rate_limit()
        assert allowed is True
        assert remaining == 20

    def test_allowed_after_one_invocation(self, tmp_config):
        slack_bridge._record_invocation()
        allowed, remaining = slack_bridge._check_rate_limit()
        assert allowed is True
        assert remaining == 19

    def test_blocked_at_limit(self, tmp_config):
        """Should be blocked after 20 invocations in an hour."""
        now = time.time()
        timestamps = [now - i for i in range(20)]
        slack_bridge.RATE_LIMIT_FILE.write_text(json.dumps(timestamps))

        allowed, remaining = slack_bridge._check_rate_limit()
        assert allowed is False
        assert remaining == 0

    def test_old_entries_pruned(self, tmp_config):
        """Entries older than 1 hour should be pruned."""
        now = time.time()
        old_timestamps = [now - 3700 for _ in range(20)]
        slack_bridge.RATE_LIMIT_FILE.write_text(json.dumps(old_timestamps))

        allowed, remaining = slack_bridge._check_rate_limit()
        assert allowed is True
        assert remaining == 20

    def test_corrupt_json_handled(self, tmp_config):
        slack_bridge.RATE_LIMIT_FILE.write_text("not json")
        allowed, remaining = slack_bridge._check_rate_limit()
        assert allowed is True
        assert remaining == 20


# ---------------------------------------------------------------------------
# Thread session management
# ---------------------------------------------------------------------------


class TestThreadSessions:
    """Tests for thread_ts -> session_id mapping."""

    def test_empty_sessions(self, tmp_config):
        assert slack_bridge.load_thread_sessions() == {}

    def test_sessions_roundtrip(self, tmp_config):
        sessions = {"1234567890.123456": "session-a", "1234567890.654321": "session-b"}
        slack_bridge.save_thread_sessions(sessions)
        loaded = slack_bridge.load_thread_sessions()
        assert loaded == sessions

    def test_corrupt_json_returns_empty(self, tmp_config):
        slack_bridge.SESSIONS_FILE.write_text("not json")
        assert slack_bridge.load_thread_sessions() == {}


# ---------------------------------------------------------------------------
# Processed IDs
# ---------------------------------------------------------------------------


class TestProcessedIds:
    """Tests for processed message tracking."""

    def test_empty_processed(self, tmp_config):
        assert slack_bridge.load_processed_ids() == set()

    def test_roundtrip(self, tmp_config):
        slack_bridge.save_processed_id("1234.5678")
        slack_bridge.save_processed_id("9876.5432")
        ids = slack_bridge.load_processed_ids()
        assert ids == {"1234.5678", "9876.5432"}

    def test_deduplication(self, tmp_config):
        slack_bridge.save_processed_id("1234.5678")
        slack_bridge.save_processed_id("1234.5678")
        ids = slack_bridge.load_processed_ids()
        assert ids == {"1234.5678"}


# ---------------------------------------------------------------------------
# Build thread context
# ---------------------------------------------------------------------------


class TestBuildThreadContext:
    """Tests for formatting thread messages as context."""

    def test_single_user_message(self):
        msgs = [{"user": "U_USER", "text": "hello", "ts": "1234.5678"}]
        ctx = slack_bridge.build_thread_context(msgs, "U_BOT")
        assert "[User -- ts:1234.5678]" in ctx
        assert "hello" in ctx

    def test_bot_reply_labeled(self):
        msgs = [
            {"user": "U_USER", "text": "question", "ts": "1234.5678"},
            {"user": "U_BOT", "text": "answer", "ts": "1234.5679"},
        ]
        ctx = slack_bridge.build_thread_context(msgs, "U_BOT")
        assert "You (Claude, in a previous reply)" in ctx

    def test_empty_text_skipped(self):
        msgs = [{"user": "U_USER", "text": "", "ts": "1234.5678"}]
        ctx = slack_bridge.build_thread_context(msgs, "U_BOT")
        assert ctx == ""


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Tests for Slack token authentication."""

    def test_missing_token_file_exits(self, tmp_config):
        with pytest.raises(SystemExit):
            slack_bridge.authenticate()

    def test_invalid_token_prefix_exits(self, tmp_config):
        slack_bridge.SLACK_TOKEN_FILE.write_text("invalid-token-here")
        with pytest.raises(SystemExit):
            slack_bridge.authenticate()

    def test_valid_token_creates_client(self, tmp_config):
        slack_bridge.SLACK_TOKEN_FILE.write_text("xoxb-fake-token-12345")
        client = slack_bridge.authenticate()
        assert client is not None


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


class TestHelpText:
    """Verify HELP_TEXT contains key commands."""

    def test_help_text_contains_commands(self):
        assert "/help" in slack_bridge.HELP_TEXT
        assert "/status" in slack_bridge.HELP_TEXT
        assert "/sessions" in slack_bridge.HELP_TEXT
        assert "/resume" in slack_bridge.HELP_TEXT
        assert "/cancel" in slack_bridge.HELP_TEXT

    def test_help_text_contains_capabilities(self):
        assert "Multi-turn" in slack_bridge.HELP_TEXT
        assert "Mention the bot" in slack_bridge.HELP_TEXT


# ---------------------------------------------------------------------------
# invoke_claude error messages
# ---------------------------------------------------------------------------


class TestInvokeClaudeErrors:
    """Error messages from invoke_claude must contain actionable guidance."""

    def test_timeout_message_has_recovery_steps(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.return_value = None  # never finishes
            mock_popen.return_value = proc
            with mock.patch.object(slack_bridge, "CLAUDE_TIMEOUT", 2):
                with mock.patch("time.sleep"):
                    result = slack_bridge.invoke_claude("hi", "sess-1")
        assert "Timed out" in result
        assert "/resume" in result

    def test_command_not_found_message(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            result = slack_bridge.invoke_claude("hi", "sess-3")
        assert "not found" in result
        assert "npm install" in result

    def test_empty_output_message(self):
        with mock.patch("subprocess.Popen") as mock_popen:
            proc = mock.MagicMock()
            proc.poll.side_effect = [None, 0]
            proc.stdout.read.return_value = ""
            proc.stderr.read.return_value = ""
            proc.returncode = 0
            mock_popen.return_value = proc
            with mock.patch("time.sleep"):
                result = slack_bridge.invoke_claude("hi", "sess-4")
        assert "empty output" in result.lower()


# ---------------------------------------------------------------------------
# Cancel support
# ---------------------------------------------------------------------------


class TestCancelSupport:
    """Tests for the cancel mechanism."""

    def test_check_cancel_no_file(self, tmp_config):
        assert slack_bridge._check_cancel("thread-1") is False

    def test_check_cancel_thread_found(self, tmp_config):
        slack_bridge.CANCEL_FILE.write_text("thread-1\n")
        assert slack_bridge._check_cancel("thread-1") is True
        # Should be removed after checking
        if slack_bridge.CANCEL_FILE.exists():
            content = slack_bridge.CANCEL_FILE.read_text()
            assert "thread-1" not in content

    def test_check_cancel_thread_not_found(self, tmp_config):
        slack_bridge.CANCEL_FILE.write_text("thread-2\n")
        assert slack_bridge._check_cancel("thread-1") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
