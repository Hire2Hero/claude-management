# Claude Management

Tkinter desktop app for managing Claude CLI working sessions and monitoring GitHub PRs across an organization.

## Features

- **PR Monitor** — polls GitHub for open PRs, shows CI status, merge conflicts, and review state. One-click branch update, Claude fix launch, and Slack review requests.
- **Working Sessions** — create and manage embedded Claude CLI sessions with streaming output. Chat history persists across panel close/reopen and app restarts.
- **Jira Integration** — start sessions from Jira tickets, link PRs to tickets.
- **Setup Wizard** — guided first-run configuration for GitHub org, Jira credentials, and Slack webhook.

## Prerequisites

- **Python 3.10+** with **tkinter** (Tk 8.6+)
- [GitHub CLI](https://cli.github.com/) (`gh`) — authenticated
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — installed and authenticated

## Installation (macOS)

Homebrew is the recommended way to get Python with tkinter on macOS. Pyenv builds Python from source and requires manual Tcl/Tk configuration to get tkinter working, which is error-prone.

```bash
# Install Python and tkinter
brew install python@3.13 python-tk@3.13

# Clone the repo
gh repo clone Hire2Hero/claude-management
cd claude-management

# Install Python dependencies
/opt/homebrew/bin/python3.13 -m pip install -r requirements.txt --break-system-packages

# Run
/opt/homebrew/bin/python3.13 main.py
```

If `python3` on your PATH is already 3.10+ with tkinter, you can just run:

```bash
pip install -r requirements.txt
python3 main.py
```

## First Run

On first launch, a setup wizard walks you through configuration:

1. **GitHub** — org name, repos to monitor
2. **Jira** — site URL, email, API token, board selection
3. **Slack** — webhook URL for review notifications
4. **Paths** — base directory for repos, Claude projects directory

Config is saved to `config.json` (gitignored). Re-run setup anytime via **File > Re-run Setup**.

## Reset

To wipe config and state and start fresh:

```bash
python3.13 main.py --reset
```

This removes `config.json` and `state.json`. Run again without `--reset` to re-enter the setup wizard.

## Tests

```bash
python3.13 -m pytest tests/ -v
```

## Project Structure

```
main.py                 Entry point and Application class
config.py               Config dataclass, load/save/bootstrap
models.py               PRData, ManagedSession, enums, helpers
session_manager.py      Session registry and state persistence
claude_process.py       Claude session manager (claude-code-sdk)
history.py              Per-session chat history (memory + disk)
github_client.py        GitHub API via gh CLI
jira_client.py          Jira REST API client
pr_monitor.py           Background PR polling thread
slack_client.py         Slack webhook sender
terminal.py             iTerm2 terminal launcher
tabs/
  pr_tab.py             Pull Requests tab
  session_tab.py        Working Sessions tab
widgets/
  chat_panel.py         Embedded chat panel with streaming
  dialogs.py            New Session / Start Ticket dialogs
  setup_wizard.py       First-run configuration wizard
tests/                  pytest test suite
```

## Runtime Files (gitignored)

| File | Purpose |
|------|---------|
| `config.json` | User configuration (credentials, org, repos) |
| `defaults.json` | Optional pre-fill defaults for setup wizard |
| `state.json` | Tracked PRs and session registry |
| `session_history/` | Per-session chat history (JSON files) |
| `claude_management.log` | Application log |
