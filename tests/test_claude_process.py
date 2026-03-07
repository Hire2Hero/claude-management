"""Tests for claude_process.py — Claude CLI subprocess manager."""

import io
import json
import os
import signal
import subprocess
from unittest.mock import MagicMock, call, patch, PropertyMock

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
    def test_starts_process_with_correct_args(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        assert "--resume" not in cmd

        assert "--input-format" in cmd
        assert cmd[cmd.index("--input-format") + 1] == "stream-json"

        kwargs = mock_popen.call_args[1]
        assert kwargs["cwd"] == "/test/dir"
        assert kwargs["stdin"] == subprocess.PIPE
        assert kwargs["stdout"] == subprocess.PIPE

    @patch("claude_process.subprocess.Popen")
    def test_starts_with_resume_flag(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            session_id="sess-xyz",
        )
        proc.start()

        cmd = mock_popen.call_args[0][0]
        assert "--resume" in cmd
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "sess-xyz"

    @patch("claude_process.subprocess.Popen")
    def test_sends_initial_prompt_via_stdin(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="hello",
        )
        proc.start()

        # Prompt is sent via stdin as stream-json, not as CLI argument
        cmd = mock_popen.call_args[0][0]
        assert cmd[-1] != "hello"
        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode())
        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        assert msg["message"]["content"] == "hello"


class TestClaudeProcessSendMessage:
    @patch("claude_process.subprocess.Popen")
    def test_send_message_writes_to_stdin(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.stdin = MagicMock()
        mock_proc.pid = 100
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            session_id="sess-123",
        )
        proc.start()

        # Reset stdin mock after initial prompt (None in this case)
        mock_proc.stdin.reset_mock()

        proc.send_message("test message")

        # Process was NOT restarted — only one Popen call
        assert mock_popen.call_count == 1

        # Message was written to stdin
        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode())
        assert msg["type"] == "user"
        assert msg["message"]["content"] == "test message"

    @patch("claude_process.subprocess.Popen")
    def test_send_without_process_starts_new(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.stdin = MagicMock()
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        # Should not raise — starts a new process
        proc.send_message("test")
        assert mock_popen.call_count == 1
        # Message sent via stdin
        mock_proc.stdin.write.assert_called_once()
        written = mock_proc.stdin.write.call_args[0][0]
        msg = json.loads(written.decode())
        assert msg["message"]["content"] == "test"


class TestClaudeProcessStop:
    @patch("claude_process.subprocess.Popen")
    def test_stop_sends_sigterm_and_waits(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()
        proc.stop()

        mock_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        mock_proc.wait.assert_called_once_with(timeout=3)
        assert proc.is_alive is False

    @patch("claude_process.subprocess.Popen")
    def test_stop_force_kills_on_timeout(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=3),
            0,
        ]
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()
        proc.stop()

        mock_proc.kill.assert_called_once()

    def test_stop_without_process_does_not_raise(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.stop()  # Should not raise


class TestClaudeProcessStdoutParsing:
    def test_parses_json_events(self):
        events = []

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )

        # Simulate stdout lines — use BytesIO so readline() works
        data = (
            json.dumps({"type": "system", "session_id": "s123"}).encode() + b"\n"
            + json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello"}}).encode() + b"\n"
        )

        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(data)
        mock_proc.poll.return_value = 0
        proc._proc = mock_proc
        proc._generation = 1

        proc._read_stdout(gen=1)

        assert len(events) == 2
        assert events[0]["type"] == "system"
        assert events[0]["session_id"] == "s123"
        assert proc.session_id == "s123"
        assert events[1]["type"] == "content_block_delta"

    def test_handles_non_json_lines(self):
        events = []

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )

        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(b"not json\n")
        mock_proc.poll.return_value = 0
        proc._proc = mock_proc
        proc._generation = 1

        proc._read_stdout(gen=1)

        assert len(events) == 1
        assert events[0]["type"] == "raw"
        assert events[0]["text"] == "not json"

    def test_calls_on_exit_when_done(self):
        exit_codes = []

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: exit_codes.append(c),
        )

        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.poll.return_value = 0
        proc._proc = mock_proc
        proc._generation = 1

        proc._read_stdout(gen=1)

        assert exit_codes == [0]

    def test_stale_gen_does_not_call_on_exit(self):
        exit_codes = []

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: exit_codes.append(c),
        )

        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.poll.return_value = -15
        proc._proc = mock_proc
        proc._generation = 2  # Current gen is 2

        proc._read_stdout(gen=1)  # But thread has old gen=1

        assert exit_codes == []  # Exit ignored


class TestClaudeProcessProperties:
    @patch("claude_process.subprocess.Popen")
    def test_pid_returns_process_pid(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 42
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()
        assert proc.pid == 42

    @patch("claude_process.subprocess.Popen")
    def test_is_alive_true_when_running(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()
        assert proc.is_alive is True

    @patch("claude_process.subprocess.Popen")
    def test_is_alive_false_when_exited(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.stderr = io.BytesIO(b"")
        mock_popen.return_value = mock_proc

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()
        assert proc.is_alive is False
