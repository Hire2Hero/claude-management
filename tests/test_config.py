"""Tests for config.py — load, save, bootstrap."""

import json
import os
import tempfile

import pytest

from config import Config


class TestConfigLoad:
    def test_load_valid_config(self, tmp_path):
        path = tmp_path / "config.json"
        data = {
            "org": "TestOrg",
            "repos": ["RepoA", "RepoB"],
            "jira_base_url": "https://test.atlassian.net/browse",
            "jira_email": "user@test.com",
            "jira_api_token": "tok123",
            "jira_board_id": 1,
            "jira_board_name": "KAN Board",
            "slack_webhook_url": "https://hooks.slack.com/services/T123/B456/xxx",
            "base_dir": "/tmp/test",
            "claude_projects_dir": "/tmp/projects",
            "poll_interval": 600,
            "cooldown": 3600,
            "ignored_checks": ["check1"],
        }
        path.write_text(json.dumps(data))

        config = Config.load(str(path))
        assert config is not None
        assert config.org == "TestOrg"
        assert config.repos == ["RepoA", "RepoB"]
        assert config.jira_email == "user@test.com"
        assert config.jira_api_token == "tok123"
        assert config.jira_board_id == 1
        assert config.jira_board_name == "KAN Board"
        assert config.poll_interval == 600
        assert config.cooldown == 3600
        assert config.ignored_checks == ["check1"]

    def test_load_missing_file(self, tmp_path):
        config = Config.load(str(tmp_path / "nonexistent.json"))
        assert config is None

    def test_load_corrupt_json(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("not valid json {{{")
        config = Config.load(str(path))
        assert config is None

    def test_load_partial_config_uses_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"org": "MyOrg"}))

        config = Config.load(str(path))
        assert config is not None
        assert config.org == "MyOrg"
        assert config.repos == []
        assert config.jira_board_id == 0  # default
        assert config.poll_interval == 300  # default
        assert config.cooldown == 1800  # default


class TestConfigSave:
    def test_save_creates_file(self, tmp_path):
        path = str(tmp_path / "config.json")
        config = Config(org="SaveOrg", repos=["R1"])
        config.save(path)

        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["org"] == "SaveOrg"
        assert data["repos"] == ["R1"]

    def test_save_is_atomic(self, tmp_path):
        """Save uses tmp file + os.replace, so no partial writes."""
        path = str(tmp_path / "config.json")
        config = Config(org="Atomic")
        config.save(path)

        # tmp file should not exist after save
        assert not os.path.exists(path + ".tmp")
        assert os.path.exists(path)

    def test_save_roundtrip(self, tmp_path):
        path = str(tmp_path / "config.json")
        original = Config(
            org="RoundTrip",
            repos=["A", "B"],
            jira_base_url="https://jira.test",
            jira_email="me@jira.test",
            jira_api_token="secret",
            jira_board_id=7,
            jira_board_name="Sprint Board",
            slack_webhook_url="https://hooks.slack.com/services/T123/B456/xxx",
            poll_interval=120,
            cooldown=900,
        )
        original.save(path)
        loaded = Config.load(path)

        assert loaded is not None
        assert loaded.org == original.org
        assert loaded.repos == original.repos
        assert loaded.jira_base_url == original.jira_base_url
        assert loaded.jira_email == original.jira_email
        assert loaded.jira_api_token == original.jira_api_token
        assert loaded.jira_board_id == original.jira_board_id
        assert loaded.jira_board_name == original.jira_board_name
        assert loaded.slack_webhook_url == original.slack_webhook_url
        assert loaded.poll_interval == original.poll_interval
        assert loaded.cooldown == original.cooldown


class TestConfigBootstrap:
    def test_bootstrap_reads_defaults(self, tmp_path, monkeypatch):
        defaults_path = str(tmp_path / "defaults.json")
        defaults_data = {
            "org": "TestOrg",
            "repos": ["Repo1", "Repo2"],
            "jira_base_url": "https://test.atlassian.net",
            "slack_webhook_url": "https://hooks.slack.com/services/T999/B999/yyy",
            "base_dir": "/custom/base",
        }
        with open(defaults_path, "w") as f:
            json.dump(defaults_data, f)

        monkeypatch.setattr("config.DEFAULTS_PATH", defaults_path)
        config = Config.bootstrap()

        assert config.org == "TestOrg"
        assert config.repos == ["Repo1", "Repo2"]
        assert config.jira_base_url == "https://test.atlassian.net"
        assert config.slack_webhook_url == "https://hooks.slack.com/services/T999/B999/yyy"
        assert config.base_dir == "/custom/base"

    def test_bootstrap_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.DEFAULTS_PATH", str(tmp_path / "no_defaults.json"))
        config = Config.bootstrap()
        assert config.org == ""
        assert config.repos == []

    def test_bootstrap_corrupt_file(self, tmp_path, monkeypatch):
        defaults_path = str(tmp_path / "defaults.json")
        with open(defaults_path, "w") as f:
            f.write("bad json")
        monkeypatch.setattr("config.DEFAULTS_PATH", defaults_path)
        config = Config.bootstrap()
        assert config.org == ""

    def test_bootstrap_partial_data(self, tmp_path, monkeypatch):
        defaults_path = str(tmp_path / "defaults.json")
        with open(defaults_path, "w") as f:
            json.dump({"org": "PartialOrg"}, f)
        monkeypatch.setattr("config.DEFAULTS_PATH", defaults_path)
        config = Config.bootstrap()
        assert config.org == "PartialOrg"
        assert config.repos == []
        assert config.jira_base_url == ""
