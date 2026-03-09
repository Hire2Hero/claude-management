"""Claude CLI subprocess manager — bidirectional JSON over stdin/stdout."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger("claude_mgmt")


def _make_env(oauth_token: str = "") -> dict:
    """Build env for Claude subprocess — strip CLAUDECODE to avoid nesting."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


class ClaudeProcess:
    """Manages a single Claude CLI subprocess communicating via stream-json."""

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
        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._generation = 0  # Incremented on each start to ignore stale exits

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def start(self):
        """Launch the Claude CLI subprocess."""
        cmd = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            # In -p mode, all MCP tools are pre-loaded (not deferred), so
            # ToolSearch finds nothing and causes Claude to wrongly conclude
            # tools are unavailable.  Disabling it forces direct tool calls.
            "--disallowedTools", "ToolSearch",
            "--append-system-prompt",
            "All MCP tools (Slack, Sentry, Jira, Unblocked, etc.) are pre-loaded and "
            "directly callable. Call them directly by their full name "
            "(e.g. mcp__claude_ai_Slack__slack_read_thread, mcp__sentry__get_issue_details). "
            "Do not attempt to check tool availability before calling — just call the tool.",
        ]
        if self._skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self._session_id:
            cmd.extend(["--resume", self._session_id])

        self._generation += 1
        gen = self._generation

        log.info("Starting Claude process gen=%d: %s (cwd=%s)", gen, " ".join(cmd[:6]) + " ...", self._cwd)

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=_make_env(self._oauth_token),
        )

        # Send initial prompt via stdin if provided
        if self._initial_prompt:
            self._write_user_message(self._initial_prompt)

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(gen,), daemon=True, name="claude-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="claude-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _write_user_message(self, text: str):
        """Write a user message to stdin in stream-json format."""
        if not self._proc or not self._proc.stdin:
            return
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        try:
            self._proc.stdin.write(msg.encode())
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            log.warning("Failed to write to Claude stdin: %s", e)

    def send_message(self, text: str):
        """Send a follow-up message to the running process via stdin."""
        if self.is_alive:
            self._write_user_message(text)
        else:
            # Process exited — restart with resume
            self._initial_prompt = text
            self.start()

    def stop(self):
        """Terminate the Claude process gracefully, then force-kill if needed."""
        if not self._proc:
            return

        proc = self._proc
        self._proc = None

        # SIGTERM
        try:
            proc.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        # Wait up to 3 seconds
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Force kill
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass

        log.info("Claude process stopped (pid=%s)", proc.pid)

    def _read_stdout(self, gen: int):
        """Read stdout line-by-line, parse JSON events, call on_event.

        Uses readline() instead of iterating proc.stdout because Python's
        file iterator uses internal buffering that delays output in real-time
        subprocess reading.
        """
        proc = self._proc
        if not proc or not proc.stdout:
            return

        try:
            while True:
                raw_line = proc.stdout.readline()
                if not raw_line:
                    break  # EOF — process exited or stdout closed
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Capture session_id from init events
                    if event.get("type") == "system" and event.get("session_id"):
                        self._session_id = event["session_id"]
                    self._on_event(event)
                except json.JSONDecodeError:
                    # Non-JSON output — emit as raw text
                    self._on_event({"type": "raw", "text": line})
        except (OSError, ValueError):
            pass
        finally:
            # Only notify exit if this is still the current generation
            # (avoids stale exits when send_message restarts the process)
            if gen == self._generation:
                returncode = proc.poll()
                self._on_exit(returncode)

    def _read_stderr(self):
        """Read stderr and forward as events + log warnings."""
        proc = self._proc
        if not proc or not proc.stderr:
            return

        try:
            while True:
                raw_line = proc.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    log.warning("Claude stderr: %s", line)
                    self._on_event({"type": "stderr", "text": line})
        except (OSError, ValueError):
            pass
