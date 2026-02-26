#!/Users/zhengli.sun/Projects/claude-remote/.venv/bin/python
"""ClaudeRemote Slack Bridge: Slack remote interface for Claude Code.

Polls a Slack channel for messages mentioning the bot, feeds them to Claude Code
via subprocess, and replies in the same Slack thread.

Usage:
    ./slack_bridge.py start   # Start daemon (background)
    ./slack_bridge.py stop    # Stop daemon
    ./slack_bridge.py run     # Run in foreground (for debugging)
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".claude-remote"
SLACK_TOKEN_FILE = CONFIG_DIR / "slack_token.txt"  # Bot User OAuth Token (xoxb-...)
SLACK_CHANNEL_ID = os.environ.get("CLAUDE_REMOTE_SLACK_CHANNEL", "")  # Channel to watch
SLACK_BOT_USER_ID = ""  # Set on startup by calling auth.test
POLL_INTERVAL = 5  # seconds (Slack is faster than email)
CLAUDE_TIMEOUT = 600  # 10 minutes
MAX_RESPONSE_LEN = 50_000  # chars
CLAUDE_CWD = "/Users/zhengli.sun/Projects"
PROCESSED_FILE = CONFIG_DIR / "slack_processed.txt"
SESSIONS_FILE = CONFIG_DIR / "slack_sessions.json"
PID_FILE = CONFIG_DIR / "slack_bridge.pid"
LOG_FILE = CONFIG_DIR / "slack_bridge.log"
RATE_LIMIT_PER_HOUR = 20
RATE_LIMIT_FILE = CONFIG_DIR / "slack_rate_limit.json"
CANCEL_FILE = CONFIG_DIR / "slack_cancel.txt"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"

HELP_TEXT = """\
*ClaudeRemote Slack Bridge* -- Available commands:

`/help` -- Show this help message
`/status` -- Show bridge status and uptime
`/sessions` -- List recent Claude Code sessions
`/resume <session-id>` -- Resume a specific session
`/cancel` -- Cancel a running task

Regular messages -- Mention the bot and your message will be sent to Claude Code
Multi-turn -- Reply in a thread to continue a conversation
"""

SETUP_INSTRUCTIONS = """\
Slack Bridge Setup Required
============================

1. Create a Slack App at https://api.slack.com/apps
2. Add Bot Token Scopes: channels:history, channels:read, chat:write, users:read
3. Install to workspace, copy Bot User OAuth Token (xoxb-...)
4. Save token to: ~/.claude-remote/slack_token.txt
5. Invite the bot to the desired channel
6. Set CLAUDE_REMOTE_SLACK_CHANNEL env var or edit the config in slack_bridge.py

Example:
    echo "xoxb-your-token-here" > ~/.claude-remote/slack_token.txt
    export CLAUDE_REMOTE_SLACK_CHANNEL="C01234ABCDE"
    ./slack_bridge.py run
"""

# Module-level state (set in run_bridge)
_startup_time: datetime | None = None
_messages_processed: int = 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("claude-remote-slack")


def setup_logging(foreground: bool = False):
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    log.addHandler(file_handler)
    if foreground:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        log.addHandler(stream_handler)


# ---------------------------------------------------------------------------
# Slack Authentication
# ---------------------------------------------------------------------------


def authenticate() -> WebClient:
    """Read token from file and create a Slack WebClient."""
    if not SLACK_TOKEN_FILE.exists():
        print(SETUP_INSTRUCTIONS, file=sys.stderr)
        sys.exit(1)

    token = SLACK_TOKEN_FILE.read_text().strip()
    if not token.startswith("xoxb-"):
        print(
            f"Error: Token in {SLACK_TOKEN_FILE} does not look like a bot token (xoxb-...).\n"
            "Please check your token and try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    return WebClient(token=token)


def get_bot_user_id(client: WebClient) -> str:
    """Call auth.test to get the bot's own user ID."""
    response = client.auth_test()
    return response["user_id"]


# ---------------------------------------------------------------------------
# Slack Message Functions
# ---------------------------------------------------------------------------


def poll_messages(client: WebClient, channel_id: str, oldest_ts: str) -> list[dict]:
    """Fetch messages from the channel newer than oldest_ts."""
    try:
        response = client.conversations_history(
            channel=channel_id,
            oldest=oldest_ts,
            limit=50,
        )
        return response.get("messages", [])
    except SlackApiError as e:
        log.error("Error polling messages: %s", e.response["error"])
        return []


def get_thread_messages(client: WebClient, channel_id: str, thread_ts: str) -> list[dict]:
    """Get all replies in a thread for context."""
    try:
        response = client.conversations_replies(
            channel=channel_id,
            ts=thread_ts,
            limit=100,
        )
        return response.get("messages", [])
    except SlackApiError as e:
        log.error("Error fetching thread: %s", e.response["error"])
        return []


def send_reply(client: WebClient, channel_id: str, thread_ts: str, text: str):
    """Post a reply in a Slack thread."""
    # Slack has a 40K char limit per message; split if needed
    max_len = 39_000
    if len(text) <= max_len:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    else:
        # Send in chunks
        parts = [text[i : i + max_len] for i in range(0, len(text), max_len)]
        for i, part in enumerate(parts):
            prefix = f"(part {i + 1}/{len(parts)})\n" if len(parts) > 1 else ""
            client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=prefix + part,
            )


def is_bot_mention(message: dict, bot_user_id: str) -> bool:
    """Check if a message mentions the bot."""
    text = message.get("text", "")
    return f"<@{bot_user_id}>" in text


def is_direct_message(channel_id: str, client: WebClient) -> bool:
    """Check if the channel is a DM (im) channel."""
    try:
        info = client.conversations_info(channel=channel_id)
        return info["channel"].get("is_im", False)
    except SlackApiError:
        return False


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the bot mention from the message text."""
    return text.replace(f"<@{bot_user_id}>", "").strip()


def build_thread_context(messages: list[dict], bot_user_id: str) -> str:
    """Format thread messages as conversation context for Claude."""
    parts = []
    for msg in messages:
        user = msg.get("user", "unknown")
        text = msg.get("text", "").strip()
        if not text:
            continue
        if user == bot_user_id:
            role = "You (Claude, in a previous reply)"
        else:
            role = "User"
        ts = msg.get("ts", "")
        parts.append(f"[{role} -- ts:{ts}]\n{text}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Claude Code Integration
# ---------------------------------------------------------------------------


def _check_cancel(thread_id: str) -> bool:
    """Check if thread_id is in the cancel file; remove it if found."""
    if not CANCEL_FILE.exists():
        return False
    lines = CANCEL_FILE.read_text().strip().splitlines()
    if thread_id in lines:
        remaining = [t for t in lines if t != thread_id]
        CANCEL_FILE.write_text("\n".join(remaining) + "\n" if remaining else "")
        return True
    return False


def invoke_claude(
    message: str,
    session_id: str,
    resume: bool = False,
    thread_id: str = None,
) -> str:
    """Run claude -p as a subprocess."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Strip nested session guard

    cmd = ["claude", "-p", "--output-format", "text"]
    if resume:
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])
    cmd.append(message)

    log.info("Invoking Claude (resume=%s, session=%s)", resume, session_id[:8])

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=CLAUDE_CWD,
        )
        elapsed = 0
        while proc.poll() is None:
            time.sleep(1)
            elapsed += 1
            if thread_id and _check_cancel(thread_id):
                proc.kill()
                proc.wait()
                log.info("Cancelled Claude for thread %s", thread_id)
                return (
                    "[Cancelled by user]\n\n"
                    "The task was cancelled. Send a new message to start fresh."
                )
            if elapsed >= CLAUDE_TIMEOUT:
                proc.kill()
                proc.wait()
                output = (
                    f"[Timed out after {CLAUDE_TIMEOUT // 60} minutes]\n\n"
                    "The task was too long for a single request. You can:\n"
                    "- Reply in this thread to continue where it left off\n"
                    "- Break the task into smaller steps\n"
                    "- Reply /resume to pick up the session"
                )
                log.warning("Claude timed out for session %s", session_id[:8])
                break
        else:
            output = proc.stdout.read().strip()
            if proc.returncode != 0 and not output:
                stderr_text = proc.stderr.read().strip()
                output = (
                    f"[Claude exited with code {proc.returncode}]\n\n"
                    + (f"Error: {stderr_text}\n\n" if stderr_text else "")
                    + "Reply in this thread to retry, or start a new thread."
                )
            if not output:
                output = (
                    "[Claude returned empty output]\n\n"
                    "This sometimes happens with very short tasks. "
                    "Try rephrasing your request."
                )
    except FileNotFoundError:
        output = (
            "[Error: 'claude' command not found]\n\n"
            "Claude Code doesn't appear to be installed or is not in PATH.\n"
            "Install it: npm install -g @anthropic-ai/claude-code"
        )
        log.error("claude command not found")

    # Truncate if too large
    if len(output) > MAX_RESPONSE_LEN:
        output = (
            output[:MAX_RESPONSE_LEN]
            + "\n\n[truncated -- response exceeded 50K chars]"
        )

    return output


def list_sessions(count: int = 10) -> str:
    """List recent Claude Code sessions by reading JSONL files from disk."""
    cwd_slug = CLAUDE_CWD.replace("/", "-").replace(".", "-")
    sessions_dir = CLAUDE_SESSIONS_DIR / cwd_slug
    if not sessions_dir.exists():
        return "No sessions found."

    entries = []
    for f in sorted(
        sessions_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:count]:
        first_msg = ""
        for line in f.read_text().splitlines():
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("type") == "summary":
                first_msg = d.get("summary", "")[:80]
                break
            msg = d.get("message")
            if isinstance(msg, dict) and msg.get("role") == "user" and not first_msg:
                content = msg.get("content", "")
                if isinstance(content, str):
                    first_msg = content.strip().replace("\n", " ")[:80]
                elif isinstance(content, list) and content:
                    c = content[0]
                    first_msg = (
                        c.get("text", "") if isinstance(c, dict) else str(c)
                    )[:80]
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        entries.append(f"`{f.stem}`\n  {mtime:%Y-%m-%d %H:%M}  {first_msg}")

    if not entries:
        return "No sessions found."
    header = "Recent Claude sessions (reply `/resume <id>` to continue one):\n\n"
    return header + "\n\n".join(entries)


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------


def load_processed_ids() -> set[str]:
    """Load processed message timestamps from file."""
    if not PROCESSED_FILE.exists():
        return set()
    return set(PROCESSED_FILE.read_text().strip().splitlines())


def save_processed_id(msg_ts: str):
    """Append a processed message timestamp to file."""
    with open(PROCESSED_FILE, "a") as f:
        f.write(msg_ts + "\n")


def load_thread_sessions() -> dict[str, str]:
    """Load thread_ts -> session_id mapping from JSON file."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_thread_sessions(sessions: dict[str, str]):
    """Save thread_ts -> session_id mapping to JSON file."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def _check_rate_limit() -> tuple[bool, int]:
    """Check if we're within the rate limit. Returns (allowed, remaining)."""
    now = time.time()
    cutoff = now - 3600  # 1 hour window

    timestamps = []
    if RATE_LIMIT_FILE.exists():
        try:
            timestamps = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            timestamps = []

    # Prune old entries
    timestamps = [t for t in timestamps if t > cutoff]

    remaining = RATE_LIMIT_PER_HOUR - len(timestamps)
    return remaining > 0, max(remaining, 0)


def _record_invocation():
    """Record a Claude invocation timestamp for rate limiting."""
    now = time.time()
    cutoff = now - 3600

    timestamps = []
    if RATE_LIMIT_FILE.exists():
        try:
            timestamps = json.loads(RATE_LIMIT_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            timestamps = []

    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    RATE_LIMIT_FILE.write_text(json.dumps(timestamps))


# ---------------------------------------------------------------------------
# Main Polling Loop
# ---------------------------------------------------------------------------


def run_bridge(foreground: bool = False):
    """Main loop: poll Slack, process messages, reply in threads."""
    global _startup_time, _messages_processed, SLACK_BOT_USER_ID
    setup_logging(foreground=foreground)
    _messages_processed = 0
    log.info("Starting ClaudeRemote Slack Bridge")

    # Validate channel ID
    channel_id = SLACK_CHANNEL_ID
    if not channel_id:
        print(
            "Error: CLAUDE_REMOTE_SLACK_CHANNEL environment variable is not set.\n"
            "Set it to the Slack channel ID to watch.\n\n"
            "Example: export CLAUDE_REMOTE_SLACK_CHANNEL='C01234ABCDE'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Authenticate
    try:
        client = authenticate()
        SLACK_BOT_USER_ID = get_bot_user_id(client)
    except Exception as e:
        log.error("Authentication failed: %s", e)
        print(f"Authentication failed: {e}", file=sys.stderr)
        sys.exit(1)

    log.info("Authenticated as bot user %s", SLACK_BOT_USER_ID)

    # Load state
    processed_ids = load_processed_ids()
    thread_sessions = load_thread_sessions()

    # Record startup time -- ignore messages older than this
    startup_ts = str(time.time())
    _startup_time = datetime.now(timezone.utc)
    log.info(
        "Startup time: %s (ignoring older messages)", _startup_time.isoformat()
    )
    log.info(
        "Loaded %d processed IDs, %d thread sessions",
        len(processed_ids),
        len(thread_sessions),
    )

    # Track the latest timestamp we have seen for efficient polling
    latest_ts = startup_ts

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        try:
            latest_ts = _poll_cycle(
                client,
                channel_id,
                processed_ids,
                thread_sessions,
                latest_ts,
                startup_ts,
            )
        except Exception:
            log.exception("Error in poll cycle")

        # Sleep in small increments so we respond to signals
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("ClaudeRemote Slack Bridge stopped")


def _poll_cycle(
    client: WebClient,
    channel_id: str,
    processed_ids: set[str],
    thread_sessions: dict[str, str],
    latest_ts: str,
    startup_ts: str,
) -> str:
    """Single poll iteration. Returns updated latest_ts."""
    global _messages_processed

    messages = poll_messages(client, channel_id, latest_ts)

    if not messages:
        return latest_ts

    # Messages come newest-first; reverse for chronological processing
    messages.sort(key=lambda m: float(m.get("ts", "0")))

    new_latest_ts = latest_ts
    for msg in messages:
        msg_ts = msg.get("ts", "")
        if not msg_ts:
            continue

        # Track latest timestamp
        if float(msg_ts) > float(new_latest_ts):
            new_latest_ts = msg_ts

        # Skip already-processed messages
        if msg_ts in processed_ids:
            continue

        # Skip messages from before startup
        if float(msg_ts) < float(startup_ts):
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Skip bot's own messages
        if msg.get("user") == SLACK_BOT_USER_ID:
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Skip messages from other bots
        if msg.get("bot_id") or msg.get("subtype") == "bot_message":
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Only respond to messages that mention the bot
        if not is_bot_mention(msg, SLACK_BOT_USER_ID):
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Extract message text, stripping the bot mention
        body = strip_bot_mention(msg.get("text", ""), SLACK_BOT_USER_ID)
        if not body:
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Determine thread_ts: use existing thread or start new one
        thread_ts = msg.get("thread_ts", msg_ts)

        log.info(
            "Processing message %s in thread %s: %.80s", msg_ts, thread_ts, body
        )

        # Built-in commands
        if body.lower() == "/help":
            send_reply(client, channel_id, thread_ts, HELP_TEXT)
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        if body.lower() == "/status":
            uptime = (
                datetime.now(timezone.utc) - _startup_time if _startup_time else None
            )
            if uptime:
                hours, remainder = divmod(int(uptime.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = f"{hours}h {minutes}m {seconds}s"
            else:
                uptime_str = "unknown"
            current_session = thread_sessions.get(thread_ts, "none")
            response = (
                f"*ClaudeRemote Slack Bridge Status*\n"
                f"---\n"
                f"Uptime: {uptime_str}\n"
                f"Messages processed: {_messages_processed}\n"
                f"Active threads: {len(thread_sessions)}\n"
                f"This thread's session: `{current_session}`\n"
                f"PID: {os.getpid()}"
            )
            send_reply(client, channel_id, thread_ts, response)
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        if body.lower() == "/cancel":
            with open(CANCEL_FILE, "a") as f:
                f.write(thread_ts + "\n")
            response = (
                "Cancel requested. If a task is running in this thread, "
                "it will be stopped."
            )
            send_reply(client, channel_id, thread_ts, response)
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        if body.lower() == "/sessions":
            response = list_sessions()
            send_reply(client, channel_id, thread_ts, response)
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        if body.lower().startswith("/resume "):
            resume_id = body.split(None, 1)[1].strip()
            session_id = resume_id
            thread_sessions[thread_ts] = session_id
            save_thread_sessions(thread_sessions)
            response = invoke_claude(
                "The user just resumed this session from Slack. "
                "Briefly summarize what you were working on, and ask how to proceed.",
                session_id,
                resume=True,
            )
            send_reply(client, channel_id, thread_ts, response)
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            _messages_processed += 1
            continue

        # Rate limit check
        allowed, remaining = _check_rate_limit()
        if not allowed:
            send_reply(
                client,
                channel_id,
                thread_ts,
                "Rate limit reached (20 invocations/hour). Please try again later.",
            )
            processed_ids.add(msg_ts)
            save_processed_id(msg_ts)
            continue

        # Determine session: resume existing or start new
        resume = thread_ts in thread_sessions
        if resume:
            session_id = thread_sessions[thread_ts]
        else:
            session_id = str(uuid.uuid4())

        # Build the prompt: for resumed sessions just send latest message,
        # for fresh sessions include the full thread for context
        if resume:
            prompt = body
        else:
            thread_msgs = get_thread_messages(client, channel_id, thread_ts)
            if len(thread_msgs) > 1:
                prompt = (
                    "Here is the Slack conversation so far:\n\n"
                    + build_thread_context(thread_msgs, SLACK_BOT_USER_ID)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = body

        # Record rate limit
        _record_invocation()

        # Invoke Claude
        response = invoke_claude(
            prompt, session_id, resume=resume, thread_id=thread_ts
        )

        # If resume failed, retry with fresh session and thread context
        if resume and "[Claude exited with code" in response:
            log.warning(
                "Resume failed for session %s, retrying fresh",
                session_id[:8],
            )
            session_id = str(uuid.uuid4())
            thread_msgs = get_thread_messages(client, channel_id, thread_ts)
            if len(thread_msgs) > 1:
                prompt = (
                    "Here is the Slack conversation so far:\n\n"
                    + build_thread_context(thread_msgs, SLACK_BOT_USER_ID)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = body
            response = invoke_claude(
                prompt, session_id, resume=False, thread_id=thread_ts
            )

        # Reply in thread
        try:
            send_reply(client, channel_id, thread_ts, response)
            log.info(
                "Replied to message %s (session=%s)", msg_ts, session_id[:8]
            )
        except Exception:
            log.exception("Failed to send reply for message %s", msg_ts)

        # Update state
        processed_ids.add(msg_ts)
        save_processed_id(msg_ts)
        thread_sessions[thread_ts] = session_id
        save_thread_sessions(thread_sessions)
        _messages_processed += 1

    return new_latest_ts


# ---------------------------------------------------------------------------
# Daemon Management
# ---------------------------------------------------------------------------


def start_daemon():
    """Start the bridge as a background process."""
    if PID_FILE.exists():
        pid = int(PID_FILE.read_text().strip())
        try:
            os.kill(pid, 0)  # Check if process exists
            print(f"Slack bridge already running (PID {pid})")
            return
        except OSError:
            PID_FILE.unlink()  # Stale PID file

    # Fork into background
    pid = os.fork()
    if pid > 0:
        # Parent
        print(f"Slack bridge started (PID {pid})")
        print(f"Logs: {LOG_FILE}")
        return

    # Child: become session leader
    os.setsid()

    # Second fork to fully detach
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect stdio
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    try:
        run_bridge(foreground=False)
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


def stop_daemon():
    """Stop the running bridge daemon."""
    if not PID_FILE.exists():
        print("Slack bridge is not running (no PID file)")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Slack bridge (PID {pid})")
        # Wait briefly for clean shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except OSError:
                break
        print("Slack bridge stopped")
    except OSError:
        print(f"Process {pid} not found (stale PID file)")
    finally:
        if PID_FILE.exists():
            PID_FILE.unlink()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "start":
        start_daemon()
    elif command == "stop":
        stop_daemon()
    elif command == "run":
        run_bridge(foreground=True)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
