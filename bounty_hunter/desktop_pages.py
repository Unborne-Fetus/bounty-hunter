"""Point-and-click scanner, setup and supervised attempt screens."""
import json
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .desktop_core import (
    load_settings, report_rows, run_worker, save_settings, scan_args,
    workspace_valid,
)
from .history import History


class DesktopPages:
    def __init__(self, app):
        self.app = app
        self.settings = load_settings()
        self.running = False
        self.log_box = None
        self.last_rows = []
        self.lookup = {}
        self.attempt_lookup = {}
        self.action_lookup = {}
        self.scan_enabled = {}
        self.status = tk.StringVar(value="Idle")

    def _label(self, parent, text, size=10, color=None, bold=False, **kw):
        return self.app._label(
            parent, text, size, color or "#f1f5fa", bold, **kw
        )

    def _button(self, parent, text, callback, accent=False):
        return self.app._button(parent, text, callback, accent)

    def _info(self, parent, text):
        self._label(
            parent, text, 10, "#a8b7ca",
            wraplength=750, justify="left"
        ).pack(anchor="w", pady=(2, 12))

    def workspace(self):
        stored = self.settings.get("workspace", "")
        if stored and workspace_valid(stored):
            return str(Path(stored).resolve())
        db = self.app.database
        if db:
            root = Path(db).resolve().parent.parent
            if workspace_valid(root):
                return str(root)
        root = Path.cwd()
        return str(root) if workspace_valid(root) else None

    def setup(self, parent):
        self._label(parent, "One-time setup", 17, bold=True).pack(anchor="w")
        self._info(
            parent, "Choose your Bounty Hunter repository, configure discovery, and "
            "check AI workflow requirements. No tokens or passwords are saved here."
        )
        panel = tk.Frame(parent, bg="#1c293b", padx=18, pady=14)
        panel.pack(fill="x")
        self.root_var = tk.StringVar(value=self.settings.get("workspace") or self.workspace() or "")
        self.country_var = tk.StringVar(value=self.settings.get("country", ""))
        self.org_var = tk.StringVar(value=self.settings.get("algora_orgs", ""))

        def field(label, var, browse=False):
            self._label(panel, label, bold=True).pack(anchor="w", pady=(7, 4))
            row = tk.Frame(panel, bg="#1c293b")
            row.pack(fill="x")
            entry = tk.Entry(
                row, textvariable=var, bg="#26364b", fg="white",
                insertbackground="white", relief="flat", font=("Segoe UI", 10)
            )
            entry.pack(side="left", fill="x", expand=True, ipady=7)
            if browse:
                def select():
                    folder = filedialog.askdirectory(title="Select cloned Bounty Hunter repository")
                    if folder:
                        var.set(folder)
                self._button(row, "Browse", select).pack(side="left", padx=(8, 0))
        field("Repository folder (contains bounty_hunter)", self.root_var, True)
        field("Payout country (2 letters, such as US; eligibility must be verified)", self.country_var)
        field("Algora organization handles (comma-separated, optional)", self.org_var)
        self._label(panel, "Sources to scan", bold=True).pack(anchor="w", pady=(12, 4))
        enabled = tk.Frame(panel, bg="#1c293b")
        enabled.pack(anchor="w")
        disabled = set(self.settings.get("disabled_sources") or [])
        self.scan_enabled = {}
        for key in ("bountypay", "boss", "opire", "taskbounty"):
            var = tk.BooleanVar(value=key not in disabled)
            tk.Checkbutton(
                enabled, text=key.title(), variable=var,
                bg="#1c293b", fg="white", selectcolor="#26364b",
                activebackground="#1c293b", activeforeground="white"
            ).pack(side="left", padx=(0, 13))
            self.scan_enabled[key] = var
        self._button(panel, "Save setup", self.save_setup, True).pack(anchor="w", pady=(18, 3))

        self._label(parent, "Environment checks", 13, bold=True).pack(anchor="w", pady=(18, 5))
        import shutil
        workspace = self.workspace()
        checks = [
            ("Repository", "Found" if workspace else "Choose repository folder"),
            ("Python / packaged app", "Ready"),
            ("Docker or Podman", "Available" if shutil.which("docker") or shutil.which("podman") else "Missing (required for safe build/test execution)"),
            ("Codex CLI", "Available" if shutil.which("codex") else "Missing (required for AI patch generation)"),
            ("GitHub CLI", "Available" if shutil.which("gh") else "Missing (required for supervised PR publication)"),
        ]
        for name, value in checks:
            self._label(parent, name + ": " + value, 10, "#a8b7ca").pack(anchor="w", pady=2)
        self._info(
            parent, "GitHub credentials are managed outside this app by your own GitHub CLI "
            "sign-in or GITHUB_TOKEN environment variable. The app never collects secrets. "
            "Marketplace registration and payout eligibility remain provider-specific."
        )

    def save_setup(self):
        workspace = self.root_var.get().strip()
        if not workspace_valid(workspace):
            messagebox.showerror("Repository required", "Choose a folder containing bounty_hunter/cli.py")
            return
        disabled = [k for k, v in self.scan_enabled.items() if not v.get()]
        try:
            scan_args(self.country_var.get(), self.org_var.get(), disabled)
            self.settings = {
                "workspace": str(Path(workspace).resolve()),
                "country": self.country_var.get().strip().upper(),
                "algora_orgs": self.org_var.get().strip(),
                "disabled_sources": disabled,
            }
            save_settings(self.settings)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Invalid setup", str(exc))
            return
        self.app.database = str(Path(workspace).resolve() / "data" / "bounty_hunter.db")
        self.app.refresh()
        messagebox.showinfo("Setup saved", "Settings saved. Open Bounty Scanner and click Scan now.")

    def _log(self, text):
        if self.log_box and self.log_box.winfo_exists():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", str(text).rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def _run(self, name, args, on_done=None):
        if self.running:
            messagebox.showwarning("Already running", "An operation is already in progress.")
            return
        workspace = self.workspace()
        if not workspace:
            messagebox.showerror("Setup needed", "Open Setup and select your cloned repository.")
            return
        self.running = True
        self.status.set("Running " + name + "…")
        self._log("Starting " + name + " " + " ".join(args[:8]))
        def job():
            try:
                result = run_worker(name, args, workspace, timeout=1800)
                output = (result.stdout or "") + "\n" + (result.stderr or "")
                code = result.returncode
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                output, code = str(exc), 1
            def finish():
                self.running = False
                self.status.set("Complete" if code == 0 else "Failed")
                self._log(output[-16000:])
                if code != 0:
                    messagebox.showerror("Operation failed", (output or "No output returned")[-1600:])
                elif on_done is not None:
                    on_done()
            try:
                self.app.after(0, finish)
            except RuntimeError:
                pass
        threading.Thread(target=job, daemon=True).start()

    def scan(self, parent):
        self._label(parent, "Bounty scanner", 17, bold=True).pack(anchor="w")
        self._info(
            parent, "Find offers from supported marketplaces and review eligibility. "
            "Scans never claim or submit a bounty. Payment and repository safeguards remain enabled."
        )
        bar = tk.Frame(parent, bg="#101827")
        bar.pack(fill="x", pady=(0, 9))
        self._button(bar, "Scan now", self.scan_now, True).pack(side="left", padx=(0, 8))
        self._button(bar, "Reload results", self._load_results).pack(side="left")
        tk.Label(bar, textvariable=self.status, bg="#101827", fg="#49d5b5").pack(side="left", padx=16)
        self._label(parent, "Discovered opportunities", 11, bold=True).pack(anchor="w", pady=(7, 7))
        cols = ("reward", "title", "source", "decision")
        table = ttk.Treeview(parent, columns=cols, show="headings", style="Bounty.Treeview", height=12)
        for col, label, width in [
            ("reward", "USD", 85), ("title", "Title", 330),
            ("source", "Provider", 95), ("decision", "Decision", 120),
        ]:
            table.heading(col, text=label)
            table.column(col, width=width, stretch=(col == "title"), minwidth=60)
        table.pack(fill="both", expand=True)
        self.scan_table = table
        self.scan_table.bind("<<TreeviewSelect>>", lambda _: self._select_bounty())
        self.details = tk.StringVar(value="Select a bounty to inspect rejection reasons.")
        self._label(parent, "", 9, "#a8b7ca", textvariable=self.details,
                    wraplength=790, justify="left").pack(fill="x", pady=(10, 5))
        btns = tk.Frame(parent, bg="#101827")
        btns.pack(fill="x", pady=(4, 10))
        self._button(btns, "Queue selected eligible bounty", self.queue_selected, True).pack(side="left", padx=(0, 8))
        self._button(btns, "Open GitHub issue", self.open_issue).pack(side="left")
        self._load_results()

    def scan_now(self):
        args = scan_args(
            self.settings.get("country", ""),
            self.settings.get("algora_orgs", ""),
            self.settings.get("disabled_sources", []),
        )
        self._run("scan", args, self._load_results)

    def _load_results(self):
        workspace = self.workspace()
        if not workspace:
            return
        try:
            self.last_rows, error = report_rows(workspace)
        except (ValueError, OSError) as exc:
            self.last_rows, error = [], str(exc)
        self.lookup = {}
        if not hasattr(self, "scan_table") or not self.scan_table.winfo_exists():
            return
        for item in self.scan_table.get_children():
            self.scan_table.delete(item)
        for index, record in enumerate(self.last_rows):
            opp = record.get("opportunity", {})
            score = record.get("score", {})
            reward = opp.get("bounty_usd") or 0
            item_id = str(index)
            self.scan_table.insert(
                "", "end", iid=item_id,
                values=(f"${reward:,.2f}", opp.get("title", ""),
                        opp.get("source", ""), "Eligible" if score.get("eligible") else "Rejected"),
            )
            self.lookup[item_id] = record
        if error:
            self.details.set(error)
        else:
            count = sum(bool(x.get("score", {}).get("eligible")) for x in self.last_rows)
            self.details.set(f"{len(self.last_rows)} opportunities discovered; {count} eligible. Select one for details.")

    def _selected_record(self):
        chosen = self.scan_table.selection() if hasattr(self, "scan_table") else []
        return self.lookup.get(chosen[0]) if chosen else None

    def _select_bounty(self):
        record = self._selected_record()
        if not record:
            return
        score = record.get("score", {})
        blockers = score.get("rejection_reasons") or score.get("reasons") or []
        if not blockers and not score.get("eligible"):
            blockers = ["See reports/opportunities.md for full rejection details"]
        self.details.set(
            ("Eligible" if score.get("eligible") else "Not eligible") + " — " +
            ("; ".join(map(str, blockers[:3])) if isinstance(blockers, list) else str(blockers))
        )

    def queue_selected(self):
        record = self._selected_record()
        if not record:
            messagebox.showinfo("Choose a bounty", "Select an opportunity first.")
            return
        if not record.get("score", {}).get("eligible"):
            messagebox.showwarning("Not eligible", "This bounty failed safety or payment checks.")
            return
        opp = record["opportunity"]
        if not messagebox.askyesno(
            "Queue opportunity", "Queue this eligible bounty for review?\n\n" + opp.get("title", "")
        ):
            return
        self._run("workflow", ["queue", "--source", str(opp["source"]),
                               "--source-id", str(opp["source_id"])],
                  lambda: self.app.refresh())

    def open_issue(self):
        import webbrowser
        from urllib.parse import urlparse
        record = self._selected_record()
        url = (record or {}).get("opportunity", {}).get("issue_url")
        if url and urlparse(url).hostname == "github.com" and url.startswith("https://"):
            webbrowser.open(url)
        else:
            messagebox.showinfo("No issue", "No verified GitHub issue URL is attached.")

    def _attempts(self):
        workspace = self.workspace()
        if not workspace:
            return []
        path = Path(workspace) / "data" / "bounty_hunter.db"
        if not path.is_file():
            return []
        history = History(str(path))
        try:
            rows = history.connection.execute(
                "SELECT id, source, title, status FROM attempts ORDER BY id DESC LIMIT 150"
            ).fetchall()
            return [dict(x) for x in rows]
        finally:
            history.close()

    def workflow(self, parent):
        self._label(parent, "AI workflow", 17, bold=True).pack(anchor="w")
        self._info(
            parent, "All coding tasks use the existing sandboxed, supervised engine. "
            "You approve the attempt and reviewed code separately before public submission."
        )
        attempts = self._attempts()
        if not attempts:
            self._info(parent, "No queued attempts. Scan and queue an eligible bounty first.")
            return
        panel = tk.Frame(parent, bg="#1c293b", padx=17, pady=15)
        panel.pack(fill="x")
        values = [f'#{x["id"]} · {x["source"]} · {x["title"][:48]} [{x["status"]}]' for x in attempts]
        self.attempt_lookup = {label: attempt for label, attempt in zip(values, attempts)}
        self.attempt_choice = tk.StringVar(value=values[0])
        self._label(panel, "Select attempt", bold=True).pack(anchor="w", pady=(0, 5))
        combo = ttk.Combobox(
            panel, textvariable=self.attempt_choice,
            state="readonly", values=values, width=78
        )
        combo.pack(anchor="w")
        combo.bind("<<ComboboxSelected>>", lambda _: self._load_attempt_details())
        row = tk.Frame(panel, bg="#1c293b")
        row.pack(anchor="w", pady=(18, 8))
        actions = [
            ("Review attempt", self.review_attempt),
            ("Approve attempt", self.approve_attempt),
            ("Run preflight", lambda: self._attempt_command("preflight", [])),
            ("Generate patch", lambda: self._attempt_command("attempt", ["patch"])),
            ("Verify patch", lambda: self._attempt_command("attempt", ["verify"])),
        ]
        for idx, (label, callback) in enumerate(actions):
            self._button(row, label, callback, idx == 1).pack(side="left", padx=(0, 7))
        row2 = tk.Frame(panel, bg="#1c293b")
        row2.pack(anchor="w", pady=(4, 12))
        actions2 = [
            ("Review verification", self.show_bundle),
            ("Approve reviewed patch", self.approve_bundle),
            ("Prepare submission", lambda: self._attempt_command("submission", ["prepare"])),
            ("Publish approved PR", self.publish_pr),
        ]
        for idx, (label, callback) in enumerate(actions2):
            self._button(row2, label, callback, idx == 1).pack(side="left", padx=(0, 7))
        self.attempt_details = tk.Text(
            parent, height=12, bg="#1c293b", fg="#a8b7ca",
            insertbackground="white", wrap="word", relief="flat"
        )
        self.attempt_details.pack(fill="both", expand=True, pady=(15, 5))
        self._load_attempt_details()
        self._info(parent, "Preflight/patch/verification require supported dependencies. "
                   "No 'publish' operation happens without a separate confirmation tied "
                   "to reviewed hashes.")

    def _chosen_attempt(self):
        return self.attempt_lookup.get(self.attempt_choice.get()) if hasattr(self, "attempt_choice") else None

    def _history(self):
        return History(str(Path(self.workspace()) / "data" / "bounty_hunter.db"))

    def _load_attempt_details(self):
        item = self._chosen_attempt()
        if not item or not hasattr(self, "attempt_details") or not self.attempt_details.winfo_exists():
            return
        history = self._history()
        try:
            state = history.get_attempt(item["id"])
            record = history.get_attempt_record(item["id"])
            content = json.dumps({
                "attempt": {k: v for k, v in state.items() if k not in {"opportunity_json", "score_json"}},
                "opportunity": record.get("opportunity"),
                "score": record.get("score"),
            }, indent=2)
        except (ValueError, OSError) as exc:
            content = str(exc)
        finally:
            history.close()
        self.attempt_details.delete("1.0", "end")
        self.attempt_details.insert("1.0", content[:15000])

    def _attempt_command(self, module, beginning):
        item = self._chosen_attempt()
        if not item:
            return
        args = beginning + [str(item["id"])] if module != "preflight" else [str(item["id"])]
        self._run(module, args, lambda: self.app.navigate("ai"))

    def review_attempt(self):
        item = self._chosen_attempt()
        if item:
            self._run("workflow", ["review", str(item["id"])], self._load_attempt_details)

    def approve_attempt(self):
        item = self._chosen_attempt()
        if not item:
            return
        history = self._history()
        try:
            digest = history.attempt_record_sha256(item["id"])
        except ValueError as exc:
            messagebox.showerror("Unable to review", str(exc))
            return
        finally:
            history.close()
        note = self._ask_text(
            "Approve attempt", "Review the full record before approval.\n"
            "Enter a reason for approving this particular attempt:\n"
            "SHA256: " + digest
        )
        if not note:
            return
        self._run("workflow", ["approve", str(item["id"]),
                               "--record-sha256", digest, "--note", note],
                  lambda: self.app.navigate("ai"))

    def show_bundle(self):
        item = self._chosen_attempt()
        if not item:
            return
        history = self._history()
        try:
            bundle = history.latest_artifact(item["id"], "review_bundle")
            if not bundle:
                messagebox.showinfo("No bundle", "Verify the patch first.")
                return
            location = bundle.get("path")
            messagebox.showinfo("Review bundle", "Review ALL files in this folder before approving:\n\n" + str(location) +
                                "\n\nApproved digest must match: " + str(bundle.get("sha256")))
        finally:
            history.close()

    def approve_bundle(self):
        item = self._chosen_attempt()
        if not item:
            return
        history = self._history()
        try:
            bundle = history.latest_artifact(item["id"], "review_bundle")
            if not bundle:
                messagebox.showerror("No bundle", "Run verification first.")
                return
            digest = bundle["sha256"]
            location = bundle["path"]
        finally:
            history.close()
        note = self._ask_text(
            "Second approval", "Inspect the entire review bundle at:\n" + location +
            "\n\nEnter your approval note (must reflect actual review):"
        )
        if note:
            self._run("workflow", ["approve-submission", str(item["id"]),
                                   "--bundle-sha256", digest, "--note", note],
                      lambda: self.app.navigate("ai"))

    def publish_pr(self):
        item = self._chosen_attempt()
        if not item:
            return
        messagebox.showinfo(
            "Explicit publication required",
            "Bounty Hunter will not publish a pull request automatically. "
            "Use the authenticated GitHub CLI and the existing "
            "bounty_hunter.submission publish command after checking the "
            "approved bundle and prepared package hashes. "
            "Public publication is intentionally not a one-click action."
        )

    def _ask_text(self, title, prompt):
        result = {"text": None}
        window = tk.Toplevel(self.app)
        window.title(title)
        window.configure(bg="#1c293b")
        window.geometry("610x300")
        window.transient(self.app)
        window.grab_set()
        self._label(window, prompt, 10, wraplength=560, justify="left").pack(
            anchor="w", padx=20, pady=18
        )
        entry = tk.Text(window, height=5, width=70, wrap="word", bg="#26364b",
                        fg="white", insertbackground="white")
        entry.pack(padx=20, fill="x")
        def submit():
            text = entry.get("1.0", "end").strip()
            if text:
                result["text"] = text
                window.destroy()
        self._button(window, "Confirm", submit, True).pack(pady=14)
        window.wait_window()
        return result["text"]
