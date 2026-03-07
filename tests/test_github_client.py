"""Tests for github_client.py — PR fetching, branch updates, repo listing."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from github_client import GitHubClient
from models import PRData


@pytest.fixture
def config():
    return Config(org="TestOrg", repos=["RepoA", "RepoB", "RepoC"])


@pytest.fixture
def client(config):
    return GitHubClient(config)


SAMPLE_PR_JSON = json.dumps([
    {
        "number": 42,
        "title": "Add feature X",
        "headRefName": "feat/KAN-10-feature-x",
        "url": "https://github.com/TestOrg/RepoA/pull/42",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "state": "OPEN",
        "statusCheckRollup": [
            {"name": "build", "conclusion": "SUCCESS", "status": "COMPLETED"},
            {"name": "test", "conclusion": "FAILURE", "status": "COMPLETED"},
        ],
        "author": {"login": "testuser"},
    },
    {
        "number": 43,
        "title": "Fix bug Y",
        "headRefName": "fix/bug-y",
        "url": "https://github.com/TestOrg/RepoA/pull/43",
        "mergeable": "CONFLICTING",
        "mergeStateStatus": "DIRTY",
        "reviewDecision": "CHANGES_REQUESTED",
        "state": "OPEN",
        "statusCheckRollup": None,
        "author": {"login": "otheruser"},
    },
])


class TestFetchPRs:
    @patch("github_client.subprocess.run")
    def test_parses_pr_data(self, mock_run, client):
        graphql_response = json.dumps({"data": {"repository": {
            "pr42": {"reviewThreads": {"nodes": [{"isResolved": False}]}},
            "pr43": {"reviewThreads": {"nodes": []}},
        }}})

        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_response, stderr="")
            return MagicMock(returncode=0, stdout=SAMPLE_PR_JSON, stderr="")

        mock_run.side_effect = side_effect

        prs = client.fetch_prs("RepoA")

        assert len(prs) == 2
        pr1 = prs[0]
        assert pr1.repo == "RepoA"
        assert pr1.number == 42
        assert pr1.title == "Add feature X"
        assert pr1.branch == "feat/KAN-10-feature-x"
        assert pr1.mergeable == "MERGEABLE"
        assert pr1.merge_state_status == "CLEAN"
        assert len(pr1.checks) == 2
        assert pr1.checks[0].name == "build"
        assert pr1.checks[1].conclusion == "FAILURE"
        assert pr1.unresolved_thread_count == 1

    @patch("github_client.subprocess.run")
    def test_handles_null_status_check_rollup(self, mock_run, client):
        graphql_empty = json.dumps({"data": {"repository": {
            "pr42": {"reviewThreads": {"nodes": []}},
            "pr43": {"reviewThreads": {"nodes": []}},
        }}})
        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_empty, stderr="")
            return MagicMock(returncode=0, stdout=SAMPLE_PR_JSON, stderr="")
        mock_run.side_effect = side_effect

        prs = client.fetch_prs("RepoA")
        pr2 = prs[1]
        assert pr2.checks == []
        assert pr2.mergeable == "CONFLICTING"

    @patch("github_client.subprocess.run")
    def test_handles_null_review_decision(self, mock_run, client):
        data = json.dumps([{
            "number": 1, "title": "T", "headRefName": "b", "url": "u",
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            "reviewDecision": None, "state": "OPEN", "statusCheckRollup": [],
        }])
        graphql_empty = json.dumps({"data": {"repository": {
            "pr1": {"reviewThreads": {"nodes": []}},
        }}})
        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_empty, stderr="")
            return MagicMock(returncode=0, stdout=data, stderr="")
        mock_run.side_effect = side_effect

        prs = client.fetch_prs("RepoA")
        assert prs[0].review_decision == ""

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_gh_error(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")
        prs = client.fetch_prs("RepoA")
        assert prs == []

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run, client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        prs = client.fetch_prs("RepoA")
        assert prs == []

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_invalid_json(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        prs = client.fetch_prs("RepoA")
        assert prs == []

    @patch("github_client.subprocess.run")
    def test_parses_author_login(self, mock_run, client):
        graphql_response = json.dumps({"data": {"repository": {
            "pr42": {"reviewThreads": {"nodes": []}},
            "pr43": {"reviewThreads": {"nodes": []}},
        }}})
        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_response, stderr="")
            return MagicMock(returncode=0, stdout=SAMPLE_PR_JSON, stderr="")
        mock_run.side_effect = side_effect

        prs = client.fetch_prs("RepoA")
        assert prs[0].author == "testuser"
        assert prs[1].author == "otheruser"

    @patch("github_client.subprocess.run")
    def test_calls_gh_with_correct_args(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        client.fetch_prs("RepoA")

        args = mock_run.call_args[0][0]
        assert args[0] == "gh"
        assert args[1] == "pr"
        assert "--repo" in args
        assert "TestOrg/RepoA" in args
        assert "--author" in args
        assert "@me" in args


class TestFetchUnresolvedThreads:
    @patch("github_client.subprocess.run")
    def test_counts_unresolved_threads(self, mock_run, client):
        graphql_response = json.dumps({"data": {"repository": {
            "pr10": {"reviewThreads": {"nodes": [
                {"isResolved": False},
                {"isResolved": True},
                {"isResolved": False},
            ]}},
            "pr20": {"reviewThreads": {"nodes": [
                {"isResolved": True},
            ]}},
        }}})
        mock_run.return_value = MagicMock(returncode=0, stdout=graphql_response, stderr="")

        counts = client._fetch_unresolved_threads("RepoA", [10, 20])
        assert counts == {10: 2, 20: 0}

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        counts = client._fetch_unresolved_threads("RepoA", [10])
        assert counts == {}

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run, client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        counts = client._fetch_unresolved_threads("RepoA", [10])
        assert counts == {}

    @patch("github_client.subprocess.run")
    def test_graceful_with_missing_pr_data(self, mock_run, client):
        graphql_response = json.dumps({"data": {"repository": {}}})
        mock_run.return_value = MagicMock(returncode=0, stdout=graphql_response, stderr="")
        counts = client._fetch_unresolved_threads("RepoA", [10])
        assert counts == {10: 0}


class TestFetchAllPRs:
    @patch("github_client.subprocess.run")
    def test_fetches_from_all_repos(self, mock_run, client):
        """Should fetch from all repos in config."""
        graphql_empty = json.dumps({"data": {"repository": {
            "pr1": {"reviewThreads": {"nodes": []}},
        }}})
        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_empty, stderr="")
            repo = cmd[cmd.index("--repo") + 1].split("/")[1]
            pr = {"number": 1, "title": f"PR in {repo}", "headRefName": "b",
                   "url": "u", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                   "reviewDecision": "", "state": "OPEN", "statusCheckRollup": []}
            return MagicMock(returncode=0, stdout=json.dumps([pr]), stderr="")

        mock_run.side_effect = side_effect
        prs = client.fetch_all_prs()
        assert len(prs) == 3  # RepoA, RepoB, RepoC

    @patch("github_client.subprocess.run")
    def test_handles_partial_failures(self, mock_run, client):
        """If one repo fails, others still return."""
        call_count = 0
        graphql_empty = json.dumps({"data": {"repository": {
            "pr1": {"reviewThreads": {"nodes": []}},
        }}})

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_empty, stderr="")
            call_count += 1
            if call_count == 1:
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout=json.dumps([{
                "number": 1, "title": "T", "headRefName": "b", "url": "u",
                "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                "reviewDecision": "", "state": "OPEN", "statusCheckRollup": [],
            }]), stderr="")

        mock_run.side_effect = side_effect
        prs = client.fetch_all_prs()
        assert len(prs) == 2  # 1 failed, 2 succeeded


class TestUpdateBranch:
    @patch("github_client.subprocess.run")
    def test_success(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert client.update_branch("RepoA", 42) is True

    @patch("github_client.subprocess.run")
    def test_calls_correct_api(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        client.update_branch("RepoA", 42)

        args = mock_run.call_args[0][0]
        assert "repos/TestOrg/RepoA/pulls/42/update-branch" in args
        assert "-X" in args
        assert "PUT" in args

    @patch("github_client.subprocess.run")
    def test_failure_returns_false(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="conflict")
        assert client.update_branch("RepoA", 42) is False

    @patch("github_client.subprocess.run")
    def test_timeout_returns_false(self, mock_run, client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        assert client.update_branch("RepoA", 42) is False


class TestListRepos:
    @patch("github_client.subprocess.run")
    def test_returns_sorted_names(self, mock_run, client):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"name": "Zeta"}, {"name": "Alpha"}, {"name": "Beta"}]),
            stderr="",
        )
        repos = client.list_repos("TestOrg")
        assert repos == ["Alpha", "Beta", "Zeta"]

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_error(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        assert client.list_repos("BadOrg") == []

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_timeout(self, mock_run, client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        assert client.list_repos("TestOrg") == []


class TestGetCurrentUser:
    @patch("github_client.subprocess.run")
    def test_returns_login(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="nickle799\n", stderr="")
        assert client.get_current_user() == "nickle799"

    @patch("github_client.subprocess.run")
    def test_caches_result(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="nickle799\n", stderr="")
        client.get_current_user()
        client.get_current_user()
        assert mock_run.call_count == 1

    @patch("github_client.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert client.get_current_user() == ""


class TestFetchTeamPRs:
    @patch("github_client.subprocess.run")
    def test_fetches_without_author_filter(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
        client.fetch_team_prs("RepoA")
        args = mock_run.call_args[0][0]
        assert "--author" not in args

    @patch("github_client.subprocess.run")
    def test_parses_author(self, mock_run, client):
        graphql_response = json.dumps({"data": {"repository": {
            "pr42": {"reviewThreads": {"nodes": []}},
            "pr43": {"reviewThreads": {"nodes": []}},
        }}})
        def side_effect(cmd, **kwargs):
            if "graphql" in cmd:
                return MagicMock(returncode=0, stdout=graphql_response, stderr="")
            return MagicMock(returncode=0, stdout=SAMPLE_PR_JSON, stderr="")
        mock_run.side_effect = side_effect

        prs = client.fetch_team_prs("RepoA")
        assert len(prs) == 2
        assert prs[0].author == "testuser"


class TestHasReviewComment:
    @patch("github_client.subprocess.run")
    def test_returns_true_when_marker_present(self, mock_run, client):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Some comment\nReviewing by Claude Local\nAnother", stderr=""
        )
        assert client.has_review_comment("RepoA", 42) is True

    @patch("github_client.subprocess.run")
    def test_returns_false_when_no_marker(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="Some comment\n", stderr="")
        assert client.has_review_comment("RepoA", 42) is False

    @patch("github_client.subprocess.run")
    def test_returns_false_on_failure(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert client.has_review_comment("RepoA", 42) is False


class TestPostReviewComment:
    @patch("github_client.subprocess.run")
    def test_success(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        assert client.post_review_comment("RepoA", 42) is True

    @patch("github_client.subprocess.run")
    def test_failure(self, mock_run, client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        assert client.post_review_comment("RepoA", 42) is False
