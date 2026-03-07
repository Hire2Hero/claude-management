#!/usr/bin/env python3
"""Claude Management GUI — entry point."""

from __future__ import annotations

import sys

MIN_PYTHON = (3, 10)
if sys.version_info < MIN_PYTHON:
    print(
        f"Error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required "
        f"(found {sys.version_info.major}.{sys.version_info.minor}).\n"
        f"Install a newer Python, e.g.: brew install python@3.13 python-tk@3.13\n"
        f"Then run: /opt/homebrew/bin/python3.13 {__file__}",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    import tkinter as tk
except ImportError:
    print(
        "Error: tkinter is not available.\n"
        "Install it, e.g.: brew install python-tk@3.13\n"
        f"Then run: /opt/homebrew/bin/python3.13 {__file__}",
        file=sys.stderr,
    )
    sys.exit(1)

# Check Tk version — 8.6+ needed for macOS compatibility
_tk_ver = float(tk.TkVersion)
if _tk_ver < 8.6:
    print(
        f"Error: Tk {_tk_ver} is too old (8.6+ required).\n"
        "Install a newer Python with Tk, e.g.: brew install python@3.13 python-tk@3.13\n"
        f"Then run: /opt/homebrew/bin/python3.13 {__file__}",
        file=sys.stderr,
    )
    sys.exit(1)

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from tkinter import ttk, messagebox

from claude_process import ClaudeProcess
from config import Config, DEFAULTS_PATH
from database import Database
from github_client import GitHubClient
from history import SessionHistoryStore
from models import ManagedSession, PRData, SessionStatus, extract_ticket_id
from pr_monitor import PRMonitorThread, Prompts
from session_manager import SessionManager
from summary_logger import SummaryLogger
from tabs.pr_review_tab import (
    PRReviewTab, STATUS_PENDING, STATUS_REVIEWING, STATUS_REVIEWED,
    STATUS_REVIEWED_BY_ME, STATUS_REVIEWED_BY_CLAUDE,
)
from tabs.pr_tab import PRTab
from tabs.session_tab import SessionTab
from terminal import TerminalLauncher
from skill_runner import SkillRunner
from widgets.dialogs import EditReposDialog, NewSessionDialog, SkillSelectionDialog, StartTicketDialog
from widgets.setup_wizard import SetupWizard

log = logging.getLogger("claude_mgmt")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DB_PATH = os.path.join(APP_DIR, "data.db")

SUMMARY_INSTRUCTIONS = (
    "\n\n---\n"
    "IMPORTANT: Throughout this session, follow these practices:\n"
    "1. Before making changes, briefly explain your reasoning and the approach you chose.\n"
    "2. After completing each major step, provide a short progress summary.\n"
    "3. If you make a decision between alternatives, explain why.\n"
    "4. At the end, provide a final summary of all changes made."
)


class Application:
    def __init__(self):
        self._setup_logging()
        self.root = tk.Tk()
        self.root.title("Claude Management")
        self.root.geometry("1100x700")
        self.root.minsize(800, 500)

        self.ui_queue: queue.Queue = queue.Queue()
        self.config: Config | None = None
        self.gh: GitHubClient | None = None
        self.terminal: TerminalLauncher | None = None
        self.session_mgr: SessionManager | None = None
        self.monitor: PRMonitorThread | None = None

        # Active Claude processes keyed by session name
        self._claude_processes: dict[str, ClaudeProcess] = {}
        self._session_gen: dict[str, int] = {}  # Generation counter per session name
        # Accumulate tool input JSON per session for display summaries
        self._tool_input_buf: dict[str, dict] = {}  # name -> {"tool": str, "json": str}
        self._summary_text_buf: dict[str, str] = {}
        # PR Review tab state
        self._review_statuses: dict[str, str] = {}
        # Sessions waiting for user input
        self._needs_attention: set[str] = set()
        # Active skill runners keyed by session name
        self._skill_runners: dict[str, SkillRunner] = {}

        # Initialize SQLite database
        self._db = Database(DB_PATH)

        # Auto-migrate from old file-based storage
        self._auto_migrate()

        # Per-session chat history
        self._history_store = SessionHistoryStore(self._db)
        # Per-session summary logger
        self._summary_logger = SummaryLogger(self._db)

        # Check prerequisites
        if not self._check_prerequisites():
            return

        # Load or create config
        self.config = Config.load(CONFIG_PATH)
        if self.config is None:
            if not self._run_setup_wizard():
                self.root.destroy()
                return

        # Initialize services
        self.gh = GitHubClient(self.config)
        self.terminal = TerminalLauncher(self.config)
        self.session_mgr = SessionManager(self.config, self._db)

        # Build UI
        self._build_menu()
        self._build_notebook()
        self._build_status_bar()

        # Start background monitor
        self.monitor = PRMonitorThread(
            config=self.config,
            gh=self.gh,
            terminal=self.terminal,
            session_mgr=self.session_mgr,
            db=self._db,
            ui_queue=self.ui_queue,
        )
        self.monitor.start()

        # Refresh statuses immediately so stale PIDs from a previous run
        # are marked as stopped before the UI first renders
        self.session_mgr.refresh_statuses()
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)

        # Fetch team PRs for review tab on startup
        self._handle_review_refresh()

        # Schedule UI queue processing and session refresh
        self.root.after(100, self._process_ui_queue)
        self.root.after(10_000, self._refresh_sessions)


    def _setup_logging(self):
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        log.setLevel(logging.INFO)
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        log.addHandler(console)

        log_file = os.path.join(APP_DIR, "claude_management.log")
        fh = logging.FileHandler(log_file, mode="a")
        fh.setFormatter(formatter)
        log.addHandler(fh)

    def _auto_migrate(self):
        """Migrate data from old file-based storage into SQLite on first run."""
        state_path = os.path.join(APP_DIR, "state.json")
        history_dir = os.path.join(APP_DIR, "session_history")
        summaries_dir = os.path.join(APP_DIR, "session_summaries")

        has_old = (
            os.path.exists(state_path)
            or os.path.exists(history_dir)
            or os.path.exists(summaries_dir)
        )
        if not has_old:
            return

        # Check if DB already has data (migration already done)
        row = self._db.fetchone("SELECT COUNT(*) as c FROM sessions")
        if row and row["c"] > 0:
            return

        log.info("Auto-migrating from file-based storage to SQLite...")

        # Build a slug-to-name mapping from state.json sessions
        slug_to_name: dict[str, str] = {}

        # Migrate state.json → sessions + tracked_prs
        if os.path.exists(state_path):
            try:
                with open(state_path, "r") as f:
                    state = json.load(f)

                for key, val in state.get("sessions", {}).items():
                    slug_to_name[re.sub(r"[^\w\-]", "_", key)] = key
                    s = ManagedSession.from_dict(val)
                    self._db.execute(
                        "INSERT OR IGNORE INTO sessions "
                        "(name, repo, pid, session_id, status, created_at, ticket_id, cwd) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (s.name, s.repo, s.pid, s.session_id, s.status.value,
                         s.created_at, s.ticket_id, s.cwd),
                    )

                from models import TrackedPR
                for key, val in state.get("tracked_prs", {}).items():
                    pr = TrackedPR.from_dict(val)
                    self._db.execute(
                        "INSERT OR IGNORE INTO tracked_prs "
                        "(key, repo, number, branch, last_issue, last_action, "
                        "last_action_time, claude_pid, ci_was_failing, slack_sent) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, pr.repo, pr.number, pr.branch, pr.last_issue,
                         pr.last_action, pr.last_action_time, pr.claude_pid,
                         int(pr.ci_was_failing), int(pr.slack_sent)),
                    )

                os.rename(state_path, state_path + ".migrated")
                log.info("Migrated state.json → data.db")
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to migrate state.json: %s", e)

        # Migrate session_history/<slug>.json → session_history table
        if os.path.exists(history_dir):
            try:
                for fname in os.listdir(history_dir):
                    if not fname.endswith(".json"):
                        continue
                    slug = fname[:-5]  # Remove .json
                    session_name = slug_to_name.get(slug, slug)
                    fpath = os.path.join(history_dir, fname)
                    try:
                        with open(fpath, "r") as f:
                            entries = json.load(f)
                        if isinstance(entries, list):
                            self._db.executemany(
                                "INSERT INTO session_history (session_name, tag, text) "
                                "VALUES (?, ?, ?)",
                                [(session_name, e[0], e[1]) for e in entries if len(e) >= 2],
                            )
                    except (json.JSONDecodeError, OSError) as e:
                        log.warning("Failed to migrate history file %s: %s", fname, e)

                os.rename(history_dir, history_dir + ".migrated")
                log.info("Migrated session_history/ → data.db")
            except OSError as e:
                log.warning("Failed to migrate session_history: %s", e)

        # Migrate session_summaries/<slug>.md → session_summaries table
        if os.path.exists(summaries_dir):
            try:
                for fname in os.listdir(summaries_dir):
                    if not fname.endswith(".md"):
                        continue
                    slug = fname[:-3]  # Remove .md
                    session_name = slug_to_name.get(slug, slug)
                    fpath = os.path.join(summaries_dir, fname)
                    try:
                        with open(fpath, "r") as f:
                            content = f.read()
                        # Parse entries: split on "---" separator, parse headings
                        for block in content.split("\n---\n"):
                            block = block.strip()
                            if not block:
                                continue
                            # Extract heading: ## YYYY-MM-DD HH:MM:SS — EntryType
                            match = re.match(
                                r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) — ([^\n]+)\n?(.*)",
                                block, re.DOTALL,
                            )
                            if match:
                                created_at = match.group(1)
                                entry_type = match.group(2).strip()
                                body = match.group(3).strip()
                                self._db.execute(
                                    "INSERT INTO session_summaries "
                                    "(session_name, entry_type, content, created_at) "
                                    "VALUES (?, ?, ?, ?)",
                                    (session_name, entry_type, body, created_at),
                                )
                    except OSError as e:
                        log.warning("Failed to migrate summary file %s: %s", fname, e)

                os.rename(summaries_dir, summaries_dir + ".migrated")
                log.info("Migrated session_summaries/ → data.db")
            except OSError as e:
                log.warning("Failed to migrate session_summaries: %s", e)

    def _check_prerequisites(self) -> bool:
        missing = []
        if not shutil.which("gh"):
            missing.append("gh (GitHub CLI)")
        if not shutil.which("claude"):
            missing.append("claude (Claude CLI)")
        if missing:
            messagebox.showerror(
                "Missing Prerequisites",
                f"The following tools are required but not found:\n\n"
                + "\n".join(f"  - {m}" for m in missing)
                + "\n\nPlease install them and try again.",
            )
            self.root.destroy()
            return False
        return True

    def _run_setup_wizard(self, prefill: Config | None = None) -> bool:
        bootstrap = prefill or Config.bootstrap()
        wizard = SetupWizard(self.root, bootstrap)
        self.root.wait_window(wizard)
        if wizard.result is None:
            return False
        self.config = wizard.result
        self.config.save(CONFIG_PATH)
        log.info("Config saved to %s", CONFIG_PATH)
        return True

    # ── Menu Bar ─────────────────────────────────────────────────────────────

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Re-run Setup...", command=self._rerun_setup)
        file_menu.add_command(label="Edit Repos...", command=self._edit_repos)
        file_menu.add_command(label="Edit Config...", command=self._edit_config)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._quit, accelerator="Cmd+Q")

        self.root.bind_all("<Command-q>", lambda e: self._quit())

    def _rerun_setup(self):
        if self._run_setup_wizard(prefill=self.config):
            # Reinitialize services with new config
            self.gh = GitHubClient(self.config)
            self.terminal = TerminalLauncher(self.config)
            self.session_mgr = SessionManager(self.config, self._db)
            if self.monitor:
                self.monitor.stop()
            self.monitor = PRMonitorThread(
                config=self.config,
                gh=self.gh,
                terminal=self.terminal,
                session_mgr=self.session_mgr,
                db=self._db,
                ui_queue=self.ui_queue,
            )
            self.monitor.start()
            messagebox.showinfo("Setup", "Configuration updated. Monitor restarted.")

    def _edit_repos(self):
        dialog = EditReposDialog(self.root, self.config)
        if dialog.result is None:
            return
        self.config.repos = dialog.result
        self.config.save(CONFIG_PATH)
        # Restart monitor with new repo list
        if self.monitor:
            self.monitor.stop()
        self.monitor = PRMonitorThread(
            config=self.config,
            gh=self.gh,
            terminal=self.terminal,
            session_mgr=self.session_mgr,
            db=self._db,
            ui_queue=self.ui_queue,
        )
        self.monitor.start()
        self._status_label.configure(
            text=f"Org: {self.config.org}  |  Poll interval: {self.config.poll_interval}s",
        )

    def _edit_config(self):
        import subprocess
        subprocess.Popen(["open", CONFIG_PATH])

    def _show_gear_menu(self):
        """Show the gear dropdown menu at the button location."""
        try:
            self._gear_menu.tk_popup(
                self.root.winfo_pointerx(),
                self.root.winfo_pointery(),
            )
        finally:
            self._gear_menu.grab_release()

    def _toggle_dangerous(self):
        self.config.dangerously_skip_permissions = self._dangerous_var.get()
        self.config.save(CONFIG_PATH)

    def _quit(self):
        # Stop all embedded Claude processes
        for name in list(self._claude_processes):
            self._stop_claude_process(name)
        self._history_store.flush_all()
        if self.monitor:
            self.monitor.stop()
        self._db.close()
        self.root.destroy()

    # ── Notebook (Tabs) ──────────────────────────────────────────────────────

    def _build_notebook(self):
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill="both", expand=True, padx=5, pady=5)

        self.pr_tab = PRTab(
            self._notebook,
            on_update_branch=self._handle_update_branch,
            on_launch_fix=self._handle_launch_fix,
            on_send_for_review=self._handle_send_for_review,
            on_merge=self._handle_merge,
            on_mark_ready=self._handle_mark_ready,
        )
        self.pr_tab.set_refresh_callback(self._handle_refresh)
        self._notebook.add(self.pr_tab, text="My Pull Requests")

        self.pr_review_tab = PRReviewTab(
            self._notebook,
            on_run_review=self._handle_run_review,
            on_run_review_all=self._handle_run_review_all,
        )
        self.pr_review_tab.set_refresh_callback(self._handle_review_refresh)
        self._notebook.add(self.pr_review_tab, text="Team Review")

        self.session_tab = SessionTab(
            self._notebook,
            config=self.config,
            on_new_session=self._handle_new_session,
            on_start_ticket=self._handle_start_ticket,
            on_open_session=self._handle_open_session,
            on_send_message=self._handle_send_message,
            on_stop_session=self._handle_stop_session,
            on_remove_session=self._handle_remove_session,
        )
        self._notebook.add(self.session_tab, text="Working Sessions")

        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ── Status Bar ───────────────────────────────────────────────────────────

    def _build_status_bar(self):
        status_frame = ttk.Frame(self.root, relief="sunken", borderwidth=1)
        status_frame.pack(fill="x", side="bottom")

        self._status_label = ttk.Label(
            status_frame,
            text=f"Org: {self.config.org}  |  Poll interval: {self.config.poll_interval}s",
            padding=(5, 2),
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        gear_btn = ttk.Button(
            status_frame, text="\u2699", width=2, command=self._show_gear_menu,
        )
        gear_btn.pack(side="right", padx=(0, 1), pady=1)

        self._dangerous_var = tk.BooleanVar(value=self.config.dangerously_skip_permissions)
        self._gear_menu = tk.Menu(self.root, tearoff=0)
        self._gear_menu.add_checkbutton(
            label="Dangerously Skip Permissions",
            variable=self._dangerous_var,
            command=self._toggle_dangerous,
        )
        self._gear_menu.add_separator()
        self._gear_menu.add_command(label="Edit Repos...", command=self._edit_repos)
        self._gear_menu.add_command(label="Re-run Setup...", command=self._rerun_setup)
        self._gear_menu.add_command(label="Edit Config File...", command=self._edit_config)

    # ── UI Queue Processing ──────────────────────────────────────────────────

    def _process_ui_queue(self):
        try:
            while True:
                event_type, data = self.ui_queue.get_nowait()
                if event_type == "update_prs":
                    self.pr_tab.update_prs(data)
                elif event_type == "poll_complete":
                    self.pr_tab.update_poll_time(data)
                elif event_type == "slack_review_result":
                    pr, ok, was_draft = data
                    if ok:
                        if was_draft:
                            pr.is_draft = False
                            self.pr_tab.update_prs(self.pr_tab._prs)
                        messagebox.showinfo("Slack", f"Review request sent for PR #{pr.number}.")
                    else:
                        messagebox.showerror("Slack", f"Failed to send review request for PR #{pr.number}.")
                elif event_type == "merge_result":
                    pr, ok = data
                    if ok:
                        messagebox.showinfo("Merged", f"PR #{pr.number} ({pr.repo}) merged successfully.")
                        self._handle_refresh()
                    else:
                        messagebox.showerror("Merge Failed", f"Failed to merge PR #{pr.number} ({pr.repo}).")
                elif event_type == "mark_ready_result":
                    pr, ok = data
                    if ok:
                        pr.is_draft = False
                        self.pr_tab.update_prs(self.pr_tab._prs)
                    else:
                        messagebox.showerror("Error", f"Failed to remove draft status from PR #{pr.number} ({pr.repo}).")
                elif event_type == "update_team_prs":
                    prs, statuses = data
                    self._review_statuses = statuses
                    self.pr_review_tab.update_prs(prs, statuses)
                elif event_type == "review_poll_complete":
                    self.pr_review_tab.update_poll_time(data)
                elif event_type == "review_status_update":
                    repo, number, status = data
                    self._review_statuses[f"{repo}#{number}"] = status
                    self.pr_review_tab.update_review_status(repo, number, status)
                elif event_type == "review_complete":
                    repo, number, success = data
                    new_status = STATUS_REVIEWED_BY_CLAUDE if success else STATUS_PENDING
                    self._review_statuses[f"{repo}#{number}"] = new_status
                    self.pr_review_tab.update_review_status(repo, number, new_status)
                elif event_type == "claude_event":
                    name, evt = data
                    self._handle_claude_event(name, evt)
                elif event_type == "claude_exit":
                    name, returncode, gen = data
                    self._handle_claude_exit(name, returncode, gen)
        except queue.Empty:
            pass
        self.root.after(100, self._process_ui_queue)

    def _refresh_sessions(self):
        if self.session_mgr:
            self.session_mgr.refresh_statuses()
            sessions = self.session_mgr.get_all_sessions()
            self.session_tab.update_sessions(sessions, self._needs_attention)
        # Periodically flush history so it survives unclean shutdowns
        self._history_store.flush_all()
        self.root.after(10_000, self._refresh_sessions)

    # ── Claude Process Management ────────────────────────────────────────────

    def _start_claude_process(
        self,
        session: ManagedSession,
        initial_prompt: str | None = None,
        clear_panel: bool = True,
        is_resume: bool = False,
    ):
        """Create and start a ClaudeProcess for the given session."""
        name = session.name
        cwd = session.cwd or self.config.base_dir

        # Stop existing process for this session if any
        if name in self._claude_processes:
            self._stop_claude_process(name)

        # Log session start to summary (before appending instructions)
        if initial_prompt and not is_resume:
            self._summary_logger.log_session_start(name, initial_prompt)

        # Append summary instructions to new sessions (not resumes)
        if initial_prompt and not is_resume:
            initial_prompt += SUMMARY_INSTRUCTIONS

        # Increment generation so stale exit events from old processes are ignored
        self._session_gen[name] = self._session_gen.get(name, 0) + 1
        gen = self._session_gen[name]

        def on_event(evt: dict):
            self.ui_queue.put(("claude_event", (name, evt)))

        def on_exit(returncode: int | None):
            self.ui_queue.put(("claude_exit", (name, returncode, gen)))

        proc = ClaudeProcess(
            cwd=cwd,
            on_event=on_event,
            on_exit=on_exit,
            session_id=session.session_id,
            initial_prompt=initial_prompt,
            oauth_token=self.config.claude_oauth_token,
            dangerously_skip_permissions=self.config.dangerously_skip_permissions,
        )
        self._claude_processes[name] = proc
        proc.start()

        # Track PID so refresh_statuses correctly identifies running sessions
        session.pid = proc.pid
        self.session_mgr.register_session(session)

        # Update chat panel
        panel = self.session_tab.chat_panel
        if clear_panel:
            panel.clear()
            if initial_prompt:
                self._history_store.append(name, "user", f"\nYou: {initial_prompt}\n")
                panel.append_user_message(initial_prompt)
        panel.set_status(f"Session: {name} | Starting...", running=True)
        panel.set_input_enabled(False)

    def _stop_claude_process(self, name: str):
        """Stop a running Claude process."""
        proc = self._claude_processes.pop(name, None)
        if proc:
            proc.stop()

    # ── Claude Event Routing ─────────────────────────────────────────────────

    def _handle_claude_event(self, name: str, evt: dict):
        """Route a stream-json event to history store and (if active) chat panel."""
        is_active = self.session_tab._active_session_name == name
        panel = self.session_tab.chat_panel

        evt_type = evt.get("type", "")

        # Unwrap stream_event envelope — inner event has the actual type
        if evt_type == "stream_event":
            inner = evt.get("event", {})
            inner_type = inner.get("type", "")

            if inner_type == "content_block_start":
                content_block = inner.get("content_block", {})
                if content_block.get("type") == "tool_use":
                    tool_name = content_block.get("name", "unknown")
                    self._tool_input_buf[name] = {"tool": tool_name, "json": ""}

            elif inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    self._history_store.append(name, "assistant", text)
                    # Accumulate for summary
                    self._summary_text_buf.setdefault(name, "")
                    self._summary_text_buf[name] += text
                    if is_active:
                        panel.append_text(text)
                elif delta.get("type") == "input_json_delta":
                    buf = self._tool_input_buf.get(name)
                    if buf:
                        buf["json"] += delta.get("partial_json", "")

            elif inner_type == "content_block_stop":
                buf = self._tool_input_buf.pop(name, None)
                if buf:
                    # Tool block completed
                    summary = self._tool_summary(buf["tool"], buf["json"])
                    tool_text = f"\n[{summary}]\n"
                    self._history_store.append(name, "tool", tool_text)
                    if is_active:
                        panel.append_tool_start(summary)
                else:
                    # Text block completed — flush accumulated text
                    accumulated = self._summary_text_buf.pop(name, "")
                    if accumulated.strip():
                        self._summary_logger.log_assistant_text(name, accumulated)
                        if is_active:
                            self._append_summary_if_active(name, self._summary_logger.get_content(name))

            elif inner_type == "message_stop":
                if is_active:
                    panel.set_input_enabled(True)

            return

        if evt_type == "system":
            # Init event — capture session_id
            session_id = evt.get("session_id")
            if session_id:
                self._update_session_id(name, session_id)
            if is_active:
                panel.set_status(f"Session: {name} | Running", running=True)
                panel.set_input_enabled(False)  # Wait until first message_stop

        elif evt_type == "assistant":
            pass

        elif evt_type == "result":
            result_text = evt.get("result", "")
            if result_text:
                text = f"\n{result_text}"
                self._history_store.append(name, "system", text)
                if is_active:
                    panel.append_text(text, "system")
            # Process is still alive (stream-json stdin) — allow follow-up messages
            proc = self._claude_processes.get(name)
            if proc and proc.is_alive:
                self._needs_attention.add(name)
                self.session_tab.update_sessions(
                    self.session_mgr.get_all_sessions(), self._needs_attention)

            # Check for active skill runner — auto-advance or show continuation
            runner = self._skill_runners.get(name)
            if runner and proc and proc.is_alive and not runner.is_complete:
                next_prompt = runner.advance()
                if next_prompt and not runner.is_complete:
                    if runner.review_between:
                        # Show continuation banner, wait for user
                        if is_active:
                            step_info = f"Step {runner.current_step - 1}/{runner.total_steps} complete."
                            panel.show_skill_continuation(
                                step_info, next_prompt,
                                on_continue=lambda n=name: self._continue_skill_runner(n),
                                on_stop=lambda n=name: self._cancel_skill_runner(n),
                            )
                            panel.set_status(f"Session: {name} | Awaiting continuation", running=True)
                            panel.set_input_enabled(True)
                    else:
                        # Auto-advance — send next command immediately
                        if is_active:
                            panel.set_input_enabled(False)
                            panel.set_status(f"Session: {name} | Running step {runner.current_step}/{runner.total_steps}", running=True)
                        panel.append_system(f"\n--- Auto-advancing: {next_prompt} ---")
                        self._history_store.append(name, "system", f"\n--- Auto-advancing: {next_prompt} ---\n")
                        self._needs_attention.discard(name)
                        proc.send_message(next_prompt)
                    return
                elif runner.is_complete:
                    self._skill_runners.pop(name, None)
                    if is_active:
                        panel.hide_skill_continuation()

            if is_active:
                if proc and proc.is_alive:
                    panel.set_status(f"Session: {name} | Ready", running=True)
                    panel.set_input_enabled(True)
                else:
                    panel.set_status(f"Session: {name} | Completed", running=False)
                    panel.set_input_enabled(False)

        elif evt_type == "error":
            error_msg = evt.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            error_text = f"\nError: {str(error_msg)}\n"
            self._history_store.append(name, "error", error_text)
            self._summary_logger.log_error(name, str(error_msg))
            if is_active:
                panel.append_error(str(error_msg))
                self._append_summary_if_active(name, self._summary_logger.get_content(name))

        elif evt_type == "stderr":
            text = evt.get("text", "") + "\n"
            self._history_store.append(name, "system", text)
            if is_active:
                panel.append_text(text, "system")

        elif evt_type == "raw":
            text = evt.get("text", "") + "\n"
            self._history_store.append(name, "system", text)
            if is_active:
                panel.append_text(text, "system")

    def _handle_claude_exit(self, name: str, returncode: int | None, gen: int = 0):
        """Handle Claude process exit."""
        self._needs_attention.discard(name)
        self._skill_runners.pop(name, None)
        # Ignore stale exit events from replaced processes
        if self._session_gen.get(name, 0) != gen:
            log.debug("Ignoring stale exit for %s (gen %d, current %d)",
                       name, gen, self._session_gen.get(name, 0))
            return

        self._claude_processes.pop(name, None)

        # Update session status
        if self.session_mgr:
            for s in self.session_mgr.get_all_sessions():
                if s.name == name:
                    s.status = SessionStatus.STOPPED
                    s.pid = None
                    self.session_mgr.register_session(s)
                    break

        status = "exited" if returncode == 0 else f"exited (code {returncode})"
        exit_text = f"\n--- Process {status} ---\n"
        self._history_store.append(name, "system", exit_text)
        self._history_store.flush(name)
        self._summary_logger.log_session_stop(name, status)

        panel = self.session_tab.chat_panel
        if self.session_tab._active_session_name == name:
            panel.hide_skill_continuation()
            panel.set_status(f"Session: {name} | {status}", running=False)
            panel.set_input_enabled(True)
            panel.append_system(f"\n--- Process {status} ---")

    def _continue_skill_runner(self, name: str):
        """Advance skill runner and send the next prompt."""
        runner = self._skill_runners.get(name)
        proc = self._claude_processes.get(name)
        panel = self.session_tab.chat_panel

        panel.hide_skill_continuation()

        if not runner or not proc or not proc.is_alive:
            return

        prompt = runner.current_prompt
        if not prompt:
            self._skill_runners.pop(name, None)
            return

        self._needs_attention.discard(name)
        self.session_tab.update_sessions(
            self.session_mgr.get_all_sessions(), self._needs_attention)

        panel.set_input_enabled(False)
        panel.set_status(f"Session: {name} | Running step {runner.current_step}/{runner.total_steps}", running=True)
        panel.append_user_message(prompt)
        self._history_store.append(name, "user", f"\nYou: {prompt}\n")
        proc.send_message(prompt)

    def _cancel_skill_runner(self, name: str):
        """Cancel the skill runner, leaving the session as-is."""
        self._skill_runners.pop(name, None)
        panel = self.session_tab.chat_panel
        panel.hide_skill_continuation()
        panel.append_system("--- Skill workflow stopped ---")
        self._history_store.append(name, "system", "\n--- Skill workflow stopped ---\n")

    @staticmethod
    def _tool_summary(tool_name: str, input_json: str) -> str:
        """Extract a one-line description from a tool's input JSON."""
        try:
            data = json.loads(input_json)
        except (json.JSONDecodeError, ValueError):
            return tool_name

        desc = ""
        if tool_name == "Bash":
            desc = data.get("command", "")
        elif tool_name in ("Read", "Edit", "Write"):
            path = data.get("file_path", "")
            if path:
                parts = path.rsplit("/", 2)
                desc = "/".join(parts[-2:]) if len(parts) > 2 else path
        elif tool_name in ("Grep", "Glob"):
            desc = data.get("pattern", "")
        elif tool_name == "Agent":
            desc = data.get("description", "")
        elif tool_name == "Skill":
            desc = data.get("skill", "")
        elif tool_name == "WebSearch":
            desc = data.get("query", "")

        if not desc:
            return tool_name
        if len(desc) > 80:
            desc = desc[:77] + "..."
        return f"{tool_name}: {desc}"

    def _update_session_id(self, name: str, session_id: str):
        """Persist the Claude session_id for resume support."""
        proc = self._claude_processes.get(name)
        if proc:
            proc._session_id = session_id

        if self.session_mgr:
            for s in self.session_mgr.get_all_sessions():
                if s.name == name:
                    s.session_id = session_id
                    self.session_mgr.register_session(s)
                    break

    def _replay_history(self, name: str):
        """Clear the chat panel and replay stored history entries."""
        panel = self.session_tab.chat_panel
        panel.load_history(self._history_store.get(name))

    def _append_summary_if_active(self, name: str, content: str):
        """Reload summary panel content if it's visible for the active session."""
        if (self.session_tab._active_session_name == name
                and self.session_tab._summary_visible):
            self.session_tab.summary_panel.load(content)

    def _find_session_by_name(self, name: str) -> ManagedSession | None:
        """Find a managed session by name."""
        if not self.session_mgr:
            return None
        for s in self.session_mgr.get_all_sessions():
            if s.name == name:
                return s
        return None

    # ── Session Tab Callbacks ────────────────────────────────────────────────

    def _handle_open_session(self, session: ManagedSession):
        """Open (or switch to) a session's chat panel — replays history, never auto-starts."""
        name = session.name
        panel = self.session_tab.chat_panel

        # Show PR link if available
        panel.set_pr_url(session.pr_url)

        # Always replay history into the panel
        self._replay_history(name)

        # Load summary content into summary panel
        summary_content = self._summary_logger.get_content(name)
        self.session_tab.summary_panel.load(summary_content)

        if name in self._claude_processes and self._claude_processes[name].is_alive:
            # Already running
            panel.set_status(f"Session: {name} | Running", running=True)
            panel.set_input_enabled(True)
        else:
            # Stopped — show history with Resume button, enable input for resume+send
            panel.set_status(f"Session: {name} | Stopped", running=False)
            panel.set_input_enabled(True)

    def _handle_send_message(self, name: str, text: str):
        """Send a user message to the Claude process (restarts with --resume)."""
        self._needs_attention.discard(name)
        self.session_tab.update_sessions(
            self.session_mgr.get_all_sessions(), self._needs_attention)
        proc = self._claude_processes.get(name)

        panel = self.session_tab.chat_panel
        user_text = f"\nYou: {text}\n"
        self._history_store.append(name, "user", user_text)
        self._summary_logger.log_user_message(name, text)
        panel.append_user_message(text)

        if not proc or not proc.is_alive:
            # Stopped session — treat Send as Resume+Send
            session = self._find_session_by_name(name)
            if not session:
                panel.append_error("Session not found")
                return
            self._start_claude_process(session, initial_prompt=text, clear_panel=False)
            return

        panel.set_input_enabled(False)
        panel.set_status(f"Session: {name} | Running", running=True)
        proc.send_message(text)

    def _handle_stop_session(self, name: str):
        """Stop the Claude process for a session."""
        self._needs_attention.discard(name)
        self._stop_claude_process(name)

        stop_text = "\n--- Stopped by user ---\n"
        self._history_store.append(name, "system", stop_text)
        self._history_store.flush(name)
        self._summary_logger.log_session_stop(name, "Stopped by user")

        panel = self.session_tab.chat_panel
        panel.set_status(f"Session: {name} | Stopped", running=False)
        panel.set_input_enabled(True)
        panel.append_system("\n--- Stopped by user ---")

    def _handle_remove_session(self, name: str):
        """Remove a stopped session from the registry."""
        self._stop_claude_process(name)
        self._history_store.remove(name)
        self._summary_logger.remove(name)
        if self.session_mgr:
            self.session_mgr.unregister_session(name)
            sessions = self.session_mgr.get_all_sessions()
            self.session_tab.update_sessions(sessions, self._needs_attention)

    # ── Action Handlers ──────────────────────────────────────────────────────

    def _handle_refresh(self):
        if self.monitor:
            self.monitor.poll_now()
            self.pr_tab._monitor_label.configure(text="Monitor: refreshing...", foreground="#d29922")

    def _handle_update_branch(self, repo: str, number: int):
        def _do():
            success = self.gh.update_branch(repo, number)
            if success:
                self.ui_queue.put(("poll_complete", time.time()))
                # Trigger re-fetch
                self.monitor.poll_now()

        threading.Thread(target=_do, daemon=True).start()

    def _handle_launch_fix(self, pr: PRData):
        """Open a working session to fix the selected PR's issues."""
        workflow = self.config.skills.fix_pr

        if workflow.builtin or not workflow.commands:
            org = self.config.org
            prompt = Prompts.fix_all(org, pr.repo, pr)
        else:
            placeholders = {
                "pr_url": pr.url,
                "pr_number": str(pr.number),
                "repo": pr.repo,
                "branch": pr.branch,
                "org": self.config.org,
            }
            runner = SkillRunner(
                session_name="",
                commands=workflow.commands,
                review_between=workflow.review_between,
                placeholders=placeholders,
            )
            prompt = runner.current_prompt

        repo_cwd = os.path.join(self.config.base_dir, pr.repo)
        ticket_id = pr.ticket_id

        # Look for an existing session for this PR
        existing = self._find_session_for_pr(pr)

        if existing:
            session = existing
            session.cwd = session.cwd or repo_cwd
            session.pr_url = session.pr_url or pr.url
        else:
            name = f"Fix {pr.repo}#{pr.number}"
            if ticket_id:
                name = f"{ticket_id}: Fix {pr.repo}#{pr.number}"
            session = ManagedSession(
                name=name,
                repo=pr.repo,
                pid=None,
                status=SessionStatus.RUNNING,
                created_at=time.time(),
                ticket_id=ticket_id,
                cwd=repo_cwd,
                session_id=self.session_mgr.find_claude_session(pr.repo, ticket_id),
                pr_url=pr.url,
            )
            self.session_mgr.register_session(session)

        # Register skill runner for multi-step non-builtin fix workflows
        if not (workflow.builtin or not workflow.commands) and len(workflow.commands) > 1:
            runner.session_name = session.name
            self._skill_runners[session.name] = runner

        # Switch to Working Sessions tab, open the panel, and send the prompt
        self._notebook.select(self.session_tab)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)
        self.session_tab.select_and_open_session(session)
        self._start_claude_process(session, initial_prompt=prompt)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)

    def _find_session_for_pr(self, pr: PRData) -> ManagedSession | None:
        """Find an existing working session that matches this PR."""
        for s in self.session_mgr.get_all_sessions():
            # Match by ticket_id if available
            if pr.ticket_id and s.ticket_id and pr.ticket_id == s.ticket_id:
                return s
            # Match by repo + PR number in session name
            if pr.repo in (s.repo or "") and f"#{pr.number}" in s.name:
                return s
        return None

    def _handle_new_session(self):
        dialog = NewSessionDialog(self.root)
        if dialog.result is None:
            return
        name, prompt = dialog.result

        session = ManagedSession(
            name=name,
            repo="",
            pid=None,
            status=SessionStatus.RUNNING,
            created_at=time.time(),
            ticket_id=extract_ticket_id(name),
            cwd=self.config.base_dir,
        )
        self.session_mgr.register_session(session)

        # Open panel and start Claude inline
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)
        self.session_tab.select_and_open_session(session)
        self._start_claude_process(session, initial_prompt=prompt or None)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)

    def _handle_start_ticket(self):
        dialog = StartTicketDialog(self.root, self.config)
        if dialog.result is None:
            return
        ticket_id, summary = dialog.result

        workflow = self.config.skills.work_ticket
        commands = workflow.commands
        if not commands:
            messagebox.showwarning("Skills", "No work_ticket skill commands configured in defaults.json.")
            return
        placeholders = {"ticket_id": ticket_id}

        # For multi-step workflows with review_between, let user pick starting step
        start_index = 0
        if len(commands) > 1 and workflow.review_between:
            sel_dialog = SkillSelectionDialog(self.root, commands)
            if sel_dialog.result is None:
                return
            start_index = sel_dialog.result

        runner = SkillRunner(
            session_name="",  # updated after session creation
            commands=commands,
            review_between=workflow.review_between,
            placeholders=placeholders,
        )
        runner.start_from(start_index)

        name = f"{ticket_id}: {summary}" if summary else f"Ticket-{ticket_id}"
        runner.session_name = name
        session = ManagedSession(
            name=name,
            repo="",
            pid=None,
            status=SessionStatus.RUNNING,
            created_at=time.time(),
            ticket_id=ticket_id,
            cwd=self.config.base_dir,
        )
        self.session_mgr.register_session(session)

        initial_prompt = runner.current_prompt
        if len(commands) > 1:
            self._skill_runners[name] = runner

        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)
        self.session_tab.select_and_open_session(session)
        self._start_claude_process(session, initial_prompt=initial_prompt)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)

    def _handle_send_for_review(self, pr: PRData):
        """Remove draft status if needed, then send a Slack review request."""
        webhook_url = self.config.slack_webhook_url
        if not webhook_url:
            messagebox.showwarning("Slack", "No Slack webhook configured. Run setup to add one.")
            return

        ticket_id = pr.ticket_id
        jira_base = self.config.jira_base_url.rstrip("/")
        if jira_base and not jira_base.endswith("/browse"):
            jira_base += "/browse"

        lines = [f"*PR ready for review:* <{pr.url}|{pr.repo}#{pr.number} — {pr.title}>"]
        if ticket_id and jira_base:
            lines.append(f"*Ticket:* <{jira_base}/{ticket_id}|{ticket_id}>")
        msg = "\n".join(lines)

        is_draft = pr.is_draft
        gh = self.gh

        def _do():
            from slack_client import send_webhook
            if is_draft:
                gh.mark_ready_for_review(pr.repo, pr.number)
            ok = send_webhook(webhook_url, msg, unfurl_links=False)
            self.ui_queue.put(("slack_review_result", (pr, ok, is_draft)))

        threading.Thread(target=_do, daemon=True).start()

    def _handle_merge(self, pr: PRData):
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Merge PR",
            f"Merge {pr.repo}#{pr.number} ({pr.branch})?\n\n"
            "This will squash-merge and delete the branch.",
            parent=self.root,
        ):
            return

        def _do():
            ok = self.gh.merge_pr(pr.repo, pr.number)
            self.ui_queue.put(("merge_result", (pr, ok)))

        threading.Thread(target=_do, daemon=True).start()

    def _handle_mark_ready(self, pr: PRData):
        def _do():
            ok = self.gh.mark_ready_for_review(pr.repo, pr.number)
            self.ui_queue.put(("mark_ready_result", (pr, ok)))

        threading.Thread(target=_do, daemon=True).start()

    # ── PR Review Handlers ──────────────────────────────────────────────────

    def _on_tab_changed(self, _event):
        current = self._notebook.select()
        if current == str(self.pr_review_tab):
            self._handle_review_refresh()

    def _handle_review_refresh(self):
        def _do():
            current_user = self.gh.get_current_user()
            all_prs = self.gh.fetch_all_team_prs()

            team_prs = [
                pr for pr in all_prs
                if pr.author != current_user
            ]

            statuses = dict(self._review_statuses)
            for pr in team_prs:
                key = f"{pr.repo}#{pr.number}"
                if key not in statuses or statuses[key] in (STATUS_PENDING, STATUS_REVIEWED_BY_ME, STATUS_REVIEWED_BY_CLAUDE):
                    review_result = self.gh.get_review_status(pr.repo, pr.number, current_user)
                    if review_result == "reviewed_by_me":
                        statuses[key] = STATUS_REVIEWED_BY_ME
                    elif review_result == "reviewed_by_claude":
                        statuses[key] = STATUS_REVIEWED_BY_CLAUDE
                    else:
                        statuses[key] = STATUS_PENDING

            self.ui_queue.put(("update_team_prs", (team_prs, statuses)))
            self.ui_queue.put(("review_poll_complete", time.time()))

        threading.Thread(target=_do, daemon=True, name="ReviewPoll").start()

    def _handle_run_review(self, pr: PRData):
        key = f"{pr.repo}#{pr.number}"
        status = self._review_statuses.get(key, STATUS_PENDING)

        if status == STATUS_REVIEWING:
            return

        self._review_statuses[key] = STATUS_REVIEWING
        self.pr_review_tab.update_review_status(pr.repo, pr.number, STATUS_REVIEWING)

        workflow = self.config.skills.review_pr
        commands = workflow.commands
        if not commands:
            messagebox.showwarning("Skills", "No review_pr skill commands configured in defaults.json.")
            return
        placeholders = {"pr_url": pr.url}

        runner = SkillRunner(
            session_name="",
            commands=commands,
            review_between=workflow.review_between,
            placeholders=placeholders,
        )

        prompt = runner.current_prompt
        repo_cwd = os.path.join(self.config.base_dir, pr.repo)

        name = f"Review {pr.repo}#{pr.number}"
        runner.session_name = name
        session = ManagedSession(
            name=name,
            repo=pr.repo,
            pid=None,
            status=SessionStatus.RUNNING,
            created_at=time.time(),
            ticket_id=pr.ticket_id,
            cwd=repo_cwd,
            pr_url=pr.url,
        )
        self.session_mgr.register_session(session)

        if len(commands) > 1:
            self._skill_runners[name] = runner

        self._notebook.select(self.session_tab)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)
        self.session_tab.select_and_open_session(session)
        self._start_claude_process(session, initial_prompt=prompt)
        self.session_tab.update_sessions(self.session_mgr.get_all_sessions(), self._needs_attention)

        # Post marker comment in background
        def _post_comment():
            self.gh.post_review_comment(pr.repo, pr.number)
            self.ui_queue.put(("review_complete", (pr.repo, pr.number, True)))
        threading.Thread(target=_post_comment, daemon=True).start()

    def _handle_run_review_all(self, prs: list[PRData]):
        for pr in prs:
            self._handle_run_review(pr)

    def run(self):
        self.root.mainloop()


def main():
    if "--reset" in sys.argv:
        for path in (CONFIG_PATH, DB_PATH):
            if os.path.exists(path):
                os.remove(path)
                print(f"Removed {path}")
            else:
                print(f"Already absent: {path}")

        # Copy a specified defaults file to defaults.json
        remaining = [a for a in sys.argv[1:] if a != "--reset"]
        if remaining:
            src = remaining[0]
            if not os.path.isabs(src):
                src = os.path.join(os.getcwd(), src)
            if os.path.exists(src):
                shutil.copy2(src, DEFAULTS_PATH)
                print(f"Copied {src} -> {DEFAULTS_PATH}")
            else:
                print(f"Defaults file not found: {src}")
                return

        print("Config reset. Run again without --reset to start fresh.")
        return
    app = Application()
    app.run()


if __name__ == "__main__":
    main()
