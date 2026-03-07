"""Modal dialogs for session creation."""

from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

from config import Config

log = logging.getLogger("claude_mgmt")


class NewSessionDialog(tk.Toplevel):
    """Dialog for creating a new Claude session."""

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.title("New Claude Session")
        self.geometry("450x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[tuple[str, str]] = None

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        # Name
        ttk.Label(frame, text="Session Name:").pack(anchor="w")
        self._name_var = tk.StringVar()
        name_entry = ttk.Entry(frame, textvariable=self._name_var, width=40)
        name_entry.pack(anchor="w", pady=(0, 10))
        name_entry.focus_set()

        # Prompt
        ttk.Label(frame, text="Initial Prompt:").pack(anchor="w")
        self._prompt_text = tk.Text(frame, height=5, width=45, wrap="word")
        self._prompt_text.pack(anchor="w", pady=(0, 15))

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side="right", padx=(5, 0))
        ttk.Button(btn_frame, text="Create", command=self._create).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.wait_window()

    def _create(self):
        name = self._name_var.get().strip()
        prompt = self._prompt_text.get("1.0", "end").strip()

        if not name:
            messagebox.showwarning("Validation", "Session name is required.", parent=self)
            return

        self.result = (name, prompt)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class StartTicketDialog(tk.Toplevel):
    """Dialog for starting work on a Jira ticket — manual entry or board browsing."""

    TICKET_RE = re.compile(r"^[A-Za-z]+-[0-9]+$")

    def __init__(self, parent: tk.Widget, config: Config):
        super().__init__(parent)
        self.title("Start Working a Ticket")
        self.geometry("720x500")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[tuple[str, str]] = None  # (ticket_id, summary)
        self._config = config
        self._bg_queue: queue.Queue = queue.Queue()
        self._board_tickets: list[tuple[str, str, str, str]] = []  # (key, summary, status, assignee)

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        # ── Mode selector ──
        default_mode = "browse" if config.jira_board_id else "manual"
        self._mode_var = tk.StringVar(value=default_mode)
        mode_frame = ttk.Frame(frame)
        mode_frame.pack(anchor="w", pady=(0, 5))
        ttk.Radiobutton(mode_frame, text="Enter Ticket ID", variable=self._mode_var,
                        value="manual", command=self._switch_mode).pack(side="left", padx=(0, 15))
        ttk.Radiobutton(mode_frame, text="Browse Board", variable=self._mode_var,
                        value="browse", command=self._switch_mode).pack(side="left")

        # ── Content area (swapped based on mode) ──
        self._content = ttk.Frame(frame)
        self._content.pack(fill="both", expand=True, pady=(5, 0))

        # ── Buttons (always visible) ──
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side="right", padx=(5, 0))
        self._start_btn = ttk.Button(btn_frame, text="Start", command=self._start)
        self._start_btn.pack(side="right")

        self._switch_mode()

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._poll_bg()
        self.wait_window()

    def _poll_bg(self):
        try:
            while True:
                callback = self._bg_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_bg)

    def _clear_content(self):
        for w in self._content.winfo_children():
            w.destroy()

    def _switch_mode(self):
        self._clear_content()
        if self._mode_var.get() == "manual":
            self._build_manual()
        else:
            self._build_browse()

    # ── Manual mode ──────────────────────────────────────────────────────────

    def _build_manual(self):
        ttk.Label(self._content, text="Ticket ID (e.g. KAN-42):").pack(anchor="w")
        self._ticket_var = tk.StringVar()
        entry = ttk.Entry(self._content, textvariable=self._ticket_var, width=20)
        entry.pack(anchor="w", pady=(0, 10))
        entry.focus_set()

    # ── Browse mode ──────────────────────────────────────────────────────────

    def _build_browse(self):
        if not self._config.jira_board_id:
            ttk.Label(self._content,
                      text="No Jira board configured.\n"
                           "Set it in File > Re-run Setup (Step 4).",
                      foreground="red", wraplength=500).pack(anchor="w", pady=(0, 10))
            return

        top_row = ttk.Frame(self._content)
        top_row.pack(fill="x", pady=(0, 5))
        board_label = self._config.jira_board_name or f"Board #{self._config.jira_board_id}"
        ttk.Label(top_row, text=f"Board: {board_label}", foreground="gray",
                  wraplength=400).pack(side="left", fill="x", expand=True)
        self._fetch_btn = ttk.Button(top_row, text="Fetch Tickets", command=self._fetch_tickets)
        self._fetch_btn.pack(side="right")

        self._browse_progress = ttk.Progressbar(self._content, mode="indeterminate")
        self._browse_progress.pack(fill="x", pady=(0, 5))
        self._browse_progress.pack_forget()

        # Treeview for tickets
        tree_frame = ttk.Frame(self._content)
        tree_frame.pack(fill="both", expand=True)

        columns = ("ticket", "summary", "status", "assignee")
        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self._tree.heading("ticket", text="Ticket")
        self._tree.heading("summary", text="Summary")
        self._tree.heading("status", text="Status")
        self._tree.heading("assignee", text="Assignee")
        self._tree.column("ticket", width=80, minwidth=60)
        self._tree.column("summary", width=280, minwidth=150)
        self._tree.column("status", width=90, minwidth=60)
        self._tree.column("assignee", width=120, minwidth=80)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Pre-populate if we already have tickets, otherwise auto-fetch
        if self._board_tickets:
            self._populate_tree(self._board_tickets)
        else:
            self._fetch_tickets()

    def _fetch_tickets(self):
        self._fetch_btn.configure(state="disabled")
        self._browse_progress.pack(fill="x", pady=(0, 5))
        self._browse_progress.start()

        config = self._config
        board_id = config.jira_board_id

        def _do_fetch():
            from jira_client import JiraClient
            try:
                tickets = JiraClient(config).fetch_board_issues(board_id)
                result = [(t.key, t.summary, t.status, t.assignee) for t in tickets]
            except Exception:
                result = []
            self._bg_queue.put(lambda: self._on_tickets_fetched(result))

        threading.Thread(target=_do_fetch, daemon=True).start()

    def _on_tickets_fetched(self, tickets: list[tuple[str, str, str, str]]):
        self._browse_progress.stop()
        self._browse_progress.pack_forget()
        self._fetch_btn.configure(state="normal")

        if not tickets:
            messagebox.showinfo("No Tickets",
                                "No In Progress or To Do tickets found (or fetch failed).",
                                parent=self)
            return

        self._board_tickets = tickets
        self._populate_tree(tickets)

    def _populate_tree(self, tickets: list[tuple[str, str, str, str]]):
        self._tree.delete(*self._tree.get_children())
        for key, summary, status, assignee in tickets:
            self._tree.insert("", "end", values=(key, summary, status, assignee))

    # ── Actions ──────────────────────────────────────────────────────────────

    def _start(self):
        if self._mode_var.get() == "manual":
            ticket = self._ticket_var.get().strip().upper()
            if not self.TICKET_RE.match(ticket):
                messagebox.showwarning("Validation",
                                       "Ticket ID must match format like KAN-42.",
                                       parent=self)
                return
            summary = ""
        else:
            # Browse mode — get selected row
            sel = self._tree.selection()
            if not sel:
                messagebox.showwarning("Validation",
                                       "Please select a ticket from the list.",
                                       parent=self)
                return
            values = self._tree.item(sel[0], "values")
            ticket = values[0]
            summary = values[1] if len(values) > 1 else ""

        self.result = (ticket, summary)
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class SkillSelectionDialog(tk.Toplevel):
    """Let the user pick which skill step to start from in a multi-step workflow."""

    def __init__(self, parent: tk.Widget, commands: list[str]):
        super().__init__(parent)
        self.title("Select Starting Step")
        self.geometry("500x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[int] = None  # index to start from, or None if cancelled

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="This workflow has multiple steps.\n"
                  "Select which step to start from:",
                  wraplength=450).pack(anchor="w", pady=(0, 10))

        # Listbox with numbered steps
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        self._listbox = tk.Listbox(list_frame, selectmode="browse", font=("Menlo", 11))
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self._listbox.yview)
        self._listbox.configure(yscrollcommand=scrollbar.set)
        self._listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for i, cmd in enumerate(commands):
            self._listbox.insert("end", f"  {i + 1}. {cmd}")
        self._listbox.selection_set(0)

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_frame, text="Cancel", command=self._cancel).pack(side="right", padx=(5, 0))
        ttk.Button(btn_frame, text="Start", command=self._start).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.wait_window()

    def _start(self):
        sel = self._listbox.curselection()
        self.result = sel[0] if sel else 0
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class EditReposDialog(tk.Toplevel):
    """Dialog for updating the monitored repository list."""

    def __init__(self, parent: tk.Widget, config: Config):
        super().__init__(parent)
        self.title("Edit Repositories")
        self.geometry("450x450")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[list[str]] = None
        self._config = config
        self._repo_vars: dict[str, tk.BooleanVar] = {}
        self._bg_queue: queue.Queue = queue.Queue()

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text=f"Repositories for {config.org}",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w", pady=(0, 5))

        # Check All / Uncheck All
        btn_row = ttk.Frame(frame)
        btn_row.pack(anchor="w", fill="x", pady=(0, 5))
        ttk.Button(btn_row, text="Check All", command=self._check_all).pack(side="left", padx=(0, 5))
        ttk.Button(btn_row, text="Uncheck All", command=self._uncheck_all).pack(side="left")

        # Progress bar
        self._progress = ttk.Progressbar(frame, mode="indeterminate")
        self._progress.pack(fill="x", pady=(0, 5))

        # Scrollable repo list
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self._repo_inner = ttk.Frame(canvas)
        self._repo_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._repo_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Buttons
        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Cancel", command=self._cancel).pack(side="right", padx=(5, 0))
        ttk.Button(bottom, text="Save", command=self._save).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._fetch_repos()
        self._poll_bg()
        self.wait_window()

    def _poll_bg(self):
        try:
            while True:
                callback = self._bg_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll_bg)

    def _fetch_repos(self):
        self._progress.start()
        org = self._config.org

        def _fetch():
            try:
                import json as _json
                r = subprocess.run(
                    ["gh", "repo", "list", org, "--json", "name", "--limit", "100"],
                    capture_output=True, text=True, timeout=30,
                )
                repos = sorted(item["name"] for item in _json.loads(r.stdout)) if r.returncode == 0 else []
            except Exception:
                repos = []
            self._bg_queue.put(lambda: self._on_fetched(repos))

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_fetched(self, repos: list[str]):
        self._progress.stop()
        self._progress.pack_forget()
        pre_selected = set(self._config.repos)
        for name in repos:
            var = tk.BooleanVar(value=name in pre_selected)
            self._repo_vars[name] = var
            ttk.Checkbutton(self._repo_inner, text=name, variable=var).pack(anchor="w")

    def _check_all(self):
        for var in self._repo_vars.values():
            var.set(True)

    def _uncheck_all(self):
        for var in self._repo_vars.values():
            var.set(False)

    def _save(self):
        selected = [name for name, var in self._repo_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("Validation", "Select at least one repo.", parent=self)
            return
        self.result = selected
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()
