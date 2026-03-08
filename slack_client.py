"""Slack messaging — webhook or Claude MCP fallback."""

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


def send_via_mcp(channel: str, text: str, oauth_token: str = "", cwd: str = ".") -> bool:
    """Send a Slack message by asking Claude to use the MCP Slack tool.

    Launches a short-lived Claude session that calls
    mcp__claude_ai_Slack__slack_send_message to deliver the message.
    Returns True on success.
    """
    import asyncio
    from claude_code_sdk import ClaudeCodeOptions, query as claude_query
    from claude_process import _make_env

    prompt = (
        f"Send the following message to the Slack channel '{channel}' "
        f"using the mcp__claude_ai_Slack__slack_send_message tool. "
        f"Do not modify the message content. Do not add any extra text or formatting. "
        f"Just send it exactly as-is.\n\n"
        f"Message:\n{text}"
    )

    env = _make_env(oauth_token)
    options = ClaudeCodeOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        env=env,
    )

    success = False
    try:
        async def _run():
            nonlocal success
            async for message in claude_query(prompt=prompt, options=options):
                # Check for tool_use of the slack send tool to confirm it ran
                if hasattr(message, "content") and isinstance(message.content, list):
                    for block in message.content:
                        if hasattr(block, "type") and block.type == "tool_use":
                            if "slack_send_message" in getattr(block, "name", ""):
                                success = True
                # ResultMessage indicates completion
                if hasattr(message, "result"):
                    if not message.is_error:
                        success = True

        asyncio.run(_run())
    except Exception as e:
        log.warning("Slack MCP send error: %s", e)
        return False

    return success
