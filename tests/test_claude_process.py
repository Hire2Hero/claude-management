"""Tests for claude_process.py — Claude CLI subprocess manager."""

import json
import os
import signal
from unittest.mock import MagicMock, patch, call

import pytest

from claude_process import ClaudeProcess, _make_env


class TestMakeEnv:
    def test_removes_claudecode(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "PATH": "/usr/bin"}):
            env = _make_env()
            assert "CLAUDECODE" not in env
            assert "PATH" in env

    def test_handles_missing_claudecode(self):
        env_copy = os.environ.copy()
        env_copy.pop("CLAUDECODE", None)
        with patch.dict(os.environ, env_copy, clear=True):
            env = _make_env()
            assert "CLAUDECODE" not in env

    def test_sets_oauth_token(self):
        env = _make_env(oauth_token="tok123")
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok123"

    def test_no_oauth_token_when_empty(self):
        env = _make_env()
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


class TestClaudeProcessInit:
    def test_initial_state(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        assert proc.pid is None
        assert proc.is_alive is False
        assert proc.session_id is None

    def test_session_id_preserved(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            session_id="abc-123",
        )
        assert proc.session_id == "abc-123"


class TestClaudeProcessStart:
    @patch("claude_process.subprocess.Popen")
    def test_start_launches_subprocess(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="hello",
        )
        proc.start()

        mock_popen.assert_called_once()
        args = mock_popen.call_args
        cmd = args[0][0] if args[0] else args[1].get("cmd", [])
        assert "claude" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert args[1]["cwd"] == "/test/dir"

    @patch("claude_process.subprocess.Popen")
    def test_start_sends_initial_prompt(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="hello world",
        )
        proc.start()

        # Should have written the user message to stdin
        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        parsed = json.loads(written.decode())
        assert parsed["type"] == "user"
        assert parsed["message"]["content"] == "hello world"

    @patch("claude_process.subprocess.Popen")
    def test_start_increments_generation(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        assert proc._generation == 1

    @patch("claude_process.subprocess.Popen")
    def test_start_with_resume(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
            session_id="sess-xyz",
        )
        proc.start()

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-xyz"

    @patch("claude_process.subprocess.Popen")
    def test_start_with_skip_permissions(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.poll.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
            dangerously_skip_permissions=True,
        )
        proc.start()

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd


class TestClaudeProcessSendMessage:
    @patch("claude_process.subprocess.Popen")
    def test_send_message_writes_to_stdin(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still alive
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="first",
        )
        proc.start()

        # Reset mock to clear the initial prompt write
        mock_proc.stdin.write.reset_mock()

        proc.send_message("follow up")

        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        parsed = json.loads(written.decode())
        assert parsed["message"]["content"] == "follow up"

    @patch("claude_process.subprocess.Popen")
    def test_send_message_restarts_if_dead(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # Exited
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="first",
        )
        proc.start()
        assert mock_popen.call_count == 1

        proc.send_message("retry")
        assert mock_popen.call_count == 2
        assert proc._initial_prompt == "retry"


class TestClaudeProcessStop:
    @patch("claude_process.subprocess.Popen")
    def test_stop_terminates_process(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        proc.stop()

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_stop_without_running_does_not_raise(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.stop()  # Should not raise


class TestClaudeProcessProperties:
    @patch("claude_process.subprocess.Popen")
    def test_pid_returns_process_pid(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        assert proc.pid == 12345

    @patch("claude_process.subprocess.Popen")
    def test_is_alive_true_when_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr.readline.return_value = b""
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        assert proc.is_alive is True

    def test_is_alive_false_initially(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        assert proc.is_alive is False


class TestClaudeProcessSessionCapture:
    def test_captures_session_id_from_system_event(self):
        events = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )
        # Simulate what _read_stdout does
        event = {"type": "system", "session_id": "s456"}
        if event.get("type") == "system" and event.get("session_id"):
            proc._session_id = event["session_id"]

        assert proc.session_id == "s456"
