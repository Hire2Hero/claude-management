"""Claude CLI manager — uses claude-code-sdk for bidirectional communication."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Callable, Optional

from claude_code_sdk import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    SystemMessage,
    query,
)
from claude_code_sdk._errors import MessageParseError
from claude_code_sdk._internal import client as _client_mod
from claude_code_sdk._internal import message_parser as _mp
from claude_code_sdk.types import StreamEvent

log = logging.getLogger("claude_mgmt")

# Patch parse_message to handle unknown message types gracefully instead of
# raising MessageParseError (the SDK doesn't handle rate_limit_event, etc.).
# Must patch both the module *and* the client module's local binding.
_original_parse_message = _mp.parse_message


def _tolerant_parse_message(data):
    try:
        return _original_parse_message(data)
    except MessageParseError:
        msg_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
        log.debug("Skipping unknown SDK message type: %s", msg_type)
        return None


_mp.parse_message = _tolerant_parse_message
_client_mod.parse_message = _tolerant_parse_message


def _make_env(oauth_token: str = "") -> dict:
    """Build env for Claude subprocess — strip CLAUDECODE to avoid nesting."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


class _AsyncBridge:
    """Run an asyncio event loop in a background thread for SDK calls."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="claude-async"
        )
        self._thread.start()

    def run(self, coro) -> asyncio.Future:
        """Schedule a coroutine on the background loop. Returns a Future."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self):
        """Stop the event loop."""
        self._loop.call_soon_threadsafe(self._loop.stop)


# Shared async bridge — one event loop for all ClaudeProcess instances
_bridge = _AsyncBridge()


class ClaudeProcess:
    """Manages a Claude session communicating via the claude-code-sdk."""

    def __init__(
        self,
        cwd: str,
        on_event: Callable[[dict], None],
        on_exit: Callable[[int | None], None],
        session_id: Optional[str] = None,
        initial_prompt: Optional[str] = None,
        oauth_token: str = "",
        dangerously_skip_permissions: bool = False,
    ):
        self._cwd = cwd
        self._on_event = on_event
        self._on_exit = on_exit
        self._session_id = session_id
        self._initial_prompt = initial_prompt
        self._oauth_token = oauth_token
        self._skip_permissions = dangerously_skip_permissions
        self._running = False
        self._generation = 0
        self._current_future: Optional[asyncio.Future] = None
        # Used to feed follow-up messages into an active query
        self._message_queue: Optional[asyncio.Queue] = None

    @property
    def pid(self) -> Optional[int]:
        # SDK manages the subprocess internally — no direct PID access.
        # Return None; callers use is_alive for liveness checks instead.
        return None

    @property
    def is_alive(self) -> bool:
        return self._running

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def start(self):
        """Launch a Claude SDK query for this session."""
        prompt = self._initial_prompt or ""
        if not prompt:
            return

        self._generation += 1
        gen = self._generation
        self._running = True
        self._message_queue = asyncio.Queue()

        log.info(
            "Starting Claude SDK session gen=%d (cwd=%s, resume=%s)",
            gen, self._cwd, self._session_id,
        )

        self._current_future = _bridge.run(self._run_query(prompt, gen))

    async def _run_query(self, prompt: str, gen: int):
        """Execute a single SDK query and emit translated events."""
        try:
            options = self._build_options()
            async for message in query(prompt=prompt, options=options):
                if gen != self._generation:
                    return  # Superseded by a newer generation
                self._translate_and_emit(message)
        except Exception as e:
            log.error("Claude SDK error: %s", e)
            if gen == self._generation:
                self._on_event({"type": "error", "error": {"message": str(e)}})
        finally:
            if gen == self._generation:
                self._running = False
                self._on_exit(0)

    def _build_options(self) -> ClaudeCodeOptions:
        """Build ClaudeCodeOptions from instance config."""
        env = _make_env(self._oauth_token)
        kwargs: dict = {
            "cwd": self._cwd,
            "include_partial_messages": True,
            "env": env,
        }
        if self._skip_permissions:
            kwargs["permission_mode"] = "bypassPermissions"
        if self._session_id:
            kwargs["resume"] = self._session_id
        return ClaudeCodeOptions(**kwargs)

    def _translate_and_emit(self, message):
        """Translate SDK message types into the dict format main.py expects."""
        if message is None:
            return  # Unknown message type — already logged by tolerant parser

        if isinstance(message, StreamEvent):
            # StreamEvent.event is the raw Claude API stream event dict
            # Wrap it in the stream_event envelope that _handle_claude_event expects
            evt = {
                "type": "stream_event",
                "event": message.event,
            }
            self._on_event(evt)

            # Capture session_id from stream events
            if message.session_id and not self._session_id:
                self._session_id = message.session_id

        elif isinstance(message, SystemMessage):
            # SystemMessage has subtype and data dict
            evt: dict = {"type": "system", "subtype": message.subtype}
            evt.update(message.data)
            # Capture session_id if present
            if "session_id" in message.data:
                self._session_id = message.data["session_id"]
            self._on_event(evt)

        elif isinstance(message, AssistantMessage):
            # Full assistant message — emit as "assistant" type
            # (main.py mostly ignores this, relying on stream_event deltas)
            self._on_event({"type": "assistant", "message": message})

        elif isinstance(message, ResultMessage):
            # Capture session_id
            if message.session_id:
                self._session_id = message.session_id

            if message.is_error:
                self._on_event({
                    "type": "error",
                    "error": {"message": message.result or message.subtype},
                })
            else:
                evt = {
                    "type": "result",
                    "result": message.result or "",
                    "session_id": message.session_id,
                    "subtype": message.subtype,
                    "cost_usd": message.total_cost_usd,
                    "duration_ms": message.duration_ms,
                    "num_turns": message.num_turns,
                }
                self._on_event(evt)

    def send_message(self, text: str):
        """Send a follow-up message. Starts a new query (with resume) if needed."""
        if self._running:
            # Can't send to an active query() — it's a single prompt call.
            # Queue it for after the current query finishes.
            log.warning("send_message while running — will restart with resume after current finishes")
            # Stop the current query and restart with the new prompt
            self.stop()

        # Start a new query with resume to continue the conversation
        self._initial_prompt = text
        self.start()

    def stop(self):
        """Cancel the running SDK query."""
        if not self._running:
            return

        self._running = False
        gen_before = self._generation
        self._generation += 1  # Invalidate current generation

        if self._current_future and not self._current_future.done():
            self._current_future.cancel()

        log.info("Claude SDK session stopped (gen=%d)", gen_before)
