"""Working Sessions tab — session list with embedded chat panel."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Callable, Optional

from config import Config
from models import ManagedSession, SessionStatus
from widgets.chat_panel import ChatPanel
from widgets.summary_panel import SummaryPanel


class SessionTab(ttk.Frame):
    def __init__(
        self,
        parent: ttk.Notebook,
        config: Config,
        on_new_session: Callable,
        on_start_ticket: Callable,
        on_open_session: Callable[[ManagedSession], None],
        on_send_message: Callable[[str, str], None],
        on_stop_session: Callable[[str], None],
        on_remove_session: Callable[[str], None] = lambda name: None,
    ):
        super().__init__(parent)
        self._config = config
        self._on_new_session = on_new_session
        self._on_start_ticket = on_start_ticket
        self._on_open_session = on_open_session
        self._on_send_message = on_send_message
        self._on_stop_session = on_stop_session
        self._on_remove_session = on_remove_session
        self._sessions: list[ManagedSession] = []
        self._active_session_name: Optional[str] = None
        self._summary_visible = False

        self._build_toolbar()
        self._build_paned()

    def _build_toolbar(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=5, pady=5)

        ttk.Button(toolbar, text="New Session",
                   command=self._on_new_session).pack(side="left", padx=(0, 5))
        ttk.Button(toolbar, text="Start Working a Ticket",
                   command=self._on_start_ticket).pack(side="left", padx=(0, 5))

        self._count_label = ttk.Label(toolbar, text="0 sessions")
        self._count_label.pack(side="right")

    def _build_paned(self):
        self._paned = ttk.PanedWindow(self, orient="horizontal")
        self._paned.pack(fill="both", expand=True, padx=5, pady=5)

        # ── Left pane: session list ───────────────────────────────────────
        self._left_frame = ttk.Frame(self._paned)
        self._paned.add(self._left_frame, weight=1)
        self._left_visible = True

        columns = ("name", "status", "ticket", "pr", "remove")
        self._tree = ttk.Treeview(
            self._left_frame, columns=columns, show="headings", selectmode="browse"
        )

        self._tree.heading("name", text="Name")
        self._tree.heading("status", text="Status")
        self._tree.heading("ticket", text="Jira Ticket")
        self._tree.heading("pr", text="PR")
        self._tree.heading("remove", text="")

        self._tree.column("name", width=250, minwidth=150)
        self._tree.column("status", width=80, minwidth=60)
        self._tree.column("ticket", width=100, minwidth=60)
        self._tree.column("pr", width=70, minwidth=50, anchor="center")
        self._tree.column("remove", width=30, minwidth=30, anchor="center", stretch=False)

        scrollbar = ttk.Scrollbar(self._left_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)

        self._tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._tree.tag_configure("running", foreground="#2da44e")
        self._tree.tag_configure("attention", foreground="#d29922")
        self._tree.tag_configure("stopped", foreground="#636c76")

        self._default_cursor = self._tree["cursor"] or ""
        self._tree.bind("<ButtonRelease-1>", self._on_click)
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Button-2>", self._on_right_click)
        self._tree.bind("<Button-3>", self._on_right_click)
        self._tree.bind("<Control-Button-1>", self._on_right_click)
        # Selection change no longer needed (Remove button removed from toolbar)
        self._tree.bind("<Motion>", self._on_motion)
        self._tree.bind("<Leave>", self._on_leave)

        self._ctx_menu = tk.Menu(self, tearoff=0)
        self._ctx_menu.add_command(label="Remove Session", command=self._ctx_remove)

        # ── Right pane: vertical container (chat + summary) ───────────────
        self._right_container = ttk.PanedWindow(self._paned, orient="vertical")

        self._chat_panel = ChatPanel(
            self._right_container,
            on_send=self._handle_chat_send,
            on_stop=self._handle_chat_stop,
            on_close=self.close_panel,
            on_toggle_summary=self._toggle_summary,
        )
        self._right_container.add(self._chat_panel, weight=2)

        self._summary_panel = SummaryPanel(self._right_container)
        # Don't add summary to right_container yet — toggled on demand
        # Don't add right_container to paned yet — shown when a session is opened

    def update_sessions(self, sessions: list[ManagedSession],
                        attention_names: set[str] = frozenset()):
        # Preserve selection
        selected_name = None
        sel = self._tree.selection()
        if sel:
            selected_name = self._tree.item(sel[0], "values")[0]

        self._sessions = sessions
        self._tree.delete(*self._tree.get_children())

        running_count = 0
        for s in sorted(sessions, key=lambda s: s.created_at, reverse=True):
            if s.status == SessionStatus.RUNNING:
                running_count += 1
            if s.name in attention_names:
                tag = "attention"
                status_display = "Needs Input"
            elif s.status == SessionStatus.RUNNING:
                tag = "running"
                status_display = "Running"
            else:
                tag = "stopped"
                status_display = "Stopped"
            pr_display = "\U0001F517 Open" if s.pr_url else ""
            remove_display = "\u2715" if s.status == SessionStatus.STOPPED else ""
            self._tree.insert("", "end", values=(
                s.name,
                status_display,
                s.ticket_id or "",
                pr_display,
                remove_display,
            ), tags=(tag,))

        total = len(sessions)
        self._count_label.configure(text=f"{running_count} running / {total} total")

        # Restore selection and focus
        if selected_name:
            for item in self._tree.get_children():
                if self._tree.item(item, "values")[0] == selected_name:
                    self._tree.selection_set(item)
                    self._tree.focus(item)
                    break

    @property
    def chat_panel(self) -> ChatPanel:
        return self._chat_panel

    @property
    def summary_panel(self) -> SummaryPanel:
        return self._summary_panel

    def select_and_open_session(self, session: ManagedSession):
        """Programmatically select a session row and show the chat panel.

        Does NOT trigger on_open_session — the caller manages process start.
        """
        for item in self._tree.get_children():
            values = self._tree.item(item, "values")
            if values[0] == session.name:
                self._tree.selection_set(item)
                break
        self._chat_panel.set_pr_url(session.pr_url)
        self._show_panel(session.name)

    def close_panel(self):
        """Hide the chat panel and summary panel."""
        self._hide_summary()
        if self._panel_visible():
            self._paned.remove(self._right_container)
        self._active_session_name = None

    def _panel_visible(self) -> bool:
        return str(self._right_container) in self._paned.panes()

    def _show_panel(self, name: str):
        """Show the chat panel without triggering process start."""
        self._active_session_name = name
        if not self._panel_visible():
            self._paned.add(self._right_container, weight=2)
        # Restore left pane if it was hidden
        if not self._left_visible:
            self._toggle_session_list()

    def _toggle_summary(self):
        if self._summary_visible:
            self._hide_summary()
        else:
            self._show_summary()

    def _show_summary(self):
        if not self._summary_visible:
            self._right_container.add(self._summary_panel, weight=1)
            self._summary_visible = True

    def _hide_summary(self):
        if self._summary_visible:
            self._right_container.remove(self._summary_panel)
            self._summary_visible = False

    def _toggle_session_list(self):
        """Collapse/expand the session list for more space."""
        if self._left_visible:
            self._paned.remove(self._left_frame)
            self._left_visible = False
        else:
            # Re-add at position 0 (before right_container)
            self._paned.insert(0, self._left_frame, weight=1)
            self._left_visible = True

    def _open_panel(self, session: ManagedSession):
        """Show panel and trigger on_open_session (used by user double-click)."""
        self._show_panel(session.name)
        self._on_open_session(session)

    def _get_selected_session(self) -> Optional[ManagedSession]:
        sel = self._tree.selection()
        if not sel:
            return None
        values = self._tree.item(sel[0], "values")
        name = values[0]
        for s in self._sessions:
            if s.name == name:
                return s
        return None

    def _on_click(self, event):
        col = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
        session = self._get_selected_session()
        if not session:
            return
        if col == "#4":
            if session.pr_url:
                webbrowser.open(session.pr_url)
        elif col == "#3":
            # Ticket column → open Jira
            if session.ticket_id and self._config.jira_base_url:
                base = self._config.jira_base_url.rstrip("/")
                if not base.endswith("/browse"):
                    base += "/browse"
                webbrowser.open(f"{base}/{session.ticket_id}")
        elif col == "#5":
            # Remove column
            if session.status == SessionStatus.STOPPED:
                self._ctx_remove()
        else:
            # Name or other column — open the panel
            self._open_panel(session)

    def _on_motion(self, event):
        col = self._tree.identify_column(event.x)
        item = self._tree.identify_row(event.y)
        if item and col == "#1":
            # Name column — always clickable
            self._tree.configure(cursor="hand2")
            return
        if item and col == "#4":
            values = self._tree.item(item, "values")
            if values and values[3]:
                self._tree.configure(cursor="hand2")
                return
        if item and col == "#3":
            values = self._tree.item(item, "values")
            if values and values[2]:
                self._tree.configure(cursor="hand2")
                return
        if item and col == "#5":
            values = self._tree.item(item, "values")
            if values and values[4]:
                self._tree.configure(cursor="hand2")
                return
        self._tree.configure(cursor=self._default_cursor)

    def _on_leave(self, _event):
        self._tree.configure(cursor=self._default_cursor)

    def _on_double_click(self, event):
        # All actions handled by single click now
        pass

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
        session = self._get_selected_session()
        if not session:
            return
        # Only allow removing stopped sessions
        state = "normal" if session.status == SessionStatus.STOPPED else "disabled"
        self._ctx_menu.entryconfigure(0, state=state)
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _ctx_remove(self):
        from tkinter import messagebox
        session = self._get_selected_session()
        if not session or session.status != SessionStatus.STOPPED:
            return
        if not messagebox.askyesno(
            "Remove Session",
            f"Remove session \"{session.name}\"?\n\n"
            "All session data (chat history, logs) will be permanently lost.",
            parent=self,
        ):
            return
        # Close panel if this session is active
        if self._active_session_name == session.name:
            self.close_panel()
        self._on_remove_session(session.name)

    def _handle_chat_send(self, text: str):
        if self._active_session_name:
            self._on_send_message(self._active_session_name, text)

    def _handle_chat_stop(self):
        if self._active_session_name:
            self._on_stop_session(self._active_session_name)


