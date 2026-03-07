"""Read-only markdown summary viewer panel."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class SummaryPanel(ttk.Frame):
    """Displays the session summary log as styled read-only text."""

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        # ── Status bar ────────────────────────────────────────────────────
        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", padx=5, pady=(5, 0))

        self._title_label = ttk.Label(
            status_frame, text="Session Summary", foreground="#569cd6"
        )
        self._title_label.pack(side="left", fill="x", expand=True)

        # ── Text display ──────────────────────────────────────────────────
        text_frame = ttk.Frame(self)
        text_frame.pack(fill="both", expand=True, padx=5, pady=5)

        self._text = tk.Text(
            text_frame,
            wrap="word",
            state="disabled",
            font=("Menlo", 10),
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
            selectbackground="#264f78",
            borderwidth=0,
            highlightthickness=0,
            padx=6,
            pady=4,
        )
        scrollbar = ttk.Scrollbar(
            text_frame, orient="vertical", command=self._text.yview
        )
        self._text.configure(yscrollcommand=scrollbar.set)

        self._text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Text tags
        self._text.tag_configure(
            "heading", foreground="#569cd6", font=("Menlo", 10, "bold")
        )
        self._text.tag_configure("separator", foreground="#3e3e3e")
        self._text.tag_configure("timestamp", foreground="#636c76", font=("Menlo", 9))
        self._text.tag_configure("body", foreground="#d4d4d4")

    # ── Public API ────────────────────────────────────────────────────────

    def load(self, content: str):
        """Replace all content with the given summary text."""
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._insert_styled(content)
        self._text.configure(state="disabled")
        self._text.see("end")

    def clear(self):
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")

    def append_entry(self, text: str):
        """Append a new entry and scroll to the end."""
        self._text.configure(state="normal")
        self._insert_styled(text)
        self._text.configure(state="disabled")
        self._text.see("end")

    # ── Internal ──────────────────────────────────────────────────────────

    def _insert_styled(self, content: str):
        """Insert content with basic styling."""
        for line in content.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            if stripped.startswith("## "):
                # Legacy heading format
                self._text.insert("end", line, "heading")
            elif stripped.startswith("[") and "] " in stripped:
                # Compact format: [HH:MM:SS] EntryType
                bracket_end = stripped.index("] ") + 2
                self._text.insert("end", stripped[:bracket_end], "timestamp")
                self._text.insert("end", stripped[bracket_end:] + "\n", "heading")
            elif stripped == "---":
                self._text.insert("end", line, "separator")
            else:
                self._text.insert("end", line, "body")
