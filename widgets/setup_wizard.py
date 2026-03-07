"""First-run setup wizard (5-step modal dialog)."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, filedialog
from typing import Optional

from config import Config, SkillsConfig, WorkflowConfig

log = logging.getLogger("claude_mgmt")


class SetupWizard(tk.Toplevel):
    """5-step setup wizard: Claude Auth, Org+Repos, Base Dir, Jira, Slack."""

    def __init__(self, parent: tk.Tk, bootstrap: Config):
        super().__init__(parent)
        self.title("Claude Management — Setup")
        self.geometry("600x550")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[Config] = None
        self._config = Config(
            org=bootstrap.org,
            repos=list(bootstrap.repos),
            jira_base_url=bootstrap.jira_base_url,
            jira_email=bootstrap.jira_email,
            jira_api_token=bootstrap.jira_api_token,
            jira_board_id=bootstrap.jira_board_id,
            jira_board_name=bootstrap.jira_board_name,
            slack_webhook_url=bootstrap.slack_webhook_url,
            base_dir=bootstrap.base_dir,
            claude_projects_dir=bootstrap.claude_projects_dir,
            claude_oauth_token=bootstrap.claude_oauth_token,
            skills=SkillsConfig(
                work_ticket=WorkflowConfig(
                    commands=list(bootstrap.skills.work_ticket.commands),
                    review_between=bootstrap.skills.work_ticket.review_between,
                ),
                review_pr=WorkflowConfig(
                    commands=list(bootstrap.skills.review_pr.commands),
                    review_between=bootstrap.skills.review_pr.review_between,
                ),
                fix_pr=WorkflowConfig(
                    commands=list(bootstrap.skills.fix_pr.commands),
                    review_between=bootstrap.skills.fix_pr.review_between,
                ),
            ),
        )
        self._bg_queue: queue.Queue = queue.Queue()
        self._step = 0
        self._repo_vars: dict[str, tk.BooleanVar] = {}
        self._all_orgs: list[str] = []
        self._all_repos: list[str] = []
        self._repos_fetched_for_org: str = ""  # track which org repos were fetched for

        # Git email for Jira pre-fill
        self._git_email = _get_git_email()

        # Slack webhook verification
        self._slack_verified = False
        self._slack_verify_code = ""

        # Jira board data cached across step visits
        self._jira_boards: list = []
        self._jira_auth_ok = False

        # Container
        self._container = ttk.Frame(self, padding=20)
        self._container.pack(fill="both", expand=True)

        # Navigation
        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=20, pady=(0, 15))
        self._back_btn = ttk.Button(nav, text="Back", command=self._go_back)
        self._back_btn.pack(side="left")
        self._next_btn = ttk.Button(nav, text="Next", command=self._go_next)
        self._next_btn.pack(side="right")

        # Step indicator
        self._step_label = ttk.Label(nav, text="")
        self._step_label.pack(side="right", padx=10)

        # Claude Auth token verification state
        self._claude_token_verified = bool(self._config.claude_oauth_token)

        self._steps = [
            self._build_step_claude_auth,
            self._build_step_base_dir,
            self._build_step_org_repos,
            self._build_step_skills,
            self._build_step_jira,
            self._build_step_slack,
        ]

        self._show_step(0)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_bg()

    def _poll_bg(self):
        try:
            while True:
                callback = self._bg_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_bg)

    def _clear_container(self):
        for w in self._container.winfo_children():
            w.destroy()

    def _show_step(self, step: int):
        self._step = step
        self._clear_container()
        # Re-enable Next before building step (step builders may disable it)
        self._next_btn.configure(state="normal")
        self._steps[step]()
        self._step_label.configure(text=f"Step {step + 1} of {len(self._steps)}")
        self._back_btn.configure(state="normal" if step > 0 else "disabled")
        self._next_btn.configure(text="Finish" if step == len(self._steps) - 1 else "Next")

    def _go_back(self):
        if self._step > 0:
            self._show_step(self._step - 1)

    def _go_next(self):
        if not self._validate_step():
            return
        if self._step < len(self._steps) - 1:
            self._show_step(self._step + 1)
        else:
            self._finish()

    def _validate_step(self) -> bool:
        if self._step == 0:
            if not self._claude_token_verified:
                messagebox.showwarning(
                    "Validation",
                    "Please set up and verify your Claude token before continuing.",
                    parent=self,
                )
                return False
        elif self._step == 1:
            base_dir = self._base_dir_var.get().strip()
            if not base_dir:
                messagebox.showwarning("Validation", "Base directory is required.", parent=self)
                return False
            if not os.path.isdir(base_dir):
                messagebox.showwarning("Validation", f"Directory does not exist:\n{base_dir}", parent=self)
                return False
            self._config.base_dir = base_dir
        elif self._step == 2:
            org = self._org_var.get().strip()
            if not org:
                messagebox.showwarning("Validation", "Please select a GitHub organization.", parent=self)
                return False
            self._config.org = org
            selected = [name for name, var in self._repo_vars.items() if var.get()]
            if not selected:
                messagebox.showwarning("Validation", "Select at least one repo.", parent=self)
                return False
            self._config.repos = selected
        elif self._step == 3:
            self._save_skills_from_ui()
        elif self._step == 4:
            site_url = self._jira_site_var.get().strip()
            email = self._jira_email_var.get().strip()
            token = self._jira_token_var.get().strip()
            if not site_url:
                messagebox.showwarning("Validation", "Site URL is required.", parent=self)
                return False
            if not email or not token:
                messagebox.showwarning(
                    "Validation",
                    "Email and API token are required.",
                    parent=self,
                )
                return False
            site_url = self._normalize_site_url(site_url)
            self._jira_site_var.set(site_url)
            self._config.jira_base_url = site_url
            self._config.jira_email = email
            self._config.jira_api_token = token
            # Require board selection
            board_text = self._jira_board_var.get().strip() if hasattr(self, "_jira_board_var") else ""
            matched = False
            if board_text and self._jira_boards:
                for b in self._jira_boards:
                    display = (f"\u2605 {b.name} ({b.project_key})" if b.favourite
                               else f"{b.name} ({b.project_key})")
                    if display == board_text:
                        self._config.jira_board_id = b.id
                        self._config.jira_board_name = b.name
                        matched = True
                        break
            if not matched:
                messagebox.showwarning(
                    "Validation",
                    "Please test your connection and select a board.",
                    parent=self,
                )
                return False
        elif self._step == 5:
            url = self._slack_webhook_var.get().strip()
            if not url:
                messagebox.showwarning("Validation", "Webhook URL is required.", parent=self)
                return False
            if not self._slack_verified:
                messagebox.showwarning(
                    "Validation",
                    "Please send a test message and enter the verification code.",
                    parent=self,
                )
                return False
            self._config.slack_webhook_url = url
        return True

    def _finish(self):
        self.result = self._config
        self.destroy()

    def _on_close(self):
        self.result = None
        self.destroy()

    # ── Step 0: Claude Authentication ────────────────────────────────────────

    def _build_step_claude_auth(self):
        ttk.Label(self._container, text="Claude Authentication",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self._container,
                  text="Paste your Claude OAuth token below.\n\n"
                       "To generate a token, click 'Setup Token' which runs\n"
                       "'claude setup-token' in iTerm and opens a browser for OAuth consent.\n"
                       "Then paste the resulting token below.",
                  wraplength=500).pack(anchor="w", pady=(0, 10))

        ttk.Button(self._container, text="Setup Token",
                   command=self._launch_setup_token).pack(anchor="w", pady=(0, 10))

        ttk.Label(self._container, text="OAuth Token:").pack(anchor="w")
        token_frame = ttk.Frame(self._container)
        token_frame.pack(anchor="w", fill="x", pady=(3, 0))
        self._token_entry_var = tk.StringVar(
            value=self._config.claude_oauth_token if self._claude_token_verified else ""
        )
        token_entry = ttk.Entry(token_frame, textvariable=self._token_entry_var, width=50, show="*")
        token_entry.pack(side="left", padx=(0, 5))
        token_entry.focus_set()
        ttk.Button(token_frame, text="Verify", command=self._verify_pasted_token).pack(side="left")

        self._claude_auth_label = ttk.Label(self._container, text="")
        self._claude_auth_label.pack(anchor="w", pady=(5, 0))

        if self._claude_token_verified:
            self._claude_auth_label.configure(
                text="Token verified.", foreground="green",
            )

    def _verify_pasted_token(self):
        token = self._token_entry_var.get().strip()
        if not token:
            self._claude_auth_label.configure(
                text="Paste your OAuth token first.", foreground="red",
            )
            return

        self._claude_auth_label.configure(text="Verifying...", foreground="gray")

        def _do_verify():
            try:
                r = subprocess.run(
                    ["claude", "--output-format", "json", "--print", "echo test"],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token},
                )
                ok = r.returncode == 0
            except Exception:
                ok = False
            self._bg_queue.put(lambda: self._on_token_verified(token, ok))

        threading.Thread(target=_do_verify, daemon=True).start()

    def _on_token_verified(self, token: str, ok: bool):
        if ok:
            self._config.claude_oauth_token = token
            self._claude_token_verified = True
            self._claude_auth_label.configure(
                text="Token verified.", foreground="green",
            )
        else:
            self._claude_token_verified = False
            self._claude_auth_label.configure(
                text="Verification failed. Check that the token is correct.",
                foreground="red",
            )

    def _launch_setup_token(self):
        applescript = (
            'tell application "iTerm"\n'
            '    activate\n'
            '    if (count of windows) = 0 then\n'
            '        set newWindow to (create window with default profile)\n'
            '        tell current session of newWindow\n'
            '            set name to "Claude Setup Token"\n'
            '            write text "unset CLAUDECODE && claude setup-token"\n'
            '        end tell\n'
            '    else\n'
            '        tell current window\n'
            '            create tab with default profile\n'
            '            tell current session\n'
            '                set name to "Claude Setup Token"\n'
            '                write text "unset CLAUDECODE && claude setup-token"\n'
            '            end tell\n'
            '        end tell\n'
            '    end if\n'
            'end tell'
        )
        subprocess.Popen(["osascript", "-e", applescript])

    # ── Step 1: GitHub Org + Repos ─────────────────────────────────────────

    def _build_step_org_repos(self):
        ttk.Label(self._container, text="GitHub Organization & Repositories",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self._container,
                  text="Select your organization, then choose repositories to monitor.").pack(anchor="w", pady=(0, 8))

        # --- Org combobox ---
        org_row = ttk.Frame(self._container)
        org_row.pack(anchor="w", fill="x")
        ttk.Label(org_row, text="Organization:").pack(side="left", padx=(0, 5))
        self._org_var = tk.StringVar()
        self._org_combo = ttk.Combobox(
            org_row, textvariable=self._org_var, width=35, state="readonly",
        )
        self._org_combo.pack(side="left")
        self._org_combo.bind("<<ComboboxSelected>>", self._on_org_selected)

        self._org_progress = ttk.Progressbar(self._container, mode="indeterminate")
        self._org_progress.pack(fill="x", pady=(5, 0))

        self._org_status = ttk.Label(self._container, text="")
        self._org_status.pack(anchor="w", pady=(2, 0))

        # --- Repo section (hidden until org selected) ---
        self._repo_section = ttk.Frame(self._container)
        # Don't pack yet

        ttk.Separator(self._repo_section, orient="horizontal").pack(fill="x", pady=(5, 5))
        repo_header = ttk.Frame(self._repo_section)
        repo_header.pack(anchor="w", fill="x", pady=(0, 3))
        ttk.Label(repo_header, text="Repositories:").pack(side="left")
        ttk.Button(repo_header, text="Check All", command=self._check_all_repos).pack(side="right", padx=(5, 0))
        ttk.Button(repo_header, text="Uncheck All", command=self._uncheck_all_repos).pack(side="right")

        # Search filter
        search_frame = ttk.Frame(self._repo_section)
        search_frame.pack(anchor="w", fill="x", pady=(0, 3))
        ttk.Label(search_frame, text="Search:").pack(side="left", padx=(0, 5))
        self._repo_search_var = tk.StringVar()
        self._repo_search_var.trace_add("write", lambda *_: self._filter_repos())
        ttk.Entry(search_frame, textvariable=self._repo_search_var, width=30).pack(side="left")

        self._repo_progress = ttk.Progressbar(self._repo_section, mode="indeterminate")
        self._repo_progress.pack(fill="x", pady=(0, 3))

        list_frame = ttk.Frame(self._repo_section)
        list_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self._repo_inner = ttk.Frame(canvas)
        self._repo_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._repo_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Disable Next while loading orgs
        self._next_btn.configure(state="disabled")

        if self._all_orgs:
            self._populate_orgs(self._all_orgs)
        else:
            self._fetch_orgs()

    def _fetch_orgs(self):
        self._org_progress.start()
        self._org_status.configure(text="Fetching organizations...", foreground="gray")

        def _fetch():
            try:
                r = subprocess.run(
                    ["gh", "api", "user/orgs", "--jq", ".[].login"],
                    capture_output=True, text=True, timeout=15,
                )
                orgs = sorted(r.stdout.strip().splitlines()) if r.returncode == 0 and r.stdout.strip() else []
            except Exception:
                orgs = []
            self._bg_queue.put(lambda: self._on_orgs_fetched(orgs))

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_orgs_fetched(self, orgs: list[str]):
        self._org_progress.stop()
        self._org_progress.pack_forget()
        self._all_orgs = orgs

        if not orgs:
            self._org_status.configure(
                text="No organizations found. Check that 'gh auth status' is working.",
                foreground="red",
            )
            return

        self._populate_orgs(orgs)

    def _populate_orgs(self, orgs: list[str]):
        self._org_combo["values"] = orgs
        self._org_status.configure(text="")

        # Pre-select existing config org if present
        current = self._config.org
        if current in orgs:
            self._org_combo.current(orgs.index(current))
            # Trigger repo load for the pre-selected org
            self._on_org_selected()
        elif orgs:
            self._org_combo.current(0)
            self._on_org_selected()

    def _on_org_selected(self, _event=None):
        """Called when the org combobox selection changes."""
        org = self._org_var.get().strip()
        if not org:
            return

        # Show repo section
        self._repo_section.pack(fill="both", expand=True, pady=(5, 0))

        # Only re-fetch if org changed
        if org == self._repos_fetched_for_org and self._all_repos:
            self._populate_repos(self._all_repos)
            self._next_btn.configure(state="normal")
        else:
            self._all_repos = []
            self._repo_vars.clear()
            for w in self._repo_inner.winfo_children():
                w.destroy()
            self._next_btn.configure(state="disabled")
            self._fetch_repos(org)

    # ── Step 2: Base Directory ──────────────────────────────────────────────

    def _build_step_base_dir(self):
        ttk.Label(self._container, text="Repository Root Directory",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(self._container,
                  text="Select the folder where your repositories are cloned.\n"
                       "Claude will use this as the working directory.",
                  wraplength=500).pack(anchor="w", pady=(0, 10))

        dir_frame = ttk.Frame(self._container)
        dir_frame.pack(anchor="w", fill="x", pady=(0, 10))

        self._base_dir_var = tk.StringVar(value=self._config.base_dir)
        entry = ttk.Entry(dir_frame, textvariable=self._base_dir_var, width=45)
        entry.pack(side="left", padx=(0, 5))
        entry.focus_set()

        ttk.Button(dir_frame, text="Browse...", command=self._browse_base_dir).pack(side="left")

        ttk.Label(self._container,
                  text=f"Default: {self._config.base_dir}",
                  foreground="gray").pack(anchor="w")

    def _browse_base_dir(self):
        current = self._base_dir_var.get().strip()
        chosen = filedialog.askdirectory(
            title="Select Repository Root Directory",
            initialdir=current if current else None,
            parent=self,
        )
        if chosen:
            self._base_dir_var.set(chosen)

    def _fetch_repos(self, org: str):
        self._repo_progress.start()

        def _fetch():
            try:
                r = subprocess.run(
                    ["gh", "repo", "list", org, "--json", "name", "--limit", "100"],
                    capture_output=True, text=True, timeout=30,
                )
                import json
                repos = sorted(item["name"] for item in json.loads(r.stdout)) if r.returncode == 0 else []
            except Exception:
                repos = []
            self._bg_queue.put(lambda: self._on_repos_fetched(org, repos))

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_repos_fetched(self, org: str, repos: list[str]):
        self._repo_progress.stop()
        self._repo_progress.pack_forget()
        self._all_repos = repos
        self._repos_fetched_for_org = org
        self._populate_repos(repos)
        self._next_btn.configure(state="normal")

    def _populate_repos(self, repos: list[str]):
        for w in self._repo_inner.winfo_children():
            w.destroy()
        self._repo_vars.clear()

        # Pre-select: configured repos + repos that exist as directories in base_dir
        pre_selected = set(self._config.repos)
        base_dir = self._config.base_dir
        if base_dir and os.path.isdir(base_dir):
            for name in repos:
                if os.path.isdir(os.path.join(base_dir, name)):
                    pre_selected.add(name)

        for name in repos:
            var = tk.BooleanVar(value=name in pre_selected)
            self._repo_vars[name] = var
            ttk.Checkbutton(self._repo_inner, text=name, variable=var).pack(anchor="w")

    def _filter_repos(self):
        query = self._repo_search_var.get().lower()
        for w in self._repo_inner.winfo_children():
            w.destroy()
        for name, var in self._repo_vars.items():
            if query in name.lower():
                ttk.Checkbutton(self._repo_inner, text=name, variable=var).pack(anchor="w")

    def _check_all_repos(self):
        for var in self._repo_vars.values():
            var.set(True)

    def _uncheck_all_repos(self):
        for var in self._repo_vars.values():
            var.set(False)

    # ── Step 3: Skill Workflows ─────────────────────────────────────────────

    def _build_step_skills(self):
        ttk.Label(self._container, text="Skill Workflows",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self._container,
                  text="Configure which skill commands run for each workflow.\n"
                       "Enter one command per line. Use {ticket_id} and {pr_url} as placeholders.",
                  wraplength=500).pack(anchor="w", pady=(0, 10))

        # Create a scrollable frame for the three workflow sections
        canvas = tk.Canvas(self._container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._container, orient="vertical", command=canvas.yview)
        skills_inner = ttk.Frame(canvas)
        skills_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=skills_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        workflows = [
            ("Work Ticket", "work_ticket", "Commands to run when starting a ticket.\nPlaceholder: {ticket_id}"),
            ("Review PR", "review_pr", "Commands to run when reviewing a PR.\nPlaceholder: {pr_url}"),
            ("Fix PR", "fix_pr", "Commands to run when fixing a PR.\nPlaceholders: {pr_url}, {pr_number}, {repo}, {branch}, {org}"),
        ]

        self._skill_texts: dict[str, tk.Text] = {}
        self._skill_review_vars: dict[str, tk.BooleanVar] = {}

        for label, key, hint in workflows:
            section = ttk.LabelFrame(skills_inner, text=label, padding=8)
            section.pack(fill="x", pady=(0, 8), padx=(0, 5))

            ttk.Label(section, text=hint, foreground="gray", wraplength=450).pack(anchor="w", pady=(0, 5))

            wf = getattr(self._config.skills, key)

            # Commands text area
            ttk.Label(section, text="Commands (one per line):").pack(anchor="w")
            text = tk.Text(section, height=3, width=55, wrap="word", font=("Menlo", 11))
            text.pack(anchor="w", fill="x", pady=(0, 5))
            text.insert("1.0", "\n".join(wf.commands))
            self._skill_texts[key] = text

            # Review between steps toggle
            review_var = tk.BooleanVar(value=wf.review_between)
            self._skill_review_vars[key] = review_var
            ttk.Checkbutton(section, text="Require review between steps",
                            variable=review_var).pack(anchor="w")

    def _save_skills_from_ui(self):
        for key in ("work_ticket", "review_pr", "fix_pr"):
            text = self._skill_texts[key].get("1.0", "end").strip()
            commands = [line.strip() for line in text.splitlines() if line.strip()]
            review_between = self._skill_review_vars[key].get()
            wf = WorkflowConfig(commands=commands, review_between=review_between)
            setattr(self._config.skills, key, wf)

    # ── Step 4: Jira (consolidated — auth + board picker) ─────────────────

    def _build_step_jira(self):
        ttk.Label(self._container, text="Jira Configuration",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(self._container,
                  text="Connect to Jira for ticket browsing. All fields optional.",
                  wraplength=500).pack(anchor="w", pady=(0, 10))

        # --- Site URL ---
        ttk.Label(self._container, text="Site URL:").pack(anchor="w")
        # Derive site URL from existing jira_base_url (strip /browse)
        existing_site = self._config.jira_base_url.rstrip("/")
        if existing_site.endswith("/browse"):
            existing_site = existing_site[: -len("/browse")]
        self._jira_site_var = tk.StringVar(value=existing_site)
        ttk.Entry(self._container, textvariable=self._jira_site_var, width=50).pack(anchor="w", pady=(0, 5))
        ttk.Label(self._container, text="Example: https://myorg.atlassian.net",
                  foreground="gray").pack(anchor="w", pady=(0, 8))

        # --- Email (pre-fill from git config if not already set) ---
        email_default = self._config.jira_email
        if not email_default:
            email_default = self._git_email or ""
        ttk.Label(self._container, text="Email:").pack(anchor="w")
        self._jira_email_var = tk.StringVar(value=email_default)
        ttk.Entry(self._container, textvariable=self._jira_email_var, width=40).pack(anchor="w", pady=(0, 8))

        # --- API Token ---
        ttk.Label(self._container, text="API Token:").pack(anchor="w")
        token_frame = ttk.Frame(self._container)
        token_frame.pack(anchor="w", fill="x", pady=(0, 5))
        self._jira_token_var = tk.StringVar(value=self._config.jira_api_token)
        ttk.Entry(token_frame, textvariable=self._jira_token_var, width=40, show="*").pack(side="left", padx=(0, 5))
        ttk.Button(token_frame, text="Generate Token", command=self._open_token_page).pack(side="left")

        # --- Test Connection ---
        test_frame = ttk.Frame(self._container)
        test_frame.pack(anchor="w", fill="x", pady=(5, 0))
        self._jira_test_btn = ttk.Button(test_frame, text="Test Connection", command=self._test_jira_auth)
        self._jira_test_btn.pack(side="left")
        self._jira_test_progress = ttk.Progressbar(test_frame, mode="indeterminate", length=120)
        self._jira_test_progress.pack(side="left", padx=(10, 0))
        self._jira_test_progress.pack_forget()

        self._jira_auth_label = ttk.Label(self._container, text="")
        self._jira_auth_label.pack(anchor="w", pady=(3, 0))

        # --- Board picker (hidden until auth succeeds) ---
        self._board_frame = ttk.Frame(self._container)
        # Don't pack yet — shown after successful auth

        ttk.Label(self._board_frame, text="Board:").pack(anchor="w")
        self._jira_board_var = tk.StringVar()
        self._jira_board_combo = ttk.Combobox(
            self._board_frame, textvariable=self._jira_board_var, width=50, state="normal",
        )
        self._jira_board_combo.pack(anchor="w", pady=(0, 5))
        self._jira_board_combo.bind("<KeyRelease>", self._filter_boards)
        self._board_display_list: list[str] = []

        # If already authenticated from a previous visit, show board picker
        if self._jira_auth_ok and self._jira_boards:
            self._show_board_picker()

    @staticmethod
    def _open_token_page():
        webbrowser.open("https://id.atlassian.com/manage-profile/security/api-tokens")

    def _normalize_site_url(self, url: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _test_jira_auth(self):
        site_url = self._normalize_site_url(self._jira_site_var.get().strip())
        self._jira_site_var.set(site_url)
        email = self._jira_email_var.get().strip()
        token = self._jira_token_var.get().strip()
        if not site_url or not email or not token:
            self._jira_auth_label.configure(text="Fill in all three fields first.", foreground="red")
            return

        self._jira_test_btn.configure(state="disabled")
        self._jira_test_progress.pack(side="left", padx=(10, 0))
        self._jira_test_progress.start()
        self._jira_auth_label.configure(text="Testing...", foreground="gray")

        def _do_test():
            from jira_client import check_credentials, fetch_all_boards
            display_name = check_credentials(site_url, email, token)
            boards = fetch_all_boards(site_url, email, token) if display_name else []
            self._bg_queue.put(lambda: self._on_jira_auth_done(display_name, boards))

        threading.Thread(target=_do_test, daemon=True).start()

    def _on_jira_auth_done(self, display_name: Optional[str], boards: list):
        self._jira_test_progress.stop()
        self._jira_test_progress.pack_forget()
        self._jira_test_btn.configure(state="normal")

        if display_name:
            self._jira_auth_ok = True
            self._jira_auth_label.configure(
                text=f"Connected as {display_name}", foreground="green",
            )
            self._jira_boards = boards
            self._show_board_picker()
        else:
            self._jira_auth_ok = False
            self._jira_auth_label.configure(
                text="Authentication failed. Check your credentials.", foreground="red",
            )

    def _show_board_picker(self):
        self._board_frame.pack(anchor="w", fill="x", pady=(8, 0))
        self._board_display_list = [
            f"\u2605 {b.name} ({b.project_key})" if b.favourite
            else f"{b.name} ({b.project_key})"
            for b in self._jira_boards
        ]
        self._jira_board_combo["values"] = self._board_display_list

        # Pre-select existing board
        if self._config.jira_board_id:
            for i, b in enumerate(self._jira_boards):
                if b.id == self._config.jira_board_id:
                    self._jira_board_combo.current(i)
                    break

    def _filter_boards(self, _event=None):
        typed = self._jira_board_var.get().lower()
        filtered = [d for d in self._board_display_list if typed in d.lower()]
        self._jira_board_combo["values"] = filtered

    # ── Step 4: Slack Webhook ────────────────────────────────────────────────

    def _build_step_slack(self):
        ttk.Label(self._container, text="Slack Webhook",
                  font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(self._container,
                  text="Set up an Incoming Webhook so the app can post PR notifications.",
                  wraplength=500).pack(anchor="w", pady=(0, 8))

        # Instructions
        steps = [
            ('Create a new Slack app — choose "From scratch", pick a name and workspace:',
             "Create Slack App (api.slack.com)",
             "https://api.slack.com/apps?new_app=1"),
            ('In the app settings, click "Incoming Webhooks" and toggle it on.',
             None, None),
            ('Click "Add New Webhook to Workspace", choose a channel, then copy the URL below.',
             None, None),
        ]
        for i, (text, btn_text, btn_url) in enumerate(steps, 1):
            row = ttk.Frame(self._container)
            row.pack(anchor="w", fill="x", pady=(0, 4))
            ttk.Label(row, text=f"{i}.").pack(side="left", anchor="n")
            col = ttk.Frame(row)
            col.pack(side="left", anchor="w", padx=(5, 0))
            ttk.Label(col, text=text, wraplength=470).pack(anchor="w")
            if btn_text and btn_url:
                url = btn_url
                ttk.Button(col, text=btn_text,
                           command=lambda u=url: webbrowser.open(u)).pack(anchor="w", pady=(2, 0))

        # Webhook URL entry
        ttk.Label(self._container, text="Webhook URL:").pack(anchor="w", pady=(10, 0))
        self._slack_webhook_var = tk.StringVar(value=self._config.slack_webhook_url)
        webhook_entry = ttk.Entry(self._container, textvariable=self._slack_webhook_var, width=60)
        webhook_entry.pack(anchor="w", pady=(0, 5))
        webhook_entry.focus_set()
        ttk.Label(self._container,
                  text="Example: https://hooks.slack.com/services/T.../B.../xxx",
                  foreground="gray").pack(anchor="w", pady=(0, 8))

        # Send Test button
        test_frame = ttk.Frame(self._container)
        test_frame.pack(anchor="w", fill="x", pady=(0, 5))
        self._slack_test_btn = ttk.Button(test_frame, text="Send Test Message",
                                          command=self._send_slack_test)
        self._slack_test_btn.pack(side="left")
        self._slack_test_progress = ttk.Progressbar(test_frame, mode="indeterminate", length=120)

        self._slack_test_label = ttk.Label(self._container, text="")
        self._slack_test_label.pack(anchor="w", pady=(3, 0))

        # Verification code entry (hidden until test sent)
        self._slack_verify_frame = ttk.Frame(self._container)
        # Don't pack yet
        ttk.Label(self._slack_verify_frame,
                  text="Enter the verification code from the Slack message:").pack(anchor="w")
        verify_row = ttk.Frame(self._slack_verify_frame)
        verify_row.pack(anchor="w", fill="x", pady=(3, 0))
        self._slack_code_var = tk.StringVar()
        ttk.Entry(verify_row, textvariable=self._slack_code_var, width=15).pack(side="left", padx=(0, 5))
        ttk.Button(verify_row, text="Verify", command=self._verify_slack_code).pack(side="left")

        self._slack_verify_label = ttk.Label(self._slack_verify_frame, text="")
        self._slack_verify_label.pack(anchor="w", pady=(3, 0))

        # If already verified from a previous visit, show status
        if self._slack_verified:
            self._slack_test_label.configure(text="Webhook verified.", foreground="green")

    def _send_slack_test(self):
        url = self._slack_webhook_var.get().strip()
        if not url:
            self._slack_test_label.configure(text="Enter a webhook URL first.", foreground="red")
            return

        import random
        self._slack_verify_code = f"{random.randint(100000, 999999)}"
        self._slack_verified = False

        self._slack_test_btn.configure(state="disabled")
        self._slack_test_progress.pack(side="left", padx=(10, 0))
        self._slack_test_progress.start()
        self._slack_test_label.configure(text="Sending...", foreground="gray")

        code = self._slack_verify_code

        def _do_send():
            from slack_client import send_webhook
            msg = f"Claude Management setup verification code: *{code}*"
            ok = send_webhook(url, msg)
            self._bg_queue.put(lambda: self._on_slack_test_done(ok))

        threading.Thread(target=_do_send, daemon=True).start()

    def _on_slack_test_done(self, ok: bool):
        self._slack_test_progress.stop()
        self._slack_test_progress.pack_forget()
        self._slack_test_btn.configure(state="normal")

        if ok:
            self._slack_test_label.configure(
                text="Test message sent! Check Slack for the verification code.",
                foreground="green",
            )
            self._slack_verify_frame.pack(anchor="w", fill="x", pady=(8, 0))
        else:
            self._slack_test_label.configure(
                text="Failed to send. Check the webhook URL and try again.",
                foreground="red",
            )

    def _verify_slack_code(self):
        entered = self._slack_code_var.get().strip()
        if entered == self._slack_verify_code:
            self._slack_verified = True
            self._slack_verify_label.configure(text="Verified!", foreground="green")
        else:
            self._slack_verified = False
            self._slack_verify_label.configure(text="Code does not match. Try again.", foreground="red")


def _get_git_email() -> str:
    """Read user.email from git config, or return empty string."""
    try:
        r = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""
