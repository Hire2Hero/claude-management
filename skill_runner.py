"""Sequential skill execution for configurable workflows."""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("claude_mgmt")

# Cache for loaded skill SKILL.md contents
_skill_cache: dict[str, str] = {}


def _version_key(v: str) -> list[int]:
    """Parse version string into list of ints for proper sorting."""
    try:
        return [int(x) for x in v.split(".")]
    except ValueError:
        return [0]


def _load_plugin_skill(plugin: str, skill: str) -> str | None:
    """Load a plugin skill's SKILL.md content by plugin and skill name."""
    cache_key = f"{plugin}:{skill}"
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]
    cache_root = os.path.expanduser("~/.claude/plugins/cache")
    if not os.path.isdir(cache_root):
        return None
    for org in os.listdir(cache_root):
        org_path = os.path.join(cache_root, org)
        if not os.path.isdir(org_path):
            continue
        plugin_path = os.path.join(org_path, plugin)
        if not os.path.isdir(plugin_path):
            continue
        versions = [d for d in os.listdir(plugin_path)
                    if os.path.isdir(os.path.join(plugin_path, d))]
        if not versions:
            continue
        latest = sorted(versions, key=_version_key)[-1]
        md_path = os.path.join(plugin_path, latest, "skills", skill, "SKILL.md")
        if os.path.isfile(md_path):
            try:
                with open(md_path) as f:
                    content = f.read()
                _skill_cache[cache_key] = content
                return content
            except OSError:
                continue
    return None


# Matches /plugin-name:skill-name followed by optional arguments
_PLUGIN_SKILL_RE = re.compile(r"^/([^:\s]+):([^:\s]+)\s*(.*)", re.DOTALL)


def expand_skill_command(command: str) -> str:
    """Expand a /plugin:skill command into its SKILL.md content.

    If the command matches /plugin:skill [args], loads the skill's SKILL.md
    and replaces $ARGUMENTS with the provided args. Returns the original
    command unchanged if the skill cannot be loaded.
    """
    m = _PLUGIN_SKILL_RE.match(command)
    if not m:
        return command
    plugin, skill, args = m.group(1), m.group(2), m.group(3).strip()
    content = _load_plugin_skill(plugin, skill)
    if not content:
        log.warning("Could not load skill %s:%s — sending command as-is", plugin, skill)
        return command
    log.info("Expanded /%s:%s to SKILL.md content", plugin, skill)
    return content.replace("$ARGUMENTS", args)


class SkillRunner:
    """Tracks progress through a sequence of skill commands."""

    def __init__(
        self,
        session_name: str,
        commands: list[str],
        review_between: bool,
        placeholders: dict[str, str],
    ):
        self.session_name = session_name
        self.review_between = review_between
        self._commands = [self._resolve(cmd, placeholders) for cmd in commands]
        self._current_index = 0

    @staticmethod
    def _resolve(command: str, placeholders: dict[str, str]) -> str:
        result = command
        for key, value in placeholders.items():
            result = result.replace(f"{{{key}}}", value)
        return expand_skill_command(result)

    @property
    def current_prompt(self) -> str | None:
        if self._current_index < len(self._commands):
            return self._commands[self._current_index]
        return None

    def advance(self) -> str | None:
        self._current_index += 1
        return self.current_prompt

    def start_from(self, index: int):
        self._current_index = max(0, min(index, len(self._commands) - 1))

    @property
    def is_complete(self) -> bool:
        return self._current_index >= len(self._commands)

    @property
    def remaining_labels(self) -> list[str]:
        return self._commands[self._current_index:]

    @property
    def current_step(self) -> int:
        return self._current_index + 1

    @property
    def total_steps(self) -> int:
        return len(self._commands)

    @property
    def next_label(self) -> str | None:
        idx = self._current_index + 1
        if idx < len(self._commands):
            return self._commands[idx]
        return None
