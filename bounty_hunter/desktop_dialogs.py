"""Dialogs for first-run setup and hash-bound human approvals."""
import json
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .desktop_settings import SOURCES, normalize_settings
from .dashboard import BG, PANEL, RAISED, TEXT, MUTED, ACCENT, FONT


class SetupDialog(tk.Toplevel):
    def __init__(self, parent, settings: dict, *, first_run=False, on_save=None):
        super().__init__(parent)
        self.title("Bounty Hunter — First-run setup" if first_run else "Bounty Hunter — Settings")
        self.configure(bg=BG)
        self.geometry("760x750")
        self.minsize(690, 680)
        self.transient(parent)
        self.first_run = first_run
        self.on_save = on_save
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.workspace = tk.StringVar(value=settings["workspace"])
        self.source_flags = {
            name: tk.BooleanVar(value=settings["sources"].get(name, False))
            for name in SOURCES
        }
        self.orgs = tk.StringVar(value=", ".join(settings["algora_organizations"]))
        self.country = tk.StringVar(value=settings["boss_payout_country"])
        self.token_env = tk.StringVar(value=settings["github_token_env"])
        self.auto_scan = tk.BooleanVar(value=settings["auto_scan"])
        self.interval = tk.StringVar(value=str(settings["scan_interval_minutes"]))
        self.timeout = tk.StringVar(value=str(settings["timeout_seconds"]))
        self._build()
        self.grab_set()
        self.focus_force()

    def _label(self, parent, text, size=10, color=TEXT, bold=False):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color,
                        font=(FONT, size, "bold" if bold else "normal"),
                        anchor="w", justify="left")

    def _entry(self, parent, variable, width=60):
        entry = tk.Entry(parent, textvariable=variable, width=width, bg=RAISED,
                         fg=TEXT, insertbackground=TEXT, relief="flat",
                         font=(FONT, 11))
        entry.pack(anchor="w", pady=(6, 14), ipady=7, fill="x")
        return entry

    def _build(self):
        footer = tk.Frame(self, bg=BG, padx=24, pady=12)
        footer.pack(side="bottom", fill="x")
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        outer = tk.Frame(canvas, bg=BG, padx=24, pady=18)
        content_id = canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(content_id, width=event.width))
        self._label(outer, "Set up your local Bounty Hunter", 18, TEXT, True).pack(anchor="w")
        self._label(
            outer, "No command line required. Settings never store credentials or wallet keys.",
            10, MUTED
        ).pack(anchor="w", pady=(6, 18))

        self._label(outer, "WORKSPACE — local reports and history database", 10, MUTED, True).pack(anchor="w")
        workspace_row = tk.Frame(outer, bg=BG)
        workspace_row.pack(fill="x", pady=(5, 14))
        tk.Entry(
            workspace_row, textvariable=self.workspace, bg=RAISED, fg=TEXT,
            insertbackground=TEXT, relief="flat", font=(FONT, 10)
        ).pack(side="left", fill="x", expand=True, ipady=8)
        tk.Button(
            workspace_row, text="Browse...", command=self._browse,
            bg=RAISED, fg=TEXT, relief="flat", padx=12, pady=6,
        ).pack(side="left", padx=(8, 0))

        self._label(outer, "BOUNTY PLATFORMS", 10, MUTED, True).pack(anchor="w")
        source_row = tk.Frame(outer, bg=BG)
        source_row.pack(anchor="w", pady=(6, 14))
        for i, name in enumerate(SOURCES):
            tk.Checkbutton(
                source_row, text=name.capitalize(), variable=self.source_flags[name],
                bg=BG, fg=TEXT, selectcolor=RAISED, activebackground=BG,
                activeforeground=TEXT, font=(FONT, 10)
            ).grid(row=0, column=i, padx=(0, 13))

        self._label(outer, "ALGORA ORGANIZATIONS (comma-separated; optional)", 10, MUTED, True).pack(anchor="w")
        self._entry(outer, self.orgs)
        self._label(outer, "BOSS PAYOUT COUNTRY (two-letter residency code; optional)", 10, MUTED, True).pack(anchor="w")
        self._entry(outer, self.country, width=12)
        self._label(
            outer, "Confirm payout eligibility with each platform. Country alone does not establish eligibility.",
            9, MUTED
        ).pack(anchor="w", pady=(0, 12))
        self._label(outer, "GITHUB TOKEN ENVIRONMENT VARIABLE (not the token)", 10, MUTED, True).pack(anchor="w")
        self._entry(outer, self.token_env)
        self._label(outer, "REQUEST TIMEOUT (5–120 seconds)", 10, MUTED, True).pack(anchor="w")
        self._entry(outer, self.timeout, width=12)
        self._label(
            outer, "Public GitHub scanning can work without a token; requests may be rate-limited.",
            9, MUTED
        ).pack(anchor="w", pady=(0, 12))

        auto_row = tk.Frame(outer, bg=BG)
        auto_row.pack(fill="x", pady=(0, 9))
        tk.Checkbutton(
            auto_row, text="Automatically scan while the app is running",
            variable=self.auto_scan, bg=BG, fg=TEXT, selectcolor=RAISED,
            activebackground=BG, activeforeground=TEXT,
        ).pack(side="left")
        self._label(auto_row, "Every (minutes):", 9, MUTED).pack(side="left", padx=(16, 5))
        tk.Entry(
            auto_row, textvariable=self.interval, width=7, bg=RAISED,
            fg=TEXT, insertbackground=TEXT, relief="flat"
        ).pack(side="left")

        readiness = tk.Frame(outer, bg=PANEL, padx=14, pady=12)
        readiness.pack(fill="x", pady=(9, 18))
        self._label(readiness, "OPTIONAL TOOLS — only needed for AI coding and PR publication",
                    10, TEXT, True).pack(anchor="w", pady=(0, 7))
        for name, choices in [
            ("Git", ("git",)),
            ("GitHub CLI", ("gh",)),
            ("Codex CLI", ("codex", "codex.cmd")),
            ("Docker / Podman", ("docker", "podman")),
        ]:
            found = next((shutil.which(c) for c in choices if shutil.which(c)), None)
            self._label(
                readiness,
                f'{"Detected" if found else "Missing"}: {name}' +
                (f" — {Path(found).name}" if found else ""),
                9, ACCENT if found else MUTED
            ).pack(anchor="w")
        self._label(
            readiness, "Detection checks installed commands only, not logins or Docker daemon readiness.",
            9, MUTED
        ).pack(anchor="w", pady=(5, 0))

        actions = footer
        actions.pack(fill="x")
        tk.Button(
            actions, text="Save & continue", command=self._save,
            bg=ACCENT, fg=BG, relief="flat", padx=20, pady=11,
            font=(FONT, 10, "bold")
        ).pack(side="right")
        if not self.first_run:
            tk.Button(
                actions, text="Cancel", command=self.destroy,
                bg=RAISED, fg=TEXT, relief="flat", padx=18, pady=11,
            ).pack(side="right", padx=(0, 9))

    def _browse(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.workspace.get())
        if folder:
            self.workspace.set(folder)

    def _save(self):
        try:
            values = normalize_settings({
                "workspace": self.workspace.get(),
                "sources": {key: var.get() for key, var in self.source_flags.items()},
                "algora_organizations": [
                    value.strip() for value in self.orgs.get().replace("\n", ",").split(",")
                    if value.strip()
                ],
                "boss_payout_country": self.country.get(),
                "github_token_env": self.token_env.get(),
                "auto_scan": self.auto_scan.get(),
                "scan_interval_minutes": self.interval.get(),
                "timeout_seconds": self.timeout.get(),
            })
            if callable(self.on_save):
                self.on_save(values)
            self.grab_release()
            self.destroy()
        except (ValueError, OSError) as error:
            messagebox.showerror("Cannot save setup", str(error), parent=self)

    def _close(self):
        if self.first_run:
            self.master.destroy()
        else:
            self.destroy()


class ReviewDialog(tk.Toplevel):
    def __init__(self, parent, title: str, summary: str, content: str,
                 *, required_phrase: str, on_approve):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.geometry("880x690")
        self.minsize(720, 580)
        self.transient(parent)
        self.on_approve = on_approve
        self.required_phrase = required_phrase
        self.confirmation = tk.StringVar()
        self.note = tk.StringVar()
        self.acknowledge = tk.BooleanVar(value=False)

        top = tk.Frame(self, bg=BG, padx=18, pady=12)
        top.pack(fill="x")
        tk.Label(
            top, text=summary, bg=BG, fg=TEXT, wraplength=820,
            justify="left", font=(FONT, 11, "bold")
        ).pack(anchor="w")
        viewer = tk.Frame(self, bg=BG, padx=18)
        viewer.pack(fill="both", expand=True)
        textbox = tk.Text(
            viewer, bg=PANEL, fg=TEXT, relief="flat", wrap="none",
            font=("Consolas", 9), padx=12, pady=12
        )
        scroll_y = tk.Scrollbar(viewer, command=textbox.yview)
        textbox.configure(yscrollcommand=scroll_y.set)
        scroll_y.pack(side="right", fill="y")
        textbox.pack(side="left", fill="both", expand=True)
        textbox.insert("1.0", content)
        textbox.configure(state="disabled")

        footer = tk.Frame(self, bg=BG, padx=18, pady=14)
        footer.pack(fill="x")
        tk.Checkbutton(
            footer, text="I reviewed the details above and approve this exact action.",
            variable=self.acknowledge, bg=BG, fg=TEXT, selectcolor=RAISED,
            activebackground=BG, activeforeground=TEXT
        ).pack(anchor="w", pady=(0, 8))
        tk.Label(
            footer, text="Approval note (required):", bg=BG, fg=MUTED
        ).pack(anchor="w")
        tk.Entry(
            footer, textvariable=self.note, bg=RAISED, fg=TEXT,
            insertbackground=TEXT, relief="flat"
        ).pack(fill="x", pady=(4, 9), ipady=5)
        if required_phrase:
            tk.Label(
                footer, text=f"Type {required_phrase} to confirm public publication:",
                bg=BG, fg=MUTED
            ).pack(anchor="w")
            tk.Entry(
                footer, textvariable=self.confirmation, bg=RAISED, fg=TEXT,
                insertbackground=TEXT, relief="flat"
            ).pack(fill="x", pady=(4, 9), ipady=5)
        buttons = tk.Frame(footer, bg=BG)
        buttons.pack(fill="x")
        tk.Button(
            buttons, text="Cancel", command=self.destroy, bg=RAISED, fg=TEXT,
            relief="flat", padx=16, pady=9
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            buttons, text="Approve action", command=self._approve, bg=ACCENT, fg=BG,
            relief="flat", padx=16, pady=9, font=(FONT, 10, "bold")
        ).pack(side="right")
        self.grab_set()

    def _approve(self):
        if not self.acknowledge.get():
            messagebox.showwarning("Review required", "You must confirm your review.", parent=self)
            return
        if not self.note.get().strip():
            messagebox.showwarning("Note required", "Add an approval note.", parent=self)
            return
        if self.required_phrase and self.confirmation.get().strip() != self.required_phrase:
            messagebox.showwarning("Confirmation required", "Type the exact phrase shown.", parent=self)
            return
        try:
            self.on_approve(self.note.get().strip())
        except Exception as error:
            messagebox.showerror("Approval failed", str(error), parent=self)
            return
        self.grab_release()
        self.destroy()

