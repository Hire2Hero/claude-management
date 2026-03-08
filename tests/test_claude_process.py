"""Tests for claude_process.py — Claude SDK-based session manager."""

import asyncio
import json
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from claude_process import ClaudeProcess, _make_env, _bridge


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
    @patch("claude_process._bridge")
    def test_start_launches_query(self, mock_bridge):
        mock_future = MagicMock()
        mock_bridge.run.return_value = mock_future

        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="hello",
        )
        proc.start()

        assert proc.is_alive is True
        mock_bridge.run.assert_called_once()
        assert proc._generation == 1

    @patch("claude_process._bridge")
    def test_start_without_prompt_does_nothing(self, mock_bridge):
        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.start()

        assert proc.is_alive is False
        mock_bridge.run.assert_not_called()

    @patch("claude_process._bridge")
    def test_start_increments_generation(self, mock_bridge):
        mock_bridge.run.return_value = MagicMock()

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        assert proc._generation == 1
        proc._running = False  # Simulate stop
        proc.start()
        assert proc._generation == 2


class TestClaudeProcessBuildOptions:
    def test_builds_basic_options(self):
        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        opts = proc._build_options()
        assert str(opts.cwd) == "/test/dir"
        assert opts.include_partial_messages is True
        assert opts.permission_mode is None
        assert opts.resume is None

    def test_builds_options_with_resume(self):
        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            session_id="sess-xyz",
        )
        opts = proc._build_options()
        assert opts.resume == "sess-xyz"

    def test_builds_options_with_skip_permissions(self):
        proc = ClaudeProcess(
            cwd="/test/dir",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            dangerously_skip_permissions=True,
        )
        opts = proc._build_options()
        assert opts.permission_mode == "bypassPermissions"


class TestClaudeProcessSendMessage:
    @patch("claude_process._bridge")
    def test_send_message_restarts_with_resume(self, mock_bridge):
        mock_bridge.run.return_value = MagicMock()

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            session_id="sess-123",
        )
        # Not running — should start a new query
        proc.send_message("test message")

        assert mock_bridge.run.call_count == 1
        assert proc._initial_prompt == "test message"

    @patch("claude_process._bridge")
    def test_send_while_running_stops_and_restarts(self, mock_bridge):
        mock_future = MagicMock()
        mock_future.done.return_value = False
        mock_bridge.run.return_value = mock_future

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="first",
        )
        proc.start()
        assert proc._generation == 1

        # Send while running — should stop and restart
        proc.send_message("second")
        assert proc._initial_prompt == "second"
        # Generation incremented by stop() and start()
        assert proc._generation == 3


class TestClaudeProcessStop:
    @patch("claude_process._bridge")
    def test_stop_cancels_future(self, mock_bridge):
        mock_future = MagicMock()
        mock_future.done.return_value = False
        mock_bridge.run.return_value = mock_future

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        proc.stop()

        mock_future.cancel.assert_called_once()
        assert proc.is_alive is False

    def test_stop_without_running_does_not_raise(self):
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
        )
        proc.stop()  # Should not raise


class TestClaudeProcessTranslation:
    """Test _translate_and_emit converts SDK messages to expected dict format."""

    def _make_proc(self):
        events = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )
        proc._running = True
        return proc, events

    def test_stream_event_translated(self):
        proc, events = self._make_proc()
        from claude_code_sdk.types import StreamEvent

        se = StreamEvent(
            uuid="u1",
            session_id="s123",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
            parent_tool_use_id=None,
        )
        proc._translate_and_emit(se)

        assert len(events) == 1
        assert events[0]["type"] == "stream_event"
        assert events[0]["event"]["type"] == "content_block_delta"
        assert proc.session_id == "s123"

    def test_system_message_translated(self):
        proc, events = self._make_proc()
        from claude_code_sdk import SystemMessage

        sm = SystemMessage(subtype="init", data={"session_id": "s456"})
        proc._translate_and_emit(sm)

        assert len(events) == 1
        assert events[0]["type"] == "system"
        assert events[0]["session_id"] == "s456"
        assert proc.session_id == "s456"

    def test_result_message_success_translated(self):
        proc, events = self._make_proc()
        from claude_code_sdk import ResultMessage

        rm = ResultMessage(
            subtype="success",
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=2,
            session_id="s789",
            total_cost_usd=0.05,
            usage=None,
            result="Done!",
        )
        proc._translate_and_emit(rm)

        assert len(events) == 1
        assert events[0]["type"] == "result"
        assert events[0]["result"] == "Done!"
        assert events[0]["session_id"] == "s789"
        assert proc.session_id == "s789"

    def test_result_message_error_translated(self):
        proc, events = self._make_proc()
        from claude_code_sdk import ResultMessage

        rm = ResultMessage(
            subtype="error_max_turns",
            duration_ms=5000,
            duration_api_ms=4000,
            is_error=True,
            num_turns=10,
            session_id="s999",
            total_cost_usd=0.50,
            usage=None,
            result="Max turns exceeded",
        )
        proc._translate_and_emit(rm)

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert events[0]["error"]["message"] == "Max turns exceeded"

    def test_assistant_message_translated(self):
        proc, events = self._make_proc()
        from claude_code_sdk import AssistantMessage, TextBlock

        am = AssistantMessage(
            content=[TextBlock(text="Hello world")],
            model="claude-sonnet-4-20250514",
            parent_tool_use_id=None,
        )
        proc._translate_and_emit(am)

        assert len(events) == 1
        assert events[0]["type"] == "assistant"


class TestClaudeProcessProperties:
    @patch("claude_process._bridge")
    def test_pid_returns_none(self, mock_bridge):
        mock_bridge.run.return_value = MagicMock()

        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: None,
            initial_prompt="test",
        )
        proc.start()
        assert proc.pid is None

    @patch("claude_process._bridge")
    def test_is_alive_true_when_running(self, mock_bridge):
        mock_bridge.run.return_value = MagicMock()

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


class TestTolerantParseMessage:
    """Test that unknown message types are handled gracefully."""

    def test_known_types_still_parse(self):
        from claude_code_sdk._internal.message_parser import parse_message
        from claude_code_sdk import SystemMessage

        result = parse_message({"type": "system", "subtype": "init", "session_id": "s1"})
        assert isinstance(result, SystemMessage)

    def test_unknown_type_returns_none_via_parser_module(self):
        from claude_code_sdk._internal.message_parser import parse_message

        result = parse_message({"type": "rate_limit_event", "retry_after": 5})
        assert result is None

    def test_unknown_type_returns_none_via_client_module(self):
        """Verify the patch on the client module's local binding works too."""
        from claude_code_sdk._internal.client import parse_message

        result = parse_message({"type": "rate_limit_event", "retry_after": 5})
        assert result is None

    def test_translate_and_emit_skips_none(self):
        proc, events = TestClaudeProcessTranslation()._make_proc()
        proc._translate_and_emit(None)
        assert len(events) == 0


class TestRunQuery:
    """Test the async _run_query method."""

    def test_run_query_calls_on_exit(self):
        """Verify _run_query calls on_exit when complete."""
        exit_codes = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: exit_codes.append(c),
        )
        proc._generation = 1
        proc._running = True

        # Mock query to yield nothing (empty conversation)
        async def mock_query(**kwargs):
            return
            yield  # Make it an async generator

        with patch("claude_process.query", mock_query):
            asyncio.run(proc._run_query("test", gen=1))

        assert exit_codes == [0]
        assert proc._running is False

    def test_stale_gen_does_not_call_on_exit(self):
        """Verify stale generation doesn't trigger on_exit."""
        exit_codes = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: None,
            on_exit=lambda c: exit_codes.append(c),
        )
        proc._generation = 2  # Current gen is 2
        proc._running = True

        async def mock_query(**kwargs):
            return
            yield

        with patch("claude_process.query", mock_query):
            asyncio.run(proc._run_query("test", gen=1))  # Old gen=1

        assert exit_codes == []  # Exit ignored

    def test_run_query_emits_events(self):
        """Verify events are translated and emitted."""
        events = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )
        proc._generation = 1
        proc._running = True

        from claude_code_sdk import ResultMessage

        async def mock_query(**kwargs):
            yield ResultMessage(
                subtype="success",
                duration_ms=100,
                duration_api_ms=80,
                is_error=False,
                num_turns=1,
                session_id="s-test",
                total_cost_usd=0.01,
                usage=None,
                result="All done",
            )

        with patch("claude_process.query", mock_query):
            asyncio.run(proc._run_query("test", gen=1))

        assert any(e.get("type") == "result" for e in events)
        assert proc.session_id == "s-test"

    def test_run_query_handles_exception(self):
        """Verify exceptions are caught and emitted as error events."""
        events = []
        proc = ClaudeProcess(
            cwd="/tmp",
            on_event=lambda e: events.append(e),
            on_exit=lambda c: None,
        )
        proc._generation = 1
        proc._running = True

        async def mock_query(**kwargs):
            raise RuntimeError("SDK connection failed")
            yield  # noqa: unreachable

        with patch("claude_process.query", mock_query):
            asyncio.run(proc._run_query("test", gen=1))

        assert any(e.get("type") == "error" for e in events)
        error_evt = next(e for e in events if e.get("type") == "error")
        assert "SDK connection failed" in error_evt["error"]["message"]
