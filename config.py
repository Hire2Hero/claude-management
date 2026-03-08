"""Configuration management for Claude Management GUI."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json")
DEFAULT_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


@dataclass
class WorkflowConfig:
    commands: list[str] = field(default_factory=list)
    review_between: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WorkflowConfig:
        return cls(
            commands=data.get("commands", []),
            review_between=data.get("review_between", False),
        )


@dataclass
class TriageConfig:
    description: str = ""
    skill_name: str = ""
    placeholder: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> TriageConfig:
        return cls(
            description=data.get("description", ""),
            skill_name=data.get("skill_name", ""),
            placeholder=data.get("placeholder", ""),
        )


@dataclass
class SkillsConfig:
    work_ticket: WorkflowConfig = field(default_factory=WorkflowConfig)
    review_pr: WorkflowConfig = field(default_factory=WorkflowConfig)
    fix_pr: WorkflowConfig = field(
        default_factory=lambda: WorkflowConfig(commands=["/builtin:fix-pr {pr_url}"])
    )
    triages: list[TriageConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "work_ticket": self.work_ticket.to_dict(),
            "review_pr": self.review_pr.to_dict(),
            "fix_pr": self.fix_pr.to_dict(),
            "triages": [t.to_dict() for t in self.triages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> SkillsConfig:
        return cls(
            work_ticket=WorkflowConfig.from_dict(data["work_ticket"])
            if "work_ticket" in data else WorkflowConfig(),
            review_pr=WorkflowConfig.from_dict(data["review_pr"])
            if "review_pr" in data else WorkflowConfig(),
            fix_pr=WorkflowConfig.from_dict(data["fix_pr"])
            if "fix_pr" in data else WorkflowConfig(commands=["/builtin:fix-pr {pr_url}"]),
            triages=[TriageConfig.from_dict(t) for t in data.get("triages", [])],
        )


@dataclass
class Config:
    org: str = ""
    repos: list[str] = field(default_factory=list)
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_board_id: int = 0
    jira_board_name: str = ""
    slack_webhook_url: str = ""
    slack_mode: str = "webhook"  # "webhook" or "mcp"
    slack_channel: str = ""
    base_dir: str = DEFAULT_BASE_DIR
    claude_projects_dir: str = DEFAULT_CLAUDE_PROJECTS_DIR
    poll_interval: int = 300
    cooldown: int = 1800
    claude_oauth_token: str = ""
    dangerously_skip_permissions: bool = False
    ignored_checks: list[str] = field(default_factory=lambda: [
        "notify / notify",
        "call-review / code-review",
        "sync / sync-jira",
    ])
    skills: SkillsConfig = field(default_factory=SkillsConfig)

    @classmethod
    def load(cls, path: str) -> Optional[Config]:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            skills_data = data.get("skills", {})
            return cls(
                org=data.get("org", ""),
                repos=data.get("repos", []),
                jira_base_url=data.get("jira_base_url", ""),
                jira_email=data.get("jira_email", ""),
                jira_api_token=data.get("jira_api_token", ""),
                jira_board_id=data.get("jira_board_id", 0),
                jira_board_name=data.get("jira_board_name", ""),
                slack_webhook_url=data.get("slack_webhook_url", ""),
                slack_mode=data.get("slack_mode", "webhook"),
                slack_channel=data.get("slack_channel", ""),
                base_dir=data.get("base_dir", DEFAULT_BASE_DIR),
                claude_projects_dir=data.get("claude_projects_dir", DEFAULT_CLAUDE_PROJECTS_DIR),
                claude_oauth_token=data.get("claude_oauth_token", ""),
                poll_interval=data.get("poll_interval", 300),
                cooldown=data.get("cooldown", 1800),
                dangerously_skip_permissions=data.get("dangerously_skip_permissions", False),
                ignored_checks=data.get("ignored_checks", [
                    "notify / notify",
                    "call-review / code-review",
                    "sync / sync-jira",
                ]),
                skills=SkillsConfig.from_dict(skills_data) if skills_data else SkillsConfig(),
            )
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, path: str):
        data = asdict(self)
        data["skills"] = self.skills.to_dict()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    @classmethod
    def bootstrap(cls) -> Config:
        """Pre-fill config from defaults.json if present."""
        config = cls()
        if os.path.exists(DEFAULTS_PATH):
            try:
                with open(DEFAULTS_PATH, "r") as f:
                    defaults = json.load(f)
                for key, val in defaults.items():
                    if key == "skills" and isinstance(val, dict):
                        config.skills = SkillsConfig.from_dict(val)
                    elif hasattr(config, key) and val:
                        setattr(config, key, val)
            except (json.JSONDecodeError, OSError):
                pass
        return config
