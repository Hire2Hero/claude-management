"""Tests for terminal.py — iTerm launcher, Claude print mode."""

import os
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from config import Config
from terminal import TerminalLauncher


@pytest.fixture
def config(tmp_path):
    return Config(org="TestOrg", repos=["RepoA"], base_dir=str(tmp_path))


@pytest.fixture
def launcher(config):
    return TerminalLauncher(config)


class TestMakeEnv:
    def test_removes_claudecode(self, launcher):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "PATH": "/usr/bin"}):
            env = launcher._make_env()
            assert "CLAUDECODE" not in env
            assert "PATH" in env

    def test_handles_missing_claudecode(self, launcher):
        env_copy = os.environ.copy()
        env_copy.pop("CLAUDECODE", None)
        with patch.dict(os.environ, env_copy, clear=True):
            env = launcher._make_env()
            assert "CLAUDECODE" not in env


class TestLaunchIterm:
    @patch("terminal.time.sleep")
    @patch("terminal.subprocess.run")
    def test_returns_pid_from_file(self, mock_run, mock_sleep, launcher, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Pre-create the PID file that the shell command would create
        pid_file = "/tmp/claude_mgmt_test_tab"
        with open(pid_file, "w") as f:
            f.write("12345\n")

        try:
            pid = launcher._launch_iterm("test_tab", "echo hello")
            assert pid == 12345

            # Verify osascript was called
            args = mock_run.call_args[0][0]
            assert args[0] == "osascript"
            assert args[1] == "-e"
        finally:
            os.unlink(pid_file)

    @patch("terminal.time.sleep")
    @patch("terminal.subprocess.run")
    def test_returns_none_when_no_pid_file(self, mock_run, mock_sleep, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        pid = launcher._launch_iterm("nonexistent_tab_xyz", "echo test")
        assert pid is None

    @patch("terminal.time.sleep")
    @patch("terminal.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run, mock_sleep, launcher):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="osascript", timeout=10)
        pid = launcher._launch_iterm("timeout_tab", "echo test")
        assert pid is None

    @patch("terminal.time.sleep")
    @patch("terminal.subprocess.run")
    def test_applescript_contains_tab_name(self, mock_run, mock_sleep, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        launcher._launch_iterm("My Tab Name", "echo test")

        applescript = mock_run.call_args[0][0][2]
        assert "My Tab Name" in applescript
        assert 'tell application "iTerm"' in applescript


class TestLaunchNewSession:
    @patch.object(TerminalLauncher, "_launch_iterm", return_value=42)
    def test_launches_with_correct_args(self, mock_launch, launcher, config):
        pid = launcher.launch_new_session("RepoA", "my-session", "do something")
        assert pid == 42

        tab_name, shell_cmd = mock_launch.call_args[0]
        assert tab_name == "Session: my-session"
        assert "RepoA" in shell_cmd
        assert "/rename my-session" in shell_cmd
        assert "do something" in shell_cmd
        assert "--dangerously-skip-permissions" not in shell_cmd


class TestLaunchPrFix:
    @patch.object(TerminalLauncher, "_launch_iterm", return_value=77)
    def test_launches_without_resume(self, mock_launch, launcher):
        pid = launcher.launch_pr_fix("RepoA", 10, "feat/branch", "fix it")
        assert pid == 77

        tab_name, shell_cmd = mock_launch.call_args[0]
        assert tab_name == "PR: RepoA#10"
        assert "--resume" not in shell_cmd
        assert "fix it" in shell_cmd

    @patch.object(TerminalLauncher, "_launch_iterm", return_value=88)
    def test_launches_with_resume(self, mock_launch, launcher):
        pid = launcher.launch_pr_fix("RepoA", 10, "feat/branch", "fix it", session_id="sess-abc")
        assert pid == 88

        _, shell_cmd = mock_launch.call_args[0]
        assert "--resume sess-abc" in shell_cmd


class TestRunClaudePrint:
    @patch("terminal.subprocess.run")
    def test_returns_stdout_on_success(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="  output text  \n", stderr="")
        result = launcher.run_claude_print("RepoA", "test prompt")
        assert result == "output text"

    @patch("terminal.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = launcher.run_claude_print("RepoA", "test prompt")
        assert result is None

    @patch("terminal.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run, launcher):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        result = launcher.run_claude_print("RepoA", "test prompt")
        assert result is None

    @patch("terminal.subprocess.run")
    def test_unsets_claudecode_env(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            launcher.run_claude_print("RepoA", "prompt")
        env = mock_run.call_args[1].get("env", {})
        assert "CLAUDECODE" not in env

    @patch("terminal.subprocess.run")
    def test_uses_correct_cwd(self, mock_run, launcher, config):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        launcher.run_claude_print("RepoA", "prompt")
        cwd = mock_run.call_args[1].get("cwd", "")
        assert cwd == os.path.join(config.base_dir, "RepoA")


class TestRunClaudePrintRaw:
    @patch("terminal.subprocess.run")
    def test_returns_stdout_on_success(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="  raw output  \n", stderr="")
        result = launcher.run_claude_print_raw("/some/dir", "test prompt")
        assert result == "raw output"

    @patch("terminal.subprocess.run")
    def test_returns_none_on_failure(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = launcher.run_claude_print_raw("/some/dir", "test prompt")
        assert result is None

    @patch("terminal.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run, launcher):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        result = launcher.run_claude_print_raw("/some/dir", "test prompt")
        assert result is None

    @patch("terminal.subprocess.run")
    def test_uses_provided_cwd(self, mock_run, launcher):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        launcher.run_claude_print_raw("/custom/path", "prompt")
        cwd = mock_run.call_args[1].get("cwd", "")
        assert cwd == "/custom/path"

    @patch("terminal.subprocess.run")
    def test_run_claude_print_delegates_to_raw(self, mock_run, launcher, config):
        """run_claude_print should delegate to run_claude_print_raw with repo_dir."""
        mock_run.return_value = MagicMock(returncode=0, stdout="delegated\n", stderr="")
        result = launcher.run_claude_print("RepoA", "prompt")
        assert result == "delegated"
        cwd = mock_run.call_args[1].get("cwd", "")
        assert cwd == os.path.join(config.base_dir, "RepoA")
