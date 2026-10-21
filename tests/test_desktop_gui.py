"""Windows desktop GUI smoke check: create a real Tk root and open every screen."""
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from bounty_hunter.desktop import Workbench
from bounty_hunter.desktop_dialogs import SetupDialog
from bounty_hunter.desktop_settings import normalize_settings, workspace_paths
from bounty_hunter.history import History
from tests.test_preflight import eligible_record


@unittest.skipUnless(os.name == "nt", "Native Windows UI smoke test")
class DesktopGuiSmokeTests(unittest.TestCase):
    def test_all_main_pages_render(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"APPDATA": folder, "LOCALAPPDATA": folder}):
                try:
                    app = Workbench()
                except tk.TclError as error:
                    self.skipTest(f"Runner has no interactive Windows desktop: {error}")
                try:
                    app.withdraw()
                    for page in ("bounties", "work", "overview", "payouts",
                                 "activity", "settings", "bounties"):
                        with self.subTest(page=page):
                            app.navigate(page)
                            app.update_idletasks()
                finally:
                    app.destroy()

    def test_actionable_only_filter_preserves_original_report_indices(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = normalize_settings({"workspace": folder, "auto_scan": False})
            app = Workbench(settings=settings)
            try:
                app.withdraw()
                records = []
                for index, decision in enumerate(
                    ("reject", "approve_for_human_review", "needs_verification")
                ):
                    records.append({
                        "opportunity": {
                            "source": "algora", "source_id": str(index),
                            "title": "Opportunity " + str(index), "bounty_usd": 100,
                        },
                        "score": {
                            "eligible": index == 1, "decision": decision,
                            "expected_value_usd": 25, "confidence": 0.75,
                            "rejection_reasons": ["a blocker"] if index != 1 else [],
                        },
                    })
                app.scan_report = {
                    "opportunities": records, "source_health": [],
                    "generated_at": "2030-01-01T00:00:00Z",
                }
                app.navigate("bounties")
                self.assertEqual(app.scan_tree.get_children(), ("1",))
                app.scan_tree.selection_set("1")
                self.assertEqual(
                    app._selected_bounty()["opportunity"]["source_id"], "1"
                )
                app.toggle_actionable_filter()
                self.assertEqual(
                    app.scan_tree.get_children(), ("0", "1", "2")
                )
                app.toggle_actionable_filter()
                self.assertEqual(app.scan_tree.get_children(), ("1",))
            finally:
                app.destroy()

    def test_attempt_controls_and_setup_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = normalize_settings({"workspace": folder, "timeout_seconds": 45})
            history = History(str(workspace_paths(settings)["db"]))
            aid = history.queue_from_record(eligible_record())
            history.close()
            app = Workbench(settings=settings)
            try:
                app.withdraw()
                app._selected_attempt_id = aid
                app.navigate("work")
                app.update_idletasks()
                self.assertEqual(str(app.work_buttons["approve"]["state"]), "normal")
                self.assertEqual(str(app.work_buttons["preflight"]["state"]), "disabled")
                self.assertEqual(str(app.work_buttons["publish"]["state"]), "disabled")
                app._busy = True
                app._update_work_detail(app.data["attempts"])
                self.assertEqual(str(app.work_buttons["approve"]["state"]), "disabled")
                app._busy = False
                saved = []
                setup = SetupDialog(app, settings, on_save=saved.append)
                setup._save()
                self.assertEqual(saved[0]["timeout_seconds"], 45)
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main()

