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

