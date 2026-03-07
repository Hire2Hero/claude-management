"""Slack incoming webhook client using urllib (no external dependencies)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("claude_mgmt")


def send_webhook(webhook_url: str, text: str, unfurl_links: bool = True) -> bool:
    """POST a message to a Slack incoming webhook. Returns True on success."""
    body: dict = {"text": text}
    if not unfurl_links:
        body["unfurl_links"] = False
        body["unfurl_media"] = False
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        log.warning("Slack webhook error: %s", e)
        return False
