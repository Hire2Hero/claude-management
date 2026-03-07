"""iTerm AppleScript launcher for Claude sessions."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Optional

from config import Config

log = logging.getLogger("claude_mgmt")


class TerminalLauncher:
    def __init__(self, config: Config):
        self.config = config

    def _make_env(self) -> dict:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        if self.config.claude_oauth_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.config.claude_oauth_token
        return env

    def _launch_iterm(self, tab_name: str, shell_cmd: str) -> Optional[int]:
        """Launch a command in a new iTerm tab and return the PID."""
        pid_file = f"/tmp/claude_mgmt_{tab_name.replace(' ', '_').replace('/', '_')}"

        token_export = ""
        if self.config.claude_oauth_token:
            token_export = f"export CLAUDE_CODE_OAUTH_TOKEN='{self.config.claude_oauth_token}' && "

        full_cmd = (
            f"unset CLAUDECODE && "
            f"{token_export}"
            f"echo $$ > {pid_file} && "
            f"{shell_cmd}"
        )

        as_escaped = full_cmd.replace("\\", "\\\\").replace('"', '\\"')
        tab_escaped = tab_name.replace('"', '\\"')

        applescript = (
            'tell application "iTerm"\n'
            '    activate\n'
            '    if (count of windows) = 0 then\n'
            '        set newWindow to (create window with default profile)\n'
            '        tell current session of newWindow\n'
            f'            set name to "{tab_escaped}"\n'
            f'            write text "{as_escaped}"\n'
            '        end tell\n'
            '    else\n'
            '        tell current window\n'
            '            create tab with default profile\n'
            '            tell current session\n'
            f'                set name to "{tab_escaped}"\n'
            f'                write text "{as_escaped}"\n'
            '            end tell\n'
            '        end tell\n'
            '    end if\n'
            'end tell'
        )

        try:
            subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, text=True, timeout=10,
            )
            time.sleep(2)
            if os.path.exists(pid_file):
                with open(pid_file) as f:
                    pid = int(f.read().strip())
                log.info("Launched iTerm tab '%s' (PID %d)", tab_name, pid)
                return pid
            log.warning("PID file not found for '%s'", tab_name)
            return None
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            log.error("iTerm launch failed for '%s': %s", tab_name, e)
            return None

    @property
    def _skip_flag(self) -> str:
        return "--dangerously-skip-permissions " if self.config.dangerously_skip_permissions else ""

    def launch_new_session(self, repo: str, name: str, prompt: str) -> Optional[int]:
        """Launch a new Claude session with a custom name and prompt."""
        repo_dir = os.path.join(self.config.base_dir, repo)
        escaped_name = name.replace("'", "'\\''")
        escaped_prompt = prompt.replace("'", "'\\''")

        shell_cmd = (
            f"cd '{repo_dir}' && "
            f"claude {self._skip_flag}"
            f"'/rename {escaped_name}' '{escaped_prompt}'"
        )
        return self._launch_iterm(f"Session: {name}", shell_cmd)

    def launch_pr_fix(self, repo: str, pr_number: int, branch: str,
                      prompt: str, session_id: Optional[str] = None) -> Optional[int]:
        """Launch Claude to fix a PR issue."""
        repo_dir = os.path.join(self.config.base_dir, repo)
        escaped_prompt = prompt.replace("'", "'\\''")

        resume_flag = f"--resume {session_id} " if session_id else ""
        shell_cmd = (
            f"cd '{repo_dir}' && "
            f"claude {self._skip_flag}{resume_flag}"
            f"'{escaped_prompt}'"
        )
        return self._launch_iterm(f"PR: {repo}#{pr_number}", shell_cmd)

    def run_claude_print(self, repo: str, prompt: str) -> Optional[str]:
        """Run Claude in -p mode (non-interactive). Returns stdout or None."""
        repo_dir = os.path.join(self.config.base_dir, repo)
        return self.run_claude_print_raw(repo_dir, prompt)

    def run_claude_print_raw(self, cwd: str, prompt: str, timeout: int = 120) -> Optional[str]:
        """Run Claude in -p mode with an explicit cwd. Returns stdout or None."""
        env = self._make_env()
        try:
            cmd = ["claude", "-p"]
            if self.config.dangerously_skip_permissions:
                cmd.append("--dangerously-skip-permissions")
            cmd.append(prompt)
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=timeout,
                cwd=cwd, env=env,
            )
            if result.returncode == 0:
                log.info("Claude -p completed (cwd=%s)", cwd)
                return result.stdout.strip()
            log.error("Claude -p failed (cwd=%s): %s", cwd, result.stderr.strip())
            return None
        except (subprocess.TimeoutExpired, OSError) as e:
            log.error("Claude -p error (cwd=%s): %s", cwd, e)
            return None
