"""All-in-one desktop Bounty Hunter. No shell commands required for normal use.

The full application uses the same scoring, state transitions and submission
guards as the CLI. Bounty scanning and long operations run in worker threads.
"""
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .dashboard import Dashboard, BG, PANEL, RAISED, TEXT, MUTED, ACCENT, WARNING, FONT, _money
from .desktop_dialogs import ReviewDialog, SetupDialog
from .desktop_settings import load_settings, save_settings, settings_path, workspace_paths
from .desktop_service import read_report, run_algora_audit, run_scan
from .desktop_workflow import (
    queue_opportunity, review_attempt, approve_attempt, approve_review_bundle,
    perform_step, publish_approved, bundle_preview, package_preview,
)
from .earnings import load_dashboard, provider_info
from .history import History


class Workbench(Dashboard):
    def __init__(self):
        self.settings = load_settings()
        self.scan_report = {"opportunities": [], "source_health": []}
        self._jobs = queue.Queue()
        self._busy = False
        self._next_auto_scan = time.monotonic() + 20
        self._selected_row = None
        self._selected_attempt_id = None
        super().__init__()
        self.title("Bounty Hunter — Desktop")
        self.database = str(workspace_paths(self.settings)["db"])
        self.refresh()
        self.navigate("bounties")
        self.after(200, self._poll_jobs)
        self.after(200, self._startup)
        self.after(60000, self._auto_tick)

    def _make_shell(self):
        super()._make_shell()
        side = self.winfo_children()[0]
        for page, title in [
            ("bounties", "Find bounties"),
            ("work", "Coding workspace"),
            ("settings", "Settings / updates"),
        ]:
            self.nav[page] = self._button(side, title, lambda p=page: self.navigate(p))
        for button in self.nav.values():
            button.pack_forget()
        for name in ("bounties", "work", "overview", "payouts", "activity", "settings"):
            self.nav[name].pack(fill="x", padx=12, pady=5)

    def navigate(self, page):
        if page in {"overview", "payouts", "activity"}:
            return super().navigate(page)
        self.page = page
        self.heading.config(text={
            "bounties": "Discover bounties",
            "work": "Supervised coding workspace",
            "settings": "Settings and updates",
        }[page])
        for name, button in self.nav.items():
            button.configure(
                bg=RAISED if name == page else "#141f30",
                fg=ACCENT if name == page else TEXT
            )
        for child in self.content.winfo_children():
            child.destroy()
        if page == "bounties":
            self._bounty_page()
        elif page == "work":
            self._work_page()
        else:
            self._settings_page()

    def _startup(self):
        if not settings_path().exists():
            self.open_setup(first_run=True)
        else:
            self._load_report()

    def choose_database(self):
        name = filedialog.askopenfilename(
            parent=self, title="Select an existing Bounty Hunter history database",
            filetypes=[("SQLite history", "*.db"), ("All files", "*.*")]
        )
        if not name:
            return
        try:
            load_dashboard(name)
            # Existing repo stores its DB under workspace/data/bounty_hunter.db.
            file = Path(name).resolve()
            if file.name != "bounty_hunter.db" or file.parent.name != "data":
                raise ValueError("Choose the original data/bounty_hunter.db")
            updated = dict(self.settings)
            updated["workspace"] = str(file.parent.parent)
            self._save_settings(updated)
        except Exception as error:
            messagebox.showerror("Cannot open database", str(error), parent=self)

    def open_setup(self, first_run=False):
        SetupDialog(
            self, self.settings, first_run=first_run, on_save=self._save_settings
        )

    def _save_settings(self, values):
        values = save_settings(values)
        self.settings = values
        self.database = str(workspace_paths(values)["db"])
        self._next_auto_scan = time.monotonic() + values["scan_interval_minutes"] * 60
        self._load_report()
        self.refresh()
        self.message.set("Settings saved. Scan to update the selected workspace.")

    def _auto_tick(self):
        if self.winfo_exists():
            if (self.settings["auto_scan"] and not self._busy
                    and time.monotonic() >= self._next_auto_scan):
                self.start_scan()
            self.after(60000, self._auto_tick)

    def _load_report(self):
        try:
            self.scan_report = read_report(self.settings)
        except Exception as error:
            self.scan_report = {"opportunities": [], "source_health": []}
            self.message.set("Saved report unavailable: " + str(error))
        if self.page == "bounties":
            self.navigate("bounties")

    def _run_async(self, description, work, *, on_success=None):
        if self._busy:
            messagebox.showinfo("Busy", "A job is already running. Finish it before starting another.", parent=self)
            return
        self._busy = True
        self.message.set(description + " — running")
        def task():
            try:
                result = work()
                self._jobs.put(("ok", description, result, on_success))
            except Exception as error:
                self._jobs.put(("error", description, str(error), None))
        threading.Thread(target=task, daemon=True, name="BountyHunterTask").start()

    def _poll_jobs(self):
        try:
            while True:
                status, label, value, callback = self._jobs.get_nowait()
                self._busy = False
                if status == "error":
                    self.message.set(label + " failed: " + str(value)[:180])
                    messagebox.showerror(label + " failed", str(value), parent=self)
                else:
                    self.message.set(label + " completed")
                    try:
                        if callback is not None:
                            callback(value)
                        self.refresh()
                    except Exception as error:
                        messagebox.showerror("Display update failed", str(error), parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(200, self._poll_jobs)

    def start_scan(self):
        settings = dict(self.settings)
        self._next_auto_scan = time.monotonic() + settings["scan_interval_minutes"] * 60
        self._run_async("Scanning bounty marketplaces", lambda: run_scan(settings),
                        on_success=self._scan_complete)

    def _scan_complete(self, result):
        self._load_report()
        self.message.set(
            f'Scan #{result["scan_id"]}: {result["count"]} found, {result["eligible"]} eligible'
        )
        self.navigate("bounties")

    def start_audit(self):
        if not self.settings["sources"]["algora"]:
            messagebox.showinfo("Algora disabled", "Enable Algora in Settings first.", parent=self)
            return
        settings = dict(self.settings)
        self._run_async(
            "Discovering Algora boards", lambda: run_algora_audit(settings),
            on_success=lambda result: self._audit_complete(result)
        )

    def _audit_complete(self, result):
        messagebox.showinfo(
            "Algora discovery finished",
            f'Found {len(result.get("review_candidates", []))} verified board candidates. '
            "Run a scan to include their currently listed opportunities.",
            parent=self
        )
        self.navigate("bounties")

    def _bounty_page(self):
        top = tk.Frame(self.content, bg=BG)
        top.pack(fill="x", pady=(5, 12))
        self._button(top, "Scan now", self.start_scan, True).pack(side="left")
        self._button(top, "Find Algora boards", self.start_audit).pack(
            side="left", padx=(10, 0)
        )
        self._button(top, "Setup", self.open_setup).pack(side="right")

        report = self.scan_report
        all_rows = report.get("opportunities") or []
        eligible_count = sum(bool(r.get("score", {}).get("eligible")) for r in all_rows)
        generated = str(report.get("generated_at") or "Never")
        self._label(
            self.content,
            f"{len(all_rows)} discovered  •  {eligible_count} eligible  •  Last scan: {generated}",
            10, MUTED
        ).pack(anchor="w", pady=(2, 9))

        cols = ("source", "reward", "decision", "title", "value")
        self.scan_tree = ttk.Treeview(
            self.content, columns=cols, show="headings", height=11,
            style="Bounty.Treeview", selectmode="browse"
        )
        for key, text, size in [
            ("source", "Marketplace", 100), ("reward", "Reward", 90),
            ("decision", "Decision", 105), ("title", "Issue", 340),
            ("value", "Est. value", 105),
        ]:
            self.scan_tree.heading(key, text=text)
            self.scan_tree.column(key, width=size, stretch=key == "title")
        self.scan_tree.tag_configure("eligible", foreground=ACCENT)
        self.scan_tree.tag_configure("rejected", foreground=MUTED)
        for i, record in enumerate(all_rows):
            opp = record["opportunity"]
            score = record["score"]
            self.scan_tree.insert(
                "", "end", iid=str(i),
                values=(opp["source"], _money(opp["bounty_usd"]),
                        score["decision"], opp["title"][:130],
                        _money(score.get("expected_value_usd", 0))),
                tags=("eligible" if score["eligible"] else "rejected",)
            )
        self.scan_tree.pack(fill="both", expand=True)
        self.scan_tree.bind("<<TreeviewSelect>>", self._bounty_selected)

        actions = tk.Frame(self.content, bg=BG)
        actions.pack(fill="x", pady=(10, 10))
        self._button(actions, "Queue selected eligible bounty", self.queue_selected, True).pack(side="left")
        self._button(actions, "Open GitHub issue / provider", self.open_selected_issue).pack(
            side="left", padx=(10, 0)
        )
        self._button(actions, "Open coding workspace", lambda: self.navigate("work")).pack(
            side="right"
        )

        self.detail = tk.Text(
            self.content, bg=PANEL, fg=TEXT, font=(FONT, 10), relief="flat",
            wrap="word", height=7, padx=12, pady=9
        )
        self.detail.pack(fill="both", expand=True)
        self._set_detail("Select a bounty above to view eligibility, payout warnings and source evidence.\n"
                         "A rejected bounty cannot be approved for AI execution.")

        health = report.get("source_health") or []
        summary = "  |  ".join(
            f'{row.get("source")}: {row.get("status")}'
            for row in health
        ) or "No source health available"
        self._label(
            self.content, summary[:190], 9, WARNING if any(
                row.get("status") in {"error", "stale", "partial"} for row in health
            ) else MUTED
        ).pack(anchor="w", pady=(8, 0))

    def _set_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _selected_bounty(self):
        selection = self.scan_tree.selection()
        if not selection:
            raise ValueError("Select a bounty from the table first")
        index = int(selection[0])
        return self.scan_report["opportunities"][index]

    def _bounty_selected(self, event=None):
        try:
            record = self._selected_bounty()
        except (ValueError, IndexError, KeyError):
            return
        opportunity, score = record["opportunity"], record["score"]
        lines = [
            opportunity.get("title", ""),
            f'Provider: {opportunity["source"]}    Bounty: {_money(opportunity["bounty_usd"])}',
            f'Repository: {opportunity.get("repository") or "not verified"}',
            f'Decision: {score.get("decision")}    Confidence: {score.get("confidence", 0):.0%}',
            "",
            "Reasons:" if score.get("rejection_reasons") else "No blocking reasons recorded",
        ]
        lines.extend("• " + item for item in score.get("rejection_reasons") or [])
        if score.get("warnings"):
            lines.extend(["", "Warnings:"])
            lines.extend("• " + item for item in score["warnings"])
        self._set_detail("\n".join(lines))

    def open_selected_issue(self):
        try:
            opp = self._selected_bounty()["opportunity"]
        except ValueError as error:
            messagebox.showinfo("Select bounty", str(error), parent=self)
            return
        issue = opp.get("issue_url") or ""
        if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(issues|pull)/[0-9]+/?", issue):
            webbrowser.open(issue)
            return
        trusted = provider_info(opp["source"])[1]
        if trusted:
            webbrowser.open(trusted)
        else:
            messagebox.showinfo("No link", "No verified source URL is available.", parent=self)

    def queue_selected(self):
        try:
            record = self._selected_bounty()
            opp = record["opportunity"]
            if not record["score"]["eligible"]:
                raise ValueError(
                    "This bounty is rejected. See the exact reasons in the details panel. "
                    "Bounty Hunter will not attempt ineligible work."
                )
            if not messagebox.askyesno(
                "Queue bounty for review",
                f'{opp["title"]}\n\nReward offered: {_money(opp["bounty_usd"])}. '
                "This does not start AI work or guarantee payment. Queue it?",
                parent=self
            ):
                return
            attempt_id = queue_opportunity(self.settings, opp["source"], str(opp["source_id"]))
            self.refresh()
            self._selected_attempt_id = attempt_id
            self.navigate("work")
        except Exception as error:
            messagebox.showerror("Could not queue bounty", str(error), parent=self)

    def _work_page(self):
        self._label(
            self.content,
            "Review → approve → isolated preflight → AI patch → verification → second approval → prepare → publish.",
            10, MUTED
        ).pack(anchor="w", pady=(4, 10))
        self._label(
            self.content,
            "Every approval is deliberate. Publication opens an external pull request only after confirmation.",
            9, MUTED
        ).pack(anchor="w", pady=(0, 12))
        data = self.data or {}
        rows = data.get("attempts") or []
        view = ttk.Treeview(
            self.content, columns=("id", "title", "source", "state", "reward"),
            show="headings", style="Bounty.Treeview", height=9
        )
        for key, heading, width in [
            ("id", "#", 50), ("title", "Bounty", 330),
            ("source", "Marketplace", 110), ("state", "Stage", 150),
            ("reward", "Recorded payout", 125),
        ]:
            view.heading(key, text=heading)
            view.column(key, width=width, stretch=key == "title")
        for item in rows:
            view.insert("", "end", iid=str(item["id"]), values=(
                item["id"], item["title"][:110], item["source"],
                item["status"], _money(item["recorded_usd"])
            ))
        view.pack(fill="both", expand=True)
        self.work_tree = view
        if self._selected_attempt_id is not None and view.exists(str(self._selected_attempt_id)):
            view.selection_set(str(self._selected_attempt_id))
        view.bind("<<TreeviewSelect>>", self._work_selected)

        action_row = tk.Frame(self.content, bg=BG)
        action_row.pack(fill="x", pady=(11, 8))
        buttons = [
            ("Inspect", self.inspect_attempt),
            ("Approve attempt", self.approve_selected_attempt),
            ("Preflight", lambda: self.run_step("preflight")),
            ("Generate patch", lambda: self.run_step("patch")),
            ("Verify", lambda: self.run_step("verify")),
            ("Review bundle", self.inspect_bundle),
            ("Approve bundle", self.approve_bundle),
            ("Prepare PR", lambda: self.run_step("prepare")),
            ("Publish PR", self.publish_selected),
        ]
        for i, (name, callback) in enumerate(buttons):
            self._button(action_row, name, callback, accent=name in {"Approve attempt", "Approve bundle"}).grid(
                row=i // 3, column=i % 3, padx=(0, 8), pady=(0, 8), sticky="ew"
            )
        for col in range(3):
            action_row.columnconfigure(col, weight=1)

        followup = tk.Frame(self.content, bg=BG)
        followup.pack(fill="x", pady=(2, 8))
        self._button(followup, "Open submitted PR", self.open_attempt_pr).pack(side="left")
        self._button(followup, "Verify merged PR", self.verify_merge).pack(side="left", padx=(8, 0))
        self._button(followup, "Record payout / hours", lambda: self.navigate("activity")).pack(
            side="right"
        )
        self.work_detail = tk.Text(
            self.content, bg=PANEL, fg=TEXT, font=(FONT, 10), relief="flat",
            wrap="word", height=7, padx=12, pady=10
        )
        self.work_detail.pack(fill="both", expand=True)
        self._update_work_detail(rows)

    def _work_selected(self, event=None):
        selection = self.work_tree.selection()
        if selection:
            self._selected_attempt_id = int(selection[0])
        self._update_work_detail((self.data or {}).get("attempts") or [])

    def _update_work_detail(self, rows):
        if not hasattr(self, "work_detail"):
            return
        item = next(
            (row for row in rows if row["id"] == self._selected_attempt_id),
            None
        )
        if not item:
            detail = "Select a queued bounty. All workflow steps are unavailable until approval."
        else:
            detail = (
                f'Attempt #{item["id"]}: {item["title"]}\n'
                f'Status: {item["status"]}   |   Provider: {item["source"]}\n'
                f'Recorded payout: {_money(item["recorded_usd"])}   |   '
                f'Logged hours: {item["hours"]:.2f}\n'
                f'Pull request: {item.get("pr_url") or "not submitted"}\n'
                "Buttons enforce the existing safety state machine; not every action "
                "is available at every stage."
            )
        self.work_detail.configure(state="normal")
        self.work_detail.delete("1.0", "end")
        self.work_detail.insert("1.0", detail)
        self.work_detail.configure(state="disabled")

    def _selected_attempt(self):
        if self._selected_attempt_id is None:
            raise ValueError("Select an attempt in Coding workspace first")
        return self._selected_attempt_id

    def inspect_attempt(self):
        try:
            aid = self._selected_attempt()
            info = review_attempt(self.settings, aid)
            self._preview_window(
                f"Attempt #{aid} — immutable record",
                json.dumps(info, indent=2, sort_keys=True, default=str)[:100000],
            )
        except Exception as error:
            messagebox.showerror("Cannot inspect", str(error), parent=self)

    def _preview_window(self, title, content):
        window = tk.Toplevel(self)
        window.title(title)
        window.configure(bg=BG)
        window.geometry("870x620")
        text = tk.Text(window, bg=PANEL, fg=TEXT, relief="flat",
                       wrap="none", padx=10, pady=10, font=("Consolas", 10))
        scrollbar = tk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(fill="both", expand=True, padx=(12, 0), pady=12)
        text.insert("1.0", content)
        text.configure(state="disabled")

    def approve_selected_attempt(self):
        try:
            aid = self._selected_attempt()
            info = review_attempt(self.settings, aid)
            if info["attempt"]["status"] != "queued":
                raise ValueError("Only queued attempts can receive first approval")
            ReviewDialog(
                self, f"Approve attempt #{aid}",
                f"Read the full queued opportunity. This approves isolated preflight, "
                f"not an immediate public PR. Snapshot SHA-256: {info['record_sha256']}",
                json.dumps(info["record"], indent=2, sort_keys=True),
                required_phrase="",
                on_approve=lambda note: self._first_approved(aid, info["record_sha256"], note)
            )
        except Exception as error:
            messagebox.showerror("Cannot approve", str(error), parent=self)

    def _first_approved(self, aid, digest, note):
        approve_attempt(self.settings, aid, digest, note)
        self.refresh()

    def run_step(self, step):
        try:
            aid = self._selected_attempt()
            action = {
                "preflight": "Check the approved bounty and clone it into an isolated container?",
                "patch": "Let Codex generate a bounded patch without repository credential access?",
                "verify": "Run regression tests in an isolated container and create review evidence?",
                "prepare": "Prepare an approved pull-request package locally? Nothing will be published.",
            }[step]
            if not messagebox.askyesno(f"Run {step}", action, parent=self):
                return
            settings = dict(self.settings)
            self._run_async(
                f"Running {step} on #{aid}",
                lambda: perform_step(settings, aid, step),
                on_success=lambda result: self.navigate("work")
            )
        except Exception as error:
            messagebox.showerror("Cannot run step", str(error), parent=self)

    def inspect_bundle(self):
        try:
            aid = self._selected_attempt()
            info = bundle_preview(self.settings, aid)
            self._preview_window(
                f"Review bundle #{aid} — {info['bundle_sha256']}",
                json.dumps(info["manifest"], indent=2) + "\n\nPATCH:\n" +
                info["patch_preview"] +
                ("\n[Preview truncated: inspect full bundle before approval.]" if info["patch_truncated"] else "")
            )
        except Exception as error:
            messagebox.showerror("No review bundle", str(error), parent=self)

    def approve_bundle(self):
        try:
            aid = self._selected_attempt()
            info = bundle_preview(self.settings, aid)
            if info["attempt"]["status"] != "verified":
                raise ValueError("Verification must pass before second approval")
            if info["patch_truncated"]:
                raise ValueError(
                    "Patch preview is too long. Open full local bundle before approving."
                )
            ReviewDialog(
                self, f"Approve verified patch #{aid}",
                "Review the immutable verification manifest AND patch below. "
                "Approval does not publish; it permits preparation of an exact PR. "
                f"Bundle SHA-256: {info['bundle_sha256']}",
                json.dumps(info["manifest"], indent=2) +
                "\n\nPATCH:\n" + info["patch_preview"],
                required_phrase="",
                on_approve=lambda note: self._bundle_approved(aid, info["bundle_sha256"], note)
            )
        except Exception as error:
            messagebox.showerror("Cannot approve bundle", str(error), parent=self)

    def _bundle_approved(self, aid, digest, note):
        approve_review_bundle(self.settings, aid, digest, note)
        self.refresh()

    def publish_selected(self):
        try:
            aid = self._selected_attempt()
            info = package_preview(self.settings, aid)
            if info["attempt"]["status"] != "submission_approved":
                raise ValueError("Only prepared, approved submissions may be published")
            ReviewDialog(
                self, f"PUBLICATION — attempt #{aid}",
                "PUBLIC ACTION: this will push a branch to your fork and create a PR "
                "against an external repository. Read the exact package and PR message.",
                json.dumps(info["manifest"], indent=2) +
                "\n\nPULL REQUEST MESSAGE:\n" + info["body_preview"] +
                "\n\nPACKAGE SHA-256: " + info["package_sha256"],
                required_phrase=f"PUBLISH #{aid}",
                on_approve=lambda note: self._confirmed_publish(aid, info)
            )
        except Exception as error:
            messagebox.showerror("Cannot publish", str(error), parent=self)

    def _confirmed_publish(self, aid, info):
        settings = dict(self.settings)
        self._run_async(
            f"Publishing approved PR #{aid}",
            lambda: publish_approved(
                settings, aid, info["bundle_sha256"], info["package_sha256"]
            ),
            on_success=lambda result: self.navigate("work"),
        )

    def open_attempt_pr(self):
        try:
            info = review_attempt(self.settings, self._selected_attempt())
            url = str(info["attempt"].get("pr_url") or "")
            if not re.fullmatch(
                r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+/?", url
            ):
                raise ValueError("This attempt has no recorded published PR URL")
            webbrowser.open(url)
        except Exception as error:
            messagebox.showinfo("Cannot open PR", str(error), parent=self)

    def verify_merge(self):
        try:
            aid = self._selected_attempt()
            info = review_attempt(self.settings, aid)
            if info["attempt"]["status"] != "submitted":
                raise ValueError("Only submitted pull requests may be marked merged")
            if not messagebox.askyesno(
                "Check upstream merge",
                "Check the upstream GitHub API for a merged PR and record the result?",
                parent=self
            ):
                return
            settings = dict(self.settings)
            self._run_async(
                f"Checking GitHub merge #{aid}",
                lambda: self._confirm_merge_from_github(settings, aid),
                on_success=lambda result: self.navigate("work")
            )
        except Exception as error:
            messagebox.showerror("Cannot verify merge", str(error), parent=self)

    @staticmethod
    def _confirm_merge_from_github(settings, aid):
        from urllib.request import Request, urlopen
        info = review_attempt(settings, aid)
        url = info["attempt"]["pr_url"] or ""
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)/?", url
        )
        if not match:
            raise ValueError("Recorded PR URL is not a valid GitHub PR")
        org, repo, number = match.groups()
        token = os.environ.get(settings["github_token_env"])
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "bounty-hunter-desktop",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(
            f"https://api.github.com/repos/{org}/{repo}/pulls/{number}",
            headers=headers
        )
        with urlopen(request, timeout=settings["timeout_seconds"]) as response:
            details = json.load(response)
        if not details.get("merged_at"):
            raise ValueError("GitHub does not report this pull request as merged")
        history = History(str(workspace_paths(settings)["db"]))
        try:
            history.transition(
                aid, "merged",
                outcome_note=f'Confirmed via GitHub pull request #{number} (merged_at: {details["merged_at"]}).'
            )
        finally:
            history.close()
        return {"merged_at": details["merged_at"], "pr_url": url}

    def _settings_page(self):
        paths = workspace_paths(self.settings)
        self._label(self.content, "LOCAL WORKSPACE", 10, MUTED, True).pack(anchor="w", pady=(8, 4))
        self._label(
            self.content, str(paths["root"]), 12, TEXT, True,
        ).pack(anchor="w", pady=(0, 14))
        self._button(
            self.content, "Edit setup / platforms", self.open_setup, True
        ).pack(anchor="w", pady=(0, 18))
        self._label(
            self.content,
            "Source settings: " + ", ".join(
                name for name, enabled in self.settings["sources"].items() if enabled
            ),
            10, MUTED
        ).pack(anchor="w", pady=(0, 6))
        self._label(
            self.content,
            "Automatic scanning: " +
            (f'On, every {self.settings["scan_interval_minutes"]} min while app is open'
             if self.settings["auto_scan"] else "Off"),
            10, MUTED
        ).pack(anchor="w", pady=(0, 18))
        self._label(
            self.content, "EXECUTION REQUIREMENTS", 10, TEXT, True
        ).pack(anchor="w")
        for name, command in [
            ("Git", "git"), ("GitHub CLI (publication)", "gh"),
            ("Codex CLI (AI patch)", "codex"),
            ("Docker (isolated tests)", "docker"),
        ]:
            located = shutil.which(command)
            self._label(
                self.content,
                f'{name}: {"Detected" if located else "Not detected"}',
                10, ACCENT if located else WARNING
            ).pack(anchor="w", pady=(7, 0))
        self._label(
            self.content,
            "Tool detection is not authentication. Docker Desktop must be running, "
            "Codex must be authorized, and GitHub CLI must be signed in. "
            "Identity, payout country and age eligibility depend on each provider.",
            10, MUTED, wraplength=780, justify="left"
        ).pack(anchor="w", pady=(20, 18))
        self._button(
            self.content, "Check for Windows app updates", self.check_updates
        ).pack(anchor="w")
        self._label(
            self.content,
            "Verified updates open GitHub for review. Bounty Hunter never silently replaces its executable.",
            9, MUTED
        ).pack(anchor="w", pady=(10, 0))

    def check_updates(self):
        from .desktop_updates import check_for_updates
        self._run_async(
            "Checking official GitHub releases",
            check_for_updates,
            on_success=self._update_check_complete
        )

    def _update_check_complete(self, result):
        url = result["url"]
        if result["available"]:
            offer = f'{result["message"]} Open the official release download page?'
        else:
            offer = f'{result["message"]} Open the GitHub downloads page?'
        if messagebox.askyesno("Bounty Hunter updates", offer, parent=self):
            webbrowser.open(url)


def main():
    app = Workbench()
    app.mainloop()


if __name__ == "__main__":
    main()
