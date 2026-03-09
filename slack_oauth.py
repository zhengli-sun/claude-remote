#!/usr/bin/env python3
"""Slack MCP OAuth flow — gets an access token and saves it for bridge.py."""

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path
from urllib.request import Request, urlopen

# From the Slack MCP plugin config
CLIENT_ID = "1601185624273.8899143856786"
CALLBACK_PORT = 3118
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
MCP_URL = "https://mcp.slack.com/mcp"

# Save to a persistent location the bridge controls
CREDENTIALS_FILE = Path.home() / ".claude-remote" / "slack_mcp_token.json"


def run_oauth_flow():
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)

    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    # Scopes from Slack MCP's well-known OAuth metadata
    scopes = ",".join([
        "search:read.public",
        "search:read.private",
        "search:read.mpim",
        "search:read.im",
        "search:read.files",
        "search:read.users",
        "chat:write",
        "channels:history",
        "groups:history",
        "mpim:history",
        "im:history",
        "canvases:read",
        "canvases:write",
        "users:read",
        "users:read.email",
    ])

    auth_url = (
        "https://slack.com/oauth/v2_user/authorize?"
        + urllib.parse.urlencode({
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": scopes,
        })
    )

    auth_code = None
    received_state = None

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code, received_state
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            auth_code = params.get("code", [None])[0]
            received_state = params.get("state", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if auth_code:
                self.wfile.write(b"<h1>Success!</h1><p>You can close this tab.</p>")
            else:
                error = params.get("error", ["unknown"])[0]
                self.wfile.write(f"<h1>Error: {error}</h1>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)

    print("Opening browser for Slack authorization...")
    webbrowser.open(auth_url)
    print(f"Waiting for callback on http://localhost:{CALLBACK_PORT}/callback ...")

    server.handle_request()
    server.server_close()

    if not auth_code:
        print("ERROR: No authorization code received.")
        return

    if received_state != state:
        print("ERROR: State mismatch — possible CSRF.")
        return

    print("Got authorization code. Exchanging for token...")

    token_data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }).encode()

    req = Request(
        "https://slack.com/api/oauth.v2.user.access",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    with urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    if not result.get("ok"):
        print(f"ERROR: Token exchange failed: {result}")
        return

    access_token = result.get("access_token")
    refresh_token = result.get("refresh_token", "")
    expires_in = result.get("expires_in", 43200)

    if not access_token:
        print(f"ERROR: No access token in response: {json.dumps(result, indent=2)}")
        return

    expires_at_ms = int((time.time() + expires_in) * 1000)

    token_data = {
        "serverUrl": MCP_URL,
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }

    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(token_data, indent=2))
    CREDENTIALS_FILE.chmod(0o600)

    print(f"Token saved to {CREDENTIALS_FILE}")
    print(f"Expires at: {time.ctime(expires_at_ms / 1000)}")
    print("Restart bridge: .venv/bin/python bridge.py stop && .venv/bin/python bridge.py start --all")


if __name__ == "__main__":
    run_oauth_flow()
