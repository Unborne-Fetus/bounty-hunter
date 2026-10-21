"""Native Windows-friendly Tkinter dashboard for Bounty Hunter.

Launch from the repo with: python -m bounty_hunter.dashboard
The optional Windows .exe is built by GitHub Actions using PyInstaller.
"""
import json
import os
import sys
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .earnings import PROVIDERS, default_database, load_dashboard, provider_info
from .history import History
from .desktop_pages import DesktopPages
from .desktop_core import load_settings

BG = "#101827"
PANEL = "#1c293b"
RAISED = "#26364b"
TEXT = "#f1f5fa"
MUTED = "#a8b7ca"
ACCENT = "#49d5b5"
WARNING = "#efbb73"
FONT = "Segoe UI"


def _money(number):
    return "${:,.2f}".format(number)


def _rate(number):
    return "—" if number is None else _money(number) + "/hr"


def _settings_path():
    home = Path(os.environ.get("APPDATA") or Path.home())
    return home / "BountyHunter" / "dashboard.json"


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bounty Hunter — Windows")
        self.configure(bg=BG)
        self.geometry("1120x740")
        self.minsize(850, 610)
        self.option_add("*Font", (FONT, 10))
        self._setup_styles()

        settings = {}
        try:
            settings = json.loads(_settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if not isinstance(settings, dict):
            settings = {}
        exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
        self.database = default_database(
            [Path.cwd(), exe_dir, exe_dir.parent],
            settings.get("database"),
        )
        self.data = None
        self.page = "scan" if not load_settings().get("workspace") else "overview"
        self.desktop = DesktopPages(self)
        self.provider = tk.StringVar(value="algora")
        self.message = tk.StringVar(value="No history database selected")
        self._make_shell()
        self.refresh()
        self.after(60000, self._periodic_refresh)

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Bounty.Treeview", background=PANEL, foreground=TEXT, rowheight=29,
            fieldbackground=PANEL, borderwidth=0, font=(FONT, 10)
        )
        style.configure(
            "Bounty.Treeview.Heading", background=RAISED, foreground=TEXT,
            font=(FONT, 10, "bold"), padding=8
        )
        style.map("Bounty.Treeview", background=[("selected", "#36546d")])
        style.configure("Bounty.TCombobox", padding=5)
        style.map("Bounty.TCombobox", fieldbackground=[("readonly", TEXT)])
        style.configure("Bounty.TNotebook", background=BG)

    def _label(self, parent, text, size=10, color=TEXT, bold=False, **kwargs):
        return tk.Label(
            parent, text=text, bg=parent.cget("bg"), fg=color,
            font=(FONT, size, "bold" if bold else "normal"), **kwargs
        )

    def _button(self, parent, text, command, accent=False):
        return tk.Button(
            parent, text=text, command=command, relief="flat", bd=0, cursor="hand2",
            padx=16, pady=9, bg=ACCENT if accent else RAISED,
            fg=BG if accent else TEXT, activebackground="#6be5c9" if accent else "#35506b",
            font=(FONT, 10, "bold")
        )

    def _make_shell(self):
        side = tk.Frame(self, bg="#141f30", width=210)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self._label(side, "BOUNTY", 20, ACCENT, True).pack(anchor="w", padx=22, pady=(30, 0))
        self._label(side, "HUNTER", 20, TEXT, True).pack(anchor="w", padx=22, pady=(0, 36))
        self.nav = {}
        for page, name in [("scan", "Bounty Scanner"), ("setup", "Setup"),
                           ("ai", "AI Workflow"), ("overview", "Overview"),
                           ("payouts", "Payouts / Withdraw"),
                           ("activity", "Record Activity")]:
            button = self._button(side, name, lambda p=page: self.navigate(p))
            button.pack(fill="x", padx=12, pady=5)
            self.nav[page] = button
        self._label(side, "LOCAL-ONLY ACCOUNTING", 9, MUTED).pack(
            anchor="w", side="bottom", padx=18, pady=22
        )

        body = tk.Frame(self, bg=BG)
        body.pack(side="left", fill="both", expand=True)
        top = tk.Frame(body, bg=BG)
        top.pack(fill="x", padx=25, pady=(25, 12))
        self.heading = self._label(top, "Earnings overview", 20, TEXT, True)
        self.heading.pack(side="left")
        self._button(top, "Refresh", self.refresh, accent=True).pack(side="right", padx=(8, 0))
        self._button(top, "Choose database", self.choose_database).pack(side="right")

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(fill="both", expand=True, padx=25, pady=(5, 8))
        bottom = tk.Frame(body, bg=BG)
        bottom.pack(fill="x", padx=25, pady=(6, 17))
        self._label(
            bottom,
            "Recorded payouts are manual entries, not independently verified transfers.",
            9, MUTED
        ).pack(anchor="w")
        tk.Label(
            bottom, textvariable=self.message, bg=BG, fg=MUTED,
            font=(FONT, 9), anchor="w"
        ).pack(fill="x", pady=(4, 0))

    def choose_database(self):
        name = filedialog.askopenfilename(
            title="Choose Bounty Hunter history database",
            filetypes=[("SQLite databases", "*.db"), ("All files", "*.*")]
        )
        if not name:
            return
        previous = self.database
        self.database = name
        try:
            self.data = load_dashboard(name)
        except (OSError, ValueError, Exception) as error:
            self.database = previous
            messagebox.showerror("Invalid database", str(error))
            return
        path = _settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"database": str(Path(name).resolve())}), encoding="utf-8")
        except OSError as error:
            messagebox.showwarning("Settings", f"Database opened but preference was not saved: {error}")
        self.refresh()

    def refresh(self):
        if self.database:
            try:
                self.data = load_dashboard(self.database)
                self.message.set("History: " + str(self.database))
            except Exception as error:
                self.data = None
                self.message.set("Unable to read history: " + str(error))
        else:
            self.data = None
            self.message.set("No history found. Run a scan in your repo or choose an existing database.")
        self.navigate(self.page)

    def _periodic_refresh(self):
        if self.winfo_exists():
            self.refresh()
            self.after(60000, self._periodic_refresh)

    def navigate(self, page):
        self.page = page
        self.heading.config(text={
            "overview": "Earnings overview",
            "payouts": "Payouts & withdrawals",
            "activity": "Record payout or work time",
            "scan": "Find coding bounties",
            "setup": "First-run setup",
            "ai": "Supervised AI workflow",
        }[page])
        for name, button in self.nav.items():
            button.config(bg=RAISED if name == page else "#141f30", fg=ACCENT if name == page else TEXT)
        for child in self.content.winfo_children():
            child.destroy()
        if page == "overview":
            self._overview()
        elif page == "payouts":
            self._payouts()
        elif page == "activity":
            self._activity()
        elif page == "scan":
            self.desktop.scan(self.content)
        elif page == "setup":
            self.desktop.setup(self.content)
        elif page == "ai":
            self.desktop.workflow(self.content)

    def _card(self, parent, title, value, detail, color=TEXT):
        card = tk.Frame(parent, bg=PANEL, padx=18, pady=14)
        self._label(card, title.upper(), 9, MUTED, True).pack(anchor="w")
        self._label(card, value, 22, color, True).pack(anchor="w", pady=(8, 3))
        self._label(card, detail, 9, MUTED).pack(anchor="w")
        return card

    def _overview(self):
        d = self.data
        if not d:
            self._label(
                self.content, "No earnings data yet. Select data/bounty_hunter.db from your repository.",
                12, MUTED
            ).pack(anchor="w", pady=20)
            self._button(self.content, "Choose database", self.choose_database, True).pack(anchor="w")
            return
        metrics = tk.Frame(self.content, bg=BG)
        metrics.pack(fill="x")
        cells = [
            ("Recorded payouts", _money(d["recorded_usd"]), "Not bank-verified", ACCENT),
            ("Avg. gross / hour", _rate(d["gross_per_hour"]), "Payouts ÷ logged hours", TEXT),
            ("Avg. net / hour", _rate(d["cash_net_per_hour"]), "After direct costs", TEXT),
        ]
        for i, (title, value, detail, color) in enumerate(cells):
            card = self._card(metrics, title, value, detail, color)
            card.grid(row=0, column=i, padx=(0, 12) if i < 2 else 0, sticky="nsew")
            metrics.columnconfigure(i, weight=1, uniform="metrics")

        secondary = tk.Frame(self.content, bg=BG)
        secondary.pack(fill="x", pady=(12, 22))
        items = [
            ("Direct costs", _money(d["direct_cost_usd"]), "Agent + compute"),
            ("Logged work", f'{d["logged_hours"]:,.2f} hours', "Only recorded human time"),
            ("Merged, unconfirmed", _money(d["unconfirmed_usd"]), "Not counted as earnings"),
        ]
        for i, (title, value, detail) in enumerate(items):
            card = self._card(secondary, title, value, detail, WARNING if i == 2 else TEXT)
            card.grid(row=0, column=i, padx=(0, 12) if i < 2 else 0, sticky="nsew")
            secondary.columnconfigure(i, weight=1, uniform="metrics")
        self._label(
            self.content, "RECENT BOUNTY ATTEMPTS", 11, TEXT, True
        ).pack(anchor="w", pady=(0, 8))
        self._attempt_table(self.content, d["attempts"])
        self._label(
            self.content,
            "Hourly rate is undefined until you log work hours. Payouts are recorded manually "
            "after receipt; offered bounty amounts never count as earned.",
            9, MUTED, wraplength=760, justify="left"
        ).pack(anchor="w", pady=(10, 0))

    def _attempt_table(self, parent, rows):
        columns = ("source", "title", "status", "payout", "hours")
        view = ttk.Treeview(parent, columns=columns, show="headings", style="Bounty.Treeview", height=11)
        headings = {"source": "Provider", "title": "Bounty", "status": "Status",
                    "payout": "Recorded paid", "hours": "Hours"}
        widths = {"source": 95, "title": 275, "status": 100, "payout": 115, "hours": 80}
        for column in columns:
            view.heading(column, text=headings[column])
            view.column(column, width=widths[column], minwidth=65, stretch=column == "title")
        for item in rows:
            view.insert("", "end", values=(
                item["provider_name"], item["title"], item["status"],
                _money(item["recorded_usd"]), f'{item["hours"]:.2f}'
            ))
        view.pack(fill="both", expand=True)

    def _payouts(self):
        self._label(
            self.content, "Withdraw through the marketplace that holds your reward.",
            13, TEXT, True
        ).pack(anchor="w", pady=(5, 6))
        self._label(
            self.content,
            "Bounty Hunter does not hold funds or connect to your bank or wallet. "
            "A merged PR does not necessarily mean funds are payable. Check that provider's "
            "actual payout status, identity/age requirements, fees and supported countries.",
            10, MUTED, wraplength=790, justify="left"
        ).pack(anchor="w", pady=(0, 20))

        options = list(PROVIDERS.keys())
        if self.data:
            options = sorted(set(options) | set(self.data["providers"]))
        panel = tk.Frame(self.content, bg=PANEL, padx=20, pady=20)
        panel.pack(fill="x")
        self._label(panel, "MARKETPLACE", 10, MUTED, True).pack(anchor="w")
        selector = ttk.Combobox(panel, textvariable=self.provider, values=options,
                                state="readonly", width=28)
        selector.pack(anchor="w", pady=(8, 18))
        self.provider_title = self._label(panel, "", 15, TEXT, True)
        self.provider_title.pack(anchor="w")
        self.provider_stats = self._label(panel, "", 11, ACCENT)
        self.provider_stats.pack(anchor="w", pady=(5, 10))
        self.provider_help = self._label(
            panel, "", 10, MUTED, wraplength=720, justify="left"
        )
        self.provider_help.pack(anchor="w", pady=(0, 14))
        self.link_button = self._button(panel, "Open provider to review payout", self.open_provider, True)
        self.link_button.pack(anchor="w")
        selector.bind("<<ComboboxSelected>>", lambda event: self._update_provider())
        self._update_provider()

        self._label(
            self.content,
            "There is no universal Withdraw API. This button opens the provider website; "
            "it does not initiate or confirm a payment. Never enter wallet secrets into Bounty Hunter.",
            10, MUTED, wraplength=790, justify="left"
        ).pack(anchor="w", pady=20)

    def _update_provider(self):
        source = self.provider.get().lower()
        name, url = provider_info(source)
        p = (self.data or {}).get("providers", {}).get(source, {})
        self.provider_title.config(text=name)
        self.provider_stats.config(
            text=f'Recorded payouts: {_money(p.get("recorded", 0))}   •   Attempt count: {p.get("count", 0)}'
        )
        details = {
            "algora": "Review the Algora account and the payout method configured there.",
            "bountypay": "Review the bounty's chain, settlement status and your wallet directly in BountyPay. On-chain payout is not a bank withdrawal.",
            "boss": "Review Boss claim status and supported payout options directly with Boss.",
            "opire": "Review reward approval and payout details in your Opire account.",
            "taskbounty": "Review acceptance and payout instructions for your task in TaskBounty.",
        }
        self.provider_help.config(
            text=details.get(source, "Check the provider's official payout instructions.") +
            (f"\nWebsite: {url}" if url else "\nNo trusted provider link configured.")
        )
        self.link_button.config(state="normal" if url else "disabled")

    def open_provider(self):
        url = provider_info(self.provider.get())[1]
        if url:
            webbrowser.open(url)

    def _activity(self):
        data = self.data
        if not data or not data["attempts"]:
            self._label(
                self.content, "No recorded attempts. Queue a bounty in Bounty Hunter first.",
                12, MUTED
            ).pack(anchor="w", pady=20)
            return
        self._label(
            self.content, "Log actual payouts received or hours spent on an existing attempt.",
            12, TEXT, True
        ).pack(anchor="w", pady=(6, 12))
        self._label(
            self.content,
            "Entering a payout updates your local accounting only. It cannot trigger a transfer "
            "or confirm that a provider sent money.",
            10, MUTED, wraplength=780, justify="left"
        ).pack(anchor="w", pady=(0, 16))
        panel = tk.Frame(self.content, bg=PANEL, padx=20, pady=20)
        panel.pack(fill="x")

        self.attempt_lookup = {}
        choices = []
        for item in data["attempts"]:
            label = f'#{item["id"]} — {item["provider_name"]}: {item["title"][:65]} ({item["status"]})'
            choices.append(label)
            self.attempt_lookup[label] = item

        self.attempt_select = tk.StringVar(value=choices[0])
        self._label(panel, "ATTEMPT", 10, MUTED, True).pack(anchor="w")
        ttk.Combobox(
            panel, textvariable=self.attempt_select, values=choices,
            state="readonly", width=78
        ).pack(anchor="w", pady=(6, 14))

        self.event_type = tk.StringVar(value="human_hours")
        self._label(panel, "WHAT ARE YOU RECORDING?", 10, MUTED, True).pack(anchor="w")
        for value, title in [
            ("human_hours", "Hours worked"),
            ("payout_usd", "Payout actually received (USD)"),
        ]:
            tk.Radiobutton(
                panel, text=title, variable=self.event_type, value=value,
                bg=PANEL, fg=TEXT, activebackground=PANEL, selectcolor=RAISED,
                activeforeground=TEXT, font=(FONT, 10)
            ).pack(anchor="w")

        self._label(panel, "AMOUNT (POSITIVE NUMBER)", 10, MUTED, True).pack(
            anchor="w", pady=(14, 3)
        )
        self.amount_entry = tk.Entry(
            panel, width=22, bg=RAISED, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=(FONT, 12)
        )
        self.amount_entry.pack(anchor="w", ipady=7)
        self._label(panel, "AUDIT NOTE / RECEIPT REFERENCE", 10, MUTED, True).pack(
            anchor="w", pady=(14, 3)
        )
        self.note_entry = tk.Entry(
            panel, width=70, bg=RAISED, fg=TEXT, insertbackground=TEXT,
            relief="flat", font=(FONT, 11)
        )
        self.note_entry.pack(anchor="w", ipady=7)
        self._button(panel, "Save local ledger entry", self.record_event, True).pack(
            anchor="w", pady=(20, 0)
        )

    def record_event(self):
        import math
        item = self.attempt_lookup.get(self.attempt_select.get())
        if not item or not self.database:
            return
        try:
            amount = float(self.amount_entry.get())
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError("Amount must be a finite positive number")
            note = self.note_entry.get().strip()
            if not note:
                raise ValueError("An audit note is required")
        except ValueError as error:
            messagebox.showerror("Invalid entry", str(error))
            return
        kind = self.event_type.get()
        if kind == "payout_usd":
            if item["status"] not in {"merged", "paid"}:
                messagebox.showerror(
                    "Payout cannot be recorded",
                    "The attempt must be merged or paid first."
                )
                return
            if not messagebox.askyesno(
                "Confirm payout receipt",
                f"Have you actually received {_money(amount)} for this bounty?\n\n"
                "This will only update the local ledger; it will not withdraw funds."
            ):
                return
        try:
            history = History(self.database)
            try:
                history.record_event(item["id"], kind, amount, note)
            finally:
                history.close()
        except Exception as error:
            messagebox.showerror("Could not save entry", str(error))
            return
        self.refresh()
        messagebox.showinfo("Saved", "Local accounting entry recorded.")


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--bounty-worker":
        name = sys.argv[2]
        from . import cli, workflow, preflight, attempt, submission
        modules = {"scan": cli, "workflow": workflow, "preflight": preflight,
                   "attempt": attempt, "submission": submission}
        if name not in modules:
            raise SystemExit("Unsupported worker")
        sys.argv = [name] + sys.argv[3:]
        raise SystemExit(modules[name].main())
    app = Dashboard()
    app.mainloop()


if __name__ == "__main__":
    main()
