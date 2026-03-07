"""Tests for jira_client.py — auth, boards, tickets."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from jira_client import JiraBoard, JiraClient, JiraTicket, fetch_all_boards, check_credentials


def _make_config(**overrides) -> Config:
    defaults = {
        "jira_base_url": "https://myorg.atlassian.net",
        "jira_email": "user@example.com",
        "jira_api_token": "tok123",
    }
    defaults.update(overrides)
    return Config(**defaults)


def _fake_response(data: dict, status: int = 200) -> MagicMock:
    body = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestSiteUrl:
    def test_strips_browse(self):
        cfg = _make_config(jira_base_url="https://myorg.atlassian.net/browse")
        assert JiraClient(cfg)._site_url == "https://myorg.atlassian.net"

    def test_strips_browse_with_trailing_slash(self):
        cfg = _make_config(jira_base_url="https://myorg.atlassian.net/browse/")
        assert JiraClient(cfg)._site_url == "https://myorg.atlassian.net"

    def test_no_browse_unchanged(self):
        cfg = _make_config(jira_base_url="https://myorg.atlassian.net")
        assert JiraClient(cfg)._site_url == "https://myorg.atlassian.net"

    def test_trailing_slash_stripped(self):
        cfg = _make_config(jira_base_url="https://myorg.atlassian.net/")
        assert JiraClient(cfg)._site_url == "https://myorg.atlassian.net"


class TestTestAuth:
    @patch("jira_client.urllib.request.urlopen")
    def test_success_returns_name(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"displayName": "Jane Doe"})
        client = JiraClient(_make_config())
        assert client.test_auth() == "Jane Doe"

    @patch("jira_client.urllib.request.urlopen")
    def test_401_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=401, msg="Unauthorized", hdrs={}, fp=io.BytesIO(b""),
        )
        client = JiraClient(_make_config())
        assert client.test_auth() is None

    @patch("jira_client.urllib.request.urlopen")
    def test_network_error_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        client = JiraClient(_make_config())
        assert client.test_auth() is None

    def test_unconfigured_returns_none(self):
        cfg = _make_config(jira_email="", jira_api_token="")
        assert JiraClient(cfg).test_auth() is None


class TestListBoards:
    @patch("jira_client.urllib.request.urlopen")
    def test_parses_boards(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({
            "values": [
                {"id": 1, "name": "Dev Board", "location": {"projectKey": "DEV"}},
                {"id": 2, "name": "QA Board", "location": {"projectKey": "QA"}},
            ],
            "isLast": True,
        })
        boards = JiraClient(_make_config()).list_boards()
        assert len(boards) == 2
        assert boards[0] == JiraBoard(id=1, name="Dev Board", project_key="DEV")
        assert boards[1] == JiraBoard(id=2, name="QA Board", project_key="QA")

    @patch("jira_client.urllib.request.urlopen")
    def test_handles_pagination(self, mock_urlopen):
        page1 = _fake_response({
            "values": [{"id": 1, "name": "Board 1", "location": {"projectKey": "P1"}}],
            "isLast": False,
        })
        page2 = _fake_response({
            "values": [{"id": 2, "name": "Board 2", "location": {"projectKey": "P2"}}],
            "isLast": True,
        })
        mock_urlopen.side_effect = [page1, page2]

        boards = JiraClient(_make_config()).list_boards()
        assert len(boards) == 2
        assert boards[0].id == 1
        assert boards[1].id == 2

    @patch("jira_client.urllib.request.urlopen")
    def test_error_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        assert JiraClient(_make_config()).list_boards() == []


class TestFetchBoardIssues:
    @patch("jira_client.urllib.request.urlopen")
    def test_parses_tickets(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({
            "issues": [
                {"key": "KAN-1", "fields": {"summary": "Fix bug", "status": {"name": "To Do"},
                                             "assignee": {"displayName": "Alice"}}},
                {"key": "KAN-2", "fields": {"summary": "Add feature", "status": {"name": "In Progress"},
                                             "assignee": None}},
            ],
        })
        tickets = JiraClient(_make_config()).fetch_board_issues(1)
        assert len(tickets) == 2
        assert tickets[0] == JiraTicket(key="KAN-1", summary="Fix bug", status="To Do", assignee="Alice")
        assert tickets[1] == JiraTicket(key="KAN-2", summary="Add feature", status="In Progress", assignee="Unassigned")

    @patch("jira_client.urllib.request.urlopen")
    def test_sends_correct_jql(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"issues": []})
        JiraClient(_make_config()).fetch_board_issues(42)

        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert "/rest/agile/1.0/board/42/issue?" in request.full_url
        assert "status" in request.full_url
        assert "To+Do" in request.full_url or "To%20Do" in request.full_url

    @patch("jira_client.urllib.request.urlopen")
    def test_error_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="", code=500, msg="Server Error", hdrs={}, fp=io.BytesIO(b""),
        )
        assert JiraClient(_make_config()).fetch_board_issues(1) == []


class TestStaticHelpers:
    @patch("jira_client.urllib.request.urlopen")
    def test_check_credentials(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"displayName": "Test User"})
        assert check_credentials("https://x.atlassian.net", "a@b.com", "tok") == "Test User"

    @patch("jira_client.urllib.request.urlopen")
    def test_fetch_all_boards(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({
            "values": [{"id": 5, "name": "B", "location": {"projectKey": "P"}}],
            "isLast": True,
        })
        boards = fetch_all_boards("https://x.atlassian.net", "a@b.com", "tok")
        assert len(boards) == 1
        assert boards[0].id == 5
