"""Jira REST API client using urllib (no external dependencies)."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from config import Config

log = logging.getLogger("claude_mgmt")


@dataclass
class JiraBoard:
    id: int
    name: str
    project_key: str
    favourite: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> JiraBoard:
        project = data.get("location", {})
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            project_key=project.get("projectKey", ""),
            favourite=data.get("favourite", False),
        )


@dataclass
class JiraTicket:
    key: str
    summary: str
    status: str
    assignee: str

    @classmethod
    def from_dict(cls, data: dict) -> JiraTicket:
        fields = data.get("fields", {})
        status_obj = fields.get("status", {})
        assignee_obj = fields.get("assignee") or {}
        return cls(
            key=data["key"],
            summary=fields.get("summary", ""),
            status=status_obj.get("name", ""),
            assignee=assignee_obj.get("displayName", "Unassigned"),
        )


class JiraClient:
    """Thin wrapper around Jira REST APIs (Cloud)."""

    def __init__(self, config: Config):
        self._config = config

    @property
    def _site_url(self) -> str:
        """Derive site root from jira_base_url (strips /browse suffix)."""
        url = self._config.jira_base_url.rstrip("/")
        if url.endswith("/browse"):
            url = url[: -len("/browse")]
        return url

    def _request(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """Authenticated GET returning parsed JSON, or None on error."""
        url = self._site_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        credentials = f"{self._config.jira_email}:{self._config.jira_api_token}"
        auth = base64.b64encode(credentials.encode()).decode()

        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            log.warning("Jira API %s returned %d", path, e.code)
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            log.warning("Jira API %s error: %s", path, e)
            return None

    def test_auth(self) -> Optional[str]:
        """Test credentials. Returns display name on success, None on failure."""
        if not self._config.jira_email or not self._config.jira_api_token:
            return None
        data = self._request("/rest/api/3/myself")
        if data:
            return data.get("displayName")
        return None

    def list_boards(self) -> list[JiraBoard]:
        """Fetch all boards, handling pagination."""
        boards: list[JiraBoard] = []
        start_at = 0
        while True:
            data = self._request("/rest/agile/1.0/board", {"startAt": start_at, "maxResults": 50})
            if not data:
                break
            for item in data.get("values", []):
                boards.append(JiraBoard.from_dict(item))
            if data.get("isLast", True):
                break
            start_at += len(data.get("values", []))
        boards.sort(key=lambda b: (not b.favourite, b.name.lower()))
        return boards

    def fetch_board_issues(self, board_id: int) -> list[JiraTicket]:
        """Fetch To Do / In Progress issues for a board."""
        jql = 'status in ("To Do","In Progress")'
        data = self._request(
            f"/rest/agile/1.0/board/{board_id}/issue",
            {"jql": jql, "fields": "summary,status,assignee", "maxResults": 100},
        )
        if not data:
            return []
        return [JiraTicket.from_dict(issue) for issue in data.get("issues", [])]


# ── Static helpers for wizard (before Config is finalized) ──────────────────


def check_credentials(site_url: str, email: str, token: str) -> Optional[str]:
    """Test Jira credentials without a full Config. Returns display name or None."""
    cfg = Config(jira_base_url=site_url, jira_email=email, jira_api_token=token)
    return JiraClient(cfg).test_auth()


def fetch_all_boards(site_url: str, email: str, token: str) -> list[JiraBoard]:
    """Fetch all boards without a full Config."""
    cfg = Config(jira_base_url=site_url, jira_email=email, jira_api_token=token)
    return JiraClient(cfg).list_boards()
