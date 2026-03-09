#!/usr/bin/env python3
"""ClaudeRemote: unified Gmail + Slack remote interface for Claude Code.

Polls Gmail for emails with "cc" subject prefix and/or Slack via MCP for
new messages, feeds them to Claude Code via subprocess, and replies in the
same email thread or Slack thread.

Usage:
    ./bridge.py run [--gmail] [--slack] [--all]   # Run in foreground
    ./bridge.py start [--gmail] [--slack] [--all]  # Start daemon
    ./bridge.py stop                                # Stop daemon
    ./bridge.py status                              # Show status
"""
from __future__ import annotations

import argparse
import base64
import email.utils
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path.home() / ".claude-remote"

# Load env overrides from ~/.claude-remote/env
_env_file = CONFIG_DIR / "env"
if _env_file.is_file():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                if _line.startswith("export "):
                    _line = _line[7:]
                _key, _, _val = _line.partition("=")
                _val = _val.strip('"').strip("'").replace("$HOME", str(Path.home()))
                os.environ.setdefault(_key.strip(), _val)

CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
GMAIL_PROCESSED_FILE = CONFIG_DIR / "processed.txt"
SESSIONS_FILE = CONFIG_DIR / "thread_sessions.json"
PID_FILE = CONFIG_DIR / "bridge.pid"
LOCK_FILE = CONFIG_DIR / "bridge.lock"
LOG_FILE = CONFIG_DIR / "bridge.log"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

POLL_INTERVAL = int(os.environ.get("CLAUDE_REMOTE_POLL_INTERVAL", "15"))
CLAUDE_TIMEOUT = 600  # 10 minutes
MAX_RESPONSE_LEN = 50_000  # chars
CLAUDE_CWD = os.environ.get("CLAUDE_REMOTE_CWD", str(Path.home() / "Projects"))
SUBJECT_PREFIX = "cc"
REPLY_SENDER_NAME = "ClaudeRemote"  # Display name on reply emails
ATTACHMENTS_DIR = CONFIG_DIR / "attachments"
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB
ATTACHMENT_MAX_AGE_HOURS = 24
PROGRESS_INTERVAL = 120  # seconds between "still working" emails
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"

HELP_TEXT = """\
Available commands and capabilities:

/help -- Show this help message
/sessions -- List recent Claude Code sessions
/resume <session-id> -- Resume a specific session
/cancel -- Cancel a running task (coming soon)
/summary -- Generate and send a work summary for today
/brief -- Morning briefing with TODOs, calendar, PRs, email, Slack

Regular messages -- Sent to Claude Code for processing
Attachments -- Attach files to emails and Claude will analyze them
Calendar, email, docs -- Claude has access to Google Workspace tools
Multi-turn -- Reply in the same thread to continue a conversation"""

DIGEST_ENABLED = True
DIGEST_HOUR = 8  # Send digest at 8am local time
DIGEST_LAST_SENT_FILE = CONFIG_DIR / "digest_last_sent.txt"

CANCEL_FILE = CONFIG_DIR / "cancel.txt"

RATE_LIMIT_PER_HOUR = 20  # Max Claude invocations per hour
RATE_LIMIT_FILE = CONFIG_DIR / "rate_limit.json"  # Tracks invocation timestamps

SUMMARY_ENABLED = True
SUMMARY_HOUR = 22  # Send work summary at 10pm local time
SUMMARY_LAST_SENT_FILE = CONFIG_DIR / "summary_last_sent.txt"

# Slack MCP
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
SLACK_STATE_FILE = CONFIG_DIR / "slack_agent_state.json"
MCP_URL = "https://mcp.slack.com/mcp"
AGENT_PREFIX = ":robot_face:"
BUSINESS_HOURS_START = int(os.environ.get("CLAUDE_REMOTE_BIZ_START", "8"))
BUSINESS_HOURS_END = int(os.environ.get("CLAUDE_REMOTE_BIZ_END", "22"))
BUSINESS_HOURS_ONLY = os.environ.get("CLAUDE_REMOTE_BIZ_ONLY", "false").lower() == "true"
SLACK_CHANNEL_NAME = os.environ.get("CLAUDE_REMOTE_SLACK_CHANNEL", "zhengli-agent")
SLACK_USER_ID = os.environ.get("CLAUDE_REMOTE_SLACK_USER_ID", "")
SLACK_NOTIFY_THRESHOLD = int(os.environ.get("CLAUDE_REMOTE_NOTIFY_THRESHOLD", "30"))  # seconds

# Module-level state (set in run_bridge)
_startup_time: Optional[datetime] = None
_messages_processed: int = 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("claude-remote")


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every emit."""
    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_logging(foreground: bool = False):
    log.handlers.clear()
    log.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = _FlushFileHandler(LOG_FILE, mode="a")
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)
    log.addHandler(fh)
    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(formatter)
        log.addHandler(sh)


# ---------------------------------------------------------------------------
# Shared Utilities
# ---------------------------------------------------------------------------


def is_business_hours() -> bool:
    """Check if we are within business hours (for Slack gating)."""
    if not BUSINESS_HOURS_ONLY:
        return True
    hour = datetime.now().hour
    return BUSINESS_HOURS_START <= hour < BUSINESS_HOURS_END


# ---------------------------------------------------------------------------
# Gmail Authentication
# ---------------------------------------------------------------------------


def authenticate():
    """Load or create OAuth credentials. Returns a Gmail API service."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise FileNotFoundError(
                    f"OAuth client secret not found at {CLIENT_SECRET}. "
                    "Run setup.sh first."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_SECRET), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        os.chmod(TOKEN_FILE, 0o600)

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Gmail Client Functions
# ---------------------------------------------------------------------------


def get_my_email(service) -> str:
    """Get the authenticated user's email address."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def search_messages(service, query: str) -> list:
    """Search Gmail and return list of {id, threadId}."""
    results = (
        service.users().messages().list(userId="me", q=query).execute()
    )
    return results.get("messages", [])


def get_message(service, msg_id: str) -> dict:
    """Fetch a full message and extract useful fields."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )

    headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}

    # Extract plain text body and attachments
    body = _extract_body(msg["payload"])
    attachment_metas = _extract_attachments(msg["payload"])

    # Internal date is epoch ms
    internal_date = int(msg.get("internalDate", 0))

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "internal_date_ms": internal_date,
        "body": body,
        "attachments": attachment_metas,
        "label_ids": msg.get("labelIds", []),
    }


def _extract_body(payload: dict) -> str:
    """Recursively extract plain text from a message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    # Fallback: decode body data if present at top level
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    return ""


def strip_quoted_reply(text: str) -> str:
    """Strip Gmail/Outlook quoted reply text from an email body."""
    # Gmail-style: "On ... wrote:" -- may span lines and have blank lines before it
    match = re.search(r'\n\s*On .+?wrote:\s*$', text, re.DOTALL | re.MULTILINE)
    if match:
        text = text[:match.start()]
    # Outlook-style
    match = re.search(r'\n\s*From: .+\n', text)
    if match:
        text = text[:match.start()]
    # Forwarded message blocks
    match = re.search(r'\n-+ ?Forwarded message', text)
    if match:
        text = text[:match.start()]
    # Strip trailing > quoted lines
    lines = text.rstrip().splitlines()
    while lines and lines[-1].lstrip().startswith(">"):
        lines.pop()
    return "\n".join(lines).strip()


def strip_claude_prefix(text: str) -> str:
    """Remove the 'cc' subject prefix if present (case-insensitive)."""
    return re.sub(r"^cc\b\s*", "", text, flags=re.IGNORECASE)


def generate_subject(body: str, max_len: int = 50) -> str:
    """Generate a short subject line from the user's message."""
    first_line = body.split("\n")[0].strip()
    first_line = re.sub(r'^cc\b\s*', '', first_line, flags=re.IGNORECASE)
    if len(first_line) > max_len:
        first_line = first_line[:max_len].rsplit(" ", 1)[0] + "..."
    return f"cc {first_line}" if first_line else "cc conversation"


def _extract_attachments(payload: dict) -> list:
    """Extract attachment metadata (filename, size, id) from a MIME payload."""
    attachments = []
    for part in payload.get("parts", []):
        filename = part.get("filename")
        if filename:
            body = part.get("body", {})
            attachments.append({
                "filename": filename,
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
                "attachment_id": body.get("attachmentId"),
                "data": body.get("data"),
            })
        attachments.extend(_extract_attachments(part))
    return attachments


def download_attachments(service, msg_id: str, attachment_metas: list) -> list:
    """Download attachments to disk and return list of file paths."""
    if not attachment_metas:
        return []

    msg_dir = ATTACHMENTS_DIR / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    paths = []

    for att in attachment_metas:
        if att["size"] > MAX_ATTACHMENT_SIZE:
            log.warning("Skipping oversized attachment %s (%d bytes)", att["filename"], att["size"])
            continue

        if att["data"]:
            data = base64.urlsafe_b64decode(att["data"])
        elif att["attachment_id"]:
            resp = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=att["attachment_id"])
                .execute()
            )
            data = base64.urlsafe_b64decode(resp["data"])
        else:
            continue

        filepath = msg_dir / att["filename"]
        filepath.write_bytes(data)
        paths.append(filepath)
        log.info("Saved attachment %s (%d bytes)", att["filename"], len(data))

    return paths


def cleanup_old_attachments():
    """Remove attachment directories older than ATTACHMENT_MAX_AGE_HOURS."""
    if not ATTACHMENTS_DIR.exists():
        return
    cutoff = time.time() - ATTACHMENT_MAX_AGE_HOURS * 3600
    for child in ATTACHMENTS_DIR.iterdir():
        if child.is_dir() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def get_thread_history(service, thread_id: str) -> list:
    """Fetch all messages in a thread, sorted chronologically."""
    thread = (
        service.users()
        .threads()
        .get(userId="me", id=thread_id, format="full")
        .execute()
    )
    messages = []
    for msg in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
        sender_name, _ = email.utils.parseaddr(headers.get("from", ""))
        body = _extract_body(msg["payload"])
        messages.append({
            "from_name": sender_name,
            "date": headers.get("date", ""),
            "body": body.strip(),
        })
    return messages


def build_thread_context(thread_messages: list) -> str:
    """Format thread history as a conversation for Claude."""
    parts = []
    for msg in thread_messages:
        sender = msg["from_name"] or "Unknown"
        if sender == REPLY_SENDER_NAME:
            role = "You (Claude, in a previous reply)"
        else:
            role = "User"
        body = msg["body"]
        if body:
            parts.append(f"[{role} -- {msg['date']}]\n{body}")
    return "\n\n---\n\n".join(parts)


def format_html_reply(text: str) -> str:
    """Convert Claude's markdown output to email-safe HTML."""
    import markdown
    html_body = markdown.markdown(
        text,
        extensions=["fenced_code", "tables", "nl2br"],
    )
    style = (
        "<style>"
        "pre { background: #f6f8fa; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; } "
        "code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 13px; font-family: 'SF Mono', Monaco, Consolas, monospace; } "
        "pre code { background: none; padding: 0; } "
        "table { border-collapse: collapse; margin: 8px 0; } "
        "th, td { border: 1px solid #ddd; padding: 6px 12px; text-align: left; } "
        "th { background: #f6f8fa; } "
        "blockquote { border-left: 3px solid #ddd; margin: 8px 0; padding: 4px 12px; color: #555; }"
        "</style>"
    )
    return (
        "<html><body>\n"
        "<div style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
        "font-size: 14px; line-height: 1.6; color: #1a1a1a;\">\n"
        + html_body + "\n"
        "</div>\n"
        + style + "\n"
        "</body></html>"
    )


def send_reply(service, original_msg: dict, body_text: str, my_email: str, override_subject: str = None):
    """Reply to a message in the same thread with a distinct sender name."""
    if override_subject:
        subject = override_subject
    else:
        subject = original_msg["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"

    # Build references chain for proper threading
    references = original_msg.get("references", "")
    if original_msg.get("message_id"):
        if references:
            references += " " + original_msg["message_id"]
        else:
            references = original_msg["message_id"]

    # Use HTML if the body contains markdown-like content
    has_markdown = any(marker in body_text for marker in ['```', '# ', '**', '- ', '| '])

    if has_markdown:
        msg_mime = MIMEMultipart("alternative")
        msg_mime.attach(MIMEText(body_text, "plain"))
        msg_mime.attach(MIMEText(format_html_reply(body_text), "html"))
    else:
        msg_mime = MIMEText(body_text)

    msg_mime["from"] = f"{REPLY_SENDER_NAME} <{my_email}>"
    msg_mime["to"] = original_msg["from"]
    msg_mime["subject"] = subject
    msg_mime["In-Reply-To"] = original_msg.get("message_id", "")
    msg_mime["References"] = references

    raw = base64.urlsafe_b64encode(msg_mime.as_bytes()).decode("ascii")

    sent = (
        service.users()
        .messages()
        .send(
            userId="me",
            body={"raw": raw, "threadId": original_msg["thread_id"]},
        )
        .execute()
    )
    return sent


def mark_as_read(service, msg_id: str):
    """Remove UNREAD label from a message."""
    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


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
    session_id: Optional[str] = None,
    resume: bool = False,
    on_progress: Optional[object] = None,
    thread_id: Optional[str] = None,
) -> str:
    """Run claude -p as a subprocess, sending progress callbacks while waiting."""
    if session_id is None:
        session_id = str(uuid.uuid4())

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
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=CLAUDE_CWD,
        )
        elapsed = 0
        next_progress = PROGRESS_INTERVAL
        while proc.poll() is None:
            time.sleep(1)
            elapsed += 1
            if thread_id and _check_cancel(thread_id):
                proc.kill()
                proc.wait()
                log.info("Cancelled Claude for thread %s", thread_id)
                return "[Cancelled by user]\n\nThe task was cancelled. Send a new message to start fresh."
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
            if on_progress and elapsed >= next_progress:
                on_progress(elapsed)
                next_progress += PROGRESS_INTERVAL
        else:
            output = proc.stdout.read().strip()
            if proc.returncode != 0 and not output:
                stderr_text = proc.stderr.read().strip()
                output = (
                    f"[Claude exited with code {proc.returncode}]\n\n"
                    + (f"Error: {stderr_text}\n\n" if stderr_text else "")
                    + "Reply in this thread to retry, or start a new thread with cc prefix."
                )
            if not output:
                output = (
                    "[Claude returned empty output]\n\n"
                    "This sometimes happens with very short tasks. Try rephrasing your request."
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
        output = output[:MAX_RESPONSE_LEN] + "\n\n[truncated -- response exceeded 50K chars]"

    return output


def list_sessions(count: int = 10) -> str:
    """List recent Claude Code sessions by reading JSONL files from disk."""
    cwd_slug = CLAUDE_CWD.replace("/", "-").replace(".", "-")
    sessions_dir = CLAUDE_SESSIONS_DIR / cwd_slug
    if not sessions_dir.exists():
        return "No sessions found."

    entries = []
    for f in sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:count]:
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
                    first_msg = (c.get("text", "") if isinstance(c, dict) else str(c))[:80]
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        entries.append(f"{f.stem}\n  {mtime:%Y-%m-%d %H:%M}  {first_msg}")

    if not entries:
        return "No sessions found."
    header = "Recent Claude sessions (reply /resume <id> to continue one):\n"
    return header + "\n\n".join(entries)


def delete_claude_session(session_id: str):
    """Delete a Claude Code session file from disk."""
    cwd_slug = CLAUDE_CWD.replace("/", "-").replace(".", "-")
    session_file = CLAUDE_SESSIONS_DIR / cwd_slug / f"{session_id}.jsonl"
    try:
        session_file.unlink()
        log.info("Deleted Claude session file: %s", session_id[:8])
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("Failed to delete session file %s: %s", session_id[:8], e)


# ---------------------------------------------------------------------------
# Gmail State Management
# ---------------------------------------------------------------------------


def load_processed_ids() -> set:
    """Load processed message IDs from file."""
    if not GMAIL_PROCESSED_FILE.exists():
        return set()
    return set(GMAIL_PROCESSED_FILE.read_text().strip().splitlines())


def save_processed_id(msg_id: str):
    """Append a processed message ID to file."""
    with open(GMAIL_PROCESSED_FILE, "a") as f:
        f.write(msg_id + "\n")


def load_thread_sessions() -> dict:
    """Load thread->session mapping from JSON file."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_thread_sessions(sessions: dict):
    """Save thread->session mapping to JSON file."""
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2))


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def _check_rate_limit() -> tuple:
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
# Daily Digest
# ---------------------------------------------------------------------------


CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"


def _skill_exists(skill_name: str) -> bool:
    """Check if a Claude Code skill is installed."""
    return (CLAUDE_SKILLS_DIR / skill_name).is_dir()


def _invoke_skill(skill_name: str) -> str:
    """Invoke a Claude Code skill (e.g. /brief, /summary) via claude -p."""
    return invoke_claude(f"/{skill_name}", str(uuid.uuid4()), resume=False)


def _send_scheduled_email(service, my_email: str, subject: str, body: str):
    """Send a styled HTML email to the user."""
    html = format_html_reply(body)
    msg_mime = MIMEMultipart("alternative")
    msg_mime.attach(MIMEText(body, "plain"))
    msg_mime.attach(MIMEText(html, "html"))
    msg_mime["from"] = f"ClaudeRemote <{my_email}>"
    msg_mime["to"] = my_email
    msg_mime["subject"] = subject
    raw = base64.urlsafe_b64encode(msg_mime.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_daily_digest(service, my_email: str, thread_sessions: dict, processed_count: int):
    """Send a morning briefing by invoking the /brief skill."""
    now = datetime.now()

    if DIGEST_LAST_SENT_FILE.exists():
        last_sent = DIGEST_LAST_SENT_FILE.read_text().strip()
        if last_sent == now.strftime("%Y-%m-%d"):
            return

    if not _skill_exists("brief"):
        log.debug("Skipping morning briefing: /brief skill not installed")
        return

    log.info("Generating morning briefing via /brief skill")
    body = _invoke_skill("brief")

    if not body or body.startswith("["):
        log.warning("Morning briefing failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Morning Briefing -- {now.strftime('%b %d')}", body)
    DIGEST_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent morning briefing")


def _maybe_send_digest(service, my_email, thread_sessions, processed_count):
    """Send digest if it is the right hour, not weekend, and not sent today."""
    if not DIGEST_ENABLED:
        return
    now = datetime.now()
    if now.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
        return
    if now.hour != DIGEST_HOUR:
        return
    send_daily_digest(service, my_email, thread_sessions, processed_count)


# ---------------------------------------------------------------------------
# Daily Work Summary
# ---------------------------------------------------------------------------


def send_work_summary(service, my_email: str):
    """Send an end-of-day work summary by invoking the /summary skill."""
    now = datetime.now()

    if SUMMARY_LAST_SENT_FILE.exists():
        last_sent = SUMMARY_LAST_SENT_FILE.read_text().strip()
        if last_sent == now.strftime("%Y-%m-%d"):
            return

    if not _skill_exists("summary"):
        log.debug("Skipping work summary: /summary skill not installed")
        return

    log.info("Generating work summary via /summary skill")
    body = _invoke_skill("summary")

    if not body or body.startswith("["):
        log.warning("Work summary failed: %s", body[:100] if body else "empty")
        return

    _send_scheduled_email(service, my_email,
                          f"[ClaudeRemote] Work Summary -- {now.strftime('%b %d')}", body)
    SUMMARY_LAST_SENT_FILE.write_text(now.strftime("%Y-%m-%d"))
    log.info("Sent work summary")


def _maybe_send_summary(service, my_email):
    """Send work summary if it is the right hour, not weekend, and not sent today."""
    if not SUMMARY_ENABLED:
        return
    now = datetime.now()
    if now.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
        return
    if now.hour != SUMMARY_HOUR:
        return
    send_work_summary(service, my_email)


# ---------------------------------------------------------------------------
# Gmail Poll Cycle
# ---------------------------------------------------------------------------


def gmail_poll_cycle(
    service,
    my_email: str,
    processed_ids: set,
    thread_sessions: dict,
    startup_time_ms: int,
):
    """Single Gmail poll iteration."""
    cleanup_old_attachments()
    _maybe_send_digest(service, my_email, thread_sessions, len(processed_ids))
    _maybe_send_summary(service, my_email)

    query = f'subject:"{SUBJECT_PREFIX}" newer_than:1d is:unread'
    messages = search_messages(service, query)

    if not messages:
        return

    log.info("Found %d unread messages matching query", len(messages))

    for msg_stub in messages:
        msg_id = msg_stub["id"]

        if msg_id in processed_ids:
            continue

        # Fetch full message
        msg = get_message(service, msg_id)

        # Sender check: only process emails from ourselves, skip our own replies
        sender_name, sender_email = email.utils.parseaddr(msg["from"])
        if sender_email.lower() != my_email.lower():
            log.info("Skipping message from %s (not self)", sender_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if sender_name == REPLY_SENDER_NAME:
            log.info("Skipping own reply %s (from %s)", msg_id, REPLY_SENDER_NAME)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        # Download attachments (if any)
        attachment_paths = download_attachments(service, msg_id, msg.get("attachments", []))

        # Extract the user's question, stripping Gmail quoted reply text
        body = strip_quoted_reply(msg["body"].strip())
        body = strip_claude_prefix(body)
        subject = strip_claude_prefix(msg["subject"])
        if not body and subject:
            body = subject
        if not body and not attachment_paths:
            log.info("Skipping message %s with empty body and no attachments", msg_id)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        thread_id = msg["thread_id"]
        log.info("Processing message %s in thread %s: %.80s", msg_id, thread_id, body)

        # Built-in commands: /help, /status, /cancel, /sessions, /resume <id>
        if body.lower() == "/help":
            response = HELP_TEXT
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/status":
            global _messages_processed
            uptime = datetime.now(timezone.utc) - _startup_time if _startup_time else None
            if uptime:
                hours, remainder = divmod(int(uptime.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = f"{hours}h {minutes}m {seconds}s"
            else:
                uptime_str = "unknown"
            current_session = thread_sessions.get(thread_id, "none")
            response = (
                f"ClaudeRemote Status\n"
                f"---\n"
                f"Uptime: {uptime_str}\n"
                f"Messages processed: {_messages_processed}\n"
                f"Active threads: {len(thread_sessions)}\n"
                f"This thread's session: {current_session}\n"
                f"PID: {os.getpid()}"
            )
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/cancel":
            with open(CANCEL_FILE, "a") as f:
                f.write(thread_id + "\n")
            response = "Cancel requested. If a task is running in this thread, it will be stopped."
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/summary":
            if not _skill_exists("summary"):
                response = "The /summary skill is not installed. Create it at ~/.claude/skills/summary/"
                send_reply(service, msg, response, my_email)
                mark_as_read(service, msg_id)
                processed_ids.add(msg_id)
                save_processed_id(msg_id)
                continue
            log.info("Manual work summary requested via /summary skill")
            response = _invoke_skill("summary")
            summary_sent = send_reply(service, msg, response, my_email)
            if summary_sent and "id" in summary_sent:
                processed_ids.add(summary_sent["id"])
                save_processed_id(summary_sent["id"])
            # Don't mark auto-summary as sent - manual /summary is on-demand
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/brief":
            if not _skill_exists("brief"):
                response = "The /brief skill is not installed. Create it at ~/.claude/skills/brief/"
                send_reply(service, msg, response, my_email)
                mark_as_read(service, msg_id)
                processed_ids.add(msg_id)
                save_processed_id(msg_id)
                continue
            log.info("Manual morning brief requested via /brief skill")
            response = _invoke_skill("brief")
            brief_sent = send_reply(service, msg, response, my_email)
            if brief_sent and "id" in brief_sent:
                processed_ids.add(brief_sent["id"])
                save_processed_id(brief_sent["id"])
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower() == "/sessions":
            response = list_sessions()
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue
        if body.lower().startswith("/resume "):
            resume_id = body.split(None, 1)[1].strip()
            session_id = resume_id
            thread_sessions[thread_id] = session_id
            save_thread_sessions(thread_sessions)
            response = invoke_claude(
                "The user just resumed this session from their phone via email. "
                "Briefly summarize what you were working on, and ask how to proceed.",
                session_id, resume=True,
            )
            send_reply(service, msg, response, my_email)
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        # Determine session: resume existing or start new
        resume = thread_id in thread_sessions
        if resume:
            session_id = thread_sessions[thread_id]
        else:
            session_id = str(uuid.uuid4())
        log.info("Session for thread %s: session=%s, resume=%s", thread_id, session_id[:8], resume)

        # Build attachment preamble
        att_preamble = ""
        if attachment_paths:
            file_list = "\n".join(f"  - {p.name}: {p}" for p in attachment_paths)
            att_preamble = f"Attached files (read and analyze these):\n{file_list}\n\n"

        # Build the prompt: for resumed sessions just send the latest message,
        # for fresh sessions include the full thread for context
        if resume:
            prompt = att_preamble + body
        else:
            thread_messages = get_thread_history(service, thread_id)
            if len(thread_messages) > 1:
                prompt = (
                    att_preamble
                    + "Here is the email conversation so far:\n\n"
                    + build_thread_context(thread_messages)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = att_preamble + body

        # Progress callback: send "still working" emails while Claude runs
        def on_progress(elapsed_secs):
            mins = elapsed_secs // 60
            try:
                progress_sent = send_reply(service, msg, f"[Still working... ({mins}m elapsed)]", my_email)
                if progress_sent and "id" in progress_sent:
                    processed_ids.add(progress_sent["id"])
                    save_processed_id(progress_sent["id"])
                log.info("Sent progress update at %ds for message %s", elapsed_secs, msg_id)
            except Exception:
                log.exception("Failed to send progress email")

        # Rate limit check
        allowed, remaining = _check_rate_limit()
        if not allowed:
            response = (
                "[Rate limit reached]\n\n"
                f"You've used {RATE_LIMIT_PER_HOUR} Claude invocations in the last hour.\n"
                "Wait a bit and try again, or adjust RATE_LIMIT_PER_HOUR in bridge.py."
            )
            rate_sent = send_reply(service, msg, response, my_email)
            if rate_sent and "id" in rate_sent:
                processed_ids.add(rate_sent["id"])
                save_processed_id(rate_sent["id"])
            mark_as_read(service, msg_id)
            processed_ids.add(msg_id)
            save_processed_id(msg_id)
            continue

        log.info("Rate limit: %d/%d remaining", remaining, RATE_LIMIT_PER_HOUR)

        # Invoke Claude
        response = invoke_claude(prompt, session_id, resume=resume, on_progress=on_progress, thread_id=thread_id)
        _record_invocation()

        # If resume failed, retry with full thread context on a fresh session
        if resume and "[Claude exited with code" in response:
            log.warning("Resume failed for session %s, retrying fresh with thread context", session_id[:8])
            session_id = str(uuid.uuid4())
            thread_messages = get_thread_history(service, thread_id)
            if len(thread_messages) > 1:
                prompt = (
                    att_preamble
                    + "Here is the email conversation so far:\n\n"
                    + build_thread_context(thread_messages)
                    + "\n\n---\n\nPlease respond to the latest message above."
                )
            else:
                prompt = att_preamble + body
            response = invoke_claude(prompt, session_id, resume=False, on_progress=on_progress, thread_id=thread_id)
            _record_invocation()

        # Reply in thread -- override subject on first reply in new thread
        try:
            if not resume:
                new_subject = generate_subject(body)
                sent = send_reply(service, msg, response, my_email, override_subject=f"Re: {new_subject}")
            else:
                sent = send_reply(service, msg, response, my_email)
            # Track sent reply ID so we never re-process our own replies
            if sent and "id" in sent:
                processed_ids.add(sent["id"])
                save_processed_id(sent["id"])
            log.info("Replied to message %s (session=%s)", msg_id, session_id[:8])
        except Exception:
            log.exception("Failed to send reply for message %s", msg_id)

        # Update state
        mark_as_read(service, msg_id)
        processed_ids.add(msg_id)
        save_processed_id(msg_id)
        thread_sessions[thread_id] = session_id
        save_thread_sessions(thread_sessions)
        _messages_processed += 1


# ---------------------------------------------------------------------------
# Slack MCP Token Management
# ---------------------------------------------------------------------------


def _load_credentials() -> dict:
    """Load the Claude Code credentials file."""
    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except (FileNotFoundError, PermissionError) as e:
        log.error("Credentials file not found: %s (%s)", CREDENTIALS_FILE, e)
        return {}


def _find_slack_token_entry(creds: dict) -> Optional[dict]:
    """Find the active Slack MCP OAuth entry."""
    mcp_oauth = creds.get("mcpOAuth", {})
    for key, entry in mcp_oauth.items():
        if "slack" not in key:
            continue
        server_url = entry.get("serverUrl", "")
        access_token = entry.get("accessToken", "")
        if "mcp.slack.com/mcp" in server_url and access_token:
            return entry
    return None


SLACK_TOKEN_FILE = CONFIG_DIR / "slack_mcp_token.json"
SLACK_MCP_CLIENT_ID = "1601185624273.8899143856786"
# Refresh token when it has less than this many hours left (0 = disabled)
SLACK_TOKEN_REFRESH_HOURS = int(os.environ.get("CLAUDE_REMOTE_SLACK_REFRESH_HOURS", "2"))


def _notify_refresh_failure(entry: dict, error_msg: str):
    """Send a Slack notification when token refresh fails."""
    token = entry.get("accessToken")
    if not token:
        return
    try:
        state = load_slack_state()
        channel_id = state.get("channel_id")
        if channel_id:
            msg = (
                f"{AGENT_PREFIX} :warning: {error_msg}\n"
                f"Run `cd ~/Projects/claude-remote && .venv/bin/python slack_oauth.py` to re-authenticate."
            )
            _mcp_call("slack_send_message", {
                "channel_id": channel_id,
                "message": msg,
            }, token)
            log.info("Sent token refresh failure notification to Slack")
    except Exception as e:
        log.warning("Failed to send refresh failure notification: %s", e)


def _refresh_slack_token(entry: dict) -> Optional[dict]:
    """Refresh the Slack OAuth token using the refresh token.

    Returns updated entry on success, None on failure.
    """
    refresh_token = entry.get("refreshToken")
    if not refresh_token:
        log.warning("No refresh token available for Slack token refresh")
        return None

    log.info("Refreshing Slack OAuth token...")
    try:
        data = urlencode({
            "client_id": SLACK_MCP_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode()

        req = URLRequest(
            "https://slack.com/api/oauth.v2.user.access",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        if not result.get("ok"):
            log.error("Slack token refresh failed: %s", result.get("error", result))
            _notify_refresh_failure(entry, f"Slack token refresh failed: {result.get('error', 'unknown')}")
            return None

        new_entry = {
            "serverUrl": entry.get("serverUrl", MCP_URL),
            "accessToken": result["access_token"],
            "refreshToken": result.get("refresh_token", refresh_token),
            "expiresAt": int((time.time() + result.get("expires_in", 43200)) * 1000),
        }

        SLACK_TOKEN_FILE.write_text(json.dumps(new_entry, indent=2))
        SLACK_TOKEN_FILE.chmod(0o600)

        expires_str = datetime.fromtimestamp(new_entry["expiresAt"] / 1000).isoformat()
        log.info("Slack token refreshed successfully, expires at %s", expires_str)
        return new_entry

    except Exception as e:
        log.error("Slack token refresh error: %s", e)
        _notify_refresh_failure(entry, f"Slack token refresh error: {e}")
        return None


def get_slack_token() -> Optional[str]:
    """Get the Slack OAuth access token.

    Checks in order:
    1. Bridge's own token file (~/.claude-remote/slack_mcp_token.json)
    2. Claude Code credentials file (~/.claude/.credentials.json)

    Auto-refreshes if SLACK_TOKEN_REFRESH_HOURS > 0 and token is near expiry.
    """
    # Try bridge's own persistent token first
    entry = None
    try:
        entry = json.loads(SLACK_TOKEN_FILE.read_text())
    except (FileNotFoundError, PermissionError):
        pass

    # Fall back to Claude Code credentials
    if not entry or not entry.get("accessToken"):
        creds = _load_credentials()
        entry = _find_slack_token_entry(creds)

    if not entry:
        log.error("No active Slack MCP token found in credentials")
        return None

    # Check expiry and auto-refresh
    expires_at = entry.get("expiresAt", 0)
    now_ms = int(time.time() * 1000)

    if expires_at:
        remaining_hours = (expires_at - now_ms) / (1000 * 3600)

        if now_ms > expires_at:
            # Token expired — try refresh before giving up
            if SLACK_TOKEN_REFRESH_HOURS > 0:
                refreshed = _refresh_slack_token(entry)
                if refreshed:
                    return refreshed["accessToken"]
            log.warning(
                "Slack token expired at %s. Run: .venv/bin/python slack_oauth.py",
                datetime.fromtimestamp(expires_at / 1000).isoformat(),
            )
            return None

        if SLACK_TOKEN_REFRESH_HOURS > 0 and remaining_hours < SLACK_TOKEN_REFRESH_HOURS:
            log.info("Slack token expires in %.1f hours, refreshing proactively", remaining_hours)
            refreshed = _refresh_slack_token(entry)
            if refreshed:
                return refreshed["accessToken"]
            # If refresh fails, continue with current token

    return entry["accessToken"]


# ---------------------------------------------------------------------------
# Slack MCP API (direct HTTP, no LLM)
# ---------------------------------------------------------------------------


def _mcp_call(tool_name: str, arguments: dict, token: str) -> Optional[dict]:
    """Call a Slack MCP tool via HTTP JSON-RPC."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }).encode()

    req = URLRequest(
        MCP_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            if "error" in body:
                log.error("MCP error: %s", body["error"])
                return None
            return body.get("result", body)
    except HTTPError as e:
        log.error("MCP HTTP error %d: %s", e.code, e.read().decode()[:500])
        return None
    except (URLError, TimeoutError) as e:
        log.error("MCP connection error: %s", e)
        return None


def _extract_mcp_text(result: dict) -> Optional[str]:
    """Extract the text payload from an MCP tool result.

    MCP returns: {"content": [{"type": "text", "text": "<json-string>"}]}
    The text field is itself a JSON string like {"messages": "..."}.
    We unwrap both layers and return the inner messages string.
    """
    if not isinstance(result, dict):
        return None
    # Extract text from content array
    raw_text = None
    if "content" in result:
        for item in result["content"]:
            if item.get("type") == "text":
                raw_text = item["text"]
                break
    if raw_text is None:
        return None
    # The text is a JSON string - parse it and extract the messages field
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and "messages" in parsed:
            return parsed["messages"]
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: return raw text as-is
    return raw_text


def mcp_read_channel(token: str, channel_id: str, oldest: str = "", limit: int = 50) -> Optional[str]:
    """Read channel messages via MCP."""
    args = {"channel_id": channel_id, "limit": limit}
    if oldest:
        args["oldest"] = oldest
    result = _mcp_call("slack_read_channel", args, token)
    if result is None:
        return None
    return _extract_mcp_text(result)


def mcp_read_thread(token: str, channel_id: str, message_ts: str) -> Optional[str]:
    """Read thread replies via MCP."""
    result = _mcp_call("slack_read_thread", {
        "channel_id": channel_id,
        "message_ts": message_ts,
    }, token)
    if result is None:
        return None
    return _extract_mcp_text(result)


def mcp_send_message(token: str, channel_id: str, thread_ts: str, message: str) -> bool:
    """Send a message via MCP."""
    result = _mcp_call("slack_send_message", {
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "message": message,
    }, token)
    return result is not None


def mcp_add_reaction(token: str, channel_id: str, message_ts: str, emoji: str = "eyes") -> bool:
    """Add an emoji reaction to a message to acknowledge receipt.

    Uses Slack Web API directly since the MCP server doesn't expose
    reactions.add. The MCP OAuth token (xoxe.xoxp-*) works with the
    Slack API.
    """
    payload = json.dumps({
        "channel": channel_id,
        "timestamp": message_ts,
        "name": emoji,
    }).encode()
    req = URLRequest(
        "https://slack.com/api/reactions.add",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if not body.get("ok"):
                log.warning("reactions.add failed: %s", body.get("error", "unknown"))
                return False
            return True
    except (HTTPError, URLError) as e:
        log.warning("reactions.add error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Slack Message Parsing
# ---------------------------------------------------------------------------


def parse_new_messages(channel_text: str, since_ts: str) -> list:
    """Parse channel read output for new messages after since_ts."""
    messages = []
    current = {}
    for line in channel_text.splitlines():
        if line.startswith("=== Message from "):
            if current:
                messages.append(current)
            current = {"user": "", "ts": "", "text_lines": []}
            # Extract user
            parts = line.split("(")
            if len(parts) > 1:
                current["user"] = parts[1].split(")")[0]
        elif line.startswith("Message TS: "):
            current["ts"] = line.replace("Message TS: ", "").strip()
        elif current:
            current["text_lines"].append(line)

    if current:
        messages.append(current)

    # Filter to messages newer than since_ts, join text lines
    result = []
    for msg in messages:
        ts = msg.get("ts", "")
        if not ts:
            continue
        try:
            if float(ts) <= float(since_ts):
                continue
        except ValueError:
            continue
        text = "\n".join(msg["text_lines"]).strip()
        result.append({"user": msg["user"], "ts": ts, "text": text})

    return result


def parse_thread_replies(thread_text: str, since_ts: str) -> list:
    """Parse thread read output for new replies after since_ts."""
    replies = []
    current = {}
    in_replies = False
    for line in thread_text.splitlines():
        if "THREAD REPLIES" in line:
            in_replies = True
            continue
        if not in_replies:
            continue
        if line.startswith("--- Reply "):
            if current:
                replies.append(current)
            current = {"user": "", "ts": "", "text_lines": []}
        elif line.startswith("From: "):
            parts = line.split("(")
            if len(parts) > 1:
                current["user"] = parts[1].split(")")[0]
        elif line.startswith("Message TS: "):
            current["ts"] = line.replace("Message TS: ", "").strip()
        elif current:
            current["text_lines"].append(line)

    if current:
        replies.append(current)

    result = []
    for reply in replies:
        ts = reply.get("ts", "")
        if not ts:
            continue
        try:
            if float(ts) <= float(since_ts):
                continue
        except ValueError:
            continue
        text = "\n".join(reply["text_lines"]).strip()
        result.append({"user": reply["user"], "ts": ts, "text": text})

    return result


def should_process(msg: dict) -> bool:
    """Check if a Slack message should be processed."""
    text = msg.get("text", "")
    # Skip agent output - check multiple markers to prevent self-reply loops
    if AGENT_PREFIX in text:
        return False
    if "Sent using" in text and "Claude" in text:
        return False
    if SLACK_USER_ID and f"<@{SLACK_USER_ID}" in text:
        return False
    if "has joined the channel" in text:
        return False
    if not text.strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Slack State Management
# ---------------------------------------------------------------------------


def load_slack_state() -> dict:
    """Load Slack agent state from file."""
    if not SLACK_STATE_FILE.exists():
        return {
            "channel_id": "",
            "channel_name": SLACK_CHANNEL_NAME,
            "last_checked_ts": "",
            "active_threads": {},
        }
    try:
        return json.loads(SLACK_STATE_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"channel_id": "", "channel_name": SLACK_CHANNEL_NAME, "last_checked_ts": "", "active_threads": {}}


def save_slack_state(state: dict):
    """Save Slack agent state to file."""
    SLACK_STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Slack Poll Cycle
# ---------------------------------------------------------------------------


def slack_poll_cycle(token: str, state: dict):
    """Single Slack poll iteration."""
    global _messages_processed
    channel_id = state["channel_id"]
    last_ts = state["last_checked_ts"]
    active_threads = state.get("active_threads", {})

    # 1. Check for new top-level messages
    channel_text = mcp_read_channel(token, channel_id, oldest=last_ts)
    if channel_text is None:
        log.warning("Failed to read channel")
        return

    new_top_level = parse_new_messages(channel_text, last_ts)

    # 2. Check active threads for new replies
    new_thread_replies = []
    thread_failures = state.get("thread_failures", {})
    for thread_ts, last_reply_ts in list(active_threads.items()):
        thread_text = mcp_read_thread(token, channel_id, thread_ts)
        if thread_text is None:
            count = thread_failures.get(thread_ts, 0) + 1
            thread_failures[thread_ts] = count
            if count >= 3:
                log.info("Removing stale thread %s after %d failures", thread_ts, count)
                session_id = state.get("thread_sessions", {}).pop(thread_ts, None)
                if session_id:
                    delete_claude_session(session_id)
                active_threads.pop(thread_ts, None)
                thread_failures.pop(thread_ts, None)
            else:
                log.info("Thread %s read failed (%d/3)", thread_ts, count)
            continue
        # Reset failure count on success
        thread_failures.pop(thread_ts, None)
        replies = parse_thread_replies(thread_text, last_reply_ts)
        for reply in replies:
            reply["thread_ts"] = thread_ts
            reply["thread_context"] = thread_text
        new_thread_replies.extend(replies)

    # 3. Filter
    to_process = []
    for msg in new_top_level:
        if should_process(msg):
            msg["is_thread_reply"] = False
            to_process.append(msg)

    for reply in new_thread_replies:
        if should_process(reply):
            reply["is_thread_reply"] = True
            to_process.append(reply)

    # Sort chronologically
    to_process.sort(key=lambda m: float(m.get("ts", "0")))

    if not to_process:
        # Update last_checked_ts even when nothing to process
        newest_ts = last_ts
        for msg in new_top_level:
            if float(msg.get("ts", "0")) > float(newest_ts):
                newest_ts = msg["ts"]
        if newest_ts != last_ts:
            state["last_checked_ts"] = newest_ts
        # Always save if thread_failures changed or timestamp updated
        state["thread_failures"] = thread_failures
        save_slack_state(state)
        return

    log.info(
        "Found %d messages to process (%d top-level, %d thread replies)",
        len(to_process),
        sum(1 for m in to_process if not m["is_thread_reply"]),
        sum(1 for m in to_process if m["is_thread_reply"]),
    )

    # 4. Process each message
    replied = 0
    for msg in to_process:
        # Rate limit
        allowed, remaining = _check_rate_limit()
        if not allowed:
            log.warning("Rate limit reached, skipping remaining messages")
            mcp_send_message(
                token, channel_id,
                msg.get("thread_ts", msg["ts"]),
                f"{AGENT_PREFIX} Rate limit reached (20/hour). Try again later.",
            )
            break

        text = msg["text"]
        thread_ts = msg.get("thread_ts", msg["ts"]) if msg["is_thread_reply"] else msg["ts"]

        # Acknowledge receipt with eyes emoji
        mcp_add_reaction(token, channel_id, msg["ts"], "eyes")

        # Determine session: resume existing or start new
        thread_sessions = state.get("thread_sessions", {})
        resume = thread_ts in thread_sessions
        if resume:
            session_id = thread_sessions[thread_ts]
        else:
            session_id = str(uuid.uuid4())
        log.info("Processing (session=%s, resume=%s): %.80s", session_id[:8], resume, text)

        # Build prompt for Claude
        if resume:
            # Resumed session already has context; just send the new message
            prompt = text
        elif msg["is_thread_reply"]:
            thread_context = msg.get("thread_context", "")
            prompt = (
                f"You are an AI assistant replying in a Slack thread. "
                f"Here is the full thread context:\n\n{thread_context}\n\n"
                f"The latest message is: {text}\n\n"
                f"Respond to this latest message. Use Slack mrkdwn formatting "
                f"(*bold*, _italic_, `code`). Be concise and helpful. "
                f"Use all available MCP tools if needed."
            )
        else:
            prompt = (
                f"You are an AI assistant responding to a Slack message. "
                f"The message is: {text}\n\n"
                f"Process this as a task. Use all available MCP tools "
                f"(Slack, Google Workspace, Glean, etc.) as needed. "
                f"Use Slack mrkdwn formatting (*bold*, _italic_, `code`). "
                f"Be concise and helpful."
            )

        _record_invocation()
        start_time = time.time()
        response = invoke_claude(prompt, session_id, resume=resume, thread_id=thread_ts)

        # If resume failed, retry fresh with full thread context
        if resume and "[Claude exited with code" in response:
            log.warning("Resume failed for Slack session %s, retrying fresh", session_id[:8])
            session_id = str(uuid.uuid4())
            thread_context = msg.get("thread_context", "")
            if thread_context:
                prompt = (
                    f"You are an AI assistant replying in a Slack thread. "
                    f"Here is the full thread context:\n\n{thread_context}\n\n"
                    f"The latest message is: {text}\n\n"
                    f"Respond to this latest message. Use Slack mrkdwn formatting "
                    f"(*bold*, _italic_, `code`). Be concise and helpful. "
                    f"Use all available MCP tools if needed."
                )
            else:
                prompt = (
                    f"You are an AI assistant responding to a Slack message. "
                    f"The message is: {text}\n\n"
                    f"Process this as a task. Use all available MCP tools "
                    f"(Slack, Google Workspace, Glean, etc.) as needed. "
                    f"Use Slack mrkdwn formatting (*bold*, _italic_, `code`). "
                    f"Be concise and helpful."
                )
            response = invoke_claude(prompt, session_id, resume=False, thread_id=thread_ts)
        elapsed = time.time() - start_time

        # Post reply - @mention user if task took longer than threshold
        if SLACK_USER_ID and elapsed >= SLACK_NOTIFY_THRESHOLD:
            reply_text = f"{AGENT_PREFIX} <@{SLACK_USER_ID}> {response}"
        else:
            reply_text = f"{AGENT_PREFIX} {response}"
        success = mcp_send_message(token, channel_id, thread_ts, reply_text)

        if success:
            replied += 1
            log.info("Replied to message %s (session=%s)", msg["ts"], session_id[:8])
            # Mark done with checkmark
            mcp_add_reaction(token, channel_id, msg["ts"], "white_check_mark")
            # Track thread and session
            active_threads[thread_ts] = msg["ts"]
            thread_sessions[thread_ts] = session_id
            state["thread_sessions"] = thread_sessions
        else:
            log.error("Failed to reply to message %s", msg["ts"])

        _messages_processed += 1

    # 5. Update state
    newest_top_ts = last_ts
    for msg in new_top_level:
        if float(msg.get("ts", "0")) > float(newest_top_ts):
            newest_top_ts = msg["ts"]
    if newest_top_ts != last_ts:
        state["last_checked_ts"] = newest_top_ts

    # Update thread tracking
    for msg in to_process:
        if msg["is_thread_reply"]:
            thread_ts = msg["thread_ts"]
            active_threads[thread_ts] = msg["ts"]
        else:
            active_threads[msg["ts"]] = msg["ts"]

    # Prune stale threads (>7 days)
    cutoff = time.time() - 7 * 86400
    pruned_threads = {
        k for k, v in active_threads.items()
        if float(v) <= cutoff
    }
    active_threads = {
        k: v for k, v in active_threads.items()
        if k not in pruned_threads
    }
    thread_sessions = state.get("thread_sessions", {})
    for thread_ts in pruned_threads:
        session_id = thread_sessions.pop(thread_ts, None)
        if session_id:
            delete_claude_session(session_id)
    thread_sessions = {
        k: v for k, v in thread_sessions.items()
        if k in active_threads
    }

    state["active_threads"] = active_threads
    state["thread_sessions"] = thread_sessions
    state["thread_failures"] = thread_failures
    save_slack_state(state)

    log.info("Processed %d, replied to %d", len(to_process), replied)


# ---------------------------------------------------------------------------
# Unified Run Bridge
# ---------------------------------------------------------------------------


def run_bridge(foreground: bool = False, gmail_enabled: bool = True, slack_enabled: bool = False):
    """Main loop: poll Gmail and/or Slack, process messages, reply."""
    global _startup_time, _messages_processed
    setup_logging(foreground=foreground)
    _messages_processed = 0
    _startup_time = datetime.now(timezone.utc)

    transports = []
    if gmail_enabled:
        transports.append("Gmail")
    if slack_enabled:
        transports.append("Slack")
    log.info("Starting ClaudeRemote (%s)", " + ".join(transports))
    if slack_enabled and BUSINESS_HOURS_ONLY:
        log.info("Slack business hours: %d:00 - %d:00", BUSINESS_HOURS_START, BUSINESS_HOURS_END)

    # --- Gmail init ---
    service = None
    my_email = None
    processed_ids = None
    thread_sessions = None
    startup_time_ms = None

    if gmail_enabled:
        try:
            service = authenticate()
            my_email = get_my_email(service)
            log.info("Gmail authenticated as %s", my_email)
            processed_ids = load_processed_ids()
            thread_sessions = load_thread_sessions()
            startup_time_ms = int(time.time() * 1000)
            log.info("Loaded %d processed IDs, %d thread sessions", len(processed_ids), len(thread_sessions))
        except Exception as e:
            log.error("Gmail authentication failed: %s", e)
            if not slack_enabled:
                print(f"Gmail authentication failed: {e}", file=sys.stderr)
                sys.exit(1)
            else:
                log.warning("Continuing with Slack only (Gmail init failed)")
                gmail_enabled = False

    # --- Slack init ---
    slack_token = None
    slack_state = None

    if slack_enabled:
        try:
            slack_token = get_slack_token()
            if not slack_token:
                raise RuntimeError(
                    "No valid Slack MCP token found. "
                    "Run any Slack MCP tool in Claude Code first to authenticate."
                )
            log.info("Slack MCP token loaded successfully")
            slack_state = load_slack_state()
            if not slack_state["channel_id"]:
                raise RuntimeError(
                    "No channel_id in state file. "
                    "Run /check-slack in Claude Code first to initialize."
                )
            if not slack_state["last_checked_ts"]:
                slack_state["last_checked_ts"] = f"{time.time():.6f}"
                save_slack_state(slack_state)
                log.info("Slack first run - initialized timestamp")
            log.info(
                "Watching #%s (%s), %d active threads",
                slack_state["channel_name"],
                slack_state["channel_id"],
                len(slack_state.get("active_threads", {})),
            )
        except Exception as e:
            log.error("Slack init failed: %s", e)
            if not gmail_enabled:
                print(f"Slack init failed: {e}", file=sys.stderr)
                sys.exit(1)
            else:
                log.warning("Continuing with Gmail only (Slack init failed)")
                slack_enabled = False

    if not gmail_enabled and not slack_enabled:
        print("Error: No transport could be initialized.", file=sys.stderr)
        sys.exit(1)

    log.info("Startup time: %s", _startup_time.isoformat())

    # Graceful shutdown
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        log.info("Received signal %d, shutting down...", signum)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    while running:
        # --- Slack poll ---
        if slack_enabled:
            if not is_business_hours():
                log.debug("Outside business hours, skipping Slack poll")
            else:
                try:
                    # Re-read token each cycle in case Claude Code refreshed it
                    slack_token = get_slack_token()
                    if not slack_token:
                        log.warning("Slack token expired or missing")
                    else:
                        log.info("Starting Slack poll cycle")
                        slack_state = load_slack_state()
                        slack_poll_cycle(slack_token, slack_state)
                        log.info("Slack poll cycle complete")
                except Exception:
                    log.exception("Error in Slack poll cycle")

        # --- Gmail poll ---
        if gmail_enabled:
            try:
                log.info("Starting Gmail poll cycle")
                gmail_poll_cycle(service, my_email, processed_ids, thread_sessions, startup_time_ms)
                log.info("Gmail poll cycle complete")
            except Exception:
                log.exception("Error in Gmail poll cycle")
                # Re-authenticate in case token issues
                try:
                    service = authenticate()
                except Exception:
                    log.exception("Gmail re-authentication failed")

        # Sleep in small increments so we respond to signals
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    log.info("ClaudeRemote stopped")


# ---------------------------------------------------------------------------
# Daemon Management
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_bridge_pids():
    """Return PIDs of running bridge.py daemon processes (not ourselves)."""
    skip_pids = {os.getpid(), os.getppid()}
    try:
        out = subprocess.check_output(["ps", "aux"], text=True)
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in out.splitlines():
        if "bridge.py" not in line or "slack_bridge" in line or "slack_mcp_bridge" in line:
            continue
        parts = line.split()
        try:
            pid = int(parts[1])
        except (IndexError, ValueError):
            continue
        if pid in skip_pids:
            continue
        pids.append(pid)
    return pids


def start_daemon(gmail_enabled: bool = True, slack_enabled: bool = False):
    """Start the bridge as a background subprocess."""
    # Check if already running
    pids = _find_bridge_pids()
    if pids:
        print(f"Bridge already running (PIDs {pids})")
        return

    # Build the run command with transport flags
    cmd = [sys.executable, __file__, "run"]
    if gmail_enabled and slack_enabled:
        cmd.append("--all")
    elif slack_enabled:
        cmd.append("--slack")
    else:
        cmd.append("--gmail")

    # Launch as a detached subprocess
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))

    transports = []
    if gmail_enabled:
        transports.append("Gmail")
    if slack_enabled:
        transports.append("Slack")
    print(f"Bridge started (PID {proc.pid}) [{' + '.join(transports)}]")
    print(f"  Poll interval: {POLL_INTERVAL}s")
    if slack_enabled and BUSINESS_HOURS_ONLY:
        print(f"  Slack business hours: {BUSINESS_HOURS_START}:00 - {BUSINESS_HOURS_END}:00")
    print(f"  Logs: {LOG_FILE}")


def stop_daemon():
    """Stop all running bridge daemon(s)."""
    pids = set(_find_bridge_pids())
    if PID_FILE.exists():
        try:
            file_pid = int(PID_FILE.read_text().strip())
            if _pid_alive(file_pid):
                pids.add(file_pid)
        except (ValueError, OSError):
            pass
    pids = list(pids)

    if not pids:
        print("Bridge is not running")
        if PID_FILE.exists():
            PID_FILE.unlink()
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"Sent SIGTERM to bridge process(es): {pids}")

    for _ in range(10):
        if not any(_pid_alive(p) for p in pids):
            break
        time.sleep(0.5)

    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    print("Bridge stopped")
    if PID_FILE.exists():
        PID_FILE.unlink()


def show_status():
    """Show bridge status including both transports."""
    pids = _find_bridge_pids()
    if pids:
        print(f"Bridge is running (PIDs {pids})")
    else:
        if PID_FILE.exists():
            file_pid = PID_FILE.read_text().strip()
            if _pid_alive(int(file_pid)):
                print(f"Bridge is running (PID {file_pid})")
            else:
                print("Bridge is not running (stale PID file)")
        else:
            print("Bridge is not running")

    # Gmail status
    print("\n[Gmail]")
    if TOKEN_FILE.exists():
        print("  Token: present")
    else:
        print("  Token: not configured")
    if GMAIL_PROCESSED_FILE.exists():
        count = len(GMAIL_PROCESSED_FILE.read_text().strip().splitlines())
        print(f"  Processed messages: {count}")
    if SESSIONS_FILE.exists():
        try:
            sessions = json.loads(SESSIONS_FILE.read_text())
            print(f"  Active threads: {len(sessions)}")
        except (json.JSONDecodeError, ValueError):
            pass

    # Slack status
    print("\n[Slack MCP]")
    slack_state = load_slack_state()
    print(f"  Channel: #{slack_state.get('channel_name', '?')} ({slack_state.get('channel_id', '?')})")
    print(f"  Last checked: {slack_state.get('last_checked_ts', 'never')}")
    print(f"  Active threads: {len(slack_state.get('active_threads', {}))}")

    token = get_slack_token()
    if token:
        creds = _load_credentials()
        entry = _find_slack_token_entry(creds)
        if entry:
            expires = entry.get("expiresAt", 0)
            if expires:
                exp_dt = datetime.fromtimestamp(expires / 1000)
                remaining = exp_dt - datetime.now()
                print(f"  Token expires: {exp_dt.isoformat()} ({remaining})")
    else:
        print("  Token: MISSING or EXPIRED")


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------


def _parse_transport_flags(args) -> tuple:
    """Parse --gmail, --slack, --all flags. Returns (gmail_enabled, slack_enabled)."""
    if args.all:
        return True, True
    if args.slack and not args.gmail:
        return False, True
    if args.gmail and not args.slack:
        return True, False
    # Default: --gmail only (backward compatible)
    if not args.gmail and not args.slack:
        return True, False
    return args.gmail, args.slack


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    parser = argparse.ArgumentParser(
        description="ClaudeRemote: unified Gmail + Slack remote interface for Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s run --gmail       # Gmail only (default)\n"
            "  %(prog)s run --slack       # Slack MCP only\n"
            "  %(prog)s run --all         # Both Gmail + Slack\n"
            "  %(prog)s start --all       # Daemon with both transports\n"
            "  %(prog)s stop              # Stop daemon\n"
            "  %(prog)s status            # Show status\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run subcommand
    run_parser = subparsers.add_parser("run", help="Run in foreground")
    run_parser.add_argument("--gmail", action="store_true", help="Enable Gmail transport")
    run_parser.add_argument("--slack", action="store_true", help="Enable Slack MCP transport")
    run_parser.add_argument("--all", action="store_true", help="Enable both Gmail + Slack")

    # start subcommand
    start_parser = subparsers.add_parser("start", help="Start as background daemon")
    start_parser.add_argument("--gmail", action="store_true", help="Enable Gmail transport")
    start_parser.add_argument("--slack", action="store_true", help="Enable Slack MCP transport")
    start_parser.add_argument("--all", action="store_true", help="Enable both Gmail + Slack")

    # stop subcommand
    subparsers.add_parser("stop", help="Stop daemon")

    # status subcommand
    subparsers.add_parser("status", help="Show bridge status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        gmail_enabled, slack_enabled = _parse_transport_flags(args)
        run_bridge(foreground=True, gmail_enabled=gmail_enabled, slack_enabled=slack_enabled)
    elif args.command == "start":
        gmail_enabled, slack_enabled = _parse_transport_flags(args)
        start_daemon(gmail_enabled=gmail_enabled, slack_enabled=slack_enabled)
    elif args.command == "stop":
        stop_daemon()
    elif args.command == "status":
        show_status()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
